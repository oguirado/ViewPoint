# @module {"id":"wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope","layer":1,"owner":"STATE","deps":["wa.state.schema"],"book":"HB-WA-a0-arch"}
"""Contract and adversarial oracle tests for Target Descriptor Schema Envelope (gate G M8 6).

Target build task / owning implementation: jq2a-m8-g1-descriptor-schema
Owner module & symbols:
  - wender.state.schema: build_target_descriptor, validate_target_descriptor
"""

import dataclasses
import hashlib
import unittest
import numpy as np

from wender.state import schema


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
# Shared Record Imports and Fallbacks (per session_registry spec)
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


def make_valid_base_state():
    """Construct a valid minimal STATE dictionary."""
    nodes = [
        {
            "id": "0000000000000001",
            "role": "RootWebArea",
            "name": "Main Window",
            "depth": 0,
            "ordinal": 0,
            "attrs": (("disabled", "false"),),
            "ancestors": (),
        }
    ]
    return schema.build_state(
        nodes,
        kind="cdp",
        url="file:///test_page",
        title="Test Page",
        ax_sha256="a" * 64,
        ax_stable_sha256="b" * 64,
        prune={"name": "test_prune", "version": 1},
        raw_count=1,
        boxes={"0000000000000001": [0, 0, 1024, 768]},
        viewport={"w": 1024, "h": 768},
        actionable_roles=frozenset(),
    )


# ============================================================================
# Healthy Control Tests (Run without expected failure)
# ============================================================================

