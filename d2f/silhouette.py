"""Model vs drawing silhouette check (E406) — independent of the spec.

The drawing reader stores, for each view it used, the outline of that view taken straight
from the drawn lines (`extract-report.json` -> "silhouettes"). After the build the add-in
returns a triangle mesh of the body; here the mesh is projected onto the same plane,
rasterised, and compared with the drawn outline (intersection over union).

Because the outline comes from the drawing lines and not from part-spec.json, this catches
mistakes anywhere between drawing and model: a misread view, an approximation the reader
made (curved outline built as a straight wall), a feature built on the wrong side.

Pure Python (no numpy / Pillow) so the CLI keeps running on a bare Python.
"""

AXES = {"XZ": (0, 2), "XY": (0, 1), "YZ": (1, 2)}
VIEW_LABEL = {"front": "정면", "top": "평면", "side": "측면"}
PASS_IOU = 0.98     # correct models measure 0.99+ (pixel edge error is kept under ~1 %)
ERROR_IOU = 0.95


class Grid:
    """Pixel grid: 320 px on the long side, at least 100 px on the short side (thin plates),
    so the half-pixel edge error stays under ~1 % of the area; capped at ~1.5 M pixels."""

    def __init__(self, x0, y0, x1, y1, n=320, n_min=100, cap=1500000):
        self.x0, self.y0 = x0, y0
        span = max(x1 - x0, y1 - y0, 1e-9)
        short = max(min(x1 - x0, y1 - y0), span / 1000)
        self.px = min(span / n, short / n_min)
        if (span / self.px) * (short / self.px) > cap:
            self.px = (span * short / cap) ** 0.5
        self.w = max(1, int((x1 - x0) / self.px + 0.5))
        self.h = max(1, int((y1 - y0) / self.px + 0.5))

    def center(self, i, j):
        return self.x0 + (i + 0.5) * self.px, self.y0 + (j + 0.5) * self.px


def _empty(g):
    return [bytearray(g.w) for _ in range(g.h)]


def _fill_row(row, g, a, b):
    i0 = max(0, int((a - g.x0) / g.px + 0.5))
    i1 = min(g.w - 1, int((b - g.x0) / g.px - 0.5))
    if i1 >= i0:
        row[i0:i1 + 1] = b"\x01" * (i1 - i0 + 1)


def raster_polygon(poly, g):
    """Even-odd scanline fill of a polygon -> mask (list of bytearray rows)."""
    mask = _empty(g)
    n = len(poly)
    for j in range(g.h):
        y = g.y0 + (j + 0.5) * g.px
        xs = []
        for k in range(n):
            (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % n]
            if (y1 > y) != (y2 > y):
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for a, b in zip(xs[0::2], xs[1::2]):
            _fill_row(mask[j], g, a, b)
    return mask


def raster_segments(segs, g):
    """Drawn lines -> mask of the pixels they pass through (8-connected, so a 4-connected
    flood fill cannot leak through a diagonal line)."""
    mask = _empty(g)
    for x1, y1, x2, y2 in segs:
        n = max(1, int(3 * max(abs(x2 - x1), abs(y2 - y1)) / g.px))
        for k in range(n + 1):
            x = x1 + (x2 - x1) * k / n
            y = y1 + (y2 - y1) * k / n
            i = min(g.w - 1, max(0, int((x - g.x0) / g.px)))
            j = min(g.h - 1, max(0, int((y - g.y0) / g.px)))
            mask[j][i] = 1
    return mask


def raster_triangles(tris, g):
    """Projected triangles -> mask, filled row by row (long thin mesh strips stay cheap)."""
    mask = _empty(g)
    for tri in tris:
        ys = [p[1] for p in tri]
        j0 = max(0, int((min(ys) - g.y0) / g.px - 0.5))
        j1 = min(g.h - 1, int((max(ys) - g.y0) / g.px + 0.5))
        for j in range(j0, j1 + 1):
            y = g.y0 + (j + 0.5) * g.px
            xs = []
            for k in range(3):
                (x1, y1), (x2, y2) = tri[k], tri[(k + 1) % 3]
                if (y1 > y) != (y2 > y):
                    xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
            if len(xs) == 2:
                _fill_row(mask[j], g, min(xs), max(xs))
    return mask


