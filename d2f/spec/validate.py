"""Pre-build spec validation (E2xx / E1xx). Pure Python, no Fusion needed."""

import json

from . import expr

FEATURE_TYPES = {"sketch", "extrude", "revolve", "hole", "fillet", "chamfer", "pattern_rect", "pattern_circ",
                 "shell", "draft", "sweep", "loft"}
FACE_SELECTORS = {"top", "bottom", "front", "back", "left", "right", "vertical_outer", "side_all"}
PLANNED_TYPES = set()
ENTITY_KINDS = {"rect", "circle", "line", "polyline", "arc", "slot", "path"}
PLANES = {"XY", "XZ", "YZ"}
# Fields whose values are dimensions (numbers or parameter expressions).
VALUE_FIELDS = {"offset", "distance", "diameter", "radius", "depth", "angle", "sweep",
                "cb_diameter", "cb_depth", "cs_diameter", "cs_angle", "size", "corner", "center",
                "start", "end", "points", "positions", "width", "count", "pitch", "to", "thickness"}
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
                if ent.get("kind") == "path":
                    _check_path(ent, resolved, issues, where)
                    for seg in ent.get("segments") or []:
                        if isinstance(seg, dict):
                            _check_refs(seg, resolved, params, issues, where)
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
        if ftype == "shell" and "thickness" not in f:
            issues.append(_issue("E202", "BLOCK", "shell '%s' has no thickness" % fid, where,
                                 {"text": "'%s' 셸 두께를 입력해 주세요" % fid, "input": {"type": "number", "unit": "mm"}}))
        if ftype in ("shell", "draft"):
            fsel = (f.get("faces") or {}).get("select", "")
            if ftype == "draft" and not fsel:
                issues.append(_issue("E201", "ERROR", "draft '%s' needs faces: {select: ...}" % fid, where))
            elif fsel and fsel not in FACE_SELECTORS and not fsel.startswith("faces_of:"):
                issues.append(_issue("E201", "ERROR", "unknown face selector '%s' (%s)" % (fsel, ", ".join(sorted(FACE_SELECTORS))),
                                     where))
            elif fsel.startswith("faces_of:") and fsel.split(":", 1)[1] not in seen:
                issues.append(_issue("E206", "ERROR", "'%s' refers to unknown feature in '%s'" % (fid, fsel), where))
        if ftype == "draft" and "angle" not in f:
            issues.append(_issue("E202", "BLOCK", "draft '%s' has no angle" % fid, where))
        if ftype == "sweep":
            for key in ("profile", "path"):
                if f.get(key) not in sketches:
                    issues.append(_issue("E206", "ERROR", "sweep '%s' %s '%s' is not an earlier sketch"
                                         % (fid, key, f.get(key)), where))
            path_sk = next((x for x in features[:i] if x.get("id") == f.get("path")), None)
            if path_sk is not None and path_sk.get("require_profile", True):
                issues.append(_issue("E201", "ERROR", "sweep path sketch '%s' is an open path: set \"require_profile\": false"
                                     % f.get("path"), where))
        if ftype == "loft":
            secs = f.get("sections") or []
            if len(secs) < 2:
                issues.append(_issue("E201", "ERROR", "loft '%s' needs at least 2 sections" % fid, where))
            for sid in secs:
                if sid not in sketches:
                    issues.append(_issue("E206", "ERROR", "loft '%s' section '%s' is not an earlier sketch" % (fid, sid), where))
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


def path_points(ent, resolved, step_deg=5.0):
    """Vertices of a path entity with arcs flattened (spec mm), or None if not numeric."""
    import math
    cur = [_safe(v, resolved) for v in ent.get("start") or []]
    if len(cur) != 2 or None in cur:
        return None
    pts = [tuple(cur)]
    for seg in ent.get("segments") or []:
        to = [_safe(v, resolved) for v in seg.get("to") or []]
        if len(to) != 2 or None in to:
            return None
        if seg.get("type") == "arc":
            c = [_safe(v, resolved) for v in seg.get("center") or []]
            if len(c) != 2 or None in c:
                return None
            a0 = math.atan2(cur[1] - c[1], cur[0] - c[0])
            a1 = math.atan2(to[1] - c[1], to[0] - c[0])
            if seg.get("ccw", True):
                sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
            else:
                sweep = -((a0 - a1) % (2 * math.pi) or 2 * math.pi)
            r = math.hypot(cur[0] - c[0], cur[1] - c[1])
            n = max(2, int(abs(math.degrees(sweep)) / step_deg))
            pts += [(c[0] + r * math.cos(a0 + sweep * k / n), c[1] + r * math.sin(a0 + sweep * k / n))
                    for k in range(1, n)]
        pts.append(tuple(to))
        cur = to
    return pts


