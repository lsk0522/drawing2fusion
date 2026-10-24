"""Turned parts drawn as a hatched (half-)section -> revolve spec.

A section view of a rotationally symmetric part shows the material cut by the axial plane:
hatched on both sides of the centre line. This module

  1. finds hatch boundaries that come in mirror pairs about a centre line (the axis),
  2. builds the material profile of one side: the hatch boundary plus closed *unhatched*
     regions that sit outside it (gear teeth, ribs and spokes are conventionally not hatched),
  3. picks the side with the full bore; a shallower inner edge on the other side is a keyway
     cut through the section (its width comes from an end view: a notch on the bore circle),
  4. ties each profile coordinate to a dimension (axial length from the datum face, diameter),
     or derives it (chamfer, 30/45/60 deg edge) or marks it inferred,
  5. writes a spec: XZ half profile revolved about X, keyway cut, bbox / bore / plane / volume checks.

Coordinates: section view axis -> world X (datum face = X 0), radius -> world Z.
"""

import math
import re

from . import tolerance

STD_ANGLES = (30.0, 45.0, 60.0)
DIA_TEXT = re.compile(r"(?:%%c|Ø|ø|⌀)\s*(\d+(?:\.\d+)?)", re.I)
CHAMFER_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×]\s*45")


class NotTurned(Exception):
    """The drawing is not a turned-part section (caller falls back to the projection reader)."""


# ----------------------------------------------------------------------------- hatch geometry
def hatch_polygons(doc, arc_steps=16):
    """HATCH boundary paths as closed point lists (drawing units)."""
    polys = []
    for h in doc.modelspace().query("HATCH"):
        for path in h.paths:
            pts = []
            if hasattr(path, "vertices"):            # polyline path
                pts = [(float(v[0]), float(v[1])) for v in path.vertices]
            else:                                    # edge path
                for e in path.edges:
                    t = type(e).__name__
                    if t == "LineEdge":
                        pts.append((float(e.start[0]), float(e.start[1])))
                    elif t == "ArcEdge":
                        a0, a1 = e.start_angle, e.end_angle
                        if not e.ccw:
                            a0, a1 = -a0, -a1
                        span = (a1 - a0) % 360 or 360.0
                        for k in range(arc_steps):
                            ang = math.radians(a0 + span * k / arc_steps)
                            pts.append((e.center[0] + e.radius * math.cos(ang), e.center[1] + e.radius * math.sin(ang)))
                    elif hasattr(e, "start"):
                        pts.append((float(e.start[0]), float(e.start[1])))
            if len(pts) >= 3:
                polys.append(pts)
    return polys


def _area(poly):
    return 0.5 * sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
                     for i in range(len(poly)))


def _ccw(poly):
    return poly if _area(poly) > 0 else poly[::-1]


def _bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(p, poly):
    x, y = p
    c = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def _centroid(poly):
    a = _area(poly)
    if abs(a) < 1e-12:
        return _bbox(poly)[0], _bbox(poly)[1]
    cx = cy = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        f = x0 * y1 - x1 * y0
        cx += (x0 + x1) * f
        cy += (y0 + y1) * f
    return cx / (6 * a), cy / (6 * a)


def _simplify(poly, tol):
    """Drop repeated and collinear vertices."""
    out = []
    for p in poly:
        if out and math.dist(out[-1], p) < tol:
            continue
        out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) < tol:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            a, b, c = out[i - 1], out[i], out[(i + 1) % len(out)]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cross) <= tol * max(math.dist(a, c), 1e-9):
                out.pop(i)
                changed = True
                break
    return out


# ----------------------------------------------------------------------------- frames
class Frame:
    """Drawing coordinates <-> (u along the axis, s signed distance from the axis)."""

    def __init__(self, horizontal, c):
        self.horizontal, self.c = horizontal, c

    def us(self, p):
        return (p[0], p[1] - self.c) if self.horizontal else (p[1], -(p[0] - self.c))

    def poly(self, pts):
        return [self.us(p) for p in pts]


def _axis_candidates(segs, tol):
    """Centre lines (horizontal or vertical) -> [(horizontal, coordinate, u0, u1)]."""
    out = []
    for s in segs:
        if s.kind != "line" or not s.center:
            continue
        if abs(s.a[1] - s.b[1]) < tol:
            out.append((True, (s.a[1] + s.b[1]) / 2, min(s.a[0], s.b[0]), max(s.a[0], s.b[0])))
        elif abs(s.a[0] - s.b[0]) < tol:
            out.append((False, (s.a[0] + s.b[0]) / 2, min(s.a[1], s.b[1]), max(s.a[1], s.b[1])))
    return out


