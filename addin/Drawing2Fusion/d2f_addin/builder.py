"""Deterministic part-spec -> Fusion model builder.

Every feature in spec["features"] maps to one builder function. Input values
are mm / deg (numbers or parameter expressions); conversion to Fusion's
internal cm / rad happens only through expr.py.

Runs on Fusion's main thread only (called from the CustomEvent handler).
"""

import math
import time

import adsk.core
import adsk.fusion

from . import expr
from . import selectors
from .log import log

EPS_CM = 1e-6


class BuildError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Context:
    def __init__(self, app, design, spec, options):
        self.app = app
        self.design = design
        self.root = design.rootComponent
        self.spec = spec
        self.options = options or {}
        self.params = expr.resolve_parameters(spec.get("parameters"))
        # spec names Fusion rejects (L, W, T, ...) are registered as L_, W_, T_ ...
        self.names = expr.fusion_name_map((spec.get("parameters") or {}).keys())
        self.objects = {}          # spec id -> Fusion object (sketch / feature / plane)
        self.entities = {}         # "sketch_id.entity_id" -> sketch entity info
        self.placed = {}           # sketch name -> located points (for alignment)
        self.planes = {}           # sketch name -> "XY" | "XZ" | "YZ" (spec coordinate mapping)
        self.fallbacks = {}        # feature id -> note about an automatic rebuild
        self.sketch_planes = {}    # sketch id -> (plane, offset) for through-all fallbacks
        self.log = []

    # numeric value in mm/deg, for initial geometry placement
    def num(self, value):
        return expr.evaluate(value, self.params)

    def cm(self, value):
        return expr.mm_to_cm(self.num(value))

    def fx(self, value, unit="mm"):
        """Expression string for Fusion, with renamed parameters."""
        return expr.to_fusion_expression(expr.rename_identifiers(value, self.names), unit)

    def vi(self, value, unit="mm"):
        return adsk.core.ValueInput.createByString(self.fx(value, unit))


# --------------------------------------------------------------------------- document
def prepare_design(app, new_document=True):
    if new_document or app.activeProduct is None:
        app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise BuildError("E302", "active product is not a Fusion design")
    design.designType = adsk.fusion.DesignTypes.ParametricDesignType
    try:
        design.fusionUnitsManager.distanceDisplayUnits = \
            adsk.fusion.DistanceUnits.MillimeterDistanceUnits
    except Exception:
        pass
    return design


def register_parameters(ctx):
    ups = ctx.design.userParameters
    spec_params = ctx.spec.get("parameters") or {}
    # ctx.params is in resolution order, so referenced parameters exist first
    for name in ctx.params:
        p = spec_params[name]
        p = p if isinstance(p, dict) else {"value": p}
        unit = p.get("unit", "mm")
        fname = ctx.names.get(name, name)
        expression = ctx.fx(p.get("value"), unit)
        comment = _param_comment(p)
        if fname != name:
            comment = ("spec name '%s' " % name + comment).strip()
        existing = ups.itemByName(fname)
        try:
            if existing:
                existing.expression = expression
                existing.comment = comment
            else:
                ups.add(fname, adsk.core.ValueInput.createByString(expression), unit, comment)
        except Exception as e:
            if "not valid" not in str(e) or existing:
                raise BuildError("E206", "user parameter '%s' = '%s' rejected by Fusion: %s"
                                 % (fname, expression, e))
            # Fusion also rejects multi-letter unit names (e.g. 'pcd'); FUSION_RESERVED
            # can't list them all, so rename on the spot. Later parameters and features
            # are converted with ctx.fx(), which reads ctx.names at call time.
            fname = name + "_"
            while ups.itemByName(fname) or fname in ctx.params:
                fname += "_"
            ctx.names[name] = fname
            try:
                ups.add(fname, adsk.core.ValueInput.createByString(expression), unit,
                        ("spec name '%s' " % name + comment).strip())
            except Exception as e2:
                raise BuildError("E206", "user parameter '%s' (renamed '%s') = '%s' rejected by "
                                 "Fusion: %s" % (name, fname, expression, e2))
    if ctx.names:
        ctx.log.append({"id": "(parameters)", "status": "info", "code": "",
                        "message": "renamed for Fusion: %s" % ", ".join(
                            "%s->%s" % kv for kv in sorted(ctx.names.items()))})


def _param_comment(p):
    src = p.get("source") or {}
    t = src.get("type", "")
    bits = ["d2f", t]
    if src.get("view"):
        bits.append(src["view"])
    if src.get("label"):
        bits.append('"%s"' % src["label"])
    if "confidence" in p or "confidence" in src:
        bits.append("conf=%s" % p.get("confidence", src.get("confidence")))
    return " ".join(b for b in bits if b)


