"""RAM-only regression tests for the asymmetric-duty-cycle flicker variant.

Hash fixtures are generated deterministically from fixed seeds; no image, luma,
pixel, or binary fixture files are read or written. Latency means an observed
frame/toggle index, not a wall-clock performance measurement.
"""
import unittest

import numpy as np

from eval.v2x import fl_dutycycle, fl_early3, fl_majority


def _hash_values(count=2, seed=0xD077C1E):
    """Return reproducible full-width S2-style uint64 hash tokens in RAM."""
    return np.random.default_rng(seed).integers(
        1, np.iinfo(np.uint64).max, size=count, dtype=np.uint64
    )


def _duty_frames(on, off, n_frames, values=None):
    """Produce one Morton-position tile, starting with a full ON run."""
    if values is None:
        values = _hash_values()
    frame = np.arange(n_frames, dtype=np.int64)
    return np.where(frame % (on + off) < on, values[0], values[1]).reshape(-1, 1)


def _run_frames(lengths, values=None):
    """Alternate the supplied hash values for the specified whole-run lengths."""
    if values is None:
        values = _hash_values()
    phases = np.resize(np.asarray(values, dtype=np.uint64), len(lengths))
    return np.repeat(phases, lengths).reshape(-1, 1)


def _feed_sequence(tracker, frames):
    """Snapshot each result so implementations may reuse their output arrays."""
    states, periods = [], []
    for hashes in frames:
        state, period = tracker.update(hashes)
        states.append(state.copy())
        periods.append(period.copy())
    return np.stack(states), np.stack(periods)


def _observe(module, frames, max_half=64):
    return _feed_sequence(module.FlickerTracker(frames.shape[1], max_half), frames)


def _claim_frames(states, tile=0):
    return np.flatnonzero(states[:, tile] == fl_dutycycle.C_FLICKER).tolist()