def find_axis(polys, segs, tol):
    """The axis about which hatch polygons come in mirror pairs. Returns (Frame, polys_used)."""
    best = None
    for horizontal, c, _, _ in _axis_candidates(segs, tol):
        fr = Frame(horizontal, c)
        pos, neg = [], []
        for p in polys:
            q = fr.poly(p)
            ss = [s for _, s in q]
            if min(ss) >= -tol:
                pos.append(q)
            elif max(ss) <= tol:
                neg.append(q)
        if not pos or not neg:
            continue
        # outer extent (u range, max |s|) must agree on both sides
        def extent(group):
            us_ = [u for q in group for u, _ in q]
            return min(us_), max(us_), max(abs(s) for q in group for _, s in q)
        ep, en = extent(pos), extent(neg)
        mtol = max(tol * 50, 1e-3 * max(ep[2], 1))
        if all(abs(a - b) <= mtol for a, b in zip(ep, en)):
            score = len(pos) + len(neg)
            if best is None or score > best[0]:
                best = (score, fr, pos, neg)
    if best is None:
        raise NotTurned("no mirror-symmetric hatch about a centre line")
    return best[1], best[2], best[3]


# ----------------------------------------------------------------------------- unhatched faces
def _split_lines(lines, tol):
    """Split lines at endpoints of other lines lying on them (T-junctions)."""
    ends = [p for a, b in lines for p in (a, b)]
    out = []
    for a, b in lines:
        L = math.dist(a, b)
        if L < tol:
            continue
        ts = []
        for p in ends:
            t = ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L)
            if tol / L < t < 1 - tol / L:
                q = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                if math.dist(p, q) < tol:
                    ts.append(t)
        pts = [a] + [(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])) for t in sorted(set(ts))] + [b]
        out += list(zip(pts, pts[1:]))
    return out


def outer_faces(hatch_uv, line_uv, tol):
    """Closed regions bounded by drawn lines, sharing an edge with the hatch polygon and lying
    farther from the axis (unhatched teeth / ribs). Polygons in (u, |s|) coordinates."""
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))
    pos, adj = {}, {}
    for a, b in _split_lines(line_uv, tol):
        ka, kb = key(a), key(b)
        if ka == kb:
            continue
        pos.setdefault(ka, a)
        pos.setdefault(kb, b)
        adj.setdefault(ka, set()).add(kb)
        adj.setdefault(kb, set()).add(ka)

    def ang(k1, k2):
        return math.atan2(pos[k2][1] - pos[k1][1], pos[k2][0] - pos[k1][0])

    def left_face(k_from, k_to):
        cyc = [k_from, k_to]
        for _ in range(64):
            u, v = cyc[-2], cyc[-1]
            d_in = ang(u, v)
            cands = [w for w in adj.get(v, ()) if w != u] or [u]
            w = max(cands, key=lambda w: (ang(v, w) - d_in + math.pi) % (2 * math.pi) - math.pi)
            if v == cyc[0] and w == cyc[1]:
                return [pos[k] for k in cyc[:-1]]
            cyc.append(w)
        return None

    faces = []
    hp = _ccw(hatch_uv)
    for i in range(len(hp)):
        a, b = hp[i], hp[(i + 1) % len(hp)]
        ka, kb = key(a), key(b)
        if ka not in adj or kb not in adj.get(ka, ()):
            continue
        f = left_face(kb, ka)       # left of b->a = outside the CCW hatch polygon
        if not f or len(f) < 3 or _area(f) <= tol * tol:
            continue
        c = _centroid(f)
        mid_s = (a[1] + b[1]) / 2
        if _inside(c, hp) or c[1] <= mid_s or min(s for _, s in f) < tol:
            continue                # inside the hatch, toward the axis, or touching the axis
        if any(_same_poly(f, g, tol) for g in faces):
            continue
        faces.append(_ccw(f))
    return faces


def _same_poly(f, g, tol):
    return len(f) == len(g) and all(any(math.dist(p, q) < tol for q in g) for p in f)


