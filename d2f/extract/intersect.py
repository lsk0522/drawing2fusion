"""Parts whose outlines have arcs or slanted edges: body = intersection of the view outlines.

The band reader (dxf_reader.z_bands) models a part as stacked straight-walled layers. That is
exact for plates, blocks and steps, but not for a rounded end, a slanted face or a fillet seen in
a view. For those drawings each view's outer outline - lines and arcs exactly as drawn - is
extruded through the part along that view's direction, and the extrusions are intersected:

    body = extrude(front outline, along Y)  ∩  extrude(top outline, along Z)  ∩  extrude(side, along X)

This is the classic orthographic reconstruction of a 2.5D machined part. A view whose outline
is the full bounding rectangle adds nothing and is skipped. Holes are added afterwards by the
normal hole code. The volume expectation is integrated numerically from the three outlines.
"""

import math


def _arc_dir_ccw(view, seg, a):
    """True when walking the arc from point `a` runs counter-clockwise in view-local coordinates."""
    from_start = math.dist(a, seg.a) <= math.dist(a, seg.b)
    ccw = from_start                       # DXF arcs run CCW from start to end
    if bool(view.flip_x) != bool(view.flip_y):
        ccw = not ccw                      # a mirrored view reverses the turning direction
    return ccw


def loop_path(view, loop, scale, ndigits=4):
    """Outer loop [(a, b, seg)] -> sketch path in view-local mm + a flattened polygon."""
    def L(p):
        u, v = view.local(p)
        return [round(u * scale, ndigits), round(v * scale, ndigits)]
    segs, poly = [], []
    start = L(loop[0][0])
    for a, b, s in loop:
        pa, pb = L(a), L(b)
        if s.kind == "arc":
            c = L(s.c)
            ccw = _arc_dir_ccw(view, s, a)
            segs.append({"type": "arc", "to": pb, "center": c, "ccw": ccw})
            a0 = math.atan2(pa[1] - c[1], pa[0] - c[0])
            a1 = math.atan2(pb[1] - c[1], pb[0] - c[0])
            sweep = (a1 - a0) % (2 * math.pi) if ccw else -((a0 - a1) % (2 * math.pi))
            r = s.r * scale
            n = max(4, int(abs(sweep) / math.radians(6)))
            for k in range(n):
                t = a0 + sweep * k / n
                poly.append((c[0] + r * math.cos(t), c[1] + r * math.sin(t)))
        else:
            segs.append({"type": "line", "to": pb})
            poly.append(tuple(pa))
    return {"start": start, "segments": segs, "polygon": poly}


def path_area(path):
    """Exact enclosed area of a path (shoelace over the vertices + circular segments of the arcs)."""
    pts = [tuple(path["start"])]
    extra = 0.0
    for sg in path["segments"]:
        a, b = pts[-1], tuple(sg["to"])
        if sg["type"] == "arc":
            c = sg["center"]
            r = math.dist(a, c)
            a0 = math.atan2(a[1] - c[1], a[0] - c[0])
            a1 = math.atan2(b[1] - c[1], b[0] - c[0])
            sw = (a1 - a0) % (2 * math.pi) if sg["ccw"] else -((a0 - a1) % (2 * math.pi))
            extra += 0.5 * r * r * (sw - math.sin(sw))
        pts.append(b)
    sh = 0.5 * sum(pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1] for i in range(len(pts) - 1))
    return abs(sh + extra)


def is_full_rect(path, w, h, eps):
    """Only axis-aligned lines lying on the view box: adds nothing to the intersection."""
    pts = [tuple(path["start"])] + [tuple(s["to"]) for s in path["segments"]]
    if any(s["type"] != "line" for s in path["segments"]):
        return False

    def on_box(p):
        return (abs(p[0]) <= eps or abs(p[0] - w) <= eps or abs(p[1]) <= eps or abs(p[1] - h) <= eps) and \
            -eps <= p[0] <= w + eps and -eps <= p[1] <= h + eps
    for a, b in zip(pts, pts[1:]):
        if not (abs(a[0] - b[0]) <= eps or abs(a[1] - b[1]) <= eps):
            return False
        if not (on_box(a) and on_box(b) and on_box(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))):
            return False
    return True


def point_in(poly, p):
    x, y = p
    c = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def _intervals(poly, y):
    xs = []
    for k in range(len(poly)):
        (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % len(poly)]
        if (y1 > y) != (y2 > y):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    return list(zip(xs[0::2], xs[1::2]))


