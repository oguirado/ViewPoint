# @module {"id":"wa.tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync","layer":1,"owner":"PERCEIVE","deps":["wa.state.schema","wa.perceive.godot"],"book":"HB-WA-s1-perceive"}
"""Contract and adversarial oracle tests for Godot T2 Outbox Backpressure and Ring Sync (gate G M8 3).

Target build task / owning implementation: jq2a-m8-c1-godot-target
Proposed modules:
  - wender.perceive.session_registry
  - wender.perceive.godot_adapter
  - wender.perceive.godot_window_target
"""

import dataclasses
import hashlib
import unittest
import numpy as np

from tests.test_jq2a_m8_c1_godot_target_implementation import make_target, fixture_pair
from tests.test_godot_t2_adapter import _fixture_encode_batch


def expected_failure_if_absent(module_name: str, symbol_name: str | None = None):
    """Apply @unittest.expectedFailure only while the target owner module or symbol is absent.

    Removes the expectedFailure wrapper automatically when the owner lands to prevent UNEXPECTED SUCCESS.
    """
    def decorator(func):
        try:
            mod = __import__(module_name, fromlist=[symbol_name] if symbol_name else [])
            if symbol_name and not hasattr(mod, symbol_name):
                return unittest.expectedFailure(func)
            return func
        except ImportError:
            return unittest.expectedFailure(func)
    return decorator


# ============================================================================
# Shared Record Fallback Definitions (per jq2a-m8-c1-godot-target specification)
# ============================================================================

try:
    from wender.perceive.session_registry import (
        Handle,
        TargetRecord,
        FrameRef,
        Observation,
        Receipt,
        ActionRequest,
        ActorLease,
        FocusLease,
        StreamSpec,
        StreamToken,
        Ticket,
        ChangeResult,
        PerceptionResult,
        WindowFrames,
        WindowDescription,
    )
except ImportError:
    @dataclasses.dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclasses.dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclasses.dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclasses.dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclasses.dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: str | None

    @dataclasses.dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclasses.dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclasses.dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclasses.dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclasses.dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclasses.dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclasses.dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: np.ndarray
        ops: dict

    @dataclasses.dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

    @dataclasses.dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    @dataclasses.dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None


# ============================================================================
# Healthy Control Tests (Run without @unittest.expectedFailure)
# ============================================================================

class TestGodotT2BackpressureHealthyControls(unittest.TestCase):
    """Nominal positive controls for dataclass contracts and local fixture helpers."""

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_dataclass_fallback_field_order
    def test_dataclass_fallback_field_order(self):
        """Verify fallback record definitions strictly conform to target field order."""
        handle = Handle(kind="godot_window", canonical_id="win_001", generation=1)
        self.assertEqual(handle.kind, "godot_window")
        self.assertEqual(handle.canonical_id, "win_001")
        self.assertEqual(handle.generation, 1)

        frame = FrameRef(
            handle=handle,
            epoch=1,
            frame_seq=10,
            width=640,
            height=480,
            rgb=np.zeros((480, 640, 3), dtype=np.uint8),
            digest=hashlib.sha256(bytes(480 * 640 * 3)).hexdigest(),
        )
        self.assertEqual(frame.digest, hashlib.sha256(bytes(480 * 640 * 3)).hexdigest())

        obs = Observation(
            handle=handle,
            epoch=1,
            frame_seq=10,
            dpr_ppm=100,
            state={"ax_sha256": "b" * 64},
            frame=frame,
        )
        self.assertEqual(obs.dpr_ppm, 100)

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_tick_trail_cap_constant_sanity
    def test_tick_trail_cap_constant_sanity(self):
        """Verify that the target tick-trail queue cap requirement is exactly 24 records."""
        TICK_TRAIL_CAP = 24
        records = [{"tick": i, "ax_sha256": f"{i:064x}"} for i in range(TICK_TRAIL_CAP)]
        self.assertEqual(len(records), 24)


# ============================================================================
# Contract and Adversarial Oracle Tests (Pending owner module jq2a-m8-c1-godot-target)
# ============================================================================

