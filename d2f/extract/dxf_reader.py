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

from . import annotations
from . import intersect
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
        if t == "TOLERANCE":                      # feature control frame (GD&T)
            texts.append({"text": raw.dxf.get("content", ""), "pos": tuple(raw.dxf.insert)[:2],
                          "layer": raw.dxf.layer, "kind": "fcf"})
            continue
        if t == "INSERT":
            # roughness / datum symbols are often blocks whose values are attributes
            try:
                for att in raw.attribs:
                    if (att.dxf.text or "").strip():
                        texts.append({"text": att.dxf.text, "pos": tuple(att.dxf.insert)[:2],
                                      "layer": raw.dxf.layer, "kind": "attrib"})
            except Exception:
                pass
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
    th = UNIFIED.search(text)
    if th:                                                             # "4X 1/4-20 UNC": value = major Ø (mm)
        lab = _unified(th)
        if lab:
            return lab["size"], lab["count"]
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
    kinds = ("linear", "aligned", "diameter", "radius")
    ratios = [d["shown"] / (d["measured"] * base) for d in dims
              if d["shown"] and d["measured"] > 1e-9 and d["type"] in kinds]
    # "<>" texts display the measurement itself: each is a vote for 1:1 (so a detail view's
    # few overridden texts cannot rescale the whole sheet)
    ratios += [1.0 for d in dims if d["shown"] is None and (d.get("text") or "<>").strip() in ("", "<>")
               and d["type"] in kinds and d["measured"] > 1e-9]
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


def _loop_curved(loop, tol):
    """True when an outer loop has an arc or a slanted line (not representable as straight bands)."""
    if not loop:
        return False
    for a, b, sg in loop:
        if sg.kind == "arc":
            return True
        if abs(a[0] - b[0]) > tol and abs(a[1] - b[1]) > tol:
            return True
    return False


def _parametrize_paths(feats, axes, extents, add_param, params):
    """Path coordinates -> parameter names where the drawing dimensions them.

    A coordinate equal to the part's full size becomes L / W / H; any other coordinate that a
    dimension measures from the origin edge becomes its own parameter (source: that dimension).
    Unmatched values stay numbers."""
    made = {}
    view_of = {"XZ": "front", "XY": "top", "YZ": "side"}
    label = {"front": "정면도", "top": "평면도", "side": "측면도"}

    def name_for(view, axis_i, v):
        if not isinstance(v, (int, float)) or abs(v) < 1e-9:
            return v
        full = axes[view][axis_i]
        if abs(v - extents[full]) <= max(0.01, abs(v) * 1e-4):
            return full
        key = (view, axis_i, round(v, 4))
        if key not in made:
            pname = "%s_%s%d" % (view[0], "uv"[axis_i], 1 + sum(1 for k in made if k[:2] == (view, axis_i)))
            add_param(pname, v, (view,), what="%s 외곽 꼭짓점 좌표" % label[view])
            if (params[pname].get("source") or {}).get("type") == "inferred":
                del params[pname]           # no dimension gives it: keep the plain number
                pname = None
            made[key] = pname
        return made[key] or v

    for f in feats:
        if f.get("type") != "sketch":
            continue
        view = view_of.get(f.get("plane"))
        for ent in f.get("entities", []):
            if ent.get("kind") != "path" or view is None:
                continue
            cur = ent["start"]
            for sg in ent["segments"]:
                if sg["type"] == "arc":
                    # the radius dimension of a drawn arc: record which dimension it came from
                    r = math.dist(cur, sg["center"])
                    rkey = (view, "r", round(r, 4))
                    if rkey not in made:
                        rname = "%s_r%d" % (view[0], 1 + sum(1 for k in made if k[:2] == (view, "r")))
                        add_param(rname, r, (view,), ("radius", "diameter"), "%s 외곽 호 반지름" % label[view])
                        if (params[rname].get("source") or {}).get("type") == "inferred":
                            del params[rname]
                            rname = None
                        made[rkey] = rname
                    if made[rkey]:
                        sg["radius_param"] = made[rkey]
                cur = sg["to"]
            ent["start"] = [name_for(view, i, v) for i, v in enumerate(ent["start"])]
            for sg in ent["segments"]:
                sg["to"] = [name_for(view, i, v) for i, v in enumerate(sg["to"])]
                if sg["type"] == "arc":
                    # a centre on a named coordinate follows it (no new parameter for centres alone)
                    sg["center"] = [made.get((view, i, round(v, 4))) or v if isinstance(v, (int, float)) else v
                                    for i, v in enumerate(sg["center"])]
    return made


