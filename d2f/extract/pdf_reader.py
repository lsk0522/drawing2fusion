"""Vector PDF drawing -> the same primitives the DXF reader works on.

A CAD program's PDF export keeps the drawing as vectors, but without any CAD meaning:
no layers, no DIMENSION objects, no line types - just strokes, fills and text. This module
rebuilds that meaning:

  * line kinds   - dash pattern (PDF dash array, or a line broken into short dashes)
                   -> hidden (equal dashes) / centre (long-short); stroke width -> visible
                   outline (thickest class) vs thin annotation
  * curves       - Bezier chains / fine polylines fitted to circles and arcs
  * dimensions   - filled arrowheads paired back to back along a line -> measured length;
                   the nearest number text along that line -> the dimension value;
                   an arrow ending on a circle + "Ø"/"R" text -> diameter / radius
  * scale        - dimension values / measured lengths (1:1, 1:2 ... detected; PDF unit = pt)
  * hatching     - many parallel thin slanted lines -> section hatch; the hatched regions are
                   the faces of the outline that contain them
Output: Seg list, texts, parsed dimensions and hatch polygons, all in mm, plus issues.
"""

import math
import re

from . import dxf_reader as R

PT_MM = 25.4 / 72.0
NUM = re.compile(r"\d+(?:\.\d+)?")


def _import_pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz as pymupdf      # older PyMuPDF
        if hasattr(pymupdf, "open"):
            return pymupdf
    except ImportError:
        pass
    return None


def ensure_pymupdf(auto_install=True):
    m = _import_pymupdf()
    if m is not None or not auto_install:
        return m
    import importlib
    import subprocess
    import sys
    print("ℹ️ PDF를 읽으려면 PyMuPDF 패키지가 필요해서 설치합니다 (한 번만) ...")
    for args in ([sys.executable, "-m", "pip", "install", "--user", "pymupdf"],
                 [sys.executable, "-m", "pip", "install", "pymupdf"]):
        try:
            subprocess.check_call(args)
        except Exception:
            continue
        import site
        try:
            site.addsitedir(site.getusersitepackages())
        except Exception:
            pass
        importlib.invalidate_caches()
        m = _import_pymupdf()
        if m is not None:
            return m
    return None


# ----------------------------------------------------------------------------- geometry helpers
def _fit_circle(pts):
    """Algebraic least-squares circle -> (cx, cy, r, max_residual) or None."""
    n = len(pts)
    if n < 4:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    suu = suv = svv = suuu = svvv = suvv = svuu = 0.0
    for x, y in pts:
        u, v = x - mx, y - my
        suu += u * u
        suv += u * v
        svv += v * v
        suuu += u * u * u
        svvv += v * v * v
        suvv += u * v * v
        svuu += v * u * u
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return None
    b1 = 0.5 * (suuu + suvv)
    b2 = 0.5 * (svvv + svuu)
    uc = (b1 * svv - b2 * suv) / det
    vc = (suu * b2 - suv * b1) / det
    r = math.sqrt(uc * uc + vc * vc + (suu + svv) / n)
    cx, cy = uc + mx, vc + my
    res = max(abs(math.dist(p, (cx, cy)) - r) for p in pts)
    return cx, cy, r, res


def _bezier(p0, p1, p2, p3, t):
    a = (1 - t) ** 3
    b = 3 * (1 - t) ** 2 * t
    c = 3 * (1 - t) * t * t
    d = t ** 3
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _signed_area(pts):
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                     for i in range(len(pts)))


def _curve_from_points(pts, tol):
    """Fitted circle/arc for a chain of points, else None."""
    fit = _fit_circle(pts)
    if not fit:
        return None
    cx, cy, r, res = fit
    if r <= 0 or res > max(0.02 * r, tol) or r > 1e5:
        return None
    angs = [math.degrees(math.atan2(y - cy, x - cx)) for x, y in pts]
    # accumulated sweep along the chain
    sweep = 0.0
    for a, b in zip(angs, angs[1:]):
        d = (b - a + 180) % 360 - 180
        sweep += d
    closed = math.dist(pts[0], pts[-1]) < max(tol * 2, r * 0.01) and abs(abs(sweep) - 360) < 10
    if closed:
        return ("circle", (cx, cy), r, 0.0, 360.0)
    if abs(sweep) < 5:
        return None
    if sweep > 0:
        return ("arc", (cx, cy), r, angs[0], angs[0] + sweep)
    return ("arc", (cx, cy), r, angs[-1], angs[-1] - sweep)


