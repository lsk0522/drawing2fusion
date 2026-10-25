"""Unit tests that run without Fusion:  python -m unittest discover -s tests -v"""

import filecmp
import json
import os
import sys
import threading
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "addin", "Drawing2Fusion"))

from d2f import compare  # noqa: E402
from d2f.bridge_client import BridgeClient, BridgeError  # noqa: E402
from d2f.spec import expr, validate  # noqa: E402
from d2f_addin import bridge  # noqa: E402  (no adsk import inside)

SPECS = os.path.join(ROOT, "samples", "specs")
BROKEN = os.path.join(ROOT, "samples", "broken")


def codes(issues, severity=None):
    return {i["code"] for i in issues if severity is None or i["severity"] == severity}


class ExprTests(unittest.TestCase):
    def test_copies_identical(self):
        self.assertTrue(filecmp.cmp(os.path.join(ROOT, "d2f", "spec", "expr.py"),
                                    os.path.join(ROOT, "addin", "Drawing2Fusion", "d2f_addin", "expr.py"),
                                    shallow=False), "expr.py copies diverged")

    def test_units_round_trip(self):
        self.assertAlmostEqual(expr.mm_to_cm(80), 8.0)
        self.assertAlmostEqual(expr.cm_to_mm(expr.mm_to_cm(6.6)), 6.6)
        self.assertAlmostEqual(expr.cm3_to_mm3(1.0), 1000.0)
        self.assertAlmostEqual(expr.rad_to_deg(expr.deg_to_rad(37.5)), 37.5)

    def test_evaluate(self):
        p = {"L": 80, "edge": 10}
        self.assertEqual(expr.evaluate("L-edge", p), 70)
        self.assertEqual(expr.evaluate("L/2", p), 40)
        self.assertEqual(expr.evaluate(5, p), 5)
        self.assertAlmostEqual(expr.evaluate("sqrt(9)+pi", p), 3 + 3.141592653589793)
        with self.assertRaises(KeyError):
            expr.evaluate("X+1", p)
        with self.assertRaises(ValueError):
            expr.evaluate("__import__('os')", p)

    def test_resolve_forward_refs_and_cycles(self):
        self.assertEqual(expr.resolve_parameters({"a": {"value": "b*2"}, "b": {"value": 3}}),
                         {"a": 6.0, "b": 3.0})
        with self.assertRaises(ValueError):
            expr.resolve_parameters({"a": {"value": "b"}, "b": {"value": "a"}})

    def test_fusion_name_map(self):
        m = expr.fusion_name_map(["L", "W", "edge", "T", "T_"])
        self.assertEqual(m, {"L": "L_", "W": "W_", "T": "T__"})
        self.assertEqual(expr.rename_identifiers("L-edge+W/2", m), "L_-edge+W_/2")
        self.assertEqual(expr.rename_identifiers("10 mm", m), "10 mm")
        self.assertEqual(expr.rename_identifiers(5, m), 5)

    def test_fusion_expression(self):
        self.assertEqual(expr.to_fusion_expression(80), "80.0 mm")
        self.assertEqual(expr.to_fusion_expression(90, "deg"), "90.0 deg")
        self.assertEqual(expr.to_fusion_expression("L-10"), "L-10")


class CodeHashTests(unittest.TestCase):
    def test_hash_functions_match(self):
        import re
        def body(path, name):
            src = open(path, encoding="utf-8").read()
            m = re.search(r"def %s\(pkg_dir=[^)]*\):\n(.*?)\n\n" % name, src, re.S)
            return [l for l in m.group(1).splitlines() if not l.strip().startswith(('"""', "#"))]
        a = body(os.path.join(ROOT, "addin", "Drawing2Fusion", "d2f_addin", "dispatcher.py"), "code_hash")
        b = body(os.path.join(ROOT, "d2f", "bridge_client.py"), "addin_code_hash")
        self.assertEqual(a, b)

    def test_hash_ignores_line_endings(self):
        import shutil
        import tempfile
        from d2f.bridge_client import addin_code_hash
        d = tempfile.mkdtemp()
        src = os.path.join(ROOT, "addin", "Drawing2Fusion", "d2f_addin")
        for n in os.listdir(src):
            if n.endswith(".py"):
                data = open(os.path.join(src, n), "rb").read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                open(os.path.join(d, n), "wb").write(data)
        self.assertEqual(addin_code_hash(d), addin_code_hash(src))
        shutil.rmtree(d)


