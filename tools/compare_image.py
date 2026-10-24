"""Side-by-side sheet: original drawing  |  Fusion snapshots  +  check results.

    python tools/compare_image.py <drawing.png|jpg> <out/<name>> [-o compare.png]

<out/<name>> is the folder `d2f run` / `d2f build` wrote (snapshots/*.png and
verify-report.json). The sheet is written to <out/<name>>/compare.png by default.
Used by the chat-reading procedure (docs/image-reading.md) so a person can check
at a glance that the model matches the drawing.
"""
import argparse
import importlib
import json
import os
import subprocess
import sys

VIEWS = ("front", "top", "right", "iso")
VIEW_LABEL = {"front": "정면 (Front)", "top": "평면 (Top)", "right": "우측면 (Right)", "iso": "등각 (Iso)"}
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _pil():
    try:
        return importlib.import_module("PIL.Image")
    except ImportError:
        pass
    print("ℹ️ 비교 이미지를 만들려면 Pillow 패키지가 필요해서 설치합니다 (한 번만) ...")
    for args in ([sys.executable, "-m", "pip", "install", "--user", "pillow"],
                 [sys.executable, "-m", "pip", "install", "pillow"]):
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
        try:
            return importlib.import_module("PIL.Image")
        except ImportError:
            continue
    raise SystemExit("🔴 Pillow 설치에 실패했습니다: pip install pillow")


def _font(size):
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _fit(img, w, h):
    img = img.copy()
    img.thumbnail((w, h))
    return img


def _crop_white(img, pad=12):
    """Snapshots have a lot of empty background: trim to the part."""
    from PIL import ImageChops, Image
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    box = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 18 else 0).getbbox()
    if not box:
        return rgb
    x0, y0, x1, y1 = box
    return rgb.crop((max(0, x0 - pad), max(0, y0 - pad), min(rgb.width, x1 + pad), min(rgb.height, y1 + pad)))


