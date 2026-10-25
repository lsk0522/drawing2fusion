"""Drawing2Fusion as an MCP server (stdio, JSON-RPC 2.0) - no extra packages.

Lets Claude Desktop / Claude Code drive the whole pipeline and close the loop itself:
read a drawing, answer or fix the spec, build in Fusion, read the verification, change the
spec, build again.

    python -m d2f.mcp_server            (Claude starts this; Fusion with the add-in must be running)

Tools
  d2f_status        Fusion bridge + add-in version
  d2f_extract       drawing (.dxf / vector .pdf) -> out/<name>/part-spec.json, issues, questions
  d2f_questions     open questions of a spec
  d2f_answer        write answers into a spec
  d2f_get_spec      read a spec (JSON)
  d2f_put_spec      replace a spec (validated first)
  d2f_validate      spec checks without Fusion
  d2f_build         build in Fusion + measure + compare (+ silhouette/section vs the drawing)
  d2f_report        verify-report.json of a build (failed checks first)
  d2f_compare_image side-by-side PNG: drawing | snapshots | checks
"""

import contextlib
import io
import json
import os
import sys
import traceback

if __package__ in (None, ""):          # started as a file: make `d2f` importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "d2f"

from d2f import cli, drawing  # noqa: E402
from d2f.bridge_client import BridgeClient, BridgeError  # noqa: E402
from d2f.spec import validate as validator  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
PROTOCOL = "2024-11-05"