def bounded_faces(lines, tol, max_edges=400):
    """All bounded faces of the planar graph of line segments, as CCW polygons."""
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))
    pos, adj = {}, {}
    for a, b in _split_lines(lines, tol):
        ka, kb = key(a), key(b)
        if ka == kb:
            continue
        pos.setdefault(ka, a)
        pos.setdefault(kb, b)
        adj.setdefault(ka, set()).add(kb)
        adj.setdefault(kb, set()).add(ka)

    def ang(k1, k2):
        return math.atan2(pos[k2][1] - pos[k1][1], pos[k2][0] - pos[k1][0])
    seen, faces = set(), []
    for u in adj:
        for v in adj[u]:
            if (u, v) in seen:
                continue
            cyc = [u, v]
            ok = False
            for _ in range(max_edges):
                a, b = cyc[-2], cyc[-1]
                d_in = ang(a, b)
                cands = [w for w in adj[b] if w != a] or [a]
                w = max(cands, key=lambda w: (ang(b, w) - d_in + math.pi) % (2 * math.pi) - math.pi)
                if b == cyc[0] and w == cyc[1]:
                    ok = True
                    break
                cyc.append(w)
            for x, y in zip(cyc, cyc[1:]):
                seen.add((x, y))
            if not ok:
                continue
            poly = [pos[k] for k in cyc[:-1]]
            if len(poly) >= 3 and _area(poly) > tol * tol:
                faces.append(poly)
    return faces


def union(polys, tol):
    """Union of edge-adjacent CCW polygons by cancelling shared edges. None if not one loop."""
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))
    pts = [p for poly in polys for p in poly]
    edges = []
    for poly in polys:
        poly = _ccw(poly)
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            # split at other polygons' vertices lying on this edge
            L = math.dist(a, b)
            if L < tol:
                continue
            ts = sorted({((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L) for p in pts
                         if math.dist(p, (a[0] + ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L) * (b[0] - a[0]),
                                          a[1] + ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L) * (b[1] - a[1]))) < tol})
            ts = [t for t in ts if tol / L < t < 1 - tol / L]
            chain = [a] + [(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])) for t in ts] + [b]
            edges += list(zip(chain, chain[1:]))
    count = {}
    for a, b in edges:
        count[(key(a), key(b))] = count.get((key(a), key(b)), 0) + 1
    pos = {}
    nxt = {}
    for a, b in edges:
        ka, kb = key(a), key(b)
        if count.get((kb, ka)):
            continue
        pos[ka], pos[kb] = a, b
        if ka in nxt:
            return None             # branching boundary
        nxt[ka] = kb
    if not nxt:
        return None
    start = next(iter(nxt))
    loop, k = [], start
    for _ in range(len(nxt) + 1):
        loop.append(pos[k])
        k = nxt.get(k)
        if k is None:
            return None
        if k == start:
            break
    return loop if len(loop) == len(nxt) else None


# ----------------------------------------------------------------------------- keyway
def keyway_width(views, bore_r, key_top, tol):
    """Width of a keyway notch on a bore circle in an end view (drawing units) or None."""
    for v in views:
        circ = [s for s in v.segs if s.kind in ("circle", "arc") and not s.center and abs(s.r - bore_r) <= tol * 20]
        for c in circ:
            for s in v.segs:
                if s.kind != "line" or s.center or s.hidden:
                    continue
                # notch roof: a line whose distance from the centre is key_top, centred on the centre
                dx, dy = s.b[0] - s.a[0], s.b[1] - s.a[1]
                L = math.hypot(dx, dy)
                if L < tol:
                    continue
                dist = abs(dx * (s.a[1] - c.c[1]) - dy * (s.a[0] - c.c[0])) / L
                mid = ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2)
                if abs(dist - key_top) <= tol * 20 and abs(math.dist(mid, c.c) - key_top) <= tol * 20 and L < 2 * bore_r:
                    return L
    return None


class _Merged:
    def __init__(self, segs, bbox):
        self.segs, self.bbox = segs, bbox

    @property
    def w(self):
        return self.bbox[2] - self.bbox[0]

    @property
    def h(self):
        return self.bbox[3] - self.bbox[1]


def _merge_views(parts):
    if len(parts) == 1:
        return parts[0]
    segs = [s for v in parts for s in v.segs]
    bb = (min(v.bbox[0] for v in parts), min(v.bbox[1] for v in parts),
          max(v.bbox[2] for v in parts), max(v.bbox[3] for v in parts))
    return _Merged(segs, bb)