def _hole_pattern(points, k, L, W, add_param, params, eps=0.01):
    """Identical holes on a bolt circle or a full rectangular grid -> one hole + a pattern feature.

    Returns {seed, feature, note} or None. Pitches / PCD become parameters only when a drawing
    dimension gives them; otherwise the measured value is written as a number."""
    n = len(points)

    def param_or_number(pname, value, kinds, what):
        add_param(pname, value, ("top",), kinds, what)
        if (params[pname].get("source") or {}).get("type") == "inferred":
            del params[pname]
            return _r(value), False
        return pname, True

    def centre_expr(v, full, name):
        return "%s/2" % name if full and abs(v - full / 2) <= eps else _r(v)

    # ---- bolt circle
    circ = None
    if n >= 3:
        cx = sum(p[0] for p in points) / n
        cy = sum(p[1] for p in points) / n
        rs = [math.dist(p, (cx, cy)) for p in points]
        rm = sum(rs) / n
        angs = sorted(math.degrees(math.atan2(p[1] - cy, p[0] - cx)) % 360 for p in points)
        steps = [(angs[(i + 1) % n] - angs[i]) % 360 for i in range(n)]
        if rm > eps and max(abs(r - rm) for r in rs) <= eps and max(abs(st - 360.0 / n) for st in steps) <= 0.05:
            circ = (cx, cy, rm, angs)
    # ---- rectangular grid
    rect = None
    xs = sorted({round(p[0], 3) for p in points})
    ys = sorted({round(p[1], 3) for p in points})
    if len(xs) * len(ys) == n and (len(xs) > 1 or len(ys) > 1):
        have = {(round(p[0], 3), round(p[1], 3)) for p in points}
        even = lambda v: len(v) < 3 or max(abs((v[i + 1] - v[i]) - (v[1] - v[0])) for i in range(len(v) - 1)) <= eps  # noqa: E731
        if all((x, y) in have for x in xs for y in ys) and even(xs) and even(ys):
            rect = (xs, ys)
    if circ and rect:
        # a square of 4 is both: a P.C.D / circle diameter dimension decides for the bolt circle
        pname = "pcd%d" % k
        name, from_dim = param_or_number(pname, 2 * circ[2], ("diameter",), "볼트 원 지름")
        if not from_dim:
            circ = None
        else:
            params.pop(pname, None)
    if circ:
        cx, cy, rm, angs = circ
        pcd, _ = param_or_number("pcd%d" % k, 2 * rm, ("diameter", "linear"), "볼트 원 지름 (P.C.D)")
        cxe, cye = centre_expr(cx, L, "L"), centre_expr(cy, W, "W")
        a0 = min(angs, key=lambda a: min(a, 360 - a))
        quarter = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}
        q = next((v for a, v in quarter.items() if abs((a0 - a) % 360) < 1e-6 or abs((a0 - a) % 360 - 360) < 1e-6), None)
        if q is not None:
            def off(base, sign):
                if sign == 0:
                    return base
                term = "%s/2" % pcd if isinstance(pcd, str) else _r(rm)
                if isinstance(base, (int, float)) and isinstance(term, (int, float)):
                    return _r(base + sign * term)
                return "%s %s %s" % (base, "+" if sign > 0 else "-", term)
            seed = [off(cxe, q[0]), off(cye, q[1])]
        else:
            seed = [_r(cx + rm * math.cos(math.radians(a0))), _r(cy + rm * math.sin(math.radians(a0)))]
        feat = {"id": "pc%d" % k, "type": "pattern_circ", "axis": "Z", "center": [cxe, cye],
                "count": n, "angle": 360}
        return {"seed": seed, "feature": feat,
                "note": "구멍 %d개를 P.C.D %.4g 원형 배열로 만듭니다 (중심 %.4g, %.4g)" % (n, 2 * rm, cx, cy)}
    if rect:
        xs, ys = rect
        feat = {"id": "pr%d" % k, "type": "pattern_rect"}
        if len(xs) > 1:
            px, _ = param_or_number("px%d" % k, xs[1] - xs[0], ("linear", "aligned"), "구멍 X 간격")
            feat["x"] = {"count": len(xs), "pitch": px, "axis": "X"}
        else:
            feat["x"] = {"count": 1, "pitch": 0, "axis": "X"}
        if len(ys) > 1:
            py, _ = param_or_number("py%d" % k, ys[1] - ys[0], ("linear", "aligned"), "구멍 Y 간격")
            feat["y"] = {"count": len(ys), "pitch": py, "axis": "Y"}
        sx, _ = param_or_number("hx%d" % k, xs[0], ("linear", "aligned"), "첫 구멍 X 위치")
        sy, _ = param_or_number("hy%d" % k, ys[0], ("linear", "aligned"), "첫 구멍 Y 위치")
        return {"seed": [sx, sy], "feature": feat,
                "note": "구멍 %d개를 %d x %d 직선 배열로 만듭니다" % (n, len(xs), len(ys))}
    return None