def fill_holes(mask, g):
    """Silhouette = everything not reachable from the border through empty pixels."""
    outside = _empty(g)
    stack = [(i, j) for i in range(g.w) for j in (0, g.h - 1)] + [(i, j) for j in range(g.h) for i in (0, g.w - 1)]
    while stack:
        i, j = stack.pop()
        if mask[j][i] or outside[j][i]:
            continue
        outside[j][i] = 1
        if i + 1 < g.w:
            stack.append((i + 1, j))
        if i > 0:
            stack.append((i - 1, j))
        if j + 1 < g.h:
            stack.append((i, j + 1))
        if j > 0:
            stack.append((i, j - 1))
    return [bytearray(0 if o else 1 for o in row) for row in outside]


def _counts(a, b):
    inter = uni = only_a = only_b = 0
    for ra, rb in zip(a, b):
        for x, y in zip(ra, rb):
            if x and y:
                inter += 1
            if x or y:
                uni += 1
            if x and not y:
                only_a += 1
            elif y and not x:
                only_b += 1
    return inter, uni, only_a, only_b


def mesh_triangles(mesh, axes):
    a, b = AXES[axes]
    nodes = mesh["nodes"]
    idx = mesh["indices"]
    pts = [(nodes[k][a], nodes[k][b]) for k in range(len(nodes))]
    return [(pts[idx[t]], pts[idx[t + 1]], pts[idx[t + 2]]) for t in range(0, len(idx) - 2, 3)]


def _bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def compare_view(name, sil, meshes):
    """One view: drawn outline vs projected model. Returns a check row (compare.py format)."""
    segs = sil.get("segments")
    poly = [tuple(p) for p in sil.get("polygon") or []]
    shape_pts = [(s[0], s[1]) for s in segs] + [(s[2], s[3]) for s in segs] if segs else poly
    tris = [t for m in meshes for t in mesh_triangles(m, sil["axes"])]
    label = "실루엣 %s" % VIEW_LABEL.get(name, name)
    row = {"check": "c_sil_%s" % name, "kind": "silhouette", "label": label, "view": name}
    if not tris or len(shape_pts) < 3:
        row.update({"pass": False, "code": "E406", "severity": "WARN", "title": "실루엣 비교 불가",
                    "message": "%s: 모델 메시나 도면 외곽이 없습니다" % label})
        return row
    # align both shapes at their bounding-box minimum (the spec origin may differ from the drawing's)
    dx0, dy0, dx1, dy1 = _bbox(shape_pts)
    mx0, my0, mx1, my1 = _bbox([p for t in tris for p in t])
    poly = [(x - dx0, y - dy0) for x, y in poly]
    segs = [(a - dx0, b - dy0, c - dx0, d - dy0) for a, b, c, d in segs or []]
    tris = [tuple((x - mx0, y - my0) for x, y in t) for t in tris]
    size_d, size_m = (dx1 - dx0, dy1 - dy0), (mx1 - mx0, my1 - my0)
    g = Grid(0, 0, max(size_d[0], size_m[0]), max(size_d[1], size_m[1]))
    a = fill_holes(raster_segments(segs, g), g) if segs else raster_polygon(poly, g)
    b = fill_holes(raster_triangles(tris, g), g)
    inter, uni, only_a, only_b = _counts(a, b)
    iou = inter / uni if uni else 0.0
    row.update({"expected": 1.0, "actual": round(iou, 4), "tol": round(1 - PASS_IOU, 4),
                "drawing_size": [round(v, 3) for v in size_d], "model_size": [round(v, 3) for v in size_m],
                "pixel_mm": round(g.px, 4)})
    if iou >= PASS_IOU:
        row.update({"pass": True, "code": None, "severity": None, "title": "실루엣 일치",
                    "message": "%s IoU %.3f OK" % (label, iou)})
    else:
        sev = "ERROR" if iou < ERROR_IOU else "WARN"
        miss = only_a * g.px * g.px
        extra = only_b * g.px * g.px
        row.update({"pass": False, "code": "E406", "severity": sev, "title": "실루엣 불일치",
                    "message": "%s IoU %.3f (기준 %.2f) — 도면에만 있는 면적 %.1f mm², 모델에만 있는 면적 %.1f mm²"
                               % (label, iou, PASS_IOU, miss, extra)})
    return row


