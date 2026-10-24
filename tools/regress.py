"""Full regression run in Fusion: every sample through the real pipeline, judged against
what it is supposed to do (pass, stop with a question, fail a check ...).

    py -3 tools\\regress.py                 (Fusion running with the add-in; ~2-4 min)
    py -3 tools\\regress.py --only section  (cases whose name contains "section")

Writes out/regress/<part>/... and the summary out/regression.md + out/regression.json.
Exit code 0 only when every case behaved as expected.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "out", "regress")

# exit codes of `d2f run/build`: 0 pass, 1 error / check failed, 2 question (BLOCK), 3 no Fusion
BROKEN_SPECS = {
    "dim_mismatch": {"exit": 1, "code": "E40"},      # builds, a dimension check must fail
    "fillet_too_big": {"exit": 1, "code": "E205"},
    "hole_outside": {"exit": 1, "code": "E205"},
    "needs_answer": {"exit": 2, "code": "E10"},
}
PUBLIC = {
    "qcad_projection": {"exit": 1, "code": "E406",
                        "note": "곡선 포크를 상자로 근사한 모델 → 실루엣 검사가 잡아내야 정상 (예전엔 통과로 잘못 보고)"},
    "qcad_flange": {"exit": 2, "code": "E208", "note": "평면도 + 단면도(회전체 아님) → 질문으로 멈춤"},
    "qcad_example01": {"exit": 0, "all_pass": True, "note": "베벨기어 단면도 → 자동 회전체"},
}
ANSWERS = {   # interactive runs: the answers typed at the questions
    "broken_dim_override": {"answers": "1\n", "exit": 0, "note": "E107 질문에 '그려진 값' 선택"},
    "broken_keyway_noview": {"answers": "6\n", "exit": 0, "note": "키홈 폭 질문에 6 입력"},
    "broken_section_dim": {"answers": "1\n", "exit": 0, "note": "E107 질문에 '그려진 값' 선택"},
}


def cases(only=None):
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "samples", "specs", "*.json"))):
        out.append({"name": "spec/" + os.path.basename(p)[:-5], "path": p, "exit": 0, "all_pass": True})
    for p in sorted(glob.glob(os.path.join(ROOT, "samples", "broken", "*.json"))):
        n = os.path.basename(p)[:-5]
        out.append(dict({"name": "broken/" + n, "path": p}, **BROKEN_SPECS.get(n, {"exit": 1})))
    for p in sorted(glob.glob(os.path.join(ROOT, "samples", "drawings", "dxf", "*.dxf"))):
        n = os.path.basename(p)[:-4]
        exp_path = p[:-4] + ".expected.json"
        exp = json.load(open(exp_path, encoding="utf-8")) if os.path.exists(exp_path) else {}
        if exp.get("expect_issue"):
            out.append({"name": "dxf/" + n, "path": p, "exit": 2, "code": exp["expect_issue"]})
            if n in ANSWERS:
                out.append(dict({"name": "dxf/" + n + " +답변", "path": p, "all_pass": True}, **ANSWERS[n]))
        else:
            out.append({"name": "dxf/" + n, "path": p, "exit": 0, "all_pass": True, "silhouette": True})
    for p in sorted(glob.glob(os.path.join(ROOT, "samples", "drawings", "pdf", "*.pdf"))):
        n = os.path.basename(p)[:-4]
        out.append({"name": "pdf/" + n, "path": p, "exit": 0, "all_pass": True, "silhouette": True,
                    "out": os.path.join(ROOT, "out", "regress_pdf")})
    for p in sorted(glob.glob(os.path.join(ROOT, "samples", "drawings", "public", "*.dxf"))):
        n = os.path.basename(p)[:-4]
        out.append(dict({"name": "public/" + n, "path": p}, **PUBLIC.get(n, {"exit": 0})))
    if only:
        out = [c for c in out if only in c["name"]]
    return out


# Mutation tests: take a spec the reader produced (out/regress/<part>/part-spec.json), plant one
# realistic mistake, rebuild, and require the verification to notice. Each mutation says which
# check should catch it - several of them change a parameter that the spec's own checks follow,
# so only the drawing-based silhouette (E406) or the face position check (E408) can see them.
def _set_param(name, value):
    def f(spec):
        spec["parameters"][name]["value"] = value
    return f


def _feature(fid, fn):
    def f(spec):
        fn(next(x for x in spec["features"] if x["id"] == fid))
    return f


MUTATIONS = [
    ("block_basic", "길이 L 80→90 (치수 오독)", _set_param("L", 90), "E406"),
    ("stepped_block", "윗단 길이 bx2 30→40", _set_param("bx2", 40), "E406"),
    ("plate_holes", "구멍 하나 위치 X 10→13", _feature("h1", lambda f: f["positions"].__setitem__(0, [13, 10])), "E404"),
    ("bushing_section", "몸통 길이 L4 40→45", _set_param("L4", 45), "E406"),
    ("gear_blank_section", "바깥 지름 D4 100→110", _set_param("D4", 110), "E406"),
    ("pulley_keyway_section", "키홈을 반대쪽(−Z)에 가공",
     _feature("sk_key", lambda f: f["entities"][0].__setitem__("corner", ["-key_w/2", "-(key_t - D1/2)"])), "E408"),
]


def mutation_cases():
    import shutil
    out = []
    for part, what, fn, code in MUTATIONS:
        src = os.path.join(OUT, part)
        if not os.path.exists(os.path.join(src, "part-spec.json")):
            continue
        dst = os.path.join(ROOT, "out", "regress_mut", part)
        os.makedirs(dst, exist_ok=True)
        spec = json.load(open(os.path.join(src, "part-spec.json"), encoding="utf-8"))
        fn(spec)
        with open(os.path.join(dst, "part-spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)
        if os.path.exists(os.path.join(src, "extract-report.json")):
            shutil.copyfile(os.path.join(src, "extract-report.json"), os.path.join(dst, "extract-report.json"))
        out.append({"name": "mutation/%s: %s" % (part, what), "path": os.path.join(dst, "part-spec.json"),
                    "build": True, "code": code, "note": "%s 가 잡아야 함" % code})
    return out


def run_case(c, py):
    if c.get("build"):
        cmd = [py, "-m", "d2f", "build", c["path"], "--out", os.path.join(ROOT, "out", "regress_mut", "_build"),
               "--no-snapshot", "--export", "", "--no-fusion-alert"]
    else:
        cmd = [py, "-m", "d2f", "run", c["path"], "--out", c.get("out", OUT), "--no-snapshot", "--export", "",
               "--no-fusion-alert"]
    if not c.get("build"):
        cmd.append("--ask" if c.get("answers") else "--no-ask")
    env = dict(os.environ, D2F_NO_OPEN="1", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, env=env, input=(c.get("answers") or "").encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
    text = p.stdout.decode("utf-8", "replace")
    res = {"name": c["name"], "exit": p.returncode, "expected_exit": c.get("exit"), "seconds": round(time.time() - t0, 1)}
    # the part's result folder: the spec's part name
    report = None
    for line in text.splitlines():
        if "결과:" in line:
            folder = line.split("결과:", 1)[1].strip()
            rp = os.path.join(folder, "verify-report.json")
            if os.path.exists(rp):
                report = json.load(open(rp, encoding="utf-8"))
    codes = sorted({a["code"] for a in (report or {}).get("alerts", []) if a.get("code")} |
                   {w.strip("[]") for w in text.split() if w.startswith("[E") and w.endswith("]")})
    res["codes"] = codes
    problems = []
    if c.get("exit") is not None and p.returncode != c["exit"]:
        problems.append("종료 코드 %s (기대 %s)" % (p.returncode, c["exit"]))
    if c.get("code") and not any(x.startswith(c["code"]) for x in codes):
        problems.append("%s 알림이 없음" % c["code"])
    if report:
        rows = report.get("checks", [])
        res["passed"], res["total"] = report.get("passed"), report.get("total")
        sil = [r for r in rows if r.get("kind") == "silhouette"]
        res["silhouette"] = {r["view"]: r.get("actual") for r in sil}
        if c.get("all_pass") and res["passed"] != res["total"]:
            problems.append("검증 %s/%s" % (res["passed"], res["total"]))
        if c.get("silhouette") and not sil:
            problems.append("실루엣 검사가 없음")
    elif c.get("all_pass"):
        problems.append("verify-report 없음")
    if "Traceback" in text:
        problems.append("파이썬 예외")
    res["ok"] = not problems
    res["problems"] = problems
    res["note"] = c.get("note", "")
    if problems:
        res["log_tail"] = text[-1500:]
    return res


def write_summary(results, path_md, path_json):
    ok = sum(1 for r in results if r["ok"])
    lines = ["# 회귀 테스트 결과", "",
             "%s · %d/%d 케이스가 기대대로 동작 · 총 %.0f초" % (time.strftime("%Y-%m-%d %H:%M"), ok, len(results),
                                                         sum(r["seconds"] for r in results)), "",
             "| 케이스 | 결과 | 종료 코드 | 검증 | 실루엣 IoU | 알림 코드 | 비고 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        sil = ", ".join("%s %.3f" % (k, v) for k, v in sorted((r.get("silhouette") or {}).items()) if v is not None)
        chk = "%s/%s" % (r["passed"], r["total"]) if r.get("total") is not None else "-"
        lines.append("| %s | %s | %s (기대 %s) | %s | %s | %s | %s |" % (
            r["name"], "✅" if r["ok"] else "❌ " + "; ".join(r["problems"]), r["exit"],
            "-" if r["expected_exit"] is None else r["expected_exit"], chk,
            sil or "-", " ".join(x for x in r["codes"] if x.startswith("E")) or "-", r["note"]))
    with open(path_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--no-mutation", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    results = []
    todo = cases(a.only)
    k = 0
    while todo:
        c = todo.pop(0)
        k += 1
        try:
            r = run_case(c, sys.executable)
        except subprocess.TimeoutExpired:
            r = {"name": c["name"], "exit": None, "expected_exit": c.get("exit"), "seconds": 240, "codes": [],
                 "ok": False, "problems": ["시간 초과"], "note": c.get("note", "")}
        results.append(r)
        print("[%2d] %s %s %s" % (k, "✅" if r["ok"] else "❌", r["name"], "; ".join(r["problems"])), flush=True)
        if not todo and not a.no_mutation and not any(x["name"].startswith("mutation/") for x in results):
            todo = [m for m in mutation_cases() if not a.only or a.only in m["name"]]
    ok = write_summary(results, os.path.join(ROOT, "out", "regression.md"), os.path.join(ROOT, "out", "regression.json"))
    print("\n%d/%d 케이스 기대대로 → out\\regression.md" % (ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
