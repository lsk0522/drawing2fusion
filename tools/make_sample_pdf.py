"""Render the sample DXF drawings as vector PDFs the way CAD programs export them.

    python tools/make_sample_pdf.py              -> samples/drawings/pdf/*.pdf (+ expected json)

Real text (not glyph outlines), line widths per layer (outline 0.5 mm, hidden 0.35, thin 0.25),
filled arrowheads, circles as 4 Bezier curves, section hatching as individual thin lines.
Two dash styles are produced because exporters differ:
  * dash array  - one path per line with a PDF dash pattern (Fusion / AutoCAD style)
  * segments    - every dash is its own short line (what many "plot to PDF" drivers do)
One drawing is also exported at 1:2 to exercise the scale detection.
"""
import json
import math
import os
import shutil
import sys

import ezdxf
from ezdxf.render import hatching
import pymupdf

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "samples", "drawings", "dxf")
OUT = os.path.join(ROOT, "samples", "drawings", "pdf")
PT = 72.0 / 25.4

STYLE = {  # layer: (width mm, dash pattern mm or None)
    "OUTLINE": (0.5, None), "HIDDEN": (0.35, (3.0, 1.5)), "CENTER": (0.25, (8.0, 1.5, 1.5, 1.5)),
    "DIM": (0.25, None), "THIN": (0.25, None), "TEXT": (0.25, None), "TITLE": (0.35, None), "HATCH": (0.18, None),
}
TEXT_MM = 3.0
ARROW_L, ARROW_W = 2.5, 0.9

CASES = [  # (dxf name, output name, dash style, paper scale)
    ("plate_holes", "plate_holes", "array", 1.0),
    ("bracket_l", "bracket_l", "segments", 1.0),
    ("stepped_block", "stepped_block", "array", 1.0),
    ("flange", "flange_1to2", "array", 0.5),
    ("plate_one_view", "plate_one_view", "segments", 1.0),
    ("block_side_holes", "block_side_holes", "array", 1.0),
    ("plate_tapped", "plate_tapped", "segments", 1.0),
    ("bushing_section", "bushing_section", "array", 1.0),
    ("pulley_keyway_section", "pulley_keyway_section", "segments", 1.0),
    ("gear_blank_section", "gear_blank_section", "array", 1.0),
]


def _dim_text(dim):
    t = dim.dxf.get("text", "") or "<>"
    m = dim.get_measurement()
    val = ("%.3f" % m).rstrip("0").rstrip(".")
    t = t.replace("<>", val).replace("%%c", "Ø").replace("%%C", "Ø").replace("%%d", "°").replace("%%p", "±")
    return t


class Pdf:
    def __init__(self, ext, scale, style):
        self.scale, self.style = scale, style
        self.k = PT * scale
        self.x0, self.y0 = ext[0], ext[1]
        self.m = 15 * PT
        w = (ext[2] - ext[0]) * self.k + 2 * self.m
        h = (ext[3] - ext[1]) * self.k + 2 * self.m
        self.doc = pymupdf.open()
        self.page = self.doc.new_page(width=w, height=h)
        self.h = h

    def p(self, x, y):
        return pymupdf.Point((x - self.x0) * self.k + self.m, self.h - ((y - self.y0) * self.k + self.m))

    def _dashes(self, pat):
        return "[%s] 0" % " ".join("%.3f" % (v * PT) for v in pat)

    def line(self, a, b, layer):
        w, pat = STYLE.get(layer, (0.25, None))
        if pat and self.style == "segments":
            L = math.dist(a, b) * self.scale          # pattern is in paper mm
            if L < 1e-9:
                return
            pos, k, on = 0.0, 0, True
            sh = self.page.new_shape()
            while pos < L:
                seg = pat[k % len(pat)]
                end = min(L, pos + seg)
                if on:
                    t0, t1 = pos / L, end / L
                    sh.draw_line(self.p(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                                 self.p(a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1))
                pos, k, on = end, k + 1, not on
            sh.finish(color=(0, 0, 0), width=w * PT, closePath=False)
            sh.commit()
            return
        sh = self.page.new_shape()
        sh.draw_line(self.p(*a), self.p(*b))
        sh.finish(color=(0, 0, 0), width=w * PT, dashes=self._dashes(pat) if pat else None, closePath=False)
        sh.commit()

    def arc(self, c, r, a0, a1, layer):
        w, pat = STYLE.get(layer, (0.25, None))
        span = (a1 - a0) % 360 or 360.0
        if pat and self.style == "segments":
            n = max(8, int(span / 4))
            pts = [(c[0] + r * math.cos(math.radians(a0 + span * i / n)), c[1] + r * math.sin(math.radians(a0 + span * i / n)))
                   for i in range(n + 1)]
            sh = self.page.new_shape()
            for i, (a, b) in enumerate(zip(pts, pts[1:])):
                if i % 2 == 0:
                    sh.draw_line(self.p(*a), self.p(*b))
            sh.finish(color=(0, 0, 0), width=w * PT, closePath=False)
            sh.commit()
            return
        sh = self.page.new_shape()
        n = max(1, int(math.ceil(span / 90.0)))
        d = math.radians(span / n)
        kk = 4.0 / 3.0 * math.tan(d / 4)
        for i in range(n):
            t0 = math.radians(a0) + i * d
            t1 = t0 + d
            p0 = (c[0] + r * math.cos(t0), c[1] + r * math.sin(t0))
            p3 = (c[0] + r * math.cos(t1), c[1] + r * math.sin(t1))
            p1 = (p0[0] - kk * r * math.sin(t0), p0[1] + kk * r * math.cos(t0))
            p2 = (p3[0] + kk * r * math.sin(t1), p3[1] - kk * r * math.cos(t1))
            sh.draw_bezier(self.p(*p0), self.p(*p1), self.p(*p2), self.p(*p3))
        sh.finish(color=(0, 0, 0), width=w * PT, dashes=self._dashes(pat) if pat else None, closePath=False)
        sh.commit()

    def arrow(self, tip, ang):
        L, W = ARROW_L / self.scale, ARROW_W / self.scale     # paper size, not model size
        dx, dy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        bx, by = tip[0] - dx * L, tip[1] - dy * L
        pts = [self.p(*tip), self.p(bx - dy * W / 2, by + dx * W / 2), self.p(bx + dy * W / 2, by - dx * W / 2)]
        sh = self.page.new_shape()
        sh.draw_polyline(pts + [pts[0]])
        sh.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.1, closePath=True)
        sh.commit()

    def text(self, pos, text, rot=0.0, h=TEXT_MM, anchor="center"):
        """h = character height on paper (mm)."""
        fs = h * PT * 1.35
        tw = pymupdf.get_text_length(text, fontname="helv", fontsize=fs)
        q = self.p(*pos)
        rot = int(round(rot / 90.0)) % 4 * 90
        if anchor == "left":
            off = 0
        else:
            off = tw / 2
        if rot == 0:
            pt = pymupdf.Point(q.x - off, q.y + fs * 0.35)
        elif rot == 90:
            pt = pymupdf.Point(q.x + fs * 0.35, q.y + off)
        elif rot == 180:
            pt = pymupdf.Point(q.x + off, q.y - fs * 0.35)
        else:
            pt = pymupdf.Point(q.x - fs * 0.35, q.y - off)
        self.page.insert_text(pt, text, fontsize=fs, fontname="helv", rotate=rot)