def _cutting_plane(top, vname, tol):
    """Where a front (side) section cuts: a centre/chain line crossing the whole plan view
    horizontally (vertically). Returns the view-local coordinate in drawing units, or None."""
    if top is None:
        return None
    best = None
    for sg in top.segs:
        if sg.kind != "line" or not sg.center:
            continue
        (u0, v0), (u1, v1) = top.local(sg.a), top.local(sg.b)
        if vname == "front" and abs(v0 - v1) < tol and abs(u1 - u0) >= 0.9 * top.w:
            best = v0
        elif vname == "side" and abs(u0 - u1) < tol and abs(v1 - v0) >= 0.9 * top.h:
            best = u0
    return best


def _symmetric_section(view, polys, tol):
    """Hatch regions mirrored about a centre line that runs the whole length of the view."""
    eps = max(tol * 50, 0.05 * min(view.w, view.h))
    for sg in view.segs:
        if sg.kind != "line" or not sg.center:
            continue
        horiz = abs(sg.a[1] - sg.b[1]) < tol
        vert = abs(sg.a[0] - sg.b[0]) < tol
        if horiz and abs(sg.b[0] - sg.a[0]) >= 0.9 * view.w:
            ax, k = sg.a[1], 1
        elif vert and abs(sg.b[1] - sg.a[1]) >= 0.9 * view.h:
            ax, k = sg.a[0], 0
        else:
            continue
        boxes = []
        for poly in polys:
            cs = [p[k] for p in poly]
            os_ = [p[1 - k] for p in poly]
            boxes.append((min(cs), max(cs), min(os_), max(os_)))
        sides = [b for b in boxes if b[0] >= ax - eps and b[1] > ax + eps]
        if len(polys) < 2 or not sides:
            continue
        mirrored = all(any(abs((2 * ax - b[1]) - o[0]) < eps and abs((2 * ax - b[0]) - o[1]) < eps and
                           abs(b[2] - o[2]) < eps and abs(b[3] - o[3]) < eps for o in boxes) for b in sides)
        if mirrored:
            return True
    return False


def _section_views_to_hidden(named, hatches, tol, issue):
    """Hatched regions inside a named view: the view is a section. Lines on a hatch boundary that
    are not the view's outer outline (hole / pocket walls cut by the plane) become hidden lines.
    Returns the number of hatch regions that lie in no named view."""
    def on_poly(p, poly, eps):
        for i in range(len(poly)):
            a, b = poly[i], poly[(i + 1) % len(poly)]
            L = math.dist(a, b)
            if L < 1e-12:
                continue
            t = max(0.0, min(1.0, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L)))
            if math.dist(p, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)) <= eps:
                return True
        return False
    loose = 0
    for poly in hatches:
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        bb = (min(xs), min(ys), max(xs), max(ys))
        host = next((v for v in named.values() if _inside(bb, v.bbox, tol * 10)), None)
        if host is None:
            loose += 1
            continue
        host.__dict__.setdefault("hatches", []).append(poly)
    for name, v in list(named.items()):
        polys = v.__dict__.get("hatches")
        if not polys:
            continue
        area = sum(abs(0.5 * sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1]
                                 for i in range(len(p)))) for p in polys)
        if _symmetric_section(v, polys, tol) or area < 0.5 * v.w * v.h:
            # hatched both sides of a full-length axis (a turned part the section reader could not
            # read), or only a small broken-out part hatched: do not guess it as a plain view
            loose += len(polys)
            v.__dict__["hatches"] = []
            continue
        eps = max(tol * 10, 1e-3)
        outer = {id(s) for _, _, s in (_graph_outer_loop(v.segs, tol) or [])}
        n = 0
        for sg in v.segs:
            if sg.hidden or sg.center or id(sg) in outer or sg.kind not in ("line", "arc"):
                continue
            pts = [sg.a, sg.b, ((sg.a[0] + sg.b[0]) / 2, (sg.a[1] + sg.b[1]) / 2)] if sg.kind == "line" else \
                [sg.a, sg.b, (sg.c[0] + sg.r * math.cos(math.radians((sg.a0 + ((sg.a1 - sg.a0) % 360) / 2))),
                              sg.c[1] + sg.r * math.sin(math.radians((sg.a0 + ((sg.a1 - sg.a0) % 360) / 2))))]
            if any(all(on_poly(p, poly, eps) for p in pts) for poly in polys):
                sg.hidden = True
                n += 1
        label = {"front": "정면도", "top": "평면도", "side": "측면도"}.get(name, name)
        issue("E207", "INFO", "%s는 단면도(해칭 %d곳)입니다 — 절단면의 구멍·홈 경계선 %d개를 숨은선처럼 읽었습니다"
              % (label, len(polys), n))
    return loose


