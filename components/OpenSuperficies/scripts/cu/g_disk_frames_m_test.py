import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import g_disk_frames as G

# W4 lane M (RD3-03, AW4-08): the gate inspects only run dirs it created (--run-prefix, default gate-<pid>/leftover-<pid>);
# foreign / kept run dirs under the host-global ramws root are counted as ignored_foreign and never fail the gate.


class Gdf(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gdf_")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.old = G.RAMROOT
        G.RAMROOT = self.root
        self.addCleanup(lambda: setattr(G, "RAMROOT", self.old))

    def touch(self, *parts):
        p = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"x")

    def test_gdf_ignores_foreign(self):
        self.touch("os-cu-0", "slive-w4-wl1", "chunk_0", "frames.rgb24")  # a kept foreign run with frames
        self.touch("os-cu-0", "slive-w4-wl3", "owner.json")
        own, foreign = G.leftovers(["gate-4242", "leftover-4242"])
        self.assertEqual(own, [])
        self.assertEqual(foreign, 2)

    def test_gdf_fails_own_frames(self):
        self.touch("os-cu-0", "slive-w4-wl1", "chunk_0", "frames.rgb24")
        self.touch("os-cu-0", "leftover-4242", "frames", "f0.rgb24")
        self.touch("gate-4242", "frames", "f1.raw")
        own, foreign = G.leftovers(["gate-4242", "leftover-4242"])
        self.assertEqual(foreign, 1)
        self.assertTrue(any("leftover-4242" in x for x in own), own)
        self.assertTrue(any("gate-4242" in x for x in own), own)
        self.assertFalse(any("slive-w4" in x for x in own), own)

    def test_gdf_run_prefix_flag(self):
        a = G.parse_args(["--run-prefix", "gate-77"])
        self.assertEqual(a["run_prefix"], ["gate-77"])
        self.assertEqual(G.main(["--bogus"]), 2)
        self.assertEqual(G.main(["--run-prefix"]), 2)


class Build(unittest.TestCase):
    """ADC-W4-01 / JW4-03: check_build scans build/ once (top-level fail_evidence/kept_frames exemptions stay top-level) and
    filters foreign room_* entries (counted, never failed)."""

    def setUp(self):
        self.b = tempfile.mkdtemp(prefix="gdfb_")
        self.addCleanup(lambda: shutil.rmtree(self.b, ignore_errors=True))

    def put(self, *parts):
        p = os.path.join(self.b, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"x")

    def test_gdf_build_own_nested_fail_evidence_fails(self):
        self.put("gate_disk", "fail_evidence", "ep", "a.png")
        bad, ign = G.check_build(self.b, 0)
        self.assertEqual((bad, ign), (["gate_disk/fail_evidence/ep/a.png"], 0))

    def test_gdf_build_top_fail_evidence_passes(self):
        self.put("fail_evidence", "ep", "a.png")
        self.assertEqual(G.check_build(self.b, 0), ([], 0))

    def test_gdf_build_rooms_ignored_and_counted(self):
        self.put("room_X1", "x.png")
        self.put("room_X2", "y.png")
        self.put("slive4", "z.png")
        bad, ign = G.check_build(self.b, 0)
        self.assertEqual((bad, ign), (["slive4/z.png"], 2))


if __name__ == "__main__":
    unittest.main()
