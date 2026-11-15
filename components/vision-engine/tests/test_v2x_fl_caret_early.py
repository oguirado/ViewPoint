"""RAM-only hash-sequence tests for the early caret flicker variant."""

import unittest

import numpy as np

from eval.v2x.fl_caret_early import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FlickerTracker,
)


def seeded_hashes(count, seed=20261018):
    """Generate distinct S2-shaped uint64 values without any fixture files."""
    values = np.random.default_rng(seed).integers(
        0, np.iinfo(np.uint64).max, size=count, dtype=np.uint64
    )
    if np.unique(values).size != count:
        raise AssertionError("The fixed fixture seed produced duplicate hashes")
    return values


def runs(values, lengths):
    """Build alternating or multistate runs entirely in RAM."""
    return np.concatenate(
        [
            np.full(length, values[index % len(values)], dtype=np.uint64)
            for index, length in enumerate(lengths)
        ]
    )


class TestFLCaretEarly(unittest.TestCase):
    def observe(self, sequence, tracker=None, max_half=64):
        frames = np.asarray(sequence, dtype=np.uint64)
        if frames.ndim == 1:
            frames = frames[:, None]
        frames.setflags(write=False)
        if tracker is None:
            tracker = FlickerTracker(frames.shape[1], max_half=max_half)

        state_frames, half_frames = [], []
        for frame in frames:
            states, halves = tracker.update(frame)
            for output in (states, halves):
                self.assertIsInstance(output, np.ndarray)
                self.assertEqual(output.shape, (frames.shape[1],))
                self.assertTrue(np.issubdtype(output.dtype, np.integer))
            claims = states == C_FLICKER
            self.assertTrue(np.all(halves[~claims] == 0))
            self.assertTrue(np.all(halves[claims] > 0))
            self.assertTrue(np.all(halves <= max_half))
            state_frames.append(states.copy())
            half_frames.append(halves.copy())
        return np.stack(state_frames), np.stack(half_frames)

    def assert_claims(self, states, halves, expected, half, tile=0):
        actual = np.flatnonzero(states[:, tile] == C_FLICKER).tolist()
        self.assertEqual(actual, expected)
        np.testing.assert_array_equal(halves[expected, tile], half)

    def test_500ms_caret_declares_within_90_frames(self):
        """At 60 fps, A30/B30/A returns after only 61 updates."""
        sequence = runs(seeded_hashes(2), [30, 30, 30])
        states, halves = self.observe(sequence)
        self.assert_claims(states, halves, [60], 30)
        self.assertLessEqual(60 + 1, 90)
        self.assertEqual(int(states[30, 0]), C_CHANGED)
        held = np.flatnonzero(sequence[1:] == sequence[:-1]) + 1
        np.testing.assert_array_equal(states[held, 0], C_UNCHANGED)

    def test_15_frame_blink_has_no_cohort_delay(self):
        """A single tile and nine simultaneous tiles use the same early rule."""
        sequence = runs(seeded_hashes(2, 1515), [15, 15, 15, 15, 1])
        for n_tiles in (1, 9):
            with self.subTest(n_tiles=n_tiles):
                frames = np.repeat(sequence[:, None], n_tiles, axis=1)
                states, halves = self.observe(frames)
                for tile in range(n_tiles):
                    self.assert_claims(states, halves, [30, 45, 60], 15, tile)

    def test_static_tiles_never_claim(self):
        frames = np.tile(seeded_hashes(5, 100), (180, 1))
        states, halves = self.observe(frames)
        self.assertFalse(np.any(states == C_FLICKER))
        np.testing.assert_array_equal(states[1:], C_UNCHANGED)
        np.testing.assert_array_equal(halves, 0)

    def test_typing_with_nonrepeating_hashes_never_claims(self):
        lengths = [8, 15, 30, 9, 16, 12, 25, 15, 8, 30]
        sequence = runs(seeded_hashes(len(lengths), 200), lengths)
        states, _ = self.observe(sequence)
        self.assertFalse(np.any(states == C_FLICKER))
        starts = np.cumsum(lengths)[:-1]
        np.testing.assert_array_equal(states[starts, 0], C_CHANGED)

    def test_scrolling_cyclic_hash_strip_never_claims(self):
        """Each Morton slot visits twelve hashes as a strip shifts past it."""
        strip = seeded_hashes(12, 300)
        frames = np.stack([np.roll(strip, frame // 10) for frame in range(240)])
        states, _ = self.observe(frames)
        self.assertFalse(np.any(states == C_FLICKER))

    def test_three_state_cycle_never_claims(self):
        sequence = runs(seeded_hashes(3, 400), [15] * 10)
        states, _ = self.observe(sequence)
        self.assertFalse(np.any(states == C_FLICKER))

    def test_one_frame_jitter_keeps_established_half_period(self):
        lengths = [30, 31, 29, 31, 30, 1]
        states, halves = self.observe(runs(seeded_hashes(2, 500), lengths))
        self.assert_claims(states, halves, [61, 90, 121, 151], 30)

    def test_fresh_tracker_and_reset_start_a_new_scene(self):
        values = seeded_hashes(2, 600)
        previous = FlickerTracker(n_tiles=1)
        self.observe(runs(values, [15, 15, 15]), tracker=previous)

        next_scene = runs(values[::-1], [15, 15, 1])
        fresh = FlickerTracker(n_tiles=1)
        fresh_states, fresh_halves = self.observe(next_scene, tracker=fresh)
        self.assert_claims(fresh_states, fresh_halves, [30], 15)

        previous.reset()
        reset_states, reset_halves = self.observe(next_scene, tracker=previous)
        np.testing.assert_array_equal(reset_states, fresh_states)
        np.testing.assert_array_equal(reset_halves, fresh_halves)

    def test_initial_run_bounds_and_max_half(self):
        """Initial runs satisfy 8..40; max_half bounds their inferred period."""
        cases = [
            (8, 8, 64, 8),
            (40, 40, 64, 40),
            (15, 16, 64, 15),
            (15, 15, 15, 15),
            (8, 9, 8, 8),
            (30, 31, 30, 30),
            (7, 8, 64, None),
            (8, 7, 64, None),
            (40, 41, 64, None),
            (41, 40, 64, None),
            (15, 17, 64, None),
            (30, 8, 64, None),
            (16, 16, 15, None),
            (8, 8, 7, None),
        ]
        values = seeded_hashes(2, 700)
        for first, second, max_half, expected_half in cases:
            with self.subTest(first=first, second=second, max_half=max_half):
                states, halves = self.observe(
                    runs(values, [first, second, 1]), max_half=max_half
                )
                if expected_half is None:
                    self.assertFalse(np.any(states == C_FLICKER))
                else:
                    self.assert_claims(
                        states, halves, [first + second], expected_half
                    )

    def test_continuation_jitter_at_initial_range_edges(self):
        """Established H accepts H +/- 1 even across the initial-run bounds."""
        values = seeded_hashes(2, 750)
        for half, next_run in ((8, 7), (8, 9), (40, 41)):
            with self.subTest(half=half, next_run=next_run):
                states, halves = self.observe(
                    runs(values, [half, half, next_run, 1]), max_half=half
                )
                self.assert_claims(
                    states, halves, [2 * half, 2 * half + next_run], half
                )

    def test_stalled_run_requires_two_fresh_complete_runs(self):
        """The H+2 stalled run cannot seed an immediate reclaim."""
        sequence = runs(seeded_hashes(2, 800), [15, 15, 17, 15, 15, 1])
        states, halves = self.observe(sequence)
        self.assert_claims(states, halves, [30, 77], 15)
        self.assertEqual(int(states[47, 0]), C_CHANGED)
        self.assertEqual(int(states[62, 0]), C_CHANGED)

    def test_established_period_cannot_drift_through_jitter(self):
        """An accepted H+1 run does not make an H+2 run acceptable."""
        sequence = runs(seeded_hashes(2, 850), [15, 15, 16, 17, 1])
        states, halves = self.observe(sequence)
        self.assert_claims(states, halves, [30, 46], 15)
        self.assertEqual(int(states[63, 0]), C_CHANGED)

    def test_third_hash_resets_history_then_allows_a_new_pair(self):
        a, b, c = seeded_hashes(3, 900)
        sequence = runs([a, b, a, c, b, c], [15, 15, 15, 15, 15, 1])
        states, halves = self.observe(sequence)
        self.assert_claims(states, halves, [30, 75], 15)
        self.assertEqual(int(states[45, 0]), C_CHANGED)
        self.assertEqual(int(states[60, 0]), C_CHANGED)

    def test_uint64_extremes_and_morton_slot_independence(self):
        """Preserve input slot order across independent mixed tile histories."""
        frames = np.empty((65, 5), dtype=np.uint64)
        frame_index = np.arange(65)
        endpoints = np.array([0, np.iinfo(np.uint64).max], dtype=np.uint64)
        frames[:, 0] = endpoints[(frame_index // 15) % 2]
        frames[:, 1] = seeded_hashes(1, 1000)[0]
        frames[:, 2] = seeded_hashes(65, 1001)
        frames[:, 3] = endpoints[1 - (frame_index // 8) % 2]
        frames[:, 4] = seeded_hashes(1, 1002)[0]
        states, halves = self.observe(frames)
        self.assert_claims(states, halves, [30, 45, 60], 15, tile=0)
        self.assert_claims(states, halves, [16, 24, 32, 40, 48, 56, 64], 8, tile=3)
        self.assertFalse(np.any(states[:, [1, 2, 4]] == C_FLICKER))

    def test_reusable_input_buffer_and_returned_snapshots(self):
        """The tracker owns its history and leaves caller input unchanged."""
        values = seeded_hashes(3, 1100)
        sequence = runs(values[:2], [15, 15, 2])
        tracker = FlickerTracker(n_tiles=1)
        buffer = np.empty(1, dtype=np.uint64)
        outputs = []
        for value in sequence:
            buffer[0] = value
            outputs.append(tracker.update(buffer))
            self.assertEqual(int(buffer[0]), int(value))

        # Mutating the caller's reusable buffer must not rewrite tracker history.
        buffer[0] = values[2]
        states, halves = tracker.update(buffer)
        self.assertEqual(int(states[0]), C_CHANGED)
        self.assertEqual(int(halves[0]), 0)

        # Retaining an output must preserve that frame after later update calls.
        old_states = np.stack([state for state, _ in outputs])
        old_halves = np.stack([half for _, half in outputs])
        self.assert_claims(old_states, old_halves, [30], 15)
        self.assertEqual(int(old_states[31, 0]), C_UNCHANGED)


if __name__ == "__main__":
    unittest.main()