# --------------------------------------------------------------------------- planes
def resolve_plane(ctx, plane_name, offset, feature_id):
    root = ctx.root
    base = {"XY": root.xYConstructionPlane,
            "XZ": root.xZConstructionPlane,
            "YZ": root.yZConstructionPlane}.get(str(plane_name).upper())
    if base is None:
        if plane_name in ctx.objects:
            return ctx.objects[plane_name]
        raise BuildError("E301", "unknown plane '%s'" % plane_name)
    if offset in (None, 0, "0"):
        return base
    planes = root.constructionPlanes
    pin = planes.createInput()
    pin.setByOffset(base, ctx.vi(offset))
    plane = planes.add(pin)
    plane.name = feature_id + "_plane"
    return plane


# --------------------------------------------------------------------------- sketch
def build_sketch(ctx, f):
    plane = resolve_plane(ctx, f.get("plane", "XY"), f.get("offset", 0), f["id"])
    sk = ctx.root.sketches.add(plane)
    sk.name = f["id"]
    kind = str(f.get("plane", "XY")).upper()
    ctx.planes[sk.name] = kind if kind in ("XY", "XZ", "YZ") else "XY"
    ctx.sketch_planes[f["id"]] = (ctx.planes[sk.name], f.get("offset", 0))
    auto = f.get("constraints", "auto") == "auto"
    sk.isComputeDeferred = True
    try:
        for ent in f.get("entities", []):
            kind = ent.get("kind")
            fn = _SKETCH_ENTITIES.get(kind)
            if fn is None:
                raise BuildError("E301", "sketch entity kind '%s' not supported yet" % kind)
            fn(ctx, sk, ent, auto)
    finally:
        sk.isComputeDeferred = False
    if sk.profiles.count == 0 and f.get("require_profile", True):
        raise BuildError("E301", "sketch '%s' has no closed profile" % f["id"])
    return sk


def _pt(ctx, sk, xy):
    """Spec point [u, v] (mm) -> sketch-space Point3D (cm).

    Spec coordinates are world axes: XY plane -> (X, Y), XZ -> (X, Z), YZ -> (Y, Z).
    Fusion's sketch axes differ (checked on 2704): an XZ sketch maps (x, y) to
    world (x, 0, -y) and a YZ sketch maps (x, y) to world (0, y, -x), so the
    point is converted through modelToSketchSpace.
    """
    u, v = ctx.cm(xy[0]), ctx.cm(xy[1])
    kind = ctx.planes.get(sk.name, "XY")
    if kind == "XY":
        return adsk.core.Point3D.create(u, v, 0)
    world = {"XZ": (u, 0, v), "YZ": (0, u, v)}[kind]
    p = sk.modelToSketchSpace(adsk.core.Point3D.create(*world))
    return adsk.core.Point3D.create(p.x, p.y, 0)


def _axes(ctx, sk):
    """Sketch dimension orientation for spec u and spec v."""
    if ctx.planes.get(sk.name, "XY") == "YZ":
        return _V(), _H()
    return _H(), _V()


def _expr_abs(ctx, value):
    """Driving-dimension expression that is always positive."""
    if ctx.num(value) >= 0:
        return ctx.fx(value)
    if isinstance(value, (int, float)):
        return ctx.fx(-value)
    return "-(%s)" % ctx.fx(value)