class ValidateTests(unittest.TestCase):
    def test_good_samples_clean(self):
        for name in os.listdir(SPECS):
            spec = validate.load(os.path.join(SPECS, name))
            issues = validate.validate(spec)
            bad = [i for i in issues if i["severity"] in ("ERROR", "BLOCK")]
            self.assertEqual(bad, [], "%s: %s" % (name, bad))

    def test_hole_outside(self):
        issues = validate.validate(validate.load(os.path.join(BROKEN, "hole_outside.json")))
        self.assertIn("E205", codes(issues, "ERROR"))

    def test_fillet_too_big(self):
        issues = validate.validate(validate.load(os.path.join(BROKEN, "fillet_too_big.json")))
        self.assertIn("E205", codes(issues, "ERROR"))

    def test_needs_answer(self):
        issues = validate.validate(validate.load(os.path.join(BROKEN, "needs_answer.json")))
        self.assertTrue({"E202", "E105"} <= codes(issues, "BLOCK"))
        self.assertIn("E207", codes(issues, "WARN"))
        self.assertTrue(any(i.get("question") for i in issues))

    def test_path_entity(self):
        def spec_with(ent, holes=()):
            feats = [{"id": "s", "type": "sketch", "entities": [ent]},
                     {"id": "e", "type": "extrude", "profile": "s", "distance": 10}]
            for k, pos in enumerate(holes):
                feats.append({"id": "h%d" % k, "type": "hole", "face": {"plane": "XY", "offset": 10},
                              "positions": [pos], "diameter": 12, "depth": "through_all"})
            return {"schema_version": "0.1", "part": {"name": "x"}, "parameters": {"R": {"value": 15}},
                    "features": feats}
        ob = {"kind": "path", "start": [15, 0], "segments": [
            {"type": "line", "to": [75, 0]}, {"type": "arc", "to": [75, 30], "center": [75, 15], "ccw": True},
            {"type": "line", "to": [15, 30]}, {"type": "arc", "to": [15, 0], "center": ["R", "R"], "ccw": True}]}
        # a hole at the arc centre is inside the rounded end (vertex box alone would say outside)
        self.assertEqual(codes(validate.validate(spec_with(ob, [[15, 15], [75, 15]])), "ERROR"), set())
        bad = json.loads(json.dumps(ob))
        bad["segments"][1]["center"] = [75, 10]                       # ends not on one circle
        self.assertIn("E205", codes(validate.validate(spec_with(bad)), "ERROR"))
        open_ = json.loads(json.dumps(ob))
        open_["segments"][-1]["to"] = [14, 0]
        open_["segments"][-1]["type"] = "line"
        self.assertIn("E205", codes(validate.validate(spec_with(open_)), "ERROR"))
        undefined = json.loads(json.dumps(ob))
        undefined["segments"][0]["to"] = ["Q", 0]
        self.assertIn("E206", codes(validate.validate(spec_with(undefined)), "ERROR"))

    def test_undefined_and_reserved(self):
        spec = {"schema_version": "0.1", "part": {"name": "x"},
                "parameters": {"9x": {"value": 1}, "L": {"value": 10}},
                "features": [{"id": "s", "type": "sketch",
                              "entities": [{"kind": "rect", "size": ["L", "Q"]}]},
                             {"id": "e", "type": "extrude", "profile": "nope", "distance": 1},
                             {"id": "e", "type": "pattern_rect"}]}
        c = codes(validate.validate(spec))
        self.assertTrue({"E206", "E201"} <= c)