def _mask(poly, nx, ny, sx, sy):
    """Cell-centre scanline fill -> rows[j][i] (j along the 2nd axis) of 0/1."""
    rows = []
    for j in range(ny):
        row = [0] * nx
        if poly is None:
            row = [1] * nx
        else:
            for a, b in _intervals(poly, (j + 0.5) * sy):
                for i in range(max(0, int(math.ceil(a / sx - 0.5))), min(nx, int(math.floor(b / sx - 0.5)) + 1)):
                    row[i] = 1
        rows.append(row)
    return rows


def volume(front, top, side, L, W, H, n=200):
    """Volume of extrude(front) ∩ extrude(top) ∩ extrude(side) by cell-centre integration (mm³).
    front: polygon in (X, Z), top: (X, Y), side: (Y, Z); None = the full rectangle."""
    sx, sy, sz = L / n, W / n, H / n
    F = _mask(front, n, n, sx, sz)        # F[z][x]
    T = _mask(top, n, n, sx, sy)          # T[y][x]
    S = _mask(side, n, n, sy, sz)         # S[z][y]
    # column sums of the top mask over a set of y cells, cached per distinct side row
    cache, total = {}, 0
    for z in range(n):
        key = tuple(S[z])
        if not any(key):
            continue
        if key not in cache:
            ys = [y for y in range(n) if key[y]]
            cache[key] = [sum(T[y][x] for y in ys) for x in range(n)]
        col = cache[key]
        Fz = F[z]
        total += sum(col[x] for x in range(n) if Fz[x])
    return total * sx * sy * sz


def build(named, scale, tol, L, W, H, loops):
    """Spec features for the intersection model.

    loops: {"front": loop or None, "top": ..., "side": ...} from dxf_reader._graph_outer_loop.
    Returns (features, info) where info has polygons (model mm), used views, volume.
    """
    plane = {"front": "XZ", "top": "XY", "side": "YZ"}
    depth = {"front": "W", "top": "H", "side": "L"}
    size = {"front": (L, H), "top": (L, W), "side": (W, H)}
    feats, polys, used, areas, paths = [], {}, [], {}, {}
    for name in ("front", "top", "side"):
        view, loop = named.get(name), loops.get(name)
        if view is None or not loop:
            continue
        path = loop_path(view, loop, scale)
        w, h = size[name]
        if is_full_rect(path, w, h, max(0.01, tol * scale * 10)):
            continue
        polys[name] = path["polygon"]
        areas[name] = path_area(path)
        paths[name] = path
        sid = "sk_%s_outline" % name
        feats.append({"id": sid, "type": "sketch", "plane": plane[name],
                      "entities": [{"id": "outline", "kind": "path", "closed": True,
                                    "start": path["start"], "segments": path["segments"]}]})
        feats.append({"id": "ex_%s" % name, "type": "extrude", "profile": sid, "distance": depth[name],
                      "direction": "positive", "operation": "new_body" if not used else "intersect"})
        used.append(name)
    depth_mm = {"front": W, "top": H, "side": L}
    if len(used) == 1:
        vol, exact = areas[used[0]] * depth_mm[used[0]], True      # a single extrusion: exact
    elif used:
        vol, exact = volume(polys.get("front"), polys.get("top"), polys.get("side"), L, W, H), False
    else:
        vol, exact = None, False
    return feats, {"polygons": polys, "views": used, "volume": vol, "exact": exact, "paths": paths}


def inside_and_top(polys, L, W, H):
    """For the hole code: is a top-view point on the part, and how high is the part there."""
    front, top, side = polys.get("front"), polys.get("top"), polys.get("side")

    def height_at(poly, u):
        zs = []
        for k in range(len(poly)):
            (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % len(poly)]
            if (x1 - u) * (x2 - u) <= 0 and abs(x2 - x1) > 1e-12:
                zs.append(y1 + (u - x1) * (y2 - y1) / (x2 - x1))
        return max(zs) if zs else None

    def inside(p):
        return top is None or point_in(top, p)

    def ztop(p, r=0.0):
        """Highest surface over the disc of radius r at p (a hole starts from there)."""
        best = None
        for k in range(-4, 5) if r > 0 else (0,):
            q = (p[0] + r * k / 4, p[1] + r * k / 4)
            z = H
            if front:
                zf = height_at(front, q[0])
                z = min(z, zf) if zf is not None else z
            if side:
                zs = height_at(side, q[1])
                z = min(z, zs) if zs is not None else z
            best = z if best is None else max(best, z)
        return best
    return inside, ztop