def _diff_expr(ctx, a, b):
    """Positive expression for |b - a| (segment length from point expressions)."""
    va, vb = ctx.num(a), ctx.num(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return ctx.fx(abs(vb - va))
    hi, lo = (b, a) if vb >= va else (a, b)
    return "(%s) - (%s)" % (ctx.fx(hi), ctx.fx(lo))


def _H():
    return adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation


def _V():
    return adsk.fusion.DimensionOrientations.VerticalDimensionOrientation


def _dim(sk, p1, p2, orientation, text_pt, expression):
    d = sk.sketchDimensions.addDistanceDimension(p1, p2, orientation, text_pt)
    d.parameter.expression = expression
    return d


def _try(fn, what="constraint"):
    try:
        return fn()
    except Exception as e:
        log("%s skipped: %s" % (what, e))
        return None


def _placed(ctx, sk):
    return ctx.placed.setdefault(sk.name, [])


def _locate_point(ctx, sk, point, xy):
    """Fix a sketch point relative to the sketch origin.

    Fusion's solver (2704) rejects a second origin dimension along an axis when
    another point already has the same coordinate (VCS_SKETCH_OVER_CONSTRAINTS),
    so points that share an x or y with an already placed point are aligned to
    it with a vertical/horizontal-points constraint instead. Identical
    expressions are aligned directly; numerically equal but differently written
    ones only as a fallback when the dimension is refused.
    """
    origin = sk.originPoint
    gc = sk.geometricConstraints
    x, y = ctx.num(xy[0]), ctx.num(xy[1])
    placed = _placed(ctx, sk)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        gc.addCoincident(point, origin)
        placed.append((xy[0], xy[1], x, y, point))
        return
    g = point.geometry
    tp = adsk.core.Point3D.create(g.x / 2.0, g.y / 2.0 - 0.5, 0)

    def align_for(orient):
        # same sketch-x -> vertical alignment, same sketch-y -> horizontal alignment
        if orient == _H():
            return lambda a, b: gc.addVerticalPoints(a, b)
        return lambda a, b: gc.addHorizontalPoints(a, b)

    def axis(idx, value, raw, orient):
        align = align_for(orient)
        if abs(value) < 1e-9:
            align(origin, point)
            return
        same_expr = [p for p in placed if str(p[idx]) == str(raw) and abs(p[idx + 2] - value) < 1e-9]
        if same_expr:
            align(same_expr[0][4], point)
            return
        try:
            _dim(sk, origin, point, orient, tp, _expr_abs(ctx, raw))
        except Exception:
            same_val = [p for p in placed if abs(p[idx + 2] - value) < 1e-9]
            if not same_val:
                raise
            align(same_val[0][4], point)

    o_u, o_v = _axes(ctx, sk)
    axis(0, x, xy[0], o_u)
    axis(1, y, xy[1], o_v)
    placed.append((xy[0], xy[1], x, y, point))


def _constrain_axis_aligned(sk, ln):
    """Add horizontal/vertical constraint to an axis-aligned line. Returns 'H', 'V' or None."""
    a, b = ln.startSketchPoint.geometry, ln.endSketchPoint.geometry
    gc = sk.geometricConstraints
    if abs(a.y - b.y) < EPS_CM:
        _try(lambda: gc.addHorizontal(ln))
        return "H"
    if abs(a.x - b.x) < EPS_CM:
        _try(lambda: gc.addVertical(ln))
        return "V"
    return None


def _ent_rect(ctx, sk, ent, auto):
    corner = ent.get("corner", [0, 0])
    size = ent["size"]
    p0 = _pt(ctx, sk, corner)
    x1 = ctx.num(corner[0]) + ctx.num(size[0])
    y1 = ctx.num(corner[1]) + ctx.num(size[1])
    p1 = _pt(ctx, sk, [x1, y1])
    lines = sk.sketchCurves.sketchLines.addTwoPointRectangle(p0, p1)
    if not auto:
        return
    # addTwoPointRectangle adds no horizontal/vertical constraints (checked on 2704)
    horiz = vert = None
    for i in range(lines.count):
        ln = lines.item(i)
        kind = _constrain_axis_aligned(sk, ln)
        if kind == "H" and horiz is None:
            horiz = ln
        elif kind == "V" and vert is None:
            vert = ln
    size_h, size_v = (size[0], size[1]) if _axes(ctx, sk)[0] == _H() else (size[1], size[0])
    _dim(sk, horiz.startSketchPoint, horiz.endSketchPoint, _H(),
         adsk.core.Point3D.create(p0.x + (p1.x - p0.x) / 2, min(p0.y, p1.y) - 0.8, 0),
         _expr_abs(ctx, size_h))
    _dim(sk, vert.startSketchPoint, vert.endSketchPoint, _V(),
         adsk.core.Point3D.create(min(p0.x, p1.x) - 0.8, p0.y + (p1.y - p0.y) / 2, 0),
         _expr_abs(ctx, size_v))
    best = None
    for i in range(lines.count):
        sp = lines.item(i).startSketchPoint
        d = sp.geometry.distanceTo(p0)
        if best is None or d < best[0]:
            best = (d, sp)
    _locate_point(ctx, sk, best[1], corner)


def _ent_circle(ctx, sk, ent, auto):
    center = ent["center"]
    dia = ent.get("diameter")
    if dia is None:
        dia = "2*(%s)" % ent["radius"] if isinstance(ent["radius"], str) else 2 * ent["radius"]
    c = sk.sketchCurves.sketchCircles.addByCenterRadius(_pt(ctx, sk, center), ctx.cm(dia) / 2.0)
    key = "%s.%s" % (sk.name, ent.get("id", "c%d" % sk.sketchCurves.sketchCircles.count))
    ctx.entities[key] = c
    if not auto:
        return
    cp = c.centerSketchPoint.geometry
    d = sk.sketchDimensions.addDiameterDimension(
        c, adsk.core.Point3D.create(cp.x + ctx.cm(dia), cp.y + ctx.cm(dia), 0))
    d.parameter.expression = ctx.fx(dia)
    _locate_point(ctx, sk, c.centerSketchPoint, center)


def _ent_line(ctx, sk, ent, auto):
    sk.sketchCurves.sketchLines.addByTwoPoints(_pt(ctx, sk, ent["start"]), _pt(ctx, sk, ent["end"]))


def _ent_polyline(ctx, sk, ent, auto):
    """Closed/open polyline. Axis-aligned segments get H/V constraints and a
    length dimension built from the point expressions; dimensions Fusion
    refuses as redundant (the closing segment of a loop) are skipped."""
    pts = list(ent["points"])
    closed = ent.get("closed", True)
    lines = sk.sketchCurves.sketchLines
    segs = []
    first = prev = None
    for i in range(1, len(pts)):
        start = prev.endSketchPoint if prev else _pt(ctx, sk, pts[0])
        ln = lines.addByTwoPoints(start, _pt(ctx, sk, pts[i]))
        segs.append((ln, pts[i - 1], pts[i]))
        first = first or ln
        prev = ln
    if closed and first is not None:
        ln = lines.addByTwoPoints(prev.endSketchPoint, first.startSketchPoint)
        segs.append((ln, pts[-1], pts[0]))
    if not auto or first is None:
        return
    iu = 0 if _axes(ctx, sk)[0] == _H() else 1   # spec index that varies along sketch x
    for ln, a, b in segs:
        kind = _constrain_axis_aligned(sk, ln)
        mid = ln.startSketchPoint.geometry
        end = ln.endSketchPoint.geometry
        tp = adsk.core.Point3D.create((mid.x + end.x) / 2 + 0.4, (mid.y + end.y) / 2 + 0.4, 0)
        if kind == "H":
            _try(lambda: _dim(sk, ln.startSketchPoint, ln.endSketchPoint, _H(), tp,
                              _diff_expr(ctx, a[iu], b[iu])), "redundant dimension")
        elif kind == "V":
            _try(lambda: _dim(sk, ln.startSketchPoint, ln.endSketchPoint, _V(), tp,
                              _diff_expr(ctx, a[1 - iu], b[1 - iu])), "redundant dimension")
    _locate_point(ctx, sk, first.startSketchPoint, pts[0])
    # vertices of slanted segments have no length dimension: pin them to the origin too
    kinds = [_seg_kind(ln) for ln, _, _ in segs]
    for i in range(1, len(segs)):
        if kinds[i - 1] is None or kinds[i] is None:
            _try(lambda: _locate_point(ctx, sk, segs[i][0].startSketchPoint, pts[i]), "vertex dimension")


def _seg_kind(ln):
    a, b = ln.startSketchPoint.geometry, ln.endSketchPoint.geometry
    if abs(a.y - b.y) < EPS_CM:
        return "H"
    if abs(a.x - b.x) < EPS_CM:
        return "V"
    return None


def _ent_arc(ctx, sk, ent, auto):
    sweep = expr.deg_to_rad(ctx.num(ent["sweep"]))
    sk.sketchCurves.sketchArcs.addByCenterStartSweep(_pt(ctx, sk, ent["center"]), _pt(ctx, sk, ent["start"]), sweep)


def _ent_slot(ctx, sk, ent, auto):
    """Center-to-center slot: {start: [u, v], end: [u, v], width}."""
    res = sk.addCenterToCenterSlot(_pt(ctx, sk, ent["start"]), _pt(ctx, sk, ent["end"]),
                                   ctx.vi(ent["width"]), auto)
    if not auto:
        return
    arcs = [r for r in res if r.objectType == adsk.fusion.SketchArc.classType()]
    for arc, xy in zip(arcs, (ent["start"], ent["end"])):
        _locate_point(ctx, sk, arc.centerSketchPoint, xy)


def _arc_mid(ctx, a, b, c, ccw):
    """Point halfway along the arc a -> b around c (spec mm), turning CCW in spec axes if ccw."""
    ax, ay, bx, by = ctx.num(a[0]), ctx.num(a[1]), ctx.num(b[0]), ctx.num(b[1])
    cx, cy = ctx.num(c[0]), ctx.num(c[1])
    a0 = math.atan2(ay - cy, ax - cx)
    a1 = math.atan2(by - cy, bx - cx)
    if ccw:
        sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    else:
        sweep = -((a0 - a1) % (2 * math.pi) or 2 * math.pi)
    r = math.hypot(ax - cx, ay - cy)
    t = a0 + sweep / 2
    return [cx + r * math.cos(t), cy + r * math.sin(t)], r


def _ent_path(ctx, sk, ent, auto):
    """Closed outline of lines and arcs, exactly as drawn:
    {start: [u, v], segments: [{type: line, to}, {type: arc, to, center, ccw}], closed: true}.

    Arcs go through three points (start, middle, end) computed in spec coordinates, so the
    plane's axis flip (XZ / YZ sketches) cannot reverse their direction. With auto constraints
    every vertex is dimensioned to the origin and every arc gets its radius, which fixes the
    whole loop (an arc with both ends fixed has one freedom left: its radius)."""
    segs = ent.get("segments") or []
    if not segs:
        raise BuildError("E301", "path '%s' has no segments" % ent.get("id", "?"))
    lines, arcs = sk.sketchCurves.sketchLines, sk.sketchCurves.sketchArcs
    closed = ent.get("closed", True)
    cur_xy = ent["start"]
    first_pt = None
    cur = _pt(ctx, sk, cur_xy)
    made = []                       # (curve, end spec xy, kind, radius)
    for i, s in enumerate(segs):
        last = closed and i == len(segs) - 1
        to_xy = s["to"]
        end = first_pt if (last and first_pt is not None) else _pt(ctx, sk, to_xy)
        if s.get("type") == "arc":
            mid, r = _arc_mid(ctx, cur_xy, to_xy, s["center"], s.get("ccw", True))
            crv = arcs.addByThreePoints(cur, _pt(ctx, sk, mid), end)
            made.append((crv, to_xy, "arc", s.get("radius_param") or r))
        else:
            crv = lines.addByTwoPoints(cur, end)
            made.append((crv, to_xy, "line", None))
        if first_pt is None:
            first_pt = crv.startSketchPoint
        cur, cur_xy = crv.endSketchPoint, to_xy
    if closed:
        tail = made[-1][0].endSketchPoint
        if tail is not first_pt and tail.geometry.distanceTo(first_pt.geometry) < EPS_CM:
            _try(lambda: sk.geometricConstraints.addCoincident(tail, first_pt), "path closing point")
    if not auto:
        return
    _try(lambda: _locate_point(ctx, sk, first_pt, ent["start"]), "path vertex dimension")
    count = len(made) - (1 if closed else 0)
    for k in range(count):
        crv, xy, _, _ = made[k]
        _try(lambda: _locate_point(ctx, sk, crv.endSketchPoint, xy), "path vertex dimension")
    for crv, _, kind, r in made:
        if kind != "arc":
            continue
        cp = crv.centerSketchPoint.geometry
        _try(lambda: _radius(ctx, sk, crv, cp, r), "arc radius dimension")


def _radius(ctx, sk, arc, cp, r):
    """Radius dimension; r is a number (mm) or a parameter name from the drawing."""
    r_cm = ctx.cm(r)
    d = sk.sketchDimensions.addRadialDimension(arc, adsk.core.Point3D.create(cp.x + r_cm, cp.y + r_cm, 0))
    d.parameter.expression = ctx.fx(r) if isinstance(r, str) else "%s mm" % repr(round(r, 6))
    return d


_SKETCH_ENTITIES = {"rect": _ent_rect, "circle": _ent_circle, "line": _ent_line,
                    "polyline": _ent_polyline, "arc": _ent_arc, "slot": _ent_slot,
                    "path": _ent_path}


# --------------------------------------------------------------------------- solids
_OPS = {
    "new_body": "NewBodyFeatureOperation",
    "join": "JoinFeatureOperation",
    "cut": "CutFeatureOperation",
    "intersect": "IntersectFeatureOperation",
}


def _operation(name):
    attr = _OPS.get(name or "new_body")
    if attr is None:
        raise BuildError("E201", "unknown operation '%s'" % name)
    return getattr(adsk.fusion.FeatureOperations, attr)


def _profiles(ctx, sketch_id, which):
    sk = ctx.objects.get(sketch_id)
    if sk is None:
        raise BuildError("E206", "profile sketch '%s' not built" % sketch_id)
    profs = [sk.profiles.item(i) for i in range(sk.profiles.count)]
    if not profs:
        raise BuildError("E301", "sketch '%s' has no profiles" % sketch_id)
    which = which or "all"
    if which in ("largest", "smallest"):
        profs.sort(key=lambda p: p.areaProperties().area, reverse=(which == "largest"))
        return profs[0]
    if isinstance(which, list):
        profs = [profs[i] for i in which]
    if len(profs) == 1:
        return profs[0]
    coll = adsk.core.ObjectCollection.create()
    for p in profs:
        coll.add(p)
    return coll


def _direction(name):
    D = adsk.fusion.ExtentDirections
    return {"positive": D.PositiveExtentDirection,
            "negative": D.NegativeExtentDirection,
            "symmetric": D.SymmetricExtentDirection}[name or "positive"]


def _extrude(ctx, fid, profile, distance, direction, operation, plane_info=None):
    exts = ctx.root.features.extrudeFeatures
    inp = exts.createInput(profile, operation)
    if distance == "through_all":
        if direction == "symmetric":
            inp.setTwoSidesExtent(adsk.fusion.ThroughAllExtentDefinition.create(),
                                  adsk.fusion.ThroughAllExtentDefinition.create())
        else:
            inp.setOneSideExtent(adsk.fusion.ThroughAllExtentDefinition.create(), _direction(direction))
    elif direction == "symmetric":
        inp.setDistanceExtent(True, ctx.vi(distance))
    else:
        inp.setOneSideExtent(adsk.fusion.DistanceExtentDefinition.create(ctx.vi(distance)),
                             _direction(direction))
    ext = exts.add(inp)
    if distance == "through_all" and direction != "symmetric" and \
            ext.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
        # Fusion 2704: a multi-profile "through all" cut whose sketch plane lies on an inner
        # face (e.g. holes next to a hub) ends in a warning state and cuts nothing.
        # Rebuild it as a distance cut to the far side of the body.
        reason = (ext.errorOrWarningMessage or "")[:80]
        ext.deleteMe()
        dist = _through_distance(ctx, plane_info, direction)
        inp = exts.createInput(profile, operation)
        inp.setOneSideExtent(adsk.fusion.DistanceExtentDefinition.create(
            adsk.core.ValueInput.createByString(dist)), _direction(direction))
        ext = exts.add(inp)
        ctx.fallbacks[fid] = "관통 자르기가 실패해서 거리 %s 로 다시 만들었습니다 (%s)" % (dist, reason)
    ext.name = fid
    return ext


def _through_distance(ctx, plane_info, direction):
    """Distance expression from the sketch plane to the far side of the body."""
    plane, offset = plane_info or ("XY", 0)
    axis = {"XY": "z", "XZ": "y", "YZ": "x"}.get(str(plane).upper(), "z")
    bodies = ctx.root.bRepBodies
    lo, hi = 1e9, -1e9
    for i in range(bodies.count):
        bb = bodies.item(i).boundingBox
        lo = min(lo, getattr(bb.minPoint, axis))
        hi = max(hi, getattr(bb.maxPoint, axis))
    level = ctx.cm(offset)
    if direction == "negative" and abs(lo) < 1e-6 and level > 1e-6:
        return ctx.fx(offset)                 # parametric: plane offset down to 0
    span = (level - lo) if direction == "negative" else (hi - level)
    if span <= 1e-6:                          # plane at/after the body's end: use the whole body
        span = hi - lo
    return "%s mm" % repr(round(expr.cm_to_mm(span), 6))


def build_extrude(ctx, f):
    profile = _profiles(ctx, f["profile"], f.get("profiles"))
    return _extrude(ctx, f["id"], profile, f["distance"], f.get("direction", "positive"),
                    _operation(f.get("operation")), ctx.sketch_planes.get(f["profile"]))


def build_revolve(ctx, f):
    profile = _profiles(ctx, f["profile"], f.get("profiles"))
    root = ctx.root
    axis = {"X": root.xConstructionAxis, "Y": root.yConstructionAxis,
            "Z": root.zConstructionAxis}.get(str(f.get("axis", "X")).upper())
    if axis is None:
        raise BuildError("E201", "revolve axis must be X, Y or Z for now")
    revs = root.features.revolveFeatures
    inp = revs.createInput(profile, axis, _operation(f.get("operation")))
    inp.setAngleExtent(False, ctx.vi(f.get("angle", 360), "deg"))
    rev = revs.add(inp)
    rev.name = f["id"]
    return rev


def build_hole(ctx, f):
    """Holes as sketch circles + cut extrude (deterministic, easy to measure).

    simple:      one cut, `diameter` x `depth`
    counterbore: extra cut `cb_diameter` x `cb_depth` named <id>_cb
    """
    hole_type = f.get("hole_type", "simple")
    if hole_type not in ("simple", "counterbore", "countersink"):
        raise BuildError("E302", "hole_type '%s' not supported yet" % hole_type)
    if hole_type == "countersink" and float(ctx.num(f.get("cs_angle", 90))) != 90.0:
        raise BuildError("E302", "only 90 deg countersinks are supported (got %s)" % f.get("cs_angle"))
    face = f.get("face", {"plane": "XY", "offset": 0})
    direction = f.get("direction", "negative")

    def circles_sketch(sid, dia):
        sk_spec = {"id": sid, "plane": face.get("plane", "XY"), "offset": face.get("offset", 0),
                   "entities": [{"kind": "circle", "id": "p%d" % i, "center": pos, "diameter": dia}
                                for i, pos in enumerate(f["positions"])]}
        sk = build_sketch(ctx, sk_spec)
        ctx.objects[sid] = sk
        return sk

    if hole_type == "counterbore":
        circles_sketch(f["id"] + "_cb_sk", f["cb_diameter"])
        prof = _profiles(ctx, f["id"] + "_cb_sk", "all")
        _extrude(ctx, f["id"] + "_cb", prof, f["cb_depth"], direction, _operation("cut"),
                 (face.get("plane", "XY"), face.get("offset", 0)))

    circles_sketch(f["id"] + "_sk", f["diameter"])
    prof = _profiles(ctx, f["id"] + "_sk", "all")
    cut = _extrude(ctx, f["id"], prof, f.get("depth", "through_all"), direction, _operation("cut"),
                   (face.get("plane", "XY"), face.get("offset", 0)))
    if hole_type == "countersink":
        _countersink(ctx, f, cut, face)
    if f.get("thread"):
        _thread(ctx, f, cut)
    return cut


def _thread(ctx, f, cut):
    """Cosmetic (or modelled) ISO metric thread on the hole's cylinder faces."""
    th = f["thread"]
    r = ctx.cm(f["diameter"]) / 2.0
    faces = adsk.core.ObjectCollection.create()
    for i in range(cut.faces.count):
        fc = cut.faces.item(i)
        g = fc.geometry
        if g.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType and abs(g.radius - r) < 1e-4:
            faces.add(fc)
    if faces.count == 0:
        raise BuildError("E305", "thread %s: no hole cylinder found" % f["id"])
    tf = ctx.root.features.threadFeatures
    q = tf.threadDataQuery
    ttype = th.get("type", "ISO Metric profile")
    designation = th.get("designation")
    cls = th.get("class", "6H")
    ok_designation = False
    try:
        size = ("%g" % float(th["size"])) if th.get("size") else None
        sizes = list(q.allSizes(ttype))
        cand = [sz for sz in sizes if size is not None and abs(float(sz) - float(size)) < 1e-6] if size else []
        if cand:
            ok_designation = designation in list(q.allDesignations(ttype, cand[0]))
            if ok_designation:
                classes = list(q.allClasses(True, ttype, designation))
                if cls not in classes and classes:
                    cls = classes[0]
    except Exception:
        ok_designation = False
    if not ok_designation:
        res = q.recommendThreadData(r * 2.0, True, ttype)
        if not res or not res[0]:
            raise BuildError("E305", "thread %s: Fusion has no %s %s" % (f["id"], ttype, designation))
        ctx.fallbacks[f["id"] + "_thread"] = "나사 규격 %s 을 찾지 못해 Fusion 추천 %s 로 넣었습니다" % (designation, res[1])
        designation, cls = res[1], res[2]
    info = tf.createThreadInfo(True, ttype, designation, cls)
    inp = tf.createInput(faces, info)
    inp.isModeled = bool(th.get("modeled", False))
    inp.isFullLength = True
    feat = tf.add(inp)
    feat.name = f["id"] + "_thread"
    return feat


def _countersink(ctx, f, cut, face):
    """90 deg countersink = equal-distance chamfer (cs_d - d)/2 on the hole's entry edges."""
    plane = str(face.get("plane", "XY")).upper()
    level = ctx.cm(face.get("offset", 0))
    coord = {"XY": "z", "XZ": "y", "YZ": "x"}[plane]
    edges = adsk.core.ObjectCollection.create()
    seen = set()
    for i in range(cut.faces.count):
        fc = cut.faces.item(i)
        for j in range(fc.edges.count):
            e = fc.edges.item(j)
            g = e.geometry
            if g.objectType != adsk.core.Circle3D.classType() or e.tempId in seen:
                continue
            if abs(getattr(g.center, coord) - level) < 1e-6:
                seen.add(e.tempId)
                edges.add(e)
    if edges.count != len(f["positions"]):
        raise BuildError("E305", "countersink %s: found %d entry edges for %d holes"
                         % (f["id"], edges.count, len(f["positions"])))
    dist = "((%s) - (%s)) / 2" % (ctx.fx(f["cs_diameter"]), ctx.fx(f["diameter"]))
    chamfers = ctx.root.features.chamferFeatures
    inp = chamfers.createInput2()
    inp.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edges, adsk.core.ValueInput.createByString(dist), False)
    ch = chamfers.add(inp)
    ch.name = f["id"] + "_cs"
    return ch


