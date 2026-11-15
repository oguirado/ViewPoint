"""S2 change classification tests (SPEC_CORE §3, SPEC_INT T-I6): positive, negative, mutants."""
import unittest

import numpy as np

from visioncore import change as C, layout, params as P
from visioncore.errors import VqaError


def mk(n, y=1000):
    t = np.zeros(n, C.TILE_DT)
    t["morton"] = np.arange(n)
    t["y_q4"] = y
    t["sig_q4"] = 100
    return t


def run(frames, params=None, eq=None):
    """frames: list of tile arrays; eq: optional list of bool arrays (default: derived by byte equality)."""
    st = C.ClassState.init(frames[0])
    out, _, summ = C.classify_first(st, frames[0])
    res = [(out, summ)]
    for i in range(1, len(frames)):
        e = eq[i] if eq else np.array([frames[i][k].tobytes() == frames[i - 1][k].tobytes() for k in range(len(frames[i]))])
        o, st, s = C.classify_tiles(st, frames[i], e, params=params)
        res.append((o, s))
    return res


class TestThresholds(unittest.TestCase):
    def test_default(self):
        self.assertEqual(C.thresholds(), (35, 28, 35))

    def test_profile_changes_thresholds(self):
        p = P.effective({"t0_q4": 64})
        self.assertEqual(C.thresholds(p)[:2], (70, 57))

    def test_record_layout(self):
        self.assertEqual(C.TILE_DT.itemsize, 32)
        self.assertEqual(tuple(C.TILE_DT.names), layout.TILE_FIELDS)
        for n, o in zip(layout.TILE_FIELDS, layout.TILE_OFFSETS):
            self.assertEqual(C.TILE_DT.fields[n][1], o)


class TestResidual(unittest.TestCase):
    def test_terms(self):
        a, b = mk(1), mk(1)
        b["y_q4"] = 1010; b["co_h"] = 1; b["cg_h"] = -1; b["sig_q4"] = 105
        b["sig64"] = 0b11111  # 5 bits -> 3 beyond the NR dead zone (H_DEAD=2; sig64 noise band SIG_DEAD_Q4, AMEND_NR)
        self.assertEqual(int(C.residual(a, b)[0]), 10 + 8 * 2 + 5 + 8 * 3)

    def test_ham_dead_zone(self):
        a, b = mk(1), mk(1)
        b["sig64"] = 0b11
        self.assertEqual(int(C.residual(a, b)[0]), 0)


