"""Generate test DXF drawings with known answers.

    python tools/make_sample_dxf.py            -> samples/drawings/dxf/*.dxf + *.expected.json

Layout: third-angle projection (KS / ISO-A), drawn 1:1 in mm unless noted.
  front view : x = X, y = Z            origin (0, 0)
  top view   : x = X, y = Y            above the front view
  right view : x = Y, y = Z            right of the front view
Layers: OUTLINE (visible), HIDDEN (DASHED), CENTER (CENTER), DIM, TEXT, TITLE (border + title block).

Turned parts (*_section): a full section along the axis (x = axial, y = radius, axis on y = 0),
hatched on both sides, with an end view to the right. Expected answers carry the half profile.
"""

import json
import math
import os
import sys

import ezdxf

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "samples", "drawings", "dxf")
GAP = 30.0


class Sheet:
    def __init__(self, title):
        self.doc = ezdxf.new("R2010", setup=True)
        self.doc.units = ezdxf.units.MM
        self.msp = self.doc.modelspace()
        for name, color, lt in (("OUTLINE", 7, "CONTINUOUS"), ("HIDDEN", 3, "DASHED"),
                                ("CENTER", 1, "CENTER"), ("DIM", 4, "CONTINUOUS"),
                                ("TEXT", 2, "CONTINUOUS"), ("TITLE", 8, "CONTINUOUS"),
                                ("HATCH", 8, "CONTINUOUS")):
            self.doc.layers.add(name, color=color, linetype=lt)
        self.title = title
        self.extent = [1e9, 1e9, -1e9, -1e9]

    def _grow(self, *pts):
        for x, y in pts:
            self.extent[0] = min(self.extent[0], x)
            self.extent[1] = min(self.extent[1], y)
            self.extent[2] = max(self.extent[2], x)
            self.extent[3] = max(self.extent[3], y)

    # geometry ---------------------------------------------------------------
    def line(self, a, b, layer="OUTLINE"):
        self.msp.add_line(a, b, dxfattribs={"layer": layer})
        self._grow(a, b)

    def poly(self, pts, layer="OUTLINE", closed=True):
        seq = list(pts) + ([pts[0]] if closed else [])
        for a, b in zip(seq, seq[1:]):
            self.line(a, b, layer)

    def rect(self, x, y, w, h, layer="OUTLINE"):
        self.poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], layer)

    def round_rect(self, x, y, w, h, r, layer="OUTLINE"):
        self.line((x + r, y), (x + w - r, y), layer)
        self.line((x + w, y + r), (x + w, y + h - r), layer)
        self.line((x + w - r, y + h), (x + r, y + h), layer)
        self.line((x, y + h - r), (x, y + r), layer)
        for cx, cy, a0 in ((x + w - r, y + r, 270), (x + w - r, y + h - r, 0),
                           (x + r, y + h - r, 90), (x + r, y + r, 180)):
            self.msp.add_arc((cx, cy), r, a0, a0 + 90, dxfattribs={"layer": layer})
        self._grow((x, y), (x + w, y + h))

    def circle(self, c, r, layer="OUTLINE"):
        self.msp.add_circle(c, r, dxfattribs={"layer": layer})
        self._grow((c[0] - r, c[1] - r), (c[0] + r, c[1] + r))

    def center_mark(self, c, r):
        e = r + 3
        self.line((c[0] - e, c[1]), (c[0] + e, c[1]), "CENTER")
        self.line((c[0], c[1] - e), (c[0], c[1] + e), "CENTER")

    def hidden_v(self, x, y0, y1):
        self.line((x, y0), (x, y1), "HIDDEN")

    def hidden_h(self, y, x0, x1):
        self.line((x0, y), (x1, y), "HIDDEN")

    # annotation -------------------------------------------------------------
    def hdim(self, a, b, y, text=None):
        d = self.msp.add_linear_dim(base=(a[0], y), p1=a, p2=b, angle=0, dimstyle="EZDXF",
                                    text=text or "<>", dxfattribs={"layer": "DIM"})
        d.render()
        self._grow((a[0], y), (b[0], y))

    def vdim(self, a, b, x, text=None):
        d = self.msp.add_linear_dim(base=(x, a[1]), p1=a, p2=b, angle=90, dimstyle="EZDXF",
                                    text=text or "<>", dxfattribs={"layer": "DIM"})
        d.render()
        self._grow((x, a[1]), (x, b[1]))

    def ddim(self, c, r, angle=45, text=None):
        d = self.msp.add_diameter_dim(center=c, radius=r, angle=angle, dimstyle="EZDXF",
                                      text=text or "<>", dxfattribs={"layer": "DIM"})
        d.render()

    def rdim(self, c, r, angle=45, text=None):
        d = self.msp.add_radius_dim(center=c, radius=r, angle=angle, dimstyle="EZDXF",
                                    text=text or "<>", dxfattribs={"layer": "DIM"})
        d.render()

    def arc(self, c, r, a0, a1, layer="OUTLINE"):  # noqa: D401
        self.msp.add_arc(c, r, a0, a1, dxfattribs={"layer": layer})
        self._grow((c[0] - r, c[1] - r), (c[0] + r, c[1] + r))

    def hatch(self, pts):
        h = self.msp.add_hatch(color=7, dxfattribs={"layer": "HATCH"})
        h.set_pattern_fill("ANSI31", scale=0.5)
        h.paths.add_polyline_path(pts, is_closed=True)

    def note(self, p, text, h=3.5):
        self.msp.add_text(text, height=h, dxfattribs={"layer": "TEXT", "insert": p})

    # sheet ------------------------------------------------------------------
    def finish(self, path, scale="1:1"):
        x0, y0, x1, y1 = self.extent
        m = 25
        bx0, by0, bx1, by1 = x0 - m, y0 - m - 30, x1 + m + 10, y1 + m
        self.rect(bx0, by0, bx1 - bx0, by1 - by0, "TITLE")
        # title block bottom-right
        tw, th = 110, 26
        tx, ty = bx1 - tw, by0
        self.rect(tx, ty, tw, th, "TITLE")
        self.line((tx, ty + 13), (bx1, ty + 13), "TITLE")
        self.line((tx + 70, ty), (tx + 70, ty + 13), "TITLE")
        self.msp.add_text(self.title, height=5, dxfattribs={"layer": "TITLE", "insert": (tx + 3, ty + 16)})
        self.msp.add_text("SCALE " + scale, height=3, dxfattribs={"layer": "TITLE", "insert": (tx + 3, ty + 4)})
        self.msp.add_text("3RD ANGLE  mm", height=3, dxfattribs={"layer": "TITLE", "insert": (tx + 73, ty + 4)})
        self.doc.saveas(path)


