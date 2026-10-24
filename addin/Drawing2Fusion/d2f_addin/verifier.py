"""Measure the built model, capture snapshots, export files.

All numbers returned to the outside are mm / mm^3 / deg. Comparison against
the spec happens outside Fusion (d2f/compare.py) so it can be unit-tested.
"""

import math
import os

import adsk.core
import adsk.fusion

from . import expr

_KEY_ROUND = 4  # mm decimals used to group cylinder faces into holes


def _design(app):
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise RuntimeError("no active Fusion design")
    return design


def _pt_mm(p):
    return [round(expr.cm_to_mm(p.x), 6), round(expr.cm_to_mm(p.y), 6), round(expr.cm_to_mm(p.z), 6)]


def _bbox_mm(bb):
    mn, mx = _pt_mm(bb.minPoint), _pt_mm(bb.maxPoint)
    return {"min": mn, "max": mx, "size": [round(mx[i] - mn[i], 6) for i in range(3)]}


def _cylinder_info(face):
    """Return hole/boss info for a cylindrical face, or None."""
    geom = face.geometry
    if geom.objectType != adsk.core.Cylinder.classType():
        return None
    o, a, r = geom.origin, geom.axis.copy(), geom.radius
    a.normalize()
    # canonical axis direction (first non-zero component positive)
    comps = [a.x, a.y, a.z]
    for c in comps:
        if abs(c) > 1e-9:
            if c < 0:
                a.scaleBy(-1)
            break
    # concave (hole) vs convex (boss): compare face normal with radial direction
    p = face.pointOnFace
    v = o.vectorTo(p)
    along = v.dotProduct(a)
    radial = adsk.core.Vector3D.create(v.x - a.x * along, v.y - a.y * along, v.z - a.z * along)
    ok, normal = face.evaluator.getNormalAtPoint(p)
    is_hole = ok and normal.dotProduct(radial) < 0
    # axis point closest to world origin
    od = o.x * a.x + o.y * a.y + o.z * a.z
    base = adsk.core.Point3D.create(o.x - a.x * od, o.y - a.y * od, o.z - a.z * od)
    # extent along the axis from the face bounding box (exact for axis-aligned holes)
    bb = face.boundingBox
    corners = [(x, y, z) for x in (bb.minPoint.x, bb.maxPoint.x)
               for y in (bb.minPoint.y, bb.maxPoint.y) for z in (bb.minPoint.z, bb.maxPoint.z)]
    proj = [c[0] * a.x + c[1] * a.y + c[2] * a.z for c in corners]
    return {
        "kind": "hole" if is_hole else "boss",
        "diameter": round(expr.cm_to_mm(2 * r), 6),
        "axis_dir": [round(a.x, 6), round(a.y, 6), round(a.z, 6)],
        "axis_point": _pt_mm(base),
        "span": [round(expr.cm_to_mm(min(proj)), 6), round(expr.cm_to_mm(max(proj)), 6)],
        "area": face.area * 100.0,   # mm^2
    }


def _group_cylinders(infos):
    """Merge split cylinder faces (same axis + radius) into one entry."""
    groups = {}
    for c in infos:
        key = (c["kind"], round(c["diameter"], _KEY_ROUND),
               tuple(round(v, _KEY_ROUND) for v in c["axis_point"]),
               tuple(round(v, 3) for v in c["axis_dir"]))
        g = groups.get(key)
        if g is None:
            groups[key] = dict(c, faces=1)
        else:
            g["faces"] += 1
            g["area"] += c["area"]
            g["span"] = [min(g["span"][0], c["span"][0]), max(g["span"][1], c["span"][1])]
    out = []
    for g in groups.values():
        g["depth"] = round(g["span"][1] - g["span"][0], 6)
        # angular coverage: a real hole/boss wraps ~360 deg; corner fillets (90 deg) and
        # half-round slot ends (180 deg) are "partial" and are not counted as holes
        full = math.pi * g["diameter"] * g["depth"]
        g["coverage"] = round(g["area"] / full, 3) if full > 1e-9 else 0.0
        if g["coverage"] < 0.9:
            g["kind"] = "partial_" + g["kind"]
        g["area"] = round(g["area"], 3)
        out.append(g)
    out.sort(key=lambda g: (g["kind"], g["diameter"], g["axis_point"]))
    return out


