"""Semantic edge selectors.

Fusion entity tokens change between rebuilds, so the spec never stores edge
IDs. Instead it says e.g. {"select": "vertical_outer", "of": "ex_base"} and
this module finds the edges by geometry.

Supported selectors (bbox = bounding box of the target body):
  vertical_all      straight edges parallel to Z
  vertical_outer    vertical edges lying on a bbox corner (x and y both on bbox)
  top_all           edges lying entirely at z = zmax
  top_outer         top edges lying on the bbox outline
  bottom_all        edges lying entirely at z = zmin
  bottom_outer      bottom edges lying on the bbox outline
  hole_edges:<id>   circular edges of the faces created by feature <id>
  vertical_edges:<id>  straight edges parallel to Z on the faces created by feature <id>
                    (e.g. the inner corners of a pocket / cavity cut)
"""

import adsk.core
import adsk.fusion

TOL = 1e-4  # cm (= 0.001 mm)


def _target_body(ctx, of):
    if of and of in ctx.objects:
        feat = ctx.objects[of]
        try:
            if feat.bodies.count > 0:
                return feat.bodies.item(0)
        except Exception:
            pass
    bodies = ctx.root.bRepBodies
    if bodies.count == 0:
        raise ValueError("no bodies to select edges from")
    return bodies.item(0)


def _is_line(edge):
    return edge.geometry.objectType == adsk.core.Line3D.classType()


def _is_circular(edge):
    t = edge.geometry.objectType
    return t in (adsk.core.Circle3D.classType(), adsk.core.Arc3D.classType())


def _near(a, b):
    return abs(a - b) <= TOL


def _edge_points(edge):
    pts = []
    for v in (edge.startVertex, edge.endVertex):
        if v is not None:
            pts.append(v.geometry)
    bb = edge.boundingBox
    pts.extend([bb.minPoint, bb.maxPoint])
    return pts


def _on_outline(points, bb):
    """Every point lies on the bbox outline in XY."""
    for p in points:
        on_x = _near(p.x, bb.minPoint.x) or _near(p.x, bb.maxPoint.x)
        on_y = _near(p.y, bb.minPoint.y) or _near(p.y, bb.maxPoint.y)
        if not (on_x or on_y):
            return False
    return True


def select_edges(ctx, sel):
    name = sel.get("select", "")
    if name.startswith("hole_edges:"):
        return _hole_edges(ctx, name.split(":", 1)[1])
    if name.startswith("vertical_edges:"):
        return _vertical_feature_edges(ctx, name.split(":", 1)[1])
    body = _target_body(ctx, sel.get("of"))
    bb = body.boundingBox
    out = []
    for i in range(body.edges.count):
        e = body.edges.item(i)
        pts = _edge_points(e)
        ebb = e.boundingBox
        if name in ("vertical_all", "vertical_outer"):
            if not _is_line(e):
                continue
            vertical = _near(ebb.minPoint.x, ebb.maxPoint.x) and _near(ebb.minPoint.y, ebb.maxPoint.y) \
                and not _near(ebb.minPoint.z, ebb.maxPoint.z)
            if not vertical:
                continue
            if name == "vertical_outer":
                x, y = ebb.minPoint.x, ebb.minPoint.y
                corner = (_near(x, bb.minPoint.x) or _near(x, bb.maxPoint.x)) and \
                         (_near(y, bb.minPoint.y) or _near(y, bb.maxPoint.y))
                if not corner:
                    continue
            out.append(e)
        elif name in ("top_all", "top_outer", "bottom_all", "bottom_outer"):
            z = bb.maxPoint.z if name.startswith("top") else bb.minPoint.z
            if not (_near(ebb.minPoint.z, z) and _near(ebb.maxPoint.z, z)):
                continue
            if name.endswith("_outer") and not _on_outline(pts, bb):
                continue
            out.append(e)
        else:
            raise ValueError("unknown edge selector '%s'" % name)
    return out


def _hole_edges(ctx, feature_id):
    feat = ctx.objects.get(feature_id)
    if feat is None:
        raise ValueError("feature '%s' not built" % feature_id)
    seen, out = set(), []
    for i in range(feat.faces.count):
        face = feat.faces.item(i)
        for j in range(face.edges.count):
            e = face.edges.item(j)
            if _is_circular(e) and e.tempId not in seen:
                seen.add(e.tempId)
                out.append(e)
    return out


def _vertical_feature_edges(ctx, feature_id):
    feat = ctx.objects.get(feature_id)
    if feat is None:
        raise ValueError("feature '%s' not built" % feature_id)
    seen, out = set(), []
    for i in range(feat.faces.count):
        face = feat.faces.item(i)
        for j in range(face.edges.count):
            e = face.edges.item(j)
            if e.tempId in seen or not _is_line(e):
                continue
            bb = e.boundingBox
            if _near(bb.minPoint.x, bb.maxPoint.x) and _near(bb.minPoint.y, bb.maxPoint.y) \
                    and not _near(bb.minPoint.z, bb.maxPoint.z):
                seen.add(e.tempId)
                out.append(e)
    return out


# --------------------------------------------------------------------------- faces
FACE_SELECTORS = ("top", "bottom", "front", "back", "left", "right", "vertical_outer", "side_all")
_SIDE = {   # name -> (axis index, at max?)
    "top": (2, True), "bottom": (2, False), "front": (1, False), "back": (1, True),
    "left": (0, False), "right": (0, True),
}


def _xyz(p):
    return (p.x, p.y, p.z)


def select_faces(ctx, sel):
    """Faces of the target body by geometry.

    top / bottom / front / back / left / right   planar faces lying on that side of the bbox
                                                 (front = -Y side, the side the front view looks at)
    vertical_outer   planar faces parallel to Z that lie on the bbox outline
    side_all         every face parallel to Z (planar walls and Z-axis cylinders / cones excluded)
    faces_of:<id>    faces created by feature <id>
    """
    name = sel.get("select", "")
    if name.startswith("faces_of:"):
        feat = ctx.objects.get(name.split(":", 1)[1])
        if feat is None:
            raise ValueError("feature '%s' not built" % name.split(":", 1)[1])
        return [feat.faces.item(i) for i in range(feat.faces.count)]
    body = _target_body(ctx, sel.get("of"))
    bb = body.boundingBox
    lo, hi = _xyz(bb.minPoint), _xyz(bb.maxPoint)
    out = []
    for i in range(body.faces.count):
        f = body.faces.item(i)
        g = f.geometry
        planar = g.objectType == adsk.core.Plane.classType()
        fb = f.boundingBox
        flo, fhi = _xyz(fb.minPoint), _xyz(fb.maxPoint)
        if name in _SIDE:
            if not planar:
                continue
            k, at_max = _SIDE[name]
            level = hi[k] if at_max else lo[k]
            if _near(flo[k], level) and _near(fhi[k], level):
                out.append(f)
        elif name in ("vertical_outer", "side_all"):
            if planar:
                n = g.normal
                if abs(n.z) > 1e-6:
                    continue
                if name == "vertical_outer":
                    on = any((_near(flo[k], lo[k]) and _near(fhi[k], lo[k])) or (_near(flo[k], hi[k]) and _near(fhi[k], hi[k]))
                             for k in (0, 1))
                    if not on:
                        continue
                out.append(f)
            elif name == "side_all" and g.objectType == adsk.core.Cylinder.classType() and abs(g.axis.z) > 1 - 1e-6:
                out.append(f)
        else:
            raise ValueError("unknown face selector '%s'" % name)
    return out
