"""Tolerances written in dimension texts -> [lower, upper] deviation in mm.

    "31.83%%p0.1" / "±0.1"            -> [-0.1, 0.1]
    "<>\\S+0.2^ 0;" (MTEXT stacked)   -> [0, 0.2]
    "+0.2/0", "0/-0.05"                -> [0, 0.2], [-0.05, 0]
    "Ø35 H6", "Ø97.66 h11", "10 P9"    -> ISO 286 limits for that size

ISO 286 coverage: IT5..IT11 up to 500 mm; holes H, JS, G, F (all grades), N and P (grade >= 8);
shafts h, js, g, f, and k, m, n, p (grades 5..7). Anything else is kept as text only.
The CAD model is built at nominal size; the tolerance widens or narrows the accepted
measurement band of the matching check and is recorded for manufacturing.
"""

import re

RANGES = (3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)
IT = {  # micrometres per nominal range
    5: (4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27),
    6: (6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40),
    7: (10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63),
    8: (14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97),
    9: (25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155),
    10: (40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250),
    11: (60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400),
}
# fundamental deviations (micrometres)
G = (2, 4, 5, 6, 7, 9, 10, 12, 14, 15, 17, 18, 20)
F = (6, 10, 13, 16, 20, 25, 30, 36, 43, 50, 56, 62, 68)
K = (0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5)          # k, grades 4..7
M = (2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21, 23)
N = (4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37, 40)
P = (6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62, 68)

FIT = re.compile(r"(?<![A-Za-z])(JS|js|[HGFNP]|[hgfkmnp])(5|6|7|8|9|10|11)(?![0-9.])")
PLUSMINUS = re.compile(r"(?:%%p|±)\s*(\d+(?:\.\d+)?)", re.I)
STACKED = re.compile(r"\\S\s*([+-]?\d*\.?\d+)\s*\^\s*([+-]?\d*\.?\d+)\s*;")
SLASH = re.compile(r"(?<![\d.])([+-]\d*\.?\d+|0)\s*/\s*([+-]\d*\.?\d+|0)(?![\d.])")


def _range_index(size):
    for i, top in enumerate(RANGES):
        if size <= top:
            return i
    return None


def iso_limits(code, grade, size):
    """ISO 286 deviations (mm) for e.g. ('H', 7, 25) -> [0, 0.021]; None if not covered."""
    i = _range_index(size)
    if i is None or grade not in IT:
        return None
    it = IT[grade][i]
    if code == "H":
        lo, hi = 0, it
    elif code == "h":
        lo, hi = -it, 0
    elif code in ("JS", "js"):
        lo, hi = -it / 2, it / 2
    elif code == "G":
        lo, hi = G[i], G[i] + it
    elif code == "g":
        lo, hi = -G[i] - it, -G[i]
    elif code == "F":
        lo, hi = F[i], F[i] + it
    elif code == "f":
        lo, hi = -F[i] - it, -F[i]
    elif code == "N" and grade >= 8:
        lo, hi = -it, 0
    elif code == "P" and grade >= 8:
        lo, hi = -P[i] - it, -P[i]
    elif code in ("k", "m", "n", "p") and grade <= 7:
        ei = {"k": K, "m": M, "n": N, "p": P}[code][i]
        lo, hi = ei, ei + it
    else:
        return None
    return [round(lo / 1000.0, 4), round(hi / 1000.0, 4)]


def parse(text, nominal=None):
    """Tolerance in a dimension text -> dict(tolerance=[lo, hi], fit=code) or {}."""
    if not text:
        return {}
    m = STACKED.search(text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return {"tolerance": sorted([a, b])}
    m = PLUSMINUS.search(text)
    if m:
        v = float(m.group(1))
        return {"tolerance": [-v, v]}
    m = SLASH.search(text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return {"tolerance": sorted([a, b])}
    m = FIT.search(text.replace("\\~", " "))
    if m:
        code, grade = m.group(1), int(m.group(2))
        out = {"fit": code + str(grade)}
        if nominal:
            lim = iso_limits(code, grade, abs(float(nominal)))
            if lim:
                out["tolerance"] = lim
        return out
    return {}
