"""RAM-only, fixed-seed hash tests for the caret Wald SPRT variant.

Fixtures contain descriptor hashes only. No frames, luma planes, pixel buffers,
or binary fixture files are read or written.
"""

import math
import unittest

import numpy as np

from eval.v2x import fl_caret_sprt as sprt
from eval.v2x.fl_caret_sprt import FlickerTracker


def _hash_bank(count, seed):
    """Return reproducible distinct uint64 descriptors generated entirely in RAM."""
    rng = np.random.default_rng(seed)
    hashes = rng.integers(0, np.iinfo(np.uint64).max, count, dtype=np.uint64)
    if np.unique(hashes).size != count:
        raise AssertionError("fixture seed unexpectedly produced duplicate hashes")
    return hashes


def _runs(hashes, lengths):
    return np.repeat(np.asarray(hashes, dtype=np.uint64), lengths).reshape(-1, 1)


def _blink(half, frames, seed):
    pair = _hash_bank(2, seed)
    return pair[(np.arange(frames) // half) % 2].reshape(-1, 1)


def _morton(x, y):
    """Morton index of a tile in a 4-by-4 RAM descriptor grid."""
    return (x & 1) | ((y & 1) << 1) | ((x & 2) << 1) | ((y & 2) << 2)


class TestFlickerTrackerCaretSPRT(unittest.TestCase):
    def _trace(self, tracker, frames):
        states, periods = [], []
        for frame_number, frame in enumerate(frames):
            before = frame.copy()
            frame.setflags(write=False)
            state, half_period = tracker.update(frame)
            self.assertEqual(state.shape, (tracker.n_tiles,))
            self.assertEqual(half_period.shape, (tracker.n_tiles,))
            self.assertIn(state.dtype.kind, "iu")
            self.assertIn(half_period.dtype.kind, "iu")
            self.assertTrue(np.isin(state, [0, 2, 4]).all())
            np.testing.assert_array_equal(frame, before)
            claims = state == 4
            self.assertTrue((half_period[claims] > 0).all())
            self.assertTrue((half_period[claims] <= tracker.max_half).all())
            self.assertTrue((half_period[~claims] == 0).all())
            if frame_number:
                held = frame == frames[frame_number - 1]
                self.assertTrue((state[held] == 0).all())
            states.append(state.copy())
            periods.append(half_period.copy())
        return np.stack(states), np.stack(periods)

    def _assert_unclaimed(self, frames, max_half=64):
        states, periods = self._trace(
            FlickerTracker(frames.shape[1], max_half=max_half), frames
        )
        self.assertFalse((states == 4).any())
        self.assertFalse(periods.any())
        return states

    def test_documented_wald_thresholds_and_fixed_point_increments(self):
        self.assertEqual(sprt.SCORE_SCALE, 1024)
        self.assertEqual(sprt.ALPHA, 0.001)
        self.assertEqual(sprt.BETA, 0.1)
        self.assertEqual(sprt.P_MATCH_H1, 0.99)
        self.assertEqual(sprt.P_MATCH_H0, 0.01)
        upper = math.ceil(sprt.SCORE_SCALE * math.log((1 - sprt.BETA) / sprt.ALPHA))
        lower = math.floor(sprt.SCORE_SCALE * math.log(sprt.BETA / (1 - sprt.ALPHA)))
        positive = round(sprt.SCORE_SCALE * math.log(sprt.P_MATCH_H1 / sprt.P_MATCH_H0))
        negative = round(
            sprt.SCORE_SCALE
            * math.log((1 - sprt.P_MATCH_H1) / (1 - sprt.P_MATCH_H0))
        )
        self.assertEqual((upper, lower, positive, negative), (6966, -2357, 4705, -4705))
        for name, expected in (
            ("UPPER_THRESHOLD", upper),
            ("LOWER_THRESHOLD", lower),
            ("MATCH_INCREMENT", positive),
            ("MISMATCH_INCREMENT", negative),
        ):
            self.assertIsInstance(getattr(sprt, name), (int, np.integer))
            self.assertEqual(getattr(sprt, name), expected)
        self.assertIn(
            "UPPER_THRESHOLD = ceil(SCORE_SCALE * log((1 - BETA) / ALPHA)) = 6966",
            sprt.__doc__,
        )
        self.assertIn(
            "LOWER_THRESHOLD = floor(SCORE_SCALE * log(BETA / (1 - ALPHA))) = -2357",
            sprt.__doc__,
        )
        self.assertLess(positive, upper)
        self.assertGreaterEqual(2 * positive, upper)

    def test_30_and_15_frame_blinks_claim_before_120_frames(self):
        for half in (30, 15):
            with self.subTest(half=half):
                frames = _blink(half, 120, seed=100 + half)
                states, periods = self._trace(FlickerTracker(1), frames)
                expected = np.arange(3 * half, 120, half)
                np.testing.assert_array_equal(np.flatnonzero(states[:, 0] == 4), expected)
                np.testing.assert_array_equal(periods[expected, 0], half)
                self.assertEqual(int(expected[0]), 3 * half)
                self.assertLess(int(expected[0]), 120)
                for frame in range(1, 3 * half):
                    self.assertEqual(int(states[frame, 0]), 2 if frame % half == 0 else 0)

    def test_evidence_accumulates_on_completed_runs_only(self):
        half = 5
        tracker = FlickerTracker(1)
        frames = _blink(half, 3 * half + 1, seed=201)
        for frame_number, hashes in enumerate(frames):
            state, period = tracker.update(hashes)
            self.assertIn(tracker.scores.dtype.kind, "iu")
            if frame_number < 2 * half:
                self.assertEqual(int(tracker.scores[0]), 0)
            elif frame_number < 3 * half:
                self.assertEqual(int(tracker.scores[0]), sprt.MATCH_INCREMENT)
            else:
                self.assertGreaterEqual(int(tracker.scores[0]), sprt.UPPER_THRESHOLD)
            if frame_number < 3 * half:
                self.assertNotEqual(int(state[0]), 4)
                self.assertEqual(int(period[0]), 0)
            else:
                self.assertEqual((int(state[0]), int(period[0])), (4, half))

    def test_negative_trials_reset_at_lower_threshold(self):
        half = 5
        a, b, c, d = _hash_bank(4, seed=202)
        frames = _runs([a, b, a, c, d, c, d], [half] * 7)
        tracker = FlickerTracker(1)
        scores, claims = [], []
        for frame_number, hashes in enumerate(frames):
            state, period = tracker.update(hashes)
            scores.append(int(tracker.scores[0]))
            if state[0] == 4:
                claims.append(frame_number)
                self.assertEqual(int(period[0]), half)
            else:
                self.assertEqual(int(period[0]), 0)
        self.assertEqual(scores[2 * half], sprt.MATCH_INCREMENT)
        self.assertEqual(scores[3 * half], 0)
        # The next negative trial crosses -2357, so a fresh score starts at zero.
        self.assertEqual(scores[4 * half], 0)
        self.assertEqual(scores[5 * half], sprt.MATCH_INCREMENT)
        self.assertEqual(claims, [6 * half])

    def test_accepted_score_resets_after_two_failed_completed_runs(self):
        half = 15
        a, b, c, d = _hash_bank(4, seed=203)
        tracker = FlickerTracker(1)
        self._trace(tracker, _runs([a, b, a, b], [half] * 4))
        self.assertEqual(int(tracker.scores[0]), 6966)
        state, period = tracker.update(np.array([c], dtype=np.uint64))
        self.assertEqual((int(state[0]), int(period[0])), (2, 0))
        self.assertEqual(int(tracker.scores[0]), 2261)
        self._trace(tracker, _runs([c], [half - 1]))
        state, period = tracker.update(np.array([d], dtype=np.uint64))
        self.assertEqual((int(state[0]), int(period[0])), (2, 0))
        self.assertEqual(int(tracker.scores[0]), 0)

    def test_partial_first_run_recovers_with_zero_and_uint64_max_hashes(self):
        endpoints = np.array([0, np.iinfo(np.uint64).max], dtype=np.uint64)
        pair = np.random.default_rng(204).permutation(endpoints)
        frames = _runs(pair[np.arange(6) % 2], [1, 30, 30, 30, 30, 30])
        states, periods = self._trace(FlickerTracker(1), frames)
        np.testing.assert_array_equal(np.unique(frames), endpoints)
        claims = np.flatnonzero(states[:, 0] == 4)
        np.testing.assert_array_equal(claims, [91, 121])
        np.testing.assert_array_equal(periods[claims, 0], 30)

    def test_static_is_never_claimed(self):
        frames = np.repeat(_hash_bank(1, seed=301), 400).reshape(-1, 1)
        states = self._assert_unclaimed(frames)
        self.assertFalse(states.any())

    def test_typing_distinct_content_with_irregular_holds(self):
        lengths = np.tile([1, 7, 3, 12, 2, 11], 8)
        frames = _runs(_hash_bank(len(lengths), seed=302), lengths)
        states = self._assert_unclaimed(frames)
        transitions = np.flatnonzero(frames[1:, 0] != frames[:-1, 0]) + 1
        np.testing.assert_array_equal(states[transitions, 0], 2)

    def test_scrolling_ram_grid_in_morton_order(self):
        n_frames = 160
        strip = _hash_bank((n_frames + 3) * 4, seed=303).reshape(n_frames + 3, 4)
        frames = np.empty((n_frames, 16), dtype=np.uint64)
        for y in range(4):
            for x in range(4):
                frames[:, _morton(x, y)] = strip[y : y + n_frames, x]
        states = self._assert_unclaimed(frames)
        np.testing.assert_array_equal(states[1:], 2)

    def test_slow_fade_with_a_new_hash_on_every_frame(self):
        frames = _hash_bank(240, seed=304).reshape(-1, 1)
        states = self._assert_unclaimed(frames)
        np.testing.assert_array_equal(states[1:], 2)

    def test_three_state_cycle_never_claimed(self):
        for half in (1, 15, 30):
            with self.subTest(half=half):
                cycle = np.tile(_hash_bank(3, seed=305 + half), 6)
                self._assert_unclaimed(_runs(cycle, [half] * len(cycle)))

    def test_jitter_between_14_and_16_frames(self):
        lengths = [14, 16] * 8
        pair = _hash_bank(2, seed=401)
        frames = _runs(np.tile(pair, 8), lengths)
        states, periods = self._trace(FlickerTracker(1), frames)
        transitions = np.cumsum(lengths)[:-1]
        claims = np.flatnonzero(states[:, 0] == 4)
        self.assertGreater(len(claims), 0)
        self.assertLessEqual(int(claims[0]), sum(lengths[:5]))
        self.assertTrue(np.isin(claims, transitions).all())
        np.testing.assert_array_equal(periods[claims, 0], 15)
        # Once enough stable jitter has been observed, every transition validates.
        for frame in transitions[5:]:
            self.assertEqual(int(states[frame, 0]), 4)

    def test_asymmetric_duty_cycle_is_rejected(self):
        pair = _hash_bank(2, seed=402)
        self._assert_unclaimed(_runs(np.tile(pair, 8), [10, 20] * 8))

    def test_short_run_after_acceptance_is_not_claimed(self):
        half = 15
        frames = _blink(half, 3 * half + 1, seed=403)
        tracker = FlickerTracker(1)
        states, _ = self._trace(tracker, frames)
        self.assertEqual(int(states[-1, 0]), 4)
        tracker.update(frames[-1])  # The new run now lasts two frames.
        state, period = tracker.update(frames[0])
        self.assertEqual((int(state[0]), int(period[0])), (2, 0))

    def test_third_state_after_acceptance_is_not_claimed(self):
        half = 15
        a, b, c = _hash_bank(3, seed=404)
        tracker = FlickerTracker(1)
        states, _ = self._trace(tracker, _runs([a, b, a, b], [half] * 4))
        self.assertEqual(int(states[3 * half, 0]), 4)
        state, period = tracker.update(np.array([c], dtype=np.uint64))
        self.assertEqual((int(state[0]), int(period[0])), (2, 0))

    def test_stall_discards_evidence_and_fresh_blink_recovers(self):
        half = 15
        pair = _hash_bank(2, seed=405)
        tracker = FlickerTracker(1)
        initial = _runs([pair[0], pair[1], pair[0], pair[1]], [half] * 3 + [half + 80])
        states, _ = self._trace(tracker, initial)
        self.assertEqual(int(states[3 * half, 0]), 4)
        self.assertEqual(int(tracker.scores[0]), 0)
        fresh = _runs(np.tile(pair, 3), [half] * 6)
        states, periods = self._trace(tracker, fresh)
        self.assertEqual((int(states[0, 0]), int(periods[0, 0])), (2, 0))
        claims = np.flatnonzero(states[:, 0] == 4)
        self.assertGreater(len(claims), 0)
        self.assertGreaterEqual(int(claims[0]), 2 * half)
        self.assertLessEqual(int(claims[0]), 4 * half)

    def test_two_toggles_then_static_never_claimed(self):
        a, b = _hash_bank(2, seed=406)
        self._assert_unclaimed(_runs([a, b, a], [30, 30, 240]))

    def test_half_period_boundaries_and_runs_above_max_half(self):
        for half in (1, 64):
            with self.subTest(half=half):
                frames = _blink(half, 4 * half + 1, seed=500 + half)
                tracker = FlickerTracker(1, max_half=half)
                states, periods = self._trace(tracker, frames)
                self.assertEqual((int(states[3 * half, 0]), int(periods[3 * half, 0])), (4, half))
        for bound in (10, 64):
            with self.subTest(max_half=bound):
                frames = _blink(bound + 1, 8 * (bound + 1), seed=600 + bound)
                self._assert_unclaimed(frames, max_half=bound)

    def test_established_64_frame_half_period_tolerates_65_frame_jitter(self):
        pair = _hash_bank(2, seed=665)
        lengths = [64, 64, 64, 64, 65, 64, 65, 64, 64]
        hashes = pair[np.arange(len(lengths)) % 2]
        states, periods = self._trace(FlickerTracker(1, max_half=64), _runs(hashes, lengths))
        for frame in np.cumsum(lengths)[:-1][2:]:
            self.assertEqual((int(states[frame, 0]), int(periods[frame, 0])), (4, 64))

    def test_independent_tiles_preserve_morton_positions(self):
        n_frames = 120
        frames = _hash_bank(n_frames * 16, seed=701).reshape(n_frames, 16)
        fast, slow, static = _morton(3, 0), _morton(0, 3), _morton(1, 1)
        frames[:, fast] = _blink(15, n_frames, seed=702)[:, 0]
        frames[:, slow] = _blink(30, n_frames, seed=703)[:, 0]
        frames[:, static] = frames[0, static]
        states, periods = self._trace(FlickerTracker(16), frames)
        for tile, half in ((fast, 15), (slow, 30)):
            expected = np.arange(3 * half, n_frames, half)
            np.testing.assert_array_equal(np.flatnonzero(states[:, tile] == 4), expected)
            np.testing.assert_array_equal(periods[expected, tile], half)
        other_tiles = [tile for tile in range(16) if tile not in (fast, slow)]
        self.assertFalse((states[:, other_tiles] == 4).any())
        self.assertFalse(states[:, static].any())

    def test_input_storage_is_not_retained_and_read_only_views_work(self):
        a, b, c, d = _hash_bank(4, seed=801)
        hashes = np.array([a, b], dtype=np.uint64)
        tracker = FlickerTracker(2)
        old_state, old_period = tracker.update(hashes)
        hashes[:] = [c, d]
        backing = np.array([a, c, b, d], dtype=np.uint64)
        readonly = backing[::2]
        readonly.setflags(write=False)
        state, period = tracker.update(readonly)
        np.testing.assert_array_equal(state, [0, 0])
        np.testing.assert_array_equal(period, [0, 0])
        tracker.update(np.array([c, d], dtype=np.uint64))
        np.testing.assert_array_equal(old_state, [0, 0])
        np.testing.assert_array_equal(old_period, [0, 0])
        np.testing.assert_array_equal(readonly, [a, b])

    def test_reset_starts_a_fresh_scene(self):
        tracker = FlickerTracker(1)
        frames = _blink(15, 61, seed=802)
        states, _ = self._trace(tracker, frames)
        self.assertTrue((states == 4).any())
        tracker.reset()
        self.assertEqual(int(tracker.scores[0]), 0)
        states, _ = self._trace(tracker, frames)
        np.testing.assert_array_equal(np.flatnonzero(states[:, 0] == 4), [45, 60])

    def test_invalid_constructor_arguments(self):
        for value in (0, -1, True, np.bool_(True), 1.5, "1"):
            with self.subTest(n_tiles=value):
                with self.assertRaises(ValueError):
                    FlickerTracker(value)
        for value in (0, -1, True, np.bool_(True), 1.5, "64"):
            with self.subTest(max_half=value):
                with self.assertRaises(ValueError):
                    FlickerTracker(1, max_half=value)
        tracker = FlickerTracker(np.int64(2), max_half=np.int64(64))
        self.assertEqual((tracker.n_tiles, tracker.max_half), (2, 64))

    def test_invalid_hash_inputs(self):
        invalid = (
            [1],
            [1, 2, 3],
            [1, "invalid"],
            [True, False],
            [1.0, 2.0],
            np.array([[1, 2]], dtype=np.uint64),
            np.array([1, 2], dtype=np.float64),
            np.array([True, False]),
        )
        for hashes in invalid:
            with self.subTest(hashes=repr(hashes)):
                with self.assertRaises(ValueError):
                    FlickerTracker(2).update(hashes)


if __name__ == "__main__":
    unittest.main()
