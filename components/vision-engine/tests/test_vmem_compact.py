import os
import tempfile
import unittest

from visioncore import params
from visioncore.errors import VqaError
from vmem import compact as C, tiers as T, units as U
from tests.test_vmem_units import SC, TK, anchor, mk


def run_id(i):
    return "r_%06d_%08x" % (i, i)


def build(n_runs=9, fail_run=None):
    """per run: a BG episode (crop+desc), a BG baseline; optional FAIL episode+verdict with FG blobs."""
    us, n = [], 0
    for i in range(1, n_runs + 1):
        r = run_id(i)
        sc = dict(SC, build_seq=i)
        crop, desc = ("%02x" % i) * 32, ("%02x" % (i + 100)) * 32
        n += 1
        us.append(U.make_unit(n, "episode", sc, TK, anchors=[anchor(r, crop, desc)], verdict="PASS"))
        n += 1
        us.append(U.make_unit(n, "baseline", sc, TK, anchors=[anchor(r, None, None)]))
        if i == fail_run:
            n += 1
            us.append(U.make_unit(n, "verdict", sc, TK, pin="FAIL", verdict="FAIL", check_id="render.flicker",
                                  anchors=[anchor(r, "f1" * 32, "f2" * 32)]))
    return us


class TestCompact(unittest.TestCase):
    def test_fail_survives_background_dropped(self):
        us = build(9, fail_run=2)
        fg0 = C.fg_ids_sha256(us)
        out = C.compact(us)
        ids_active = {u["id"] for u in out["active"]}
        fail = [u for u in us if u["pin"] == "FAIL"]
        self.assertTrue(fail)
        for f in fail:
            self.assertIn(f["id"], ids_active)  # FAIL episode survives
            self.assertIn(("f1" * 32), out["live_blobs"])
            self.assertIn(("f2" * 32), out["live_blobs"])
        self.assertEqual(C.fg_ids_sha256(out["active"] + out["archive"]), fg0)
        self.assertTrue(C.g_fg_monotone(us, out["active"] + out["archive"]))
        self.assertGreater(len(out["retired"]), 0)
        self.assertTrue(out["summaries"])
        self.assertTrue(set(out["retired"]).isdisjoint(ids_active))
        # old BG crops dropped, newest 5 clean runs untouched
        self.assertIn("01" * 32, out["dropped_blobs"])
        self.assertNotIn("09" * 32, out["dropped_blobs"])
        for u in out["active"]:
            if u["kind"] == "episode" and u["value"] != "@unchanged":
                self.assertEqual(u["anchors"][0]["run_id"], run_id(int(u["anchors"][0]["run_id"][2:8])))
                self.assertGreaterEqual(int(u["anchors"][0]["run_id"][2:8]), 4)

    def test_no_dropping_when_few_runs(self):
        us = build(5)
        out = C.compact(us)
        self.assertEqual(out["retired"], [])
        self.assertEqual(out["dropped_blobs"], set())

    def test_idempotent_and_input_unmutated(self):
        us = build(9, fail_run=2)
        import copy
        snap = copy.deepcopy(us)
        o1 = C.compact(us)
        self.assertEqual(us, snap)
        o2 = C.compact(o1["active"], gen=2)
        self.assertEqual(o1["fg_ids_sha256"], o2["fg_ids_sha256"])
        self.assertEqual(o2["retired"], [])

    def test_fg_monotone_mutant_killed(self):
        us = build(9, fail_run=2)
        out = C.compact(us)
        dropped_fg = [u for u in out["active"] if not U.is_fg(u)]  # mutant: FG removed
        with self.assertRaises(VqaError) as c:
            C.g_fg_monotone(us, dropped_fg)
        self.assertEqual((c.exception.rc, c.exception.detail["reason"]), (5, "g_fg_monotone"))

    def test_fg_cap_block(self):
        us = build(9, fail_run=2)
        tbl = dict(params.PARAMS, L2_CAP_MIB=1, FG_BLOCK_PERMILLE=900)
        sizes = {"f1" * 32: 1 << 20}
        with self.assertRaises(VqaError) as c:
            C.compact(us, sizes, table=tbl)
        self.assertEqual((c.exception.rc, c.exception.detail["reason"]), (5, "fg_cap"))

    def test_gc_window(self):
        a, b = "aa" * 32, "bb" * 32
        u_old = mk(1, anchors=[anchor(crop=a)])
        u_new = mk(2, anchors=[anchor(crop=b)])
        self.assertEqual(C.gc_plan([a, b], [[u_old], [u_new]]), [])  # a still in rollback window
        self.assertEqual(C.gc_plan([a, b], [[u_new]]), [a])
        self.assertEqual(C.gc_plan([a, b], [[u_new]], durable=False), [])
        with self.assertRaises(VqaError):
            C.gc_plan([a], [[u_new]] * 5)

    def test_defaults_mirror_params(self):
        for k, v in T.DEFAULTS.items():
            self.assertEqual(params.PARAMS[k], v)

    def test_tiers(self):
        t = params.PARAMS
        cap = t["L2_CAP_MIB"] << 20
        self.assertEqual(T.fg_state(0), "OK")
        self.assertEqual(T.fg_state(cap * 250 // 1000), "OK")
        self.assertEqual(T.fg_state(cap * 250 // 1000 + 1), "FG_WARN")
        self.assertEqual(T.fg_state(cap * 900 // 1000 + 1), "FG_BLOCK")
        with self.assertRaises(VqaError) as c:
            T.check_l2(cap + 1)
        self.assertEqual(c.exception.rc, 6)
        with self.assertRaises(VqaError):
            T.check_digest(b"x" * 6001)
        with tempfile.TemporaryDirectory() as d:
            with T.L1Stage("r_000001_00000001", base=d, cap_bytes=10) as s:
                s.put("a", b"12345")
                with self.assertRaises(VqaError) as c:
                    s.put("b", b"123456")
                self.assertEqual(c.exception.rc, 6)
                p = s.path
                self.assertTrue(os.path.isdir(p))
            self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