# ----------------------------------------------------------------------------- main entry
def read(views, segs, polys, dims, texts, scale, tol, issue):
    """Try to read a turned part from a hatched section. Returns spec dict or raises NotTurned.
    `polys`: hatch boundary polygons in drawing units (DXF HATCH entities or PDF hatch regions)."""
    if not polys:
        raise NotTurned("no hatch")
    fr, pos, neg = find_axis(polys, segs, tol)
    snap = max(tol * 20, 1e-3)

    # the section view = the view holding the hatch
    hb = _bbox([p for q in polys for p in q])
    # a full section often splits into two clusters (the bore separates the halves): every view
    # overlapping the hatch area belongs to the section
    parts = [v for v in views if v.bbox[0] <= hb[2] + snap and hb[0] <= v.bbox[2] + snap and
             v.bbox[1] <= hb[3] + snap and hb[1] <= v.bbox[3] + snap]
    if not parts:
        raise NotTurned("hatch outside every view")
    section = _merge_views(parts)
    views = [v for v in views if all(v is not p for p in parts)] + [section]
    lines = [(s.a, s.b) for s in section.segs if s.kind == "line" and not s.center and not s.hidden]
    lines_us = [(fr.us(a), fr.us(b)) for a, b in lines]

    def side_profile(group, sign):
        hp = [[(u, sign * s) for u, s in q] for q in group]
        ln = []
        for a, b in lines_us:
            sa, sb = sign * a[1], sign * b[1]
            if sa < -snap and sb < -snap:
                continue
            if (sa < -snap) != (sb < -snap):          # crosses the axis: keep this side's part
                t = sa / (sa - sb)
                cut = (a[0] + t * (b[0] - a[0]), 0.0)
                a, b = ((cut, (b[0], b[1])) if sa < -snap else ((a[0], a[1]), cut))
            ln.append(((a[0], sign * a[1]), (b[0], sign * b[1])))
        parts = [_ccw(q) for q in hp]
        for q in list(parts):
            parts += [f for f in outer_faces(q, ln, snap) if not any(_same_poly(f, g, snap) for g in parts)]
        merged = union(parts, snap) if len(parts) > 1 else parts[0]
        return merged, len(parts) - len(hp)

    prof_pos, extra_pos = side_profile(pos, 1)
    prof_neg, extra_neg = side_profile(neg, -1)
    if prof_pos is None and prof_neg is None:
        raise NotTurned("could not merge the section regions into one profile")
    inner = lambda p: min(s for _, s in p) if p else float("inf")   # noqa: E731
    # the side with the full bore (smaller inner radius) is the profile; the other may show a keyway
    sides = [(inner(p), -abs(_area(p)) if p else 0, sgn, p, ex) for sgn, p, ex in
             ((1, prof_pos, extra_pos), (-1, prof_neg, extra_neg)) if p]
    sides.sort(key=lambda t: (t[0], t[1]))
    r_in, _, sign, prof, extra = sides[0]
    other = sides[1] if len(sides) > 1 else None
    prof = _simplify(_ccw(prof), snap)
    if extra:
        issue("E207", "INFO", "단면도에서 해칭되지 않은 바깥 영역 %d개(기어 이·리브 등)를 재료로 포함했습니다" % extra)

    # contradiction check: an end view whose size equals the outer diameter must show that circle
    r_out = max(s for _, s in prof)
    for v in views:
        if v is section:
            continue
        if abs(v.w - 2 * r_out) < snap * 10 and abs(v.h - 2 * r_out) < snap * 10:
            if not any(s.kind in ("circle", "arc") and abs(s.r - r_out) < snap * 10 for s in v.segs):
                raise NotTurned("end view of the outer size has no circle")

    u0 = min(u for u, _ in prof)
    pts = [((u - u0) * scale, s * scale) for u, s in prof]

    # ---- keyway seen in the section (other side's inner edge)
    key = None
    if other and other[0] - r_in > snap * 5:
        top = other[0]
        oprof = _simplify(_ccw(other[3]), snap)
        edge_u = [u for u, s in oprof if abs(s - top) < snap]
        through = edge_u and abs(min(edge_u) - min(u for u, _ in oprof)) < snap * 5 and \
            abs(max(edge_u) - max(u for u, s in prof if s < top + snap)) < snap * 5
        width = keyway_width([v for v in views if v is not section], r_in, top, tol)
        # positive s = paper up (horizontal axis) or paper left (vertical axis) -> world +Z
        key = {"top": top * scale, "width": width * scale if width else None, "through": bool(through),
               "side": other[2]}
        if not through:
            issue("E207", "WARN", "단면도의 키홈이 전체 길이를 관통하지 않는 것 같습니다 — 관통 키홈으로 근사했습니다")

    def to_u(p):
        return (fr.us(p)[0] - u0) * scale
    res = _build_spec(pts, r_in * scale, key, fr, dims, texts, scale, snap * scale, issue, to_u)
    res["silhouette"] = section_envelope(section, fr, u0, max(u for u, _ in prof), scale)
    # material regions of both halves as drawn (hatch + unhatched outer regions), XZ mm:
    # the model cut at Y = 0 must show exactly these (E410)
    regions = []
    for poly, sgn in ((prof_pos, 1), (prof_neg, -1)):
        if poly:
            regions.append([[_r((u - u0) * scale), _r(sgn * abs(s_) * scale)] for u, s_ in poly])
    res["section_regions"] = regions
    return res


