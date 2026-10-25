"""Drawing -> part-spec pipeline and interactive answers to BLOCK questions.

  python -m d2f extract drawing.dxf|.pdf       write out/<name>/part-spec.json + extract-report.json
  python -m d2f run drawing.dxf [build opts]   extract -> (ask) -> validate -> build -> verify
"""

import importlib
import json
import os
import subprocess
import sys

from .spec import expr
from .spec import validate as validator

SUPPORTED = {".dxf": "dxf", ".pdf": "pdf"}
PLANNED = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".heic"}


def ensure_ezdxf(auto_install=True):
    try:
        return importlib.import_module("ezdxf")
    except ImportError:
        if not auto_install:
            return None
    print("ℹ️ DXF를 읽으려면 ezdxf 패키지가 필요해서 설치합니다 (한 번만) ...")
    for args in ([sys.executable, "-m", "pip", "install", "--user", "ezdxf"],
                 [sys.executable, "-m", "pip", "install", "ezdxf"]):
        try:
            subprocess.check_call(args)
        except Exception:
            continue
        # a first --user install creates the user site folder after startup: add it now
        import site
        try:
            site.addsitedir(site.getusersitepackages())
        except Exception:
            pass
        importlib.invalidate_caches()
        try:
            return importlib.import_module("ezdxf")
        except ImportError:
            continue
    return None


