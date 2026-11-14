# @module {"id":"wa.tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter","wa.perceive.vision_bridge","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""@module wa.tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align - Gate G M3 4 contract and control tests.

Per-window DPR and affordance alignment under multi-target scenarios with unequal positive DPR values,
overlapping AX IDs, and resize/geometry epoch changes.
"""

import hashlib
import unittest

import numpy as np

from wender.perceive.vision_adapter import FakeVisionEngine, Frame, fuse_regions
from wender.perceive import vision_bridge
from wender.state import schema


# Landed canonical APIs are mandatory; missing dependencies fail at import.
from wender.perceive.session_registry import (
    FrameRef, Observation, PerceptionResult, SessionRegistry, WindowDescription, WindowFrames,
)


class FakeWindowRunner:
    """RAM-only collaborator for RealVisionEngine's describe_window protocol."""

    def describe_window(self, item):
        descriptors = tuple(hashlib.sha256(frame.tobytes()).digest() for frame in item.frames)
        return WindowDescription(item.handle, item.epoch, item.frame_seqs, descriptors, (), None)


def rgb_array(frame):
    """Adapt a Frame's immutable RGB bytes to the canonical ndarray contract."""
    return np.frombuffer(frame.data, dtype=np.uint8).reshape(frame.height, frame.width, 3)


def make_test_frame(width, height, fill_color=(10, 20, 30)):
    """Create an in-memory RGB24 Frame."""
    data = bytes(fill_color) * (width * height)
    return Frame(width=width, height=height, data=data)


def make_test_state(node_id, box, dpr_ppm, kind="cdp_tab", title="Window"):
    """Construct a minimal schema-valid state for testing."""
    nodes = [
        {
            "id": node_id,
            "role": "button",
            "name": "TargetButton",
            "depth": 0,
            "ordinal": 0,
            "attrs": (),
        }
    ]
    boxes = {node_id: box}
    return schema.build_state(
        nodes,
        kind=kind if kind in ("cdp", "godot", "atspi") else "cdp",
        url="https://example.test",
        title=title,
        ax_sha256="a" * 64,
        ax_stable_sha256="b" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=1,
        boxes=boxes,
        viewport={"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": dpr_ppm},
        actionable_roles={"button"},
    )


