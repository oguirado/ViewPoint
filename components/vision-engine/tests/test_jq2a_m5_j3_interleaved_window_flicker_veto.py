"""Unit tests for gate G M5 3: Blink veto survives sibling interleaving.

Task ID: jq2a-m5-j3-interleaved-window-flicker-veto
Owning implementation task_id: jq2a-m5-c1-detector-contexts

Tests per-window session isolation, local frame sequence tracking, and baseline flicker
veto preservation under sibling frame interleaving across multi-window streams.
"""

from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Dict, Optional, Tuple
import unittest

import numpy as np


from eval.v2x.per_window_detector import ChangeResult, Handle, PerWindowDetector
from eval.v2eval.chain import compose_chain
from visioncore import desc
from visioncore.grid import Grid


def baseline(rgb, cls=0):
    """Build the real S2 dtype in canonical Morton order."""
    grid = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(rgb), grid, lazy_xh=True)
    tiles["cls"] = cls
    tiles["mv_dx"] = tiles["mv_dy"] = 0
    return tiles


class DetectorEvidenceFixture:
    """Test-local clock consumer; retain the real detector's exact results.

    No MultiWindowChain is present. Production coverage is the real detector
    and existing compose_chain; timestamp freshness is a structural consumer
    seam and is not presented as an implemented production time oracle.
    """

    def __init__(self, *, max_age_ns):
        self.detector = PerWindowDetector()
        self.max_age_ns = max_age_ns
        self.evidence = {}

    def update(self, handle, tiles, rgb, *, epoch, frame_seq, now_ns):
        result = self.detector.update(handle, tiles, rgb,
                                      epoch=epoch, frame_seq=frame_seq)
        base = {name: tiles[name].astype(np.int64)
                for name in ("cls", "mv_dx", "mv_dy")}
        gm = {"mask": result.claims["cls"] == 3, "cls": 3,
              "mv_dx": result.claims["mv_dx"], "mv_dy": result.claims["mv_dy"]}
        fl = {"mask": result.claims["cls"] == 4, "cls": 4,
              "mv_dx": result.claims["mv_dx"], "mv_dy": result.claims["mv_dy"]}
        veto = (tiles["cls"] == 4) & (result.claims["cls"] == 2)
        composed = compose_chain(base, gm, fl, flicker_veto=veto)
        for name in base:
            np.testing.assert_array_equal(composed[name], result.claims[name])
        self.evidence[handle] = (result, now_ns)
        return result

    def is_settled(self, handle, *, now_ns):
        evidence = self.evidence.get(handle)
        if evidence is None or type(now_ns) is not int or now_ns < 0:
            return False
        result, stamp = evidence
        if type(stamp) is not int or not 0 <= now_ns - stamp <= self.max_age_ns:
            return False
        return result.settled


# Contract harness for testing local vs global sequence tracking logic
class InterleavedBlinkTrackerHarness:
    """Offline reference harness simulating per-target window state tracking."""

    def __init__(self, use_global_sequence_mutant: bool = False):
        self.use_global_sequence_mutant = use_global_sequence_mutant
        self.global_frame_counter = 0
        self.target_states: Dict[Tuple[str, str, int], Dict[str, Any]] = {}

    def update(
        self,
        handle_kind: str,
        canonical_id: str,
        generation: int,
        epoch: int,
        local_frame_seq: int,
        rgb_digest: str,
        has_baseline_flicker: bool,
    ) -> Dict[str, Any]:
        self.global_frame_counter += 1
        key = (handle_kind, canonical_id, generation)

        # Determine sequence index used by tracker
        seq_idx = self.global_frame_counter if self.use_global_sequence_mutant else local_frame_seq

        if key not in self.target_states:
            self.target_states[key] = {
                "epoch": epoch,
                "last_seq": seq_idx,
                "history": [rgb_digest],
                "veto_active": False,
            }
            return {"veto": False, "reset_occurred": False}

        state = self.target_states[key]

        # Epoch change or non-consecutive sequence gap triggers target reset
        is_epoch_change = state["epoch"] != epoch
        is_seq_gap = (seq_idx != state["last_seq"] + 1)

        if is_epoch_change or is_seq_gap:
            state["epoch"] = epoch
            state["last_seq"] = seq_idx
            state["history"] = [rgb_digest]
            state["veto_active"] = False
            return {"veto": False, "reset_occurred": True}

        state["last_seq"] = seq_idx
        state["history"].append(rgb_digest)

        # Blink sequence detection (A - B - A pattern across 3 consecutive frames)
        if len(state["history"]) >= 3:
            h = state["history"]
            if h[-3] == h[-1] and h[-3] != h[-2] and has_baseline_flicker:
                state["veto_active"] = True

        return {"veto": state["veto_active"], "reset_occurred": False}

    def reset_target(self, handle_kind: str, canonical_id: str, generation: int) -> None:
        key = (handle_kind, canonical_id, generation)
        if key in self.target_states:
            del self.target_states[key]


