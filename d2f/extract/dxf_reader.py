"""DXF drawing -> part-spec.json (Phase 3, prismatic / stacked parts).

What it understands
  * orthographic views (front / top / right), third angle by default, first angle detected
    from the view layout or "1ST ANGLE" / "제1각법" text
  * a single top view plus a thickness note ("t=5")
  * parts made of stacked extrusions along Z: each Z band (from the front view) is
    extruded from the top-view outline, a top-view circle, or a box from the front/side views
  * outline: rectangle, rectangle with equal corner radii, circle, straight-edged polygon
  * holes from top-view circles: through / blind (front-view hidden lines), counterbores
    (concentric circles), positions and diameters
  * DIMENSION entities: used as the source of parameter values and cross-checked against
    the drawn geometry (an overridden text that disagrees with the geometry -> E107 BLOCK)
  * border and title block are ignored (layer names, enclosing rectangle, title keywords)

Everything is in drawing units scaled to mm. Spec coordinates: origin at the part's
bounding-box minimum corner (X right in the front view, Y away from the viewer, Z up).
"""

import math
import os
import re

from . import tolerance

try:
    import ezdxf
except ImportError:  # pragma: no cover - reported by the CLI
    ezdxf = None

TITLE_LAYER = re.compile(r"title|border|frame|sheet|표제|윤곽|도곽", re.I)
CONSTRUCTION_LAYER = re.compile(r"^(aux|construct\w*|defpoints|hilfs\w*|보조\w*|xline)$", re.I)
CENTER_LT = re.compile(r"center|chain|dashdot|중심|일점", re.I)
HIDDEN_LT = re.compile(r"hidden|dash|숨은|파선", re.I)
TITLE_WORDS = re.compile(r"scale|척도|drawn|title|도명|품명|도번|dwg|approved|검도|설계", re.I)
FIRST_ANGLE = re.compile(r"1st\s*angle|first\s*angle|제\s*1\s*각법", re.I)
THICKNESS = re.compile(r"(?:^|[\s,(])[tT]\s*=?\s*(\d+(?:\.\d+)?)")
MATERIAL = re.compile(r"(?:material|재질|재료)\s*[:：]?\s*([A-Za-z0-9\-]+)", re.I)
NUMBER = re.compile(r"\d+(?:\.\d+)?")

INSUNITS_TO_MM = {0: 1.0, 1: 25.4, 4: 1.0, 5: 10.0, 6: 1000.0}


class ExtractError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------- primitives
class Seg:
    """A drawing primitive in drawing coordinates."""

    __slots__ = ("kind", "a", "b", "c", "r", "a0", "a1", "hidden", "center", "layer")

    def __init__(self, kind, layer, hidden=False, center=False, a=None, b=None, c=None, r=0.0,
                 a0=0.0, a1=0.0):
        self.kind, self.layer, self.hidden, self.center = kind, layer, hidden, center
        self.a, self.b, self.c, self.r, self.a0, self.a1 = a, b, c, r, a0, a1

    def bbox(self):
        if self.kind == "line":
            return (min(self.a[0], self.b[0]), min(self.a[1], self.b[1]),
                    max(self.a[0], self.b[0]), max(self.a[1], self.b[1]))
        if self.kind == "circle":
            return (self.c[0] - self.r, self.c[1] - self.r, self.c[0] + self.r, self.c[1] + self.r)
        pts = [self.a, self.b]
        for ang in (0, 90, 180, 270):
            if _angle_in(ang, self.a0, self.a1):
                pts.append((self.c[0] + self.r * math.cos(math.radians(ang)),
                            self.c[1] + self.r * math.sin(math.radians(ang))))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))


def _angle_in(a, a0, a1):
    a, a0, a1 = a % 360, a0 % 360, a1 % 360
    return a0 <= a <= a1 if a0 <= a1 else (a >= a0 or a <= a1)


def _arc_ends(c, r, a0, a1):
    return ((c[0] + r * math.cos(math.radians(a0)), c[1] + r * math.sin(math.radians(a0))),
            (c[0] + r * math.cos(math.radians(a1)), c[1] + r * math.sin(math.radians(a1))))


def _union(b1, b2):
    return (min(b1[0], b2[0]), min(b1[1], b2[1]), max(b1[2], b2[2]), max(b1[3], b2[3]))


def _overlap(b1, b2, tol):
    return not (b1[2] < b2[0] - tol or b2[2] < b1[0] - tol or b1[3] < b2[1] - tol or b2[3] < b1[1] - tol)


def _inside(inner, outer, tol=1e-6):
    return inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and \
        inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol


# ----------------------------------------------------------------------------- loading
def _linetype(e, doc):
    lt = e.dxf.get("linetype", "BYLAYER")
    if lt.upper() in ("BYLAYER", "BYBLOCK"):
        try:
            lt = doc.layers.get(e.dxf.layer).dxf.linetype
        except Exception:
            lt = "CONTINUOUS"
    return lt or "CONTINUOUS"


def _flatten(e):
    """Yield primitive entities from polylines / inserts."""
    t = e.dxftype()
    if t in ("LWPOLYLINE", "POLYLINE", "INSERT"):
        try:
            for v in e.virtual_entities():
                yield from _flatten(v)
        except Exception:
            return
    else:
        yield e


def load(path):
    if ezdxf is None:
        raise ExtractError("E101", "ezdxf가 설치되어 있지 않습니다 (pip install ezdxf)")
    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        raise ExtractError("E101", "DXF를 읽을 수 없습니다: %s" % e)
    msp = doc.modelspace()
    segs, texts, dims = [], [], []
    for raw in msp:
        t = raw.dxftype()
        if t == "DIMENSION":
            dims.append(raw)
            continue
        if t in ("TEXT", "MTEXT"):
            txt = raw.plain_text() if t == "MTEXT" else raw.dxf.text
            texts.append({"text": txt, "pos": tuple(raw.dxf.insert)[:2], "layer": raw.dxf.layer})
            continue
        for e in _flatten(raw):
            et = e.dxftype()
            layer = e.dxf.get("layer", "0")
            if CONSTRUCTION_LAYER.match(layer) or et in ("XLINE", "RAY"):
                continue            # construction geometry is not part of the drawing
            lt = _linetype(e, doc)
            center = bool(CENTER_LT.search(lt)) or "CENTER" in layer.upper() or "중심" in layer
            # "DASHDOT" etc. contain "dash": centre lines win over hidden lines
            hidden = not center and (bool(HIDDEN_LT.search(lt)) or bool(HIDDEN_LT.search(layer)))
            if et == "LINE":
                a, b = tuple(e.dxf.start)[:2], tuple(e.dxf.end)[:2]
                if math.dist(a, b) > 1e-9:
                    segs.append(Seg("line", layer, hidden, center, a=a, b=b))
            elif et == "CIRCLE":
                segs.append(Seg("circle", layer, hidden, center, c=tuple(e.dxf.center)[:2], r=e.dxf.radius))
            elif et == "ARC":
                c, r, a0, a1 = tuple(e.dxf.center)[:2], e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle
                a, b = _arc_ends(c, r, a0, a1)
                segs.append(Seg("arc", layer, hidden, center, a=a, b=b, c=c, r=r, a0=a0, a1=a1))
    units = doc.header.get("$INSUNITS", 0)
    return doc, segs, texts, dims, units


# ----------------------------------------------------------------------------- sheet cleanup
def _rectangles(segs):
    """Axis-aligned rectangles formed by 4 lines (bbox, member indices)."""
    lines = [(i, s) for i, s in enumerate(segs) if s.kind == "line"]
    horiz = [(i, s) for i, s in lines if abs(s.a[1] - s.b[1]) < 1e-6]
    vert = [(i, s) for i, s in lines if abs(s.a[0] - s.b[0]) < 1e-6]
    rects = []
    for i, h1 in horiz:
        x0, x1 = sorted((h1.a[0], h1.b[0]))
        for j, h2 in horiz:
            if j <= i or abs(h2.a[1] - h1.a[1]) < 1e-6:
                continue
            if sorted((h2.a[0], h2.b[0])) != [x0, x1]:
                continue
            y0, y1 = sorted((h1.a[1], h2.a[1]))
            vs = [k for k, v in vert if sorted((v.a[1], v.b[1])) == [y0, y1] and
                  (abs(v.a[0] - x0) < 1e-6 or abs(v.a[0] - x1) < 1e-6)]
            if len(vs) >= 2:
                rects.append(((x0, y0, x1, y1), {i, j, *vs}))
    return rects


