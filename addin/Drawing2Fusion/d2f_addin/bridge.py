"""HTTP bridge between external processes and Fusion's main thread.

This module deliberately does NOT import adsk, so it can be unit-tested
outside Fusion. The add-in entry point wires `fire` to
`app.fireCustomEvent`, and calls `BridgeServer.handle_main_thread` from the
CustomEvent handler (which Fusion runs on the main thread).

Flow:
    HTTP thread  -> register pending request -> fire(json) -> wait(timeout)
    main thread  -> handle_main_thread(json) -> dispatch -> set result
    HTTP thread  -> wake up -> write JSON response
"""

import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_S = 120.0
MAX_BODY_BYTES = 20 * 1024 * 1024

# Routes that are answered on the HTTP thread without touching Fusion.
LOCAL_ROUTES = {"/health"}

# Map of URL path -> dispatcher command name.
ROUTES = {
    "/ping": "ping",
    "/new_document": "new_document",
    "/build": "build",
    "/measure": "measure",
    "/snapshot": "snapshot",
    "/export": "export",
    "/alert": "alert",
    "/reload": "reload",
    "/close_document": "close_document",
}


def token_path():
    return os.path.join(os.path.expanduser("~"), ".d2f", "token")


def load_or_create_token(path=None):
    """Read the shared bearer token, creating it on first run."""
    path = path or token_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tok = secrets.token_hex(24)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


class _Pending:
    __slots__ = ("event", "result", "created")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.created = time.time()


class _ExclusiveServer(ThreadingHTTPServer):
    """One bridge per port. HTTPServer sets SO_REUSEADDR, which on Windows lets a second
    Fusion add-in instance bind the same port and steal half the requests (seen when the
    add-in was registered twice). Refuse instead, so the second instance reports E503."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class BridgeServer:
    """Owns the HTTP server thread and the pending-request table."""

    def __init__(self, fire, dispatch, token, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 timeout_s=DEFAULT_TIMEOUT_S, log=None):
        """
        fire(payload_json: str)          -> hands a request to the main thread
        dispatch(cmd: str, args: dict)   -> runs on the main thread, returns dict
        """
        self._fire = fire
        self._dispatch = dispatch
        self._token = token
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._log = log or (lambda msg: None)
        self._pending = {}
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None

    # ------------------------------------------------------------------ HTTP side
    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "D2FBridge/0.1"

            def log_message(self, fmt, *args):  # silence default stderr logging
                bridge._log("http: " + (fmt % args))

            def _send(self, status, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                auth = self.headers.get("Authorization", "")
                return secrets.compare_digest(auth, "Bearer " + bridge._token)

            def _handle(self, method):
                path = self.path.split("?", 1)[0]
                if path in LOCAL_ROUTES:
                    return self._send(200, {"ok": True, "protocol_version": PROTOCOL_VERSION})
                if not self._authorized():
                    return self._send(401, {"ok": False, "code": "E501", "error": "unauthorized"})
                cmd = ROUTES.get(path)
                if cmd is None:
                    return self._send(404, {"ok": False, "error": "unknown route " + path})
                args = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY_BYTES:
                        return self._send(413, {"ok": False, "error": "body too large"})
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        try:
                            args = json.loads(raw.decode("utf-8"))
                        except ValueError as e:
                            return self._send(400, {"ok": False, "error": "bad json: %s" % e})
                timeout = float(args.pop("_timeout_s", bridge.timeout_s)) if isinstance(args, dict) else bridge.timeout_s
                status, result = bridge.call(cmd, args, timeout)
                return self._send(status, result)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._httpd = _ExclusiveServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]  # resolves port=0 in tests
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="d2f-bridge", daemon=True)
        self._thread.start()
        self._log("bridge listening on %s:%d" % (self.host, self.port))

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        # Wake any waiters so their HTTP threads can exit.
        with self._lock:
            for p in self._pending.values():
                p.result = {"ok": False, "code": "E501", "error": "bridge stopped"}
                p.event.set()
            self._pending.clear()

    def call(self, cmd, args, timeout_s):
        """Called from an HTTP thread. Returns (http_status, result_dict)."""
        req_id = uuid.uuid4().hex
        pending = _Pending()
        with self._lock:
            self._pending[req_id] = pending
        try:
            self._fire(json.dumps({"id": req_id, "cmd": cmd, "args": args}))
        except Exception as e:  # Fusion refused the event
            with self._lock:
                self._pending.pop(req_id, None)
            return 500, {"ok": False, "code": "E501", "error": "fire failed: %s" % e}

        if not pending.event.wait(timeout_s):
            with self._lock:
                self._pending.pop(req_id, None)
            return 504, {"ok": False, "code": "E502",
                         "error": "main thread did not answer within %.0fs" % timeout_s}
        result = pending.result or {"ok": False, "error": "empty result"}
        return (200 if result.get("ok", True) else 500), result

    # ------------------------------------------------------------- main thread side
    def handle_main_thread(self, payload_json):
        """Called from the CustomEvent handler on Fusion's main thread."""
        try:
            payload = json.loads(payload_json)
        except ValueError:
            self._log("dropping malformed payload")
            return
        req_id = payload.get("id")
        with self._lock:
            pending = self._pending.get(req_id)
        if pending is None:
            # HTTP waiter already timed out; skip to save main-thread time.
            self._log("skipping stale request %s (%s)" % (str(req_id)[:8], payload.get("cmd")))
            return
        started = time.time()
        try:
            result = self._dispatch(payload.get("cmd"), payload.get("args") or {})
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            result.setdefault("ok", True)
        except Exception as e:
            import traceback
            result = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
        result["elapsed_s"] = round(time.time() - started, 3)
        with self._lock:
            pending = self._pending.pop(req_id, None)
        if pending is not None:
            pending.result = result
            pending.event.set()