def _annotate(spec, texts, report):
    """GD&T, roughness, general tolerance, hardness, finish -> structured notes + report."""
    found = annotations.collect(texts)
    if not found:
        return
    notes = spec.setdefault("notes", [])
    by_text = {}
    for a, src_text in found:
        by_text.setdefault(src_text, []).append(a)
    for src_text, items in by_text.items():
        note = next((n for n in notes if n.get("text") == src_text and "annotation" not in n), None)
        entry = {"text": src_text, "applied": False,
                 "reason": "; ".join(annotations.describe(a) for a in items) + " — 모델은 공칭 치수, 가공 요구로 기록",
                 "annotation": items if len(items) > 1 else items[0]}
        if note is not None:
            note.update(entry)
        else:
            notes.append(entry)
    report["annotations"] = [dict(a, text=t) for a, t in found]
    general = next((a for a, _ in found if a["kind"] == "general_tolerance"), None)
    if general:
        # dimensions without their own tolerance follow the general tolerance (recorded, not judged)
        for name, p in (spec.get("parameters") or {}).items():
            src = (p or {}).get("source") or {}
            if src.get("type") != "drawing" or "tolerance" in p or not isinstance(p.get("value"), (int, float)):
                continue
            pm = annotations.iso2768(general["class"], abs(p["value"]))
            if pm is not None:
                p["general_tolerance"] = {"standard": "ISO 2768-%s" % general["class"], "plusminus": pm}


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


DETAIL_FACTORS = (2, 2.5, 4, 5, 10, 20, 0.5, 0.4, 0.25, 0.2, 0.1)     # detail-view scale / sheet scale
THREAD = re.compile(r"(?:(\d+)\s*[-xX×]\s*)?M\s*(\d+(?:\.\d+)?)(?:\s*[xX×]\s*(\d+(?:\.\d+)?))?")
COARSE_PITCH = {1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75,
                14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5, 22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5, 36: 4.0}


# Unified inch threads (ASME B1.1): "1/4-20 UNC", "4X #10-32 UNF-2B", "3/8-16UNC"
UNIFIED = re.compile(r"(?:(\d+)\s*[xX×]\s*)?(#\s*\d{1,2}|\d+\s*/\s*\d+|\d+(?:\s+\d+/\d+)?)\s*-\s*(\d{1,3})\s*"
                     r"(UNC|UNF|UNEF)(?:\s*-\s*([123][AB]))?", re.I)
NUMBER_SIZE_IN = {0: 0.060, 1: 0.073, 2: 0.086, 3: 0.099, 4: 0.112, 5: 0.125, 6: 0.138, 8: 0.164,
                  10: 0.190, 12: 0.216}
# 75 % thread tap drills (inch), the usual shop table
UNIFIED_TAP_DRILL_IN = {
    ("#4", 40): 0.089, ("#5", 40): 0.0995, ("#6", 32): 0.1065, ("#8", 32): 0.136, ("#10", 24): 0.1495,
    ("#10", 32): 0.159, ("#12", 24): 0.177, ("1/4", 20): 0.201, ("1/4", 28): 0.213, ("5/16", 18): 0.257,
    ("5/16", 24): 0.272, ("3/8", 16): 0.3125, ("3/8", 24): 0.332, ("7/16", 14): 0.368, ("7/16", 20): 0.3906,
    ("1/2", 13): 0.4219, ("1/2", 20): 0.4531, ("9/16", 12): 0.4844, ("5/8", 11): 0.5312, ("5/8", 18): 0.5781,
    ("3/4", 10): 0.6562, ("3/4", 16): 0.6875, ("7/8", 9): 0.7656, ("1", 8): 0.875,
}


def _unified(m):
    """Regex match -> thread label fields (mm) or None."""
    size_txt = re.sub(r"\s*/\s*", "/", re.sub(r"\s+", " ", m.group(2).strip())).replace("# ", "#")
    tpi = int(m.group(3))
    series = m.group(4).upper()
    if size_txt.startswith("#"):
        major_in = NUMBER_SIZE_IN.get(int(size_txt[1:]))
    else:
        whole, _, frac = size_txt.partition(" ")
        try:
            if "/" in whole and not frac:
                a, b = whole.split("/")
                major_in = int(a) / int(b)
            else:
                major_in = int(whole) + (int(frac.split("/")[0]) / int(frac.split("/")[1]) if frac else 0)
        except (ValueError, ZeroDivisionError):
            major_in = None
    if not major_in or tpi <= 0:
        return None
    drill_in = UNIFIED_TAP_DRILL_IN.get((size_txt, tpi)) or (major_in - 1.0 / tpi)
    return {"size": round(major_in * 25.4, 4), "pitch": round(25.4 / tpi, 4), "drill": round(drill_in * 25.4, 4),
            "type": "ANSI Unified Screw Threads", "size_label": size_txt,
            "designation": "%s-%d %s" % (size_txt, tpi, series), "class": (m.group(5) or "2B").upper(),
            "count": int(m.group(1)) if m.group(1) else 1}