# ---------------------------------------------------------------------------- parts
def block_basic():
    L, W, H = 80.0, 40.0, 20.0
    s = Sheet("BLOCK_BASIC")
    ty, rx = H + GAP, L + GAP
    s.rect(0, 0, L, H)                    # front
    s.rect(0, ty, L, W)                   # top
    s.rect(rx, 0, W, H)                   # right
    s.hdim((0, 0), (L, 0), -12)
    s.vdim((L, 0), (L, H), L + 12)
    s.vdim((0, ty), (0, ty + W), -12)
    return s, {"bbox": [L, W, H], "holes": [], "volume": L * W * H}


def plate_holes():
    L, W, T, d, e, r = 80.0, 40.0, 6.0, 6.6, 10.0, 3.0
    s = Sheet("PLATE_HOLES")
    ty, rx = T + GAP, L + GAP
    s.rect(0, 0, L, T)
    for x in (e, L - e):
        for dx in (-d / 2, d / 2):
            s.hidden_v(x + dx, 0, T)
        s.line((x, -3), (x, T + 3), "CENTER")
    s.round_rect(0, ty, L, W, r)
    holes = []
    for x in (e, L - e):
        for y in (e, W - e):
            s.circle((x, ty + y), d / 2)
            s.center_mark((x, ty + y), d / 2)
            holes.append([x, y])
    s.rect(rx, 0, W, T)
    for y in (e, W - e):
        for dy in (-d / 2, d / 2):
            s.hidden_v(rx + y + dy, 0, T)
    s.hdim((0, 0), (L, 0), -12)
    s.vdim((L, 0), (L, T), L + 12)
    s.vdim((0, ty), (0, ty + W), -14)
    s.hdim((0, ty + W), (e, ty + W - e), ty + W + 10)
    s.hdim((e, ty + W - e), (L - e, ty + W - e), ty + W + 10)
    s.vdim((L, ty), (L - e, ty + e), L + 12)
    s.vdim((L - e, ty + e), (L - e, ty + W - e), L + 12)
    s.ddim((e, ty + e), d / 2, angle=225, text="4-%%c<>")
    s.rdim((L - r, ty + W - r), r, angle=45, text="4-R<>")
    vol = L * W * T - 4 * (r * r - math.pi * r * r / 4) * T - 4 * math.pi * (d / 2) ** 2 * T
    return s, {"bbox": [L, W, T], "holes": [{"axis": "Z", "diameter": d, "positions": holes}],
               "fillet": r, "volume": vol}


