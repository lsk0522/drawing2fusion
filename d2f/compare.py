"""Compare measurements from Fusion against the spec's `checks` (E4xx)."""

import os

from .spec import expr

DEFAULT_TOL_MM = float(os.environ.get("D2F_DEFAULT_TOL_MM", "0.05"))
VOLUME_REL_TOL = 0.01


def _tol_for(check, expected_raw, params_spec):
    if "tol" in check:
        return check["tol"]
    if isinstance(expected_raw, str) and expected_raw in params_spec:
        p = params_spec[expected_raw]
        if isinstance(p, dict) and "tolerance" in p:
            return p["tolerance"]
    return DEFAULT_TOL_MM


def judge(expected, actual, tol):
    """Return (pass, delta). tol: number (+-) or [lower, upper] on delta."""
    delta = actual - expected
    if isinstance(tol, (list, tuple)):
        lower, upper = float(tol[0]), float(tol[1])
        # a [0, +0.1] drawing tolerance still accepts a model built at nominal
        # (|delta| <= 1 um: measurement noise around the nominal model)
        ok = (lower - 1e-9) <= delta <= (upper + 1e-9) or abs(delta) <= 1e-3
    else:
        ok = abs(delta) <= float(tol) + 1e-9
    return ok, delta


def unit_suspect(expected, actual):
    if not expected or not actual:
        return False
    r = abs(actual / expected)
    return abs(r - 10) < 0.05 or abs(r - 0.1) < 0.0005


def _row(check, code, severity, title, message, **extra):
    row = {"check": check.get("id"), "kind": check.get("kind"), "pass": severity is None,
           "code": code, "severity": severity, "title": title, "message": message}
    row.update(extra)
    return row


def _dim_row(check, label, expected, actual, tol, code, title):
    ok, delta = judge(expected, actual, tol)
    if ok:
        return _row(check, None, None, title, "%s OK (%.4f)" % (label, actual),
                    label=label, expected=expected, actual=actual, delta=delta, tol=tol)
    if unit_suspect(expected, actual):
        code, title = "E407", "단위 의심 (cm/mm 변환 확인)"
    return _row(check, code, "ERROR", title,
                "%s: 기대 %.4g, 실측 %.4g, 오차 %+.4g mm" % (label, expected, actual, delta),
                label=label, expected=expected, actual=actual, delta=delta, tol=tol)


def _body(measure, check):
    bodies = measure.get("bodies") or []
    if not bodies:
        return None
    name = check.get("body")
    if name:
        for b in bodies:
            if b["name"] == name:
                return b
        return None
    return bodies[0]


def compare(spec, measure):
    params_spec = spec.get("parameters") or {}
    resolved = expr.resolve_parameters(params_spec)
    rows = []
    for check in spec.get("checks") or []:
        kind = check.get("kind")
        try:
            rows.extend(_COMPARERS[kind](check, measure, resolved, params_spec))
        except KeyError as e:
            rows.append(_row(check, "E201", "ERROR", "검증 항목 오류", "unknown kind or name: %s" % e))
        except Exception as e:  # never fail silently
            rows.append(_row(check, "E401", "ERROR", "검증 실행 실패", "%s: %s" % (type(e).__name__, e)))
    return rows


def _cmp_bbox(check, measure, resolved, params_spec):
    body = _body(measure, check)
    if body is None:
        return [_row(check, "E401", "ERROR", "바디 없음", "측정할 솔리드 바디가 없습니다")]
    out = []
    for axis, raw, actual in zip("XYZ", check["expected"], body["bbox"]["size"]):
        if raw is None:
            continue
        out.append(_dim_row(check, "bbox %s" % axis, expr.evaluate(raw, resolved), actual,
                            _tol_for(check, raw, params_spec), "E401", "바운딩 박스 불일치"))
    return out


def _holes(measure, check):
    if check.get("feature"):
        f = (measure.get("features") or {}).get(check["feature"]) or {}
        if not f.get("found"):
            return None
        return f.get("holes") or []
    body = _body(measure, check)
    return None if body is None else body.get("holes") or []