def _cylinders_of_faces(faces):
    infos = []
    for i in range(faces.count):
        info = _cylinder_info(faces.item(i))
        if info:
            infos.append(info)
    return _group_cylinders(infos)


def _axis_planes(faces):
    """Axis-aligned planar faces grouped by (axis, position): [{axis, at (mm), area (mm2)}].
    Lets a check confirm *where* a flat face sits (e.g. which side a keyway was cut)."""
    groups = {}
    for i in range(faces.count):
        f = faces.item(i)
        g = f.geometry
        if g.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
            continue
        n = g.normal
        comps = (abs(n.x), abs(n.y), abs(n.z))
        k = max(range(3), key=lambda j: comps[j])
        if comps[k] < 1 - 1e-6:
            continue
        p = f.pointOnFace
        at = round(expr.cm_to_mm((p.x, p.y, p.z)[k]), 4)
        key = ("XYZ"[k], at)
        groups[key] = groups.get(key, 0.0) + f.area * 100.0
    return [{"axis": a, "at": at, "area": round(area, 3)} for (a, at), area in sorted(groups.items())]


def _mesh(body):
    """Triangle mesh of a body in mm: {"nodes": [[x, y, z], ...], "indices": [i0, i1, i2, ...]}."""
    calc = body.meshManager.createMeshCalculator()
    calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.NormalQualityTriangleMesh)
    mesh = calc.calculate()
    c = mesh.nodeCoordinatesAsDouble
    nodes = [[round(c[k] * 10.0, 4), round(c[k + 1] * 10.0, 4), round(c[k + 2] * 10.0, 4)] for k in range(0, len(c), 3)]
    return {"nodes": nodes, "indices": list(mesh.nodeIndices)}


def _section(body, plane, offset_mm):
    """Body cut by an axis plane -> list of polylines [[(a, b), ...]] in that plane's (mm) axes."""
    normal = {"XY": (0, 0, 1), "XZ": (0, 1, 0), "YZ": (1, 0, 0)}[plane]
    origin = adsk.core.Point3D.create(*[n * offset_mm / 10.0 for n in normal])
    pl = adsk.core.Plane.create(origin, adsk.core.Vector3D.create(*normal))
    wire = adsk.fusion.TemporaryBRepManager.get().planeIntersection(body, pl)
    out = []
    if wire is None:
        return out
    ia, ib = {"XY": (0, 1), "XZ": (0, 2), "YZ": (1, 2)}[plane]
    for i in range(wire.edges.count):
        e = wire.edges.item(i)
        ev = e.evaluator
        ok, p0, p1 = ev.getParameterExtents()
        ok, pts = ev.getStrokes(p0, p1, 0.001)
        if not ok:
            continue
        line = []
        for p in pts:
            c = (p.x * 10.0, p.y * 10.0, p.z * 10.0)
            line.append([round(c[ia], 4), round(c[ib], 4)])
        out.append(line)
    return out