class TestFlickerTrackerDutycycle(unittest.TestCase):
    def assert_toggle_contract(self, frames, states, periods, max_half=64):
        """Held frames and unclaimed tiles carry no flicker half-period."""
        self.assertEqual(states.shape, frames.shape)
        self.assertEqual(periods.shape, frames.shape)
        self.assertIn(states.dtype.kind, ("i", "u"))
        self.assertIn(periods.dtype.kind, ("i", "u"))
        self.assertTrue(np.all((states >= 0) & (states <= 6)))
        self.assertTrue(np.all((periods >= 0) & (periods <= max_half)))
        held = np.ones(frames.shape, dtype=bool)
        held[1:] = frames[1:] == frames[:-1]
        np.testing.assert_array_equal(states[held], fl_dutycycle.C_UNCHANGED)
        np.testing.assert_array_equal(periods[held], 0)
        np.testing.assert_array_equal(periods[states != fl_dutycycle.C_FLICKER], 0)
        self.assertTrue(np.all(periods[states == fl_dutycycle.C_FLICKER] > 0))

    def test_500_ms_caret_within_120_frames_and_latency_references(self):
        """Thirty-frame runs give a claim on toggle 3, where majority misses."""
        frames = _duty_frames(30, 30, 120)
        frames.setflags(write=False)
        observed = {module.__name__: _observe(module, frames) for module in (
            fl_dutycycle, fl_early3, fl_majority
        )}
        states, periods = observed[fl_dutycycle.__name__]
        self.assertEqual(_claim_frames(states), [90])
        self.assertEqual(int(periods[90, 0]), 30)
        toggles = np.flatnonzero(frames[1:, 0] != frames[:-1, 0]) + 1
        self.assertEqual(toggles.tolist(), [30, 60, 90])
        self.assertLessEqual(int(np.searchsorted(toggles, 90)) + 1, 4)
        self.assertEqual(_claim_frames(observed[fl_early3.__name__][0]), [90])
        self.assertEqual(_claim_frames(observed[fl_majority.__name__][0]), [])
        self.assert_toggle_contract(frames, states, periods)

        # The same longer sequence exposes the reference's actual frame latency.
        longer = _duty_frames(30, 30, 181)
        first_claims = {
            module.__name__: _claim_frames(_observe(module, longer)[0])[0]
            for module in (fl_dutycycle, fl_early3, fl_majority)
        }
        self.assertEqual(first_claims, {
            fl_dutycycle.__name__: 90,
            fl_early3.__name__: 90,
            fl_majority.__name__: 150,
        })

    def test_both_asymmetric_duty_cycles_where_references_miss(self):
        """Both 18/42 and 42/18 are detected; neither reference claims them."""
        for on, off, first_claim in ((18, 42, 78), (42, 18, 102)):
            for n_frames in (120, 360):
                with self.subTest(on=on, off=off, n_frames=n_frames):
                    frames = _duty_frames(on, off, n_frames)
                    frames.setflags(write=False)
                    states, periods = _observe(fl_dutycycle, frames)
                    toggles = np.flatnonzero(frames[1:, 0] != frames[:-1, 0]) + 1
                    self.assertEqual(_claim_frames(states), toggles[2:].tolist())
                    self.assertEqual(_claim_frames(states)[0], first_claim)
                    np.testing.assert_array_equal(periods[states == 4], 30)
                    for reference in (fl_majority, fl_early3):
                        self.assertEqual(
                            _claim_frames(_observe(reference, frames)[0]), [],
                            reference.__name__,
                        )
                    self.assert_toggle_contract(frames, states, periods)

    def test_jitter_plus_minus_one_compares_the_same_phase(self):
        """Each A or B run may differ by +/-1 from its own previous run."""
        lengths = [18, 42, 19, 41, 18, 42, 17, 43, 18, 42]
        frames = _run_frames(lengths)
        states, periods = _observe(fl_dutycycle, frames)
        toggles = np.cumsum(lengths)[:-1]
        self.assertEqual(_claim_frames(states), toggles[2:].tolist())
        for ended_run in range(2, len(lengths) - 1):
            frame = int(toggles[ended_run])
            expected = (lengths[ended_run] + lengths[ended_run - 1] + 1) // 2
            self.assertEqual(int(periods[frame, 0]), expected, f"frame {frame}")
        self.assert_toggle_contract(frames, states, periods)

    def test_mismatched_same_phase_runs_cannot_establish_a_claim(self):
        """Alternating hashes alone do not establish periodic phase lengths."""
        frames = _run_frames([18, 42, 22, 46, 26, 50, 30, 54, 34, 58])
        states, periods = _observe(fl_dutycycle, frames)
        self.assertEqual(_claim_frames(states), [])
        self.assert_toggle_contract(frames, states, periods)

    def test_partial_first_phase_recovers_on_the_fourth_toggle(self):
        """A short initial observation cannot match A, but repeated B can."""
        lengths = [5, 42, 18, 42, 18, 42]
        frames = _run_frames(lengths)
        states, periods = _observe(fl_dutycycle, frames)
        toggles = np.cumsum(lengths)[:-1]
        third_toggle = int(toggles[2])
        self.assertEqual(int(states[third_toggle, 0]), fl_dutycycle.C_CHANGED)
        self.assertEqual(int(periods[third_toggle, 0]), 0)
        self.assertEqual(_claim_frames(states), toggles[3:].tolist())
        np.testing.assert_array_equal(periods[states == 4], 30)
        self.assert_toggle_contract(frames, states, periods)

    def test_static_one_off_and_two_changes_never_declare(self):
        for lengths in ([240], [18, 240], [18, 42, 240]):
            with self.subTest(lengths=lengths):
                frames = _run_frames(lengths)
                states, periods = _observe(fl_dutycycle, frames)
                self.assertEqual(_claim_frames(states), [])
                self.assert_toggle_contract(frames, states, periods)

    def test_repeated_three_hash_cycle_never_declares(self):
        for lengths in ([1] * 120, [18, 42, 7] * 6):
            with self.subTest(lengths=lengths[:3]):
                frames = _run_frames(lengths, _hash_values(3))
                states, periods = _observe(fl_dutycycle, frames)
                self.assertEqual(_claim_frames(states), [])
                self.assert_toggle_contract(frames, states, periods)

    def test_third_hash_resets_a_declared_tile_immediately(self):
        """The third hash is a hard reset, not the first Schmitt mismatch."""
        a, b, c = _hash_values(3)
        lengths = [6, 10, 6, 10, 6, 10, 6, 10]
        values = np.array([a, b, a, b, c, a, c, a], dtype=np.uint64)
        frames = _run_frames(lengths, values)
        states, periods = _observe(fl_dutycycle, frames)
        self.assertEqual(int(states[22, 0]), fl_dutycycle.C_FLICKER)
        third_hash_frame = sum(lengths[:4])
        for frame in (third_hash_frame, third_hash_frame + 6, third_hash_frame + 16):
            self.assertEqual(int(states[frame, 0]), fl_dutycycle.C_CHANGED)
            self.assertEqual(int(periods[frame, 0]), 0)
        self.assertEqual(int(states[third_hash_frame + 22, 0]), fl_dutycycle.C_FLICKER)
        self.assert_toggle_contract(frames, states, periods)

    def test_stall_longer_than_twice_max_half_resets(self):
        max_half = 16
        lengths = [6, 10, 6, 2 * max_half + 1, 6, 10, 6, 10]
        frames = _run_frames(lengths)
        states, periods = _observe(fl_dutycycle, frames, max_half)
        self.assertEqual(int(states[22, 0]), fl_dutycycle.C_FLICKER)
        after_stall = sum(lengths[:4])
        self.assertEqual(int(states[after_stall, 0]), fl_dutycycle.C_CHANGED)
        self.assertEqual(int(periods[after_stall, 0]), 0)
        fresh_third_toggle = after_stall + sum(lengths[4:7])
        self.assertEqual(_claim_frames(states), [22, fresh_third_toggle])
        self.assert_toggle_contract(frames, states, periods, max_half)

    def test_two_consecutive_mismatched_runs_release_the_claim(self):
        """A mismatch of A followed by a mismatch of B releases at the latter."""
        lengths = [18, 42, 18, 42, 22, 46, 22, 46]
        frames = _run_frames(lengths)
        states, periods = _observe(fl_dutycycle, frames)
        toggles = np.cumsum(lengths)[:-1]
        for toggle in toggles[2:5]:
            self.assertEqual(int(states[toggle, 0]), fl_dutycycle.C_FLICKER)
        second_mismatch = int(toggles[5])
        self.assertEqual(int(states[second_mismatch, 0]), fl_dutycycle.C_CHANGED)
        self.assertEqual(int(periods[second_mismatch, 0]), 0)
        self.assert_toggle_contract(frames, states, periods)

    def test_a_matching_run_clears_the_mismatch_streak(self):
        """Separated mismatches must not accumulate into a release."""
        lengths = [18, 42, 18, 42, 22, 42, 22, 46, 22, 46, 22]
        frames = _run_frames(lengths)
        states, periods = _observe(fl_dutycycle, frames)
        self.assertEqual(_claim_frames(states), np.cumsum(lengths)[:-1][2:].tolist())
        self.assert_toggle_contract(frames, states, periods)

    def test_rounded_mean_and_clipping_include_run_limit(self):
        """Round half up; individual phases can exceed the output max_half."""
        cases = (
            (1, 1, 64, 1),
            (2, 3, 64, 3),
            (3, 2, 64, 3),
            (18, 42, 32, 30),
            (16, 1, 8, 8),
            (20, 24, 16, 16),
            (32, 32, 16, 16),
            (128, 128, 64, 64),
        )
        for on, off, max_half, expected in cases:
            with self.subTest(on=on, off=off, max_half=max_half):
                frames = _run_frames([on, off] * 5)
                states, periods = _observe(fl_dutycycle, frames, max_half)
                toggles = np.cumsum([on, off] * 5)[:-1]
                self.assertEqual(_claim_frames(states), toggles[2:].tolist())
                np.testing.assert_array_equal(periods[states == 4], expected)
                self.assert_toggle_contract(frames, states, periods, max_half)

    def test_runs_above_twice_max_half_never_establish(self):
        for on, off, max_half in ((33, 10, 16), (10, 33, 16), (33, 33, 16), (17, 1, 8)):
            with self.subTest(on=on, off=off, max_half=max_half):
                frames = _run_frames([on, off] * 6)
                states, periods = _observe(fl_dutycycle, frames, max_half=max_half)
                self.assertEqual(_claim_frames(states), [])
                self.assert_toggle_contract(frames, states, periods, max_half=max_half)

    def test_many_toggles_and_frame_slot_collisions_keep_the_cycle(self):
        """More than eight toggles work even when every toggle is at f % 8 == 0."""
        for on, off, repeats in ((1, 1, 160), (8, 8, 40), (8, 16, 40)):
            with self.subTest(on=on, off=off):
                lengths = [on, off] * repeats
                frames = _run_frames(lengths)
                states, periods = _observe(fl_dutycycle, frames, max_half=16)
                self.assertEqual(_claim_frames(states), np.cumsum(lengths)[:-1][2:].tolist())
                np.testing.assert_array_equal(periods[states == 4], (on + off + 1) // 2)
                self.assert_toggle_contract(frames, states, periods, max_half=16)

    def test_full_frame_counter_wrap_preserves_outputs(self):
        """History ages remain valid when the modular frame counter wraps."""
        frames = _duty_frames(18, 42, 240)
        expected_states, expected_periods = _observe(fl_dutycycle, frames)
        tracker = fl_dutycycle.FlickerTracker(1)
        initial_state, initial_period = tracker.update(frames[0])
        # No toggle evidence exists yet. Shift only the clock, so the first
        # toggle precedes the wrap and the second/third toggles follow it.
        tracker.frame_idx = tracker._frame_mask - 50
        remaining_states, remaining_periods = _feed_sequence(tracker, frames[1:])
        states = np.concatenate((initial_state[None, :], remaining_states), axis=0)
        periods = np.concatenate((initial_period[None, :], remaining_periods), axis=0)
        np.testing.assert_array_equal(states, expected_states)
        np.testing.assert_array_equal(periods, expected_periods)
        self.assertEqual(_claim_frames(states)[0], 78)
        self.assert_toggle_contract(frames, states, periods)

    def test_tiles_are_independent_and_preserve_morton_input_positions(self):
        """Distinct behaviors stay at their original supplied tile indices."""
        values = _hash_values(16)
        n_frames = 240
        frames = np.column_stack((
            np.full(n_frames, values[0], dtype=np.uint64),
            _duty_frames(18, 42, n_frames, values[1:3])[:, 0],
            _duty_frames(42, 18, n_frames, values[3:5])[:, 0],
            _duty_frames(30, 30, n_frames, values[5:7])[:, 0],
            values[7:10][(np.arange(n_frames) // 3) % 3],
            np.arange(n_frames, dtype=np.uint64) + np.uint64(123456),
            _run_frames([18, n_frames - 18], values[10:12])[:, 0],
        ))
        snapshot = frames.copy()
        frames.setflags(write=False)
        states, periods = _observe(fl_dutycycle, frames)
        for tile in range(frames.shape[1]):
            single_states, single_periods = _observe(fl_dutycycle, frames[:, tile:tile + 1])
            np.testing.assert_array_equal(states[:, tile], single_states[:, 0])
            np.testing.assert_array_equal(periods[:, tile], single_periods[:, 0])
        for tile, first in ((1, 78), (2, 102), (3, 90)):
            self.assertEqual(_claim_frames(states, tile)[0], first)
        for tile in (0, 4, 5, 6):
            self.assertEqual(_claim_frames(states, tile), [])
        np.testing.assert_array_equal(frames, snapshot)
        self.assert_toggle_contract(frames, states, periods)

    def test_full_width_hashes_remain_distinct(self):
        """Adjacent high-bit hashes must not collapse through float conversion."""
        a = (int(_hash_values(1)[0]) | (1 << 63)) & ~1
        for values in ((a, a + 1), (0, (1 << 64) - 1)):
            with self.subTest(values=values):
                frames = _run_frames([2, 3] * 6, np.array(values, dtype=np.uint64))
                states, periods = _observe(fl_dutycycle, frames)
                self.assertEqual(_claim_frames(states)[0], 7)
                self.assert_toggle_contract(frames, states, periods)

    def test_update_does_not_mutate_or_retain_the_callers_hash_buffer(self):
        frames = _duty_frames(18, 42, 180)
        tracker = fl_dutycycle.FlickerTracker(1)
        buffer = np.zeros(1, dtype=np.uint64)
        states, periods = [], []
        for row in frames:
            buffer[:] = row
            state, period = tracker.update(buffer)
            np.testing.assert_array_equal(buffer, row)
            states.append(state.copy())
            periods.append(period.copy())
        expected_states, expected_periods = _observe(fl_dutycycle, frames)
        np.testing.assert_array_equal(np.stack(states), expected_states)
        np.testing.assert_array_equal(np.stack(periods), expected_periods)
        self.assertEqual(_claim_frames(np.stack(states))[0], 78)

    def test_reset_restarts_scene_evidence(self):
        frames = _duty_frames(18, 42, 180)
        tracker = fl_dutycycle.FlickerTracker(1)
        first = _feed_sequence(tracker, frames)
        tracker.reset()
        second = _feed_sequence(tracker, frames)
        np.testing.assert_array_equal(first[0], second[0])
        np.testing.assert_array_equal(first[1], second[1])
        self.assertEqual(_claim_frames(second[0])[0], 78)

    def test_ops_is_an_integer_increases_and_resets(self):
        fl_dutycycle.reset_ops()
        self.assertIs(type(fl_dutycycle.OPS), int)
        self.assertEqual(fl_dutycycle.OPS, 0)
        tracker = fl_dutycycle.FlickerTracker(4)
        values = _hash_values(4)
        previous = fl_dutycycle.OPS
        for row in (values, values, values ^ np.uint64(1)):
            tracker.update(row)
            self.assertIs(type(fl_dutycycle.OPS), int)
            self.assertGreater(fl_dutycycle.OPS, previous)
            previous = fl_dutycycle.OPS
        fl_dutycycle.reset_ops()
        self.assertIs(type(fl_dutycycle.OPS), int)
        self.assertEqual(fl_dutycycle.OPS, 0)

    def test_invalid_constructor_and_input_shapes_are_rejected(self):
        for n_tiles, max_half in ((0, 64), (-1, 64), (True, 64), (1, 0), (1, True)):
            with self.subTest(n_tiles=n_tiles, max_half=max_half):
                with self.assertRaises(ValueError):
                    fl_dutycycle.FlickerTracker(n_tiles, max_half)
        tracker = fl_dutycycle.FlickerTracker(2)
        for hashes in (
            np.ones(1, dtype=np.uint64),
            np.ones((1, 2), dtype=np.uint64),
            np.ones(2, dtype=np.float64),
            np.ones(2, dtype=bool),
        ):
            with self.subTest(shape=hashes.shape, dtype=hashes.dtype):
                with self.assertRaises(ValueError):
                    tracker.update(hashes)


if __name__ == "__main__":
    unittest.main()
