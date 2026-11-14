import hashlib
import unittest

from eval import x4_fp as X
from visioncore.errors import VqaError
from visioncore.grid import Grid

TINY = dict(W=256, H=160, n=16)


def synth_rows(tile_frames, seeds=range(X.TEST_SEEDS), controls=None, **over):
    """Bound, zero-FP rows with made-up tile counts (unit logic only; no driver run)."""
    rows = []
    for c in controls or X.CONTROLS:
        for s in seeds:
            rows.append({"schema": "vqa.eval-row/v1", "x": "X4", "item": c, "tier": "PX", "control": c, "seed": s, "rep": 0,
                         "tile_frames": tile_frames, "fault_tiles": 0, "dirty_tiles": 0, "fp_tiles": 0, "drift_tiles": 0,
                         "classes": {}, "fp": False, "split": "test",
                         "capture_sha256": hashlib.sha256(("%s|%d" % (c, s)).encode()).hexdigest(),
                         "partition_sha256": X.partition_sha(), "config_sha256": X.config_sha(), **over})
    return rows


class X4Binding(unittest.TestCase):
    def leak(self, rows):
        with self.assertRaises(VqaError) as cm:
            X.summarize(rows)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_GATE", 5))
        self.assertEqual(cm.exception.detail["reason"], "split_leak")

    def test_x4_absent_partition_fails_closed(self):
        for k in ("partition_sha256", "config_sha256", "capture_sha256"):
            rows = synth_rows(39000)
            del rows[0][k]
            self.leak(rows)

    def test_x4_cal_seed_cannot_be_relabelled(self):
        r = X.run_trial("clean_static", 30, **TINY)
        self.assertEqual(r["split"], "cal")
        self.leak([dict(r, split="test", config_sha256=X.config_sha())])
        rows = synth_rows(39000)
        rows[3]["seed"] = 31   # label says test, seed is a fit seed
        self.leak(rows)

    def test_x4_altered_binding_fails(self):
        self.leak(synth_rows(39000, partition_sha256="0" * 64))
        self.leak(synth_rows(39000, config_sha256="0" * 64))
        rows = synth_rows(39000)
        rows[1]["capture_sha256"] = rows[0]["capture_sha256"]
        self.leak(rows)

    def test_x4_rows_bind_capture_and_config(self):
        a = X.run_trial("clean_static", 0, **TINY)
        b = X.run_trial("clean_static", 1, **TINY)
        self.assertEqual(a["partition_sha256"], X.partition_sha())
        self.assertEqual(len(a["capture_sha256"]), 64)
        self.assertNotEqual(a["capture_sha256"], b["capture_sha256"])
        self.assertNotEqual(a["config_sha256"], X.config_sha())   # tiny size is not the preregistered config

    def test_x4_required_upper_bound(self):
        need = X.required_tile_frames()
        self.assertGreaterEqual(need, 4400000)
        self.assertGreaterEqual(Grid(X.SEAL_W, X.SEAL_H).N * (X.N_FRAMES - 1) * len(X.CONTROLS) * X.TEST_SEEDS, need)
        small = X.summarize(synth_rows(6240))      # 1.12 M tile-frames: the rejected JC2-3 sample
        self.assertEqual((small["verdict"], small["bound_met"], small["sample_complete"]), ("FAIL", False, False))
        full = X.summarize(synth_rows(Grid(X.SEAL_W, X.SEAL_H).N * (X.N_FRAMES - 1)))
        self.assertEqual((full["verdict"], full["bound_met"]), ("PASS", True))
        self.assertLessEqual(full["tile_fp_cp95_upper_ppb"], 680)

    def test_x4_incomplete_sample_fails(self):
        big = Grid(X.SEAL_W, X.SEAL_H).N * (X.N_FRAMES - 1)
        s = X.summarize(synth_rows(big * 6, seeds=range(15)))   # enough tile-frames, half the registered seeds
        self.assertEqual((s["verdict"], s["sample_complete"]), ("FAIL", False))

    def test_x4_fp_run_still_fails(self):
        big = Grid(X.SEAL_W, X.SEAL_H).N * (X.N_FRAMES - 1)
        rows = synth_rows(big)
        rows[0].update(fp=True, fp_tiles=1)
        self.assertEqual(X.summarize(rows)["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