def measure(app, args):
    design = _design(app)
    root = design.rootComponent
    bodies = []
    for i in range(root.bRepBodies.count):
        b = root.bRepBodies.item(i)
        if not b.isSolid:
            continue
        cyl = _cylinders_of_faces(b.faces)
        bodies.append({
            "name": b.name,
            "bbox": _bbox_mm(b.boundingBox),
            "volume": round(expr.cm3_to_mm3(b.volume), 4),
            "area": round(b.area * 100.0, 4),
            "faces": b.faces.count,
            "edges": b.edges.count,
            "holes": [c for c in cyl if c["kind"] == "hole"],
            "bosses": [c for c in cyl if c["kind"] == "boss"],
            "partial_cylinders": len([c for c in cyl if c["kind"].startswith("partial")]),
            "planes": _axis_planes(b.faces),
        })
        if args.get("mesh"):
            try:
                bodies[-1]["mesh"] = _mesh(b)
            except Exception as e:  # mesh is optional: the silhouette check reports it missing
                bodies[-1]["mesh_error"] = str(e)
        for sec in args.get("sections") or []:
            try:
                bodies[-1].setdefault("sections", []).append(
                    {"plane": sec["plane"], "offset": sec.get("offset", 0),
                     "polylines": _section(b, sec["plane"], float(sec.get("offset", 0)))})
            except Exception as e:
                bodies[-1].setdefault("sections", []).append({"plane": sec["plane"], "error": str(e)})

    features = {}
    for fid in args.get("features", []):
        feat = root.features.itemByName(fid)
        if feat is None:
            features[fid] = {"found": False}
            continue
        try:
            faces = feat.faces
            cyl = _cylinders_of_faces(faces)
        except Exception:
            cyl = []
        features[fid] = {"found": True, "holes": [c for c in cyl if c["kind"] == "hole"]}

    threads = []
    try:
        for i in range(root.features.threadFeatures.count):
            t = root.features.threadFeatures.item(i)
            ti = t.threadInfo
            threads.append({"name": t.name, "designation": ti.threadDesignation, "class": ti.threadClass,
                            "modeled": t.isModeled, "faces": t.faces.count if t.faces else 0})
    except Exception as e:
        threads.append({"error": str(e)})

    params = {}
    for i in range(design.userParameters.count):
        p = design.userParameters.item(i)
        params[p.name] = {"expression": p.expression, "unit": p.unit,
                          "value": round(expr.cm_to_mm(p.value), 6) if p.unit == "mm" else p.value}

    return {"ok": True, "bodies": bodies, "features": features, "user_parameters": params, "threads": threads}


_VIEWS = {
    "front": "FrontViewOrientation",
    "back": "BackViewOrientation",
    "top": "TopViewOrientation",
    "bottom": "BottomViewOrientation",
    "left": "LeftViewOrientation",
    "right": "RightViewOrientation",
    "iso": "IsoTopRightViewOrientation",
}


def snapshot(app, args):
    out_dir = args["dir"]
    os.makedirs(out_dir, exist_ok=True)
    w, h = int(args.get("width", 1200)), int(args.get("height", 900))
    vp = app.activeViewport
    files = {}
    for view in args.get("views", ["front", "top", "right", "iso"]):
        attr = _VIEWS.get(view)
        if attr is None:
            continue
        cam = vp.camera
        cam.viewOrientation = getattr(adsk.core.ViewOrientations, attr)
        cam.isFitView = True
        cam.isSmoothTransition = False
        vp.camera = cam
        vp.refresh()
        adsk.doEvents()
        path = os.path.join(out_dir, "%s.png" % view)
        if vp.saveAsImageFile(path, w, h):
            files[view] = path
    return {"ok": True, "files": files}


def export(app, args):
    design = _design(app)
    em = design.exportManager
    path = args["path"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = args.get("format") or os.path.splitext(path)[1].lstrip(".").lower()
    if fmt == "f3d":
        opts = em.createFusionArchiveExportOptions(path)
    elif fmt in ("step", "stp"):
        opts = em.createSTEPExportOptions(path)
    elif fmt == "stl":
        body = design.rootComponent.bRepBodies.item(0)
        opts = em.createSTLExportOptions(body, path)
    else:
        raise ValueError("unsupported export format '%s'" % fmt)
    ok = em.execute(opts)
    return {"ok": bool(ok), "path": path, "format": fmt}