def _metric(m):
    size = float(m.group(2))
    pitch = float(m.group(3)) if m.group(3) else (COARSE_PITCH.get(size) or COARSE_PITCH.get(int(size)))
    if not pitch:
        return None
    return {"size": size, "pitch": pitch, "drill": round(size - pitch, 4), "type": "ISO Metric profile",
            "size_label": "%g" % size, "designation": "M%gx%g" % (size, pitch), "class": "6H",
            "count": int(m.group(1)) if m.group(1) else 1}


def parse_thread(text):
    """Thread callout in a text -> label fields, or None. Inch (UNC/UNF) is tried first because
    '1/4-20 UNC' must not be read as a metric size."""
    text = (text or "").replace("%%c", "")
    m = UNIFIED.search(text)
    if m:
        return _unified(m)
    m = THREAD.search(text)
    return _metric(m) if m else None


def thread_labels(dims, texts):
    """'4-M6', 'M8x1', '1/4-20 UNC' in dimension texts / notes -> [{size, pitch, drill, designation, ...}]."""
    out = []
    for d in dims:
        lab = parse_thread(d.get("text"))
        if lab:
            lab.update(points=[p for p in d["points"] if p], dim=d, text=d["text"])
            out.append(lab)
    for t in texts:
        lab = parse_thread(t["text"])
        if lab:
            lab.update(points=[t["pos"]], dim=None, text=t["text"])
            out.append(lab)
    for lab in out:
        lab["used"] = False
    return out


# ----------------------------------------------------------------------------- spec
def _label(text, value):
    """Human-readable dimension label: '4-%%c<>' -> '4-Ø6.6'."""
    t = text or "<>"
    t = t.replace("%%c", "Ø").replace("%%C", "Ø").replace("%%d", "°").replace("%%p", "±")
    return t.replace("<>", "%g" % round(value, 4))


def _r(v, nd=4):
    v = round(float(v), nd)
    return int(v) if v.is_integer() else v


def load_source(path, page=None):
    """Drawing file -> primitives. DXF directly; vector PDF through pdf_reader (already in mm).
    page: 1-based PDF page (None = automatic)."""
    if path.lower().endswith(".pdf"):
        from . import pdf_reader
        p = pdf_reader.load(path, None if page is None else int(page) - 1)
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