def bracket_l():
    A, B, arm, T = 60.0, 50.0, 20.0, 8.0
    d, cbd, cbh = 5.5, 9.5, 5.4
    hp = [[A - arm / 2 - 5, arm / 2], [arm / 2, B - arm / 2 - 5]]
    s = Sheet("BRACKET_L")
    ty, rx = T + GAP, A + GAP
    s.rect(0, 0, A, T)
    for x, _ in hp:
        for dx in (-cbd / 2, cbd / 2):
            s.hidden_v(x + dx, T - cbh, T)
        for dx in (-d / 2, d / 2):
            s.hidden_v(x + dx, 0, T - cbh)
        s.hidden_h(T - cbh, x - cbd / 2, x + cbd / 2)
    s.poly([(0, ty), (A, ty), (A, ty + arm), (arm, ty + arm), (arm, ty + B), (0, ty + B)])
    for x, y in hp:
        s.circle((x, ty + y), d / 2)
        s.circle((x, ty + y), cbd / 2)
        s.center_mark((x, ty + y), cbd / 2)
    s.rect(rx, 0, B, T)
    s.hdim((0, 0), (A, 0), -12)
    s.vdim((A, 0), (A, T), A + 12)
    s.vdim((0, ty), (0, ty + B), -14)
    s.vdim((A, ty), (A, ty + arm), A + 12)
    s.hdim((0, ty + B), (arm, ty + B), ty + B + 10)
    s.hdim((0, ty + B), (hp[0][0], ty + hp[0][1]), ty + B + 20)
    s.vdim((0, ty), (hp[1][0], ty + hp[1][1]), -26)
    s.ddim((hp[0][0], ty + hp[0][1]), d / 2, angle=300, text="2-%%c<>")
    s.ddim((hp[0][0], ty + hp[0][1]), cbd / 2, angle=60, text="C'BORE %%c<> DP5.4")
    return s, {"bbox": [A, B, T], "holes": [{"axis": "Z", "diameter": d, "positions": hp},
                                             {"axis": "Z", "diameter": cbd, "positions": hp}]}


def stepped_block():
    L, W, H, h1, step_x = 60.0, 40.0, 30.0, 15.0, 30.0
    s = Sheet("STEPPED_BLOCK")
    ty, rx = H + GAP, L + GAP
    s.poly([(0, 0), (L, 0), (L, h1), (step_x, h1), (step_x, H), (0, H)])
    s.rect(0, ty, L, W)
    s.line((step_x, ty), (step_x, ty + W))
    s.rect(rx, 0, W, H)
    s.line((rx, h1), (rx + W, h1))
    s.hdim((0, 0), (L, 0), -12)
    s.hdim((0, H), (step_x, H), H + 10)
    s.vdim((L, 0), (L, h1), L + 12)
    s.vdim((0, 0), (0, H), -12)
    s.vdim((0, ty), (0, ty + W), -12)
    vol = L * W * h1 + step_x * W * (H - h1)
    return s, {"bbox": [L, W, H], "holes": [], "volume": vol}


