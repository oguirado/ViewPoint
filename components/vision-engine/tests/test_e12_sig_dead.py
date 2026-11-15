"""Lane N (v12e room NR): noise dead band on the sig64 Hamming term of the S2 residual (SPEC_CORE §3.1, AMEND_NR).

A flipped sig64 bit counts only if its sub-block luma mean is more than SIG_DEAD_Q4/16 grey from the tile mean in the
current frame OR in the comparator (prev1 / prev2 / ref). +-1 LSB iid noise (SPEC_CORE §8 n_iid_noise) only flips bits
inside that band; a 1 px shift of real content flips bits far outside it. SIG_DEAD_Q4 was fitted on CAL only
(X4 seeds 30..199, X3 seeds 40..69; never 0..29, 1000..1029, 2000..2029): tests/golden/nr_sigdead_fit.json."""
import json
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from eval import x3_power as X3, x4_fp as X4
from visioncore import change as C, desc, params as P
from visioncore.grid import Grid

ROOT = Path(__file__).resolve().parent.parent
FIT = ROOT / "tests" / "golden" / "nr_sigdead_fit.json"


def fit():
    return json.loads(FIT.read_text())


class Registry(unittest.TestCase):
    def test_param_row_matches_fit(self):
        sel = fit()["selected"]
        self.assertEqual(P.PARAMS["SIG_DEAD_Q4"], sel["sig_dead_q4"])
        self.assertEqual(P.PARAMS["H_DEAD"], sel["h_dead"])
        self.assertFalse("SIG_DEAD_Q4" in P.PROFILE_KEYS)
        for name in ("thresholds.src", "thresholds.tsv"):
            rows = [ln.split("\t") for ln in (ROOT / "catalogue" / name).read_text().splitlines()]
            self.assertIn(["SIG_DEAD_Q4", str(sel["sig_dead_q4"]), "q4", "vc.p.SIG_DEAD_Q4"], [r[:4] for r in rows])

    def test_fit_table_applies_declared_rule(self):
        d = fit()
        self.assertEqual((d["x4_cal_seeds"], d["x3_cal_seeds"]), ([30, 199], [40, 69]))
        for r in d["table"]:
            self.assertEqual((r["x4_runs"], r["x3_trials"]), (1020, 450))
            self.assertEqual(r["zero_fp"], r["x4_fp_runs_total"] == 0)
            self.assertEqual(r["power_ok"], r["x3_min_cell"] >= 29 and r["x3_aggregate_cp95_lower_ppm"] >= 950000)
        feas = [[r["sig_dead_q4"], r["h_dead"]] for r in d["table"] if r["zero_fp"] and r["power_ok"]]
        self.assertEqual(d["feasible"], feas)
        self.assertIn([d["selected"]["sig_dead_q4"], d["selected"]["h_dead"]], feas)
        off = {r["h_dead"]: r for r in d["table"] if r["sig_dead_q4"] == 0}
        self.assertEqual(off[4]["x4_fp_runs"], {"iid_noise": 20})   # lever off = BASE_E behaviour: iid FP on CAL
        self.assertEqual(d["feasible"], [[16, 2], [24, 2], [32, 2]])
        self.assertEqual(d["selected"], {"sig_dead_q4": 24, "h_dead": 2})   # 24: lower neighbour 16 also 0-FP