def _check_path(ent, resolved, issues, where):
    """Arc ends must lie on the arc's circle; a closed path must end where it starts."""
    import math
    cur = [_safe(v, resolved) for v in ent.get("start") or []]
    if len(cur) != 2 or None in cur:
        return
    segs = ent.get("segments") or []
    if not segs:
        issues.append(_issue("E201", "ERROR", "path has no segments", where))
        return
    for k, seg in enumerate(segs):
        if not isinstance(seg, dict) or seg.get("type", "line") not in ("line", "arc") or "to" not in seg:
            issues.append(_issue("E201", "ERROR", "path segment %d needs type line/arc and 'to'" % k, where))
            return
        to = [_safe(v, resolved) for v in seg["to"]]
        if None in to:
            return
        if seg.get("type") == "arc":
            c = [_safe(v, resolved) for v in seg.get("center") or [None, None]]
            if None in c:
                issues.append(_issue("E201", "ERROR", "path arc %d needs a numeric 'center'" % k, where))
                return
            r0, r1 = math.dist(cur, c), math.dist(to, c)
            if abs(r0 - r1) > max(1e-3, r0 * 1e-4):
                issues.append(_issue("E205", "ERROR", "path arc %d: start and end are not on one circle "
                                     "(R%.4f vs R%.4f)" % (k, r0, r1), where))
        cur = to
    if ent.get("closed", True):
        start = [_safe(v, resolved) for v in ent["start"]]
        if math.dist(start, cur) > 1e-3:
            issues.append(_issue("E205", "ERROR", "closed path ends at (%g, %g), not at its start (%g, %g)"
                                 % (cur[0], cur[1], start[0], start[1]), where))


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
        elif k == "path":
            pts = path_points(ent, resolved, 2.0)
            if pts:
                boxes.append((min(p[0] for p in pts), min(p[1] for p in pts),
                              max(p[0] for p in pts), max(p[1] for p in pts)))
    return boxes


def hole_instances(features, hole, resolved):
    """All (u, v) positions of a hole feature, with the rectangular / circular patterns that copy it.
    Circular patterns about an XY-hole axis "Z" use their center (default origin)."""
    import math
    pts = []
    for pos in hole.get("positions") or []:
        u, v = _safe(pos[0], resolved), _safe(pos[1], resolved)
        if u is not None and v is not None:
            pts.append((u, v))
    for f in features:
        if hole.get("id") not in (f.get("features") or []):
            continue
        if f.get("type") == "pattern_rect":
            out = []
            dx, dy = f.get("x") or {}, f.get("y") or {}
            nx, px = int(_safe(dx.get("count", 1), resolved) or 1), _safe(dx.get("pitch", 0), resolved) or 0.0
            ny, py = int(_safe(dy.get("count", 1), resolved) or 1), _safe(dy.get("pitch", 0), resolved) or 0.0
            for (u, v) in pts:
                out += [(u + i * px, v + j * py) for i in range(nx) for j in range(ny)]
            pts = out
        elif f.get("type") == "pattern_circ":
            c = [_safe(x, resolved) for x in (f.get("center") or [0, 0])]
            n = int(_safe(f.get("count", 1), resolved) or 1)
            ang = _safe(f.get("angle", 360), resolved) or 360.0
            step = ang / n if abs(ang - 360) < 1e-9 else ang / max(n - 1, 1)
            out = []
            for (u, v) in pts:
                for k in range(n):
                    t = math.radians(step * k)
                    du, dv = u - c[0], v - c[1]
                    out.append((c[0] + du * math.cos(t) - dv * math.sin(t), c[1] + du * math.sin(t) + dv * math.cos(t)))
            pts = out
    return pts


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
            for k, (x, y) in enumerate(hole_instances(features, f, resolved)):
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