def clean_sheet(segs, texts):
    """Remove border, title block and title-layer geometry. Returns (segs, texts, title_texts)."""
    keep = [s for s in segs if not TITLE_LAYER.search(s.layer)]
    title_texts = [t for t in texts if TITLE_LAYER.search(t["layer"])]
    texts = [t for t in texts if not TITLE_LAYER.search(t["layer"])]
    if not keep:
        return keep, texts, title_texts
    ext = keep[0].bbox()
    for s in keep[1:]:
        ext = _union(ext, s.bbox())
    drop = set()
    rects = _rectangles(keep)
    # border: a rectangle that encloses everything else AND holds title-block text inside it;
    # otherwise a one-view part outline would be mistaken for the border
    all_texts = texts + title_texts
    for bb, members in rects:
        if not (bb[0] <= ext[0] + 1e-6 and bb[1] <= ext[1] + 1e-6 and bb[2] >= ext[2] - 1e-6 and bb[3] >= ext[3] - 1e-6):
            continue
        if any(TITLE_WORDS.search(t["text"]) and _inside((t["pos"][0], t["pos"][1], t["pos"][0], t["pos"][1]), bb)
               for t in all_texts):
            drop |= members
    # title block: a rectangle holding title keywords
    for bb, members in rects:
        if members & drop:
            continue
        inside = [t for t in texts if _inside((t["pos"][0], t["pos"][1], t["pos"][0], t["pos"][1]), bb)]
        if any(TITLE_WORDS.search(t["text"]) for t in inside):
            drop |= members
            for i, s in enumerate(keep):
                if _inside(s.bbox(), bb):
                    drop.add(i)
            title_texts += inside
            texts = [t for t in texts if t not in inside]
    return [s for i, s in enumerate(keep) if i not in drop], texts, title_texts


# ----------------------------------------------------------------------------- views
class View:
    def __init__(self, segs, bbox):
        self.segs = segs
        self.bbox = bbox
        self.name = None
        self.dims = []
        self.flip_y = False   # first-angle top view
        self.flip_x = False   # first-angle side view

    @property
    def w(self):
        return self.bbox[2] - self.bbox[0]

    @property
    def h(self):
        return self.bbox[3] - self.bbox[1]

    def local(self, p):
        """Drawing point -> view-local (u, v) with origin at the view's min corner."""
        u, v = p[0] - self.bbox[0], p[1] - self.bbox[1]
        if self.flip_x:
            u = self.w - u
        if self.flip_y:
            v = self.h - v
        return u, v


def is_isometric(view):
    """A pictorial (isometric) view: many straight edges at ~30 / 150 degrees."""
    lines = [s for s in view.segs if s.kind == "line" and not s.center]
    if len(lines) < 6:
        return False
    a30 = a150 = vert = 0
    for s in lines:
        ang = math.degrees(math.atan2(s.b[1] - s.a[1], s.b[0] - s.a[0])) % 180
        if abs(ang - 30) < 1:
            a30 += 1
        elif abs(ang - 150) < 1:
            a150 += 1
        elif abs(ang - 90) < 3:
            vert += 1
    # an isometric view shows all three axis families; a rhombus plan view (only +-30) is not one
    # true isometric axes are exactly 30/150 deg; a rhombus flange at ~31 deg must not match
    return a30 + a150 >= 6 and (a30 + a150) / len(lines) > 0.3 and a30 and a150 and vert / len(lines) > 0.1


def split_views(segs, tol):
    shape = [s for s in segs if not s.center]
    n = len(shape)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    boxes = [s.bbox() for s in shape]
    order = sorted(range(n), key=lambda i: boxes[i][0])
    for a_i, i in enumerate(order):
        for j in order[a_i + 1:]:
            if boxes[j][0] > boxes[i][2] + tol:
                break
            if _overlap(boxes[i], boxes[j], tol):
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    views = []
    for members in groups.values():
        bb = boxes[members[0]]
        for k in members[1:]:
            bb = _union(bb, boxes[k])
        views.append(View([shape[k] for k in members], bb))
    if not views:
        return []
    # a cluster inside another cluster's box (holes, inner pockets) belongs to that view
    views.sort(key=lambda v: v.w * v.h, reverse=True)
    merged = []
    for v in views:
        host = next((m for m in merged if _inside(v.bbox, m.bbox, tol)), None)
        if host is not None:
            host.segs.extend(v.segs)
        else:
            merged.append(v)
    views = merged
    biggest = max(max(v.w, v.h) for v in views)
    views = [v for v in views if max(v.w, v.h) >= 0.05 * biggest and
             any(not s.hidden for s in v.segs)]
    # centre lines / marks join the view they sit in
    for s in segs:
        if s.center:
            for v in views:
                if _overlap(s.bbox(), v.bbox, tol):
                    v.segs.append(s)
                    break
    return views


def identify_views(views, texts, tol):
    """Name views front / top / side; detect projection. Returns (dict, projection, issues)."""
    issues = []
    first_angle = any(FIRST_ANGLE.search(t["text"]) for t in texts)
    if not views:
        issues.append(("E102", "BLOCK", "투상도를 찾지 못했습니다"))
        return {}, "unknown", issues
    if len(views) == 1:
        views[0].name = "top"
        return {"top": views[0]}, "single", issues

    def same_x(a, b):
        return abs(a.bbox[0] - b.bbox[0]) <= tol and abs(a.bbox[2] - b.bbox[2]) <= tol

    def same_y(a, b):
        return abs(a.bbox[1] - b.bbox[1]) <= tol and abs(a.bbox[3] - b.bbox[3]) <= tol

    best = None
    for f in views:
        vp = [v for v in views if v is not f and same_x(v, f)]
        hp = [v for v in views if v is not f and same_y(v, f)]
        score = (1 if vp else 0) + (1 if hp else 0)
        if best is None or score > best[0]:
            best = (score, f, vp, hp)
    score, front, vps, hps = best
    if score == 0:
        issues.append(("E102", "BLOCK", "정면도/평면도/측면도를 정렬로 찾지 못했습니다 (뷰 %d개)" % len(views)))
        return {}, "unknown", issues
    named = {"front": front}
    if vps and hps:
        top = vps[0]
        side = hps[0]
    elif vps:
        top, side = vps[0], None
    else:
        top, side = None, hps[0]
    # with 2 views of the same width the lower one is the front in third angle
    if top is not None and not hps and top.bbox[1] < front.bbox[1] and not first_angle:
        front, top = top, front
        named["front"] = front
    projection = "third"
    if top is not None:
        above = top.bbox[1] > front.bbox[3]
        if not above:
            projection = "first"
        named["top"] = top
    if side is not None:
        named["side"] = side
        if projection == "first" or (top is None and first_angle):
            projection = "first"
    if first_angle:
        projection = "first"
    if projection == "first":
        if top is not None:
            top.flip_y = True       # top view below the front: Y=0 at its upper edge
        if side is not None and side.bbox[0] < front.bbox[0]:
            pass                    # left of front in first angle = right-side view, Y grows right
        elif side is not None:
            side.flip_x = True      # right of front in first angle = left-side view
    elif side is not None and side.bbox[0] < front.bbox[0]:
        side.flip_x = True          # third angle, left view
    for k, v in named.items():
        v.name = k
    return named, projection, issues


# ----------------------------------------------------------------------------- dimensions
def dim_text_value(text):
    """Number written in a dimension text -> (value or None for '<>' / no number, multiplier).
    Handles '4-%%c6.6', 'R5', '1.5x45%%d' (chamfer), 'P.C.D 75', '31.83%%p0.1', '(18)'."""
    text = (text or "").strip()
    chamfer = re.match(r"\s*(\d+(?:\.\d+)?)\s*[xX×]\s*45", text)     # "1.5x45°" chamfer callout
    if chamfer:
        return float(chamfer.group(1)), 1
    mm = re.match(r"\s*(\d+)\s*[-xX×]\s*", text)
    mult = int(mm.group(1)) if mm else 1
    if text in ("", "<>") or "<>" in text:
        return None, mult
    body = text[mm.end():] if mm else text
    body = re.sub(r"(?i)%%c|Ø|ø|⌀|\bR(?=\d)|p\.?c\.?d\.?|\\A\d;", " ", body)
    n = NUMBER.search(body)
    return (float(n.group(0)) if n else None), mult


