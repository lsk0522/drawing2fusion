"""DXF reader tests against samples/drawings/dxf/*.expected.json (needs ezdxf)."""

import glob
import json
import math
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

try:
    import ezdxf  # noqa: F401
    HAVE_EZDXF = True
except ImportError:
    HAVE_EZDXF = False

from d2f.spec import expr, validate  # noqa: E402

DXF_DIR = os.path.join(ROOT, "samples", "drawings", "dxf")
PDF_DIR = os.path.join(ROOT, "samples", "drawings", "pdf")
try:
    import pymupdf  # noqa: F401
    HAVE_PYMUPDF = True
except ImportError:
    HAVE_PYMUPDF = False


@unittest.skipUnless(HAVE_EZDXF, "ezdxf not installed")
class DxfReaderTests(unittest.TestCase):
    def _cases(self, folder=DXF_DIR, ext=".dxf"):
        for exp_path in sorted(glob.glob(os.path.join(folder, "*.expected.json"))):
            name = os.path.basename(exp_path)[:-len(".expected.json")]
            with open(exp_path, encoding="utf-8") as f:
                yield name, os.path.join(folder, name + ext), json.load(f)

    def test_all_samples(self):
        self._run_samples(DXF_DIR, ".dxf", 12)

    @unittest.skipUnless(HAVE_PYMUPDF, "pymupdf not installed")
    def test_all_pdf_samples(self):
        """Same drawings exported as vector PDF (two dash styles, one at 1:2) -> same answers."""
        self._run_samples(PDF_DIR, ".pdf", 8)

    def _run_samples(self, folder, ext, minimum):
        from d2f.extract.dxf_reader import extract
        n = 0
        for name, path, exp in self._cases(folder, ext):
            n += 1
            with self.subTest(drawing=name):
                spec, report = extract(path)
                issues = validate.validate(spec) + report["issues"]
                codes = {i["code"] for i in issues if i["severity"] in ("ERROR", "BLOCK")}
                if exp.get("expect_issue"):
                    self.assertIn(exp["expect_issue"], codes)
                    continue
                self.assertEqual(codes, set(), [i["message"] for i in issues if i["severity"] in ("ERROR", "BLOCK")])
                p = expr.resolve_parameters(spec["parameters"])
                if exp.get("kind") == "turned":
                    self._check_turned(spec, report, exp, p)
                    continue
                for got, want in zip((p["L"], p["W"], p["H"]), exp["bbox"]):
                    self.assertAlmostEqual(got, want, places=3)
                off = exp.get("origin_offset", [0, 0, 0])
                holes = [f for f in spec["features"] if f["type"] == "hole"]
                for eh in exp["holes"]:
                    if eh["axis"] != "Z":
                        plane = {"Y": "XZ", "X": "YZ"}[eh["axis"]]
                        side = [h for h in holes if h["face"]["plane"] == plane and
                                abs(expr.evaluate(h["diameter"], p) - eh["diameter"]) < 1e-3]
                        got = sorted((round(expr.evaluate(x, p), 3), round(expr.evaluate(y, p), 3))
                                     for h in side for x, y in h["positions"])
                        self.assertEqual(got, sorted(tuple(q) for q in eh["positions"]), "side holes Ø%s" % eh["diameter"])
                        if isinstance(eh.get("depth"), (int, float)):
                            self.assertAlmostEqual(expr.evaluate(side[0]["depth"], p), eh["depth"], places=3)
                        elif eh.get("depth") == "through":
                            self.assertEqual(side[0]["depth"], "through_all")
                        if eh.get("cb_diameter"):
                            self.assertEqual(side[0]["hole_type"], "counterbore")
                            self.assertAlmostEqual(expr.evaluate(side[0]["cb_diameter"], p), eh["cb_diameter"], places=3)
                            self.assertAlmostEqual(expr.evaluate(side[0]["cb_depth"], p), eh["cb_depth"], places=3)
                            self.assertEqual(side[0]["face"]["offset"], eh["face"])
                        continue
                    want = sorted((round(x - off[0], 3), round(y - off[1], 3)) for x, y in eh["positions"])
                    match = []
                    for h in holes:
                        d = expr.evaluate(h["diameter"], p)
                        cb = expr.evaluate(h["cb_diameter"], p) if h.get("cb_diameter") else None
                        if abs(d - eh["diameter"]) < 1e-3 or (cb and abs(cb - eh["diameter"]) < 1e-3):
                            match += [(round(x, 3) + 0.0, round(y, 3) + 0.0)
                                      for x, y in validate.hole_instances(spec["features"], h, p)]
                            if eh.get("thread") and abs(d - eh["diameter"]) < 1e-3:
                                self.assertEqual(h["thread"]["designation"], eh["thread"])
                    self.assertEqual(sorted(match), want, "holes Ø%s" % eh["diameter"])
                if exp.get("volume") is not None:
                    vol = [c for c in spec["checks"] if c["kind"] == "volume"][0]["expected"]
                    if vol is not None:
                        self.assertTrue(math.isclose(vol, exp["volume"], rel_tol=1e-3), (vol, exp["volume"]))
        self.assertGreaterEqual(n, minimum)

    def _check_turned(self, spec, report, exp, p):
        """Turned part: profile from the spec expressions, bbox / volume checks, keyway."""
        pts = [(expr.evaluate(u, p), expr.evaluate(v, p)) for u, v in
               [f for f in spec["features"] if f["id"] == "sk_profile"][0]["entities"][0]["points"]]
        self.assertEqual(len(pts), len(exp["profile"]))
        for q in exp["profile"]:
            self.assertTrue(any(math.dist(q, r) < 1e-3 for r in pts), (q, pts))
        bbox = [c for c in spec["checks"] if c["kind"] == "bbox"][0]["expected"]
        for got, want in zip(bbox, exp["bbox"]):
            self.assertAlmostEqual(expr.evaluate(got, p), want, places=3)
        vol = [c for c in spec["checks"] if c["kind"] == "volume"][0]["expected"]
        self.assertTrue(math.isclose(vol, exp["volume"], rel_tol=2e-4), (vol, exp["volume"]))
        key = [f for f in spec["features"] if f["id"] == "sk_key"]
        if exp.get("keyway"):
            rect = key[0]["entities"][0]
            self.assertAlmostEqual(expr.evaluate(rect["size"][0], p), exp["keyway"]["width"], places=3)
            self.assertAlmostEqual(expr.evaluate(rect["size"][1], p), exp["keyway"]["top"], places=3)
            plane = [c for c in spec["checks"] if c["kind"] == "plane"][0]
            self.assertAlmostEqual(expr.evaluate(plane["at"], p), exp["keyway"]["top"], places=3)
        else:
            self.assertEqual(key, [])

    def test_cli_extract_every_drawing(self):
        """d2f extract (file writing + console summary) must work for every sample drawing."""
        import contextlib
        import io
        import tempfile
        from d2f import drawing
        paths = sorted(glob.glob(os.path.join(DXF_DIR, "*.dxf")) +
                       glob.glob(os.path.join(ROOT, "samples", "drawings", "public", "*.dxf")) +
                       (glob.glob(os.path.join(PDF_DIR, "*.pdf")) if HAVE_PYMUPDF else []))
        out = tempfile.mkdtemp()
        for path in paths:
            with self.subTest(drawing=os.path.basename(path)):
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        spec_path, spec, report = drawing.extract_to(path, out)
                        drawing.print_summary(spec, report)
                except SystemExit as e:          # unreadable drawing: reported, not crashed
                    self.assertIn("E10", str(e))
                    continue
                self.assertTrue(os.path.exists(spec_path))
                self.assertIn("도면 해석", buf.getvalue())

    def test_projection_and_report(self):
        from d2f.extract.dxf_reader import extract
        spec, report = extract(os.path.join(DXF_DIR, "plate_holes.dxf"))
        self.assertEqual(report["projection"], "third")
        self.assertEqual(set(report["views"]), {"front", "top", "side"})
        self.assertEqual(spec["parameters"]["d1"]["source"]["type"], "drawing")
        # every dimension of the plate lands on a parameter (hole grid: first hole + pitches)
        self.assertFalse(any("미사용 치수" in n["text"] for n in spec["notes"]))
        self.assertEqual({spec["parameters"][k]["source"]["type"] for k in ("px1", "py1", "hx1", "hy1")}, {"drawing"})
        spec, report = extract(os.path.join(DXF_DIR, "bracket_l.dxf"))
        self.assertTrue(any("미사용 치수" in n["text"] for n in spec["notes"]))

    def test_first_angle_layout(self):
        """Mirror the plate drawing into first-angle layout: top view below the front view."""
        import tempfile
        import ezdxf as ez
        from d2f.extract.dxf_reader import extract
        doc = ez.readfile(os.path.join(DXF_DIR, "stepped_block.dxf"))
        msp = doc.modelspace()
        # move everything above y=40 (top view) below the front view: y -> -(y - 60) - 70
        for e in msp:
            if e.dxftype() == "LINE" and e.dxf.layer != "TITLE" and min(e.dxf.start.y, e.dxf.end.y) > 40:
                s, t = e.dxf.start, e.dxf.end
                e.dxf.start = (s.x, -(s.y - 60) - 70, 0)
                e.dxf.end = (t.x, -(t.y - 60) - 70, 0)
        for e in list(msp.query("DIMENSION")):
            msp.delete_entity(e)
        msp.add_text("1ST ANGLE", dxfattribs={"insert": (0, -150), "height": 3})
        path = os.path.join(tempfile.mkdtemp(), "first.dxf")
        doc.saveas(path)
        spec, report = extract(path)
        self.assertEqual(report["projection"], "first")
        p = expr.resolve_parameters(spec["parameters"])
        self.assertAlmostEqual(p["L"], 60, places=3)
        self.assertAlmostEqual(p["H"], 30, places=3)


    def test_curved_outlines(self):
        """Arcs / slopes in the outline: exact path sketches, view-intersection when needed."""
        from d2f.extract.dxf_reader import extract
        spec, rep = extract(os.path.join(DXF_DIR, "link_plate.dxf"))
        ent = spec["features"][0]["entities"][0]
        self.assertEqual(ent["kind"], "path")
        self.assertEqual(sum(1 for s_ in ent["segments"] if s_["type"] == "arc"), 2)
        self.assertNotIn("intersect", rep)
        for name, views in (("clevis_lug", ["front"]), ("slanted_block", ["front"]),
                            ("rounded_wedge", ["front", "top"])):
            with self.subTest(drawing=name):
                spec, rep = extract(os.path.join(DXF_DIR, name + ".dxf"))
                self.assertEqual(rep["intersect"]["views"], views)
                ops = [f["operation"] for f in spec["features"] if f["type"] == "extrude"]
                self.assertEqual(ops, ["new_body"] + ["intersect"] * (len(views) - 1))
                # no "approximated with straight walls" warning any more
                self.assertFalse(any(i["code"] == "E207" and i["severity"] == "WARN" and "곧은 벽" in i["message"]
                                     for i in rep["issues"]))
        # dimensioned vertex coordinates become parameters with the dimension as source
        spec, rep = extract(os.path.join(DXF_DIR, "slanted_block.dxf"))
        self.assertEqual(spec["parameters"]["f_u1"]["source"]["type"], "drawing")
        self.assertEqual(spec["parameters"]["f_u1"]["value"], 40)
        spec, rep = extract(os.path.join(DXF_DIR, "clevis_lug.dxf"))
        self.assertEqual(spec["parameters"]["f_r1"]["source"]["type"], "drawing")
        arc = [s_ for s_ in spec["features"][0]["entities"][0]["segments"] if s_["type"] == "arc"][0]
        self.assertEqual(arc["radius_param"], "f_r1")

    def test_hole_patterns_and_threads(self):
        from d2f.extract.dxf_reader import extract, parse_thread
        spec, rep = extract(os.path.join(DXF_DIR, "flange.dxf"))
        pc = [f for f in spec["features"] if f["type"] == "pattern_circ"][0]
        self.assertEqual((pc["count"], pc["center"]), (6, ["L/2", "W/2"]))
        self.assertEqual(spec["parameters"]["pcd1"]["source"]["label"], "P.C.D 75")
        h1 = [f for f in spec["features"] if f["id"] == pc["features"][0]][0]
        self.assertEqual(h1["positions"], [["L/2 + pcd1/2", "W/2"]])
        # tapped holes are not patterned (the thread would stay on the first hole only)
        spec, rep = extract(os.path.join(DXF_DIR, "plate_tapped.dxf"))
        self.assertFalse(any(f["type"].startswith("pattern") for f in spec["features"]))
        self.assertEqual(parse_thread("4X #10-32 UNF-2B")["designation"], "#10-32 UNF")
        self.assertEqual(parse_thread("1 1/8-7 UNC")["size_label"], "1 1/8")
        self.assertAlmostEqual(parse_thread("1/4-20 UNC")["drill"], 5.1054, places=4)
        self.assertEqual(parse_thread("4-M6")["designation"], "M6x1")
        self.assertIsNone(parse_thread("MATERIAL: AL5052"))
        spec, rep = extract(os.path.join(DXF_DIR, "plate_unc.dxf"))
        th = [f for f in spec["features"] if f.get("thread")][0]["thread"]
        self.assertEqual((th["type"], th["size_label"]), ("ANSI Unified Screw Threads", "1/4"))

    @unittest.skipUnless(HAVE_PYMUPDF, "pymupdf not installed")
    def test_pdf_pages_and_terminators(self):
        from d2f.extract.dxf_reader import extract
        path = os.path.join(PDF_DIR, "multipage_set.pdf")
        spec, rep = extract(path)                                     # cover sheet skipped
        self.assertEqual(spec["parameters"]["L"]["value"], 80)
        spec, rep = extract(path, page=3)
        self.assertEqual((spec["part"]["name"], spec["parameters"]["H"]["value"]), ("multipage_set_p3", 30))
        from d2f.extract.dxf_reader import ExtractError
        with self.assertRaises(ExtractError):
            extract(path, page=9)
        for name, word in (("stepped_block_ticks", "사선"), ("plate_holes_dots", "점")):
            spec, rep = extract(os.path.join(PDF_DIR, name + ".pdf"))
            self.assertTrue(any(word in i["message"] for i in rep["issues"] if i["code"] == "E207"))
            self.assertEqual(rep["summary"]["dims_total"], rep["summary"]["dims_used"])

    def test_annotations(self):
        from d2f.extract import annotations as A
        from d2f.extract.dxf_reader import extract
        self.assertEqual(A.parse_text("⌖ Ø0.1 Ⓜ A B C")[0]["datums"], ["A", "B", "C"])
        self.assertEqual(A.parse_text("// 0.01 A-B")[0]["characteristic"], "parallelism")
        self.assertEqual(A.parse_text("일반공차 KS B 0412 보통급")[0]["class"], "m")
        self.assertEqual(A.parse_text("PLATE_HOLES"), [])                  # title words are not finishes
        fr = A.parse_fcf(r"{\Fgdt;j}%%v{\Fgdt;n}0.1{\Fgdt;m}%%vA%%vB^J{\Fgdt;c}%%v0.01")
        self.assertEqual([(f["characteristic"], f["value"], f.get("modifier"), f["datums"]) for f in fr],
                         [("position", 0.1, "MMC", ["A", "B"]), ("flatness", 0.01, None, [])])
        self.assertEqual((A.iso2768("m", 80), A.iso2768("f", 5), A.iso2768("v", 1)), (0.3, 0.05, None))
        spec, rep = extract(os.path.join(DXF_DIR, "block_side_cbore.dxf"))
        kinds = sorted(a["kind"] for a in rep["annotations"])
        self.assertEqual(kinds, ["gdt", "general_tolerance", "hardness", "roughness"])
        self.assertEqual(spec["parameters"]["L"]["general_tolerance"]["plusminus"], 0.3)
        self.assertTrue(all(n["applied"] is False for n in spec["notes"] if "annotation" in n))

    def test_intersect_helpers(self):
        from d2f.extract import intersect as I
        ob = {"start": [15, 0], "segments": [
            {"type": "line", "to": [75, 0]}, {"type": "arc", "to": [75, 30], "center": [75, 15], "ccw": True},
            {"type": "line", "to": [15, 30]}, {"type": "arc", "to": [15, 0], "center": [15, 15], "ccw": True}]}
        self.assertAlmostEqual(I.path_area(ob), 60 * 30 + math.pi * 225, places=6)
        # walking the same outline clockwise gives the same area
        cw = {"start": [15, 0], "segments": [
            {"type": "arc", "to": [15, 30], "center": [15, 15], "ccw": False}, {"type": "line", "to": [75, 30]},
            {"type": "arc", "to": [75, 0], "center": [75, 15], "ccw": False}, {"type": "line", "to": [15, 0]}]}
        self.assertAlmostEqual(I.path_area(cw), I.path_area(ob), places=6)
        rect = {"start": [0, 0], "segments": [{"type": "line", "to": p_} for p_ in
                                              ([30, 0], [60, 0], [60, 20], [0, 20], [0, 0])]}
        self.assertTrue(I.is_full_rect(rect, 60, 20, 0.01))
        notch = {"start": [0, 0], "segments": [{"type": "line", "to": p_} for p_ in
                                               ([60, 0], [60, 10], [50, 20], [0, 20], [0, 0])]}
        self.assertFalse(I.is_full_rect(notch, 60, 20, 0.01))
        # grid volume of a half-cylinder-topped lug against the closed form
        fr = [(0, 0), (50, 0), (50, 25)] + [(25 + 25 * math.cos(math.radians(a)), 25 + 25 * math.sin(math.radians(a)))
                                             for a in range(1, 180)] + [(0, 25)]
        v = I.volume(fr, None, None, 50, 12, 50)
        self.assertTrue(math.isclose(v, (50 * 25 + math.pi * 625 / 2) * 12, rel_tol=2e-3), v)

    def test_public_drawings(self):
        """QCAD example sheets (samples/drawings/public): what the reader can and cannot do."""
        from d2f.extract.dxf_reader import extract
        pub = os.path.join(ROOT, "samples", "drawings", "public")
        spec, rep = extract(os.path.join(pub, "qcad_projection.dxf"))
        self.assertEqual(set(rep["views"]), {"front", "top", "side"})
        self.assertEqual(rep["projection"], "first")
        # rhombus flange: plan view at ~31 deg must not be taken for an isometric view
        spec, rep = extract(os.path.join(pub, "qcad_flange.dxf"))
        msgs = [i for i in rep["issues"] if i["severity"] == "BLOCK"]
        self.assertTrue(any(i["code"] == "E208" for i in msgs))   # section hatch -> must ask
        # bevel gear section: the automatic reader must agree with the independent hand reading
        spec, rep = extract(os.path.join(pub, "qcad_example01.dxf"))
        self.assertEqual(rep["projection"], "section")
        self.assertEqual({i["code"] for i in validate.validate(spec) + rep["issues"]
                          if i["severity"] in ("ERROR", "BLOCK")}, set())
        hand = validate.load(os.path.join(ROOT, "samples", "specs", "bevel_gear_from_section.json"))
        hp = expr.resolve_parameters(hand["parameters"])
        hand_pts = [(expr.evaluate(u, hp), expr.evaluate(v, hp)) for u, v in
                    hand["features"][0]["entities"][0]["points"]]
        auto_pts = [tuple(p) for p in rep["section_profile_mm"]]
        self.assertEqual(len(auto_pts), len(hand_pts))
        for p in hand_pts:
            self.assertTrue(any(math.dist(p, q) < 0.03 for q in auto_pts), p)
        vol = lambda sp: [c for c in sp["checks"] if c["kind"] == "volume"][0]["expected"]  # noqa: E731
        self.assertTrue(math.isclose(vol(spec), vol(hand), rel_tol=1e-4))
        kinds = [c["kind"] for c in spec["checks"]]
        self.assertIn("plane", kinds)                               # keyway side is checked


if __name__ == "__main__":
    unittest.main()
