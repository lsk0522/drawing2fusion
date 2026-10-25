"""E411 - circles drawn in the views vs the model's cylinders (independent of the spec).

Every visible full circle in a view is a cylinder seen end-on: a plan-view circle is a Z-axis
hole or boss at (X, Y), a front-view circle a Y-axis one at (X, Z), a side-view circle an X-axis
one at (Y, Z). The reader records them in model millimetres (extract-report "view_circles");
after the build each one must have a model cylinder with the same axis, centre and diameter.
And the other way: a full model hole whose axis view exists must appear in that view as a
circle (visible or dashed). Because the expectations come from the drawing lines, a hole the
spec forgot - together with its checks - is still caught.
"""

import math

AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
VIEW_LABEL = {"top": "평면도", "front": "정면도", "side": "측면도"}
CENTER_TOL = 0.06      # mm
DIA_TOL = 0.06         # mm (Fusion opens a tapped hole to the thread's minor diameter, e.g. Ø5 -> Ø5.035)


def _uv(axis, p):
    """Model point -> the two coordinates seen in the view along `axis`."""
    return {"Z": (p[0], p[1]), "Y": (p[0], p[2]), "X": (p[1], p[2])}[axis]


def _cylinders(measure, axis):
    want = AXES[axis]
    out = []
    for b in measure.get("bodies", []):
        for kind in ("holes", "bosses"):
            for c in b.get(kind) or []:
                d = c.get("axis_dir") or [0, 0, 0]
                if abs(abs(d[0] * want[0] + d[1] * want[1] + d[2] * want[2]) - 1) < 1e-3:
                    out.append(dict(c, _kind="hole" if kind == "holes" else "boss"))
    return out


def compare_circles(view_circles, measure):
    """Rows for the verify report (one per view)."""
    rows = []
    for view, info in sorted((view_circles or {}).items()):
        axis = info["axis"]
        cyl = _cylinders(measure, axis)
        # entries: [u, v, r] or [u, v, r, r_alt] (tapped hole drawn at nominal size, modelled as tap drill)
        drawn = [tuple(c) for c in info.get("circles") or []]
        dashed = [tuple(c) for c in info.get("hidden") or []]

        def fits(d, c):
            return any(abs(d - 2 * r) <= max(DIA_TOL, 2 * r * 2e-3) for r in c[2:])
        missing, concentric_only = [], []
        for c in drawn:
            u, v, r = c[:3]
            hit = [m for m in cyl if math.dist(_uv(axis, m["axis_point"]), (u, v)) <= CENTER_TOL]
            if any(fits(m["diameter"], c) for m in hit):
                continue
            (concentric_only if hit else missing).append((u, v, r))
        extra = []
        for c in cyl:
            if c["_kind"] != "hole" or c.get("coverage", 1) < 0.9:
                continue
            cu, cv = _uv(axis, c["axis_point"])
            if not any(math.dist((cu, cv), d[:2]) <= CENTER_TOL and fits(c["diameter"], d) for d in drawn + dashed):
                extra.append((cu, cv, c["diameter"]))
        label = "%s 원 (%s축 원통)" % (VIEW_LABEL.get(view, view), axis)
        row = {"check": "c_circles_%s" % view, "kind": "view_circles", "label": label, "view": view,
               "expected": len(drawn), "actual": len(drawn) - len(missing) - len(concentric_only)}
        if missing or extra:
            parts = []
            if missing:
                parts.append("도면 원인데 모델에 원통이 없음: " + ", ".join("Ø%g @ (%.2f, %.2f)" % (round(2 * r, 3), u, v)
                                                                 for u, v, r in missing[:6]))
            if extra:
                parts.append("모델 구멍인데 도면에 원이 없음: " + ", ".join("Ø%g @ (%.2f, %.2f)" % (round(d, 3), u, v)
                                                                 for u, v, d in extra[:6]))
            row.update({"pass": False, "code": "E411", "severity": "ERROR", "title": "도면 원과 모델 원통 불일치",
                        "message": "; ".join(parts)})
        elif concentric_only:
            row.update({"pass": True, "code": None, "severity": None, "title": "도면 원 일치",
                        "message": "원 %d개 일치, 동심원 %d개는 원통이 아님 (카운터싱크·모따기면 정상): %s"
                        % (row["actual"], len(concentric_only),
                           ", ".join("Ø%g" % round(2 * r, 3) for _, _, r in concentric_only[:4]))})
        else:
            row.update({"pass": True, "code": None, "severity": None, "title": "도면 원 일치",
                        "message": "원 %d개 모두 모델 원통과 일치" % len(drawn)})
        rows.append(row)
    return rows