def parse_dims(dims, tol):
    out = []
    for d in dims:
        try:
            m = float(d.get_measurement())
        except Exception:
            continue
        dtype = d.dxf.dimtype & 0x0F
        text = (d.dxf.get("text", "") or "").strip()
        pts = []
        for attr in ("defpoint", "defpoint2", "defpoint3", "defpoint4", "text_midpoint"):
            if d.dxf.hasattr(attr):
                pts.append(tuple(d.dxf.get(attr))[:2])
        shown, mult = dim_text_value(text)
        out.append({"type": {0: "linear", 1: "aligned", 2: "angular", 3: "diameter", 4: "radius",
                             5: "angular3", 6: "ordinate"}.get(dtype, str(dtype)),
                    "measured": m, "shown": shown, "text": text, "mult": mult, "points": pts,
                    "entity": d})
    return out


def assign_dims(dims, views, tol):
    for d in dims:
        best, bd = None, None
        for v in views.values():
            for p in d["points"][:3]:
                dx = max(v.bbox[0] - p[0], 0, p[0] - v.bbox[2])
                dy = max(v.bbox[1] - p[1], 0, p[1] - v.bbox[3])
                dist = math.hypot(dx, dy)
                if bd is None or dist < bd:
                    best, bd = v, dist
        d["view"] = best.name if best else None
        if best:
            best.dims.append(d)


def drawing_scale(dims, units):
    """mm per drawing unit, from $INSUNITS and dims whose text differs from the measurement."""
    base = INSUNITS_TO_MM.get(units, 1.0)
    ratios = [d["shown"] / (d["measured"] * base) for d in dims
              if d["shown"] and d["measured"] > 1e-9 and d["type"] in ("linear", "aligned", "diameter", "radius")]
    if not ratios:
        return base, []
    ratios.sort()
    med = ratios[len(ratios) // 2]
    agree = [r for r in ratios if abs(r - med) / med < 0.002]
    if len(agree) >= max(2, len(ratios) // 2 + 1) and abs(med - 1.0) > 0.002:
        return base * med, []
    return base, []


# ----------------------------------------------------------------------------- outline
def _graph_outer_loop(segs, tol):
    """Outer boundary of a view's visible line/arc segments as an ordered list of segments."""
    edges = [s for s in segs if s.kind in ("line", "arc") and not s.hidden and not s.center]
    if not edges:
        return None

    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))
    adj = {}
    for s in edges:
        ka, kb = key(s.a), key(s.b)
        if ka == kb:
            continue
        adj.setdefault(ka, []).append((kb, s))
        adj.setdefault(kb, []).append((ka, s))
    if not adj:
        return None
    pos = {}
    for s in edges:
        pos[key(s.a)] = s.a
        pos[key(s.b)] = s.b
    start = min(adj, key=lambda k: (pos[k][1], pos[k][0]))
    prev_dir = math.pi  # arriving from the left, walking along the bottom boundary

    def direction(frm, s, to):
        if s.kind == "line":
            return math.atan2(pos[to][1] - pos[frm][1], pos[to][0] - pos[frm][0])
        # arc: tangent at the start point
        p = pos[frm]
        ang = math.atan2(p[1] - s.c[1], p[0] - s.c[0])
        ccw = key(s.a) == frm
        return ang + (math.pi / 2 if ccw else -math.pi / 2)
    loop, cur, used = [], start, set()
    for _ in range(len(edges) + 2):
        best = None
        for nxt, s in adj[cur]:
            if id(s) in used and len(adj[cur]) > 1 and cur != start:
                continue
            d = direction(cur, s, nxt)
            # turn = how far clockwise from the reverse of the arrival direction; smallest = right-most
            turn = (prev_dir + math.pi - d) % (2 * math.pi)
            if turn < 1e-9:
                turn = 2 * math.pi
            if best is None or turn < best[0]:
                best = (turn, nxt, s, d)
        if best is None:
            return None
        _, nxt, s, d = best
        if id(s) in used:
            break
        used.add(id(s))
        loop.append((cur, nxt, s))
        if s.kind == "arc":
            q = pos[nxt]
            ang = math.atan2(q[1] - s.c[1], q[0] - s.c[0])
            d = ang + (math.pi / 2 if key(s.a) == cur else -math.pi / 2)
        prev_dir = d
        cur = nxt
        if cur == start:
            return [(pos[a], pos[b], s) for a, b, s in loop]
    return None


def loop_points(loop, tol, steps=24):
    """Ordered outer-loop segments -> point list with arcs flattened (drawing coordinates)."""
    pts = []
    for a, b, s in loop:
        pts.append(a)
        if s.kind != "arc":
            continue
        a_ang = math.degrees(math.atan2(a[1] - s.c[1], a[0] - s.c[0]))
        b_ang = math.degrees(math.atan2(b[1] - s.c[1], b[0] - s.c[0]))
        forward = math.dist(a, s.a) < math.dist(a, s.b)      # walking from the arc's start = CCW
        span = (b_ang - a_ang) % 360 if forward else -((a_ang - b_ang) % 360)
        for k in range(1, steps):
            ang = math.radians(a_ang + span * k / steps)
            pts.append((s.c[0] + s.r * math.cos(ang), s.c[1] + s.r * math.sin(ang)))
    return pts


def view_silhouette(view, tol, scale):
    """Visible drawn lines of a view as segments in view-local mm. The checker fills the region
    they enclose, so the outline comes from the drawing itself, not from the reader's model."""
    out = []
    for sg in view.segs:
        if sg.hidden or sg.center:
            continue
        if sg.kind == "line":
            pts = [sg.a, sg.b]
        else:
            a0, a1 = (sg.a0, sg.a1) if sg.kind == "arc" else (0.0, 360.0)
            span = (a1 - a0) % 360 or 360.0
            k = max(8, int(span / 5))
            pts = [(sg.c[0] + sg.r * math.cos(math.radians(a0 + span * i / k)),
                    sg.c[1] + sg.r * math.sin(math.radians(a0 + span * i / k))) for i in range(k + 1)]
        loc = [(view.local(p)[0] * scale, view.local(p)[1] * scale) for p in pts]
        out += [[round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4)] for a, b in zip(loc, loc[1:])]
    return out


def outline(view, tol):
    """Classify the top-view outline. Returns dict(kind=...) in view-local coordinates."""
    circles = [s for s in view.segs if s.kind == "circle" and not s.hidden and not s.center]
    vb = view.bbox
    for c in circles:
        if abs(2 * c.r - view.w) <= tol and abs(2 * c.r - view.h) <= tol:
            return {"kind": "circle", "center": view.local(c.c), "diameter": 2 * c.r, "seg": c}
    loop = _graph_outer_loop(view.segs, tol)
    if loop is None:
        return {"kind": "rect", "corner": (0.0, 0.0), "size": (view.w, view.h), "approx": True}
    lines = [x for x in loop if x[2].kind == "line"]
    arcs = [x for x in loop if x[2].kind == "arc"]
    pts = [view.local(a) for a, _, _ in loop]
    if not arcs and len(loop) == 4 and all(
            abs(a[0] - b[0]) < tol or abs(a[1] - b[1]) < tol for a, b, _ in loop):
        return {"kind": "rect", "corner": (0.0, 0.0), "size": (view.w, view.h)}
    if len(arcs) == 4 and len(lines) == 4 and len({round(x[2].r / tol) for x in arcs}) == 1:
        return {"kind": "rect", "corner": (0.0, 0.0), "size": (view.w, view.h), "fillet": arcs[0][2].r}
    if not arcs:
        # merge collinear points
        clean = []
        for p in pts:
            if len(clean) >= 2:
                a, b = clean[-2], clean[-1]
                cross = (b[0] - a[0]) * (p[1] - b[1]) - (b[1] - a[1]) * (p[0] - b[0])
                if abs(cross) < tol * tol:
                    clean[-1] = p
                    continue
            clean.append(p)
        if len(clean) > 2:   # also merge across the start point
            a, b, c = clean[-1], clean[0], clean[1]
            if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) < tol * tol:
                clean = clean[1:]
        if len(clean) == 4 and all(abs(clean[i][0] - clean[i - 1][0]) < tol or abs(clean[i][1] - clean[i - 1][1]) < tol
                                   for i in range(4)):
            return {"kind": "rect", "corner": (0.0, 0.0), "size": (view.w, view.h)}
        return {"kind": "polygon", "points": clean}
    return {"kind": "polygon", "points": pts, "approx": True,
            "note": "곡선이 섞인 외곽을 꼭짓점 다각형으로 근사했습니다"}


