"""reading.png - what the reader took from the drawing, drawn over the drawing itself.

    green  dimension used for a parameter (label: parameter = value)
    red    dimension whose text and drawn length disagree (E107)
    orange dimension read at a detail view's scale
    grey   dimension not used (listed in part-spec notes as "미사용 치수")
    blue boxes: the views the reader named (front / top / side); dashed grey: views it left out

The Fusion panel shows it after every run, so a person sees at a glance which numbers the model
came from and which ones nobody looked at.
"""

import importlib
import math
import os
import subprocess
import sys

COLORS = {"used": (26, 127, 55), "conflict": (198, 40, 40), "detail": (230, 120, 0), "unused": (140, 140, 140)}
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\malgun.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _pil(auto_install=True):
    try:
        return importlib.import_module("PIL.Image")
    except ImportError:
        pass
    if not auto_install:
        return None
    for args in ([sys.executable, "-m", "pip", "install", "--user", "-q", "pillow"],
                 [sys.executable, "-m", "pip", "install", "-q", "pillow"]):
        try:
            subprocess.check_call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        try:
            import site
            site.addsitedir(site.getusersitepackages())
        except Exception:
            pass
        importlib.invalidate_caches()
        try:
            return importlib.import_module("PIL.Image")
        except ImportError:
            continue
    return None


def _font(size):
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _arc_points(c, r, a0, a1, n=None):
    span = (a1 - a0) % 360 or 360.0
    n = n or max(8, int(span / 6))
    return [(c[0] + r * math.cos(math.radians(a0 + span * k / n)), c[1] + r * math.sin(math.radians(a0 + span * k / n)))
            for k in range(n + 1)]


def render(data, dest, width=1800, auto_install=True):
    """data: report["_overlay"] from the reader. Returns dest, or None when Pillow is missing."""
    Image = _pil(auto_install)
    if Image is None or not data or not data.get("segs"):
        return None
    from PIL import ImageDraw
    xs, ys = [], []
    for s in data["segs"]:
        if s[0] == "l":
            xs += [s[1], s[3]]
            ys += [s[2], s[4]]
        else:
            xs += [s[1] - s[3], s[1] + s[3]]
            ys += [s[2] - s[3], s[2] + s[3]]
    for d in data.get("dims", []):
        for p in (d.get("line") or []) + [d.get("label_at") or d["points"][0]]:
            xs.append(p[0])
            ys.append(p[1])
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    pad, head = 40, 70
    k = (width - 2 * pad) / max(x1 - x0, 1e-9)
    height = int((y1 - y0) * k + 2 * pad + head)
    if height > 2600:                                   # very tall sheets: fit the height instead
        k *= 2600 / height
        height = 2600
    img = Image.new("RGB", (width, height), "white")
    dr = ImageDraw.Draw(img)

    def P(p):
        return (pad + (p[0] - x0) * k, height - pad - (p[1] - y0) * k)

    def dashed(pts, fill, w=1, on=7, off=5):
        for a, b in zip(pts, pts[1:]):
            L = math.dist(P(a), P(b))
            if L < 1e-6:
                continue
            pa, pb = P(a), P(b)
            t = 0.0
            while t < L:
                t1 = min(L, t + on)
                dr.line([(pa[0] + (pb[0] - pa[0]) * t / L, pa[1] + (pb[1] - pa[1]) * t / L),
                         (pa[0] + (pb[0] - pa[0]) * t1 / L, pa[1] + (pb[1] - pa[1]) * t1 / L)], fill=fill, width=w)
                t = t1 + off

    # views
    for name, bb in (data.get("views") or {}).items():
        dr.rectangle([P((bb[0], bb[3])), P((bb[2], bb[1]))], outline=(120, 160, 230), width=2)
        dr.text((P((bb[0], bb[3]))[0], P((bb[0], bb[3]))[1] - 22), name, fill=(60, 100, 200), font=_font(18))
    for bb in data.get("unused_views") or []:
        a, b = P((bb[0], bb[3])), P((bb[2], bb[1]))
        dashed([(bb[0], bb[1]), (bb[2], bb[1]), (bb[2], bb[3]), (bb[0], bb[3]), (bb[0], bb[1])], (170, 170, 170), 1)
        dr.text((a[0], a[1] - 20), "(제외된 뷰)", fill=(150, 150, 150), font=_font(15))
    # geometry
    for s in data["segs"]:
        hidden, center = s[-2], s[-1]
        col = (40, 40, 40) if not (hidden or center) else ((110, 110, 110) if hidden else (90, 140, 220))
        if s[0] == "l":
            pts = [(s[1], s[2]), (s[3], s[4])]
        elif s[0] == "c":
            pts = _arc_points((s[1], s[2]), s[3], 0, 360)
        else:
            pts = _arc_points((s[1], s[2]), s[3], s[4], s[5])
        if hidden or center:
            dashed(pts, col, 1, *((6, 4) if hidden else (14, 4)))
        else:
            dr.line([P(p) for p in pts], fill=col, width=2)
    # dimensions
    f = _font(17)
    counts = {}
    for d in data.get("dims", []):
        col = COLORS.get(d["status"], COLORS["unused"])
        counts[d["status"]] = counts.get(d["status"], 0) + 1
        a, b = d.get("line") or (d["points"][0], d["points"][0])
        if math.dist(a, b) > 1e-9:
            dr.line([P(a), P(b)], fill=col, width=3)
            for q in (a, b):
                c = P(q)
                dr.ellipse([c[0] - 4, c[1] - 4, c[0] + 4, c[1] + 4], fill=col)
        label = d["text"]
        if d.get("param"):
            label = "%s = %s" % (d["param"], d["text"])
        t = P(d.get("label_at") or d["points"][0])
        tw = dr.textlength(label, font=f) if hasattr(dr, "textlength") else 8 * len(label)
        dr.rectangle([t[0] - 3, t[1] - 12, t[0] + tw + 3, t[1] + 12], fill=(255, 255, 255))
        dr.text((t[0], t[1] - 11), label, fill=col, font=f)
    # legend
    lf = _font(20)
    x = pad
    title = "도면 해석 — %s" % data.get("name", "")
    dr.text((x, 14), title, fill="black", font=_font(24))
    x = pad
    for key, word in (("used", "파라미터로 사용"), ("conflict", "문자와 형상 불일치"), ("detail", "상세도 축척"),
                      ("unused", "사용 안 함")):
        dr.rectangle([x, 48, x + 18, 64], fill=COLORS[key])
        txt = "%s %d" % (word, counts.get(key, 0))
        dr.text((x + 24, 44), txt, fill=(60, 60, 60), font=lf)
        x += 34 + int(dr.textlength(txt, font=lf) if hasattr(dr, "textlength") else 10 * len(txt))
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    img.save(dest)
    return dest
