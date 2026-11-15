"""Offline Area-C GM budget/lanes contract; real producer GM and S2 tiles."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
import hashlib
from threading import Barrier
import unittest
from unittest.mock import patch

import numpy as np

from eval.v2x import gm_bands_hv
from eval.v2x.per_window_detector import (
    ChangeResult, DetectorInputError, Handle, MissingEvidenceError,
    PerWindowDetector,
)
from eval.v2x.proto1_gm_budget import (
    BudgetedGM, consensus_op_envelope, estimate_op_envelope,
)
from visioncore import desc
from visioncore.grid import Grid


def rgb_plane(plane):
    return np.repeat(plane[:, :, None], 3, axis=2)


def rgb(value, h=16, w=16):
    return np.full((h, w, 3), value, dtype=np.uint8)


def tiles_for(frame):
    grid = Grid(frame.shape[1], frame.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(frame), grid, lazy_xh=True)
    return tiles


def pattern(h=48, w=48, seed=812):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (h, w), dtype=np.uint8)


def shifted(luma, dx=2, dy=1):
    result = np.zeros_like(luma)
    result[dy:, dx:] = luma[:-dy, :-dx]
    return result


class TestBudgetAdmission(unittest.TestCase):
    def test_missing_and_malformed_budget_is_typed(self):
        with self.assertRaisesRegex(MissingEvidenceError, "^GM budget not configured$"):
            BudgetedGM()
        for invalid in (True, False, 2.0, "500", -1, np.int64(600)):
            with self.subTest(value=repr(invalid)):
                with self.assertRaisesRegex(DetectorInputError,
                                            "^GM budget must be an exact nonnegative integer$"):
                    BudgetedGM(invalid)
        self.assertEqual(BudgetedGM(0).max_ops, 0)

    def test_exact_five_ops_per_pixel_on_flat_frames(self):
        p, c = np.full((16, 19), 34, np.uint8), np.full((16, 19), 79, np.uint8)
        necessary = 5 * p.size
        plugin = BudgetedGM(necessary - 1)
        with self.assertRaisesRegex(MissingEvidenceError,
                                    "^GM operation budget exhausted before estimate$"):
            plugin.estimate_global_motion(p, c, 18)
        self.assertEqual(plugin.OPS, 0)
        self.assertEqual(plugin.OPS_DETAIL, {})
        plugin = BudgetedGM(necessary)
        self.assertIsNone(plugin.estimate_global_motion(p, c, 18))
        self.assertEqual(plugin.OPS, necessary)
        self.assertEqual(plugin.OPS_DETAIL, {})
        self.assertEqual(gm_bands_hv.estimate_global_motion(p, c, 18), None)
        self.assertEqual(gm_bands_hv.OPS, necessary)

    def test_consensus_exact_integer_bound_and_pixel_oracle(self):
        prev = np.arange(17 * 19, dtype=np.uint32).reshape(17, 19).astype(np.uint8)
        cur = prev.copy()
        cur[2, 3] ^= np.uint8(1)
        cap = consensus_op_envelope(17, 19, 16)
        bad = BudgetedGM(cap - 1)
        with self.assertRaisesRegex(MissingEvidenceError,
                                    "^GM operation budget exhausted before consensus$"):
            bad.consensus_tiles(prev, cur, 16, 0, 0)
        self.assertEqual(bad.OPS, 0)
        plugin = BudgetedGM(cap)
        got = plugin.consensus_tiles(prev, cur, 16, 0, 0)
        direct = gm_bands_hv.consensus_tiles(prev, cur, 16, 0, 0)
        self.assertEqual(plugin.OPS, cap)
        self.assertEqual(gm_bands_hv.OPS, cap)
        np.testing.assert_array_equal(got, direct)
        # Independent hand-computed negative result for zero displacement:
        # nothing can be changed in place and simultaneously agree with prev.
        np.testing.assert_array_equal(got, np.zeros((2, 2), dtype=np.bool_))

    def test_paired_estimate_and_consensus_share_one_cap(self):
        p = pattern(16, 16, seed=100)
        estimate_cost = 5 * p.size  # identical textured frame, native fast path
        consensus_cost = consensus_op_envelope(16, 16, 16)
        self.assertEqual(consensus_cost, 5 * p.size)
        insufficient = BudgetedGM(estimate_cost + consensus_cost - 1)
        self.assertEqual(insufficient.estimate_global_motion(p, p, 0), (0, 0, 0))
        self.assertEqual(insufficient.OPS, estimate_cost)
        with self.assertRaisesRegex(MissingEvidenceError,
                                    "^GM operation budget exhausted before consensus$"):
            insufficient.consensus_tiles(p, p, 16, 0, 0)
        self.assertEqual(insufficient.OPS, 0)
        sufficient = BudgetedGM(estimate_cost + consensus_cost)
        self.assertEqual(sufficient.estimate_global_motion(p, p, 0), (0, 0, 0))
        np.testing.assert_array_equal(sufficient.consensus_tiles(p, p, 16, 0, 0),
                                      np.zeros((1, 1), dtype=np.bool_))
        self.assertEqual(sufficient.OPS, consensus_cost)

    def test_native_validation_errors_not_hidden_by_budget(self):
        p = pattern(16, 16)
        g = BudgetedGM(10**9)
        with self.assertRaisesRegex(TypeError, "^luma must be uint8$"):
            g.estimate_global_motion(p.astype(np.int16), p, 1)
        self.assertEqual(g.OPS, 0)
        with self.assertRaisesRegex(ValueError, "^frames must have the same nonempty H by W shape$"):
            g.estimate_global_motion(p, p[:, :-1], 1)
        with self.assertRaisesRegex(TypeError, "^max_d must be an integer$"):
            g.estimate_global_motion(p, p, True)
        with self.assertRaisesRegex(ValueError, "^max_d must be nonnegative$"):
            g.estimate_global_motion(p, p, -1)
        with self.assertRaisesRegex(TypeError, "^displacements must be integers$"):
            g.consensus_tiles(p, p, 16, False, 0)
        with self.assertRaisesRegex(ValueError, "^tile side must be a positive integer$"):
            g.consensus_tiles(p, p, 0, 0, 0)
        self.assertEqual(g.OPS, 0)

    def test_full_search_parity_and_real_op_detail(self):
        p = pattern(48, 48)
        c = shifted(p)
        md = 4
        expected = gm_bands_hv.estimate_global_motion(p, c, md)
        # Independent pixel construction: exact shift (2 right, 1 down).
        self.assertIsNotNone(expected, "seeded shift must be a positive GM control")
        self.assertEqual(expected[:2], (2, 1))
        measured = gm_bands_hv.OPS
        details = dict(gm_bands_hv.OPS_DETAIL)
        limit = estimate_op_envelope(48, 48, md) + consensus_op_envelope(48, 48, 16)
        g = BudgetedGM(limit)
        self.assertEqual(g.estimate_global_motion(p, c, md), expected)
        self.assertEqual(g.OPS, measured)
        self.assertEqual(g.OPS_DETAIL, details)
        self.assertGreater(measured, 0)
        self.assertGreaterEqual(g.max_ops, measured)
        if expected is not None and expected[:2] != (0, 0):
            x, y = expected[:2]
            expected_tiles = gm_bands_hv.consensus_tiles(p, c, 16, x, y)
            expected_ops = gm_bands_hv.OPS
            expected_details = dict(gm_bands_hv.OPS_DETAIL)
            got_tiles = g.consensus_tiles(p, c, 16, x, y)
            np.testing.assert_array_equal(got_tiles, expected_tiles)
            # A translated 48x48 frame has four fully in-bounds 16x16
            # destination tiles in the lower-right 2x2 block.
            np.testing.assert_array_equal(got_tiles, np.array(
                [[False, False, False], [False, True, True], [False, True, True]],
                dtype=np.bool_))
            self.assertEqual(g.OPS, expected_ops)
            self.assertEqual(g.OPS_DETAIL, expected_details)
        else:
            # GM is allowed to abstain; this fixture must not be silently turned
            # into a positive claim by the budget adapter.
            self.assertEqual(g.OPS, measured)
        # Every public call resets the visible OPS as in the upstream decorator.
        zero = np.zeros((8, 8), dtype=np.uint8)
        self.assertIsNone(g.estimate_global_motion(zero, zero, 0))
        self.assertEqual(g.OPS, 5 * 64)

    def test_insufficient_reference_count_always_refused(self):
        p, c = pattern(32, 32, seed=998), shifted(pattern(32, 32, seed=998))
        ref = gm_bands_hv.estimate_global_motion(p, c, 2)
        required_ops = gm_bands_hv.OPS
        self.assertGreater(required_ops, 1)
        with self.assertRaisesRegex(MissingEvidenceError,
                                    "^GM operation budget exhausted before (estimate|prepare|candidates|verification)$"):
            BudgetedGM(required_ops - 1).estimate_global_motion(p, c, 2)
        sufficient = BudgetedGM(estimate_op_envelope(32, 32, 2))
        self.assertEqual(sufficient.estimate_global_motion(p, c, 2), ref)
        self.assertEqual(sufficient.OPS, required_ops)

    def test_contextvar_counter_leakage_under_two_simultaneous_calls(self):
        cases = [(pattern(16, 16, 4), pattern(16, 16, 4), 0),
                 (pattern(32, 32, 5), shifted(pattern(32, 32, 5)), 2)]
        independent = []
        for p, c, d in cases:
            v = gm_bands_hv.estimate_global_motion(p, c, d)
            independent.append((v, gm_bands_hv.OPS, dict(gm_bands_hv.OPS_DETAIL)))
        shared = BudgetedGM(max(estimate_op_envelope(p.shape[0], p.shape[1], d)
                                 for p, c, d in cases))
        begin, finish = Barrier(3, timeout=40), Barrier(3, timeout=40)

        def worker(inp):
            p, c, d = inp
            begin.wait()
            vector = shared.estimate_global_motion(p, c, d)
            finish.wait()   # both have completed before either samples OPS
            return vector, shared.OPS, shared.OPS_DETAIL

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, case) for case in cases]
            begin.wait()
            finish.wait()
            observed = [future.result(timeout=40) for future in futures]
        self.assertEqual(observed, independent)

    def test_conservative_planning_bounds_real_operations(self):
        for h, w, d in ((1, 1, 0), (16, 16, 1), (17, 19, 4), (48, 48, 2)):
            with self.subTest(shape=(h, w), d=d):
                a = pattern(h, w, seed=h * 23 + w)
                b = np.roll(a, shift=1, axis=1)
                gm_bands_hv.estimate_global_motion(a, b, d)
                self.assertLessEqual(gm_bands_hv.OPS, estimate_op_envelope(h, w, d))
        with self.assertRaisesRegex(ValueError, "^GM envelope geometry must be positive exact integers$"):
            estimate_op_envelope(True, 2, 1)
        with self.assertRaisesRegex(ValueError, "^GM consensus envelope geometry must be positive exact integers$"):
            consensus_op_envelope(8, 8, False)


class TestBudgetMutationProbes(unittest.TestCase):
    """Two deliberately invalid local variants; baseline assertions run first."""

    def test_two_mutants_are_killed_by_independent_contract_assertions(self):
        p = np.full((16, 16), 12, np.uint8)
        c = np.full((16, 16), 18, np.uint8)
        required = 5 * p.size
        baseline = BudgetedGM(required - 1)
        with self.assertRaises(MissingEvidenceError):
            baseline.estimate_global_motion(p, c, 0)
        control = BudgetedGM(required)
        self.assertIsNone(control.estimate_global_motion(p, c, 0))
        self.assertEqual(control.OPS, required)

        # M1: suppress the admission guard.  It incorrectly executes with a
        # strict one-below budget; a contract assertion distinguishes it.
        m1 = BudgetedGM(required - 1)
        with patch.object(m1, "_admit", side_effect=lambda phase, upper, preceding=0: gm_bands_hv.OPS):
            result = m1.estimate_global_motion(p, c, 0)
        self.assertIsNone(result)
        self.assertGreater(m1.OPS, m1.max_ops)  # killed: accepted forbidden work

        # M2: change the visible operation count without changing GM's decision.
        m2 = BudgetedGM(required)
        good_finish = m2._finish

        def bad_finish(spent):
            good_finish(spent)
            m2._ops.set(spent + 1)

        with patch.object(m2, "_finish", side_effect=bad_finish):
            m2.estimate_global_motion(p, c, 0)
        self.assertNotEqual(m2.OPS, required)  # killed: violates independent 5*N


class TestDetectorIntegration(unittest.TestCase):
    def test_real_s2_dtype_morton_and_result_order(self):
        handle = Handle("cdp_tab", "budget-tab", 1)
        self.assertEqual([field.name for field in fields(ChangeResult)],
                         ["handle", "epoch", "settled", "claims", "ops"])
        base = rgb(42, 17, 19)
        grid = Grid(19, 17)
        tiles = tiles_for(base)
        self.assertEqual(tiles.dtype, desc.TILE_DTYPE)
        np.testing.assert_array_equal(tiles["morton"], grid.mortons)
        budget = 5 * (grid.gh * grid.s) * (grid.gw * grid.s) * 10
        detector = PerWindowDetector(gm=BudgetedGM(budget))
        result = detector.update(handle, tiles, base, epoch=0, frame_seq=0)
        self.assertIsInstance(result, ChangeResult)
        self.assertEqual(result.handle, handle)
        self.assertEqual(result.epoch, 0)
        self.assertFalse(result.settled)
        self.assertEqual(list(result.ops), ["gm", "fl"])
        self.assertEqual(result.ops["gm"], 0)
        np.testing.assert_array_equal(result.claims, tiles)
        self.assertFalse(result.claims.flags.writeable)
        lane = detector._lanes[handle].stream
        self.assertEqual(lane.prev_digest, hashlib.sha256(base.tobytes(order="C")).digest())
        self.assertFalse(lane.prev_rgb.flags.writeable)
        self.assertFalse(lane.prev_y.flags.writeable)

    def test_detector_full_reference_decisions_and_operation_counts(self):
        a = Handle("x11_window", "parity-a", 1)
        p = rgb_plane(pattern(48, 48))
        c = rgb_plane(shifted(pattern(48, 48)))
        limit = (estimate_op_envelope(48, 48, 47)
                 + consensus_op_envelope(48, 48, Grid(48, 48).s))
        bounded = PerWindowDetector(gm=BudgetedGM(limit))
        reference = PerWindowDetector(gm=gm_bands_hv)
        for i, frame in enumerate((p, c, p)):
            x = bounded.update(a, tiles_for(frame), frame, epoch=5, frame_seq=i)
            y = reference.update(a, tiles_for(frame), frame, epoch=5, frame_seq=i)
            np.testing.assert_array_equal(x.claims, y.claims)
            self.assertEqual(x.ops, y.ops)
            self.assertEqual(x.settled, y.settled)
            self.assertEqual(x.epoch, y.epoch)
        self.assertGreater(x.ops["gm"], 0)

    def test_invalid_rgb_tiles_epochs_sequences_and_geometry(self):
        h = Handle("capsule", "window", 1)
        d = PerWindowDetector(gm=BudgetedGM(10000))
        original = rgb(22, 16, 16)
        tiles = tiles_for(original)
        with self.assertRaisesRegex(DetectorInputError,
                                    "^rgb must be a nonempty HxWx3 uint8 ndarray$"):
            d.update(h, tiles, original.astype(np.int16), epoch=1, frame_seq=0)
        wrong = tiles.copy()
        wrong["morton"][0] = 65535
        with self.assertRaisesRegex(DetectorInputError,
                                    "^tiles must match the exact S2 grid, dtype and Morton order$"):
            d.update(h, wrong, original, epoch=1, frame_seq=0)
        with self.assertRaisesRegex(DetectorInputError, "^epoch must be a nonnegative integer$"):
            d.update(h, tiles, original, epoch=True, frame_seq=0)
        self.assertEqual(d.update(h, tiles, original, epoch=1, frame_seq=0).ops["gm"], 0)
        with self.assertRaisesRegex(DetectorInputError, "^non-increasing local frame_seq$"):
            d.update(h, tiles, original, epoch=1, frame_seq=0)
        with self.assertRaisesRegex(DetectorInputError, "^stale geometry epoch$"):
            d.update(h, tiles, original, epoch=0, frame_seq=1)
        resized = rgb(22, 17, 16)
        with self.assertRaisesRegex(DetectorInputError, "^geometry change requires new epoch$"):
            d.update(h, tiles_for(resized), resized, epoch=1, frame_seq=1)
        self.assertEqual(d.update(h, tiles_for(resized), resized,
                                  epoch=2, frame_seq=1).epoch, 2)

    def test_failed_lane_discards_only_its_history(self):
        # A has 16x16 padding and 5*256 GM work; B has 32x32 and needs
        # 5*1024 even for a flat-frame early abstention, above this limit.
        budget = 3000
        detector = PerWindowDetector(gm=BudgetedGM(budget))
        solo_a = PerWindowDetector(gm=BudgetedGM(budget))
        ha = Handle("cdp_tab", "small-lane", 1)
        hb = Handle("godot_window", "large-lane", 1)
        a0, a1 = rgb(20, 16, 16), rgb(30, 16, 16)
        b0, b1 = rgb(50, 32, 32), rgb(70, 32, 32)
        detector.update(ha, tiles_for(a0), a0, epoch=2, frame_seq=0)
        detector.update(hb, tiles_for(b0), b0, epoch=9, frame_seq=0)
        solo_a.update(ha, tiles_for(a0), a0, epoch=2, frame_seq=0)
        a_result = detector.update(ha, tiles_for(a1), a1, epoch=2, frame_seq=1)
        expected_a = solo_a.update(ha, tiles_for(a1), a1, epoch=2, frame_seq=1)
        self.assertEqual(a_result.ops, expected_a.ops)
        self.assertEqual(a_result.ops["gm"], 1280)
        with self.assertRaisesRegex(MissingEvidenceError,
                                    "^GM operation budget exhausted before estimate$"):
            detector.update(hb, tiles_for(b1), b1, epoch=9, frame_seq=1)
        self.assertIsNone(detector._lanes[hb].stream)
        self.assertIsNotNone(detector._lanes[ha].stream)
        reset_b = detector.update(hb, tiles_for(b1), b1, epoch=9, frame_seq=2)
        self.assertEqual(reset_b.ops["gm"], 0)  # no stale preceding B frame
        continued_a = detector.update(ha, tiles_for(a1), a1, epoch=2, frame_seq=2)
        solo_cont = solo_a.update(ha, tiles_for(a1), a1, epoch=2, frame_seq=2)
        self.assertEqual(continued_a.ops, solo_cont.ops)
        np.testing.assert_array_equal(continued_a.claims, solo_cont.claims)
        self.assertEqual(continued_a.settled, solo_cont.settled)


if __name__ == "__main__":
    unittest.main()
