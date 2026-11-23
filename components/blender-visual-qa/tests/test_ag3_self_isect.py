"""AG3 N2: self-intersection + sliver gate (VG-0245). Fake-runner tests always run; the real mathutils worker needs
BVQA_BLENDER=1:  BVQA_BLENDER=1 taskset -c 5-9 python3 -m unittest tests.test_ag3_self_isect -v
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from blender_visual_qa.assets import glb_core, self_isect
from tests.test_ag2_hull import cube, prim

UM = 1000000
BLENDER_ON = os.environ.get("BVQA_BLENDER") == "1"


def scene_of(*parts):
    return glb_core.Scene(prims=[prim(n, p, i) for n, (p, i) in parts])


def pierced():
    """Unit cube (half 1 m) plus one triangle that pierces its +X face (x=1m) from inside to outside."""
    pts, idx = cube(UM)
    n = len(pts)
    pts = pts + [(UM // 2, 0, 0), (2 * UM, 0, UM // 4), (2 * UM, 0, -UM // 4)]
    return pts, idx + [n, n + 1, n + 2]


def sliver_mesh():
    """Closed cube with a needle triangle (altitude 1 um over a 1 m base) attached far away."""
    pts, idx = cube(UM)
    n = len(pts)
    pts = pts + [(5 * UM, 0, 0), (6 * UM, 0, 0), (5 * UM + UM // 2, 1, 0)]
    return pts, idx + [n, n + 1, n + 2]


def runner_returning(pairs_by_prim, rc=0):
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        if rc:
            return rc, "", "boom"
        body = {"prims": [{"name": "x", "count": len(p), "pairs": [list(q) for q in p]} for p in pairs_by_prim]}
        return 0, "noise\nBVQA_SELF_ISECT " + json.dumps(body) + "\n", ""
    run.calls = calls
    return run


class SliverMath(unittest.TestCase):
    def test_equilateral_and_needle_and_degenerate(self):
        eq = self_isect.sliver_ratio_ppm((0, 0, 0), (UM, 0, 0), (UM // 2, 866025, 0))
        self.assertTrue(866000 <= eq <= 866100, eq)
        self.assertEqual(self_isect.sliver_ratio_ppm((0, 0, 0), (UM, 0, 0), (UM // 2, 1, 0)), 1)
        self.assertEqual(self_isect.sliver_ratio_ppm((0, 0, 0), (UM, 0, 0), (2 * UM, 0, 0)), 0)
        self.assertEqual(self_isect.sliver_ratio_ppm((3, 3, 3), (3, 3, 3), (3, 3, 3)), 0)

    def test_clean_cube_has_no_sliver(self):
        pts, idx = cube(UM)
        verts, tris = self_isect.weld(prim("c", pts, idx))
        self.assertEqual(self_isect.slivers(verts, tris, self_isect.SLIVER_MIN_PPM), [])


class HostGate(unittest.TestCase):
    def test_clean_cube_passes(self):
        sc = scene_of(("cube", cube(UM)))
        rc, body = self_isect.gate_scene(sc, runner=runner_returning([[]]))
        self.assertEqual(rc, 0)
        self.assertEqual(body["status"], "PASS")

    def test_reported_pair_fails_rc5(self):
        sc = scene_of(("p", pierced()))
        rc, body = self_isect.gate_scene(sc, runner=runner_returning([[(0, 12)]]))
        self.assertEqual(rc, 5)
        self.assertEqual(body["failed"], ["self_intersection"])
        self.assertEqual(body["prims"][0]["pairs"], [[0, 12]])

    def test_sliver_fails_rc5_without_pairs(self):
        sc = scene_of(("s", sliver_mesh()))
        rc, body = self_isect.gate_scene(sc, runner=runner_returning([[]]))
        self.assertEqual(rc, 5)
        self.assertEqual(body["failed"], ["sliver"])
        self.assertEqual(body["prims"][0]["slivers"], [12])
        self.assertEqual(body["prims"][0]["sliver_min_ratio_ppm"], 1)

    def test_mutant_sliver_threshold_zero_is_detected_by_test(self):
        # mutant: threshold 0 lets the needle through; the real default must not
        sc = scene_of(("s", sliver_mesh()))
        rc, _ = self_isect.gate_scene(sc, runner=runner_returning([[]]), sliver_min=0)
        self.assertEqual(rc, 0)
        rc, _ = self_isect.gate_scene(sc, runner=runner_returning([[]]))
        self.assertEqual(rc, 5)

    def test_invalid_worker_pairs_rejected_rc2(self):
        sc = scene_of(("cube", cube(UM)))
        for bad in ([(5, 3)], [(0, 99)], [(0, 1)]):  # unordered, out of range, adjacent (shared vertex)
            with self.assertRaises(self_isect.IsectError) as cm:
                self_isect.gate_scene(sc, runner=runner_returning([bad]))
            self.assertEqual(cm.exception.rc, 2)

    def test_worker_failure_and_missing_line_rc2(self):
        sc = scene_of(("cube", cube(UM)))
        with self.assertRaises(self_isect.IsectError) as cm:
            self_isect.gate_scene(sc, runner=runner_returning([[]], rc=1))
        self.assertEqual(cm.exception.rc, 2)
        with self.assertRaises(self_isect.IsectError):
            self_isect.gate_scene(sc, runner=lambda a, t: (0, "no line\n", ""))

    def test_empty_scene_rc3(self):
        rc, body = self_isect.gate_scene(glb_core.Scene(prims=[prim("e", [(0, 0, 0)], [])]), runner=runner_returning([]))
        self.assertEqual(rc, 3)
        self.assertEqual(body["status"], "INCOMPLETE_ZERO_CHECKS")

    def test_welding_hands_shared_ids_to_worker(self):
        # two coincident cube copies as one prim: worker sees welded ids (8 verts), 24 tris
        pts, idx = cube(UM)
        n = len(pts)
        verts, tris = self_isect.weld(prim("d", pts + pts, idx + [i + n for i in idx]))
        self.assertEqual(len(verts), n)
        self.assertEqual(len(tris), 24)


class CliReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ag3n2_", dir=os.path.join(os.getcwd(), "build", "room_N2")
                                    if os.path.isdir("build/room_N2") else None)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, parts):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(glb_core.write_glb(scene_of(*parts)))
        return path

    def test_receipt_chain_and_canonical(self):
        glb = self.write("c.glb", [("cube", cube(UM))])
        prev = os.path.join(self.tmp, "prev.json")
        with open(prev, "w") as fh:
            fh.write("{}\n")
        out = os.path.join(self.tmp, "r.json")
        rc, rec = self_isect.run(["--mesh", glb, "--prev-receipt", prev, "--out", out], runner=runner_returning([[]]))
        self.assertEqual(rc, 0)
        self.assertEqual(rec["schema"], "bvqa.asset-self-isect/v1")
        self.assertEqual(rec["input_sha256"], glb_core.sha256(open(glb, "rb").read()))
        self.assertEqual(len(rec["prev_sha256"]), 64)
        self.assertEqual(open(out).read(), self_isect.canon(rec))

    def test_usage_errors_rc2(self):
        self.assertEqual(self_isect.run(["--nope"])[0], 2)
        self.assertEqual(self_isect.run(["--mesh", os.path.join(self.tmp, "absent.glb")])[0], 2)

    @unittest.skipUnless(BLENDER_ON, "BVQA_BLENDER=1")
    def test_real_worker_cube_pass_pierced_fail(self):
        rc, rec = self_isect.run(["--mesh", self.write("c.glb", [("cube", cube(UM))])])
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["prims"][0]["intersecting_pairs"], 0)
        rc, rec = self_isect.run(["--mesh", self.write("p.glb", [("p", pierced())])])
        self.assertEqual(rc, 5, rec)
        self.assertGreaterEqual(rec["prims"][0]["intersecting_pairs"], 1)
        self.assertIn("self_intersection", rec["failed"])

    @unittest.skipUnless(BLENDER_ON, "BVQA_BLENDER=1")
    def test_real_worker_sliver_fan_and_mutant_adjacency(self):
        rc, rec = self_isect.run(["--mesh", self.write("s.glb", [("s", sliver_mesh())])])
        self.assertEqual(rc, 5, rec)
        self.assertEqual(rec["failed"], ["sliver"])
        # mutant: a worker that drops every pair lets the pierced mesh through (rc 0); the genuine worker gives rc 5
        src = open(self_isect.WORKER).read()
        self.assertIn("seen.add((a, b))", src)
        mut = os.path.join(self.tmp, "self_isect_mut.py")
        with open(mut, "w") as fh:
            fh.write(src.replace("seen.add((a, b))", "pass"))
        glb = self.write("p.glb", [("p", pierced())])
        self.assertEqual(self_isect.run(["--mesh", glb])[0], 5)
        with mock.patch.object(self_isect, "WORKER", mut):
            self.assertEqual(self_isect.run(["--mesh", glb])[0], 0)


if __name__ == "__main__":
    unittest.main()