def _path(p):
    p = os.path.expanduser(str(p))
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _quiet(fn, *a, **kw):
    """Run fn with stdout captured (stdout is the MCP channel). Returns (result, text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            res = fn(*a, **kw)
        except SystemExit as e:
            res = e.code if isinstance(e.code, int) else str(e.code)
    return res, buf.getvalue()


# ----------------------------------------------------------------------------- tools
def t_status(_):
    try:
        info = BridgeClient().ping()
        return {"fusion": "connected", **info}
    except BridgeError as e:
        return {"fusion": "not connected", "error": str(e),
                "hint": "Fusion을 켜고 Drawing2Fusion 애드인을 실행하세요"}


def t_extract(a):
    path = _path(a["drawing"])
    (res, text) = _quiet(drawing.extract_to, path, OUT, a.get("page"))
    if not isinstance(res, tuple):
        return {"ok": False, "error": text.strip() or str(res)}
    spec_path, spec, report = res
    issues = validator.validate(spec) + report["issues"]
    return {"ok": True, "spec": spec_path, "projection": report.get("projection"),
            "reading_png": report.get("reading_png"),
            "summary": report.get("summary"), "parameters": {k: {"value": v.get("value"),
                                                                 "source": (v.get("source") or {}).get("type"),
                                                                 "label": (v.get("source") or {}).get("label")}
                                                             for k, v in spec["parameters"].items()},
            "issues": [{"code": i["code"], "severity": i["severity"], "message": i["message"]} for i in issues],
            "questions": drawing.questions(spec)}


def t_questions(a):
    spec = validator.load(_path(a["spec"]))
    return {"questions": drawing.questions(spec)}


def t_answer(a):
    path = _path(a["spec"])
    spec = validator.load(path)
    qs = {q["id"]: q for q in drawing.questions(spec)}
    done = []
    for ans in a.get("answers", []):
        q = qs.get(ans.get("id"))
        ok = bool(q) and drawing.apply_answer(spec, q, str(ans.get("answer", "")), ans.get("value"))
        done.append({"id": ans.get("id"), "applied": ok})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    return {"applied": done, "remaining": drawing.questions(spec)}


def t_get_spec(a):
    return validator.load(_path(a["spec"]))


def t_put_spec(a):
    spec = a["content"] if isinstance(a["content"], dict) else json.loads(a["content"])
    issues = validator.validate(spec)
    errors = [i for i in issues if i["severity"] == "ERROR"]
    if errors and not a.get("force"):
        return {"written": False, "errors": errors}
    path = _path(a["spec"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    return {"written": True, "spec": path, "issues": issues}


def t_validate(a):
    spec = validator.load(_path(a["spec"]))
    issues = validator.validate(spec)
    return {"counts": validator.summarize(issues), "issues": issues}


def _summary(out_dir, code, text):
    rp = os.path.join(out_dir, "verify-report.json")
    out = {"exit_code": code, "meaning": {0: "통과", 1: "오류/치수 불일치", 2: "질문(BLOCK)으로 멈춤",
                                          3: "Fusion 연결 안 됨"}.get(code, str(code)),
           "out_dir": out_dir}
    if os.path.exists(rp):
        with open(rp, encoding="utf-8") as f:
            r = json.load(f)
        out.update({"passed": r.get("passed"), "total": r.get("total"),
                    "failed": [c for c in r.get("checks", []) if not c.get("pass")],
                    "snapshots": r.get("snapshots"), "exports": r.get("exports"),
                    "alerts": [{"code": x.get("code"), "severity": x.get("severity"), "title": x.get("title"),
                                "message": x.get("message")} for x in r.get("alerts", [])
                               if x.get("severity") in ("WARN", "ERROR", "BLOCK")]})
    else:
        out["console"] = text[-3000:]
    return out


def t_build(a):
    spec_path = _path(a["spec"])
    spec = validator.load(spec_path)
    name = (spec.get("part") or {}).get("name") or os.path.splitext(os.path.basename(spec_path))[0]
    argv = ["build", spec_path, "--out", _path(a.get("out", OUT)), "--export", a.get("export", "f3d")]
    if not a.get("snapshot", True):
        argv.append("--no-snapshot")
    if a.get("force"):
        argv.append("--force")
    code, text = _quiet(cli.main, argv)
    return _summary(os.path.join(_path(a.get("out", OUT)), name), code, text)


def t_report(a):
    out_dir = _path(a["out_dir"])
    return _summary(out_dir, None, "")


def t_compare_image(a):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import compare_image
    dest, text = _quiet(compare_image.build_sheet, _path(a["drawing_image"]), _path(a["out_dir"]))
    return {"image": dest}


TOOLS = {
    "d2f_status": (t_status, "Fusion 브리지 연결과 애드인 버전 확인", {}),
    "d2f_extract": (t_extract, "도면(.dxf / 벡터 .pdf)을 읽어 out/<이름>/part-spec.json을 만들고 파라미터·이슈·질문을 돌려줍니다",
                    {"drawing": {"type": "string", "description": "도면 경로 (프로젝트 기준 상대 경로 가능)"},
                     "page": {"type": "integer", "description": "PDF 쪽 번호(1부터). 없으면 선이 가장 많은 쪽"}}),
    "d2f_questions": (t_questions, "스펙의 BLOCK 질문 목록",
                      {"spec": {"type": "string"}}),
    "d2f_answer": (t_answer, "질문에 답을 써 넣습니다. answers: [{id, answer: '1'|'2'|숫자, value?}]",
                   {"spec": {"type": "string"},
                    "answers": {"type": "array", "items": {"type": "object"}}}),
    "d2f_get_spec": (t_get_spec, "part-spec.json 읽기", {"spec": {"type": "string"}}),
    "d2f_put_spec": (t_put_spec, "part-spec.json 덮어쓰기 (검증 오류가 있으면 쓰지 않음, force로 무시)",
                     {"spec": {"type": "string"}, "content": {"type": ["object", "string"]},
                      "force": {"type": "boolean"}}),
    "d2f_validate": (t_validate, "Fusion 없이 스펙 검사", {"spec": {"type": "string"}}),
    "d2f_build": (t_build, "Fusion에서 빌드 → 실측 → 비교 (도면에서 읽은 스펙이면 실루엣·단면도 비교). 실패한 검사를 돌려줍니다",
                  {"spec": {"type": "string"}, "export": {"type": "string", "description": "f3d,step,stl"},
                   "snapshot": {"type": "boolean"}, "force": {"type": "boolean", "description": "BLOCK 무시"}}),
    "d2f_report": (t_report, "빌드 결과(verify-report.json) 요약", {"out_dir": {"type": "string"}}),
    "d2f_compare_image": (t_compare_image, "원본 도면 이미지와 스냅샷·검증표를 한 장으로",
                          {"drawing_image": {"type": "string"}, "out_dir": {"type": "string"}}),
}
REQUIRED = {"d2f_extract": ["drawing"], "d2f_questions": ["spec"], "d2f_answer": ["spec", "answers"],
            "d2f_get_spec": ["spec"], "d2f_put_spec": ["spec", "content"], "d2f_validate": ["spec"],
            "d2f_build": ["spec"], "d2f_report": ["out_dir"], "d2f_compare_image": ["drawing_image", "out_dir"]}


def tool_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": props,
                                                          "required": REQUIRED.get(n, [])}}
            for n, (_, d, props) in TOOLS.items()]


def handle(msg):
    """One JSON-RPC message -> response dict (or None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None                                   # notifications (initialized, cancelled ...)
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "drawing2fusion", "version": "0.4.0"},
                  "instructions": "도면 → Fusion 모델 → 검증. 보통 순서: d2f_extract → (질문 있으면 d2f_answer) → "
                                  "d2f_build → 실패 검사를 보고 d2f_get_spec/d2f_put_spec 으로 고친 뒤 다시 d2f_build."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool %s" % name}}
        try:
            out = TOOLS[name][0](params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                      "isError": False}
        except Exception as e:
            result = {"content": [{"type": "text", "text": "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-1500:])}],
                      "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    cli._load_dotenv()
    os.environ.setdefault("D2F_NO_OPEN", "1")
    os.environ.setdefault("D2F_NOTIFY_CHANNELS", "fusion")      # console output would corrupt the channel
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
