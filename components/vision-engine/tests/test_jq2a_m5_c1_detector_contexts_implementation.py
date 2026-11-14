"""Offline production-path gates for target-local M5 detector and FL accounting."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import fields
from threading import Barrier
import unittest

import numpy as np

from visioncore import desc
from visioncore.grid import Grid
from eval.v2x import fl_blinkprior, fl_v3
from eval.v2x.per_window_detector import (
    ChangeResult, DetectorInputError, Handle, MissingEvidenceError,
    PerWindowDetector,
)


def frame(value, size=16):
    return np.full((size, size, 3), value, dtype=np.uint8)


def baseline(rgb, cls=0):
    g = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(g.pad(rgb), g, lazy_xh=True)
    tiles = tiles.copy()
    tiles["cls"] = cls
    tiles["mv_dx"] = 0
    tiles["mv_dy"] = 0
    tiles["xh64"] = 0  # adversarial native lazy-hash provenance
    return tiles


def play(detector, handle, frames, *, epoch=1, seq_start=0, classes=None):
    return [detector.update(handle, baseline(rgb, classes[i] if classes else 0), rgb,
                            epoch=epoch, frame_seq=seq_start + i)
            for i, rgb in enumerate(frames)]



def canvas_noise(seed):
    # Fixed integer UI-canvas renderer formula used by the source regression.
    x = np.arange(16, dtype=np.uint32)[None, :]
    y = np.arange(16, dtype=np.uint32)[:, None]
    z = (x * np.uint32(374761393)) ^ (y * np.uint32(668265263)) ^ np.uint32(seed)
    z = (z ^ (z >> np.uint32(13))) * np.uint32(1274126177)
    z ^= z >> np.uint32(16)
    grey = (z & np.uint32(31)).astype(np.uint8)
    bands = ((x // 8) % 3).astype(np.uint8) + ((y // 13) % 3).astype(np.uint8)
    np.subtract(bands, 3, out=bands, where=bands >= 3)
    grey += bands * np.uint8(23)
    grey += np.uint8(45)
    rgb = np.empty((16,16,3), dtype=np.uint8)
    rgb[:,:,0] = grey
    rgb[:,:,1] = grey + np.uint8(12)
    rgb[:,:,2] = grey + np.uint8(24)
    return rgb


class TestPerWindowDetector(unittest.TestCase):
    def setUp(self):
        self.a = Handle("cdp_tab", "tab:10", 0)
        self.b = Handle("godot_window", "arena:2", 2**85)

    def test_canonical_records_and_validation(self):
        self.assertEqual([f.name for f in fields(Handle)],
                         ["kind", "canonical_id", "generation"])
        self.assertEqual([f.name for f in fields(ChangeResult)],
                         ["handle", "epoch", "settled", "claims", "ops"])
        with self.assertRaises(DetectorInputError):
            Handle("unknown", "id", 0)
        with self.assertRaises(DetectorInputError):
            Handle("cdp_tab", "id", True)
        d = PerWindowDetector()
        rgb = frame(77)
        inp = baseline(rgb)
        out = d.update(self.a, inp, rgb, epoch=4, frame_seq=0)
        self.assertEqual(out.handle, self.a)
        self.assertEqual(out.epoch, 4)
        self.assertFalse(out.settled)
        self.assertEqual(out.claims.dtype, inp.dtype)
        self.assertFalse(out.claims.flags.writeable)
        np.testing.assert_array_equal(out.claims, inp)
        with self.assertRaises(DetectorInputError):
            d.update(self.a, inp, rgb, epoch=4, frame_seq=0)
        with self.assertRaises(DetectorInputError):
            d.update(self.a, inp, rgb, epoch=3, frame_seq=1)
        with self.assertRaises(DetectorInputError):
            d.update(self.a, inp, rgb, epoch=4, frame_seq=1.0)
        with self.assertRaises(DetectorInputError):
            d.update(self.a, baseline(frame(77,32)), frame(77,32), epoch=4, frame_seq=1)
        self.assertFalse(d.update(self.a, inp, rgb, epoch=4, frame_seq=1).settled)

    def test_interleaving_matches_independent_replay_and_ops(self):
        aa = [frame(31) for _ in range(8)] + [frame(93) for _ in range(8)] + [frame(31) for _ in range(8)] + [frame(93) for _ in range(8)]
        bb = [frame(int(x)) for x in (80, 80, 84, 85, 85, 93, 93, 94) * 4]
        solo_a = play(PerWindowDetector(), self.a, aa)
        solo_b = play(PerWindowDetector(), self.b, bb)
        shared = PerWindowDetector()
        got_a, got_b = [], []
        for i, (a, b) in enumerate(zip(aa, bb)):
            got_a.append(shared.update(self.a, baseline(a), a, epoch=1, frame_seq=i))
            got_b.append(shared.update(self.b, baseline(b), b, epoch=1, frame_seq=i))
        for actual, serial in [(got_a, solo_a), (got_b, solo_b)]:
            for x, y in zip(actual, serial):
                np.testing.assert_array_equal(x.claims, y.claims)
                self.assertEqual(x.ops, y.ops)
                self.assertEqual(x.settled, y.settled)
        # A has a real two-state caret history, B must not disturb its toggle.
        self.assertEqual(int(got_a[24].claims["cls"][0]), fl_v3.C_FLICKER)
        self.assertGreater(int(got_a[24].ops["fl"]), 0)
        self.assertEqual(got_a[24].ops, solo_a[24].ops)

    def test_owned_rgb_and_lazy_zero_never_replace_eager(self):
        d = PerWindowDetector()
        original = frame(55)
        lazy = baseline(original)
        d.update(self.a, lazy, original, epoch=1, frame_seq=0)
        original[:] = 200
        lazy["xh64"][:] = 888
        following = frame(55)
        out = d.update(self.a, baseline(following), following, epoch=1, frame_seq=1)
        self.assertTrue(np.all(out.claims["cls"] == 0))
        # This is also a regression against capturing caller mutable RGB as history.
        self.assertEqual(out.ops["gm"], 0)

    def test_gap_epoch_reset_and_settle_are_target_local(self):
        d = PerWindowDetector()
        for i in range(4):
            a = d.update(self.a, baseline(frame(40)), frame(40), epoch=1, frame_seq=i)
            b = d.update(self.b, baseline(frame(80)), frame(80), epoch=7, frame_seq=i)
        self.assertTrue(a.settled)
        self.assertTrue(b.settled)
        discontinuous = d.update(self.b, baseline(frame(80)), frame(80), epoch=7, frame_seq=10)
        self.assertFalse(discontinuous.settled)
        self.assertTrue(d.update(self.a, baseline(frame(40)), frame(40), epoch=1, frame_seq=4).settled)
        self.assertFalse(d.update(self.a, baseline(frame(40)), frame(40), epoch=2, frame_seq=5).settled)
        d.reset_window(self.b)
        self.assertFalse(d.update(self.b, baseline(frame(80)), frame(80), epoch=7, frame_seq=0).settled)

    def test_same_target_baseline_flicker_exact_return_is_not_demoted(self):
        d = PerWindowDetector()
        a, b = frame(26), frame(204)
        # Exact A/B/A content return: P1 must preserve baseline class FLICKER.
        for i, rgb in enumerate((a, b, a)):
            r = d.update(self.a, baseline(rgb, 4 if i == 2 else 2), rgb,
                         epoch=0, frame_seq=i)
        self.assertEqual(int(r.claims["cls"][0]), 4)
        # Sibling with intervening seqs must not corrupt the A/B/A observation.
        other = PerWindowDetector()
        other.update(self.a, baseline(a,2), a, epoch=0, frame_seq=0)
        other.update(self.b, baseline(frame(54)), frame(54), epoch=0, frame_seq=99)
        other.update(self.a, baseline(b,2), b, epoch=0, frame_seq=1)
        other.update(self.b, baseline(frame(55)), frame(55), epoch=0, frame_seq=100)
        s = other.update(self.a, baseline(a,4), a, epoch=0, frame_seq=2)
        np.testing.assert_array_equal(s.claims, r.claims)

    def test_canvas_nonreturn_veto_uses_eager_pixel_history(self):
        d = PerWindowDetector()
        f0, f1, f2 = (canvas_noise(seed) for seed in (0, 1, 2))
        for i, rgb in enumerate((f0, f1, f2)):
            result = d.update(self.a, baseline(rgb, 4 if i == 2 else 2), rgb,
                              epoch=1, frame_seq=i)
        # Three distinct eager RGB hashes, weak descriptor return and strong
        # pixel SAD: the real P2 evidence demotes baseline FLICKER to CHANGED.
        self.assertEqual(int(result.claims["cls"][0]), 2)
        self.assertFalse(result.settled)

    def test_motion_consensus_wins_even_when_fl_requests_flicker(self):
        class MotionGM:
            @staticmethod
            def estimate_global_motion(prev, cur, max_d):
                return (16, 0, 0)

            @staticmethod
            def consensus_tiles(prev, cur, side, dx, dy):
                h, w = cur.shape
                mask = np.zeros((h//side, w//side), dtype=np.bool_)
                for ty in range(h//side):
                    for tx in range(w//side):
                        y, x = ty*side, tx*side
                        if x >= dx and not np.array_equal(cur[y:y+side,x:x+side], prev[y:y+side,x:x+side]):
                            mask[ty,tx] = np.array_equal(cur[y:y+side,x:x+side], prev[y:y+side,x-dx:x+side-dx])
                return mask

        class AllFlickerTracker:
            def __init__(self, n_tiles, max_half):
                self.n = n_tiles
            def update(self, hashes):
                return np.full(self.n, 4, dtype=np.int64), np.zeros(self.n, dtype=np.int64)

        class FlFixture:
            FlickerTracker = AllFlickerTracker

        p = np.zeros((16,32,3), dtype=np.uint8)
        p[:, :16, :] = 180
        c = p.copy()
        c[:,16:,:] = p[:,:16,:]
        c[:,:16,:] = 7
        d = PerWindowDetector(gm=MotionGM(), fl=FlFixture)
        d.update(self.a, baseline(p), p, epoch=1, frame_seq=0)
        out = d.update(self.a, baseline(c), c, epoch=1, frame_seq=1)
        self.assertEqual(int(out.claims["cls"][1]), 3)
        self.assertEqual(int(out.claims["mv_dx"][1]), 16)
        self.assertFalse(out.settled)

    def test_missing_veto_evidence_refuses_success(self):
        class BadVeto:
            def __init__(self, grid):
                pass
            def update(self, tiles, rgb, *, frame_id):
                return object()

        d = PerWindowDetector(veto=BadVeto)
        with self.assertRaises(MissingEvidenceError):
            d.update(self.a, baseline(frame(40)), frame(40), epoch=0, frame_seq=0)

    def test_fl_counter_contexts_are_isolated_and_restored(self):
        fl_blinkprior.reset_ops()
        fl_blinkprior.FlickerTracker(1).update([29])
        prior = fl_blinkprior.OPS
        barrier = Barrier(2)

        class Racing(fl_v3.FlickerTracker):
            def update(self, hashes):
                barrier.wait(timeout=5)
                value = super().update(hashes)
                barrier.wait(timeout=5)
                return value

        def run(n):
            return fl_v3.update_with_ops(Racing(n), [3] * n)[1]

        c1 = copy_context()
        c2 = copy_context()
        with ThreadPoolExecutor(max_workers=2) as executor:
            x = executor.submit(c1.run, run, 1)
            y = executor.submit(c2.run, run, 5)
            concurrent = (x.result(), y.result())
        solo = tuple(fl_v3.update_with_ops(fl_v3.FlickerTracker(n), [3] * n)[1]
                     for n in (1, 5))
        self.assertEqual(concurrent, solo)
        self.assertGreater(solo[1], solo[0])
        self.assertEqual(fl_blinkprior.OPS, prior)

        class Explodes:
            def update(self, hashes):
                raise RuntimeError("deliberate source failure")
        with self.assertRaisesRegex(RuntimeError, "deliberate"):
            fl_v3.update_with_ops(Explodes(), [2])
        self.assertEqual(fl_blinkprior.OPS, prior)


if __name__ == "__main__":
    unittest.main()