def trim_capture(img):
    """Phone screenshots: keep the largest run of mostly-bright rows (the drawing paper),
    dropping the status bar and black letterbox bands. Returns img unchanged if unsure."""
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    step = max(1, w // 200)
    bright = []
    for y in range(h):
        n = sum(1 for x in range(0, w, step) if px[x, y] > 200)
        bright.append(n / len(range(0, w, step)) > 0.5)
    # drawing lines crossing a row make it dark: bridge short dark gaps inside the paper
    gap_max, y = max(3, h // 100), 0
    while y < h:
        if not bright[y]:
            e = y
            while e < h and not bright[e]:
                e += 1
            if 0 < y and e < h and e - y <= gap_max:
                bright[y:e] = [True] * (e - y)
            y = e
        else:
            y += 1
    best, start = (0, 0, h), None
    for y, b in enumerate(bright + [False]):
        if b and start is None:
            start = y
        elif not b and start is not None:
            if y - start > best[0]:
                best = (y - start, start, y)
            start = None
    run, y0, y1 = best
    if run < h * 0.25 or run > h * 0.97:
        return img
    return img.crop((0, y0, w, y1))


def check_lines(report):
    rows = report.get("checks") or []
    lines = []
    for r in rows:
        if r.get("expected") is None and r.get("pass") is None:
            continue
        mark = "[OK]" if r.get("pass") else ("[--]" if r.get("pass") is None else "[NG]")
        label = r.get("label") or r.get("check")
        if r.get("expected") is not None and r.get("actual") is not None:
            lines.append((r.get("pass"), "%s %s: %s / 실측 %s" % (mark, label, _n(r["expected"]), _n(r["actual"]))))
        else:
            lines.append((r.get("pass"), "%s %s" % (mark, r.get("message", label))))
    return lines


def _n(v):
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".")
    return str(v)


def build_sheet(drawing_path, out_dir, dest=None, width=2000, crop=None, trim=True):
    Image = _pil()
    from PIL import ImageDraw
    report_path = os.path.join(out_dir, "verify-report.json")
    report = json.load(open(report_path, encoding="utf-8")) if os.path.exists(report_path) else {}
    snaps = {v: os.path.join(out_dir, "snapshots", v + ".png") for v in VIEWS}
    snaps = {v: p for v, p in snaps.items() if os.path.exists(p)}
    if not snaps:
        raise SystemExit("🔴 %s/snapshots 에 스냅샷이 없습니다 (먼저 run.bat 으로 빌드하세요)" % out_dir)

    title_f, label_f, text_f = _font(34), _font(24), _font(20)
    margin, gap = 30, 20
    col = (width - 2 * margin - gap) // 2
    cell = (col - gap) // 2

    if drawing_path.lower().endswith(".pdf"):
        # vector PDF drawing: render the first page (PyMuPDF is installed by the PDF reader)
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from d2f.extract.pdf_reader import ensure_pymupdf
        pm = ensure_pymupdf()
        pix = pm.open(drawing_path)[0].get_pixmap(dpi=150)
        import io as _io
        drawing = Image.open(_io.BytesIO(pix.tobytes("png"))).convert("RGB")
        trim = False
    else:
        drawing = Image.open(drawing_path).convert("RGB")
    if crop:
        drawing = drawing.crop(tuple(int(v) for v in crop.split(",")))
    elif trim:
        drawing = trim_capture(drawing)
    left = _fit(drawing, col, int(col * 1.25))
    grid_h = 2 * (cell + 34) + gap

    lines = check_lines(report)
    passed, total = report.get("passed"), report.get("total")
    line_h = 28
    per_col = max(1, (len(lines) + 1) // 2)
    text_h = 60 + per_col * line_h if lines else 0
    height = margin + 50 + max(left.height + 34, grid_h) + gap + text_h + margin

    sheet = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(sheet)
    name = os.path.basename(os.path.normpath(out_dir))
    head = "Drawing2Fusion 비교 — %s" % name
    if total:
        head += "   ·   치수 검증 %s/%s 통과" % (passed, total)
    d.text((margin, margin), head, fill="black", font=title_f)
    y0 = margin + 50

    d.text((margin, y0), "원본 도면", fill="#444", font=label_f)
    sheet.paste(left, (margin, y0 + 34))

    x0 = margin + col + gap
    for i, v in enumerate(VIEWS):
        cx = x0 + (i % 2) * (cell + gap)
        cy = y0 + (i // 2) * (cell + 34 + gap)
        d.text((cx, cy), VIEW_LABEL[v], fill="#444", font=label_f)
        d.rectangle((cx, cy + 34, cx + cell, cy + 34 + cell), outline="#ccc")
        if v in snaps:
            img = _fit(_crop_white(Image.open(snaps[v])), cell - 8, cell - 8)
            sheet.paste(img, (cx + (cell - img.width) // 2, cy + 34 + (cell - img.height) // 2))
        else:
            d.text((cx + 10, cy + 44), "(없음)", fill="#999", font=text_f)

    if lines:
        ty = y0 + max(left.height + 34, grid_h) + gap
        d.line((margin, ty, width - margin, ty), fill="#ddd", width=2)
        d.text((margin, ty + 12), "검증 항목", fill="#444", font=label_f)
        for k, (ok, text) in enumerate(lines):
            cx = margin + (k // per_col) * (col + gap)
            cy = ty + 52 + (k % per_col) * line_h
            colr = "#1a7f37" if ok else ("#666" if ok is None else "#c62828")
            d.text((cx, cy), text, fill=colr, font=text_f)

    dest = dest or os.path.join(out_dir, "compare.png")
    sheet.save(dest)
    return dest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("drawing", help="원본 도면 이미지 (png/jpg) 또는 벡터 PDF")
    p.add_argument("out_dir", help="빌드 결과 폴더 (out/<이름>)")
    p.add_argument("-o", "--output")
    p.add_argument("--width", type=int, default=2000)
    p.add_argument("--crop", metavar="X0,Y0,X1,Y1", help="도면 이미지에서 이 영역만 사용")
    p.add_argument("--no-trim", action="store_true", help="휴대폰 캡처 여백 자동 자르기 끄기")
    a = p.parse_args(argv)
    dest = build_sheet(a.drawing, a.out_dir, a.output, a.width, a.crop, not a.no_trim)
    print("✅ 비교 이미지: %s" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