def compare_all(silhouettes, measure):
    meshes = [b["mesh"] for b in measure.get("bodies", []) if b.get("mesh")]
    return [compare_view(name, sil, meshes) for name, sil in sorted(silhouettes.items())]


def compare_section(region, measure):
    """Model cut by the section plane vs the drawing's material regions (turned parts, E410)."""
    polys = [[tuple(p) for p in poly] for poly in region["polygons"]]
    lines = []
    for b in measure.get("bodies", []):
        for sec in b.get("sections") or []:
            if sec.get("plane") == region["plane"]:
                lines += sec.get("polylines") or []
    row = {"check": "c_section", "kind": "section", "label": "단면 (%s)" % region["plane"], "view": "section"}
    pts_d = [p for poly in polys for p in poly]
    pts_m = [tuple(p) for ln in lines for p in ln]
    if not pts_m or not pts_d:
        row.update({"pass": False, "code": "E410", "severity": "WARN", "title": "단면 비교 불가",
                    "message": "모델 단면이나 도면 단면 영역이 없습니다"})
        return row
    x0 = min(p[0] for p in pts_d + pts_m)
    y0 = min(p[1] for p in pts_d + pts_m)
    x1 = max(p[0] for p in pts_d + pts_m)
    y1 = max(p[1] for p in pts_d + pts_m)
    g = Grid(x0, y0, x1, y1, n=400, n_min=120)
    a = _empty(g)
    # both masks include their boundary pixels (the model one is drawn from its edges), so the
    # drawing regions get their outline rasterised too - otherwise thin walls lose ~1 pixel per side
    rim = [(p[0], p[1], q[0], q[1]) for poly in polys for p, q in zip(poly, poly[1:] + poly[:1])]
    edge = raster_segments(rim, g)
    for poly in polys:
        m = raster_polygon(poly, g)
        for ra, rm, re_ in zip(a, m, edge):
            for i in range(g.w):
                if rm[i] or re_[i]:
                    ra[i] = 1
    segs = [(p[0], p[1], q[0], q[1]) for ln in lines for p, q in zip(ln, ln[1:])]
    b = fill_holes(raster_segments(segs, g), g)
    inter, uni, only_a, only_b = _counts(a, b)
    iou = inter / uni if uni else 0.0
    row.update({"expected": 1.0, "actual": round(iou, 4), "tol": round(1 - PASS_IOU, 4), "pixel_mm": round(g.px, 4)})
    if iou >= PASS_IOU - 0.01:
        row.update({"pass": True, "code": None, "severity": None, "title": "단면 일치",
                    "message": "단면 IoU %.3f OK" % iou})
    else:
        row.update({"pass": False, "code": "E410", "severity": "ERROR" if iou < ERROR_IOU else "WARN",
                    "title": "단면 불일치",
                    "message": "모델 단면 vs 도면 해칭 영역 IoU %.3f — 도면에만 있는 면적 %.1f mm², 모델에만 있는 면적 %.1f mm²"
                               % (iou, only_a * g.px * g.px, only_b * g.px * g.px)})
    return row
