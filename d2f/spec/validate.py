"""Pre-build spec validation (E2xx / E1xx). Pure Python, no Fusion needed."""

import json

from . import expr

FEATURE_TYPES = {"sketch", "extrude", "revolve", "hole", "fillet", "chamfer", "pattern_rect", "pattern_circ"}
PLANNED_TYPES = set()
ENTITY_KINDS = {"rect", "circle", "line", "polyline", "arc", "slot"}
PLANES = {"XY", "XZ", "YZ"}
# Fields whose values are dimensions (numbers or parameter expressions).
VALUE_FIELDS = {"offset", "distance", "diameter", "radius", "depth", "angle", "sweep",
                "cb_diameter", "cb_depth", "cs_diameter", "cs_angle", "size", "corner", "center",
                "start", "end", "points", "positions", "width", "count", "pitch"}
NESTED_FIELDS = {"x", "y", "face"}
DEFAULT_MIN_CONFIDENCE = 0.8


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _issue(code, severity, message, where=None, question=None):
    d = {"code": code, "severity": severity, "message": message}
    if where:
        d["where"] = where
    if question:
        d["question"] = question
    return d


def _walk_values(v):
    """Yield every scalar inside nested lists."""
    if isinstance(v, list):
        for x in v:
            yield from _walk_values(x)
    else:
        yield v