def _cmp_hole_count(check, measure, resolved, params_spec):
    holes = _holes(measure, check)
    if holes is None:
        return [_row(check, "E403", "ERROR", "구멍 측정 불가", "feature/body not found")]
    if "diameter" in check:
        d = expr.evaluate(check["diameter"], resolved)
        tol = check.get("diameter_tol", _tol_for(check, check["diameter"], params_spec))
        holes = [h for h in holes if judge(d, h["diameter"], tol)[0]]
    expected = int(expr.evaluate(check["expected"], resolved))
    if len(holes) == expected:
        return [_row(check, None, None, "구멍 개수", "%d개 OK" % expected, expected=expected,
                     actual=len(holes))]
    return [_row(check, "E403", "ERROR", "구멍 개수 불일치", "기대 %d개, 실측 %d개" % (expected, len(holes)),
                 expected=expected, actual=len(holes))]


def _cmp_hole_diameter(check, measure, resolved, params_spec):
    holes = _holes(measure, check)
    if not holes:
        return [_row(check, "E402", "ERROR", "구멍 없음", "검사할 구멍을 찾지 못했습니다")]
    raw = check["expected"]
    exp = expr.evaluate(raw, resolved)
    tol = _tol_for(check, raw, params_spec)
    diameters = sorted({round(h["diameter"], 4) for h in holes}, key=lambda d: abs(d - exp))
    if not check.get("feature"):
        # body scope: counterbores etc. add other cylinders; compare the closest group only
        return [_dim_row(check, "Ø", exp, diameters[0], tol, "E402", "구멍 지름 불일치")]
    # feature scope: every hole made by this feature must match
    rows = []
    for d in diameters:
        n = sum(1 for h in holes if round(h["diameter"], 4) == d)
        row = _dim_row(check, "Ø (%d개)" % n, exp, d, tol, "E402", "구멍 지름 불일치")
        row["where_holes"] = [h["axis_point"][:2] for h in holes if round(h["diameter"], 4) == d]
        if not row["pass"]:
            row["message"] += " · 위치 %s" % ", ".join("(%.1f, %.1f)" % tuple(p) for p in row["where_holes"])
        rows.append(row)
    return rows


def _cmp_hole_positions(check, measure, resolved, params_spec):
    holes = _holes(measure, check)
    if holes is None:
        return [_row(check, "E404", "ERROR", "구멍 측정 불가", "feature/body not found")]
    if "diameter" in check:
        d = expr.evaluate(check["diameter"], resolved)
        holes = [h for h in holes if abs(h["diameter"] - d) <= float(check.get("diameter_tol", 0.01))]
    # holes along `axis` (default Z): centre = the two other coordinates of the axis point
    ai = "XYZ".index(str(check.get("axis", "Z")).upper())
    oi = [i for i in range(3) if i != ai]
    centers = [(h["axis_point"][oi[0]], h["axis_point"][oi[1]]) for h in holes
               if abs(abs(h["axis_dir"][ai]) - 1) < 1e-6]
    tol = check.get("tol", DEFAULT_TOL_MM)
    out = []
    for k, pos in enumerate(check["expected"]):
        ex, ey = expr.evaluate(pos[0], resolved), expr.evaluate(pos[1], resolved)
        if not centers:
            out.append(_row(check, "E404", "ERROR", "구멍 위치 불일치",
                            "[%d] (%.3f, %.3f) 위치에 구멍이 없습니다" % (k, ex, ey)))
            continue
        cx, cy = min(centers, key=lambda c: (c[0] - ex) ** 2 + (c[1] - ey) ** 2)
        dist = ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5
        if dist <= float(tol) + 1e-9:
            out.append(_row(check, None, None, "구멍 위치", "[%d] OK" % k, expected=[ex, ey],
                            actual=[cx, cy], delta=dist))
        else:
            out.append(_row(check, "E404", "ERROR", "구멍 위치 불일치",
                            "[%d] 기대 (%.3f, %.3f), 가장 가까운 구멍 (%.3f, %.3f), 거리 %.3f mm"
                            % (k, ex, ey, cx, cy, dist), expected=[ex, ey], actual=[cx, cy], delta=dist))
    return out