def _edges(ctx, f):
    sel = f.get("edges") or {}
    edges = selectors.select_edges(ctx, sel)
    expect = sel.get("expect")
    if len(edges) == 0 or (expect is not None and len(edges) != int(expect)):
        raise BuildError("E305", "edge selector %s matched %d edges%s"
                         % (sel, len(edges), "" if expect is None else " (expected %s)" % expect))
    coll = adsk.core.ObjectCollection.create()
    for e in edges:
        coll.add(e)
    return coll


def build_fillet(ctx, f):
    edges = _edges(ctx, f)
    fillets = ctx.root.features.filletFeatures
    inp = fillets.createInput()
    radius = ctx.vi(f["radius"])
    try:
        inp.edgeSetInputs.addConstantRadiusEdgeSet(edges, radius, True)
    except AttributeError:
        inp.addConstantRadiusEdgeSet(edges, radius, True)
    fil = fillets.add(inp)
    fil.name = f["id"]
    return fil


def build_chamfer(ctx, f):
    edges = _edges(ctx, f)
    chamfers = ctx.root.features.chamferFeatures
    dist = ctx.vi(f["distance"])
    try:
        inp = chamfers.createInput2()
        inp.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edges, dist, True)
    except AttributeError:
        inp = chamfers.createInput(edges, True)
        inp.setToEqualDistance(dist)
    ch = chamfers.add(inp)
    ch.name = f["id"]
    return ch