def render(name, out_name, style, scale):
    doc = ezdxf.readfile(os.path.join(SRC, name + ".dxf"))
    msp = doc.modelspace()
    xs, ys = [], []
    for e in msp:
        try:
            for v in (e.virtual_entities() if e.dxftype() == "DIMENSION" else [e]):
                if v.dxftype() == "LINE":
                    xs += [v.dxf.start.x, v.dxf.end.x]
                    ys += [v.dxf.start.y, v.dxf.end.y]
                elif v.dxftype() in ("CIRCLE", "ARC"):
                    xs += [v.dxf.center.x - v.dxf.radius, v.dxf.center.x + v.dxf.radius]
                    ys += [v.dxf.center.y - v.dxf.radius, v.dxf.center.y + v.dxf.radius]
                elif v.dxftype() in ("TEXT", "MTEXT"):
                    xs.append(v.dxf.insert.x)
                    ys.append(v.dxf.insert.y)
        except Exception:
            pass
    pdf = Pdf((min(xs), min(ys), max(xs), max(ys)), scale, style)
    for e in msp:
        t, layer = e.dxftype(), e.dxf.layer
        if t == "LINE":
            pdf.line(tuple(e.dxf.start)[:2], tuple(e.dxf.end)[:2], layer)
        elif t == "CIRCLE":
            pdf.arc(tuple(e.dxf.center)[:2], e.dxf.radius, 0, 360, layer)
        elif t == "ARC":
            pdf.arc(tuple(e.dxf.center)[:2], e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle, layer)
        elif t == "TEXT":
            pdf.text(tuple(e.dxf.insert)[:2], e.dxf.text, e.dxf.get("rotation", 0), e.dxf.height * scale, anchor="left")
        elif t == "HATCH":
            for a, b in hatching.hatch_entity(e):
                pdf.line((a.x, a.y), (b.x, b.y), "HATCH")
        elif t == "DIMENSION":
            txt = _dim_text(e)
            parts = list(e.virtual_entities())
            ins = [tuple(v.dxf.insert)[:2] for v in parts if v.dxftype() == "INSERT"]
            mid = (sum(p[0] for p in ins) / len(ins), sum(p[1] for p in ins) / len(ins)) if ins else (0, 0)
            for v in parts:
                vt = v.dxftype()
                if vt == "LINE":
                    pdf.line(tuple(v.dxf.start)[:2], tuple(v.dxf.end)[:2], "DIM")
                elif vt == "INSERT":
                    tip = tuple(v.dxf.insert)[:2]
                    # tick blocks carry one rotation for both ends; arrows point away from the middle
                    ang = math.degrees(math.atan2(tip[1] - mid[1], tip[0] - mid[0])) if len(ins) == 2 \
                        and math.dist(tip, mid) > 1e-9 else v.dxf.rotation
                    pdf.arrow(tip, ang)
                elif vt == "MTEXT":
                    rot = v.dxf.get("rotation", 0) or 0
                    pos = tuple(v.dxf.insert)[:2]
                    # lift the text off the dimension line like a CAD exporter does
                    n = math.radians(rot + 90)
                    lift = 0.8 * TEXT_MM / scale
                    pdf.text((pos[0] + math.cos(n) * lift, pos[1] + math.sin(n) * lift), txt, rot, TEXT_MM)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, out_name + ".pdf")
    pdf.doc.save(path)
    exp = os.path.join(SRC, name + ".expected.json")
    if os.path.exists(exp):
        data = json.load(open(exp, encoding="utf-8"))
        data["source_dxf"] = name + ".dxf"
        data["pdf"] = {"dash_style": style, "paper_scale": scale}
        with open(os.path.join(OUT, out_name + ".expected.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    return path


def main(argv):
    for name, out_name, style, scale in CASES:
        if argv and out_name not in argv:
            continue
        print("wrote", os.path.relpath(render(name, out_name, style, scale), ROOT))


if __name__ == "__main__":
    main(sys.argv[1:])