def _point_in_polygon(p, poly):
    x, y = p
    inside = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


# ----------------------------------------------------------------------------- bands
def _crossings(view, seg, z, tol):
    """u positions where a visible line/arc crosses the horizontal line v=z (view-local)."""
    if seg.kind == "line":
        (u0, v0), (u1, v1) = view.local(seg.a), view.local(seg.b)
        if abs(v1 - v0) < tol or not (min(v0, v1) - tol <= z <= max(v0, v1) + tol):
            return []
        return [u0 + (z - v0) * (u1 - u0) / (v1 - v0)]
    if seg.kind in ("arc", "circle"):
        a0, a1 = (seg.a0, seg.a1) if seg.kind == "arc" else (0.0, 360.0)
        span = (a1 - a0) % 360 or 360.0
        pts = [view.local((seg.c[0] + seg.r * math.cos(math.radians(a0 + span * k / 48)),
                           seg.c[1] + seg.r * math.sin(math.radians(a0 + span * k / 48)))) for k in range(49)]
        out = []
        for (u0, v0), (u1, v1) in zip(pts, pts[1:]):
            if (v0 - z) * (v1 - z) <= 0 and abs(v1 - v0) > 1e-12:
                out.append(u0 + (z - v0) * (u1 - u0) / (v1 - v0))
        return out
    return []


def z_bands(view, axis_u, tol, notes=None):
    """Bands along v (Z) of a front/side view: list of (z0, z1, u0, u1) in view-local units.

    Band levels come from horizontal visible lines; the band width is the outermost visible
    silhouette (lines and arcs) at mid height. A band whose outer edge is curved is reported
    in `notes` because it is extruded as straight-sided.
    """
    vis = [s for s in view.segs if not s.hidden and not s.center and s.kind in ("line", "arc", "circle")]
    lines = [s for s in vis if s.kind == "line"]
    levels = sorted({round(view.local(s.a)[1], 6) for s in lines if abs(s.a[1] - s.b[1]) < tol}
                    | {0.0, round(view.h, 6)})
    merged = []
    for z in levels:
        if not merged or z - merged[-1] > tol:
            merged.append(z)
    bands = []
    for z0, z1 in zip(merged, merged[1:]):
        mid = (z0 + z1) / 2
        us, curved = [], {}
        for sgm in vis:
            slanted = sgm.kind != "line" or abs(sgm.a[0] - sgm.b[0]) > tol   # arc, spline piece, taper
            for u in _crossings(view, sgm, mid, tol):
                us.append(u)
                curved[round(u, 6)] = curved.get(round(u, 6), False) or slanted
        if len(us) < 2:
            continue
        u0, u1 = min(us), max(us)
        if notes is not None and (curved.get(round(u0, 6)) or curved.get(round(u1, 6))):
            notes.append((z0, z1))
        if bands and abs(bands[-1][2] - u0) < tol and abs(bands[-1][3] - u1) < tol:
            bands[-1] = (bands[-1][0], z1, u0, u1)
        else:
            bands.append((z0, z1, u0, u1))
    return bands


# ----------------------------------------------------------------------------- holes
def hole_groups(top, exclude, tol):
    """Concentric top-view circles -> [{center, radii}] (view-local). `exclude`: footprint circles."""
    circles = [s for s in top.segs if s.kind == "circle" and not s.hidden and not s.center
               and not any(s is e for e in exclude)]
    groups = []
    for c in sorted(circles, key=lambda c: c.r):
        p = top.local(c.c)
        for g in groups:
            if math.dist(g["center"], p) < tol:
                g["radii"].append(c.r)
                break
        else:
            groups.append({"center": p, "radii": [c.r]})
    return groups


def hidden_span(view, u_a, u_b, tol):
    """z-span of hidden vertical lines at u_a and u_b in a front/side view (view-local)."""
    spans = {}
    for s in view.segs:
        if s.kind != "line" or not s.hidden or abs(s.a[0] - s.b[0]) > tol:
            continue
        (u, v0), (_, v1) = view.local(s.a), view.local(s.b)
        for target in (u_a, u_b):
            if abs(u - target) < tol:
                lo, hi = spans.get(target, (1e18, -1e18))
                spans[target] = (min(lo, v0, v1), max(hi, v0, v1))
    if len(spans) < 2:
        return None
    (a0, a1), (b0, b1) = spans[u_a], spans[u_b]
    return max(a0, b0), min(a1, b1)


def hidden_span_h(view, v_a, v_b, tol):
    """u-span of hidden horizontal lines at v_a and v_b (view-local), or None."""
    spans = {}
    for s in view.segs:
        if s.kind != "line" or not s.hidden or abs(s.a[1] - s.b[1]) > tol:
            continue
        (u0, v), (u1, _) = view.local(s.a), view.local(s.b)
        for target in (v_a, v_b):
            if abs(v - target) < tol * 10:
                lo, hi = spans.get(target, (1e18, -1e18))
                spans[target] = (min(lo, u0, u1), max(hi, u0, u1))
    if len(spans) < 2:
        return None
    (a0, a1), (b0, b1) = spans[v_a], spans[v_b]
    return max(a0, b0), min(a1, b1)


def side_hole_groups(view, tol):
    """Concentric visible circles in a front / side view (holes drilled from the side)."""
    circles = [s for s in view.segs if s.kind == "circle" and not s.hidden and not s.center]
    groups = []
    for c in sorted(circles, key=lambda c: c.r):
        p = view.local(c.c)
        for g in groups:
            if math.dist(g["center"], p) < tol * 10:
                g["radii"].append(c.r)
                break
        else:
            groups.append({"center": p, "radii": [c.r]})
    return groups


THREAD = re.compile(r"(?:(\d+)\s*[-xX×]\s*)?M\s*(\d+(?:\.\d+)?)(?:\s*[xX×]\s*(\d+(?:\.\d+)?))?")
COARSE_PITCH = {1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75,
                14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5, 22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5, 36: 4.0}


def thread_labels(dims, texts):
    """'4-M6', 'M8x1' in dimension texts / notes -> [{size, pitch, points, dim, text}]."""
    out = []
    for d in dims:
        m = THREAD.search((d.get("text") or "").replace("%%c", ""))
        if m:
            out.append({"size": float(m.group(2)), "pitch": float(m.group(3)) if m.group(3) else None,
                        "points": [p for p in d["points"] if p], "dim": d, "text": d["text"],
                        "count": int(m.group(1)) if m.group(1) else 1})
    for t in texts:
        m = THREAD.search(t["text"])
        if m:
            out.append({"size": float(m.group(2)), "pitch": float(m.group(3)) if m.group(3) else None,
                        "points": [t["pos"]], "dim": None, "text": t["text"],
                        "count": int(m.group(1)) if m.group(1) else 1})
    for lab in out:
        if lab["pitch"] is None:
            lab["pitch"] = COARSE_PITCH.get(lab["size"]) or COARSE_PITCH.get(int(lab["size"]))
        lab["used"] = False
    return [lab for lab in out if lab["pitch"]]


# ----------------------------------------------------------------------------- spec
def _label(text, value):
    """Human-readable dimension label: '4-%%c<>' -> '4-Ø6.6'."""
    t = text or "<>"
    t = t.replace("%%c", "Ø").replace("%%C", "Ø").replace("%%d", "°").replace("%%p", "±")
    return t.replace("<>", "%g" % round(value, 4))


def _r(v, nd=4):
    v = round(float(v), nd)
    return int(v) if v.is_integer() else v