def flange():
    D, T, hub, hh, bore, pcd, bd = 100.0, 12.0, 60.0, 10.0, 40.0, 75.0, 9.0
    s = Sheet("FLANGE")
    R = D / 2
    ty = T + hh + GAP + R          # top view centre
    cx = R
    # front view: disc + hub, hidden bore and bolt holes
    s.poly([(0, 0), (D, 0), (D, T), (cx + hub / 2, T), (cx + hub / 2, T + hh),
            (cx - hub / 2, T + hh), (cx - hub / 2, T), (0, T)])
    for dx in (-bore / 2, bore / 2):
        s.hidden_v(cx + dx, 0, T + hh)
    for dx in (-pcd / 2, pcd / 2):
        for ddx in (-bd / 2, bd / 2):
            s.hidden_v(cx + dx + ddx, 0, T)
    s.line((cx, -4), (cx, T + hh + 4), "CENTER")
    # top view
    s.circle((cx, ty), R)
    s.circle((cx, ty), hub / 2)
    s.circle((cx, ty), bore / 2)
    s.circle((cx, ty), pcd / 2, "CENTER")
    holes = []
    for k in range(6):
        a = math.radians(60 * k)
        p = (cx + pcd / 2 * math.cos(a), ty + pcd / 2 * math.sin(a))
        s.circle(p, bd / 2)
        holes.append([round(pcd / 2 * math.cos(a), 6), round(pcd / 2 * math.sin(a), 6)])
    s.center_mark((cx, ty), R)
    s.hdim((0, 0), (D, 0), -12, text="%%c<>")
    s.vdim((D, 0), (D, T), D + 12)
    s.vdim((cx + hub / 2, T), (cx + hub / 2, T + hh), D + 24)
    s.ddim((cx, ty), hub / 2, angle=135, text="%%c<>")
    s.ddim((cx, ty), bore / 2, angle=200, text="%%c<>")
    s.ddim((cx + pcd / 2, ty), bd / 2, angle=45, text="6-%%c<>")
    s.ddim((cx, ty), pcd / 2, angle=100, text="P.C.D <>")
    vol = math.pi * R * R * T + math.pi * (hub / 2) ** 2 * hh - math.pi * (bore / 2) ** 2 * (T + hh) \
        - 6 * math.pi * (bd / 2) ** 2 * T
    return s, {"bbox": [D, D, T + hh], "origin_offset": [-R, -R, 0],
               "holes": [{"axis": "Z", "diameter": bd, "positions": holes},
                         {"axis": "Z", "diameter": bore, "positions": [[0, 0]]}],
               "volume": vol}


def plate_one_view():
    L, W, t, d = 70.0, 50.0, 5.0, 8.0
    s = Sheet("PLATE_ONE_VIEW")
    s.rect(0, 0, L, W)
    pos = [[15, 25], [55, 25]]
    for x, y in pos:
        s.circle((x, y), d / 2)
        s.center_mark((x, y), d / 2)
    s.hdim((0, 0), (L, 0), -12)
    s.vdim((0, 0), (0, W), -12)
    s.hdim((0, W), (15, 25), W + 10)
    s.hdim((15, 25), (55, 25), W + 10)
    s.vdim((L, 0), (55, 25), L + 12)
    s.ddim((15, 25), d / 2, angle=45, text="2-%%c<>")
    s.note((0, -30), "t=5  MATERIAL: AL5052")
    return s, {"bbox": [L, W, t], "holes": [{"axis": "Z", "diameter": d, "positions": pos}],
               "volume": L * W * t - 2 * math.pi * (d / 2) ** 2 * t}


def broken_dim_override():
    """Length drawn as 80 but the dimension text says 85 (manually overridden)."""
    s, exp = block_basic()
    s.title = "BROKEN_DIM_OVERRIDE"
    for e in list(s.msp.query("DIMENSION")):
        if abs(e.get_measurement() - 80.0) < 1e-6:
            s.msp.delete_entity(e)
    s.hdim((0, 0), (80, 0), -24, text="85")
    exp["expect_issue"] = "E107"
    return s, exp