def _curve_from_beziers(chain, samples, tol):
    """Circle/arc from Bezier pieces. The centre comes from the on-curve end points (exact for a
    CAD export), or from the end tangents for a single piece; samples only validate the fit
    (a cubic Bezier quarter circle bulges by ~0.03 %, which would bias a least-squares fit)."""
    on = [chain[0][0]] + [c[3] for c in chain]
    centre = None
    if len(on) >= 3 and abs(_signed_area(on[:3])) > 1e-6:
        fit = _fit_circle(on) if len(on) >= 4 else _circle3(*on[:3])
        if fit:
            centre = fit[:3]
    if centre is None:
        p0, p1, p2, p3 = chain[0]
        t0 = (p1[0] - p0[0], p1[1] - p0[1])
        t1 = (p3[0] - p2[0], p3[1] - p2[1])
        # normals: p0 + s * (-t0y, t0x) and p3 + u * (-t1y, t1x)
        a, b = (-t0[1], t0[0]), (-t1[1], t1[0])
        det = a[0] * (-b[1]) - a[1] * (-b[0])
        if abs(det) < 1e-9:
            return None
        rx, ry = p3[0] - p0[0], p3[1] - p0[1]
        sp = (rx * (-b[1]) - ry * (-b[0])) / det
        c = (p0[0] + sp * a[0], p0[1] + sp * a[1])
        centre = (c[0], c[1], (math.dist(c, p0) + math.dist(c, p3)) / 2)
    cx, cy, r = centre
    if r <= 0 or r > 1e5 or max(abs(math.dist(p, (cx, cy)) - r) for p in samples) > max(0.004 * r, tol):
        return None
    return _classify_arc(samples, cx, cy, r, tol)


