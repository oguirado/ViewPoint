"""FC-A1/A2/A3 (AMEND_ACORE): DRIFT latch, FROZEN on fixed camera, FLICKER without a transition counter."""
import unittest

import numpy as np

from tests.test_change import mk, run
from visioncore import change as C
from visioncore.params import PARAMS


def _cls(res, tile):
    return [int(o["cls"][tile]) for o, _ in res]


class TestDriftLatch(unittest.TestCase):
    def test_drift_ramp_hold(self):  # 0.5 grey/frame (8 q4) for 30 frames, then stop
        frames = []
        for f in range(46):
            t = mk(3)
            t["y_q4"][0] = 1000 + 8 * min(f, 29)
            frames.append(t)
        c = _cls(run(frames), 0)
        self.assertTrue(all(x == C.C_DRIFT for x in c[5:30]), c)
        self.assertLessEqual(sum(1 for x in c[30:] if x == C.C_DRIFT), C.DRIFT_STALL - 1)
        self.assertEqual(c[-1], C.C_UNCHANGED)
        self.assertTrue((run(frames)[-1][0]["cls"][1:] == 0).all())

    def test_ref_not_reset_on_drift(self):
        frames = []
        for f in range(12):
            t = mk(1)
            t["y_q4"][0] = 1000 + 8 * f
            frames.append(t)
        st = C.ClassState.init(frames[0])
        C.classify_first(st, frames[0])
        for t in frames[1:]:
            C.classify_tiles(st, t, np.zeros(1, bool))
        self.assertEqual(int(st.ref["y_q4"][0]), 1000)  # still anchored at frame 0
        self.assertEqual(int(st.ref_max[0]), 88)

    def test_state_digest_covers_latch(self):
        t = mk(2)
        a, b = C.ClassState.init(t), C.ClassState.init(t)
        b.stall[0] = 3
        self.assertNotEqual(a.digest_bytes(), b.digest_bytes())

    def test_noise_not_drift(self):
        rng = np.random.RandomState(9)
        frames = []
        for _ in range(80):
            t = mk(200)
            t["y_q4"] += rng.randint(-8, 9, 200).astype(np.uint16)
            frames.append(t)
        for o, _ in run(frames, eq=[np.zeros(200, bool)] * 80)[1:]:
            self.assertFalse((o["cls"] == C.C_DRIFT).any())


def _scene(n_frames, spin_until, patch, still_from=None, slide=None, pan_all=False, n=1000, mover=30):
    """n tiles; `patch` tiles animate until spin_until then freeze; first `mover` tiles move every frame."""
    rng = np.random.RandomState(5)
    frames = []
    for f in range(n_frames):
        t = mk(n)
        t["y_q4"][:mover] = rng.randint(500, 3500, mover)
        if f < spin_until:
            t["y_q4"][patch] = rng.randint(500, 3500, len(patch))
        else:
            t["y_q4"][patch] = frames[spin_until - 1]["y_q4"][patch]
        frames.append(t)
    return frames


class TestFrozen(unittest.TestCase):
    def test_frozen_fixed_cam(self):  # 3 % of tiles move, no pan: spinner freezes after 24 frames of animation
        patch = np.arange(100, 104)
        res = run(_scene(24 + 16, 24, patch))
        for f in range(24 + 8, 40):
            self.assertTrue((res[f][0]["cls"][patch] == C.C_FROZEN).all(), f)
        self.assertFalse((res[24 + 3][0]["cls"][patch] == C.C_FROZEN).any())
        self.assertEqual(int((res[-1][0]["cls"][200:] == C.C_FROZEN).sum()), 0)  # static HUD

    def test_mutant_cov30_dies(self):
        patch = np.arange(100, 104)
        res = run(_scene(40, 24, patch), params=dict(PARAMS, FROZEN_COV_PPM=300000))
        self.assertFalse(any((o["cls"] == C.C_FROZEN).any() for o, _ in res))  # 3 % < 30 %: the old gate misses it

    def test_toast_stop_not_frozen(self):  # 4-frame slide then stop, scene alive via pan: pre_act = 4 < 8
        rng = np.random.RandomState(8)
        frames = []
        for f in range(40):
            t = mk(100)
            t["y_q4"][:40] = rng.randint(500, 3500, 40)
            t["y_q4"][50:55] = rng.randint(500, 3500, 5) if 10 <= f < 14 else 2000
            frames.append(t)
        res = run(frames)
        self.assertEqual(sum(int((o["cls"][50:55] == C.C_FROZEN).sum()) for o, _ in res), 0)

    def test_static_hud_never_frozen(self):
        res = run(_scene(30, 5, np.arange(100, 104), mover=60))
        self.assertEqual(sum(int((o["cls"][500:] == C.C_FROZEN).sum()) for o, _ in res), 0)


class TestFlicker(unittest.TestCase):
    def test_two_step_not_flicker(self):  # A 8 f, B 2 f, C far from A
        a, b, c = mk(1), mk(1), mk(1)
        b["y_q4"] = 2000
        c["y_q4"] = 3500
        res = run([a] * 8 + [b] * 2 + [c] * 4)
        self.assertNotIn(C.C_FLICKER, _cls(res, 0))

    def test_active_pattern_1001_not_flicker(self):  # A, B, B, B, C: active at t and t-3 only
        a, b, c = mk(1), mk(1), mk(1)
        b["y_q4"] = 2000
        c["y_q4"] = 3500
        res = run([a, a, b, b, b, c, c])
        self.assertNotIn(C.C_FLICKER, _cls(res, 0))

    def test_aba_still_flicker(self):
        a, b = mk(1), mk(1)
        b["y_q4"] = 1500
        self.assertEqual(run([a, a, b, a, a])[3][0]["cls"][0], C.C_FLICKER)


if __name__ == "__main__":
    unittest.main()