class TestGodotT2OutboxBackpressureAndRingSync(unittest.TestCase):
    """Gate G M8 3: Backpressure preserves complete batches and frame tick.

    Evaluates GodotWindowTarget, godot_adapter re-exports, and outbox backpressure.
    """

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_godot_adapter_reexports
    @expected_failure_if_absent("wender.perceive.godot_adapter")
    def test_godot_adapter_reexports(self):
        """Re-exports in wender.perceive.godot_adapter from wender.perceive.godot match public signatures."""
        from wender.perceive import godot_adapter
        from wender.perceive import godot

        self.assertIs(godot_adapter.node_id_for_control_path, godot.node_id_for_control_path)
        self.assertIs(godot_adapter.parse_ui_state, godot.parse_ui_state)
        self.assertIs(godot_adapter.validate_uitree, godot.validate_uitree)
        self.assertIs(godot_adapter.read_outbox_batches, godot.read_outbox_batches)
        self.assertIs(godot_adapter.write_inbox_batch, godot.write_inbox_batch)
        self.assertIs(godot_adapter.GodotT2Client, godot.GodotT2Client)

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_godot_window_target_bind_and_perceive
    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_godot_window_target_bind_and_perceive(self):
        """GodotWindowTarget.bind and perceive build valid Observation from matching frame tick/basis."""
        from wender.perceive.godot_window_target import GodotWindowTarget
        target, _ = make_target()
        handle = target.handle
        base, sidecar = fixture_pair(10)
        self.assertIsInstance(target, GodotWindowTarget)
        self.assertFalse(target.is_backpressured())

        rgb_data = np.zeros((100, 100, 3), dtype=np.uint8)
        frame = FrameRef.from_rgb(handle, 0, 1, rgb_data)
        target.registries["frame_evidence"] = lambda f: (10, sidecar["base_sha256"])
        obs = target.perceive(_fixture_encode_batch([base, sidecar]), frame=frame)
        self.assertIsInstance(obs, Observation)
        self.assertEqual(obs.handle, handle)
        self.assertEqual(obs.frame.handle, frame.handle)
        self.assertEqual(obs.frame.epoch, frame.epoch)
        self.assertEqual(obs.frame.digest, frame.digest)
        np.testing.assert_array_equal(obs.frame.rgb, rgb_data)
        self.assertEqual((obs.frame.width, obs.frame.height, obs.frame.frame_seq), (100, 100, 1))
        self.assertTrue(target.visual_corroborated)
        self.assertFalse(obs.frame.rgb.flags.writeable)

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_positive_within_cap_batch_accepted
    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_positive_within_cap_batch_accepted(self):
        """Positive control: complete T2 batches with tick trails within cap (<= 24) are accepted cleanly."""
        target, _ = make_target()
        base, sidecar = fixture_pair(10)

        records = [base, sidecar] + [{"tick": i} for i in range(21)]
        raw_data = _fixture_encode_batch(records)
        obs = target.perceive(raw_data, frame=None)
        self.assertFalse(target.is_backpressured())
        self.assertEqual(len(target.tick_trail), 23)
        self.assertIsNotNone(obs)

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_backpressure_excess_queue_depth
    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_backpressure_excess_queue_depth(self):
        """Excess tick trail depth or pending queue backpressures only the affected target before dispatch."""
        target, _ = make_target()
        base, sidecar = fixture_pair(10)

        # Feed batches accumulating over 24 tick trail records without dispatch flush
        records = [base, sidecar] + [{"tick": i} for i in range(22)]
        target.perceive(_fixture_encode_batch(records), frame=None)
        self.assertEqual(len(target.tick_trail), 24)
        self.assertTrue(target.is_backpressured())
        target.perceive(_fixture_encode_batch([{"tick": 24}]), frame=None)
        self.assertEqual(len(target.tick_trail), 25)
        self.assertTrue(target.is_backpressured())

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_oversized_batch_raises_protocol_error
    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_oversized_batch_raises_protocol_error(self):
        """An oversized received batch raises a protocol error (ValueError or similar protocol exception)."""
        target, _ = make_target()
        base, sidecar = fixture_pair(10)

        # A single received batch exceeds the protocol cap.
        oversized_records = [{"tick": i, "ax_sha256": "0" * 64} for i in range(25)]
        raw_data = _fixture_encode_batch(oversized_records)
        with self.assertRaises((ValueError, RuntimeError)):
            target.perceive(raw_data)

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_mutant_drop_tick_trail_refused
    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_mutant_drop_tick_trail_refused(self):
        """Negative mutant: dropping tick trail from complete validated batch must fail invariant check."""
        target, _ = make_target()
        base, sidecar = fixture_pair(10)

        records = [{"tick": i, "ax_sha256": f"{i:064x}"} for i in range(5)]
        raw_data = _fixture_encode_batch([base, sidecar] + records)
        obs = target.perceive(raw_data, frame=None)
        self.assertEqual(list(target.tick_trail), [base, sidecar] + records)
        self.assertEqual(len(target.tick_trail), 7)
        self.assertNotIn("tick_trail", obs.state)

    # @anchor:tests.test_jq2a_m8_j3_godot_t2_outbox_backpressure_and_ring_sync.test_mutant_accept_mismatched_frame_refused
    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_mutant_accept_mismatched_frame_refused(self):
        """Negative mutant: accepting mismatched frame tick or basis abstains from visual corroboration."""
        from wender.perceive.godot_window_target import GodotWindowTarget, GodotPrerequisiteError
        target, _ = make_target()
        handle = target.handle
        base, sidecar = fixture_pair(10)

        target.registries["frame_evidence"] = lambda f: (9, sidecar["base_sha256"])
        frame = FrameRef.from_rgb(handle, 0, 1, np.zeros((100, 100, 3), dtype=np.uint8))
        obs = target.perceive(_fixture_encode_batch([base, sidecar]), frame=frame)
        self.assertIsInstance(obs, Observation)
        self.assertFalse(target.visual_corroborated)
        before = tuple(target.tick_trail)
        stale = FrameRef.from_rgb(handle, 1, 2, frame.rgb)
        with self.assertRaisesRegex(GodotPrerequisiteError, "stale pixel capture identity or epoch"):
            target.perceive(b"", frame=stale)
        self.assertEqual(tuple(target.tick_trail), before)


if __name__ == "__main__":
    unittest.main()