def block_side_holes():
    """Holes drilled from the side: Ø10 through along Y (front view circle), Ø8 x 25 deep along X
    from the right face (side view circle, M-less), plus a Ø6 through hole from the top."""
    L, W, H = 80.0, 40.0, 30.0
    s = Sheet("BLOCK_SIDE_HOLES")
    ty, rx = H + GAP, L + GAP
    # front view (x = X, y = Z)
    s.rect(0, 0, L, H)
    s.circle((20, 15), 5)
    s.center_mark((20, 15), 5)
    s.hidden_h(11, 55, L)                         # Ø8 blind hole from the right face
    s.hidden_h(19, 55, L)
    s.hidden_v(55, 11, 19)
    for x in (37, 43):                            # Ø6 from the top
        s.hidden_v(x, 0, H)
    # top view (x = X, y = Y)
    s.rect(0, ty, L, W)
    for x in (15, 25):                            # Ø10 through along Y
        s.hidden_v(x, ty, ty + W)
    s.hidden_h(ty + 16, 55, L)                    # Ø8 along X
    s.hidden_h(ty + 24, 55, L)
    s.hidden_v(55, ty + 16, ty + 24)
    s.circle((40, ty + 30), 3)
    s.center_mark((40, ty + 30), 3)
    # right view (x = Y, y = Z)
    s.rect(rx, 0, W, H)
    s.circle((rx + 20, 15), 4)
    s.center_mark((rx + 20, 15), 4)
    s.hidden_h(10, rx, rx + W)                    # Ø10 along Y seen from the side
    s.hidden_h(20, rx, rx + W)
    for y in (27, 33):
        s.hidden_v(rx + y, 0, H)
    # dimensions
    s.hdim((0, 0), (L, 0), -12)
    s.vdim((L, 0), (L, H), L + 12)
    s.vdim((0, ty), (0, ty + W), -12)
    s.hdim((0, H), (20, 15), H + 8)
    s.vdim((0, 0), (20, 15), -24)
    s.ddim((20, 15), 5, angle=45, text="%%c<>")
    s.ddim((rx + 20, 15), 4, angle=45, text="%%c<> DP25")
    s.hdim((rx, H), (rx + 20, 15), H + 8)
    s.hdim((55, 19), (L, 19), 19 + 6 if False else H + 16)
    s.ddim((40, ty + 30), 3, angle=45, text="%%c<>")
    s.hdim((0, ty + W), (40, ty + 30), ty + W + 10)
    s.vdim((L, ty), (40, ty + 30), L + 12)
    vol = L * W * H - math.pi * 25 * W - math.pi * 16 * 25 - math.pi * 9 * H
    return s, {"bbox": [L, W, H],
               "holes": [{"axis": "Z", "diameter": 6.0, "positions": [[40, 30]]},
                         {"axis": "Y", "diameter": 10.0, "positions": [[20, 15]], "depth": "through"},
                         {"axis": "X", "diameter": 8.0, "positions": [[20, 15]], "depth": 25.0}],
               "volume": vol}


def plate_tapped():
    """4x M6 tapped through holes (tap-drill circle Ø5 + thin 3/4 thread arc Ø6, callout '4-M6')
    and one Ø8 plain hole."""
    L, W, T = 60.0, 40.0, 10.0
    s = Sheet("PLATE_TAPPED")
    if "THIN" not in s.doc.layers:
        s.doc.layers.add("THIN", color=3, linetype="CONTINUOUS")
    ty, rx = T + GAP, L + GAP
    pos = [[10, 10], [10, 30], [50, 10], [50, 30]]
    s.rect(0, 0, L, T)
    for x in (10, 50):
        for dx in (-2.5, 2.5):
            s.hidden_v(x + dx, 0, T)
        s.line((x, -3), (x, T + 3), "CENTER")
    for dx in (-4, 4):
        s.hidden_v(30 + dx, 0, T)
    s.rect(0, ty, L, W)
    for x, y in pos:
        s.circle((x, ty + y), 2.5)
        s.arc((x, ty + y), 3.0, 0, 270, "THIN")          # thread major diameter, 3/4 circle
        s.center_mark((x, ty + y), 3)
    s.circle((30, ty + 20), 4)
    s.center_mark((30, ty + 20), 4)
    s.rect(rx, 0, W, T)
    s.hdim((0, 0), (L, 0), -12)
    s.vdim((L, 0), (L, T), L + 12)
    s.vdim((0, ty), (0, ty + W), -14)
    s.hdim((0, ty + W), (10, ty + 30), ty + W + 10)
    s.hdim((10, ty + 30), (50, ty + 30), ty + W + 10)
    s.vdim((L, ty), (50, ty + 10), L + 12)
    s.vdim((50, ty + 10), (50, ty + 30), L + 12)
    s.ddim((50, ty + 30), 3.0, angle=45, text="4-M6")
    s.ddim((30, ty + 20), 4, angle=45, text="%%c<>")
    vol = L * W * T - 4 * math.pi * 2.5 ** 2 * T - math.pi * 16 * T
    return s, {"bbox": [L, W, T],
               "holes": [{"axis": "Z", "diameter": 5.0, "positions": pos, "thread": "M6x1"},
                         {"axis": "Z", "diameter": 8.0, "positions": [[30, 20]]}],
               "volume": vol}


