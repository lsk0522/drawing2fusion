"""d2f command line.

  python -m d2f ping
  python -m d2f validate samples/specs/plate_holes.json
  python -m d2f build    samples/specs/plate_holes.json --live 500 --export f3d,step
  python -m d2f verify   samples/specs/plate_holes.json      (measure the open document)

Exit codes: 0 all good, 1 errors / mismatches, 2 blocked (needs user input), 3 bridge unreachable.
"""

import argparse
import datetime
import json
import os
import sys
import time

from . import compare as comparator
from .bridge_client import BridgeClient, BridgeError
from .notify import Notifier, make_alert
from .spec import validate as validator

EXIT_OK, EXIT_ERR, EXIT_BLOCK, EXIT_BRIDGE = 0, 1, 2, 3


def _load_dotenv(path=None):
    root_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    path = path or (".env" if os.path.exists(".env") else root_env)
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _run_id(name):
    return "%s_%s" % (datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S"), name)


# --------------------------------------------------------------------------- commands
def cmd_ping(args):
    try:
        res = BridgeClient().ping()
    except BridgeError as e:
        print("🔴 %s" % e)
        return EXIT_BRIDGE
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_validate(args):
    spec = validator.load(args.spec)
    issues = validator.validate(spec, args.min_confidence)
    _print_issues(issues)
    counts = validator.summarize(issues)
    print("ERROR %(ERROR)d · BLOCK %(BLOCK)d · WARN %(WARN)d" % counts)
    if counts["ERROR"]:
        return EXIT_ERR
    return EXIT_BLOCK if counts["BLOCK"] else EXIT_OK


def _print_issues(issues):
    icon = {"ERROR": "🔴", "BLOCK": "🟠", "WARN": "🟡", "INFO": "ℹ️"}
    for i in issues:
        print("%s [%s] %s" % (icon.get(i["severity"], "•"), i["code"], i["message"]))
        q = i.get("question")
        if q:
            print("   ❓ " + q.get("text", ""))
            for n, opt in enumerate(q.get("options") or [], 1):
                print("      %d) %s" % (n, opt))


def cmd_build(args, measure_only=False):
    spec = validator.load(args.spec)
    name = (spec.get("part") or {}).get("name") or os.path.splitext(os.path.basename(args.spec))[0]
    out_dir = os.path.abspath(os.path.join(args.out, name))
    run_id = _run_id(name)
    started = time.time()

    client = BridgeClient()
    try:
        info = client.ping()
        sync_status, sync_msg = client.sync_code(info)
    except BridgeError as e:
        print("🔴 [%s] %s" % (e.code, e))
        return EXIT_BRIDGE
    notifier = Notifier(part_name=name, bridge=None if args.no_fusion_alert else client)
    if sync_status == "reloaded":
        notifier.send(make_alert(run_id, "", "INFO", "connect", "애드인 갱신", sync_msg))
    elif sync_status == "old_addin":
        notifier.send(make_alert(run_id, "E503", "WARN", "connect", "애드인 재실행 필요", sync_msg))

    def alert(code, sev, stage, title, msg, **kw):
        notifier.send(make_alert(run_id, code, sev, stage, title, msg, **kw))

    report = {"run_id": run_id, "spec": os.path.abspath(args.spec), "out_dir": out_dir}
    _write(os.path.join(out_dir, "part-spec.json"), spec)

    # 1. validate ---------------------------------------------------------------
    issues = validator.validate(spec, args.min_confidence) + list(getattr(args, "extra_issues", None) or [])
    _write(os.path.join(out_dir, "validation.json"), issues)
    for i in _group_inferred(issues):
        title = "도면 해석 참고" if i["severity"] == "INFO" and i.get("stage") == "extract" else \
            _VALIDATE_TITLES.get(i["code"], "스펙 검증")
        alert(i["code"], i["severity"], "validate", title,
              i["message"], where=i.get("where"), question=i.get("question"))
    counts = validator.summarize(issues)
    if counts["ERROR"] or (counts["BLOCK"] and not args.force):
        reason = "스펙 오류" if counts["ERROR"] else "확인이 필요한 값"
        alert("", "BLOCK" if not counts["ERROR"] else "ERROR", "validate", "빌드 중단",
              "%s가 있어 Fusion 모델링을 시작하지 않았습니다. part-spec.json을 고친 뒤 다시 실행하세요%s."
              % (reason, "" if counts["ERROR"] else " (무시하려면 --force)"))
        return EXIT_ERR if counts["ERROR"] else EXIT_BLOCK

    # 2. build ------------------------------------------------------------------
    build = None
    if not measure_only:
        alert("", "INFO", "build", "모델링 시작", "%d개 피처" % len(spec.get("features", [])))
        try:
            build = client.build(spec, {"new_document": not args.reuse_document,
                                        "live_delay_ms": args.live, "stop_on_error": True})
        except BridgeError as e:
            alert(e.code, "ERROR", "build", "Fusion 응답 오류", str(e))
            return EXIT_BRIDGE
        _write(os.path.join(out_dir, "build-log.json"), build)
        for f in build.get("features", []):
            if f["status"] == "info" and f["id"] == "(parameters)":
                alert("", "INFO", "build", "파라미터 이름 변경",
                      "Fusion 단위 기호와 겹치는 이름이라 Fusion 안에서만 바꿔 등록했습니다 (%s). "
                      "스펙과 검증은 원래 이름을 그대로 씁니다." % f.get("message", "").split(": ", 1)[-1])
            if f.get("note"):
                alert("", "INFO", "build", "자동 보정", "%s: %s" % (f["id"], f["note"]), where={"feature": f["id"]})
            if f["status"] == "info":
                pass
            elif f["status"] in ("failed", "error"):
                alert(f.get("code", "E302"), "ERROR", "build", "피처 생성 실패", "%s (%s): %s"
                      % (f["id"], f.get("type", ""), f.get("message", "")), where={"feature": f["id"]})
            elif f["status"] == "warning":
                alert(f.get("code", "E303"), "WARN", "build",
                      "스케치 미완전 구속" if f.get("code") == "E304" else "피처 경고",
                      "%s: %s" % (f["id"], f.get("message", "fully constrained = %s"
                                                % f.get("fully_constrained"))),
                      where={"feature": f["id"]})
        report["build_ok"] = build.get("build_ok")

    # 3. measure + compare -------------------------------------------------------
    rows = []
    try:
        measure = client.measure(comparator.checked_features(spec))
        _write(os.path.join(out_dir, "measurements.json"), measure)
        rows = comparator.compare(spec, measure)
    except BridgeError as e:
        alert(e.code, "ERROR", "verify", "측정 실패", str(e))
    for r in rows:
        if not r["pass"]:
            alert(r["code"], r["severity"], "verify", r["title"], r["message"],
                  where={"check": r["check"]},
                  suggestion="빌드 로그(build-log.json)에서 해당 피처와 파라미터 값을 확인하세요."
                  if r["code"] != "E407" else "Fusion 내부 단위는 cm입니다. mm 값을 그대로 넘겼는지 확인하세요.")
    # 3b. silhouette vs drawing (independent of the spec) ----------------------------
    xrep = _load_extract_report(args)
    sil = (xrep or {}).get("silhouettes")
    region = (xrep or {}).get("section_regions")
    if sil or region:
        try:
            from . import silhouette
            mesh_measure = client.measure([], mesh=bool(sil),
                                          sections=[{"plane": region["plane"], "offset": region.get("offset", 0)}]
                                          if region else None)
            srows = silhouette.compare_all(sil, mesh_measure) if sil else []
            if region:
                srows.append(silhouette.compare_section(region, mesh_measure))
            rows += srows
            for r in srows:
                if not r["pass"]:
                    alert(r["code"], r["severity"], "verify", r["title"], r["message"], where={"check": r["check"]},
                          suggestion="도면의 그 뷰와 스냅샷을 비교해 보세요 (compare.png). 곡선을 직선으로 근사했거나 "
                                     "형상을 잘못 읽었을 수 있습니다.")
        except BridgeError as e:
            alert(e.code, "WARN", "verify", "실루엣 측정 실패", str(e))
    if not spec.get("checks"):
        alert("E401", "WARN", "verify", "검증 항목 없음",
              "spec에 checks가 없어 치수 검증을 하지 못했습니다.")

    # 4. snapshots / export --------------------------------------------------------
    if not args.no_snapshot:
        try:
            report["snapshots"] = client.snapshot(os.path.join(out_dir, "snapshots")).get("files")
        except BridgeError as e:
            alert(e.code, "WARN", "snapshot", "스냅샷 실패", str(e))
    exports = {}
    for fmt in [f.strip() for f in (args.export or "").split(",") if f.strip()]:
        try:
            res = client.export(os.path.join(out_dir, "%s.%s" % (name, fmt)), fmt)
            exports[fmt] = res.get("path")
        except BridgeError as e:
            alert(e.code, "WARN", "export", "내보내기 실패", "%s: %s" % (fmt, e))
    report["exports"] = exports
    if getattr(args, "close_after", False) and not args.reuse_document:
        try:
            client.close_document()
        except BridgeError as e:
            alert(e.code, "WARN", "export", "문서 닫기 실패", str(e))

    # 5. report -------------------------------------------------------------------
    passed = sum(1 for r in rows if r["pass"])
    failed = [r for r in rows if not r["pass"] and r["severity"] == "ERROR"]
    warns = [a for a in notifier.sent if a["severity"] == "WARN"]
    build_failed = build is not None and not build.get("build_ok")
    report.update({"checks": rows, "passed": passed, "total": len(rows),
                   "elapsed_s": round(time.time() - started, 1), "alerts": notifier.sent,
                   "notify_failures": notifier.failures})
    _write(os.path.join(out_dir, "verify-report.json"), report)

    summary = "치수 검증 %d/%d 통과 · 경고 %d건 · %.1f초 · 결과: %s" % (
        passed, len(rows), len(warns), report["elapsed_s"], out_dir)
    if failed or build_failed:
        alert("", "ERROR", "done", "완료 (문제 있음)", summary)
        return EXIT_ERR
    alert("", "DONE", "done", "완료", summary)
    return EXIT_OK


def _load_extract_report(args):
    """The reader's report saved next to the spec (out/<name>/extract-report.json)."""
    rep = getattr(args, "extract_report", None)
    if rep is None:
        path = os.path.join(os.path.dirname(os.path.abspath(args.spec)), "extract-report.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    rep = json.load(f)
            except (OSError, ValueError):
                rep = None
    return rep


_VALIDATE_TITLES = {
    "E105": "판독 신뢰도 낮음", "E107": "치수 문자와 형상 불일치", "E201": "스펙 형식 오류",
    "E202": "필수 치수 누락", "E205": "형상 불가능 예상", "E206": "정의되지 않은 참조",
    "E207": "추론값 사용", "E208": "단면도", "E204": "뷰 크기 불일치", "E102": "뷰 판별 실패",
}


def _group_inferred(issues, limit=3):
    """Many 'inferred parameter' warnings (an undimensioned drawing) become one alert."""
    inferred = [i for i in issues if i["code"] == "E207" and i.get("stage") != "extract"
                and isinstance(i.get("where"), dict) and "parameter" in i["where"]]
    if len(inferred) <= limit:
        return issues
    names = [i["where"]["parameter"] for i in inferred]
    merged = dict(inferred[0], where={"parameters": names},
                  message="치수 표기가 없어 형상에서 잰 값 %d개를 썼습니다: %s — 실제 치수와 다르면 part-spec.json에서 고치세요"
                  % (len(names), ", ".join(names)))
    out, done = [], False
    for i in issues:
        if i in inferred:
            if not done:
                out.append(merged)
                done = True
        else:
            out.append(i)
    return out


def cmd_extract(args):
    from . import drawing
    spec_path, spec, report = drawing.extract_to(args.drawing, os.path.abspath(args.out), getattr(args, "page", None))
    drawing.print_summary(spec, report)
    print("\n→ %s" % spec_path)
    blocks = [i for i in validator.validate(spec, args.min_confidence) + report["issues"] if i["severity"] in ("BLOCK", "ERROR")]
    return EXIT_BLOCK if blocks else EXIT_OK


def cmd_run(args):
    """Drawing or spec -> (questions) -> build -> verify."""
    from . import drawing
    path = args.drawing
    if path.lower().endswith(".json"):
        spec_path = path
    else:
        spec_path, spec, report = drawing.extract_to(path, os.path.abspath(args.out), getattr(args, "page", None))
        drawing.print_summary(spec, report)
        # drawing-reading findings travel with the build: alerts + verify-report
        args.extra_issues = [dict(i, stage="extract") for i in report["issues"]]
        args.extract_report = report
    interactive = args.ask or (not args.no_ask and sys.stdin.isatty())
    if interactive:
        drawing.ask_and_apply(spec_path, args.min_confidence)
    args.spec = spec_path
    return cmd_build(args)


def main(argv=None):
    _load_dotenv()
    p = argparse.ArgumentParser(prog="d2f", description="Drawing2Fusion CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping", help="check the Fusion add-in bridge")
    sub.add_parser("menu", help="interactive menu (run.bat)")
    sub.add_parser("doctor", help="check Python, add-in install and Fusion connection")
    pq = sub.add_parser("quick", help="build one spec with defaults and open the result folder")
    pq.add_argument("spec")

    pv = sub.add_parser("validate", help="check a part-spec.json without Fusion")
    pv.add_argument("spec")
    pv.add_argument("--min-confidence", type=float, default=validator.DEFAULT_MIN_CONFIDENCE)

    pe = sub.add_parser("extract", help="read a drawing (.dxf / vector .pdf) into out/<name>/part-spec.json")
    pe.add_argument("drawing")
    pe.add_argument("--out", default="out")
    pe.add_argument("--page", type=int, help="PDF page (1-based); default: the page with the most lines")
    pe.add_argument("--min-confidence", type=float, default=validator.DEFAULT_MIN_CONFIDENCE)

    for name, help_ in (("build", "validate, build in Fusion, measure, compare, report"),
                        ("verify", "measure the open Fusion document against the spec"),
                        ("run", "drawing (.dxf/.pdf) or spec (.json) -> questions -> build -> verify")):
        pb = sub.add_parser(name, help=help_)
        pb.add_argument("drawing" if name == "run" else "spec")
        if name == "run":
            pb.add_argument("--ask", action="store_true", help="always ask BLOCK questions in the terminal")
            pb.add_argument("--no-ask", action="store_true", help="never ask; stop on BLOCK")
        pb.add_argument("--out", default="out")
        if name == "run":
            pb.add_argument("--page", type=int, help="PDF page (1-based); default: the page with the most lines")
        pb.add_argument("--live", type=int, default=0, metavar="MS",
                        help="live build mode: pause MS milliseconds after each feature")
        pb.add_argument("--reuse-document", action="store_true",
                        help="build into the active document instead of a new one")
        pb.add_argument("--export", default="f3d", help="comma list: f3d,step,stl ('' to skip)")
        pb.add_argument("--no-snapshot", action="store_true")
        pb.add_argument("--close-after", action="store_true",
                        help="close the new Fusion document when done (batch / regression runs)")
        pb.add_argument("--no-fusion-alert", action="store_true")
        pb.add_argument("--force", action="store_true", help="build even with BLOCK issues")
        pb.add_argument("--min-confidence", type=float, default=validator.DEFAULT_MIN_CONFIDENCE)

    args = p.parse_args(argv)
    if args.cmd in ("menu", "doctor", "quick"):
        from . import menu
        if args.cmd == "menu":
            return menu.menu()
        if args.cmd == "doctor":
            return menu.doctor()
        return menu.quick(args.spec)
    if args.cmd == "ping":
        return cmd_ping(args)
    if args.cmd == "validate":
        return cmd_validate(args)
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "verify":
        return cmd_build(args, measure_only=True)
    if args.cmd == "extract":
        return cmd_extract(args)
    if args.cmd == "run":
        return cmd_run(args)
    return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