class TestClasses(unittest.TestCase):
    def test_unchanged_exact(self):
        f = [mk(4)] * 5
        for o, s in run(f)[1:]:
            self.assertTrue((o["cls"] == C.C_UNCHANGED).all())
            self.assertEqual(s["cov_ppm"], 0)

    def test_real_change_detected_once(self):
        a, b = mk(8), mk(8)
        b["y_q4"][3] = 1200
        res = run([a, b, b, b])
        self.assertEqual(res[1][0]["cls"].tolist(), [0, 0, 0, 2, 0, 0, 0, 0])
        self.assertEqual(res[1][1]["cov_ppm"], 125000)
        self.assertTrue((res[2][0]["cls"] == 0).all())

    def test_jitter_no_false_changed(self):
        rng = np.random.RandomState(7)
        frames = []
        for _ in range(200):
            t = mk(1000)
            t["y_q4"] += rng.randint(-3, 4, 1000).astype(np.uint16)
            t["sig_q4"] += rng.randint(-2, 3, 1000).astype(np.uint16)
            t["sig64"] = rng.randint(0, 4, 1000).astype(np.uint64)  # <= 2 bits flipped: dead zone
            frames.append(t)
        for o, s in run(frames, eq=[np.zeros(1000, bool)] * 200)[1:]:
            self.assertTrue((o["cls"] <= C.C_JITTER).all())
            self.assertEqual(s["cov_ppm"], 0)

    def test_hysteresis_no_flip_in_band(self):
        # tile 0 cold: |dy| = 30 in [T_exit=28, T_enter=35] stays JITTER; tile 1 hot: stays active.
        def frame(k):
            t = mk(2)
            t["y_q4"][0] = 1000 + 30 * (k % 2)
            t["y_q4"][1] = 1000 + 30 * (k % 2)
            return t
        frames = [frame(k) for k in range(60)]
        frames[1] = mk(2)
        frames[1]["y_q4"][1] = 1200  # tile 1 goes hot
        frames[1]["y_q4"][0] = 1000
        res = run(frames)
        self.assertTrue(all(o["cls"][0] == C.C_JITTER for o, _ in res[3:]))
        self.assertTrue(all(o["cls"][1] == C.C_CHANGED for o, _ in res[3:]))

    def test_flicker_aba(self):
        a = mk(1); b = mk(1); b["y_q4"] = 1500
        res = run([a, a, b, a, a, a])
        self.assertEqual(res[3][0]["cls"][0], C.C_FLICKER)

    def test_frozen_during_pan_and_static_hud_not(self):
        rng = np.random.RandomState(3)
        n, frames = 100, []
        base = mk(n)
        for f in range(40):
            t = mk(n)
            t["y_q4"][:40] = rng.randint(500, 3500, 40)        # panning tiles: always changing
            if f < 12:
                t["y_q4"][40:45] = rng.randint(500, 3500, 5)   # patch alive (>= 8 of 16 frames), then freezes
            else:
                t["y_q4"][40:45] = frames[11]["y_q4"][40:45]
            frames.append(t)
        res = run(frames)
        for f in range(12 + 8, 40):
            c = res[f][0]["cls"]
            self.assertTrue((c[40:45] == C.C_FROZEN).all(), f)
            self.assertTrue((c[45:] == C.C_UNCHANGED).all(), f)   # static HUD: 0 FROZEN
        self.assertFalse((res[14][0]["cls"][40:45] == C.C_FROZEN).any())
        self.assertEqual(sum(int((o["cls"][45:] == C.C_FROZEN).sum()) for o, _ in res), 0)

    def test_frozen_needs_coverage(self):
        rng = np.random.RandomState(4)
        frames = []
        for f in range(34):
            t = mk(1000)
            t["y_q4"][:5] = rng.randint(500, 3500, 5) if f < 12 else frames[11]["y_q4"][:5]
            frames.append(t)
        for o, _ in run(frames):
            self.assertFalse((o["cls"] == C.C_FROZEN).any())  # nothing else moves: cov < 1 %

    def test_drift_ramp_within_5_frames(self):
        frames = []
        for f in range(12):
            t = mk(3); t["y_q4"][0] = 1000 + 8 * f
            frames.append(t)
        res = run(frames)
        first = next(i for i, (o, _) in enumerate(res) if o["cls"][0] == C.C_DRIFT)
        self.assertLessEqual(first, 5)
        self.assertTrue((res[-1][0]["cls"][1:] == 0).all())

    def test_drift_negative_stationary(self):
        rng = np.random.RandomState(9)
        frames = []
        for _ in range(100):
            t = mk(200); t["y_q4"] += rng.randint(-8, 9, 200).astype(np.uint16)
            frames.append(t)
        for o, _ in run(frames, eq=[np.zeros(200, bool)] * 100)[1:]:
            self.assertFalse((o["cls"] >= 2).any())

    def test_cov_ppm_counts_dirty_not_jitter(self):  # T-I6
        a = mk(10); b = mk(10)
        b["y_q4"][0] = 1004      # JITTER
        b["y_q4"][1] = 2000      # CHANGED
        _, s = run([a, b])[1]
        self.assertEqual(s["cov_ppm"], 1 * 1000000 // 10)
        self.assertEqual(s["n"][1], 1)

    def test_resid_field(self):
        a = mk(2); b = mk(2); b["y_q4"][0] = 1400
        o, _ = run([a, b])[1]
        self.assertEqual(int(o["resid"][0]), 100)
        self.assertEqual(int(o["resid"][1]), 0)

    def test_deterministic(self):
        rng = np.random.RandomState(1)
        frames = [mk(50) for _ in range(30)]
        for t in frames:
            t["y_q4"] = rng.randint(900, 1100, 50)
        a = [o.tobytes() for o, _ in run(frames)]
        b = [o.tobytes() for o, _ in run(frames)]
        self.assertEqual(a, b)

    def test_input_not_mutated(self):
        a = mk(4); b = mk(4); b["y_q4"][0] = 2000
        before = b.tobytes()
        run([a, b])
        self.assertEqual(b.tobytes(), before)

    def test_size_mismatch_rejected(self):
        st = C.ClassState.init(mk(4))
        with self.assertRaises(VqaError) as cm:
            C.classify_tiles(st, mk(5), np.zeros(5, bool))
        self.assertEqual(cm.exception.rc, 4)


class TestMotion(unittest.TestCase):
    def _setup(self, shift):
        s, gw, gh = 16, 8, 4
        rng = np.random.RandomState(5)
        big = rng.randint(0, 256, (gh * s, gw * s + 64)).astype(np.int16)
        prev = big[:, 32:32 + gw * s]
        cur = big[:, 32 - shift[0]:32 - shift[0] + gw * s]  # cur(p) = prev(p - shift)
        def tiles(y):
            t = mk(gw * gh)
            m = y.reshape(gh, s, gw, s).sum(axis=(1, 3))
            order = np.arange(gw * gh)
            tx = order % gw; ty = order // gw
            t["morton"] = [layout.morton(int(a), int(b)) for a, b in zip(tx, ty)]
            t["y_q4"] = (16 * m[ty, tx]) // (s * s)
            return t
        return (s, gw, gh), prev, cur, tiles(prev), tiles(cur)

    def test_global_vector(self):
        grid, prev, cur, _, _ = self._setup((4, 0))
        self.assertEqual(C.global_vector(cur, prev, 16), (4, 0))
        self.assertEqual(C.global_vector(prev, prev, 16), (0, 0))

    def test_scroll_moved(self):
        grid, prev, cur, t0, t1 = self._setup((4, 0))
        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=prev)
        o, st, s = C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur, grid=grid)
        self.assertEqual(s["global_mv"], [4, 0])
        moved = o["cls"] == C.C_MOVED
        self.assertGreaterEqual(int(moved.sum()) * 2, len(o) // 2)
        self.assertTrue((o["mv_dx"][moved] == 4).all() and (o["mv_dy"][moved] == 0).all())
        self.assertTrue((o["flags"][moved] & C.FLAG_GLOBAL_MV).all())

    def test_no_motion_given_stays_changed(self):
        grid, prev, cur, t0, t1 = self._setup((4, 0))
        st = C.ClassState.init(t0)
        o, st, s = C.classify_tiles(st, t1, np.zeros(len(t1), bool))
        self.assertFalse((o["cls"] == C.C_MOVED).any())
        self.assertTrue((o["cls"] == C.C_CHANGED).any())


class TestMutantsKilled(unittest.TestCase):
    """A no-hysteresis variant must flip class in the band (the hysteresis test would catch it)."""

    def test_band_zero_flips(self):
        p = dict(P.PARAMS); p["HYST_BAND_PERMILLE"] = 0
        t_enter, t_exit, _ = C.thresholds(p)
        self.assertEqual(t_enter, t_exit)
        self.assertNotEqual(C.thresholds()[0], C.thresholds()[1])


if __name__ == "__main__":
    unittest.main()