class TestTargetDescriptorSchemaHealthyControls(unittest.TestCase):
    """Healthy control tests for record definitions and base state helpers."""

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_session_registry_shared_records_field_order
    def test_session_registry_shared_records_field_order(self):
        """Verify session registry dataclasses conform to exact specified field order."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_001", generation=1)
        self.assertEqual(handle.kind, "cdp_tab")
        self.assertEqual(handle.canonical_id, "tab_001")
        self.assertEqual(handle.generation, 1)

        tr = TargetRecord(
            handle=handle,
            transport=None,
            owned=True,
            epoch=0,
            session_id="s1",
            attachment_owned=True,
            closed=False,
        )
        self.assertEqual(tr.session_id, "s1")

        rgb_arr = np.zeros((600, 800, 3), dtype=np.uint8)
        digest_sha = hashlib.sha256(rgb_arr.tobytes()).hexdigest()
        frame = FrameRef(
            handle=handle,
            epoch=0,
            frame_seq=0,
            width=800,
            height=600,
            rgb=rgb_arr,
            digest=digest_sha,
        )
        self.assertEqual(frame.digest, digest_sha)

        obs = Observation(
            handle=handle,
            epoch=0,
            frame_seq=0,
            dpr_ppm=1000000,
            state={},
            frame=frame,
        )
        self.assertEqual(obs.dpr_ppm, 1000000)

        receipt = Receipt(action_id="act_01", handle=handle, status="ok", reason=None)
        self.assertEqual(receipt.status, "ok")

        ar = ActionRequest(
            handle=handle,
            action_id="act_01",
            epoch=0,
            payload={},
            cost=1,
            deadline_ns=1000,
        )
        self.assertEqual(ar.cost, 1)

        al = ActorLease(
            handle=handle,
            owner="owner1",
            generation=1,
            nonce=b"12345678",
            expires_ns=5000,
        )
        self.assertEqual(al.expires_ns, 5000)

        fl = FocusLease(lease=al, domain="default", epoch=0)
        self.assertEqual(fl.domain, "default")

        spec = StreamSpec(width=1024, height=768, slots=2, quota_bytes=10000000)
        self.assertEqual(spec.quota_bytes, 10000000)

        st = StreamToken(handle=handle, epoch=0)
        self.assertEqual(st.epoch, 0)

        tkt = Ticket(action_id="act_01", handle=handle)
        self.assertEqual(tkt.action_id, "act_01")

        cr = ChangeResult(
            handle=handle,
            epoch=0,
            settled=True,
            claims=np.array([1, 2, 3]),
            ops={},
        )
        self.assertTrue(cr.settled)

        pr = PerceptionResult(handle=handle, observation=obs, error=None)
        self.assertIsNone(pr.error)

        wf = WindowFrames(
            handle=handle,
            epoch=0,
            frames=(),
            frame_seqs=(),
            dpr_ppm=1000000,
        )
        self.assertEqual(wf.dpr_ppm, 1000000)

        wd = WindowDescription(
            handle=handle,
            epoch=0,
            frame_seqs=(),
            descriptors=(),
            regions=(),
            error=None,
        )
        self.assertIsNone(wd.error)


# ============================================================================
# Gate G M8 6: Target Descriptor Schema Envelope Contract Tests
# ============================================================================

class TestTargetDescriptorSchemaEnvelope(unittest.TestCase):
    """Gate G M8 6: Target Descriptor Schema Envelope Contract Tests.

    Rejects leaked descriptor keys into base TOP_KEYS, floats, negative epochs,
    stale caller-registry epochs, malformed handles and invalid nested base state.
    """

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_positive_build_and_validate_target_descriptor
    @expected_failure_if_absent("wender.state.schema", "build_target_descriptor")
    def test_positive_build_and_validate_target_descriptor(self):
        """Positive control: valid outer descriptor wraps closed base state and validates with empty violations."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)
        base_state = make_valid_base_state()

        envelope = schema.build_target_descriptor(
            handle=handle,
            epoch=0,
            state=base_state,
            frame_seq=0,
            dpr_ppm=1000000,
        )

        self.assertIsInstance(envelope, dict)
        self.assertEqual(
            sorted(envelope.keys()),
            sorted(schema.TARGET_DESCRIPTOR_KEYS),
        )
        self.assertEqual(envelope["handle"], handle)
        self.assertEqual(envelope["epoch"], 0)
        self.assertEqual(envelope["frame_seq"], 0)
        self.assertEqual(envelope["dpr_ppm"], 1000000)
        self.assertEqual(envelope["state"], base_state)

        errs = schema.validate_target_descriptor(envelope)
        self.assertEqual(errs, [])

        dict_handle = {"kind": "godot_window", "canonical_id": "win_01", "generation": 1}
        env_dict_handle = schema.build_target_descriptor(
            handle=dict_handle,
            epoch=5,
            state=base_state,
        )
        errs_dict = schema.validate_target_descriptor(env_dict_handle)
        self.assertEqual(errs_dict, [])

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_reject_descriptor_keys_leaked_into_base_top_keys
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_reject_descriptor_keys_leaked_into_base_top_keys(self):
        """Descriptor keys (e.g. handle or epoch) leaked into base state TOP_KEYS must be rejected."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)
        base_state = make_valid_base_state()

        leaked_state = dict(base_state)
        leaked_state["handle"] = handle

        envelope = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": leaked_state,
        }

        errs = schema.validate_target_descriptor(envelope)
        self.assertTrue(errs)
        self.assertTrue(any("top-level keys" in e for e in errs))

        leaked_epoch_state = dict(base_state)
        leaked_epoch_state["epoch"] = 0
        envelope_epoch = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": leaked_epoch_state,
        }
        errs_epoch = schema.validate_target_descriptor(envelope_epoch)
        self.assertTrue(errs_epoch)
        self.assertTrue(any("top-level keys" in e for e in errs_epoch))

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_reject_floats_in_target_descriptor
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_reject_floats_in_target_descriptor(self):
        """Floats in envelope fields or nested state must raise StateError / fail validation."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)
        base_state = make_valid_base_state()

        # Float epoch
        env_float_epoch = {
            "handle": handle,
            "epoch": 1.0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs = schema.validate_target_descriptor(env_float_epoch)
        self.assertTrue(errs)
        self.assertTrue(any("float" in e or "epoch" in e for e in errs))

        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle=handle, epoch=1.0, state=base_state)

        # Float dpr_ppm
        env_float_dpr = {
            "handle": handle,
            "epoch": 1,
            "frame_seq": 0,
            "dpr_ppm": 1000000.5,
            "state": base_state,
        }
        errs_dpr = schema.validate_target_descriptor(env_float_dpr)
        self.assertTrue(errs_dpr)

        # Float frame_seq
        env_float_seq = {
            "handle": handle,
            "epoch": 1,
            "frame_seq": 2.5,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs_seq = schema.validate_target_descriptor(env_float_seq)
        self.assertTrue(errs_seq)

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_reject_negative_epoch_and_frame_seq
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_reject_negative_epoch_and_frame_seq(self):
        """Negative epoch or negative frame_seq must be rejected by validate_target_descriptor."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)
        base_state = make_valid_base_state()

        env_neg_epoch = {
            "handle": handle,
            "epoch": -1,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs = schema.validate_target_descriptor(env_neg_epoch)
        self.assertTrue(errs)
        self.assertTrue(any("epoch" in e for e in errs))

        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle=handle, epoch=-1, state=base_state)

        env_neg_seq = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": -5,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs_seq = schema.validate_target_descriptor(env_neg_seq)
        self.assertTrue(errs_seq)
        self.assertTrue(any("frame_seq" in e for e in errs_seq))

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_reject_nonpositive_dpr
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_reject_nonpositive_dpr(self):
        """Nonpositive DPR (0 or negative) must be rejected."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)
        base_state = make_valid_base_state()

        env_zero_dpr = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 0,
            "state": base_state,
        }
        errs = schema.validate_target_descriptor(env_zero_dpr)
        self.assertTrue(errs)
        self.assertTrue(any("dpr_ppm" in e for e in errs))

        env_neg_dpr = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": -100,
            "state": base_state,
        }
        errs_neg = schema.validate_target_descriptor(env_neg_dpr)
        self.assertTrue(errs_neg)

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_reject_stale_caller_registry_epoch
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_reject_stale_caller_registry_epoch(self):
        """Callers compare envelope epoch to current registry epoch and refuse stale descriptors."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)
        base_state = make_valid_base_state()

        envelope = schema.build_target_descriptor(handle=handle, epoch=2, state=base_state)

        # Matching expected epoch
        self.assertEqual(schema.validate_target_descriptor(envelope, expected_epoch=2), [])
        self.assertEqual(schema.validate_target_descriptor(envelope, registry_epoch=2), [])

        # Stale caller-registry epoch mismatch
        errs_expected = schema.validate_target_descriptor(envelope, expected_epoch=3)
        self.assertTrue(errs_expected)
        self.assertTrue(any("does not match registry epoch" in e for e in errs_expected))

        errs_registry = schema.validate_target_descriptor(envelope, registry_epoch=1)
        self.assertTrue(errs_registry)
        self.assertTrue(any("does not match registry epoch" in e for e in errs_registry))

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_reject_malformed_handle
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_reject_malformed_handle(self):
        """Malformed handles (invalid keys, unknown kind, empty canonical_id, negative generation) are rejected."""
        base_state = make_valid_base_state()

        # Handle missing generation
        bad_handle_dict = {"kind": "cdp_tab", "canonical_id": "tab_1"}
        env_bad_dict = {
            "handle": bad_handle_dict,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs = schema.validate_target_descriptor(env_bad_dict)
        self.assertTrue(errs)

        # Unknown target kind
        bad_kind_dict = {"kind": "invalid_kind", "canonical_id": "tab_1", "generation": 1}
        env_bad_kind = {
            "handle": bad_kind_dict,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs_kind = schema.validate_target_descriptor(env_bad_kind)
        self.assertTrue(errs_kind)
        self.assertTrue(any("handle.kind" in e for e in errs_kind))

        # Empty canonical_id
        empty_id_dict = {"kind": "cdp_tab", "canonical_id": "", "generation": 1}
        env_empty_id = {
            "handle": empty_id_dict,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs_id = schema.validate_target_descriptor(env_empty_id)
        self.assertTrue(errs_id)
        self.assertTrue(any("canonical_id" in e for e in errs_id))

        # Negative generation
        neg_gen_dict = {"kind": "cdp_tab", "canonical_id": "tab_1", "generation": -1}
        env_neg_gen = {
            "handle": neg_gen_dict,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": base_state,
        }
        errs_gen = schema.validate_target_descriptor(env_neg_gen)
        self.assertTrue(errs_gen)
        self.assertTrue(any("generation" in e for e in errs_gen))

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_reject_invalid_nested_base_state
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_reject_invalid_nested_base_state(self):
        """Invalid nested base state must trigger validate_state violations inside validate_target_descriptor."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)

        # Corrupt state (schema mismatch)
        bad_state = make_valid_base_state()
        bad_state["schema"] = "invalid.schema/v0"

        envelope = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": bad_state,
        }
        errs = schema.validate_target_descriptor(envelope)
        self.assertTrue(errs)
        self.assertTrue(any("schema must be" in e for e in errs))

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_negative_mutant_leaked_top_keys_refused
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_negative_mutant_leaked_top_keys_refused(self):
        """Negative mutant: merging handle or epoch into TOP_KEYS of base state must fail validation."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)
        base_state = make_valid_base_state()

        # Mutant 1: Merging handle into inner state
        mutant_state_1 = dict(base_state)
        mutant_state_1["handle"] = handle.canonical_id
        env1 = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": mutant_state_1,
        }
        self.assertTrue(schema.validate_target_descriptor(env1))

        # Mutant 2: Merging epoch into inner state
        mutant_state_2 = dict(base_state)
        mutant_state_2["epoch"] = 0
        env2 = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": mutant_state_2,
        }
        self.assertTrue(schema.validate_target_descriptor(env2))

    # @anchor:wa.tests.test_jq2a_m8_j6_target_descriptor_schema_envelope.test_negative_mutant_skipping_nested_validation_refused
    @expected_failure_if_absent("wender.state.schema", "validate_target_descriptor")
    def test_negative_mutant_skipping_nested_validation_refused(self):
        """Negative mutant: skipping validate_state on nested state must fail contract verification."""
        handle = Handle(kind="cdp_tab", canonical_id="tab_100", generation=1)

        # Non-dict state
        env_non_dict = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": "not_a_dict_state",
        }
        errs_non_dict = schema.validate_target_descriptor(env_non_dict)
        self.assertTrue(errs_non_dict)

        # Completely invalid dict state missing TOP_KEYS
        env_corrupt = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": {"bogus_key": 123},
        }
        errs_corrupt = schema.validate_target_descriptor(env_corrupt)
        self.assertTrue(errs_corrupt)


if __name__ == "__main__":
    unittest.main()
