"""Command dispatcher. Runs on Fusion's main thread."""

import hashlib
import importlib
import os
import sys

import adsk.core
import adsk.fusion

from . import builder, verifier
from .bridge import PROTOCOL_VERSION
from .log import log

ADDIN_VERSION = "0.3.0"
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
# Modules reloaded in place by the "reload" command (dependency order). Reloading
# keeps the module objects, so the running bridge and handlers see the new code.
RELOAD_ORDER = ["expr", "log", "selectors", "builder", "verifier", "bridge", "dispatcher"]


def code_hash(pkg_dir=_PKG_DIR):
    """sha1 of the add-in's .py files. Kept identical to d2f/bridge_client.py:addin_code_hash."""
    h = hashlib.sha1()
    for name in sorted(os.listdir(pkg_dir)):
        if name.endswith(".py"):
            h.update(name.encode("utf-8"))
            with open(os.path.join(pkg_dir, name), "rb") as f:
                h.update(f.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


CODE_HASH = code_hash()


def dispatch(cmd, args):
    app = adsk.core.Application.get()
    fn = _COMMANDS.get(cmd)
    if fn is None:
        return {"ok": False, "error": "unknown command '%s'" % cmd}
    return fn(app, args)


def _ping(app, args):
    doc = app.activeDocument
    return {"ok": True, "addin_version": ADDIN_VERSION, "protocol_version": PROTOCOL_VERSION,
            "code_hash": CODE_HASH, "fusion_version": app.version,
            "active_document": doc.name if doc else None}


def _reload(app, args):
    before = CODE_HASH
    done = []
    for name in RELOAD_ORDER:
        mod = sys.modules.get("d2f_addin." + name)
        if mod is not None:
            importlib.reload(mod)
            done.append(name)
    new = sys.modules["d2f_addin.dispatcher"].CODE_HASH
    log("reloaded %s (%s -> %s)" % (",".join(done), before, new))
    return {"ok": True, "reloaded": done, "code_hash": new,
            "addin_version": sys.modules["d2f_addin.dispatcher"].ADDIN_VERSION}


def _new_document(app, args):
    builder.prepare_design(app, new_document=True)
    return {"ok": True, "active_document": app.activeDocument.name}


def _build(app, args):
    return builder.build(app, args["spec"], args.get("options"))


def _measure(app, args):
    return verifier.measure(app, args)


def _snapshot(app, args):
    return verifier.snapshot(app, args)


def _export(app, args):
    return verifier.export(app, args)


def _alert(app, args):
    """Show an alert inside Fusion.

    Default: write to the Text Commands window (non-blocking).
    modal=true: message box; blocks the main thread until the user clicks OK,
    so the caller should pass a long _timeout_s.
    """
    sev = args.get("severity", "INFO")
    code = args.get("code", "")
    title = args.get("title", "Drawing2Fusion")
    msg = args.get("message", "")
    log("ALERT %s %s %s - %s" % (sev, code, title, msg))
    try:
        from . import palette          # not reloaded: it owns live UI handlers
        palette.push_alert({"severity": sev, "code": code, "title": title, "message": msg})
    except Exception:
        pass
    if args.get("modal"):
        icon = {"ERROR": adsk.core.MessageBoxIconTypes.CriticalIconType,
                "BLOCK": adsk.core.MessageBoxIconTypes.WarningIconType,
                "WARN": adsk.core.MessageBoxIconTypes.WarningIconType}.get(
                    sev, adsk.core.MessageBoxIconTypes.InformationIconType)
        app.userInterface.messageBox("[%s] %s\n\n%s" % (code or sev, title, msg), "Drawing2Fusion",
                                     adsk.core.MessageBoxButtonTypes.OKButtonType, icon)
    return {"ok": True}


_COMMANDS = {
    "ping": _ping,
    "new_document": _new_document,
    "build": _build,
    "measure": _measure,
    "snapshot": _snapshot,
    "export": _export,
    "alert": _alert,
    "reload": _reload,
}
