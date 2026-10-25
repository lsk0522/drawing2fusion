"""Manufacturing annotations -> structured notes (not modelled, but kept machine-readable).

    feature control frames  DXF TOLERANCE "{\\Fgdt;j}%%v{\\Fgdt;n}0.1{\\Fgdt;m}%%vA%%vB"
                            text "⊥ 0.02 A", "◎ Ø0.05 A", "// 0.01 A-B"
    datum labels            "A", "DATUM A" next to a datum triangle (text only)
    surface roughness       "Ra 1.6", "√Ra3.2", "Rz 6.3", KS triangles "▽▽"
    general tolerance       "ISO 2768-m", "KS B 0412 보통급", "일반 공차 ISO 2768-mK"
    heat treatment / finish "HRC 58~62", "침탄", "열처리", "도금", "아노다이징"

The CAD model is nominal; these become `spec.notes[*].annotation` so a reviewer (or Claude via
MCP) sees exactly which requirement the model does not carry, and the general tolerance is
attached to dimensioned parameters that have no tolerance of their own.
"""

import re

GDT_FONT = {   # AutoCAD GDT font letter -> characteristic
    "a": "angularity", "b": "perpendicularity", "c": "flatness", "d": "profile_of_surface",
    "e": "circularity", "f": "parallelism", "g": "cylindricity", "h": "circular_runout",
    "i": "symmetry", "j": "position", "k": "profile_of_line", "r": "concentricity",
    "t": "total_runout", "u": "straightness",
}
GDT_UNICODE = {
    "∠": "angularity", "⊥": "perpendicularity", "▱": "flatness", "⏥": "flatness", "⌓": "profile_of_surface",
    "○": "circularity", "∥": "parallelism", "//": "parallelism", "⌭": "cylindricity", "↗": "circular_runout",
    "⌯": "symmetry", "⌖": "position", "⌒": "profile_of_line", "◎": "concentricity", "⌰": "total_runout",
    "⏤": "straightness",
}
KOREAN = {
    "angularity": "경사도", "perpendicularity": "직각도", "flatness": "평면도", "profile_of_surface": "면의 윤곽도",
    "circularity": "진원도", "parallelism": "평행도", "cylindricity": "원통도", "circular_runout": "원주 흔들림",
    "symmetry": "대칭도", "position": "위치도", "profile_of_line": "선의 윤곽도", "concentricity": "동심도",
    "total_runout": "온 흔들림", "straightness": "진직도",
}
MODIFIER = {"m": "MMC", "l": "LMC", "s": "RFS", "Ⓜ": "MMC", "Ⓛ": "LMC", "Ⓢ": "RFS"}

_NUM = r"(\d+(?:\.\d+)?)"
TEXT_FCF = re.compile(r"(⊥|∠|▱|⏥|⌓|○|∥|//|⌭|↗|⌯|⌖|⌒|◎|⌰|⏤)\s*(?:\|\s*)?([Øø⌀]|%%c)?\s*" + _NUM +
                      r"\s*([ⓂⓁⓈ])?((?:\s*\|?\s*[A-Z](?:\s*-\s*[A-Z])?){0,3})")
ROUGH = re.compile(r"\b(Ra|Rz|Rmax|Ry)\s*[=:]?\s*" + _NUM, re.I)
KS_TRIANGLES = re.compile(r"(▽{1,4}|∇{1,4})")
KS_TRI_RA = {1: 25, 2: 6.3, 3: 1.6, 4: 0.2}         # KS B 0617 legacy symbols -> typical Ra (µm)
GENERAL = re.compile(r"(ISO\s*2768|KS\s*B\s*ISO\s*2768|KS\s*B\s*0412)\s*[-–:]?\s*(?:([fmcv])([HKL])?\b|(정밀|중간|보통|거친|매우\s*거친)급?)",
                     re.I)
KS_CLASS = {"정밀": "f", "중간": "m", "보통": "m", "거친": "c", "매우거친": "v", "매우 거친": "v"}
HARDNESS = re.compile(r"\b(HRC|HRB|HV|HB)\s*" + _NUM + r"(?:\s*[~\-–]\s*" + _NUM + r")?", re.I)
PROCESS = re.compile(r"(열처리|침탄|질화|고주파|담금질|뜨임|도금|아노다이징|양극산화|흑착색|도장|분체|"
                     r"heat\s*treat\w*|carburi\w*|nitrid\w*|anodi[sz]\w*|plating|plated|black\s*oxide|painted|painting)", re.I)

# ISO 2768-1 linear tolerances (±mm) by nominal range (0.5-3, 3-6, 6-30, 30-120, 120-400, 400-1000, 1000-2000, 2000-4000)
ISO2768_TOP = (3, 6, 30, 120, 400, 1000, 2000, 4000)
ISO2768 = {
    "f": (0.05, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, None),
    "m": (0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0),
    "c": (0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 3.0, 4.0),
    "v": (None, 0.5, 1.0, 1.5, 2.5, 4.0, 6.0, 8.0),
}