def validate(spec, min_confidence=DEFAULT_MIN_CONFIDENCE):
    issues = []

    # ---- structure
    if not isinstance(spec, dict):
        return [_issue("E201", "ERROR", "spec must be a JSON object")]
    if "schema_version" not in spec:
        issues.append(_issue("E201", "ERROR", "missing schema_version"))
    if not (spec.get("part") or {}).get("name"):
        issues.append(_issue("E201", "ERROR", "missing part.name"))
    features = spec.get("features")
    if not isinstance(features, list) or not features:
        issues.append(_issue("E201", "ERROR", "features must be a non-empty list"))
        features = []

    # ---- parameters
    params = spec.get("parameters") or {}
    for name, p in params.items():
        p = p if isinstance(p, dict) else {"value": p}
        if not expr._IDENT.fullmatch(name) or name in ("pi",):
            issues.append(_issue("E206", "ERROR", "parameter name '%s' is not allowed" % name,
                                 {"parameter": name}))
        src = p.get("source") or {}
        stype = src.get("type")
        conf = p.get("confidence", src.get("confidence"))
        if stype == "conflict":
            issues.append(_issue("E107", "BLOCK", "parameter '%s': 도면 치수 문자 '%s'(%s)와 그려진 형상 %s mm가 다릅니다"
                                 % (name, src.get("label"), src.get("view"), src.get("drawn")),
                                 {"parameter": name, "drawing": src},
                                 {"text": "'%s' 값을 정해 주세요" % name,
                                  "options": ["그려진 형상 %s 사용" % src.get("drawn"),
                                              "치수 문자 %s 사용" % src.get("shown"), "직접 입력"],
                                  "values": [src.get("drawn"), src.get("shown"), None],
                                  "input": {"type": "number", "unit": p.get("unit", "mm")}}))
            continue
        if stype == "unresolved" or p.get("value") is None:
            issues.append(_issue("E202", "BLOCK", "parameter '%s' has no value from the drawing" % name,
                                 {"parameter": name, "drawing": src},
                                 {"text": "'%s' 값을 입력해 주세요%s" % (name, (" (" + src["reason"] + ")")
                                                                if src.get("reason") else ""),
                                  "input": {"type": "number", "unit": p.get("unit", "mm")}}))
        elif stype == "inferred":
            issues.append(_issue("E207", "WARN", "parameter '%s' = %s is inferred (%s)"
                                 % (name, p.get("value"), src.get("reason", "no reason given")),
                                 {"parameter": name}))
        if conf is not None and stype != "user" and float(conf) < min_confidence:
            issues.append(_issue("E105", "BLOCK", "parameter '%s' = %s read with low confidence %.2f"
                                 % (name, p.get("value"), float(conf)),
                                 {"parameter": name, "drawing": src},
                                 {"text": "'%s' 값이 %s 맞나요?" % (name, p.get("value")),
                                  "options": ["맞음", "다른 값 입력"]}))
    resolved = {}
    try:
        resolved = expr.resolve_parameters({k: v for k, v in params.items()
                                            if (v if not isinstance(v, dict) else v.get("value")) is not None})
    except ValueError as e:
        issues.append(_issue("E206", "ERROR", str(e)))

    # ---- features
    seen = set()
    sketches = set()
    for i, f in enumerate(features):
        fid = f.get("id")
        where = {"feature": fid, "index": i}
        if not fid:
            issues.append(_issue("E201", "ERROR", "feature #%d has no id" % i, where))
            continue
        if fid in seen:
            issues.append(_issue("E201", "ERROR", "duplicate feature id '%s'" % fid, where))
        seen.add(fid)
        ftype = f.get("type")
        if ftype in PLANNED_TYPES:
            issues.append(_issue("E201", "ERROR", "feature type '%s' is planned but not implemented yet"
                                 % ftype, where))
            continue
        if ftype not in FEATURE_TYPES:
            issues.append(_issue("E201", "ERROR", "unknown feature type '%s'" % ftype, where))
            continue

        if ftype == "sketch":
            sketches.add(fid)
            for ent in f.get("entities", []):
                if ent.get("kind") not in ENTITY_KINDS:
                    issues.append(_issue("E201", "ERROR", "unknown sketch entity kind '%s'"
                                         % ent.get("kind"), where))
                _check_refs(ent, resolved, params, issues, where)
        if ftype in ("extrude", "revolve") and f.get("profile") not in sketches:
            issues.append(_issue("E206", "ERROR", "'%s' uses profile '%s' which is not an earlier sketch"
                                 % (fid, f.get("profile")), where))
        if ftype == "extrude" and "distance" not in f:
            issues.append(_issue("E202", "BLOCK", "extrude '%s' has no distance" % fid, where,
                                 {"text": "'%s' 돌출 거리가 없습니다." % fid,
                                  "input": {"type": "number", "unit": "mm"}}))
        if ftype == "hole":
            if "depth" not in f:
                issues.append(_issue("E202", "BLOCK", "hole '%s' depth is not specified" % fid, where,
                                     {"text": "'%s' 구멍 깊이가 표기되어 있지 않습니다. 어떻게 할까요?" % fid,
                                      "options": ["관통", "깊이 직접 입력", "이 구멍 제외"]}))
            if not f.get("positions"):
                issues.append(_issue("E202", "BLOCK", "hole '%s' has no positions" % fid, where))
            if f.get("hole_type") == "counterbore" and not ("cb_diameter" in f and "cb_depth" in f):
                issues.append(_issue("E202", "BLOCK", "counterbore '%s' needs cb_diameter and cb_depth"
                                     % fid, where))
        if ftype == "hole" and f.get("hole_type") == "countersink":
            if "cs_diameter" not in f:
                issues.append(_issue("E202", "BLOCK", "countersink '%s' needs cs_diameter" % fid, where))
            else:
                d, cs = _safe(f.get("diameter"), resolved), _safe(f.get("cs_diameter"), resolved)
                if d is not None and cs is not None and cs <= d:
                    issues.append(_issue("E205", "ERROR", "countersink '%s': cs_diameter %g must be larger "
                                         "than diameter %g" % (fid, cs, d), where))
        if ftype in ("pattern_rect", "pattern_circ"):
            refs = f.get("features") or []
            if not refs:
                issues.append(_issue("E201", "ERROR", "pattern '%s' has no features" % fid, where))
            for r in refs:
                if r not in seen or r == fid:
                    issues.append(_issue("E206", "ERROR", "pattern '%s' refers to '%s' which is not an "
                                         "earlier feature" % (fid, r), where))
            if ftype == "pattern_circ" and "count" not in f:
                issues.append(_issue("E202", "BLOCK", "circular pattern '%s' has no count" % fid, where))
        if ftype in ("fillet", "chamfer"):
            sel = (f.get("edges") or {}).get("select", "")
            if sel.startswith("hole_edges:") and sel.split(":", 1)[1] not in seen:
                issues.append(_issue("E206", "ERROR", "'%s' refers to unknown feature in '%s'" % (fid, sel),
                                     where))
        _check_refs(f, resolved, params, issues, where)

    issues.extend(_geometry_checks(features, resolved))
    issues.extend(_check_checks(spec, resolved, params, seen))
    return issues


def _check_refs(obj, resolved, params, issues, where):
    for key, val in obj.items():
        if key in NESTED_FIELDS and isinstance(val, dict):
            _check_refs(val, resolved, params, issues, where)
            continue
        if key not in VALUE_FIELDS:
            continue
        for v in _walk_values(val):
            if v == "through_all":
                continue
            names = expr.identifiers(v)
            unknown = names - set(params)
            if unknown:
                issues.append(_issue("E206", "ERROR", "%s.%s references undefined parameter(s) %s"
                                     % (where.get("feature"), key, sorted(unknown)), where))
                continue
            if isinstance(v, str) and names <= set(resolved):
                try:
                    expr.evaluate(v, resolved)
                except (ValueError, ZeroDivisionError) as e:
                    issues.append(_issue("E201", "ERROR", "%s.%s: %s" % (where.get("feature"), key, e),
                                         where))


def _safe(v, resolved):
    try:
        return expr.evaluate(v, resolved)
    except Exception:
        return None