def fake_measure(volume=28800.0, hole_d=6.6, size=(80.0, 40.0, 6.0), shift=0.0):
    holes = [{"kind": "hole", "diameter": hole_d, "axis_dir": [0, 0, 1],
              "axis_point": [x + shift, y, 0], "depth": 6.0}
             for x, y in ((10, 10), (70, 10), (10, 30), (70, 30))]
    return {"bodies": [{"name": "Body1", "bbox": {"size": list(size)}, "volume": volume,
                        "holes": holes}],
            "features": {"h_mount": {"found": True, "holes": holes}}}


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.spec = validate.load(os.path.join(SPECS, "plate_holes.json"))

    def test_all_pass(self):
        rows = compare.compare(self.spec, fake_measure())
        self.assertTrue(rows)
        self.assertTrue(all(r["pass"] for r in rows), [r for r in rows if not r["pass"]])

    def test_wrong_diameter(self):
        rows = compare.compare(self.spec, fake_measure(hole_d=6.0))
        self.assertIn("E402", {r["code"] for r in rows})
        self.assertIn("E403", {r["code"] for r in rows})  # 0 holes match Ø6.6

    def test_drawing_tolerance_accepts_upper(self):
        rows = compare.compare(self.spec, fake_measure(hole_d=6.68))
        d = [r for r in rows if r["check"] == "c_hole_d"][0]
        self.assertTrue(d["pass"])

    def test_unit_suspect(self):
        rows = compare.compare(self.spec, fake_measure(size=(8.0, 4.0, 0.6)))
        self.assertIn("E407", {r["code"] for r in rows})

    def test_position(self):
        rows = compare.compare(self.spec, fake_measure(shift=0.5))
        self.assertIn("E404", {r["code"] for r in rows})

    def test_volume_and_missing_body(self):
        spec = validate.load(os.path.join(SPECS, "block_basic.json"))
        ok = compare.compare(spec, {"bodies": [{"name": "b", "bbox": {"size": [80, 40, 20]},
                                                "volume": 64000.0, "holes": []}]})
        self.assertTrue(all(r["pass"] for r in ok))
        bad = compare.compare(spec, {"bodies": []})
        self.assertTrue(all(not r["pass"] for r in bad))

    def test_judge(self):
        self.assertTrue(compare.judge(10, 10.05, 0.05)[0])
        self.assertFalse(compare.judge(10, 10.051, 0.05)[0])
        self.assertTrue(compare.judge(6.6, 6.6, [0, 0.1])[0])
        self.assertFalse(compare.judge(6.6, 6.5, [0, 0.1])[0])


class BridgeTests(unittest.TestCase):
    """Round trip through the real HTTP server with a fake 'main thread'."""

    def _server(self, dispatch, timeout_s=5.0, delay=0.0):
        def fire(payload):
            def main_thread():
                if delay:
                    threading.Event().wait(delay)
                srv.handle_main_thread(payload)
            threading.Thread(target=main_thread, daemon=True).start()
        srv = bridge.BridgeServer(fire=fire, dispatch=dispatch, token="t0k", port=0, timeout_s=timeout_s)
        srv.start()
        self.addCleanup(srv.stop)
        return srv

    def test_round_trip(self):
        calls = []

        def dispatch(cmd, args):
            calls.append((cmd, args))
            return {"ok": True, "protocol_version": bridge.PROTOCOL_VERSION, "echo": args}
        srv = self._server(dispatch)
        c = BridgeClient(port=srv.port, token="t0k")
        self.assertEqual(c.ping()["protocol_version"], 1)
        res = c.measure(["h1"])
        self.assertEqual(res["echo"], {"features": ["h1"]})
        self.assertEqual(calls[-1][0], "measure")

    def test_bad_token(self):
        srv = self._server(lambda cmd, args: {"ok": True})
        with self.assertRaises(BridgeError) as cm:
            BridgeClient(port=srv.port, token="wrong").ping()
        self.assertEqual(cm.exception.code, "E501")

    def test_timeout_and_stale_skip(self):
        ran = []

        def dispatch(cmd, args):
            ran.append(cmd)
            return {"ok": True}
        srv = self._server(dispatch, timeout_s=0.2, delay=0.6)
        with self.assertRaises(BridgeError) as cm:
            BridgeClient(port=srv.port, token="t0k").measure()
        self.assertEqual(cm.exception.code, "E502")
        threading.Event().wait(0.8)
        self.assertEqual(ran, [], "stale request should be skipped on the main thread")

    def test_dispatch_exception(self):
        def dispatch(cmd, args):
            raise RuntimeError("boom")
        srv = self._server(dispatch)
        with self.assertRaises(BridgeError) as cm:
            BridgeClient(port=srv.port, token="t0k").measure()
        self.assertIn("boom", str(cm.exception))

    def test_unreachable(self):
        with self.assertRaises(BridgeError) as cm:
            BridgeClient(port=1, token="x").ping()
        self.assertEqual(cm.exception.code, "E501")

    def test_token_file(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "sub", "token")
        t1 = bridge.load_or_create_token(p)
        self.assertEqual(t1, bridge.load_or_create_token(p))
        self.assertGreaterEqual(len(t1), 32)