# ---------------------------------------------------------------------------- turned parts (sections)
def _pappus(prof):
    a = cy = 0.0
    for i in range(len(prof)):
        (x0, y0), (x1, y1) = prof[i], prof[(i + 1) % len(prof)]
        f = x0 * y1 - x1 * y0
        a += f
        cy += (y0 + y1) * f
    a /= 2
    return 2 * math.pi * abs(a) * (cy / (6 * a))


def _key_removed(rb, w, top, length):
    h = w / 2
    inside = 2 * (h / 2 * math.sqrt(rb * rb - h * h) + rb * rb / 2 * math.asin(h / rb))
    return length * (w * top - inside)


def _draw_section(s, upper_hatch, lower_hatch, extra_upper=(), extra_lower=(), length=0.0):
    """Hatch + outline of both halves. Polygons in (u, r) with r >= 0 for the upper half."""
    edges = set()

    def add_poly(poly, sign):
        pts = [(u, sign * r) for u, r in poly]
        s.hatch(pts)
        for a, b in zip(pts, pts[1:] + pts[:1]):
            edges.add(tuple(sorted((tuple(round(c, 6) for c in a), tuple(round(c, 6) for c in b)))))

    for poly in upper_hatch:
        add_poly(poly, 1)
    for poly in lower_hatch:
        add_poly(poly, -1)
    for sign, extra in ((1, extra_upper), (-1, extra_lower)):
        for a, b in extra:
            edges.add(tuple(sorted(((a[0], sign * a[1]), (b[0], sign * b[1])))))
    for a, b in sorted(edges):
        s.line(a, b)
    s.line((-6, 0), (length + 6, 0), "CENTER")


def bushing_section():
    """Flanged bushing: chained axial dimensions and a 1x45 bore chamfer."""
    prof = [(0, 9), (1, 8), (50, 8), (50, 12.5), (10, 12.5), (10, 20), (0, 20)]
    s = Sheet("BUSHING_SECTION")
    _draw_section(s, [prof], [prof], length=50)
    s.hdim((0, -20), (10, -20), -30)
    s.hdim((10, -12.5), (50, -12.5), -30)                    # chained from the flange face
    s.hdim((0, 9), (1, 8), 28, text="1x45%%d")
    s.vdim((0, -20), (0, 20), -12, text="%%c<>")
    s.vdim((50, -12.5), (50, 12.5), 62, text="%%c<>")
    s.vdim((30, -8), (30, 8), 30, text="%%c<>")
    cx = 100
    for r in (20, 12.5, 9, 8):
        s.circle((cx, 0), r)
    s.center_mark((cx, 0), 20)
    return s, {"kind": "turned", "bbox": [50, 40, 40], "profile": prof, "volume": _pappus(prof), "keyway": None}


def pulley_keyway_section():
    """V-groove pulley with a keyway: the groove bottom has no diameter (60 deg flanks),
    the keyway shows in the upper half of the section and as a notch in the end view."""
    g = 30 - 6 * math.tan(math.radians(60))
    prof = [(0, 10), (36, 10), (36, 30), (28, 30), (22, g), (14, g), (8, 30), (0, 30)]
    top, w = 12.8, 6.0
    upper = [(0, top), (36, top), (36, 30), (28, 30), (22, g), (14, g), (8, 30), (0, 30)]
    band = [((0, 10), (36, 10)), ((0, 10), (0, top)), ((36, 10), (36, top))]   # bore seen behind the keyway
    s = Sheet("PULLEY_KEYWAY_SECTION")
    _draw_section(s, [upper], [prof], extra_upper=band, length=36)
    for k, x in enumerate((8, 14, 22, 28, 36)):
        s.hdim((0, -30), (x, -30 if x in (8, 28, 36) else -g), -38 - 7 * k)
    s.vdim((0, -30), (0, 30), -12, text="%%c<>")
    s.vdim((5, -10), (5, -10 + 20), 5, text="%%c<>")
    cx = 100
    s.circle((cx, 0), 30)
    yk = math.sqrt(10 * 10 - (w / 2) ** 2)
    a = math.degrees(math.atan2(yk, w / 2))
    s.arc((cx, 0), 10, 180 - a, 360 + a)
    s.line((cx - w / 2, yk), (cx - w / 2, top))
    s.line((cx - w / 2, top), (cx + w / 2, top))
    s.line((cx + w / 2, top), (cx + w / 2, yk))
    s.center_mark((cx, 0), 30)
    s.hdim((cx - w / 2, top), (cx + w / 2, top), top + 8)
    s.vdim((cx, -10), (cx + w / 2, top), cx + 42)
    vol = _pappus(prof) - _key_removed(10, w, top, 36)
    return s, {"kind": "turned", "bbox": [36, 60, 60], "profile": prof, "volume": vol,
               "keyway": {"width": w, "top": top}}