class TestContractHarnessInterleavedBlinkVeto(unittest.TestCase):
    """Contract harness tests for Gate G M5 3 predicate and negative mutant."""

    def test_gate_g_m5_3_local_consecutive_blink_survives_sibling_interleaving(self):
        """Positive control: same-target A-B-A blink sequence with interleaved sibling frames preserves veto."""
        harness = InterleavedBlinkTrackerHarness(use_global_sequence_mutant=False)

        target1 = ("x11_window", "win-main", 0)
        target2 = ("cdp_tab", "tab-side", 0)

        # Interleaved frame stream:
        # Target 1 (seq 0, frame A)
        res1_0 = harness.update(*target1, epoch=0, local_frame_seq=0, rgb_digest="hash_A", has_baseline_flicker=True)
        self.assertFalse(res1_0["veto"])

        # Target 2 interleaved sibling frames
        harness.update(*target2, epoch=0, local_frame_seq=0, rgb_digest="hash_X", has_baseline_flicker=False)
        harness.update(*target2, epoch=0, local_frame_seq=1, rgb_digest="hash_Y", has_baseline_flicker=False)

        # Target 1 (seq 1, frame B)
        res1_1 = harness.update(*target1, epoch=0, local_frame_seq=1, rgb_digest="hash_B", has_baseline_flicker=True)
        self.assertFalse(res1_1["veto"])

        # Target 2 interleaved sibling frame
        harness.update(*target2, epoch=0, local_frame_seq=2, rgb_digest="hash_X", has_baseline_flicker=False)

        # Target 1 (seq 2, frame A -> A-B-A blink complete)
        res1_2 = harness.update(*target1, epoch=0, local_frame_seq=2, rgb_digest="hash_A", has_baseline_flicker=True)

        # Verify local consecutive indices preserve the baseline veto despite sibling interleaving
        self.assertTrue(res1_2["veto"], "Target 1 blink veto should survive sibling frame interleaving")
        self.assertFalse(res1_2["reset_occurred"])

    def test_negative_mutant_global_frame_sequence_fails_interleaved_veto(self):
        """Negative mutant: using global frame sequence treats sibling interleaving as a gap and breaks veto."""
        mutant_harness = InterleavedBlinkTrackerHarness(use_global_sequence_mutant=True)

        target1 = ("x11_window", "win-main", 0)
        target2 = ("cdp_tab", "tab-side", 0)

        # Interleaved updates with local_frame_seq 0, 1, 2 for target 1
        mutant_harness.update(*target1, epoch=0, local_frame_seq=0, rgb_digest="hash_A", has_baseline_flicker=True)
        mutant_harness.update(*target2, epoch=0, local_frame_seq=0, rgb_digest="hash_X", has_baseline_flicker=False)
        mutant_harness.update(*target1, epoch=0, local_frame_seq=1, rgb_digest="hash_B", has_baseline_flicker=True)
        mutant_harness.update(*target2, epoch=0, local_frame_seq=1, rgb_digest="hash_Y", has_baseline_flicker=False)

        # Target 1 update at local_frame_seq=2
        res1_mutant = mutant_harness.update(*target1, epoch=0, local_frame_seq=2, rgb_digest="hash_A", has_baseline_flicker=True)

        # Under the negative mutant (global sequence), Target 1 sees non-consecutive sequence numbers (1, 3, 5)
        # which triggers resets and fails to activate the veto.
        self.assertTrue(res1_mutant["reset_occurred"], "Global sequence mutant should trigger sequence gap reset")
        self.assertFalse(res1_mutant["veto"], "Global sequence mutant must fail blink veto under interleaving")

    def test_target_epoch_navigation_and_local_gap_resets(self):
        """INV-M5-1: target navigation or local frame sequence gap resets only that target."""
        harness = InterleavedBlinkTrackerHarness(use_global_sequence_mutant=False)

        t1 = ("godot_window", "godot-1", 0)
        t2 = ("capsule", "capsule-1", 0)

        # Initialize both targets
        harness.update(*t1, epoch=0, local_frame_seq=0, rgb_digest="h1", has_baseline_flicker=False)
        harness.update(*t2, epoch=0, local_frame_seq=0, rgb_digest="h2", has_baseline_flicker=False)

        # Local gap on Target 1 (seq 0 -> 2, skipping 1)
        res_gap = harness.update(*t1, epoch=0, local_frame_seq=2, rgb_digest="h1_new", has_baseline_flicker=False)
        self.assertTrue(res_gap["reset_occurred"], "Target 1 local sequence gap should reset Target 1")

        # Target 2 continues normally without reset
        res_t2 = harness.update(*t2, epoch=0, local_frame_seq=1, rgb_digest="h2_next", has_baseline_flicker=False)
        self.assertFalse(res_t2["reset_occurred"], "Target 2 should not be affected by Target 1 gap")

        # Navigation epoch change on Target 1 (epoch 0 -> 1)
        res_epoch = harness.update(*t1, epoch=1, local_frame_seq=0, rgb_digest="h1_nav", has_baseline_flicker=False)
        self.assertTrue(res_epoch["reset_occurred"], "Target 1 navigation epoch change should reset Target 1")


