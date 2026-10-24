"""Drawing2Fusion add-in entry point.

Starts a localhost HTTP bridge on a background thread. Each request is handed
to Fusion's main thread through a CustomEvent, because the Fusion API may
only be called from the main thread.

Shift+S -> Add-Ins -> Drawing2Fusion -> Run
Text Commands window shows: [d2f] bridge listening on 127.0.0.1:8765
"""

import os
import sys
import traceback

import adsk.core

# Make `d2f_addin` importable whether Fusion loads this file as a package
# member or as a top-level module.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

for _m in [m for m in list(sys.modules) if m == "d2f_addin" or m.startswith("d2f_addin.")]:
    # Stop -> Run should pick up edited code without restarting Fusion.
    del sys.modules[_m]

from d2f_addin import bridge as _bridge  # noqa: E402
from d2f_addin import dispatcher as _dispatcher  # noqa: E402
from d2f_addin import palette as _palette  # noqa: E402
from d2f_addin.log import log  # noqa: E402

EVENT_ID = "d2f.command"

_app = None
_server = None
_event = None
_handlers = []  # keep handler objects alive


class _CommandEventHandler(adsk.core.CustomEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            _server.handle_main_thread(args.additionalInfo)
        except Exception:
            log("handler error:\n" + traceback.format_exc())


def _port_in_use(host, port):
    import socket
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def run(context):
    global _app, _server, _event
    try:
        _app = adsk.core.Application.get()
        host = os.environ.get("D2F_BRIDGE_HOST", _bridge.DEFAULT_HOST)
        port = int(os.environ.get("D2F_BRIDGE_PORT", _bridge.DEFAULT_PORT))

        if _port_in_use(host, port):
            # another Drawing2Fusion (e.g. the add-in registered twice) already serves this port:
            # leave it alone - unregistering the shared custom event would break it
            log("not started: %s:%d is already served by another Drawing2Fusion instance" % (host, port))
            return
        try:
            _app.unregisterCustomEvent(EVENT_ID)  # left over from a crashed run
        except Exception:
            pass
        _event = _app.registerCustomEvent(EVENT_ID)
        handler = _CommandEventHandler()
        _event.add(handler)
        _handlers.append(handler)

        token = _bridge.load_or_create_token()
        _server = _bridge.BridgeServer(
            fire=lambda payload: _app.fireCustomEvent(EVENT_ID, payload),
            dispatch=_dispatcher.dispatch,
            token=token, host=host, port=port, log=log)
        try:
            _server.start()
        except OSError as e:
            # another Drawing2Fusion (e.g. the add-in registered twice) already owns the port
            log("bridge NOT started: port %d busy (%s) - another Drawing2Fusion instance is running" % (port, e))
            _server = None
            return
        try:
            _palette.start(_app)
        except Exception:
            log("palette start failed:\n" + traceback.format_exc())
        log("Drawing2Fusion %s started, token file: %s"
            % (_dispatcher.ADDIN_VERSION, _bridge.token_path()))
    except Exception:
        msg = traceback.format_exc()
        log("start failed:\n" + msg)
        try:
            _app.userInterface.messageBox("Drawing2Fusion failed to start:\n" + msg)
        except Exception:
            pass


def stop(context):
    global _server, _event
    try:
        if _server is not None:
            _palette.stop(_app)
    except Exception:
        log("palette stop failed:\n" + traceback.format_exc())
    try:
        if _server is not None:
            _server.stop()
            _server = None
        if _event is not None:
            for h in _handlers:
                try:
                    _event.remove(h)
                except Exception:
                    pass
            _handlers.clear()
            _app.unregisterCustomEvent(EVENT_ID)
            _event = None
        log("Drawing2Fusion stopped")
    except Exception:
        log("stop failed:\n" + traceback.format_exc())