class NotifyTests(unittest.TestCase):
    def test_dedup_and_format(self):
        from d2f.notify import Notifier, format_text, make_alert
        n = Notifier(part_name="p", channels=["console"])
        a = make_alert("r", "E402", "ERROR", "verify", "구멍 지름 불일치", "기대 Ø6.6, 실측 Ø6.0",
                       where={"feature": "h_mount"})
        n.send(a)
        n.send(dict(a))
        self.assertEqual(len(n.sent), 1)
        self.assertIn("E402", format_text(a))
        self.assertIn("h_mount", format_text(a))


class PlaneCheckAndGroupingTests(unittest.TestCase):
    def test_plane_check_catches_wrong_side(self):
        spec = validate.load(os.path.join(SPECS, "bevel_gear_from_section.json"))
        self.assertEqual(codes(validate.validate(spec), "ERROR"), set())
        check = [c for c in spec["checks"] if c["kind"] == "plane"][0]
        resolved = expr.resolve_parameters({k: p["value"] for k, p in spec["parameters"].items()})
        wrong = {"bodies": [{"planes": [{"axis": "Y", "at": -20.8, "area": 1}, {"axis": "Z", "at": 5, "area": 1}]}]}
        right = {"bodies": [{"planes": [{"axis": "Z", "at": 20.8, "area": 1}]}]}
        self.assertEqual(compare._cmp_plane(check, wrong, resolved, spec["parameters"])[0]["code"], "E408")
        self.assertTrue(compare._cmp_plane(check, right, resolved, spec["parameters"])[0]["pass"])

    def test_plane_check_needs_at(self):
        spec = {"parameters": {"L": {"value": 1, "source": {"type": "drawing"}}}, "features": [],
                "checks": [{"id": "p", "kind": "plane", "axis": "Q"}]}
        self.assertIn("E201", codes(validate.validate(spec), "ERROR"))

    def test_inferred_warnings_grouped(self):
        from d2f import cli
        issues = [{"code": "E207", "severity": "WARN", "message": "m", "where": {"parameter": n}} for n in "abcde"]
        issues.append({"code": "E207", "severity": "INFO", "message": "x", "stage": "extract"})
        out = cli._group_inferred(issues)
        self.assertEqual(len(out), 2)
        self.assertIn("a, b, c, d, e", out[0]["message"])
        self.assertEqual(cli._group_inferred(issues[:2]), issues[:2])


def _prism_mesh(poly, depth):
    """Extrude an XZ polygon (convex fan) along Y -> mesh dict like the add-in returns."""
    nodes = [[x, 0.0, z] for x, z in poly] + [[x, depth, z] for x, z in poly]
    n = len(poly)
    idx = []
    for k in range(1, n - 1):
        idx += [0, k, k + 1, n, n + k, n + k + 1]
    for k in range(n):
        a, b = k, (k + 1) % n
        idx += [a, b, n + b, a, n + b, n + a]
    return {"nodes": nodes, "indices": idx}


