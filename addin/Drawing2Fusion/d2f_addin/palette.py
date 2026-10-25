"""Drawing2Fusion palette inside Fusion: pick a drawing, watch the run, answer questions.

The palette never models anything itself. It starts the same command line pipeline
(`python -m d2f run ...`) in a background process; that process talks to this add-in over the
localhost bridge exactly as run.bat does. Its console output is streamed into the palette, and
when it stops on questions (exit code 2) the questions are shown as a form; the answers are
written into out/<part>/part-spec.json and the build continues with `d2f build`.

Threads: the watcher thread only reads the process output; every Fusion / palette call happens
on the main thread through the "d2f.palette" custom event.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import traceback

import adsk.core

from .log import log

PALETTE_ID = "d2fPalette"
CMD_ID = "d2fShowPalette"
EVENT_ID = "d2f.palette"
PANEL_ID = "SolidScriptsAddinsPanel"

_app = None
_ui = None
_handlers = []
_event = None
_state = {"proc": None, "spec": None, "out_root": None, "drawing": None}


# ----------------------------------------------------------------------------- paths
def project_root():
    """drawing2fusion folder: the add-in folder is <root>/addin/Drawing2Fusion (often via a junction)."""
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if os.path.isdir(os.path.join(root, "d2f")):
        return root
    hint = os.path.join(os.path.expanduser("~"), ".d2f", "project_root")
    if os.path.exists(hint):
        with open(hint, encoding="utf-8") as f:
            p = f.read().strip()
        if os.path.isdir(os.path.join(p, "d2f")):
            return p
    return root


def python_cmd():
    """A Python 3.9+ outside Fusion (same order as tools/find_python.bat)."""
    if shutil.which("py"):
        return ["py", "-3"]
    for name in ("python", "python3"):
        exe = shutil.which(name)
        if exe and "WindowsApps" not in exe:
            return [exe]
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Autodesk", "webdeploy", "production")
    if os.path.isdir(base):
        for d in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, d, "Python", "python.exe")
            if os.path.exists(exe):
                return [exe]
    return [sys.executable]


# ----------------------------------------------------------------------------- palette messaging
def _send(action, data):
    pal = _ui.palettes.itemById(PALETTE_ID) if _ui else None
    if pal:
        try:
            pal.sendInfoToHTML(action, json.dumps(data, ensure_ascii=False))
        except Exception:
            log("palette send failed:\n" + traceback.format_exc())


def push_alert(alert):
    """Called by the dispatcher for every /alert the pipeline sends (main thread)."""
    _send("alert", alert)


def _fire(kind, **data):
    data["type"] = kind
    _app.fireCustomEvent(EVENT_ID, json.dumps(data, ensure_ascii=False))


# ----------------------------------------------------------------------------- running the pipeline
def _start(args, label):
    if _state["proc"] is not None and _state["proc"].poll() is None:
        _send("log", {"line": "⏳ 이미 실행 중입니다. 끝난 뒤 다시 눌러 주세요."})
        return
    root = project_root()
    cmd = python_cmd() + ["-m", "d2f"] + args
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", D2F_NO_OPEN="1")
    log("palette run: %s" % " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        _send("log", {"line": "🔴 실행 실패: %s" % e})
        return
    _state["proc"] = proc
    _send("started", {"label": label, "cmd": " ".join(cmd)})

    def watch():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                _fire("log", line=line)
        proc.wait()
        _fire("done", code=proc.returncode)
    threading.Thread(target=watch, name="d2f-palette-watch", daemon=True).start()


def _run_drawing(path):
    root = project_root()
    out_root = os.path.join(root, "out")
    _state.update(drawing=path, out_root=out_root)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        try:
            with open(path, encoding="utf-8") as f:
                name = (json.load(f).get("part") or {}).get("name")
        except Exception:
            name = None
        name = name or os.path.splitext(os.path.basename(path))[0]
    else:
        name = os.path.splitext(os.path.basename(path))[0]
    _state["spec"] = os.path.join(out_root, name, "part-spec.json")
    _start(["run", path, "--no-ask", "--out", out_root, "--export", "f3d"], os.path.basename(path))


def _on_done(code):
    _state["proc"] = None
    spec = _state.get("spec")
    payload = {"code": code, "out_dir": os.path.dirname(spec) if spec else None}
    report = os.path.join(payload["out_dir"] or "", "verify-report.json")
    if code in (0, 1) and os.path.exists(report):
        try:
            with open(report, encoding="utf-8") as f:
                r = json.load(f)
            payload["passed"], payload["total"] = r.get("passed"), r.get("total")
            payload["failed"] = [c.get("message") for c in r.get("checks", []) if not c.get("pass")][:12]
        except Exception:
            pass
    if code == 2 and spec and os.path.exists(spec):
        payload["questions"] = _questions(spec)
    png = os.path.join(payload["out_dir"] or "", "reading.png")
    if payload["out_dir"] and os.path.exists(png) and os.path.getsize(png) < 6 * 1024 * 1024:
        try:
            import base64
            with open(png, "rb") as f:
                payload["reading"] = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        except Exception:
            pass
    _send("done", payload)


def _questions(spec_path):
    root = project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from d2f import drawing    # pure Python, no Fusion calls
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    return drawing.questions(spec)


def _answer(data):
    spec_path = _state.get("spec")
    if not spec_path or not os.path.exists(spec_path):
        _send("log", {"line": "🔴 답을 쓸 스펙 파일이 없습니다."})
        return
    root = project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from d2f import drawing
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    qs = {q["id"]: q for q in drawing.questions(spec)}
    applied = 0
    for a in data.get("answers", []):
        q = qs.get(a.get("id"))
        if q and drawing.apply_answer(spec, q, str(a.get("answer", "")), a.get("value")):
            applied += 1
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    _send("log", {"line": "✏️ 답 %d개를 스펙에 적었습니다 → 다시 빌드합니다" % applied})
    _start(["build", spec_path, "--out", _state["out_root"], "--export", "f3d"], os.path.basename(spec_path))


# ----------------------------------------------------------------------------- handlers
class _PaletteEvent(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            d = json.loads(args.additionalInfo)
            if d["type"] == "log":
                _send("log", {"line": d["line"]})
            elif d["type"] == "done":
                _on_done(d["code"])
        except Exception:
            log("palette event error:\n" + traceback.format_exc())


class _HTMLHandler(adsk.core.HTMLEventHandler):
    def notify(self, args):
        try:
            ev = adsk.core.HTMLEventArgs.cast(args)
            action, data = ev.action, json.loads(ev.data or "{}")
            if action == "pick":
                dlg = _ui.createFileDialog()
                dlg.title = "도면 또는 스펙 선택"
                dlg.filter = "도면/스펙 (*.dxf;*.pdf;*.json);;DXF (*.dxf);;PDF (*.pdf);;part-spec (*.json)"
                dlg.initialDirectory = os.path.join(project_root(), "samples", "drawings")
                if dlg.showOpen() == adsk.core.DialogResults.DialogOK:
                    _run_drawing(dlg.filename)
            elif action == "answers":
                _answer(data)
            elif action == "open_folder":
                p = data.get("path") or (os.path.dirname(_state["spec"]) if _state.get("spec") else None)
                if p and os.path.isdir(p):
                    os.startfile(p)
            elif action == "rerun" and _state.get("drawing"):
                _run_drawing(_state["drawing"])
            elif action == "ready":
                _send("hello", {"root": project_root(), "python": " ".join(python_cmd())})
            ev.returnData = "OK"
        except Exception:
            log("palette html error:\n" + traceback.format_exc())


class _ShowCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            h = _ShowExecute()
            cmd.execute.add(h)
            _handlers.append(h)
        except Exception:
            log("palette command error:\n" + traceback.format_exc())


class _ShowExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        show()


def show():
    pal = _ui.palettes.itemById(PALETTE_ID)
    if pal is None:
        html = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "palette", "index.html")
        pal = _ui.palettes.add(PALETTE_ID, "Drawing2Fusion", html.replace("\\", "/"), True, True, True, 380, 620)
        pal.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
        h = _HTMLHandler()
        pal.incomingFromHTML.add(h)
        _handlers.append(h)
    pal.isVisible = True


def start(app):
    """Register the palette command (ADD-INS panel button) and the palette event."""
    global _app, _ui, _event
    _app, _ui = app, app.userInterface
    try:
        app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _event = app.registerCustomEvent(EVENT_ID)
    h = _PaletteEvent()
    _event.add(h)
    _handlers.append(h)
    cdef = _ui.commandDefinitions.itemById(CMD_ID)
    if cdef is None:
        cdef = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID, "Drawing2Fusion", "도면(DXF/PDF)이나 스펙으로 모델을 만들고 검증합니다", "")
    hc = _ShowCreated()
    cdef.commandCreated.add(hc)
    _handlers.append(hc)
    panel = _ui.allToolbarPanels.itemById(PANEL_ID)
    if panel is not None and panel.controls.itemById(CMD_ID) is None:
        ctrl = panel.controls.addCommand(cdef)
        ctrl.isPromotedByDefault = True


def stop(app):
    global _event
    ui = app.userInterface
    try:
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        c = panel.controls.itemById(CMD_ID) if panel else None
        if c:
            c.deleteMe()
        cdef = ui.commandDefinitions.itemById(CMD_ID)
        if cdef:
            cdef.deleteMe()
        pal = ui.palettes.itemById(PALETTE_ID)
        if pal:
            pal.deleteMe()
        if _event is not None:
            app.unregisterCustomEvent(EVENT_ID)
            _event = None
        _handlers.clear()
    except Exception:
        log("palette stop error:\n" + traceback.format_exc())
