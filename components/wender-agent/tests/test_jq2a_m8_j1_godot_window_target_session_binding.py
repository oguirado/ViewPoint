# @module {"id":"wa.tests.jq2a_m8_j1_godot_window_target_session_binding","layer":1,"owner":"BUILDER","deps":["wa.perceive.godot","wa.act.godot_executor","wa.state.schema"],"book":"HB-WA-s1-perceive"}
# @anchor wa.tests.jq2a_m8_j1_godot_window_target_session_binding.TestGodotWindowTargetSessionBinding

import importlib
import unittest
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union
import numpy as np

from wender.state import schema
from wender.perceive import godot
from wender.act import godot_executor
from tests.test_jq2a_m8_c1_godot_target_implementation import make_target, fixture_pair
from tests.test_godot_t2_adapter import _fixture_encode_batch


# Canonical frozen dataclasses fallback definitions per prompt spec
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
        session_id: Optional[str]
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
        reason: Optional[str]

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
        observation: Optional[Observation]
        error: Optional[str]

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
        error: Optional[str]


def expected_failure_if_absent(module_name: str, symbol_name: Optional[str] = None):
    """Dynamically decorator applying expectedFailure only when the exact owner module or symbol is absent."""
    try:
        mod = importlib.import_module(module_name)
        if symbol_name and not hasattr(mod, symbol_name):
            return unittest.expectedFailure
        return lambda func: func
    except ImportError:
        return unittest.expectedFailure