def iso2768(cls, size):
    """± tolerance (mm) for a linear size under ISO 2768-1 class f/m/c/v, or None."""
    row = ISO2768.get((cls or "").lower())
    if row is None or size < 0.5:
        return None
    for top, val in zip(ISO2768_TOP, row):
        if size <= top:
            return val
    return None


def _clean_mtext(s):
    return re.sub(r"\\[A-Za-z][^;]*;|[{}]", "", s or "")


def parse_fcf(content):
    """DXF TOLERANCE content -> list of frames (one per row)."""
    frames = []
    for row in re.split(r"\^J|\\P", content or ""):
        cells = row.split("%%v")
        if not cells or not cells[0].strip():
            continue
        m = re.search(r"\\Fgdt;\s*([a-z])", cells[0])
        sym = GDT_FONT.get(m.group(1)) if m else None
        if sym is None:
            continue
        frame = {"kind": "gdt", "characteristic": sym, "name_ko": KOREAN[sym], "datums": []}
        if len(cells) > 1:
            tc = cells[1]
            letters = re.findall(r"\\Fgdt;\s*([a-z])", tc)
            frame["diameter_zone"] = "n" in letters or "%%c" in tc.lower()
            mods = [MODIFIER[x] for x in letters if x in MODIFIER]
            if mods:
                frame["modifier"] = mods[0]
            num = re.search(_NUM, re.sub(r"\\Fgdt;\s*[a-z]", "", tc))
            frame["value"] = float(num.group(1)) if num else None
        for c in cells[2:]:
            lab = _clean_mtext(c).strip()
            if re.fullmatch(r"[A-Z](?:\s*-\s*[A-Z])?", lab):
                frame["datums"].append(lab.replace(" ", ""))
        frames.append(frame)
    return frames


def parse_text(text):
    """One plain text -> list of annotations found in it."""
    out = []
    t = _clean_mtext(text)
    for m in TEXT_FCF.finditer(t):
        sym = GDT_UNICODE.get(m.group(1))
        datums = [d.replace(" ", "") for d in re.findall(r"[A-Z](?:\s*-\s*[A-Z])?", m.group(5) or "")]
        a = {"kind": "gdt", "characteristic": sym, "name_ko": KOREAN[sym], "value": float(m.group(3)),
             "diameter_zone": bool(m.group(2)), "datums": datums}
        if m.group(4):
            a["modifier"] = MODIFIER.get(m.group(4))
        out.append(a)
    for m in ROUGH.finditer(t):
        out.append({"kind": "roughness", "parameter": m.group(1).capitalize() if m.group(1).lower() != "rmax" else "Rmax",
                    "value_um": float(m.group(2))})
    if not ROUGH.search(t):
        m = KS_TRIANGLES.search(t)
        if m:
            n = len(m.group(1))
            out.append({"kind": "roughness", "parameter": "Ra", "value_um": KS_TRI_RA.get(n),
                        "symbol": m.group(1), "note": "KS 삼각 기호 %d개 → 대략 Ra %s µm" % (n, KS_TRI_RA.get(n))})
    m = GENERAL.search(t)
    if m:
        cls = (m.group(2) or KS_CLASS.get(re.sub(r"\s+", "", m.group(4) or ""), "m")).lower()
        out.append({"kind": "general_tolerance", "standard": "ISO 2768", "class": cls,
                    "geometric_class": (m.group(3) or "").upper() or None})
    m = HARDNESS.search(t)
    if m:
        a = {"kind": "hardness", "scale": m.group(1).upper(), "min": float(m.group(2))}
        if m.group(3):
            a["max"] = float(m.group(3))
        out.append(a)
    for m in PROCESS.finditer(t):
        out.append({"kind": "process", "name": m.group(1)})
    return out


def collect(texts):
    """texts: [{text, kind?}] (kind 'fcf' = DXF TOLERANCE content). Returns [(annotation, source text)]."""
    found = []
    for t in texts:
        if t.get("kind") == "fcf":
            found += [(a, "[기하공차 틀] " + describe(a)) for a in parse_fcf(t["text"])]
        else:
            found += [(a, t["text"]) for a in parse_text(t["text"])]
    return found


def describe(a):
    """Short Korean label for a structured annotation."""
    k = a["kind"]
    if k == "gdt":
        zone = "Ø" if a.get("diameter_zone") else ""
        mod = " (%s)" % a["modifier"] if a.get("modifier") else ""
        dat = " 기준 %s" % ",".join(a["datums"]) if a.get("datums") else ""
        return "기하공차 %s %s%s%s%s" % (a["name_ko"], zone, a.get("value"), mod, dat)
    if k == "roughness":
        return "표면 거칠기 %s %s µm" % (a["parameter"], a.get("value_um"))
    if k == "general_tolerance":
        return "일반 공차 %s-%s%s" % (a["standard"], a["class"], a.get("geometric_class") or "")
    if k == "hardness":
        return "경도 %s %g%s" % (a["scale"], a["min"], ("~%g" % a["max"]) if a.get("max") else "")
    if k == "process":
        return "처리 %s" % a["name"]
    return k