def section_envelope(section, fr, u0, u1, scale, n=400):
    """Side silhouette of the turned part straight from the drawn section lines:
    at each axial position the farthest visible line from the axis, mirrored (view XZ, mm)."""
    segs = []
    for s in section.segs:
        if s.center or s.hidden:
            continue
        if s.kind == "line":
            segs.append((fr.us(s.a), fr.us(s.b)))
        elif s.kind in ("arc", "circle"):
            a0, a1 = (s.a0, s.a1) if s.kind == "arc" else (0.0, 360.0)
            span = (a1 - a0) % 360 or 360.0
            pts = [fr.us((s.c[0] + s.r * math.cos(math.radians(a0 + span * k / 32)),
                          s.c[1] + s.r * math.sin(math.radians(a0 + span * k / 32)))) for k in range(33)]
            segs += list(zip(pts, pts[1:]))
    top = []
    eps = (u1 - u0) * 1e-6
    for k in range(n + 1):
        u = u0 + (u1 - u0) * k / n
        u = min(max(u, u0 + eps), u1 - eps)
        r = 0.0
        for (ua, sa), (ub, sb) in segs:
            if abs(ub - ua) < 1e-12:
                if abs(ua - u) <= (u1 - u0) / n / 2:
                    r = max(r, abs(sa), abs(sb))
                continue
            if min(ua, ub) <= u <= max(ua, ub):
                r = max(r, abs(sa + (u - ua) * (sb - sa) / (ub - ua)))
        top.append(((u - u0) * scale, r * scale))
    poly = top + [(x, -y) for x, y in reversed(top)]
    return [[round(x, 4), round(y, 4)] for x, y in poly]


# ----------------------------------------------------------------------------- spec writing
def _r(v, nd=4):
    v = round(float(v), nd)
    return int(v) if v.is_integer() else v


def _label(text, value):
    t = text or "<>"
    t = t.replace("%%c", "Ø").replace("%%C", "Ø").replace("%%d", "°").replace("%%p", "±")
    t = re.sub(r"\\A\d;|\\S([^;^]*)\^\s*([^;]*);", lambda m: (" %s/%s" % (m.group(1).strip(), m.group(2).strip())) if m.group(0).startswith("\\S") else "", t)
    t = t.replace("\\~", " ")
    return t.replace("<>", "%g" % round(value, 4)).strip()


def _diff(a, b):
    """'(a - b)' with the trivial cases folded."""
    if b in ("0", 0):
        return a if re.fullmatch(r"[A-Za-z_]\w*(/2)?", a) else "(%s)" % a
    return "(%s - %s)" % (a, b)