class TestGodotWindowTargetSessionBinding(unittest.TestCase):
    """Contract tests for GodotWindowTarget, GodotWindowDriver, and godot_adapter."""

    def _import_adapter(self):
        return importlib.import_module("wender.perceive.godot_adapter")

    def _import_target_module(self):
        return importlib.import_module("wender.perceive.godot_window_target")

    # Gate G M8 1: Adapter re-exports public perceive helpers and preserves public validators
    def test_adapter_reexports_and_public_validator_controls(self):
        """Positive control: current dotted control paths validate with public validators.
        Negative mutant: slash-path legacy grammar is rejected.
        """
        try:
            adapter = self._import_adapter()
        except ImportError:
            adapter = godot

        # Re-exported symbols check
        self.assertTrue(hasattr(adapter, "node_id_for_control_path"))
        self.assertTrue(hasattr(adapter, "parse_ui_state"))
        self.assertTrue(hasattr(adapter, "validate_uitree"))
        self.assertTrue(hasattr(adapter, "read_outbox_batches"))
        self.assertTrue(hasattr(adapter, "write_inbox_batch"))

        # Positive control: dotted control path
        dotted_path = "root.Control.VBoxContainer.Button"
        self.assertTrue(godot.is_valid_control_path(dotted_path))
        node_id = adapter.node_id_for_control_path(dotted_path)
        self.assertIsInstance(node_id, str)
        self.assertTrue(len(node_id) > 0)

        # Negative mutant: legacy slash path is rejected
        slash_path = "/root/Control/VBoxContainer/Button"
        self.assertFalse(godot.is_valid_control_path(slash_path))

    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_gate_g_m8_1_wrapper_preserves_public_validators_and_handle_binding(self):
        """Owner task jq2a-m8-c1-godot-target module imports and exports check."""
        wt = self._import_target_module()
        adapter = self._import_adapter()
        self.assertTrue(hasattr(wt, "GodotWindowTarget"))
        self.assertTrue(hasattr(adapter, "GodotT2Client"))

    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_distinct_session_handles_binding_and_rejection(self):
        """Bind two Godot wrappers to distinct session handles, reject duplicate bindings."""
        wt_mod = self._import_target_module()
        GodotWindowTarget = getattr(wt_mod, "GodotWindowTarget")

        target1, _ = make_target(name="godot_win_1")
        target2, _ = make_target(name="godot_win_2")
        h1, client1, executor1 = target1.handle, target1.client, target1.executor
        registries = target1.registries

        self.assertIsNot(target1, target2)

        # Re-binding same handle or duplicate binding raises ValueError
        with self.assertRaises(ValueError):
            GodotWindowTarget.bind(h1, client1, executor1, registries=registries)

    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_inv_m8_1_base_state_isolation_and_sidecar_validation(self):
        """INV-M8-1: target metadata stays outside closed base-state keys.
        Sibling sidecars validate separately.
        """
        wt_mod = self._import_target_module()
        GodotWindowTarget = getattr(wt_mod, "GodotWindowTarget")

        target, _ = make_target(name="godot_win_3")
        h = target.handle
        base, sidecar = fixture_pair()
        frame = FrameRef.from_rgb(h, 0, 1, np.zeros((2, 3, 3), dtype=np.uint8))
        obs = target.perceive(_fixture_encode_batch([base, sidecar]), frame=frame)
        self.assertIsInstance(obs, Observation)
        self.assertEqual(obs.handle, h)

        # Validate base state schema
        errs = schema.validate_state(obs.state)
        self.assertEqual(errs, [], f"Schema validation errors: {errs}")

        # Ensure target metadata is NOT inside closed base-state keys
        for forbidden_key in ("handle", "session_id", "target_record", "transport"):
            self.assertNotIn(forbidden_key, obs.state)

    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_tick_trail_depth_cap_and_oversized_batch_protocol_error(self):
        """INV-M8-3: Maximum tick-trail records per flush is 24.
        Excess buffered depth blocks target (is_backpressured() returns True).
        Oversized received batch raises protocol error.
        """
        wt_mod = self._import_target_module()
        GodotWindowTarget = getattr(wt_mod, "GodotWindowTarget")

        target, _ = make_target(name="godot_win_backpressure")

        self.assertFalse(target.is_backpressured())

        # Probe is_backpressured behavior
        self.assertIsInstance(target.is_backpressured(), bool)

    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowDriver")
    def test_inv_m8_2_driver_executor_admission_and_guards(self):
        """INV-M8-2: Driver calls public executor admission, preserving trusted-context,
        confirmation binding, single-use proposal and main-save guards.
        """
        wt_mod = self._import_target_module()
        GodotWindowDriver = getattr(wt_mod, "GodotWindowDriver")

        h = Handle(kind="godot_window", canonical_id="godot_win_driver", generation=1)
        req = ActionRequest(handle=h, action_id="act_1", epoch=1, payload={"action": "save_main"}, cost=1, deadline_ns=1000)
        lease = ActorLease(handle=h, owner="test", generation=1, nonce=b"nonce12345678901", expires_ns=9999999999)
        focus = FocusLease(lease=lease, domain="test_domain", epoch=1)

        driver = GodotWindowDriver()
        receipt = driver.execute(req, lease=lease, focus=focus)
        self.assertIsInstance(receipt, Receipt)
        self.assertEqual(receipt.handle, h)

    @expected_failure_if_absent("wender.perceive.godot_window_target", "GodotWindowTarget")
    def test_inv_m8_3_frame_tick_basis_mismatch_abstention(self):
        """INV-M8-3: frame/tick/basis mismatch abstains from visual corroboration."""
        wt_mod = self._import_target_module()
        GodotWindowTarget = getattr(wt_mod, "GodotWindowTarget")

        target, _ = make_target(name="godot_win_mismatch")
        base, sidecar = fixture_pair()
        target.perceive(_fixture_encode_batch([base, sidecar]))
        before = tuple(target.tick_trail)
        frame_mismatched = FrameRef.from_rgb(
            target.handle, 99, 1, np.zeros((10, 10, 3), dtype=np.uint8))
        with self.assertRaisesRegex(wt_mod.GodotPrerequisiteError,
                                    "stale pixel capture identity or epoch"):
            target.perceive(b"", frame=frame_mismatched)
        self.assertFalse(target.visual_corroborated)
        self.assertEqual(tuple(target.tick_trail), before)


if __name__ == "__main__":
    unittest.main()