def extract(path, name=None, tol=None, page=None):
    """Read a DXF / vector PDF drawing. Returns (spec, report). page: 1-based PDF page."""
    name = name or os.path.splitext(os.path.basename(path))[0]
    report = {"file": os.path.abspath(path), "issues": [], "views": {}, "dims": []}

    def issue(code, sev, msg, **kw):
        report["issues"].append(dict(code=code, severity=sev, message=msg, **kw))

    src = load_source(path, page)
    segs, texts, units = src["segs"], src["texts"], src["units"]
    if page is not None and path.lower().endswith(".pdf") and name == os.path.splitext(os.path.basename(path))[0]:
        name = "%s_p%d" % (name, int(page))       # each page of a drawing set gets its own out folder
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
            _annotate(turned, texts + title_texts, report)
            return turned, report
    named, projection, vissues = identify_views(views, texts + title_texts, max(tol * 10, size * 1e-4))
    if hatches:
        # a prismatic part drawn as a section view: its cut edges are what a plain view shows as
        # hidden lines, so turn them into hidden lines and read the view normally
        loose = _section_views_to_hidden(named, hatches, tol, issue)
        if loose:
            issue("E208", "BLOCK", "해석하지 못한 단면도가 있습니다 (해칭 영역 %d개: 부분 단면, 별도 단면도, 또는 읽지 못한 회전체). "
                  "형상이 틀릴 수 있습니다 — 외형 투상도만으로 맞는지 확인해 주세요" % loose,
                  question={"text": "단면도를 무시하고 외형 투상도만으로 모델링할까요?",
                            "options": ["예, 진행", "아니오, 중단"]})
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

    def box_dist(p, v):
        return math.hypot(max(v.bbox[0] - p[0], 0, p[0] - v.bbox[2]), max(v.bbox[1] - p[1], 0, p[1] - v.bbox[3]))
    for d in dims:
        real = d["measured"] * scale
        if d["shown"] and unused and abs(d["shown"] - real) > max(0.05, real * 0.003):
            # a detail view drawn at its own scale ("DETAIL A (2:1)"): its dimensions measure k x
            ratio = real / d["shown"]
            f = next((k for k in DETAIL_FACTORS if abs(ratio / k - 1) < 0.002), None)
            pts = [p for p in d["points"][:3] if p]
            if f and pts:
                near_unused = min(box_dist(p, v) for p in pts for v in unused)
                near_named = min(box_dist(p, v) for p in pts for v in named.values())
                if near_unused < near_named:
                    d["measured"] /= f
                    d["detail_scale"] = f
                    if d.get("view") and d in named[d["view"]].dims:
                        named[d["view"]].dims.remove(d)
                    d["view"] = "detail"
                    real = d["measured"] * scale
                    issue("E207", "INFO", "치수 '%s'는 상세도(축척 %s:%s)에 있어 그 축척으로 읽었습니다"
                          % (_label(d["text"], real), *(("%g" % f, "1") if f >= 1 else ("1", "%g" % (1 / f)))))
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
    # a section view of a prismatic part: its hatched regions are checked against the model cut (E410)
    for vname, plane in (("front", "XZ"), ("side", "YZ")):
        v = named.get(vname)
        polys = (v.__dict__.get("hatches") if v is not None else None) or []
        if polys and "section_regions" not in report:
            cut = _cutting_plane(top, vname, tol)
            if cut is None:          # no cutting-plane line drawn: assume the usual cut through the middle
                other = named.get("side") if vname == "front" else named.get("front")
                span = (top.h if vname == "front" else top.w) if top is not None else (other.w if other is not None else 0)
                mid = span / 2
            report["section_regions"] = {
                "plane": plane, "offset": _r((cut if cut is not None else mid) * scale),
                "polygons": [[[round(v.local(p)[0] * scale, 4), round(v.local(p)[1] * scale, 4)] for p in poly]
                             for poly in polys]}
            if cut is None:
                report["section_regions"]["offset_from"] = "middle"

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
    loops = {k: _graph_outer_loop(v.segs, tol) for k, v in named.items()}
    curved_views = [k for k in ("front", "side") if named.get(k) is not None and _loop_curved(loops.get(k), tol)]
    inter = bool(curved_views) and W is not None and H is not None
    top_path = None
    if top and out.get("approx") and loops.get("top") and not inter:
        # lines + arcs in the plan view: sketch them exactly instead of a vertex polygon
        top_path = intersect.loop_path(top, loops["top"], scale)
        issue("E207", "INFO", "평면도 외곽에 곡선이 있어 선·호를 그대로 스케치합니다")
    elif out.get("approx") and not inter:
        issue("E207", "WARN", out.get("note", "평면도 외곽을 찾지 못해 사각형으로 가정했습니다"))
    bands = []
    curved_bands = []
    if front:
        bands = z_bands(front, "x", tol, curved_bands)
    if inter:
        issue("E207", "INFO", "%s 외곽에 곡선/경사가 있어 투상도 외곽을 각각 돌출해 교차시킨 형상으로 만듭니다"
              % "·".join({"front": "정면도", "side": "측면도"}[k] for k in curved_views))
    else:
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
    band_tops = []   # (footprint-test fn, z_top_mm or fn(point, r) -> z_top_mm)
    footprint_circles = [out["seg"]] if out.get("kind") == "circle" else []
    if inter:
        feats_i, info = intersect.build(named, scale, tol, L, W, H, loops)
        _parametrize_paths(feats_i, {"front": ("L", "H"), "top": ("L", "W"), "side": ("W", "H")},
                           {"L": L, "W": W, "H": H}, add_param, params)
        features += feats_i
        features_vol = info["volume"] or 0.0
        if not info["exact"]:
            notes.append({"text": "부피 기대값은 투상도 외곽 %s의 교차를 격자 적분한 값입니다" % "·".join(info["views"]),
                          "applied": True, "reason": "곡선 외곽 교차 형상"})
        report["intersect"] = {"views": info["views"], "volume": info["volume"], "exact": info["exact"]}
        band_tops.append(intersect.inside_and_top(info["polygons"], L, W, H))
    for bi, (z0, z1, u0, u1) in enumerate(bands if not inter else []):
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
            elif top_path is not None:
                sketch["entities"].append({"kind": "path", "closed": True, "start": top_path["start"],
                                           "segments": top_path["segments"]})
                area = intersect.path_area(top_path)
                poly = [tuple(p) for p in top_path["polygon"]]
                inside = (lambda poly_: (lambda p: _point_in_polygon(p, poly_)))(poly)
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

    if top_path is not None:
        _parametrize_paths([f for f in features if f.get("id") == "sk_base"], {"top": ("L", "W")},
                           {"L": L, "W": W, "H": H}, add_param, params)

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
        drill = lab["drill"]
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
            r_out = radii[-1] * scale
            ztop = max([zt((cx, cy), r_out) if callable(zt) else zt for ins, zt in band_tops if ins((cx, cy))],
                       default=None)
            if ztop is None:
                issue("E205", "WARN", "평면도의 원 (%.2f, %.2f)이 형상 밖에 있어 무시했습니다" % (cx, cy))
                continue
            # material height along the hole axis (for the volume): the surface over the centre
            zvol = max([zt((cx, cy), 0) if callable(zt) else zt for ins, zt in band_tops if ins((cx, cy))])
            d = 2 * radii[0] * scale
            cb = 2 * radii[-1] * scale if len(radii) > 1 else None
            # tapped hole: an "M6" callout next to it (drawn as tap-drill circle + thin 3/4 arc)
            thread = thread_of.get(gidx)
            if thread:
                thread["used"] = True
                drill = thread["drill"]
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
                      "depth": depth, "ztop": ztop, "zvol": zvol, "assumed": assumed}
            if thread:
                spec_h["thread"] = {"designation": thread["designation"], "size": thread["size"],
                                    "pitch": thread["pitch"], "class": thread["class"], "modeled": False,
                                    "type": thread["type"], "size_label": thread["size_label"],
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
            if th["type"].startswith("ISO"):
                why = "%s 탭 드릴 지름 = %g - 피치 %g" % (th["designation"], th["size"], th["pitch"])
            else:
                why = "%s 탭 드릴 지름 %.4g mm (%.4g in, 75%% 나사산 표준 드릴)" % (th["designation"], h0["d"], h0["d"] / 25.4)
            params[dn] = {"value": _r(h0["d"]), "source": {
                "type": "derived", "reason": "%s (도면 '%s')" % (why, _label(th["label"], th["size"]))}}
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
        if isinstance(h0["depth"], (int, float)):
            # blind hole: depth from a drawing dimension when one measures it
            feat["depth"] = add_param("hd%d" % (gi + 1), h0["depth"], ("front", "side"), ("linear", "aligned"), "구멍 깊이")
            dl = params[dn]["source"].get("label", "") if dn in params else ""
            m = re.search(r"(?:DP|DEEP|깊이|↧)\s*(\d+(?:\.\d+)?)", dl, re.I)
            if m and params[feat["depth"]]["source"]["type"] == "inferred" and abs(float(m.group(1)) - h0["depth"]) <= 0.01:
                params[feat["depth"]] = {"value": _r(h0["depth"]), "source": {"type": "drawing", "view": params[dn]["source"].get("view"),
                                                                              "label": dl, "confidence": 1.0}}
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
            if not th["type"].startswith("ISO"):
                feat["thread"].update(type=th["type"], size_label=th["size_label"])
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
        if not th and len(hs) >= 2:
            pat = _hole_pattern([h["center"] for h in hs], gi + 1, L, W, add_param, params)
            if pat:
                feat["positions"] = [pat["seed"]]
                pat["feature"]["features"] = [feat["id"]]
                features.append(pat["feature"])
                notes.append({"text": pat["note"], "applied": True, "reason": "같은 구멍의 규칙 배열"})
        r = h0["d"] / 2
        for h in hs:
            # through: the material under the surface at the hole centre (a sloped top cuts a
            # linear wedge whose mean height is the centre height)
            depth_mm = h["zvol"] if h0["depth"] == "through_all" else float(h0["depth"])
            if h0["cb"]:
                cbr, cbh = h0["cb"] / 2, h0["cb_depth"] or 0
                features_vol -= math.pi * cbr * cbr * cbh + math.pi * r * r * max(depth_mm - cbh, 0)
            else:
                features_vol -= math.pi * r * r * depth_mm
        if h0["cb"]:
            checks.append({"id": "c_%s_cb" % feat["id"], "kind": "hole_count", "diameter": feat["cb_diameter"], "expected": n})

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
            def span_of(rad):
                sp = None
                if axis == "Y":
                    if top is not None:
                        sp = hidden_span(top, (a - rad) / scale, (a + rad) / scale, tol)
                    if sp is None and side is not None:
                        sp = hidden_span_h(side, (z - rad) / scale, (z + rad) / scale, tol)
                else:
                    if top is not None:
                        sp = hidden_span_h(top, (a - rad) / scale, (a + rad) / scale, tol)
                    if sp is None and front is not None:
                        sp = hidden_span_h(front, (z - rad) / scale, (z + rad) / scale, tol)
                return None if sp is None else (sp[0] * scale, sp[1] * scale)
            span = span_of(r)
            plane = "XZ" if axis == "Y" else "YZ"
            far = "W" if axis == "Y" else "L"
            assumed = span is None
            s0, s1 = span if span is not None else (0.0, extent)
            eps = max(0.01, extent * 1e-4)
            # counterbore: the bigger concentric circle, its depth from its own hidden lines
            cb = cb_span = None
            if len(radii) > 1:
                cb = 2 * radii[-1] * scale
                cb_span = span_of(radii[-1] * scale)
            if cb_span is not None and span is not None:
                # inside the counterbore the small hole has no wall, so its hidden lines stop at the
                # counterbore floor: the hole really continues to the counterbore's face
                if cb_span[0] <= eps and abs(cb_span[1] - s0) <= max(eps, 0.05):
                    s0 = 0.0
                if cb_span[1] >= extent - eps and abs(cb_span[0] - s1) <= max(eps, 0.05):
                    s1 = extent
            cb_at_far = cb_span is not None and cb_span[1] >= extent - eps and cb_span[0] > eps
            if s0 <= eps and s1 >= extent - eps:
                face_off, direction, depth = (far, "negative", "through_all") if cb_at_far else (0, "positive", "through_all")
            elif s0 <= eps:
                face_off, direction, depth = 0, "positive", s1
            elif s1 >= extent - eps:
                face_off, direction, depth = far, "negative", extent - s0
            else:
                face_off, direction, depth, assumed = 0, "positive", "through_all", True
            cb_depth = None
            if cb is not None and cb_span is not None:
                cb_depth = (extent - cb_span[0]) if direction == "negative" else cb_span[1]
                if cb_depth <= eps or cb_depth >= extent - eps:
                    cb_depth = None
            gi = n_groups = n_groups + 1
            d = 2 * r
            dn = add_param("d%d" % gi, d, ("front", "side", "top"), ("diameter", "linear"), "옆 구멍 지름")
            def pos_param(pname, value, what):
                # a hole position measured by a drawing dimension becomes a parameter, else a number
                add_param(pname, value, ("front", "side", "top"), what=what)
                if (params[pname].get("source") or {}).get("type") == "inferred":
                    del params[pname]
                    return _r(value)
                return pname
            pa = pos_param("h%d_%s" % (gi, "x" if axis == "Y" else "y"), a, "옆 구멍 위치")
            pz = pos_param("h%d_z" % gi, z, "옆 구멍 높이")
            feat = {"id": "h%d" % gi, "type": "hole", "face": {"plane": plane, "offset": face_off},
                    "direction": direction, "positions": [[pa, pz]], "diameter": dn, "hole_type": "simple"}
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
            if cb is not None and cb_depth is not None:
                feat["hole_type"] = "counterbore"
                feat["cb_diameter"] = add_param("cb%d" % gi, cb, ("front", "side", "top"), ("diameter", "linear"),
                                                "옆 구멍 카운터보어 지름")
                ck = add_param("cbh%d" % gi, cb_depth, ("front", "top", "side"), ("linear", "aligned"), "옆 구멍 카운터보어 깊이")
                cb_label = params[feat["cb_diameter"]]["source"].get("label", "")
                m = re.search(r"(?:DP|DEEP|깊이|↧)\s*(\d+(?:\.\d+)?)", cb_label, re.I)
                if m and params[ck]["source"]["type"] == "inferred" and abs(float(m.group(1)) - cb_depth) <= 0.01:
                    params[ck] = {"value": _r(cb_depth), "source": {"type": "drawing", "view": params[feat["cb_diameter"]]["source"].get("view"),
                                                                   "label": cb_label, "confidence": 1.0}}
                feat["cb_depth"] = ck
            elif cb is not None:
                issue("E207", "WARN", "옆 구멍 Ø%.3f의 동심원 Ø%.3f: 깊이를 알려 주는 숨은선이 없어 카운터보어로 만들지 않았습니다"
                      % (d, cb))
            if assumed:
                issue("E207", "WARN", "옆 구멍 Ø%.3f (%s 방향): 숨은선을 찾지 못해 관통으로 가정했습니다" % (d, axis),
                      where={"feature": feat["id"]})
            features.append(feat)
            checks.append({"id": "c_%s_n" % feat["id"], "kind": "hole_count", "diameter": dn, "expected": 1})
            checks.append({"id": "c_%s_p" % feat["id"], "kind": "hole_positions", "axis": axis, "diameter": dn,
                           "expected": [[_r(a), _r(z)]]})
            if feat["hole_type"] == "counterbore":
                checks.append({"id": "c_%s_cb" % feat["id"], "kind": "hole_count", "diameter": feat["cb_diameter"],
                               "expected": 1})
                features_vol -= math.pi * (cb / 2) ** 2 * cb_depth + math.pi * r * r * max(depth_mm - cb_depth, 0)
            else:
                features_vol -= math.pi * r * r * depth_mm

    checks.insert(0, {"id": "c_bbox", "kind": "bbox", "expected": ["L", "W", "H"]})
    checks.append({"id": "c_vol", "kind": "volume", "expected": _r(features_vol, 3) if vol_ok else None,
                   "note": "도면에서 계산한 부피" if vol_ok else "곡선 외곽 근사로 기록만"})

    unused = [d for d in dims if id(d) not in used_dims]
    for d in unused:
        notes.append({"text": "미사용 치수 %s (%s)" % (_label(d["text"], d["measured"] * scale), d["view"]),
                      "applied": False, "reason": "자동 해석에서 사용하지 않은 치수 - 모델 검증 때 직접 확인"})
    for t in texts:
        if t.get("kind") != "fcf":
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
    _annotate(spec, texts + title_texts, report)
    return spec, report