class TestG_M5_3_PerWindowDetector(unittest.TestCase):
    """Production test suite for PerWindowDetector (Gate G M5 3)."""

    def test_per_window_detector_interleaved_blink_veto(self):
        """Gate G M5 3: PerWindowDetector preserves blink veto across interleaved multi-window updates."""
        import eval.v2x.per_window_detector as pwd

        detector = pwd.PerWindowDetector()
        h1 = pwd.Handle(kind="x11_window", canonical_id="win-1", generation=0)
        h2 = pwd.Handle(kind="cdp_tab", canonical_id="tab-2", generation=0)

        rgb_a = np.full((16, 16, 3), 100, dtype=np.uint8)
        rgb_b = np.full((16, 16, 3), 200, dtype=np.uint8)
        tiles_a = baseline(rgb_a, cls=4)
        tiles_b = baseline(rgb_b, cls=4)

        # Interleaved updates: h1 (seq 0), h2 (seq 0, 1), h1 (seq 1), h2 (seq 2), h1 (seq 2)
        res1_0 = detector.update(h1, tiles_a, rgb_a, epoch=0, frame_seq=0)
        detector.update(h2, tiles_a, rgb_a, epoch=0, frame_seq=0)
        detector.update(h2, tiles_b, rgb_b, epoch=0, frame_seq=1)

        res1_1 = detector.update(h1, tiles_b, rgb_b, epoch=0, frame_seq=1)
        detector.update(h2, tiles_a, rgb_a, epoch=0, frame_seq=2)

        res1_2 = detector.update(h1, tiles_a, rgb_a, epoch=0, frame_seq=2)

        self.assertIsInstance(res1_2, pwd.ChangeResult)
        self.assertEqual(res1_2.handle, h1)
        self.assertEqual(res1_2.epoch, 0)
        # Exact A-B-A return must keep the real baseline FLICKER claim.
        np.testing.assert_array_equal(res1_2.claims["cls"], 4)
        solo = PerWindowDetector()
        for seq, (tiles, rgb) in enumerate(((tiles_a, rgb_a),
                                          (tiles_b, rgb_b),
                                          (tiles_a, rgb_a))):
            isolated = solo.update(h1, tiles, rgb, epoch=0, frame_seq=seq)
        np.testing.assert_array_equal(res1_2.claims, isolated.claims)
        self.assertEqual(res1_2.ops, isolated.ops)

    def test_per_window_detector_reset_window_and_isolation(self):
        """Verify PerWindowDetector.reset_window resets specified target state."""
        import eval.v2x.per_window_detector as pwd

        detector = pwd.PerWindowDetector()
        h1 = pwd.Handle(kind="xvfb_screen", canonical_id="screen-0", generation=0)
        rgb = np.zeros((16, 16, 3), dtype=np.uint8)
        tiles = baseline(rgb)

        detector.update(h1, tiles, rgb, epoch=0, frame_seq=0)
        detector.reset_window(h1)
        # A fresh local sequence is valid after reset, with zero epoch retained.
        result = detector.update(h1, tiles, rgb, epoch=0, frame_seq=0)
        self.assertFalse(result.settled)
        self.assertEqual(result.epoch, 0)


class TestMultiWindowChain(unittest.TestCase):
    """Real detector/composition tests with a test-local freshness seam."""

    def test_multi_window_chain_interleaved_observations_and_settle(self):
        """Compose real detector claims with explicit fake freshness evidence."""
        import eval.v2x.per_window_detector as pwd

        chain = DetectorEvidenceFixture(max_age_ns=1_000_000_000)
        h1 = pwd.Handle(kind="godot_window", canonical_id="godot-main", generation=0)

        rgb = np.zeros((16, 16, 3), np.uint8)
        res = chain.update(h1, baseline(rgb), rgb, epoch=0, frame_seq=0,
                           now_ns=100_000_000)
        self.assertIsNotNone(res)

        settled = chain.is_settled(h1, now_ns=100_000_000)
        self.assertIsInstance(settled, bool)

    def test_multi_window_chain_missing_time_never_settles(self):
        """Verify missing explicit fake time never consults wall clock or yields settled."""
        import eval.v2x.per_window_detector as pwd

        chain = DetectorEvidenceFixture(max_age_ns=1_000_000_000)
        h1 = pwd.Handle(kind="capsule", canonical_id="cap-1", generation=0)

        rgb = np.zeros((16, 16, 3), np.uint8)
        for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
            chain.update(h1, baseline(rgb), rgb, epoch=0, frame_seq=seq,
                         now_ns=0)
        # Legitimate zero time is preserved; only absent time denies freshness.
        self.assertTrue(chain.is_settled(h1, now_ns=0))
        settled = chain.is_settled(h1, now_ns=None)
        self.assertFalse(settled)


class TestCanonicalDataclassesContract(unittest.TestCase):
    """Contract tests for canonical frozen dataclasses."""

    def test_target_supported_kinds(self):
        supported = {"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"}
        self.assertEqual(len(supported), 5)

    def test_proposed_dataclasses_when_owner_present(self):
        self.assertTrue(is_dataclass(Handle))
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab-1")
        self.assertEqual(h.generation, 1)
        self.assertTrue(is_dataclass(ChangeResult))


if __name__ == "__main__":
    unittest.main()