def load_source(path):
    """Drawing file -> primitives. DXF directly; vector PDF through pdf_reader (already in mm)."""
    if path.lower().endswith(".pdf"):
        from . import pdf_reader
        p = pdf_reader.load(path)
        return {"kind": "pdf", "segs": p["segs"], "texts": p["texts"], "dims": p["dims"], "raw_dims": None,
                "units": 4, "scale": 1.0, "hatch_polys": p["hatch_polys"], "issues": p["issues"],
                "pdf_scale_mm_per_pt": p["scale_mm_per_pt"]}
    doc, segs, texts, raw_dims, units = load(path)
    from .section_reader import hatch_polygons
    return {"kind": "dxf", "segs": segs, "texts": texts, "dims": None, "raw_dims": raw_dims, "units": units,
            "scale": None, "hatch_polys": hatch_polygons(doc), "issues": []}


def source_dims(src, tol):
    """Parsed dimensions + mm per drawing unit for a loaded source."""
    if src["dims"] is not None:
        return [dict(d) for d in src["dims"]], src["scale"]
    dims = parse_dims(src["raw_dims"], tol)
    return dims, drawing_scale(dims, src["units"])[0]


def _try_turned_section(path, name, src, views, segs, texts, title_texts, tol, report, issue):
    """Hatched half-section of a turned part -> revolve spec, or None (not a turned section)."""
    from . import section_reader
    dims, scale = source_dims(src, tol)
    found = []
    try:
        res = section_reader.read(views, segs, src["hatch_polys"], dims, texts, scale, tol,
                                  lambda c, s, m, **kw: found.append(dict(code=c, severity=s, message=m, **kw)))
    except section_reader.NotTurned as e:
        report["section_reader"] = "not used: %s" % e
        return None
    report["issues"].extend(found)
    issue("E208", "INFO", "단면도(해칭)를 회전체 반단면으로 읽었습니다 — 축 X, 기준면 X=0")
    report["projection"] = "section"
    report["section_profile_mm"] = res.pop("profile_mm")
    sil = res.pop("silhouette", None)
    if sil:
        report["silhouettes"] = {"front": {"axes": "XZ", "polygon": sil}}
    regions = res.pop("section_regions", None)
    if regions:
        report["section_regions"] = {"plane": "XZ", "offset": 0, "polygons": regions}
    report["scale_mm_per_unit"] = scale
    for d in dims:
        report["dims"].append({"type": d["type"], "text": d["text"], "measured_mm": round(d["measured"] * scale, 4),
                               "shown": d["shown"]})
    material = None
    for t in texts + title_texts:
        m = MATERIAL.search(t["text"])
        if m:
            material = m.group(1)
            break
    report["summary"] = {"views": ["section"], "kind": "turned", "profile_points": len(report["section_profile_mm"]),
                         "dims_total": len(dims), "dims_used": len(dims) - sum(1 for n in res["notes"] if n["text"].startswith("미사용"))}
    return {
        "schema_version": "0.1",
        "part": {"name": name, "material": material, "note": res.pop("note"),
                 "drawing": {"file": os.path.basename(path), "units": "mm", "projection": "section",
                             "scale_mm_per_unit": scale, "reader": "dxf section_reader (turned part)"}},
        "parameters": res["parameters"], "features": res["features"], "checks": res["checks"], "notes": res["notes"],
    }