class SilhouetteTests(unittest.TestCase):
    def test_box_matches_and_mismatch_is_caught(self):
        from d2f import silhouette
        box = _prism_mesh([(0, 0), (80, 0), (80, 30), (0, 30)], 40)
        sil = {"front": {"axes": "XZ", "polygon": [[0, 0], [80, 0], [80, 30], [0, 30]]},
               "top": {"axes": "XY", "polygon": [[0, 0], [80, 0], [80, 40], [0, 40]]}}
        rows = silhouette.compare_all(sil, {"bodies": [{"mesh": box}]})
        self.assertTrue(all(r["pass"] for r in rows), rows)
        # a drawing whose front view has a 45 deg chamfered corner the model lacks
        sil["front"]["polygon"] = [[0, 0], [80, 0], [80, 10], [60, 30], [0, 30]]
        row = silhouette.compare_all(sil, {"bodies": [{"mesh": box}]})[0]
        self.assertFalse(row["pass"])
        self.assertEqual(row["code"], "E406")

    def test_drawn_segments_are_filled(self):
        from d2f import silhouette
        box = _prism_mesh([(0, 0), (70, 0), (70, 5), (0, 5)], 40)          # thin plate, front view
        segs = [[0, 0, 70, 0], [70, 0, 70, 5], [70, 5, 0, 5], [0, 5, 0, 0], [20, 0, 20, 5]]   # + an inner edge
        row = silhouette.compare_view("front", {"axes": "XZ", "segments": segs}, [box])
        self.assertTrue(row["pass"], row)
        # L-shaped drawing vs the plain box: must fail
        segs = [[0, 0, 70, 0], [70, 0, 70, 5], [70, 5, 10, 5], [10, 5, 10, 30], [10, 30, 0, 30], [0, 30, 0, 0]]
        tall = _prism_mesh([(0, 0), (70, 0), (70, 30), (0, 30)], 40)
        row = silhouette.compare_view("front", {"axes": "XZ", "segments": segs}, [tall])
        self.assertFalse(row["pass"])

    def test_holes_in_projection_are_filled(self):
        from d2f import silhouette
        # a ring seen from the front: outer silhouette only (the hole must not count as missing)
        outer = [(0, 0), (50, 0), (50, 50), (0, 50)]
        ring = _prism_mesh(outer, 10)
        row = silhouette.compare_view("front", {"axes": "XZ", "polygon": outer}, [ring])
        self.assertTrue(row["pass"], row)


class ToleranceTests(unittest.TestCase):
    def test_parse_forms(self):
        from d2f.extract import tolerance as t
        self.assertEqual(t.parse("31.83%%p0.1")["tolerance"], [-0.1, 0.1])
        self.assertEqual(t.parse("\\A1;<>\\S0^ -0.05;")["tolerance"], [-0.05, 0.0])
        self.assertEqual(t.parse("+0.2/0")["tolerance"], [0.0, 0.2])
        self.assertEqual(t.parse("%%c<>\\~H7", 25)["tolerance"], [0.0, 0.021])
        self.assertEqual(t.parse("Ø97.66 h11", 97.66)["tolerance"], [-0.22, 0.0])
        self.assertEqual(t.parse("10 P9", 10)["tolerance"], [-0.051, -0.015])
        self.assertEqual(t.parse("Ø20 g6", 20)["tolerance"], [-0.02, -0.007])
        for text in ("M6", "4-%%c6.6", "C'BORE %%c9.5 DP5.4", "R5", "P.C.D 75"):
            self.assertEqual(t.parse(text, 6), {}, text)


class McpServerTests(unittest.TestCase):
    def test_protocol_and_extract(self):
        import shutil
        import tempfile
        from d2f import mcp_server as m
        r = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "drawing2fusion")
        self.assertIsNone(m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("d2f_build", names)
        out = tempfile.mkdtemp()
        old = m.OUT
        m.OUT = out
        try:
            r = m.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "d2f_extract", "arguments": {"drawing": "samples/drawings/dxf/broken_keyway_noview.dxf"}}})
            res = json.loads(r["result"]["content"][0]["text"])
            self.assertTrue(res["ok"])
            self.assertEqual([q["code"] for q in res["questions"]], ["E202"])
            r = m.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "d2f_answer", "arguments": {"spec": res["spec"], "answers": [{"id": 0, "answer": "6"}]}}})
            self.assertEqual(json.loads(r["result"]["content"][0]["text"])["remaining"], [])
        finally:
            m.OUT = old
            shutil.rmtree(out, ignore_errors=True)
        r = m.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}})
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