def _cmp_volume(check, measure, resolved, params_spec):
    body = _body(measure, check)
    if body is None:
        return [_row(check, "E405", "ERROR", "바디 없음", "측정할 솔리드 바디가 없습니다")]
    actual = body["volume"]
    if check.get("expected") is None:
        return [_row(check, None, None, "부피", "기록만: %.2f mm³" % actual, actual=actual)]
    exp = expr.evaluate(check["expected"], resolved)
    rel = float(check.get("rel_tol", VOLUME_REL_TOL))
    delta = actual - exp
    if abs(delta) <= abs(exp) * rel:
        return [_row(check, None, None, "부피", "OK (%.2f mm³)" % actual, expected=exp, actual=actual,
                     delta=delta)]
    return [_row(check, "E405", "WARN", "부피 편차 과다", "기대 %.2f, 실측 %.2f mm³ (%+.2f%%)"
                 % (exp, actual, 100.0 * delta / exp if exp else 0), expected=exp, actual=actual,
                 delta=delta)]


def _cmp_plane(check, measure, resolved, params_spec):
    """A flat face perpendicular to `axis` must exist at coordinate `at` (mm)."""
    body = _body(measure, check)
    if body is None:
        return [_row(check, "E408", "ERROR", "바디 없음", "측정할 솔리드 바디가 없습니다")]
    axis = str(check.get("axis", "Z")).upper()
    raw = check["at"]
    at = expr.evaluate(raw, resolved)
    tol = _tol_for(check, raw, params_spec)
    planes = [p for p in body.get("planes") or [] if p["axis"] == axis]
    label = "%s=%s 평면" % (axis, ("%.3f" % at).rstrip("0").rstrip("."))
    if not planes:
        return [_row(check, "E408", "ERROR", "평면 위치 불일치", "%s 축에 수직인 평면이 없습니다" % axis,
                     label=label, expected=at)]
    best = min(planes, key=lambda p: abs(p["at"] - at))
    if abs(best["at"] - at) <= tol + 1e-9:
        return [_row(check, None, None, "평면 위치", "%s OK" % label, label=label, expected=at,
                     actual=best["at"], delta=best["at"] - at)]
    others = sorted({p["at"] for p in planes})
    return [_row(check, "E408", "ERROR", "평면 위치 불일치",
                 "%s 이 없습니다 (가장 가까운 %s=%.3f, 이 축의 평면: %s) — 형상이 다른 쪽/다른 높이에 만들어졌을 수 있습니다"
                 % (label, axis, best["at"], ", ".join("%.3f" % v for v in others[:8])),
                 label=label, expected=at, actual=best["at"], delta=best["at"] - at)]


def _cmp_thread(check, measure, resolved, params_spec):
    """Number of hole faces carrying a thread of this designation (e.g. 4 x M6x1)."""
    want = str(check.get("designation", ""))
    expected = int(expr.evaluate(check["expected"], resolved))
    found = sum(t.get("faces", 0) for t in measure.get("threads") or [] if t.get("designation") == want)
    if found == expected:
        return [_row(check, None, None, "나사", "%s %d개 OK" % (want, found), label="나사 %s" % want,
                     expected=expected, actual=found)]
    have = ", ".join("%s x%d" % (t.get("designation"), t.get("faces", 0)) for t in measure.get("threads") or []) or "없음"
    return [_row(check, "E409", "ERROR", "나사 불일치", "%s 기대 %d개, 실측 %d개 (모델의 나사: %s)"
                 % (want, expected, found, have), label="나사 %s" % want, expected=expected, actual=found)]


_COMPARERS = {
    "thread": _cmp_thread,
    "plane": _cmp_plane,
    "bbox": _cmp_bbox,
    "hole_count": _cmp_hole_count,
    "hole_diameter": _cmp_hole_diameter,
    "hole_positions": _cmp_hole_positions,
    "volume": _cmp_volume,
}


def checked_features(spec):
    return sorted({c["feature"] for c in spec.get("checks") or [] if c.get("feature")})