def _build_spec(pts, r_bore, key, fr, dims, texts, scale, tol, issue_raw, to_u=None):
    seen = set()

    def issue(code, sev, msg, **kw):
        if (code, msg) not in seen:
            seen.add((code, msg))
            issue_raw(code, sev, msg, **kw)
    SMALL = lambda real: max(0.05, abs(real) * 0.003)   # noqa: E731  drawing vs text: drafting error
    used = set()
    params, notes = {}, []

    def ends(d):
        """Axial positions (mm from the datum face) of a linear dimension's extension points."""
        if to_u is None or len(d["points"]) < 3 or d["type"] not in ("linear", "aligned"):
            return None
        return sorted((to_u(d["points"][1]), to_u(d["points"][2])))

    def dim_match(value, kinds, want_dia, span=None):
        """Dimension whose drawn length equals value (and, for axial ones, spans `span`)."""
        best = None
        for d in dims:
            if id(d) in used or d["type"] not in kinds:
                continue
            real = d["measured"] * scale
            if span is not None:
                e = ends(d)
                if e is not None and not (abs(e[0] - span[0]) <= max(tol, SMALL(value)) and
                                          abs(e[1] - span[1]) <= max(tol, SMALL(value))):
                    continue
            if abs(real - value) <= max(tol, SMALL(value)):
                is_dia = bool(re.search(r"%%c|Ø|ø|⌀", d["text"] or "", re.I)) or d["type"] == "diameter"
                score = (abs(real - value), 0 if is_dia == want_dia else 1)
                if best is None or score < best[0]:
                    best = (score, d)
        return best[1] if best else None

    def from_dim(name, value, kinds, want_dia, what, span=None):
        d = dim_match(value, kinds, want_dia, span)
        if d is None:
            return None
        used.add(id(d))
        real = d["measured"] * scale
        shown = d["shown"]
        val = shown if shown is not None else real
        src = {"type": "drawing", "view": "section", "label": _label(d["text"], real), "confidence": 1.0}
        if shown is not None and abs(shown - real) > max(0.01, real * 1e-4):
            if abs(shown - real) <= SMALL(real):
                src["drawn"] = _r(real)
                issue("E107", "INFO", "치수 '%s': 그려진 길이 %.3f와 %.3f mm 차이 — 작은 작도 오차로 보고 치수 문자 값 %g을 씁니다"
                      % (src["label"], real, abs(shown - real), shown))
            else:
                params[name] = {"value": _r(real), "source": {"type": "conflict", "view": "section",
                                                              "label": src["label"], "drawn": _r(real), "shown": shown}}
                return name
        params[name] = {"value": _r(val), "source": src}
        params[name].update(tolerance.parse(d["text"], val))
        return name

    def from_text(name, dia):
        for t in texts:
            for m in DIA_TEXT.finditer(t["text"]):
                if abs(float(m.group(1)) - dia) <= SMALL(dia):
                    params[name] = {"value": _r(float(m.group(1))), "source": {
                        "type": "drawing", "view": "section", "label": t["text"].replace("\\~", " ").strip(), "confidence": 0.95}}
                    params[name].update(tolerance.parse(t["text"], float(m.group(1))))
                    return name
        return None

    # ---- axial coordinates (from the datum face u=0)
    us = sorted({round(u, 4) for u, _ in pts})
    ax = {0.0: 0}
    for i, u in enumerate([u for u in us if u > tol]):
        name = "L%d" % (i + 1)
        if from_dim(name, u, ("linear", "aligned"), False, "axial", span=(0.0, u)):
            ax[u] = name
    # chained dimensions: a step measured from another, already known, axial position
    for _ in range(len(us)):
        progress = False
        for i, u in enumerate(us):
            if u in ax:
                continue
            for k, ek in list(ax.items()):
                lo, hi = sorted((k, u))
                name = "L%d" % (i + 1)
                if from_dim(name, hi - lo, ("linear", "aligned"), False, "axial chain", span=(lo, hi)):
                    if params[name]["source"]["type"] == "drawing":
                        params[name]["source"]["chain_from"] = _r(k)
                    ax[u] = ("%s + %s" % (ek, name) if u > k else "%s - %s" % (ek, name)) if ek != 0 else name
                    progress = True
                    break
        if not progress:
            break
    # chamfer callouts "1.5x45" give short axial steps
    for d in dims:
        if id(d) in used:
            continue
        m = CHAMFER_TEXT.search(d["text"] or "")
        if m:
            c = float(m.group(1))
            for u in us:
                if u not in ax and abs(u - c) <= SMALL(c):
                    used.add(id(d))
                    params["c1"] = {"value": _r(c), "source": {"type": "drawing", "view": "section",
                                                              "label": _label(d["text"], c), "confidence": 1.0}}
                    ax[u] = "c1"
    # ---- radial coordinates as diameters
    rs = sorted({round(s, 4) for _, s in pts})
    rad = {}
    for i, s in enumerate(rs):
        name = "D%d" % (i + 1)
        if from_dim(name, 2 * s, ("linear", "aligned", "diameter"), True, "diameter") or from_text(name, 2 * s):
            rad[s] = name + "/2"

    def val(e):
        return expr_value(e, params)

    # ---- derive the rest: 45/30/60 deg edges and chamfers from a parameterised neighbour
    exprs = [[ax.get(round(u, 4)), rad.get(round(s, 4))] for u, s in pts]
    n = len(pts)
    for _ in range(3):
        for i in range(n):
            for j in ((i - 1) % n, (i + 1) % n):
                (u, s), (uj, sj) = pts[i], pts[j]
                du, ds = u - uj, s - sj
                if abs(du) < tol or abs(ds) < tol:
                    continue
                angle = math.degrees(math.atan2(abs(ds), abs(du)))
                std = next((a for a in STD_ANGLES if abs(angle - a) < 0.15), None)
                if std is None:
                    continue
                k = math.tan(math.radians(std))
                eu, es = exprs[i]
                euj, esj = exprs[j]
                if es is None and eu is not None and euj is not None and esj is not None:
                    sgn = "+" if ds > 0 else "-"
                    du_e = _diff(eu, euj) if du > 0 else _diff(euj, eu)
                    exprs[i][1] = "%s %s %s%s" % (esj, sgn, du_e, "" if std == 45 else " * %.10g" % k)
                    issue("E207", "INFO", "반지름 %.3f: 치수가 없어 %g° 경사 연장으로 계산했습니다" % (s, std))
                elif eu is None and es is not None and esj is not None and euj is not None:
                    sgn = "+" if du > 0 else "-"
                    ds_e = _diff(es, esj) if ds > 0 else _diff(esj, es)
                    exprs[i][0] = "%s %s %s%s" % (euj, sgn, ds_e, "" if std == 45 else " / %.10g" % k)
                    issue("E207", "INFO", "축 방향 %.3f: 치수가 없어 %g° 경사 연장으로 계산했습니다" % (u, std))
    # ---- anything left: inferred from the drawn geometry
    for i, (u, s) in enumerate(pts):
        if exprs[i][0] is None:
            name = "Lx%d" % (i + 1)
            params[name] = {"value": _r(u), "confidence": 0.9, "source": {"type": "inferred", "reason": "축 방향 치수 없음 - 형상에서 잰 값"}}
            exprs[i][0] = name
            for k2, (u2, _) in enumerate(pts):
                if exprs[k2][0] is None and abs(u2 - u) < tol:
                    exprs[k2][0] = name
        if exprs[i][1] is None:
            name = "Dx%d" % (i + 1)
            params[name] = {"value": _r(2 * s), "confidence": 0.9, "source": {"type": "inferred", "reason": "지름 치수 없음 - 형상에서 잰 값"}}
            exprs[i][1] = name + "/2"
            for k2, (_, s2) in enumerate(pts):
                if exprs[k2][1] is None and abs(s2 - s) < tol:
                    exprs[k2][1] = name + "/2"

    # values as the model will see them (text values win over drawn geometry)
    model_pts = [(val(eu), val(es)) for eu, es in exprs]
    for (u, s), (mu, ms) in zip(pts, model_pts):
        if abs(u - mu) > 0.1 or abs(s - ms) > 0.1:
            issue("E107", "BLOCK", "단면 꼭짓점 (%.3f, %.3f)이 치수로 계산한 (%.3f, %.3f)과 많이 다릅니다 — 도면 확인 필요" % (u, s, mu, ms))

    features = [
        {"id": "sk_profile", "type": "sketch", "plane": "XZ",
         "entities": [{"id": "half", "kind": "polyline", "closed": True, "points": [[eu, es] for eu, es in exprs]}]},
        {"id": "rv_body", "type": "revolve", "profile": "sk_profile", "axis": "X", "angle": 360, "operation": "new_body"},
    ]
    length_e = max(zip(model_pts, exprs), key=lambda t: t[0][0])[1][0]
    dia_e = max(zip(model_pts, exprs), key=lambda t: t[0][1])[1][1]
    dia_e = dia_e[:-2] if dia_e.endswith("/2") else "2 * (%s)" % dia_e
    checks = [{"id": "c_bbox", "kind": "bbox", "expected": [length_e, dia_e, dia_e]}]

    # bore
    bore_e = None
    inner_pts = [(mp, e) for mp, e in zip(model_pts, exprs) if abs(mp[1] - min(p[1] for p in model_pts)) < 0.01]
    if min(p[1] for p in model_pts) > 0.01:
        bore_e = inner_pts[0][1][1]
        bore_d = bore_e[:-2] if bore_e.endswith("/2") else "2 * (%s)" % bore_e
        checks.append({"id": "c_bore", "kind": "hole_diameter", "expected": bore_d})
        checks.append({"id": "c_bore_pos", "kind": "hole_positions", "axis": "X", "diameter": bore_d, "expected": [[0, 0]]})

    # keyway
    key_vol = 0.0
    if key:
        r_b = min(p[1] for p in model_pts)
        tname = from_dim("key_t", r_b + key["top"], ("linear", "aligned"), False, "keyway") or \
            from_dim("key_t", key["top"], ("linear", "aligned"), False, "keyway")
        if tname and abs(val(tname) - key["top"]) <= SMALL(key["top"]):
            top_e = tname
        elif tname:
            top_e = "%s - %s" % (tname, bore_e or "0")
        else:
            params["key_t"] = {"value": _r(key["top"]), "confidence": 0.9,
                               "source": {"type": "inferred", "reason": "키홈 깊이 치수 없음 - 단면도에서 잰 값 (축 중심 기준)"}}
            top_e = "key_t"
        if key["width"] is not None:
            wname = from_dim("key_w", key["width"], ("linear", "aligned"), False, "keyway width")
            if not wname:
                params["key_w"] = {"value": _r(key["width"]), "confidence": 0.9,
                                   "source": {"type": "inferred", "reason": "키홈 폭 치수 없음 - 끝 모양에서 잰 값"}}
        else:
            params["key_w"] = {"value": None, "source": {"type": "unresolved",
                                                         "reason": "키홈 폭을 보여 주는 끝 모양(원 위의 홈)을 찾지 못함"}}
        sgn = key["side"]
        corner_v = "0" if sgn > 0 else "-(%s)" % top_e
        features.append({"id": "sk_key", "type": "sketch", "plane": "YZ",
                         "entities": [{"id": "key", "kind": "rect", "corner": ["-key_w/2", corner_v], "size": ["key_w", top_e]}]})
        features.append({"id": "ex_key", "type": "extrude", "profile": "sk_key", "distance": "through_all",
                         "direction": "symmetric", "operation": "cut"})
        checks.append({"id": "c_key_top", "kind": "plane", "axis": "Z", "at": top_e if sgn > 0 else "-(%s)" % top_e,
                       "note": "키홈 바닥면 위치 (단면도에서 키홈이 보이는 쪽)"})
        if params["key_w"]["value"] is not None:
            key_vol = _keyway_volume(model_pts, val(top_e), val("key_w"))
        else:
            key_vol = None

    if key_vol is None:
        checks.append({"id": "c_vol", "kind": "volume", "expected": None, "note": "키홈 폭을 몰라 기록만"})
    else:
        vol = _revolve_volume(model_pts) - key_vol
        checks.append({"id": "c_vol", "kind": "volume", "expected": _r(vol, 3), "rel_tol": 0.005,
                       "note": "단면 프로파일 Pappus 계산%s" % (" - 키홈" if key else "")})

    for d in dims:
        if id(d) not in used:
            notes.append({"text": "미사용 치수 %s" % _label(d["text"], d["measured"] * scale), "applied": False,
                          "reason": "단면 프로파일 꼭짓점에 해당하지 않음 (기준 거리·각도·이폭 등) - 직접 확인"})
    for t in texts:
        notes.append({"text": t["text"].replace("\\~", " "), "applied": False, "reason": "도면 주기"})
    return {"parameters": params, "features": features, "checks": checks, "notes": notes,
            "note": "회전체 단면도: 축 = X, 기준면 X=0, 반단면 프로파일을 XZ 평면에 그려 X축으로 회전",
            "profile_mm": [[_r(u), _r(s)] for u, s in model_pts]}