class SigConf(unittest.TestCase):
    def test_bit_layout_matches_desc_sig64(self):
        """One 2x2 sub-block lifted by 100 grey: conf and sig64 are both exactly that bit (same order as desc)."""
        g = Grid(16, 16)
        for k in (0, 7, 9, 63):
            f = np.full((16, 16, 3), 100, np.uint8)
            i, j = divmod(k, 8)
            f[2 * i:2 * i + 2, 2 * j:2 * j + 2] = 200
            pad = g.pad(f)
            t, _ = desc.describe_padded(pad, g)
            cf = C.sig_conf(desc.luma_of_padded(pad), g.s, g.gw, g.gh, t["morton"], 32)
            self.assertEqual(int(t["sig64"][0]), 1 << k)
            self.assertEqual(int(cf[0]), 1 << k)

    def test_matches_int64_reference_all_grids(self):
        """Strided int32 fast path == plain int64 reshape reference at s = 16/32/64 (720p, 1080p, 4K)."""
        rng = np.random.default_rng(3)
        for W, H in ((1280, 720), (1920, 1080), (3840, 2160)):
            g = Grid(W, H)
            y = desc.luma_of_padded(g.pad(rng.integers(0, 256, (H, W, 3), dtype=np.uint8)))
            tx, ty = C._morton_xy(g.mortons)
            b, n = g.s // 8, g.s * g.s
            sub = y.astype(np.int64).reshape(g.gh, 8, b, g.gw, 8, b).sum(axis=(2, 5)).transpose(0, 2, 1, 3)
            tot = sub.sum(axis=(2, 3))
            for d in (1, 24, 400):
                bits = (16 * np.abs(sub * n - tot[:, :, None, None] * b * b) > d * n * b * b)[ty, tx].reshape(-1, 64)
                want = np.packbits(bits, axis=1, bitorder="little").view("<u8")[:, 0]
                self.assertTrue((C.sig_conf(y, g.s, g.gw, g.gh, g.mortons, d) == want).all(), (W, H, d))

    def test_zero_band_is_all_bits(self):
        g = Grid(64, 32)
        f = np.zeros((32, 64, 3), np.uint8)
        cf = C.sig_conf(desc.luma_of_padded(g.pad(f)), g.s, g.gw, g.gh, g.mortons, 0)
        self.assertTrue((cf == np.uint64(0xFFFFFFFFFFFFFFFF)).all())

    def test_masked_residual(self):
        a = np.zeros(1, C.TILE_DT)
        b = a.copy()
        b["sig64"] = 0xFF   # 8 flipped bits
        self.assertEqual(int(C.residual(a, b, 4)[0]), C.W_HAM * 4)
        self.assertEqual(int(C.residual(a, b, 4, np.array([0x3F], np.uint64))[0]), C.W_HAM * 2)
        self.assertEqual(int(C.residual(a, b, 4, np.array([0], np.uint64))[0]), 0)

    def test_state_digest_covers_masks(self):
        t = np.zeros(4, C.TILE_DT)
        t["morton"] = np.arange(4)
        a, b = C.ClassState.init(t), C.ClassState.init(t)
        b.cf1[0] = np.uint64(1)
        self.assertNotEqual(a.digest_bytes(), b.digest_bytes())


class CalEdges(unittest.TestCase):
    """The two edges of the H_DEAD trade-off (room HD) on CAL seeds, through the real drivers."""

    def test_iid_noise_cal42_zero_fp(self):
        """BASE_E: 1 FP tile (cal seed 42, X4 SEAL config). Lever on: 0; BASE_E config (no band, H_DEAD 4) reproduces it."""
        self.assertEqual(X4.run_trial("iid_noise", 42, X4.SEAL_W, X4.SEAL_H)["fp_tiles"], 0)
        with mock.patch.dict(P.PARAMS, {"SIG_DEAD_Q4": 0, "H_DEAD": 4}):
            self.assertEqual(X4.run_trial("iid_noise", 42, X4.SEAL_W, X4.SEAL_H)["fp_tiles"], 1)

    def test_shift_L2_cal45_detected(self):
        """1 px shift: power kept with the lever on (cal seeds 45 and the shift/L2 seeds missed by HD's H_DEAD 5)."""
        for seed in (45, 54, 66):   # eval/splits/hdead_cal_v3.json repro x3_shift_L2_missed_at_5
            with self.subTest(seed=seed):
                self.assertTrue(X3.run_trial(X3.trial_spec("shift", 2, seed))["detected"])

    def test_deterministic(self):
        a = X4.run_trial("iid_noise", 31, X4.SEAL_W, X4.SEAL_H)
        b = X4.run_trial("iid_noise", 31, X4.SEAL_W, X4.SEAL_H)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