def extract_to(path, out_root):
    ext = os.path.splitext(path)[1].lower()
    if ext in PLANNED:
        raise SystemExit("🔴 [E101] %s 이미지 도면은 자동으로 읽지 못합니다. Claude 채팅에 사진을 올려 판독하세요 "
                         "(docs/image-reading.md). 자동으로 되는 형식: .dxf, 벡터 .pdf, part-spec .json" % ext)
    if ext == ".dwg":
        raise SystemExit("🔴 [E101] DWG는 바로 읽지 못합니다. CAD에서 DXF(또는 PDF)로 저장해서 넣어 주세요.")
    if ext not in SUPPORTED:
        raise SystemExit("🔴 [E101] 알 수 없는 파일 형식: %s" % ext)
    if ext == ".pdf":
        from .extract import pdf_reader
        if pdf_reader.ensure_pymupdf() is None:
            raise SystemExit("🔴 [E101] PyMuPDF 설치에 실패했습니다. 명령창에서 'py -m pip install pymupdf' 를 실행해 주세요.")
    if ensure_ezdxf() is None:
        raise SystemExit("🔴 [E101] ezdxf 설치에 실패했습니다. 명령창에서 'py -m pip install ezdxf' 를 실행해 주세요.")
    from .extract.dxf_reader import ExtractError, extract
    try:
        spec, report = extract(path)
    except ExtractError as e:
        raise SystemExit("🔴 [%s] %s" % (e.code, e))
    name = spec["part"]["name"]
    out_dir = os.path.join(out_root, name)
    os.makedirs(out_dir, exist_ok=True)
    spec_path = os.path.join(out_dir, "part-spec.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "extract-report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return spec_path, spec, report


def print_summary(spec, report):
    s = report["summary"]
    views = {"front": "정면도", "top": "평면도", "side": "측면도"}
    proj = {"third": "제3각법", "first": "제1각법", "single": "1면도", "section": "단면도"}.get(
        report["projection"], report["projection"])
    if s.get("kind") == "turned":
        print("📐 도면 해석: 회전체 단면도 · 프로파일 꼭짓점 %d개 · 키홈 %s · 치수 %d개 중 %d개 사용"
              % (s["profile_points"], "있음" if any(f["id"] == "ex_key" for f in spec["features"]) else "없음",
                 s["dims_total"], s["dims_used"]))
    else:
        print("📐 도면 해석: %s · %s · 층 %d개 · 구멍 %d개 · 치수 %d개 중 %d개 사용"
              % (", ".join(views.get(v, v) for v in s["views"]), proj, s["bands"], s["holes"],
                 s["dims_total"], s["dims_used"]))
    try:
        vals = expr.resolve_parameters({k: v for k, v in spec["parameters"].items() if v.get("value") is not None})
    except ValueError:
        vals = {}
    for k, p in spec["parameters"].items():
        src = p.get("source") or {}
        tag = {"drawing": "치수 " + str(src.get("label", "")), "derived": "계산 (%s)" % p["value"],
               "inferred": "⚠ 치수 없음 - 형상에서 잼", "conflict": "❓ 치수 문자와 형상이 다름",
               "unresolved": "❓ 알 수 없음", "user": "사용자 입력"}.get(src.get("type"), src.get("type"))
        v = vals.get(k, p.get("value"))
        print("   %-9s %10s   %s" % (k, ("%g" % v) if isinstance(v, (int, float)) else v, tag))
    pats = [f for f in spec.get("features") or [] if f.get("type") in ("pattern_rect", "pattern_circ")]
    for f in pats:
        n = f.get("count") if f["type"] == "pattern_circ" else \
            (f.get("x") or {}).get("count", 1) * (f.get("y") or {}).get("count", 1)
        print("   🔁 %s: %s %s개 (%s)" % (f["id"], "원형 배열" if f["type"] == "pattern_circ" else "직선 배열", n,
                                       ", ".join(f.get("features") or [])))
    ann = [n for n in spec.get("notes") or [] if n.get("annotation")]
    if ann:
        print("   📝 가공 요구 %d개 (모델에는 반영 안 됨, part-spec.json notes에 기록):" % len(ann))
        for n in ann:
            print("      · %s" % n["reason"].split(" — ")[0])
    for i in report["issues"]:
        print("   %s [%s] %s" % ({"BLOCK": "🟠", "ERROR": "🔴", "WARN": "🟡"}.get(i["severity"], "ℹ️"),
                                i["code"], i["message"]))


# ----------------------------------------------------------------------------- answers
def _read(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _number(text):
    try:
        return float(text)
    except ValueError:
        return None


def questions(spec, min_confidence=validator.DEFAULT_MIN_CONFIDENCE):
    """BLOCK issues of a spec as answerable questions (used by the terminal and the Fusion palette).

    Each: {id, code, message, text, options, input: "choice"|"number", where}
    """
    out = []
    for n, i in enumerate(x for x in validator.validate(spec, min_confidence) if x["severity"] == "BLOCK"):
        q = i.get("question") or {}
        out.append({"id": n, "code": i["code"], "message": i["message"], "text": q.get("text", ""),
                    "options": q.get("options") or [], "values": q.get("values") or [],
                    "input": "choice" if q.get("options") else "number", "where": i.get("where") or {}})
    return out


def apply_answer(spec, q, ans, value=None):
    """Write one answer into the spec. `ans`: option number ("1".."n") or a number; `value`: the
    number typed for an option that needs one ("직접 입력"). Returns True when applied."""
    ans = (ans or "").strip()
    if not ans:
        return False
    where = q.get("where") or {}
    opts = q.get("options") or []
    pname = where.get("parameter")
    if pname and pname in spec.get("parameters", {}):
        p = spec["parameters"][pname]
        v = None
        if ans.isdigit() and 1 <= int(ans) <= len(opts):
            vals = q.get("values") or []
            k = int(ans) - 1
            if k < len(vals) and vals[k] is not None:
                v = vals[k]
            elif q["code"] == "E105" and k == 0:
                v = p.get("value")
            else:
                v = _number(str(value)) if value is not None else None
        else:
            v = _number(ans)
        if v is None:
            return False
        p["source"] = {"type": "user", "previous": p.get("source")}
        p["value"] = v
        p.pop("confidence", None)
        return True
    fid = where.get("feature")
    feat = next((f for f in spec.get("features", []) if f.get("id") == fid), None)
    if feat is not None and feat.get("type") == "hole" and q["code"] == "E202" and "depth" in q["message"]:
        if ans == "1":
            feat["depth"] = "through_all"
        elif ans == "3":
            spec["features"] = [f for f in spec["features"] if f is not feat]
            spec["checks"] = [c for c in spec.get("checks", []) if c.get("feature") != fid]
        else:
            v = _number(ans) if ans != "2" else (_number(str(value)) if value is not None else None)
            if v is None:
                return False
            feat["depth"] = v
        return True
    return False


def ask_and_apply(spec_path, min_confidence=validator.DEFAULT_MIN_CONFIDENCE):
    """Ask the BLOCK questions in the terminal and write the answers into the spec.

    Returns True when every BLOCK issue was answered.
    """
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    qs = questions(spec, min_confidence)
    if not qs:
        return True
    print("\n🟠 확인이 필요한 값이 %d개 있습니다. 답하면 그대로 모델링을 이어갑니다 (Enter = 건너뛰기)\n" % len(qs))
    for q in qs:
        print("── [%s] %s" % (q["code"], q["message"]))
        if q["text"]:
            print("   ❓ " + q["text"])
        for n, o in enumerate(q["options"], 1):
            print("      %d) %s" % (n, o))
        ans = _read("   답: ")
        if not ans:
            continue
        value = None
        needs_value = ans.isdigit() and 1 <= int(ans) <= len(q["options"]) and not (
            int(ans) - 1 < len(q["values"]) and q["values"][int(ans) - 1] is not None) and not (
            q["code"] == "E105" and ans == "1") and not (q["code"] == "E202" and ans in ("1", "3"))
        if needs_value:
            value = _read("   값 (mm): ")
        if not apply_answer(spec, q, ans, value):
            print("   숫자가 아니라 건너뜁니다")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    left = [i for i in validator.validate(spec, min_confidence) if i["severity"] == "BLOCK"]
    if left:
        print("\n🟠 아직 %d개가 남아 있습니다. %s 을 직접 고친 뒤 다시 실행해 주세요." % (len(left), spec_path))
    return not left