def gear_blank_section():
    """Spur gear blank: hub, web and rim; the tooth band (Ø92..Ø100) is drawn but not hatched."""
    prof = [(0, 6), (24, 6), (24, 15), (16, 15), (16, 40), (22, 40), (22, 50), (2, 50), (2, 40), (8, 40), (8, 15), (0, 15)]
    hatch = [(0, 6), (24, 6), (24, 15), (16, 15), (16, 40), (22, 40), (22, 46), (2, 46), (2, 40), (8, 40), (8, 15), (0, 15)]
    teeth = [((2, 46), (2, 50)), ((2, 50), (22, 50)), ((22, 50), (22, 46))]
    s = Sheet("GEAR_BLANK_SECTION")
    _draw_section(s, [hatch], [hatch], extra_upper=teeth, extra_lower=teeth, length=24)
    for k, (x, y) in enumerate(((2, -50), (8, -15), (16, -15), (22, -50), (24, -6))):
        s.hdim((0, -15 if x != 2 and x != 22 else -50), (x, y), -58 - 7 * k)
    for k, (x, d) in enumerate(((12, 100), (-10, 92), (4, 80), (-4, 30))):
        s.vdim((x, -d / 2), (x, d / 2), -20 - 8 * k, text="%%c<>")
    s.vdim((20, -6), (20, 6), 32, text="%%c<>")
    cx = 110
    for r, layer in ((50, "OUTLINE"), (46, "CENTER"), (15, "OUTLINE"), (6, "OUTLINE")):
        s.circle((cx, 0), r, layer)
    s.center_mark((cx, 0), 50)
    return s, {"kind": "turned", "bbox": [24, 100, 100], "profile": prof, "volume": _pappus(prof), "keyway": None}


def broken_section_dim():
    """Bushing whose body diameter text says 28 but is drawn 25."""
    s, exp = bushing_section()
    s.title = "BROKEN_SECTION_DIM"
    for e in list(s.msp.query("DIMENSION")):
        if abs(e.get_measurement() - 25.0) < 1e-6:
            s.msp.delete_entity(e)
    s.vdim((50, -12.5), (50, 12.5), 62, text="%%c28")
    exp["expect_issue"] = "E107"
    return s, exp


def broken_keyway_noview():
    """Pulley section without the end view: the keyway width cannot be known -> question."""
    s, exp = pulley_keyway_section()
    s.title = "BROKEN_KEYWAY_NOVIEW"
    for e in list(s.msp):
        t = e.dxftype()
        pts = []
        if t == "LINE":
            pts = [e.dxf.start.x, e.dxf.end.x]
        elif t in ("CIRCLE", "ARC"):
            pts = [e.dxf.center.x]
        elif t == "DIMENSION":
            pts = [e.dxf.defpoint.x]
        if pts and min(pts) > 60 and e.dxf.layer != "TITLE":
            s.msp.delete_entity(e)
    exp["expect_issue"] = "E202"
    return s, exp


PARTS = {
    "block_basic": block_basic,
    "plate_holes": plate_holes,
    "bracket_l": bracket_l,
    "stepped_block": stepped_block,
    "flange": flange,
    "plate_one_view": plate_one_view,
    "broken_dim_override": broken_dim_override,
    "block_side_holes": block_side_holes,
    "plate_tapped": plate_tapped,
    "bushing_section": bushing_section,
    "pulley_keyway_section": pulley_keyway_section,
    "gear_blank_section": gear_blank_section,
    "broken_section_dim": broken_section_dim,
    "broken_keyway_noview": broken_keyway_noview,
}


def main(argv):
    os.makedirs(OUT, exist_ok=True)
    names = argv or list(PARTS)
    for name in names:
        sheet, expected = PARTS[name]()
        path = os.path.join(OUT, name + ".dxf")
        sheet.finish(path)
        with open(os.path.join(OUT, name + ".expected.json"), "w", encoding="utf-8") as f:
            json.dump(expected, f, indent=2)
        print("wrote", os.path.relpath(path, ROOT))


if __name__ == "__main__":
    main(sys.argv[1:])