def _circle3(a, b, c):
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    ux = ((a[0] ** 2 + a[1] ** 2) * (b[1] - c[1]) + (b[0] ** 2 + b[1] ** 2) * (c[1] - a[1]) + (c[0] ** 2 + c[1] ** 2) * (a[1] - b[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (c[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (a[0] - c[0]) + (c[0] ** 2 + c[1] ** 2) * (b[0] - a[0])) / d
    return ux, uy, math.dist((ux, uy), a), 0.0


def _classify_arc(pts, cx, cy, r, tol):
    angs = [math.degrees(math.atan2(y - cy, x - cx)) for x, y in pts]
    sweep = 0.0
    for a, b in zip(angs, angs[1:]):
        sweep += (b - a + 180) % 360 - 180
    closed = math.dist(pts[0], pts[-1]) < max(tol * 2, r * 0.01) and abs(abs(sweep) - 360) < 10
    if closed:
        return ("circle", (cx, cy), r, 0.0, 360.0)
    if abs(sweep) < 5:
        return None
    if sweep > 0:
        return ("arc", (cx, cy), r, angs[0], angs[0] + sweep)
    return ("arc", (cx, cy), r, angs[-1], angs[-1] - sweep)


class Stroke:
    __slots__ = ("kind", "a", "b", "c", "r", "a0", "a1", "width", "dash", "lengths")

    def __init__(self, kind, width, dash, a=None, b=None, c=None, r=0.0, a0=0.0, a1=0.0, lengths=None):
        self.kind, self.width, self.dash = kind, width, dash
        self.a, self.b, self.c, self.r, self.a0, self.a1 = a, b, c, r, a0, a1
        self.lengths = lengths


def _dash_kind(pattern):
    vals = [float(v) for v in NUM.findall(pattern.split("]")[0])] if pattern else []
    vals = [v for v in vals if v > 0]
    if len(vals) < 2:
        return None
    on = vals[0::2]
    return "center" if max(on) > 2.2 * min(on) or len(set(round(v, 2) for v in on)) > 1 else "hidden"


def _merge_dashes(lines, tol):
    """Short collinear pieces of one path (a line drawn as dashes) -> one dashed stroke."""
    if len(lines) < 2:
        return None
    a0, b0 = lines[0]
    ang = math.atan2(b0[1] - a0[1], b0[0] - a0[0])
    ux, uy = math.cos(ang), math.sin(ang)
    for a, b in lines:
        for p in (a, b):
            if abs((p[0] - a0[0]) * uy - (p[1] - a0[1]) * ux) > tol:
                return None
    ts = sorted((min((a[0] - a0[0]) * ux + (a[1] - a0[1]) * uy, (b[0] - a0[0]) * ux + (b[1] - a0[1]) * uy),
                 max((a[0] - a0[0]) * ux + (a[1] - a0[1]) * uy, (b[0] - a0[0]) * ux + (b[1] - a0[1]) * uy))
                for a, b in lines)
    gaps = [t[0] - s[1] for s, t in zip(ts, ts[1:])]
    if not gaps or min(gaps) < tol * 0.2:
        return None
    lengths = [t[1] - t[0] for t in ts]
    core = lengths[:-1]                                       # the last dash may be cut short
    kind = "center" if len(core) >= 2 and max(core) > 2.2 * min(core) else "hidden"
    p = lambda t: (a0[0] + ux * t, a0[1] + uy * t)   # noqa: E731
    return kind, p(ts[0][0]), p(ts[-1][1]), lengths


# ----------------------------------------------------------------------------- page reading
def read_page(page, tol_pt=0.6, dots=None):
    """PDF page -> strokes (y up, pt), arrows, texts. Small filled circles (dimension dots) are
    appended to `dots` as (centre, radius) when a list is given."""
    H = page.rect.height

    def P(q):
        return (float(q.x), H - float(q.y))
    strokes, arrows, fills = [], [], []
    for d in page.get_drawings():
        typ = d.get("type", "s")
        items = d.get("items") or []
        width = float(d.get("width") or 0.0)
        dash = _dash_kind(d.get("dashes") or "")
        if "f" in typ and "s" not in typ or (typ == "fs" and width < 0.3):
            # filled shapes: small triangles are arrowheads; page background etc. are ignored
            pts, curve_pts = [], []
            for it in items:
                if it[0] == "l":
                    pts += [P(it[1]), P(it[2])]
                elif it[0] == "re":
                    r = it[1]
                    pts += [P(r.tl), P(r.tr), P(r.br), P(r.bl)]
                elif it[0] == "c":
                    curve_pts += [_bezier(P(it[1]), P(it[2]), P(it[3]), P(it[4]), k / 4) for k in range(5)]
            if curve_pts and not pts and dots is not None:
                fit = _fit_circle(curve_pts)
                if fit and fit[2] < 4.0 and max(abs(math.dist(q, fit[:2]) - fit[2]) for q in curve_pts) < 0.1 * fit[2] + 0.05:
                    dots.append(((fit[0], fit[1]), fit[2]))     # a dot terminator
                    continue
            uniq = []
            for p in pts:
                if not any(math.dist(p, q) < 1e-3 for q in uniq):
                    uniq.append(p)
            if len(uniq) == 3:
                edges = [(math.dist(uniq[i], uniq[(i + 1) % 3]), i) for i in range(3)]
                short, i = min(edges)
                longest = max(e for e, _ in edges)
                if longest < 25 and short < longest * 0.7:
                    tip = uniq[(i + 2) % 3]
                    base = ((uniq[i][0] + uniq[(i + 1) % 3][0]) / 2, (uniq[i][1] + uniq[(i + 1) % 3][1]) / 2)
                    L = math.dist(tip, base)
                    if L > 1e-6:
                        arrows.append({"tip": tip, "dir": ((tip[0] - base[0]) / L, (tip[1] - base[1]) / L), "len": L})
                        continue
            fills.append(uniq)
            continue
        # strokes: collect line items and Bezier chains of this path
        lines, chain = [], []

        def flush_chain():
            if not chain:
                return
            pts = []
            for (p0, p1, p2, p3) in chain:
                for k in range(8):
                    pts.append(_bezier(p0, p1, p2, p3, k / 8))
            pts.append(chain[-1][3])
            cur = _curve_from_beziers(chain, pts, tol_pt)
            if cur:
                kind, c, r, a0, a1 = cur
                strokes.append(Stroke(kind, width, dash, c=c, r=r, a0=a0 % 360, a1=a0 % 360 + (a1 - a0)))
            else:
                for a, b in zip(pts, pts[1:]):
                    strokes.append(Stroke("line", width, dash, a=a, b=b))
            chain.clear()
        for it in items:
            if it[0] == "c":
                chain.append((P(it[1]), P(it[2]), P(it[3]), P(it[4])))
                continue
            flush_chain()
            if it[0] == "l":
                lines.append((P(it[1]), P(it[2])))
            elif it[0] == "re":
                r = it[1]
                q = [P(r.tl), P(r.tr), P(r.br), P(r.bl)]
                lines += list(zip(q, q[1:] + q[:1]))
            elif it[0] == "qu":
                qd = it[1]
                q = [P(qd.ul), P(qd.ur), P(qd.lr), P(qd.ll)]
                lines += list(zip(q, q[1:] + q[:1]))
        flush_chain()
        lines = [(a, b) for a, b in lines if math.dist(a, b) > 1e-6]
        if not lines:
            continue
        # a line drawn as separate dashes
        if dash is None and len(lines) >= 2:
            m = _merge_dashes(lines, tol_pt)
            if m:
                kind, a, b, lengths = m
                strokes.append(Stroke("line", width, kind, a=a, b=b, lengths=lengths))
                continue
        # a fine polyline that is really a circle / arc
        if len(lines) >= 8 and all(math.dist(lines[i][1], lines[i + 1][0]) < 1e-3 for i in range(len(lines) - 1)):
            pts = [lines[0][0]] + [b for _, b in lines]
            cur = _curve_from_points(pts, tol_pt)
            if cur:
                kind, c, r, a0, a1 = cur
                strokes.append(Stroke(kind, width, dash, c=c, r=r, a0=a0 % 360, a1=a0 % 360 + (a1 - a0)))
                continue
        for a, b in lines:
            strokes.append(Stroke("line", width, dash, a=a, b=b))

    texts = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            dx, dy = line.get("dir", (1.0, 0.0))
            ang = math.degrees(math.atan2(-dy, dx))
            # PyMuPDF joins texts that merely line up ("12" and "10" of two stacked dimensions):
            # split where the gap along the writing direction is larger than ~half a character
            groups = [[spans[0]]]
            for sp in spans[1:]:
                prev = groups[-1][-1]
                if abs(dx) >= abs(dy):
                    gap = sp["bbox"][0] - prev["bbox"][2] if dx > 0 else prev["bbox"][0] - sp["bbox"][2]
                else:
                    gap = prev["bbox"][1] - sp["bbox"][3] if dy < 0 else sp["bbox"][1] - prev["bbox"][3]
                if gap > 0.6 * max(sp.get("size", 1), 1):
                    groups.append([sp])
                else:
                    groups[-1].append(sp)
            for g in groups:
                txt = "".join(s["text"] for s in g).strip()
                x0 = min(s["bbox"][0] for s in g)
                y0 = min(s["bbox"][1] for s in g)
                x1 = max(s["bbox"][2] for s in g)
                y1 = max(s["bbox"][3] for s in g)
                size = max(s.get("size", 0) for s in g)
                texts.append({"text": txt, "center": ((x0 + x1) / 2, H - (y0 + y1) / 2), "angle": ang, "size": size,
                              "bbox": (x0, H - y1, x1, H - y0)})
    return strokes, arrows, texts


def _dedupe(strokes, tol):
    out, seen = [], set()
    for s in strokes:
        if s.kind == "line":
            a, b = sorted((s.a, s.b))
            k = ("l", round(a[0] / tol), round(a[1] / tol), round(b[0] / tol), round(b[1] / tol))
        else:
            k = (s.kind, round(s.c[0] / tol), round(s.c[1] / tol), round(s.r / tol), round(s.a0), round(s.a1))
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


# ----------------------------------------------------------------------------- dimensions
def _shown(text):
    """Number written in a dimension text (same rules as the DXF reader)."""
    return R.dim_text_value(text)


def find_dimensions(arrows, texts, curves, tol):
    """Arrow pairs (+ value text) -> dims in pt. Also returns indices of used texts and arrows."""
    dims, used_t, used_a = [], set(), set()
    numeric = [i for i, t in enumerate(texts) if NUM.search(t["text"])]

    def text_score(p, q, u, i):
        """Distance from a number text to the dimension segment p-q (None if implausible)."""
        t = texts[i]
        c = t["center"]
        L = math.dist(p, q)
        h = max(t["size"], 4.0)
        ta = math.radians(t["angle"])
        parallel = abs(math.sin(ta - math.atan2(u[1], u[0]))) < 0.2
        horizontal = abs(math.sin(ta)) < 0.2
        s_ = max(0.0, min(L, (c[0] - p[0]) * u[0] + (c[1] - p[1]) * u[1]))
        near = (p[0] + u[0] * s_, p[1] + u[1] * s_)
        d = math.dist(c, near)
        half = max(abs(t["bbox"][2] - t["bbox"][0]), abs(t["bbox"][3] - t["bbox"][1])) / 2
        if d > half + 3 * h:
            return None
        if not (parallel or horizontal):
            # odd text angle (some exporters): only when it sits right at the dimension
            return d + h if d < half + h else None
        return d

    # arrow pairs on one line: back to back (<-->, arrows inside) first, then face to face
    # (-> <-, arrows outside a short dimension or a small diameter)
    order = sorted(range(len(arrows)), key=lambda i: arrows[i]["tip"])
    paired = set()

    def partner(i, facing):
        A = arrows[i]
        best = None
        for j in order:
            if j == i or j in paired:
                continue
            B = arrows[j]
            if A["dir"][0] * B["dir"][0] + A["dir"][1] * B["dir"][1] > -0.98:
                continue
            d = (B["tip"][0] - A["tip"][0], B["tip"][1] - A["tip"][1])
            along = d[0] * A["dir"][0] + d[1] * A["dir"][1]
            perp = abs(d[0] * A["dir"][1] - d[1] * A["dir"][0])
            ok = (along > tol) if facing else (along < -tol)
            if ok and perp < max(tol, 0.15 * A["len"]) and (best is None or abs(along) < best[0]):
                best = (abs(along), j)
        return best[1] if best else None

    pairs = []
    for facing in (False, True):
        for i in order:
            if i in paired:
                continue
            j = partner(i, facing)
            if j is not None:
                paired.update((i, j))
                pairs.append((i, j))
    # assign value texts globally, closest first (a greedy per-dimension pick steals neighbours' texts)
    cands = []
    for k, (i, j) in enumerate(pairs):
        A, B = arrows[i], arrows[j]
        L = math.dist(A["tip"], B["tip"]) or 1.0
        u = ((B["tip"][0] - A["tip"][0]) / L, (B["tip"][1] - A["tip"][1]) / L)
        for ti in numeric:
            sc = text_score(A["tip"], B["tip"], u, ti)
            if sc is not None:
                cands.append((sc, k, ti, u, L))
    cands.sort(key=lambda c: c[0])
    # value check: a first assignment gives the drawing scale (number / measured length); then
    # texts whose number does not fit their dimension at that scale are not allowed to pair with it
    trial, seen_k, seen_t = [], set(), set()
    for sc, k, ti, u, L in cands:
        if k in seen_k or ti in seen_t:
            continue
        seen_k.add(k)
        seen_t.add(ti)
        v = R.dim_text_value(texts[ti]["text"])[0]
        if v:
            trial.append(v / L)
    ratio = None
    if trial:
        trial.sort()
        med = trial[len(trial) // 2]
        if sum(1 for r in trial if abs(r / med - 1) < 0.01) >= max(2, len(trial) // 2):
            ratio = med

    def fits(ti, L):
        v = R.dim_text_value(texts[ti]["text"])[0]
        return ratio is None or v is None or abs(v / (L * ratio) - 1) < 0.03
    cands = [c for c in cands if fits(c[2], c[4])] + [c for c in cands if not fits(c[2], c[4])]
    done = set()
    for sc, k, ti, u, L in cands:
        if k in done or ti in used_t:
            continue
        i, j = pairs[k]
        done.add(k)
        used_a.update((i, j))
        used_t.add(ti)
        t = texts[ti]["text"]
        kind = "aligned" if abs(u[0]) > 1e-3 and abs(u[1]) > 1e-3 else "linear"
        if re.search(r"[Øø⌀]|%%c", t, re.I) and kind == "aligned":
            kind = "diameter"          # a slanted dimension across a circle with a Ø value
        elif re.match(r"\s*(\d+\s*-\s*)?R\s*\d", t):
            kind = "radius"
        dims.append({"type": kind, "measured": L, "text": t,
                     "points": [texts[ti]["center"], arrows[i]["tip"], arrows[j]["tip"]]})
    # single arrows ending on a circle / arc: diameter or radius callouts
    for i, A in enumerate(arrows):
        if i in used_a:
            continue
        on = None
        for c in curves:
            if abs(math.dist(A["tip"], c.c) - c.r) < max(tol, 0.02 * c.r):
                if on is None or c.r < on.r:
                    on = c
        if on is None:
            continue
        best = None
        for k, t in enumerate(texts):
            if k in used_t or not re.search(r"[ØøR⌀]\s*\d|%%c", t["text"], re.I):
                continue
            dd = math.dist(t["center"], A["tip"])
            if dd < 40 + 3 * on.r and (best is None or dd < best[0]):
                best = (dd, k)
        if best is None:
            continue
        k = best[1]
        used_t.add(k)
        used_a.add(i)
        is_r = bool(re.search(r"(^|[^A-Za-z])R\s*\d", texts[k]["text"]))
        dims.append({"type": "radius" if is_r else "diameter", "measured": on.r if is_r else 2 * on.r,
                     "text": texts[k]["text"], "points": [texts[k]["center"], A["tip"], on.c]})
    return dims, used_t, used_a


def _terminator_arrows(strokes, dots, arrows, tol):
    """Dimension lines ended by oblique ticks (architectural) or dots instead of arrowheads.

    A thin line carrying exactly two terminators (a short stroke crossing it at 30-60 deg through
    its midpoint, or a small filled dot on it) becomes a pair of synthetic arrows pointing outward
    at those points, so the normal arrow-pair code reads it. Returns (arrows, ids of tick strokes).
    """
    solid = [s for s in strokes if s.kind == "line" and s.dash is None]
    if not solid:
        return [], set()
    wmax = max(s.width for s in solid)
    split = min(s.width for s in solid) < 0.75 * wmax
    thin = [s for s in solid if not split or s.width < 0.85 * wmax]
    ticks = []
    for s in thin:
        L = math.dist(s.a, s.b)
        if 1.5 <= L <= 8.0:
            ticks.append((s, ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2),
                          math.degrees(math.atan2(s.b[1] - s.a[1], s.b[0] - s.a[0])), L))
    if not ticks and not dots:
        return [], set()
    tips = [a["tip"] for a in arrows]
    out, used = [], set()
    for D in thin:
        LD = math.dist(D.a, D.b)
        if LD <= 8.0 or any(math.dist(D.a, t) < tol * 3 or math.dist(D.b, t) < tol * 3 for t in tips):
            continue
        u = ((D.b[0] - D.a[0]) / LD, (D.b[1] - D.a[1]) / LD)
        ang_d = math.degrees(math.atan2(u[1], u[0]))
        on = []

        def place(p):
            d = (p[0] - D.a[0], p[1] - D.a[1])
            along = d[0] * u[0] + d[1] * u[1]
            perp = abs(d[0] * u[1] - d[1] * u[0])
            return along if perp < tol and -tol <= along <= LD + tol else None
        for s, m, ang, L in ticks:
            if s is D or L * 2 > LD:
                continue
            rel = (ang - ang_d) % 180
            if not (30 <= rel <= 60 or 120 <= rel <= 150):
                continue
            al = place(m)
            if al is not None:
                on.append((al, m, s))
        for c, r in dots:
            al = place(c)
            if al is not None:
                on.append((al, c, None))
        uniq = []
        for al, p, s in sorted(on, key=lambda x: x[0]):
            if not uniq or al - uniq[-1][0] > 2 * tol:
                uniq.append((al, p, s))
        if len(uniq) != 2:
            continue
        (a0, p0, s0), (a1, p1, s1) = uniq
        out.append({"tip": p0, "dir": (-u[0], -u[1]), "len": 3.0, "terminator": "tick" if s0 is not None else "dot"})
        out.append({"tip": p1, "dir": u, "len": 3.0, "terminator": "tick" if s1 is not None else "dot"})
        used |= {id(s) for s in (s0, s1) if s is not None}
    return out, used


# ----------------------------------------------------------------------------- hatching
def find_hatch_lines(lines, tol, tips=()):
    """Indices of thin lines forming section hatching: >= 6 parallel slanted lines at a regular
    spacing (dimension lines ending at arrowheads do not count)."""
    groups = {}
    for i, s in enumerate(lines):
        if any(math.dist(s.a, t) < 1.0 or math.dist(s.b, t) < 1.0 for t in tips):
            continue
        ang = math.degrees(math.atan2(s.b[1] - s.a[1], s.b[0] - s.a[0])) % 180
        if min(abs(ang - 0), abs(ang - 90), abs(ang - 180)) < 5:
            continue
        groups.setdefault(round(ang), []).append(i)
    out = set()
    for ang, idx in groups.items():
        if len(idx) < 6:
            continue
        nx, ny = -math.sin(math.radians(ang)), math.cos(math.radians(ang))
        offs = sorted({round(lines[i].a[0] * nx + lines[i].a[1] * ny, 2) for i in idx})
        steps = [b - a for a, b in zip(offs, offs[1:]) if b - a > tol]
        if len(steps) < 4:
            continue
        steps.sort()
        base = steps[len(steps) // 4]                 # pattern spacing (lower quartile)
        regular = sum(1 for d in steps if abs(d / base - round(d / base)) < 0.15)
        if base > 0 and regular >= 0.8 * len(steps):
            out |= set(idx)
    return out


def hatch_regions(outline_lines, hatch_lines, tol):
    """Faces of the outline graph that contain hatch-line midpoints -> polygons."""
    from .section_reader import bounded_faces, _inside
    faces = bounded_faces([(s.a, s.b) for s in outline_lines], tol)
    mids = [((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2) for s in hatch_lines]
    polys = []
    for f in faces:
        n = sum(1 for m in mids if _inside(m, f))
        if n >= 3:
            polys.append(f)
    # a face inside another hatched face is a hole in it; keep the smallest faces that hold hatch
    return polys


# ----------------------------------------------------------------------------- entry
def pick_page(doc):
    """Page with the most drawing paths (cover sheets, parts lists and notes pages have few)."""
    best = (-1, 0)
    for i in range(doc.page_count):
        try:
            n = len(doc[i].get_drawings())
        except Exception:
            n = 0
        if n > best[0]:
            best = (n, i)
    return best[1]


def load(path, page_no=None):
    """Returns dict(segs, texts, dims, hatch_polys, issues, scale_mm_per_pt) in mm.
    page_no: 0-based page; None = the page with the most drawing paths."""
    pymupdf = ensure_pymupdf()
    if pymupdf is None:
        raise R.ExtractError("E101", "PDF를 읽으려면 PyMuPDF가 필요합니다 (pip install pymupdf)")
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise R.ExtractError("E101", "PDF를 열 수 없습니다: %s" % e)
    if doc.page_count == 0:
        raise R.ExtractError("E101", "PDF에 페이지가 없습니다")
    issues = []
    if page_no is None:
        page_no = pick_page(doc) if doc.page_count > 1 else 0
        if doc.page_count > 1:
            issues.append(("E207", "INFO", "PDF %d쪽 중 선이 가장 많은 %d쪽을 읽었습니다 (다른 쪽: --page 번호)"
                           % (doc.page_count, page_no + 1)))
    elif not 0 <= page_no < doc.page_count:
        raise R.ExtractError("E101", "PDF에 %d쪽이 없습니다 (전체 %d쪽)" % (page_no + 1, doc.page_count))
    elif doc.page_count > 1:
        issues.append(("E207", "INFO", "PDF %d쪽 중 %d쪽을 읽었습니다" % (doc.page_count, page_no + 1)))
    page = doc[page_no]
    dots = []
    strokes, arrows, texts = read_page(page, dots=dots)
    if not strokes and texts:
        raise R.ExtractError("E103", "PDF에 선이 없습니다 — 스캔한 이미지 PDF 같습니다. 채팅 판독(docs/image-reading.md)을 쓰세요")
    if not strokes:
        raise R.ExtractError("E102", "PDF에서 도형을 찾지 못했습니다")
    tol = 0.3
    strokes = _dedupe(strokes, 0.05)
    term, tick_ids = _terminator_arrows(strokes, dots, arrows, tol)
    if term:
        arrows = arrows + term
        strokes = [s for s in strokes if id(s) not in tick_ids]
        kinds = sorted({a["terminator"] for a in term})
        issues.append(("E207", "INFO", "화살표 대신 %s 끝 표시 치수 %d개를 읽었습니다"
                       % ("·".join({"tick": "사선(틱)", "dot": "점"}[k] for k in kinds), len(term) // 2)))

    # ---- a short centre line drawn as pieces may have only one long dash + one dot, which looks
    # "uniform" on its own: compare with the drawing's typical hidden-line dash
    core = sorted(st.lengths[0] for st in strokes if st.dash == "hidden" and st.lengths and len(st.lengths) >= 2)
    if core:
        hid_dash = core[len(core) // 4]          # lower quartile: misread centre lines are the minority
        for st in strokes:
            if st.dash == "hidden" and st.lengths and max(st.lengths) > 1.6 * hid_dash:
                st.dash = "center"

    # ---- a dashed line shorter than one dash is a single solid piece: recognise it by the pen
    # width the drawing uses for its hidden / centre lines
    for kind in ("hidden", "center"):
        ws = {}
        for s in strokes:
            if s.dash == kind and s.kind == "line":
                ws[round(s.width, 2)] = ws.get(round(s.width, 2), 0) + 1
        if not ws:
            continue
        w_kind = max(ws, key=ws.get)
        dash_max = max((max(s.lengths) for s in strokes if s.dash == kind and s.lengths), default=None)
        if dash_max is None:
            continue
        others = {round(s.width, 2) for s in strokes if s.dash is None and s.kind == "line"}
        for s in strokes:
            if s.dash is None and s.kind == "line" and abs(s.width - w_kind) < 0.02 and \
                    math.dist(s.a, s.b) <= dash_max * 1.05 and len(others) > 1:
                s.dash = kind

    # ---- line classes by width (solid strokes only)
    solid = [s for s in strokes if s.dash is None]
    widths = {}
    for s in solid:
        L = math.dist(s.a, s.b) if s.kind == "line" else s.r * math.radians(abs(s.a1 - s.a0) or 360)
        widths[round(s.width, 2)] = widths.get(round(s.width, 2), 0.0) + L
    wmax = max(widths) if widths else 0.0
    split = bool(widths) and min(widths) < 0.75 * wmax
    vis_min = 0.85 * wmax if split else 0.0

    curves = [s for s in strokes if s.kind in ("circle", "arc") and s.dash is None]
    dims_pt, used_t, used_a = find_dimensions(arrows, texts, curves, tol)

    # ---- scale from dimension values
    ratios = []
    for d in dims_pt:
        v = R.dim_text_value(d["text"])[0]
        if v and d["measured"] > 1e-6:
            ratios.append(v / d["measured"])
    scale = PT_MM
    if ratios:
        ratios.sort()
        med = ratios[len(ratios) // 2]
        agree = [r for r in ratios if abs(r - med) / med < 0.005]
        if len(agree) >= max(2, (len(ratios) + 1) // 2):
            scale = sum(agree) / len(agree)
            # snap to a standard drawing scale (PDF coordinates carry ~1e-5 rounding)
            for paper_std in (1, 2, 5, 10, 20, 50, 100, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 1 / 1.5, 1 / 2.5, 1 / 3, 1 / 4, 4, 3, 2.5, 1.5):
                if abs(PT_MM / scale / paper_std - 1) < 0.002:
                    scale = PT_MM / paper_std
                    break
            paper = PT_MM / scale
            if abs(paper - 1) > 0.01:
                issues.append(("E207", "INFO", "PDF 도면 축척 약 1:%g로 읽었습니다 (치수 %d개 기준)"
                               % (round(1 / paper, 3), len(agree))))
        else:
            issues.append(("E106", "WARN", "치수 값과 그려진 길이의 비율이 일정하지 않아 1:1로 가정했습니다"))
    else:
        issues.append(("E106", "WARN", "PDF에서 치수를 찾지 못해 축척 1:1로 가정했습니다"))

    # ---- geometry: drop annotation lines
    tips = [a["tip"] for a in arrows]
    dim_dirs = [((d["points"][1], d["points"][2])) for d in dims_pt if d["type"] in ("linear", "aligned")]

    def is_annotation(s):
        if s.dash is not None:
            return False
        if split:
            return s.width < vis_min
        # one line width: remove dimension / extension / leader lines structurally
        if s.kind != "line":
            return False
        for t in tips:
            if math.dist(s.a, t) < tol * 3 or math.dist(s.b, t) < tol * 3:
                return True
        for p, q in dim_dirs:
            ux, uy = q[0] - p[0], q[1] - p[1]
            L = math.hypot(ux, uy) or 1.0
            ux, uy = ux / L, uy / L
            sx, sy = s.b[0] - s.a[0], s.b[1] - s.a[1]
            sl = math.hypot(sx, sy) or 1.0
            if abs((sx * ux + sy * uy) / sl) < 0.05:      # perpendicular to the dimension line
                for t in (p, q):
                    dist = abs((t[0] - s.a[0]) * sy - (t[1] - s.a[1]) * sx) / sl
                    proj = ((t[0] - s.a[0]) * sx + (t[1] - s.a[1]) * sy) / (sl * sl)
                    if dist < tol * 3 and -0.2 <= proj <= 1.2:
                        return True
        return False

    thin_lines = [s for s in strokes if s.kind == "line" and s.dash is None and is_annotation(s)]
    hatch_idx = find_hatch_lines(thin_lines, tol, tips)
    hatch_lines = [thin_lines[i] for i in hatch_idx]
    if not split:
        # with one width, hatch lines were not removed as annotation
        all_lines = [s for s in strokes if s.kind == "line" and s.dash is None]
        hl = find_hatch_lines(all_lines, tol, tips)
        hatch_lines = [all_lines[i] for i in hl]
    hatch_ids = {id(s) for s in hatch_lines}
    geo = [s for s in strokes if id(s) not in hatch_ids and not is_annotation(s)]

    k = scale

    def mm(p):
        return (p[0] * k, p[1] * k)
    segs = []
    for s in geo:
        hidden = s.dash == "hidden"
        center = s.dash == "center"
        if s.kind == "line":
            segs.append(R.Seg("line", "0", hidden, center, a=mm(s.a), b=mm(s.b)))
        elif s.kind == "circle":
            segs.append(R.Seg("circle", "0", hidden, center, c=mm(s.c), r=s.r * k))
        else:
            c, r = mm(s.c), s.r * k
            a, b = R._arc_ends(c, r, s.a0, s.a1)
            segs.append(R.Seg("arc", "0", hidden, center, a=a, b=b, c=c, r=r, a0=s.a0, a1=s.a1))

    _snap_ends(segs, 0.1)
    hatch_polys = []
    if hatch_lines:
        outline = [s for s in geo if s.kind == "line" and s.dash is None]
        polys = hatch_regions(outline, hatch_lines, tol * 2)
        hatch_polys = [[mm(p) for p in poly] for poly in polys]
        if not hatch_polys:
            issues.append(("E208", "WARN", "PDF에 단면 해칭으로 보이는 선이 있지만 해칭 영역을 찾지 못했습니다"))

    out_texts = [{"text": t["text"], "pos": mm(t["center"]), "layer": "0"} for i, t in enumerate(texts) if i not in used_t]
    dims = []
    for d in dims_pt:
        shown, mult = R.dim_text_value(d["text"])
        dims.append({"type": d["type"], "measured": d["measured"] * k, "shown": shown, "text": _dxf_like(d["text"]),
                     "mult": mult, "points": [mm(p) for p in d["points"]], "entity": None})
    return {"segs": segs, "texts": out_texts, "dims": dims, "hatch_polys": hatch_polys, "issues": issues,
            "scale_mm_per_pt": k, "arrows": len(arrows), "line_width_split": split}


def _snap_ends(segs, tol):
    """Fitted arcs end a hair away from the lines they meet: move the arc ends onto them."""
    ends = [p for sg in segs if sg.kind == "line" for p in (sg.a, sg.b)]
    for sg in segs:
        if sg.kind != "arc":
            continue
        for attr in ("a", "b"):
            p = getattr(sg, attr)
            best = min(ends, key=lambda q: math.dist(p, q), default=None)
            if best is not None and 0 < math.dist(p, best) < tol:
                setattr(sg, attr, best)


def _dxf_like(text):
    """PDF text -> the DXF dimension text conventions the readers understand."""
    return text.replace("⌀", "%%c").replace("Ø", "%%c").replace("ø", "%%c").replace("°", "%%d").replace("±", "%%p")
