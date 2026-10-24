"""HTTP client for the Drawing2Fusion add-in bridge (stdlib only)."""

import hashlib
import json
import os
import urllib.error
import urllib.request

ADDIN_PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "addin", "Drawing2Fusion", "d2f_addin")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROTOCOL_VERSION = 1


def addin_code_hash(pkg_dir=ADDIN_PKG_DIR):
    """Same algorithm as d2f_addin/dispatcher.py:code_hash."""
    h = hashlib.sha1()
    for name in sorted(os.listdir(pkg_dir)):
        if name.endswith(".py"):
            h.update(name.encode("utf-8"))
            with open(os.path.join(pkg_dir, name), "rb") as f:
                h.update(f.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


class BridgeError(Exception):
    def __init__(self, code, message, payload=None):
        super().__init__("[%s] %s" % (code, message))
        self.code = code
        self.payload = payload or {}


def _token_path():
    return os.path.join(os.path.expanduser("~"), ".d2f", "token")


def read_token():
    tok = os.environ.get("D2F_BRIDGE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(_token_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        raise BridgeError("E501", "token file %s not found - is the Drawing2Fusion add-in running "
                                  "in Fusion?" % _token_path())


class BridgeClient:
    def __init__(self, host=None, port=None, token=None, timeout_s=180.0):
        self.host = host or os.environ.get("D2F_BRIDGE_HOST", DEFAULT_HOST)
        self.port = int(port or os.environ.get("D2F_BRIDGE_PORT", DEFAULT_PORT))
        self._token = token
        self.timeout_s = timeout_s

    @property
    def base(self):
        return "http://%s:%d" % (self.host, self.port)

    def _request(self, method, path, body=None, timeout_s=None):
        token = self._token or read_token()
        data = None
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            body = dict(body)
            if timeout_s is not None:
                body["_timeout_s"] = timeout_s
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        wait = (timeout_s or self.timeout_s) + 10
        try:
            with urllib.request.urlopen(req, timeout=wait) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {"error": str(e)}
            code = payload.get("code") or ("E502" if e.code == 504 else "E501")
            raise BridgeError(code, payload.get("error", str(e)), payload)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise BridgeError("E501", "cannot reach Fusion bridge at %s (%s). Start Fusion and run "
                                      "the Drawing2Fusion add-in." % (self.base, e))

    # ---- commands
    def ping(self):
        res = self._request("GET", "/ping")
        if res.get("protocol_version") != PROTOCOL_VERSION:
            raise BridgeError("E503", "protocol mismatch: add-in %s, cli %s"
                              % (res.get("protocol_version"), PROTOCOL_VERSION), res)
        return res

    def reload(self):
        return self._request("POST", "/reload", {})

    def sync_code(self, ping_result=None):
        """Reload the add-in's modules when its code differs from this checkout.

        Returns (status, message): status is "same", "reloaded", "old_addin" or "unknown".
        """
        info = ping_result or self.ping()
        remote = info.get("code_hash")
        try:
            local = addin_code_hash()
        except OSError:
            return "unknown", "local add-in folder not found"
        if remote is None:
            return "old_addin", ("Fusion에서 실행 중인 애드인이 옛 버전이라 자동 갱신을 못 합니다. "
                                 "Fusion을 다시 켜거나 추가 기능에서 Drawing2Fusion을 중지→실행 해 주세요.")
        if remote == local:
            return "same", ""
        res = self.reload()
        if res.get("code_hash") != local:
            return "unknown", "reloaded but hash is %s (local %s)" % (res.get("code_hash"), local)
        return "reloaded", "애드인 코드가 바뀌어 다시 불러왔습니다 (%s → %s)" % (remote, local)

    def new_document(self):
        return self._request("POST", "/new_document", {})

    def build(self, spec, options=None, timeout_s=300.0):
        return self._request("POST", "/build", {"spec": spec, "options": options or {}}, timeout_s)

    def measure(self, features=None, mesh=False, sections=None):
        body = {"features": list(features or [])}
        if mesh:
            body["mesh"] = True
        if sections:
            body["sections"] = sections
        return self._request("POST", "/measure", body)

    def snapshot(self, out_dir, views=None, width=1200, height=900):
        return self._request("POST", "/snapshot", {"dir": os.path.abspath(out_dir),
                                                   "views": views or ["front", "top", "right", "iso"],
                                                   "width": width, "height": height})

    def export(self, path, fmt=None):
        return self._request("POST", "/export", {"path": os.path.abspath(path), "format": fmt})

    def alert(self, severity, code, title, message, modal=False):
        return self._request("POST", "/alert", {"severity": severity, "code": code, "title": title,
                                                "message": message, "modal": modal},
                             timeout_s=600.0 if modal else None)