class TestPerWindowDprAndAffordanceAlign(unittest.TestCase):
    """Test suite for per-window DPR alignment, stale epoch refusal, and positive DPR validation."""

    def setUp(self):
        # Two windows with unequal positive DPR values and overlapping AX IDs
        self.registry = SessionRegistry()
        self.handle_a = self.registry.register("cdp_tab", "win_a", object(), owned=False)
        self.handle_b = self.registry.register("x11_window", "win_b", object(), owned=False)
        self.registry.advance_epoch(self.handle_a)
        self.registry.advance_epoch(self.handle_b)

        self.dpr_a = 1000000  # 1.0x DPR
        self.dpr_b = 2000000  # 2.0x DPR

        # Overlapping AX ID in both windows
        self.shared_ax_id = "0000000000000001"

        # Window A box in CSS space at 1.0x DPR: [100, 100, 200, 100]
        self.state_a = make_test_state(self.shared_ax_id, [100, 100, 200, 100], self.dpr_a, title="Window A")

        # Window B box in CSS space at 2.0x DPR: [50, 50, 100, 50]
        # (Same physical region [100, 100, 200, 100] on a 2.0x display maps to CSS [50, 50, 100, 50])
        self.state_b = make_test_state(self.shared_ax_id, [50, 50, 100, 50], self.dpr_b, title="Window B")

        # Physical region detected by vision engine: x=100, y=100, w=200, h=100
        self.physical_region = [{"x": 100, "y": 100, "w": 200, "h": 100, "tiles": 1}]

    # --- Healthy Controls (Passing in current codebase) ---

    def test_healthy_control_per_target_dpr_fusion_coordinate_alignment(self):
        """Positive control: different DPR fixtures align coordinates correctly to their target AX namespace."""
        # Fuse physical region with Window A's target state & DPR (1.0x)
        fused_a = fuse_regions(self.state_a, self.physical_region, dpr_ppm=self.dpr_a)
        self.assertEqual(len(fused_a["hits"]), 1)
        self.assertEqual(fused_a["hits"][0]["node"], self.shared_ax_id)
        # CSS overlap area at 1.0x DPR: 200 * 100 = 20000
        self.assertEqual(fused_a["hits"][0]["overlap"], 20000)
        self.assertEqual(fused_a["unmatched_regions"], [])

        # Fuse physical region with Window B's target state & DPR (2.0x)
        # Physical [100, 100, 200, 100] / 2.0 -> CSS [50, 50, 100, 50]
        fused_b = fuse_regions(self.state_b, self.physical_region, dpr_ppm=self.dpr_b)
        self.assertEqual(len(fused_b["hits"]), 1)
        self.assertEqual(fused_b["hits"][0]["node"], self.shared_ax_id)
        # CSS overlap area at 2.0x DPR: 100 * 50 = 5000
        self.assertEqual(fused_b["hits"][0]["overlap"], 5000)
        self.assertEqual(fused_b["unmatched_regions"], [])

    def test_healthy_control_negative_mutant_global_dpr_misalignment(self):
        """Negative mutant control: using global DPR (e.g. Window A's DPR for Window B) causes misalignment."""
        # Using global DPR (1.0x) on Window B state (whose CSS box is [50, 50, 100, 50])
        fused_mutant = fuse_regions(self.state_b, self.physical_region, dpr_ppm=self.dpr_a)

        # Physical [100, 100, 200, 100] with 1.0x DPR becomes CSS [100, 100, 200, 100],
        # which does NOT overlap Window B's box [50, 50, 100, 50]!
        self.assertEqual(fused_mutant["hits"], [])
        self.assertEqual(fused_mutant["unmatched_regions"], [0])

    def test_healthy_control_positive_dpr_validation(self):
        """Positive-DPR validation: non-positive or non-integer DPR values must be rejected."""
        for invalid_dpr in (0, -1000000, 1.5, False, True, None):
            with self.subTest(dpr=invalid_dpr):
                with self.assertRaises(ValueError):
                    fuse_regions(self.state_a, self.physical_region, dpr_ppm=invalid_dpr)

    def test_healthy_control_resize_and_geometry_epoch_transition(self):
        """Resize between admission and fusion: state updated to new epoch/geometry."""
        # Window A epoch 1 (1000x800 at 1.0x DPR)
        state_epoch_1 = make_test_state(self.shared_ax_id, [100, 100, 200, 100], 1000000)

        # Window A resizes before fusion (epoch 2): viewport scales to 1.5x DPR (1500000) and box becomes [150, 150, 300, 150]
        state_epoch_2 = make_test_state(self.shared_ax_id, [100, 100, 200, 100], 1500000)

        # Fusion using old epoch DPR vs new epoch DPR yields distinct coordinate alignments
        res_ep1 = fuse_regions(state_epoch_1, self.physical_region, dpr_ppm=1000000)
        res_ep2 = fuse_regions(state_epoch_2, self.physical_region, dpr_ppm=1500000)

        self.assertEqual(res_ep1["hits"][0]["overlap"], 20000)
        # At 1.5x DPR: physical 100..300 / 1.5 -> CSS 66..200 (width 134, height 67) -> overlap ~ 8978
        self.assertNotEqual(res_ep1["hits"][0]["overlap"], res_ep2["hits"][0]["overlap"])

    # --- Landed Contract Tests ---

    def test_contract_perceive_many_per_target_dpr_and_affordance_alignment(self):
        """Contract: perceive_many executes multi-window perception using each target's DPR and AX namespace."""
        from wender.perceive.parallel_perceive import perceive_many

        frame_bytes_a = make_test_frame(200, 200, (10, 20, 30))
        frame_bytes_b = make_test_frame(400, 400, (40, 50, 60))

        ref_a = self.registry.admit_frame(self.handle_a, 1, rgb_array(frame_bytes_a), epoch=1)
        ref_b = self.registry.admit_frame(self.handle_b, 1, rgb_array(frame_bytes_b), epoch=1)

        obs_a = self.registry.admit_observation(
            self.handle_a, ref_a, dpr_ppm=self.dpr_a, state=self.state_a,
        )
        obs_b = self.registry.admit_observation(
            self.handle_b, ref_b, dpr_ppm=self.dpr_b, state=self.state_b,
        )

        engine = FakeVisionEngine()
        results = perceive_many([obs_a, obs_b], adapter=engine)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].handle, self.handle_a)
        self.assertEqual(results[1].handle, self.handle_b)
        self.assertIsNone(results[0].error)
        self.assertIsNone(results[1].error)

    def test_contract_vision_bridge_screen_identities_delegation(self):
        """Contract: vision_bridge.screen_identities delegates to perceive_many for multi-window observation sequences."""
        from wender.perceive import vision_bridge

        ref_a = self.registry.admit_frame(
            self.handle_a, 1, rgb_array(make_test_frame(200, 200)), epoch=1,
        )
        obs_a = self.registry.admit_observation(
            self.handle_a, ref_a, dpr_ppm=self.dpr_a, state=self.state_a,
        )

        identities = vision_bridge.screen_identities([obs_a])
        self.assertEqual(len(identities), 1)

    def test_contract_fuse_regions_handle_epoch_and_stale_refusal(self):
        """Contract: fuse_regions accepts handle and epoch keyword args and refuses stale epochs."""
        # Explicit target-bound fusion requires the complete handle/epoch/DPR envelope.
        state = {"state": self.state_a, "handle": self.handle_a,
                 "epoch": 1, "dpr_ppm": self.dpr_a}
        res = fuse_regions(
            state,
            self.physical_region,
            dpr_ppm=self.dpr_a,
            handle=self.handle_a,
            epoch=1,
        )
        self.assertEqual(len(res["hits"]), 1)

        # Stale epoch refusal check (epoch 0 vs state epoch 1)
        with self.assertRaises(ValueError):
            fuse_regions(
                state,
                self.physical_region,
                dpr_ppm=self.dpr_a,
                handle=self.handle_a,
                epoch=0,
            )

        # Target handle mismatch check
        with self.assertRaises(ValueError):
            fuse_regions(
                state,
                self.physical_region,
                dpr_ppm=self.dpr_a,
                handle=self.handle_b,
                epoch=1,
            )

    def test_contract_real_vision_engine_describe_many_invariants(self):
        """Contract: RealVisionEngine.describe_many enforces per-target DPR and window frames invariants."""
        from wender.perceive.vision_engine_real import RealVisionEngine

        frame_a = make_test_frame(100, 100)
        frame_b = make_test_frame(200, 200)

        wf_a = WindowFrames(
            handle=self.handle_a,
            epoch=1,
            frames=(rgb_array(frame_a),),
            frame_seqs=(1,),
            dpr_ppm=self.dpr_a,
        )
        wf_b = WindowFrames(
            handle=self.handle_b,
            epoch=1,
            frames=(rgb_array(frame_b),),
            frame_seqs=(1,),
            dpr_ppm=self.dpr_b,
        )

        engine = RealVisionEngine(runner=FakeWindowRunner())
        descriptions = engine.describe_many([wf_a, wf_b])

        self.assertEqual(len(descriptions), 2)
        self.assertEqual(descriptions[0].handle, self.handle_a)
        self.assertEqual(descriptions[1].handle, self.handle_b)
        self.assertTrue(all(result.error is None for result in descriptions))
        self.assertTrue(all(isinstance(result, PerceptionResult) for result in descriptions))
        self.assertEqual([result.observation.dpr_ppm for result in descriptions], [self.dpr_a, self.dpr_b])


if __name__ == "__main__":
    unittest.main()