def _sketch_boxes(f, resolved):
    """Bounding boxes (xmin, ymin, xmax, ymax) of the XY sketch entities of one sketch feature."""
    boxes = []
    for ent in f.get("entities", []):
        k = ent.get("kind")
        if k == "rect":
            c = [_safe(v, resolved) for v in ent.get("corner", [0, 0])]
            s_ = [_safe(v, resolved) for v in ent.get("size", [None, None])]
            if None not in c and None not in s_:
                boxes.append((min(c[0], c[0] + s_[0]), min(c[1], c[1] + s_[1]),
                              max(c[0], c[0] + s_[0]), max(c[1], c[1] + s_[1])))
        elif k == "circle":
            c = [_safe(v, resolved) for v in ent.get("center", [0, 0])]
            d = _safe(ent.get("diameter"), resolved)
            if d is None and ent.get("radius") is not None:
                r = _safe(ent.get("radius"), resolved)
                d = None if r is None else 2 * r
            if None not in c and d is not None:
                boxes.append((c[0] - d / 2, c[1] - d / 2, c[0] + d / 2, c[1] + d / 2))
        elif k == "polyline":
            pts = [[_safe(v, resolved) for v in p] for p in ent.get("points", [])]
            if pts and all(None not in p for p in pts):
                boxes.append((min(p[0] for p in pts), min(p[1] for p in pts),
                              max(p[0] for p in pts), max(p[1] for p in pts)))
    return boxes


def _geometry_checks(features, resolved):
    """Cheap sanity checks that predict build failures (E205). Only XY sketches are considered."""
    issues = []
    xy_sketches = {f.get("id"): f for f in features
                   if f.get("type") == "sketch" and str(f.get("plane", "XY")).upper() == "XY"}
    boxes = [b for f in xy_sketches.values() for b in _sketch_boxes(f, resolved)]
    if not boxes:
        return issues
    xmin, ymin = min(b[0] for b in boxes), min(b[1] for b in boxes)
    xmax, ymax = max(b[2] for b in boxes), max(b[3] for b in boxes)
    profile_of = {f.get("id"): f.get("profile") for f in features if f.get("type") in ("extrude", "revolve")}
    for f in features:
        where = {"feature": f.get("id")}
        if f.get("type") == "hole" and str((f.get("face") or {}).get("plane", "XY")).upper() == "XY":
            d = _safe(f.get("diameter"), resolved) or 0.0
            for k, pos in enumerate(f.get("positions") or []):
                x, y = _safe(pos[0], resolved), _safe(pos[1], resolved)
                if x is None or y is None:
                    continue
                if x - d / 2 < xmin - 1e-9 or x + d / 2 > xmax + 1e-9 or \
                        y - d / 2 < ymin - 1e-9 or y + d / 2 > ymax + 1e-9:
                    issues.append(_issue("E205", "ERROR", "hole %s[%d] at (%g, %g) Ø%g is outside the "
                                         "part outline" % (f.get("id"), k, x, y, d), where))
        if f.get("type") == "fillet":
            r = _safe(f.get("radius"), resolved)
            sel = (f.get("edges") or {})
            src = sel.get("of")
            name = sel.get("select", "")
            if ":" in name:
                src = name.split(":", 1)[1]
            sk = xy_sketches.get(profile_of.get(src))
            rects = [b for b in _sketch_boxes(sk, resolved)] if sk else []
            if r is None or not rects:
                continue
            min_side = min(min(b[2] - b[0], b[3] - b[1]) for b in rects)
            if r * 2 >= min_side:
                issues.append(_issue("E205", "ERROR", "fillet %s R%g is too large for a %g mm side"
                                     % (f.get("id"), r, min_side), where))
    return issues


def _check_checks(spec, resolved, params, feature_ids):
    issues = []
    for c in spec.get("checks") or []:
        where = {"check": c.get("id")}
        if c.get("kind") not in ("bbox", "hole_count", "hole_diameter", "hole_positions", "volume", "plane", "thread"):
            issues.append(_issue("E201", "ERROR", "unknown check kind '%s'" % c.get("kind"), where))
        if c.get("feature") and c["feature"] not in feature_ids:
            issues.append(_issue("E206", "ERROR", "check %s refers to unknown feature '%s'"
                                 % (c.get("id"), c["feature"]), where))
        if c.get("kind") == "plane" and ("at" not in c or str(c.get("axis", "Z")).upper() not in ("X", "Y", "Z")):
            issues.append(_issue("E201", "ERROR", "check %s (plane) needs 'axis' (X/Y/Z) and 'at'" % c.get("id"), where))
        for v in _walk_values([c.get("expected"), c.get("at")]):
            unknown = expr.identifiers(v) - set(params)
            if unknown:
                issues.append(_issue("E206", "ERROR", "check %s expected references undefined %s"
                                     % (c.get("id"), sorted(unknown)), where))
    return issues


def summarize(issues):
    counts = {"ERROR": 0, "BLOCK": 0, "WARN": 0, "INFO": 0}
    for i in issues:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    return counts