def extract(path, name=None, tol=None):
    """Read a DXF drawing. Returns (spec, report)."""
    name = name or os.path.splitext(os.path.basename(path))[0]
    report = {"file": os.path.abspath(path), "issues": [], "views": {}, "dims": []}

    def issue(code, sev, msg, **kw):
        report["issues"].append(dict(code=code, severity=sev, message=msg, **kw))

    src = load_source(path)
    segs, texts, units = src["segs"], src["texts"], src["units"]
    report["source"] = src["kind"]
    for code, sev, msg in src.get("issues", []):
        issue(code, sev, msg)

    segs, texts, title_texts = clean_sheet(segs, texts)
    if not segs:
        raise ExtractError("E102", "도면에서 형상을 찾지 못했습니다")
    ext = segs[0].bbox()
    for s in segs[1:]:
        ext = _union(ext, s.bbox())
    size = max(ext[2] - ext[0], ext[3] - ext[1])
    tol = tol or max(1e-4, size * 1e-5)
    if src["kind"] == "pdf":
        tol = max(tol, 0.02)          # PDF coordinates are rounded (~0.01 pt) by the exporter
    view_gap_tol = max(0.5, size * 0.002)

    views = split_views(segs, view_gap_tol)
    iso_views = [v for v in views if is_isometric(v)]
    if iso_views:
        views = [v for v in views if v not in iso_views]
        # fragments of the pictorial view (separate clusters) lie inside its neighbourhood
        iso_box = iso_views[0].bbox
        for v in iso_views[1:]:
            iso_box = _union(iso_box, v.bbox)
        pad = max(iso_box[2] - iso_box[0], iso_box[3] - iso_box[1]) * 0.6
        grown = (iso_box[0] - pad, iso_box[1] - pad, iso_box[2] + pad, iso_box[3] + pad)
        views = [v for v in views if not _inside(v.bbox, grown) or max(v.w, v.h) > 2 * pad]
        issue("E207", "INFO", "등각도(입체도) %d개는 참고용이라 해석에서 뺐습니다" % len(iso_views))
    hatches = src["hatch_polys"]
    if hatches:
        turned = _try_turned_section(path, name, src, views, segs, texts, title_texts, tol, report, issue)
        if turned is not None:
            return turned, report
    if hatches:
        issue("E208", "BLOCK", "단면도(해칭 영역 %d개)가 있습니다. 단면도 해석은 아직 지원하지 않아 형상이 틀릴 수 있습니다 — "
              "외형 투상도만으로 맞는지 확인해 주세요" % len(hatches),
              question={"text": "단면도를 무시하고 외형 투상도만으로 모델링할까요?",
                        "options": ["예, 진행", "아니오, 중단"]})
    named, projection, vissues = identify_views(views, texts + title_texts, max(tol * 10, size * 1e-4))
    for code, sev, msg in vissues:
        issue(code, sev, msg)
    if not named:
        why = [i["message"] for i in report["issues"]]
        raise ExtractError("E102", "뷰를 판별하지 못했습니다 (뷰 후보 %d개)%s" % (
            len(views), "".join("\n   · " + w for w in why)))
    report["projection"] = projection
    unused = [v for v in views if all(v is not u for u in named.values())]
    if unused:
        issue("E207", "INFO", "정렬되지 않은 뷰 %d개는 해석에서 뺐습니다 (등각도·상세도·단면도 등)" % len(unused))
    for k, v in named.items():
        report["views"][k] = {"bbox": [round(x, 3) for x in v.bbox], "segments": len(v.segs)}

    dims, scale = source_dims(src, tol)
    assign_dims(dims, named, tol)
    report["scale_mm_per_unit"] = scale
    if units not in INSUNITS_TO_MM:
        issue("E106", "WARN", "도면 단위($INSUNITS=%s)를 알 수 없어 mm로 가정했습니다" % units)

    # cross-check every dimension: text vs drawn geometry
    conflicts = []
    for d in dims:
        real = d["measured"] * scale
        entry = {"view": d["view"], "type": d["type"], "text": d["text"], "measured_mm": round(real, 4),
                 "shown": d["shown"]}
        if d["shown"] is not None and abs(d["shown"] - real) > max(0.01, real * 1e-4):
            if abs(d["shown"] - real) <= max(0.05, real * 0.003):
                # real drawings are often drawn slightly off: a tiny gap is a drafting error, text wins
                entry["small_diff"] = True
                d["small_diff"] = True
                issue("E107", "INFO", "치수 '%s': 그려진 길이 %.3f와 %.3f mm 차이 — 작은 작도 오차로 보고 치수 문자 값 %g을 씁니다"
                      % (_label(d["text"], real), real, abs(d["shown"] - real), d["shown"]))
            else:
                entry["conflict"] = True
                d["conflict"] = True
                conflicts.append(d)
        report["dims"].append(entry)

    front, top, side = named.get("front"), named.get("top"), named.get("side")
    report["silhouettes"] = {}
    for vname, axes in (("front", "XZ"), ("top", "XY"), ("side", "YZ")):
        v = named.get(vname)
        segs_mm = view_silhouette(v, tol, scale) if v is not None else None
        if segs_mm:
            report["silhouettes"][vname] = {"axes": axes, "segments": segs_mm}

    # ---- overall size
    L = (top.w if top else front.w) * scale
    W = (top.h if top else (side.w if side else None))
    W = W * scale if W is not None else None
    H = (front.h if front else (side.h if side else None))
    H = H * scale if H is not None else None
    if front and top and abs(front.w - top.w) > view_gap_tol:
        issue("E204", "BLOCK", "정면도 폭 %.3f ≠ 평면도 폭 %.3f" % (front.w * scale, top.w * scale))
    if side and top and abs(side.w - top.h) > view_gap_tol:
        issue("E204", "BLOCK", "측면도 폭 %.3f ≠ 평면도 깊이 %.3f" % (side.w * scale, top.h * scale))
    thickness_note = None
    for t in texts:
        m = THICKNESS.search(t["text"])
        if m:
            thickness_note = (float(m.group(1)), t["text"])
            break
    if H is None and thickness_note:
        H = thickness_note[0]

    material = None
    for t in texts + title_texts:
        m = MATERIAL.search(t["text"])
        if m:
            material = m.group(1)
            break

    # ---- parameters with dimension sources
    params = {}
    used_dims = set()

    def dim_for(value, views_ok, kinds=("linear", "aligned")):
        """The first unused dimension whose drawn length equals value (any view as fallback)."""
        for pass_views in (views_ok, None):
            for d in dims:
                if id(d) in used_dims or d["type"] not in kinds:
                    continue
                if pass_views is not None and d.get("view") not in pass_views:
                    continue
                if abs(d["measured"] * scale - value) <= max(0.01, value * 1e-4):
                    used_dims.add(id(d))
                    return d
        return None

    def add_param(pname, value, views_ok, kinds=("linear", "aligned"), what=""):
        name = _add_param(pname, value, views_ok, kinds, what)
        d = last_dim.get(name)
        if d is not None and params[name]["source"]["type"] == "drawing":
            params[name].update(tolerance.parse(d["text"], params[name]["value"]))
        return name

    last_dim = {}

    def _add_param(pname, value, views_ok, kinds=("linear", "aligned"), what=""):
        d = dim_for(value, views_ok, kinds)
        last_dim[pname] = d
        if d is None:
            params[pname] = {"value": _r(value), "confidence": 0.9,
                             "source": {"type": "inferred", "reason": "도면에 치수가 없어 그려진 형상에서 잰 값 (%s)" % what}}
        elif d.get("conflict"):
            params[pname] = {"value": _r(value), "source": {
                "type": "conflict", "view": d["view"], "label": _label(d["text"], value), "drawn": _r(value),
                "shown": d["shown"]}}
        elif d["shown"] is not None and abs(d["shown"] - value) <= max(0.01, abs(value) * 1e-4):
            # the written value is exact; the drawn length carries float / plotting noise
            params[pname] = {"value": _r(d["shown"]), "source": {"type": "drawing", "view": d["view"],
                                                                  "label": _label(d["text"], value), "confidence": 1.0}}
        elif d.get("small_diff"):
            params[pname] = {"value": _r(d["shown"]), "source": {"type": "drawing", "view": d["view"],
                                                                  "label": _label(d["text"], value), "confidence": 1.0,
                                                                  "drawn": _r(value)}}
        else:
            params[pname] = {"value": _r(value), "source": {"type": "drawing", "view": d["view"],
                                                             "label": _label(d["text"], value), "confidence": 1.0}}
        return pname

    add_param("L", L, ("front", "top"), what="X 전체 길이")
    if W is not None:
        add_param("W", W, ("top", "side"), what="Y 전체 길이")
    if H is not None:
        if thickness_note and not front and not side:
            params["H"] = {"value": _r(H), "source": {"type": "drawing", "view": "note",
                                                      "label": thickness_note[1], "confidence": 0.95}}
        else:
            add_param("H", H, ("front", "side"), what="Z 전체 높이")
    else:
        params["H"] = {"value": None, "source": {"type": "unresolved",
                                                 "reason": "정면도/측면도와 두께 주기(t=...)가 없어 높이를 알 수 없음"}}

    # conflicting dims that did not land on a parameter still block the build
    for d in conflicts:
        if id(d) not in used_dims:
            issue("E107", "BLOCK", "치수 문자 '%s'(%s)와 그려진 길이 %.3f mm가 다릅니다"
                  % (d["text"], d["view"], d["measured"] * scale))

    # ---- footprint (top view) and Z bands
    out = outline(top, tol) if top else {"kind": "rect", "corner": (0, 0), "size": (L / scale, (W or 0) / scale)}
    if out.get("approx"):
        issue("E207", "WARN", out.get("note", "평면도 외곽을 찾지 못해 사각형으로 가정했습니다"))
    bands = []
    curved_bands = []
    if front:
        bands = z_bands(front, "x", tol, curved_bands)
    for z0, z1 in curved_bands:
        issue("E207", "WARN", "정면도 높이 %.3f~%.3f 구간의 바깥선이 곡선/경사입니다 — 곧은 벽으로 근사했습니다 (평면도 원으로 맞으면 원기둥)"
              % (z0 * scale, z1 * scale))
    if not bands:
        bands = [(0.0, (H or 0) / scale, 0.0, L / scale)]
    side_bands = z_bands(side, "y", tol) if side else []

    features, checks, notes = [], [], []
    band_params = []
    features_vol = 0.0
    vol_ok = True
    band_tops = []   # (footprint-test fn, z_top_mm)
    footprint_circles = [out["seg"]] if out.get("kind") == "circle" else []
    for bi, (z0, z1, u0, u1) in enumerate(bands):
        zb0, zb1 = z0 * scale, z1 * scale
        full_x = abs(u0) < tol * 10 and abs(u1 - (front.w if front else L / scale)) < tol * 10
        yb = None
        for (sz0, sz1, v0, v1) in side_bands:
            if sz0 - tol <= (z0 + z1) / 2 <= sz1 + tol:
                yb = (v0 * scale, v1 * scale)
        full_y = yb is None or (abs(yb[0]) < 0.01 and W is not None and abs(yb[1] - W) < 0.01)
        sid = "sk_base" if bi == 0 else "sk_band%d" % bi
        hid = "H" if len(bands) == 1 else None
        height = zb1 - zb0
        if len(bands) > 1:
            hid = add_param("h%d" % (bi + 1), height, ("front", "side"), what="%d번째 층 높이" % (bi + 1))
            band_params.append(hid)
        sketch = {"id": sid, "type": "sketch", "plane": "XY", "entities": []}
        if bi > 0:
            sketch["offset"] = _r(zb0)
        fillet = None
        if full_x and full_y and top:
            kind = out["kind"]
            if kind == "circle":
                c = out["center"]
                cx, cy = c[0] * scale, c[1] * scale
                sketch["entities"].append({"kind": "circle", "center": [_r(cx), _r(cy)], "diameter": "L"})
                area = math.pi * (out["diameter"] * scale / 2) ** 2
                inside = (lambda cx, cy, r: (lambda p: math.dist(p, (cx, cy)) <= r + 1e-6))(cx, cy, out["diameter"] * scale / 2)
            elif kind == "rect":
                sketch["entities"].append({"kind": "rect", "corner": [0, 0], "size": ["L", "W"]})
                area = L * W
                inside = (lambda L_, W_: (lambda p: -1e-6 <= p[0] <= L_ + 1e-6 and -1e-6 <= p[1] <= W_ + 1e-6))(L, W)
                if out.get("fillet"):
                    fillet = add_param("r_corner", out["fillet"] * scale, ("top",), ("radius",), "모서리 R")
                    rr = out["fillet"] * scale
                    area -= 4 * (rr * rr - math.pi * rr * rr / 4)
            else:
                pts = [[_r(p[0] * scale), _r(p[1] * scale)] for p in out["points"]]
                sketch["entities"].append({"kind": "polyline", "closed": True, "points": pts})
                area = 0.5 * abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                                     for i in range(len(pts))))
                if out.get("approx"):
                    vol_ok = False
                poly = [tuple(p) for p in pts]
                inside = (lambda poly_: (lambda p: _point_in_polygon(p, poly_)))(poly)
        else:
            x0, x1 = u0 * scale, u1 * scale
            y0, y1 = yb if yb else (0.0, W or 0.0)
            circ = None
            if top:
                # lower bands are hidden under upper ones in the top view: dashed circles count too
                for s in sorted(top.segs, key=lambda q: q.hidden):
                    if s.kind == "circle" and not s.center:
                        c = top.local(s.c)
                        side_ok = yb is None or abs((yb[1] - yb[0]) - 2 * s.r * scale) < 0.01
                        if side_ok and abs(2 * s.r * scale - (x1 - x0)) < 0.01 and \
                                abs(c[0] * scale - (x0 + x1) / 2) < 0.01:
                            circ = (c[0] * scale, c[1] * scale, 2 * s.r * scale)
                            footprint_circles.append(s)
                            break
            if circ:
                dname = add_param("D%d" % (bi + 1), circ[2], ("top", "front"), ("diameter", "linear"), "%d번째 층 지름" % (bi + 1))
                sketch["entities"].append({"kind": "circle", "center": [_r(circ[0]), _r(circ[1])], "diameter": dname})
                area = math.pi * (circ[2] / 2) ** 2
                inside = (lambda cx, cy, r: (lambda p: math.dist(p, (cx, cy)) <= r + 1e-6))(circ[0], circ[1], circ[2] / 2)
            else:
                bx = add_param("bx%d" % (bi + 1), x1 - x0, ("front", "top"), what="%d번째 층 X 길이" % (bi + 1))
                by = add_param("by%d" % (bi + 1), y1 - y0, ("side", "top"), what="%d번째 층 Y 길이" % (bi + 1)) \
                    if abs((y1 - y0) - (W or 0)) > 0.01 else "W"
                sketch["entities"].append({"kind": "rect", "corner": [_r(x0), _r(y0)], "size": [bx, by]})
                area = (x1 - x0) * (y1 - y0)
                inside = (lambda a, b, c_, d_: (lambda p: a - 1e-6 <= p[0] <= b + 1e-6 and c_ - 1e-6 <= p[1] <= d_ + 1e-6))(x0, x1, y0, y1)
        features.append(sketch)
        eid = "ex_base" if bi == 0 else "ex_band%d" % bi
        features.append({"id": eid, "type": "extrude", "profile": sid, "distance": hid or _r(height),
                         "operation": "new_body" if bi == 0 else "join"})
        if fillet:
            features.append({"id": "f_corners", "type": "fillet",
                             "edges": {"select": "vertical_outer", "of": eid, "expect": 4}, "radius": fillet})
        features_vol += area * height
        band_tops.append((inside, zb1))

    # ---- derived parameters: express undimensioned sizes through dimensioned ones
    def src(n):
        return (params.get(n) or {}).get("source", {}).get("type")
    if out.get("kind") == "circle" and src("W") == "inferred":
        params["W"] = {"value": "L", "source": {"type": "derived", "reason": "원형 외곽이라 W = L"}}
    if band_params:
        inferred = [n for n in band_params if src(n) == "inferred"]
        if src("H") == "inferred" and not inferred:
            params["H"] = {"value": " + ".join(band_params),
                           "source": {"type": "derived", "reason": "층 높이의 합"}}
        elif len(inferred) == 1 and src("H") == "drawing":
            others = [n for n in band_params if n != inferred[0]]
            params[inferred[0]] = {"value": "H - (%s)" % " + ".join(others),
                                   "source": {"type": "derived", "reason": "전체 높이에서 나머지 층을 뺀 값"}}

    # ---- holes
    hole_specs = []
    labels = thread_labels(dims, texts) if top else []
    top_groups = hole_groups(top, footprint_circles, tol) if top else []
    # a callout "4-M6" belongs to the 4 nearest holes whose circle fits M6 (tap drill or nominal)
    thread_of = {}
    for lab in labels:
        drill = lab["size"] - lab["pitch"]
        cands = []
        for gi_, g in enumerate(top_groups):
            dd = 2 * min(g["radii"]) * scale
            if gi_ in thread_of or not (abs(dd - drill) < 0.35 or abs(dd - lab["size"]) < 0.35):
                continue
            c = (g["center"][0] * scale, g["center"][1] * scale)
            near = min((math.dist((top.local(p)[0] * scale, top.local(p)[1] * scale), c) for p in lab["points"]),
                       default=1e9)
            cands.append((near, gi_))
        cands.sort()
        if not cands or (lab["count"] == 1 and cands[0][0] > max(3 * lab["size"], 15)):
            continue
        for _, gi_ in cands[:lab["count"]]:
            thread_of[gi_] = lab
    if top:
        for gidx, g in enumerate(top_groups):
            radii = sorted(g["radii"])
            cx, cy = g["center"][0] * scale, g["center"][1] * scale
            ztop = max([zt for ins, zt in band_tops if ins((cx, cy))], default=None)
            if ztop is None:
                issue("E205", "WARN", "평면도의 원 (%.2f, %.2f)이 형상 밖에 있어 무시했습니다" % (cx, cy))
                continue
            d = 2 * radii[0] * scale
            cb = 2 * radii[-1] * scale if len(radii) > 1 else None
            # tapped hole: an "M6" callout next to it (drawn as tap-drill circle + thin 3/4 arc)
            thread = thread_of.get(gidx)
            if thread:
                thread["used"] = True
                drill = thread["size"] - thread["pitch"]
                if cb is not None and abs(cb - thread["size"]) < 0.35:
                    cb = None                       # the bigger circle is the thread, not a counterbore
                drawn_nominal = abs(d - thread["size"]) < 0.35 and abs(d - drill) >= 0.35
                d = drill
            depth, cb_depth, assumed = "through_all", None, False
            if front:
                u = g["center"][0]
                span = hidden_span(front, u - radii[0], u + radii[0], tol)
                if span is None:
                    assumed = True
                elif cb is None and span[0] * scale > 0.01:
                    depth = _r(ztop - span[0] * scale)
                if cb is not None:
                    sp = hidden_span(front, u - radii[-1], u + radii[-1], tol)
                    if sp is not None:
                        cb_depth = ztop - sp[0] * scale
            else:
                assumed = True
            spec_h = {"center": (cx, cy), "d": d, "cb": cb, "cb_depth": cb_depth,
                      "depth": depth, "ztop": ztop, "assumed": assumed}
            if thread:
                spec_h["thread"] = {"designation": "M%gx%g" % (thread["size"], thread["pitch"]), "size": thread["size"],
                                    "pitch": thread["pitch"], "class": "6H", "modeled": False,
                                    "label": thread["text"], "drawn_nominal": drawn_nominal, "dim": thread["dim"]}
            hole_specs.append(spec_h)
    # group identical holes
    groups = {}
    for h in hole_specs:
        k = (round(h["d"], 4), round(h["cb"] or 0, 4), round(h["cb_depth"] or 0, 4), str(h["depth"]), round(h["ztop"], 4),
             (h.get("thread") or {}).get("designation", ""))
        groups.setdefault(k, []).append(h)
    for gi, (k, hs) in enumerate(sorted(groups.items())):
        h0 = hs[0]
        th = h0.get("thread")
        if th:
            dn = "d%d" % (gi + 1)
            params[dn] = {"value": _r(h0["d"]), "source": {
                "type": "derived", "reason": "%s 탭 드릴 지름 = %g - 피치 %g (도면 '%s')"
                % (th["designation"], th["size"], th["pitch"], _label(th["label"], th["size"]))}}
            if th["dim"] is not None:
                used_dims.add(id(th["dim"]))
            if th["drawn_nominal"]:
                issue("E207", "INFO", "%s: 도면 원이 호칭 지름이라 탭 드릴 Ø%g로 모델링합니다" % (th["designation"], h0["d"]))
        else:
            dn = add_param("d%d" % (gi + 1), h0["d"], ("top", "front"), ("diameter", "linear"), "구멍 지름")
        feat = {"id": "h%d" % (gi + 1), "type": "hole",
                "face": {"plane": "XY", "offset": _r(h0["ztop"]) if abs(h0["ztop"] - (H or 0)) > 1e-6 else "H"},
                "direction": "negative",
                "positions": [[_r(h["center"][0]), _r(h["center"][1])] for h in hs],
                "diameter": dn, "depth": h0["depth"]}
        if h0["cb"]:
            feat["hole_type"] = "counterbore"
            feat["cb_diameter"] = add_param("cb%d" % (gi + 1), h0["cb"], ("top", "front"), ("diameter", "linear"), "카운터보어 지름")
            if h0["cb_depth"] is not None:
                feat["cb_depth"] = add_param("cbh%d" % (gi + 1), h0["cb_depth"], ("front", "top", "side"),
                                             ("linear", "aligned"), "카운터보어 깊이")
                # depth written in the counterbore callout: "C'BORE Ø9.5 DP5.4" / "깊이 5.4" / "↧5.4"
                cb_label = params[feat["cb_diameter"]]["source"].get("label", "")
                m = re.search(r"(?:DP|DEEP|깊이|↧)\s*(\d+(?:\.\d+)?)", cb_label, re.I)
                pk = feat["cb_depth"]
                if m and params[pk]["source"]["type"] == "inferred":
                    shown = float(m.group(1))
                    if abs(shown - h0["cb_depth"]) <= 0.01:
                        params[pk] = {"value": _r(h0["cb_depth"]), "source": {
                            "type": "drawing", "view": params[feat["cb_diameter"]]["source"].get("view"),
                            "label": cb_label, "confidence": 1.0}}
                    else:
                        params[pk] = {"value": _r(h0["cb_depth"]), "source": {
                            "type": "conflict", "view": "top", "label": cb_label,
                            "drawn": _r(h0["cb_depth"]), "shown": shown}}
            else:
                params["cbh%d" % (gi + 1)] = {"value": None, "source": {"type": "unresolved", "reason": "카운터보어 깊이를 찾지 못함"}}
                feat["cb_depth"] = "cbh%d" % (gi + 1)
        else:
            feat["hole_type"] = "simple"
        if th:
            feat["thread"] = {k2: th[k2] for k2 in ("designation", "size", "pitch", "class", "modeled")}
            checks.append({"id": "c_%s_thread" % feat["id"], "kind": "thread", "designation": th["designation"],
                           "expected": len(hs)})
            hole_checks_tol = 0.3   # Fusion resizes a threaded hole to the thread's minor diameter
        else:
            hole_checks_tol = None
        if h0["assumed"]:
            issue("E207", "WARN", "구멍 Ø%.3f: 숨은선이 없어 관통으로 가정했습니다" % h0["d"], where={"feature": feat["id"]})
        features.append(feat)
        n = len(hs)
        checks.append({"id": "c_%s_n" % feat["id"], "kind": "hole_count", "diameter": dn, "expected": n})
        checks.append({"id": "c_%s_p" % feat["id"], "kind": "hole_positions", "diameter": dn,
                       "expected": feat["positions"]})
        if hole_checks_tol:
            checks[-1]["diameter_tol"] = checks[-2]["diameter_tol"] = hole_checks_tol
        depth_mm = h0["ztop"] if h0["depth"] == "through_all" else float(h0["depth"])
        r = h0["d"] / 2
        if h0["cb"]:
            cbr, cbh = h0["cb"] / 2, h0["cb_depth"] or 0
            features_vol -= n * (math.pi * cbr * cbr * cbh + math.pi * r * r * max(depth_mm - cbh, 0))
            checks.append({"id": "c_%s_cb" % feat["id"], "kind": "hole_count", "diameter": feat["cb_diameter"], "expected": n})
        else:
            features_vol -= n * math.pi * r * r * depth_mm

    # ---- holes drilled from the side: circles in the front view (along Y) / side view (along X)
    n_groups = len(groups)
    for axis, view in (("Y", front), ("X", side)):
        if view is None:
            continue
        for g in side_hole_groups(view, tol):
            radii = sorted(g["radii"])
            a, z = g["center"][0] * scale, g["center"][1] * scale      # (X, Z) or (Y, Z)
            r = radii[0] * scale
            extent = W if axis == "Y" else L
            if extent is None or not (0 < z < (H or 0) and 0 < a < ((L if axis == "Y" else W) or 0)):
                issue("E205", "WARN", "%s 원 (%.2f, %.2f)이 형상 밖에 있어 무시했습니다"
                      % ("정면도" if axis == "Y" else "측면도", a, z))
                continue
            # depth: hidden lines of the hole in the other views
            span = None
            if axis == "Y":
                if top is not None:
                    span = hidden_span(top, (a - r) / scale, (a + r) / scale, tol)
                if span is None and side is not None:
                    span = hidden_span_h(side, (z - r) / scale, (z + r) / scale, tol)
            else:
                if top is not None:
                    span = hidden_span_h(top, (a - r) / scale, (a + r) / scale, tol)
                if span is None and front is not None:
                    span = hidden_span_h(front, (z - r) / scale, (z + r) / scale, tol)
            plane = "XZ" if axis == "Y" else "YZ"
            far = "W" if axis == "Y" else "L"
            assumed = span is None
            if span is None:
                s0, s1 = 0.0, extent
            else:
                s0, s1 = span[0] * scale, span[1] * scale
            eps = max(0.01, extent * 1e-4)
            if s0 <= eps and s1 >= extent - eps:
                face_off, direction, depth = 0, "positive", "through_all"
            elif s0 <= eps:
                face_off, direction, depth = 0, "positive", s1
            elif s1 >= extent - eps:
                face_off, direction, depth = far, "negative", extent - s0
            else:
                face_off, direction, depth, assumed = 0, "positive", "through_all", True
            gi = n_groups = n_groups + 1
            d = 2 * r
            dn = add_param("d%d" % gi, d, ("front", "side", "top"), ("diameter", "linear"), "옆 구멍 지름")
            feat = {"id": "h%d" % gi, "type": "hole", "face": {"plane": plane, "offset": face_off},
                    "direction": direction, "positions": [[_r(a), _r(z)]], "diameter": dn, "hole_type": "simple"}
            if depth == "through_all":
                feat["depth"] = "through_all"
                depth_mm = extent
            else:
                pk = add_param("hd%d" % gi, depth, ("front", "top", "side"), ("linear", "aligned"), "옆 구멍 깊이")
                label = params[dn]["source"].get("label", "")
                m = re.search(r"(?:DP|DEEP|깊이|↧)\s*(\d+(?:\.\d+)?)", label, re.I)
                if m and params[pk]["source"]["type"] == "inferred" and abs(float(m.group(1)) - depth) <= 0.01:
                    params[pk] = {"value": _r(depth), "source": {"type": "drawing", "view": params[dn]["source"].get("view"),
                                                                "label": label, "confidence": 1.0}}
                feat["depth"] = pk
                depth_mm = depth
            if len(radii) > 1:
                issue("E207", "WARN", "옆 구멍 Ø%.3f의 동심원(카운터보어?)은 아직 모델링하지 않습니다" % d)
            if assumed:
                issue("E207", "WARN", "옆 구멍 Ø%.3f (%s 방향): 숨은선을 찾지 못해 관통으로 가정했습니다" % (d, axis),
                      where={"feature": feat["id"]})
            features.append(feat)
            checks.append({"id": "c_%s_n" % feat["id"], "kind": "hole_count", "diameter": dn, "expected": 1})
            checks.append({"id": "c_%s_p" % feat["id"], "kind": "hole_positions", "axis": axis, "diameter": dn,
                           "expected": feat["positions"]})
            features_vol -= math.pi * r * r * depth_mm

    checks.insert(0, {"id": "c_bbox", "kind": "bbox", "expected": ["L", "W", "H"]})
    checks.append({"id": "c_vol", "kind": "volume", "expected": _r(features_vol, 3) if vol_ok else None,
                   "note": "도면에서 계산한 부피" if vol_ok else "곡선 외곽 근사로 기록만"})

    unused = [d for d in dims if id(d) not in used_dims]
    for d in unused:
        notes.append({"text": "미사용 치수 %s (%s)" % (_label(d["text"], d["measured"] * scale), d["view"]),
                      "applied": False, "reason": "자동 해석에서 사용하지 않은 치수 - 모델 검증 때 직접 확인"})
    for t in texts:
        notes.append({"text": t["text"], "applied": False, "reason": "도면 주기"})

    spec = {
        "schema_version": "0.1",
        "part": {"name": name, "material": material,
                 "drawing": {"file": os.path.basename(path), "units": "mm", "projection": projection,
                             "scale_mm_per_unit": scale}},
        "parameters": params,
        "features": features,
        "checks": checks,
        "notes": notes,
    }
    if out.get("kind") == "circle" and top:
        spec["part"]["note"] = "원점 = 외곽 바운딩 박스 최소 모서리"
    report["summary"] = {"views": sorted(named), "bands": len(bands), "holes": len(hole_specs),
                         "dims_total": len(dims), "dims_used": len(used_dims), "conflicts": len(conflicts)}
    return spec, report