def expr_value(e, params):
    from ..spec import expr as E
    resolved = E.resolve_parameters({k: v["value"] for k, v in params.items() if v.get("value") is not None})
    return E.evaluate(e, resolved)


def _revolve_volume(pts):
    """Pappus: volume of the (u, r) polygon revolved about the u axis."""
    a = cy = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        f = x0 * y1 - x1 * y0
        a += f
        cy += (y0 + y1) * f
    a /= 2
    if abs(a) < 1e-12:
        return 0.0
    cy /= 6 * a
    return 2 * math.pi * abs(a) * cy


def _inner_radius(pts, u):
    """Smallest r where the profile crosses the axial position u."""
    rs = []
    n = len(pts)
    for i in range(n):
        (u0, r0), (u1, r1) = pts[i], pts[(i + 1) % n]
        if min(u0, u1) <= u <= max(u0, u1) and abs(u1 - u0) > 1e-12:
            rs.append(r0 + (u - u0) * (r1 - r0) / (u1 - u0))
    return min(rs) if rs else None


def _keyway_volume(pts, top, width, steps=4000):
    """Material removed by a through keyway |y| <= w/2, 0 <= z <= top along the whole profile."""
    u_min, u_max = min(p[0] for p in pts), max(p[0] for p in pts)
    half = width / 2
    vol = 0.0
    for k in range(steps):
        u = u_min + (k + 0.5) * (u_max - u_min) / steps
        rb = _inner_radius(pts, u)
        if rb is None or rb >= top:
            continue
        # rectangle area minus the part inside the bore circle (|y| <= half, 0 <= z <= sqrt(rb^2 - y^2))
        if rb <= half:
            inside = math.pi * rb * rb / 2
        else:
            inside = 2 * (half / 2 * math.sqrt(rb * rb - half * half) + rb * rb / 2 * math.asin(half / rb))
        vol += (width * top - inside) * (u_max - u_min) / steps
    return vol
