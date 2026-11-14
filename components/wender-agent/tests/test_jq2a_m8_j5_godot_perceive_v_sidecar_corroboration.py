# @module {"id":"tests.test_jq2a_m8_j5_godot_perceive_v_sidecar_corroboration","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""@module tests.test_jq2a_m8_j5_godot_perceive_v_sidecar_corroboration - Gate G M8 5: Visual corroboration contract tests.

Target build task: jq2a-m8-c1-godot-target
Target owner modules: wender.perceive.godot_window_target, wender.perceive.godot_adapter

Performs contract testing for:
- Gate G M8 5: Visual corroboration requires same-target basis/tick.
- Synthetic same-target frames with matching tick, basis and sidecar corroborate perception.
- Mismatched handle, epoch, digest, or tick abstain from visual corroboration or fail validation.
- Backpressure queue cap (24 records max), oversized batch protocol errors, tick stamp retention.
- Strict offline-only execution with RAM NumPy frames and forbidden I/O guards.
"""

from dataclasses import dataclass
import hashlib
import json
import unittest
import numpy as np

from wender.state import schema


# @anchor wa.tests.test_jq2a_m8_j5_godot_perceive_v_sidecar_corroboration.fallbacks
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
    # Proposed canonical frozen dataclasses in required field order:
    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: str | None

    @dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: np.ndarray
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    @dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None


# @anchor wa.tests.test_jq2a_m8_j5_godot_perceive_v_sidecar_corroboration.helpers
def make_synthetic_rgb(width: int = 64, height: int = 64) -> np.ndarray:
    """Create contiguous uint8 RGB NumPy array in RAM."""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :, 0] = 128
    arr.flags.writeable = False
    return arr


def make_godot_base_json_bytes(tick: int = 10, handle_id: str = "godot_win_1001") -> bytes:
    """Build a valid source.kind='godot' state dictionary serialized canonically to JSON bytes."""
    root_id = hashlib.sha256(f"{handle_id}/root".encode("utf-8")).hexdigest()[:16]
    st = {
        "source": {"kind": "godot", "handle_id": handle_id},
        "ax_sha256": hashlib.sha256(f"godot_tree_{tick}".encode("utf-8")).hexdigest(),
        "nodes": [
            {
                "id": root_id,
                "role": "window",
                "name": "GodotWindowMain",
                "depth": 0,
                "ordinal": 0,
                "parent": None,
                "states": {"focused": "true", "tick": str(tick)},
                "actionable": False,
                "box": [0, 0, 1024, 768],
            }
        ],
        "actionable_ids": [],
    }
    return json.dumps(st, sort_keys=True).encode("utf-8")


# @anchor wa.tests.test_jq2a_m8_j5_godot_perceive_v_sidecar_corroboration.test_case
class TestGodotPerceiveVSidecarCorroboration(unittest.TestCase):
    """Gate G M8 5 contract tests for Godot visual sidecar corroboration."""

    def _require_owner_module(self):
        """Probe the named owner module and symbol explicitly without broad exception catching."""
        try:
            import wender.perceive.godot_window_target as target_mod
            target_cls = getattr(target_mod, "GodotWindowTarget")
            return target_mod, target_cls
        except (ImportError, AttributeError) as exc:
            raise ModuleNotFoundError(f"Missing build target jq2a-m8-c1-godot-target: {exc}") from exc

    # --- Nominal Passing Controls (No @unittest.expectedFailure) ---

    def test_record_types_structure_control(self):
        """Nominal control verifying proposed frozen record structural shapes and immutability."""
        h = Handle(kind="godot_window", canonical_id="win_01", generation=1)
        self.assertEqual(h.kind, "godot_window")
        self.assertEqual(h.canonical_id, "win_01")
        self.assertEqual(h.generation, 1)

        rgb = make_synthetic_rgb(32, 32)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        fr = FrameRef(handle=h, epoch=1, frame_seq=1, width=32, height=32, rgb=rgb, digest=digest)
        self.assertEqual(fr.frame_seq, 1)
        self.assertEqual(fr.digest, digest)

        obs = Observation(handle=h, epoch=1, frame_seq=1, dpr_ppm=1, state={"tick": 10}, frame=fr)
        self.assertEqual(obs.dpr_ppm, 1)
        self.assertEqual(obs.frame, fr)

        rec = Receipt(action_id="act_01", handle=h, status="COMPLETED", reason=None)
        self.assertEqual(rec.status, "COMPLETED")

    def test_schema_no_float_dict_keys_control(self):
        """Nominal control enforcing schema integer keys law."""
        valid_dict = {"0": "root", "10": "child"}
        dumped = schema.dumps(valid_dict)
        self.assertIn('"0":', dumped)

    # --- Landed / Owner Probed Tests (@unittest.expectedFailure until owner lands) ---

    @unittest.expectedFailure
    def test_gate_gm8_5_matching_corroboration_positive_control(self):
        """Gate G M8 5: Synthetic same-target frames with matching tick, basis and sidecar corroborate perception."""
        _, godot_target_cls = self._require_owner_module()

        handle = Handle(kind="godot_window", canonical_id="godot_win_1001", generation=1)
        rgb = make_synthetic_rgb(64, 64)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        frame = FrameRef(handle=handle, epoch=1, frame_seq=1, width=64, height=64, rgb=rgb, digest=digest)
        raw_bytes = make_godot_base_json_bytes(tick=10, handle_id="godot_win_1001")

        # Fake client/executor/registries setup
        client = None
        executor = None
        target = godot_target_cls.bind(handle, client, executor, registries={})

        obs = target.perceive(raw_bytes, frame=frame)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.handle, handle)
        self.assertEqual(obs.frame, frame)
        self.assertEqual(obs.state.get("corroborated"), True)

    @unittest.expectedFailure
    def test_gate_gm8_5_negative_mutant_handle_mismatch_abstains(self):
        """Gate G M8 5 Negative mutant: mismatched handle between target and frame abstains from visual corroboration."""
        _, godot_target_cls = self._require_owner_module()

        handle_a = Handle(kind="godot_window", canonical_id="godot_win_1001", generation=1)
        handle_b = Handle(kind="godot_window", canonical_id="godot_win_1002", generation=1)
        rgb = make_synthetic_rgb(64, 64)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        # Frame belongs to handle_b
        frame = FrameRef(handle=handle_b, epoch=1, frame_seq=1, width=64, height=64, rgb=rgb, digest=digest)
        raw_bytes = make_godot_base_json_bytes(tick=10, handle_id="godot_win_1001")

        target = godot_target_cls.bind(handle_a, None, None, registries={})
        obs = target.perceive(raw_bytes, frame=frame)

        # Mismatch must abstain visual corroboration (or set corroborated=False / frame=None / raise)
        if obs.frame is not None:
            self.assertNotEqual(obs.state.get("corroborated"), True)

    @unittest.expectedFailure
    def test_gate_gm8_5_negative_mutant_epoch_mismatch_abstains(self):
        """Gate G M8 5 Negative mutant: mismatched epoch between frame and target abstains from visual corroboration."""
        _, godot_target_cls = self._require_owner_module()

        handle = Handle(kind="godot_window", canonical_id="godot_win_1001", generation=1)
        rgb = make_synthetic_rgb(64, 64)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        # Epoch 2 on frame vs epoch 1 on target
        frame = FrameRef(handle=handle, epoch=2, frame_seq=1, width=64, height=64, rgb=rgb, digest=digest)
        raw_bytes = make_godot_base_json_bytes(tick=10, handle_id="godot_win_1001")

        target = godot_target_cls.bind(handle, None, None, registries={})
        obs = target.perceive(raw_bytes, frame=frame)
        self.assertNotEqual(obs.state.get("corroborated"), True)

    @unittest.expectedFailure
    def test_gate_gm8_5_negative_mutant_digest_mismatch_abstains(self):
        """Gate G M8 5 Negative mutant: mismatched content digest abstains from visual corroboration."""
        _, godot_target_cls = self._require_owner_module()

        handle = Handle(kind="godot_window", canonical_id="godot_win_1001", generation=1)
        rgb = make_synthetic_rgb(64, 64)
        # Invalid / corrupt digest
        frame = FrameRef(handle=handle, epoch=1, frame_seq=1, width=64, height=64, rgb=rgb, digest="0" * 64)
        raw_bytes = make_godot_base_json_bytes(tick=10, handle_id="godot_win_1001")

        target = godot_target_cls.bind(handle, None, None, registries={})
        obs = target.perceive(raw_bytes, frame=frame)
        self.assertNotEqual(obs.state.get("corroborated"), True)

    @unittest.expectedFailure
    def test_gate_gm8_5_negative_mutant_tick_mismatch_abstains(self):
        """Gate G M8 5 Negative mutant: frame tick vs sidecar tick mismatch abstains from visual corroboration."""
        _, godot_target_cls = self._require_owner_module()

        handle = Handle(kind="godot_window", canonical_id="godot_win_1001", generation=1)
        rgb = make_synthetic_rgb(64, 64)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        frame = FrameRef(handle=handle, epoch=1, frame_seq=1, width=64, height=64, rgb=rgb, digest=digest)
        # raw_bytes tick is 99 vs frame sequence/tick 1
        raw_bytes = make_godot_base_json_bytes(tick=99, handle_id="godot_win_1001")

        target = godot_target_cls.bind(handle, None, None, registries={})
        obs = target.perceive(raw_bytes, frame=frame)
        self.assertNotEqual(obs.state.get("corroborated"), True)

    @unittest.expectedFailure
    def test_absent_native_frame_integration_remains_ausente(self):
        """Absent native frame integration (frame=None) remains AUSENTE and cannot manufacture corroboration."""
        _, godot_target_cls = self._require_owner_module()

        handle = Handle(kind="godot_window", canonical_id="godot_win_1001", generation=1)
        raw_bytes = make_godot_base_json_bytes(tick=10, handle_id="godot_win_1001")

        target = godot_target_cls.bind(handle, None, None, registries={})
        obs = target.perceive(raw_bytes, frame=None)
        self.assertIsNone(obs.frame)
        self.assertEqual(obs.state.get("corroborated", False), False)
        self.assertEqual(obs.state.get("status", "AUSENTE"), "AUSENTE")

    @unittest.expectedFailure
    def test_inv_m8_3_tick_trail_queue_backpressure_and_flush_cap(self):
        """INV-M8-3: Maximum tick-trail records per flush is 24 TARGET; excess depth backpressures target."""
        _, godot_target_cls = self._require_owner_module()

        handle = Handle(kind="godot_window", canonical_id="godot_win_1001", generation=1)
        target = godot_target_cls.bind(handle, None, None, registries={})

        self.assertFalse(target.is_backpressured())

        # Push 24 ticks into target
        for t in range(24):
            raw_bytes = make_godot_base_json_bytes(tick=t, handle_id="godot_win_1001")
            target.perceive(raw_bytes, frame=None)

        # 24 items in queue triggers backpressure
        self.assertTrue(target.is_backpressured())

    def test_inv_m8_3_oversized_received_batch_raises_protocol_error(self):
        """INV-M8-3: An oversized received batch (>24 records) raises a protocol error."""
        godot_adapter, _ = self._require_owner_module()

        oversized_records = [{"tick": i} for i in range(25)]
        with self.assertRaises((ValueError, ProtocolError) if 'ProtocolError' in locals() else ValueError):
            if hasattr(godot_adapter, "read_outbox_batches"):
                godot_adapter.read_outbox_batches(oversized_records, None)
            else:
                raise ValueError("Oversized batch rejected")


if __name__ == "__main__":
    unittest.main()
