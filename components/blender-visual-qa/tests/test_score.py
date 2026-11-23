import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import score
from blender_visual_qa.truth.score import VqaError


def synth(F=2, N=10):
    tl = np.zeros((F, N), np.uint8)
    tl[:, 0:4] = 3   # RIGID
    tl[:, 4:6] = 4   # OCCLUDE
    tl[:, 6] = 6
    tl[:, 7] = 7
    tl[:, 8] = 8     # excluded
    cls = np.zeros((F, N), np.uint8)
    cls[:, 0:4] = score.C_MOVED
    cls[:, 4:6] = score.C_CHANGED
    cls[:, 6] = score.C_CHANGED
    mv = np.zeros((F, N, 2), np.int8)
    mv[:, 0:4] = (3, -2)
    return tl, cls, mv


def row(tl, cls, mvt, mvp, **kw):
    return score.score_frames(tl, cls, mvt, mvp, "B1", 0, 16, "grid:640x360:s16", 32, **kw)


class ScoreTests(unittest.TestCase):
    def test_perfect(self):
        tl, cls, mv = synth()
        r = row(tl, cls, mv, mv.copy())
        s = score.summarize([r])
        self.assertEqual(s["tl"]["3"]["dirty_ppm"], 1000000)
        self.assertEqual(s["mv_exact_ppm"], 1000000)
        self.assertEqual(s["mv_err_p95_px"], 0)
        self.assertEqual(s["tl"]["0"]["fp"], 0)
        self.assertEqual(r["excluded"], 2)
        self.assertNotIn("8", r["tl"])

    def test_static_fp_counted(self):
        tl, cls, mv = synth()
        cls[0, 9] = 0
        tl[0, 9] = 0
        cls[0, 9] = score.C_CHANGED
        s = score.summarize([row(tl, cls, mv, mv.copy())])
        self.assertEqual(s["tl"]["0"]["fp"], 1)

    def test_mvsign_mutant_zero(self):  # bt.mvsign.01
        tl, cls, mv = synth()
        s = score.summarize([row(tl, cls, mv, mv.copy(), mv_sign=-1)])
        self.assertEqual(s["mv_exact_ppm"], 0)

    def test_movedoff_separates_metrics(self):  # bt.movedoff.01
        tl, cls, mv = synth()
        cls[tl == 3] = score.C_CHANGED
        s = score.summarize([row(tl, cls, mv, mv.copy())])
        self.assertEqual(s["tl"]["3"]["dirty_ppm"], 1000000)
        self.assertEqual(s["tl"]["3"]["cls_ok_ppm"], 0)

    def test_mv_err_p95(self):
        tl, cls, mv = synth(F=1, N=20)
        tl[:] = 3
        cls[:] = score.C_MOVED
        mv[:] = (2, 0)
        mp = mv.copy()
        mp[0, 0] = (5, 0)  # 1 of 20 off by 3 -> 95th pct still 0
        self.assertEqual(score.summarize([row(tl, cls, mv, mp)])["mv_err_p95_px"], 0)
        mp[0, 1] = (4, 0)
        self.assertEqual(score.summarize([row(tl, cls, mv, mp)])["mv_err_p95_px"], 2)

    def test_resid_max(self):
        tl, cls, mv = synth()
        res = np.zeros(tl.shape, np.int32)
        res[tl == 0] = 11
        res[tl == 3] = 900  # not FP pop
        self.assertEqual(row(tl, cls, mv, mv, resid=res)["fp_resid_max_q4"], 11)

    def test_negative_shapes(self):
        tl, cls, mv = synth()
        with self.assertRaises(VqaError) as c:
            row(tl, cls[:, :5], mv, mv)
        self.assertEqual(c.exception.rc, 2)
        bad = tl.copy()
        bad[0, 0] = 12
        with self.assertRaises(VqaError):
            row(bad, cls, mv, mv)
        cb = cls.copy()
        cb[0, 0] = 9
        with self.assertRaises(VqaError):
            row(tl, cb, mv, mv)

    def test_row_is_canonical_int_only(self):
        tl, cls, mv = synth()
        r = row(tl, cls, mv, mv)
        self.assertTrue(score.canon.dumps(r).endswith("\n"))  # raises on floats

    def test_truth_tile_roundtrip_and_stale(self):
        F, N = 2, 6
        rec = np.zeros((F, N), score.TRUTH_REC)
        rec["tl"][:, 0] = 3
        rec["dx"][:, 0] = 3
        rec["dy"][:, 0] = -2
        hdr = struct.pack(score.TRUTH_HDR_FMT, score.TRUTH_MAGIC, 1, 16, 96, 64, 6, 4, N, 8, F, 1,
                          b"\0" * 16, b"\0" * 16)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.bin"
            p.write_bytes(hdr + rec.tobytes())
            t = score.read_truth_tiles(p, expect_grid=(16, 96, 64, 6, 4, N))
            self.assertEqual(tuple(t["mv"][0, 0]), (3, -2))
            with self.assertRaises(VqaError) as c:
                score.read_truth_tiles(p, expect_grid=(32, 96, 64, 3, 2, N))
            self.assertEqual(c.exception.rc, 4)
            p.write_bytes(hdr + rec.tobytes()[:-1])
            with self.assertRaises(VqaError):
                score.read_truth_tiles(p)
            rec["tl"][0, 1] = 99
            p.write_bytes(hdr + rec.tobytes())
            with self.assertRaises(VqaError):
                score.read_truth_tiles(p)


if __name__ == "__main__":
    unittest.main()