def _pattern_inputs(ctx, f):
    coll = adsk.core.ObjectCollection.create()
    for fid in f.get("features", []):
        obj = ctx.objects.get(fid)
        if obj is None:
            raise BuildError("E206", "pattern '%s' refers to unbuilt feature '%s'" % (f["id"], fid))
        coll.add(obj)
        # holes are built as several features (counterbore / countersink): pattern them all
        for extra in (fid + "_cb", fid + "_cs"):
            feat = ctx.root.features.itemByName(extra)
            if feat is not None:
                coll.add(feat)
    return coll


def _axis(ctx, name):
    root = ctx.root
    ax = {"X": root.xConstructionAxis, "Y": root.yConstructionAxis,
          "Z": root.zConstructionAxis}.get(str(name).upper())
    if ax is None:
        raise BuildError("E201", "axis must be X, Y or Z (got %s)" % name)
    return ax


def build_pattern_rect(ctx, f):
    """{features: [ids], x: {count, pitch, axis="X"}, y: {count, pitch, axis="Y"}}"""
    pats = ctx.root.features.rectangularPatternFeatures
    d1 = f.get("x") or {"count": 1, "pitch": 0}
    inp = pats.createInput(_pattern_inputs(ctx, f), _axis(ctx, d1.get("axis", "X")),
                           ctx.vi(d1.get("count", 1), ""), ctx.vi(d1.get("pitch", 0)),
                           adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
    d2 = f.get("y")
    if d2 and int(ctx.num(d2.get("count", 1))) > 1:
        inp.setDirectionTwo(_axis(ctx, d2.get("axis", "Y")), ctx.vi(d2["count"], ""), ctx.vi(d2["pitch"]))
    pat = pats.add(inp)
    pat.name = f["id"]
    return pat


def build_pattern_circ(ctx, f):
    """{features: [ids], axis: "Z", count, angle: 360} - axis through the origin."""
    pats = ctx.root.features.circularPatternFeatures
    inp = pats.createInput(_pattern_inputs(ctx, f), _axis(ctx, f.get("axis", "Z")))
    inp.quantity = ctx.vi(f["count"], "")
    inp.totalAngle = ctx.vi(f.get("angle", 360), "deg")
    inp.isSymmetric = False
    pat = pats.add(inp)
    pat.name = f["id"]
    return pat


BUILDERS = {
    "sketch": build_sketch,
    "extrude": build_extrude,
    "revolve": build_revolve,
    "hole": build_hole,
    "fillet": build_fillet,
    "chamfer": build_chamfer,
    "pattern_rect": build_pattern_rect,
    "pattern_circ": build_pattern_circ,
}


# --------------------------------------------------------------------------- main
def _health(obj):
    try:
        hs = obj.healthState
    except Exception:
        return "ok", ""
    H = adsk.fusion.FeatureHealthStates
    msg = ""
    try:
        msg = obj.errorOrWarningMessage or ""
    except Exception:
        pass
    if hs == H.ErrorFeatureHealthState:
        return "error", msg
    if hs == H.WarningFeatureHealthState:
        return "warning", msg
    return "ok", msg


def _live_pause(ctx):
    delay_ms = int(ctx.options.get("live_delay_ms", 0) or 0)
    if delay_ms <= 0:
        return
    vp = ctx.app.activeViewport
    if ctx.options.get("live_fit", True):
        try:
            vp.fit()
        except Exception:
            pass
    vp.refresh()
    end = time.time() + delay_ms / 1000.0
    while time.time() < end:
        adsk.doEvents()
        time.sleep(0.02)


def build(app, spec, options=None):
    options = options or {}
    started = time.time()
    design = prepare_design(app, options.get("new_document", True))
    ctx = Context(app, design, spec, options)
    stop_on_error = options.get("stop_on_error", True)
    entries = ctx.log

    try:
        register_parameters(ctx)
    except BuildError as e:
        entries.append({"id": "(parameters)", "status": "failed", "code": e.code, "message": str(e)})
        return _result(ctx, started, ok=False)

    for f in spec.get("features", []):
        fid, ftype = f.get("id"), f.get("type")
        entry = {"id": fid, "type": ftype}
        builder = BUILDERS.get(ftype)
        try:
            if builder is None:
                raise BuildError("E302", "feature type '%s' not supported yet" % ftype)
            obj = builder(ctx, f)
            ctx.objects[fid] = obj
            status, msg = _health(obj)
            entry["status"] = status
            if msg:
                entry["message"] = msg
            if fid in ctx.fallbacks:
                entry["note"] = ctx.fallbacks[fid]
            if status == "error":
                entry["code"] = "E302"
            elif status == "warning":
                entry["code"] = "E303"
            if ftype == "sketch":
                fully = bool(obj.isFullyConstrained)
                entry["fully_constrained"] = fully
                if not fully and status == "ok":
                    entry["status"], entry["code"] = "warning", "E304"
        except BuildError as e:
            entry.update(status="failed", code=e.code, message=str(e))
        except Exception as e:
            entry.update(status="failed", code="E301" if ftype == "sketch" else "E302",
                         message="%s: %s" % (type(e).__name__, e))
        entries.append(entry)
        log("build %s %s -> %s %s" % (ftype, fid, entry["status"], entry.get("message", "")))
        if entry["status"] in ("failed", "error") and stop_on_error:
            break
        _live_pause(ctx)

    bodies = ctx.root.bRepBodies
    if bodies.count > 1 and not spec.get("part", {}).get("multi_body", False):
        entries.append({"id": "(bodies)", "status": "error", "code": "E306",
                        "message": "%d bodies after build (expected 1)" % bodies.count})
    try:
        ctx.app.activeViewport.fit()
    except Exception:
        pass
    ok = all(e["status"] in ("ok", "warning", "info") for e in entries)
    return _result(ctx, started, ok=ok)


def _result(ctx, started, ok):
    return {"ok": True, "build_ok": ok, "elapsed_s": round(time.time() - started, 3),
            "parameters": ctx.params, "features": ctx.log,
            "document": ctx.app.activeDocument.name if ctx.app.activeDocument else None}
