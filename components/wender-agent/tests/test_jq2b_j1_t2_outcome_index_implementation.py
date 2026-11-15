"""Substantive public-path conformance test suite for jq2b-j1-t2-outcome-index.

Validates wender.learn.t2_outcome_index.OutcomeIndex production owner implementation
against invariants INV J1 1 through INV J1 4, gate G J1 3, and source-agnostic policies.
No skipped tests, no expectedFailure or unexpected successes.
"""
import builtins
import hashlib
import unittest

import numpy as np

import wender.learn.t2_outcome_index as t2_mod
from wender.learn.t2_outcome_index import (
    ATTEMPT_FIELD_SET,
    COMMAND_FIELD_SET,
    ENGINE_OUTCOME_FIELD_SET,
    PHASE_ACCEPTED,
    PHASE_PENDING,
    PHASE_PROPOSED,
    PHASE_REFUSED,
    PHASE_REJECTED,
    PHASE_UNKNOWN,
    PHASES,
    Attempt,
    CanonicalFramingError,
    CapacityExceededError,
    Command,
    ConflictingDuplicateError,
    CorrelationError,
    EngineOutcome,
    OutcomeIndex,
    OutcomeIndexError,
    UnauthenticatedStreamError,
    compute_canonical_sha256,
    dumps_canonical,
    parse_canonical_json,
    validate_attempt,
    validate_command,
    validate_outcome,
)
from wender.perceive.session_registry import (
    SUPPORTED_KINDS,
    FrameRef,
    Handle,
    Observation,
    Receipt,
    RegistryError,
    SessionRegistry,
    StreamSpec,
    StreamToken,
    Ticket,
)

# Test-only deterministic SHA-256 fixture hashes
HASH_STATE_0 = "0" * 64
HASH_STATE_POST_1 = "1" * 64
HASH_STATE_POST_2 = "2" * 64
HASH_BASIS_PRE = "b" * 64


class TestGateGJ13CanonicalCompleteFraming(unittest.TestCase):
    """Gate G J1 3 conformance tests: Canonical complete framing, negative contrast, positive controls."""

    def test_positive_control_complete_canonical_records_roundtrip(self):
        """Positive control: complete canonical records parse, serialize, and round-trip without loss."""
        raw_cmd = {"tick": 10, "seq": 1, "cmd": "pause", "args": {}}
        cmd = validate_command(raw_cmd)
        self.assertEqual(cmd.tick, 10)
        self.assertEqual(cmd.seq, 1)
        self.assertEqual(cmd.cmd, "pause")
        self.assertEqual(cmd.args, {})

        # Serializes deterministically to sorted JSON without floats
        dumped = dumps_canonical(cmd)
        self.assertIn('"cmd":"pause"', dumped)
        parsed = parse_canonical_json(dumped)
        self.assertEqual(parsed, raw_cmd)

        # Outcome canonical roundtrip
        raw_outcome = {
            "seq": 1,
            "requested_tick": 10,
            "applied_tick": 10,
            "order_seq_id": 101,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }
        outcome = validate_outcome(raw_outcome)
        self.assertEqual(outcome.seq, 1)
        self.assertEqual(outcome.applied_tick, 10)
        self.assertEqual(outcome.status, "accepted")
        out_dumped = dumps_canonical(outcome)
        self.assertEqual(parse_canonical_json(out_dumped), raw_outcome)

    def test_negative_contrast_duplicate_json_key_rejected(self):
        """Negative contrast: duplicate JSON key in framing raises CanonicalFramingError."""
        dup_json = '{"tick": 10, "seq": 1, "cmd": "pause", "seq": 2, "args": {}}'
        with self.assertRaises(CanonicalFramingError):
            parse_canonical_json(dup_json)

    def test_negative_contrast_float_in_command_or_args_rejected(self):
        """Negative contrast: floats anywhere in command, tick, or args raise CanonicalFramingError."""
        # Float tick
        with self.assertRaises(CanonicalFramingError):
            validate_command({"tick": 1.5, "seq": 1, "cmd": "pause", "args": {}})

        # Float in args
        with self.assertRaises(CanonicalFramingError):
            validate_command({"tick": 1, "seq": 1, "cmd": "pause", "args": {"speed": 2.0}})

        # Float in JSON parser
        float_json = '{"tick": 1, "seq": 1, "cmd": "step", "args": {"val": 1.25}}'
        with self.assertRaises(CanonicalFramingError):
            parse_canonical_json(float_json)

    def test_negative_contrast_float_in_outcome_rejected(self):
        """Negative contrast: floats in engine outcome fields raise CanonicalFramingError."""
        bad_outcome = {
            "seq": 1,
            "requested_tick": 10.0,
            "applied_tick": 10,
            "order_seq_id": 101,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }
        with self.assertRaises(CanonicalFramingError):
            validate_outcome(bad_outcome)

    def test_negative_contrast_boolean_in_integer_domain_rejected(self):
        """Negative contrast: booleans used in integer tick or seq domains are rejected."""
        with self.assertRaises(CanonicalFramingError):
            validate_command({"tick": True, "seq": 1, "cmd": "pause", "args": {}})
        with self.assertRaises(CanonicalFramingError):
            validate_command({"tick": 10, "seq": False, "cmd": "pause", "args": {}})

    def test_negative_contrast_unicode_line_separators_rejected(self):
        """Negative contrast: raw Unicode line separators (U+2028, U+2029) or CR in line rejected."""
        u2028_json = '{"tick": 1, "seq": 1, "cmd": "pause\u2028cmd", "args": {}}'
        with self.assertRaises(CanonicalFramingError):
            parse_canonical_json(u2028_json)

        cr_json = '{"tick": 1, "seq": 1, "cmd": "pause", "args": {}}\r'
        with self.assertRaises(CanonicalFramingError):
            parse_canonical_json(cr_json)

    def test_negative_contrast_authority_fields_inside_command_rejected(self):
        """Negative contrast: trusted authority fields inside external command envelope are rejected."""
        for auth_key in ("principal", "company", "role", "mandate", "session", "ticket"):
            with self.subTest(auth_key=auth_key):
                bad_cmd = {
                    "tick": 1,
                    "seq": 1,
                    "cmd": "pause",
                    "args": {},
                    auth_key: "injected_authority",
                }
                with self.assertRaises(CanonicalFramingError):
                    validate_command(bad_cmd)
                bad_nested_cmd = {
                    "tick": 1,
                    "seq": 1,
                    "cmd": "pause",
                    "args": {auth_key: "injected_authority"},
                }
                with self.assertRaises(CanonicalFramingError):
                    validate_command(bad_nested_cmd)


class TestInvariantJ1TraceSemantics(unittest.TestCase):
    """Conformance tests for Invariants INV J1 1 through INV J1 4."""

    def setUp(self):
        self.handle = Handle(kind="godot_window", canonical_id="godot_win_1", generation=1)
        self.stream_token = StreamToken(handle=self.handle, epoch=1)
        self.ticket = Ticket(action_id="act_001", handle=self.handle)

    def test_inv_j1_1_local_admission_distinct_from_engine_acceptance(self):
        """INV J1 1: External command fields strictly tick, seq, cmd, args; authority remains outside."""
        index = OutcomeIndex(capacity=10)
        cmd = {"tick": 5, "seq": 1, "cmd": "pause", "args": {}}

        attempt = index.add(
            attempt_id="att_001",
            action_id="act_001",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command=cmd,
            local_status="admitted",
        )

        # External command envelope has strictly 4 fields
        self.assertEqual(
            set(attempt.command.keys() if isinstance(attempt.command, dict) else attempt.command.to_dict().keys()),
            COMMAND_FIELD_SET,
        )
        # Authority context remains on attempt record outside command envelope
        self.assertEqual(attempt.stream_ref, self.stream_token)
        self.assertEqual(attempt.source_ticket, self.ticket)
        # Local admission is PENDING, not yet engine accepted
        self.assertEqual(attempt.phase, PHASE_PENDING)
        self.assertIsNone(attempt.committed_state_sha256)
        self.assertIsNotNone(attempt.emitted_sha256)

    def test_inv_j1_2_attempt_outcome_fields_and_exact_hash_binding(self):
        """INV J1 2: Canonical attempt and engine outcome fields with correlated post-commit hash binding."""
        index = OutcomeIndex(capacity=10)
        cmd = {"tick": 5, "seq": 1, "cmd": "pause", "args": {}}
        outcome = {
            "seq": 1,
            "requested_tick": 5,
            "applied_tick": 5,
            "order_seq_id": 1001,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }

        attempt = index.add(
            attempt_id="att_002",
            action_id="act_002",
            stream_ref=self.stream_token,
            source_ticket=Ticket(action_id="act_002", handle=self.handle),
            command=cmd,
            local_status="admitted",
            outcome=outcome,
        )

        # Check all 11 Attempt fields
        att_dict = attempt.to_dict()
        self.assertEqual(set(att_dict.keys()), ATTEMPT_FIELD_SET)
        # Engine outcome fields
        self.assertEqual(
            set(
                attempt.engine_outcome.keys()
                if isinstance(attempt.engine_outcome, dict)
                else attempt.engine_outcome.to_dict().keys()
            ),
            ENGINE_OUTCOME_FIELD_SET,
        )
        # Phase and exact post-commit hash binding
        self.assertEqual(attempt.phase, PHASE_ACCEPTED)
        self.assertEqual(attempt.committed_state_sha256, HASH_STATE_POST_1)
        self.assertEqual(attempt.engine_outcome["state_sha256"], HASH_STATE_POST_1)

    def test_inv_j1_3_local_refusal_emits_nothing_and_burns_no_sequence(self):
        """INV J1 3: Local refusal emits nothing and burns no sequence; engine rejection consumes bookkeeping."""
        index = OutcomeIndex(capacity=10)
        cmd = {"tick": 5, "seq": 1, "cmd": "pause", "args": {}}

        refused = index.add(
            attempt_id="att_refused",
            action_id="act_refused",
            stream_ref=self.stream_token,
            source_ticket=Ticket(action_id="act_refused", handle=self.handle),
            command=cmd,
            local_status="refused",
            local_reason="TICK_WINDOW",
        )

        self.assertEqual(refused.phase, PHASE_REFUSED)
        self.assertIsNone(refused.emitted_sha256, "Local refusal must emit nothing")
        self.assertIsNone(refused.committed_state_sha256, "Local refusal burns no simulation state")
        self.assertIsNone(refused.engine_outcome)
        self.assertEqual(refused.local_reason, "TICK_WINDOW")

        # Since local refusal burned no sequence, seq=1 can still be admitted and emitted on the same stream!
        admitted_seq1_outcome = {
            "seq": 1,
            "requested_tick": 6,
            "applied_tick": 6,
            "order_seq_id": 1001,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }
        admitted_seq1 = index.add(
            attempt_id="att_admitted_seq1",
            action_id="act_admitted_seq1",
            stream_ref=self.stream_token,
            source_ticket=Ticket(action_id="act_admitted_seq1", handle=self.handle),
            command={"tick": 6, "seq": 1, "cmd": "pause", "args": {}},
            local_status="admitted",
            outcome=admitted_seq1_outcome,
        )
        self.assertEqual(admitted_seq1.phase, PHASE_ACCEPTED)

        # In contrast, an engine rejection was locally admitted and emitted, but rejected by engine
        engine_rej_outcome = {
            "seq": 2,
            "requested_tick": 6,
            "applied_tick": 6,
            "order_seq_id": 1002,
            "status": "rejected",
            "reason": "REJECTED_TICK_WINDOW",
            "state_sha256": HASH_STATE_0,
        }
        cmd2 = {"tick": 6, "seq": 2, "cmd": "pause", "args": {}}
        rejected_attempt = index.add(
            attempt_id="att_eng_rej",
            action_id="act_eng_rej",
            stream_ref=self.stream_token,
            source_ticket=Ticket(action_id="act_eng_rej", handle=self.handle),
            command=cmd2,
            local_status="admitted",
            outcome=engine_rej_outcome,
        )

        self.assertEqual(rejected_attempt.phase, PHASE_REJECTED)
        self.assertIsNotNone(rejected_attempt.emitted_sha256, "Admitted command produced emitted hash")
        self.assertEqual(rejected_attempt.committed_state_sha256, HASH_STATE_0)
        self.assertEqual(rejected_attempt.engine_outcome["reason"], "REJECTED_TICK_WINDOW")
        self.assertEqual(rejected_attempt.local_reason, "REJECTED_TICK_WINDOW")

    def test_inv_j1_4_missing_outcome_pending_and_timeout_unknown(self):
        """INV J1 4: Missing outcome remains PENDING, and timeout transitions to UNKNOWN."""
        index = OutcomeIndex(capacity=10)
        cmd = {"tick": 1, "seq": 1, "cmd": "pause", "args": {}}

        att = index.add(
            attempt_id="att_pend",
            action_id="act_pend",
            stream_ref=self.stream_token,
            source_ticket=Ticket(action_id="act_pend", handle=self.handle),
            command=cmd,
            local_status="admitted",
        )
        self.assertEqual(att.phase, PHASE_PENDING)
        self.assertIsNone(att.committed_state_sha256)

        # Timeout marks attempt UNKNOWN
        timed_out = index.mark_timeout("att_pend")
        self.assertEqual(timed_out.phase, PHASE_UNKNOWN)
        self.assertIsNone(timed_out.committed_state_sha256)
        self.assertEqual(timed_out.local_reason, "TIMEOUT")

    def test_inv_j1_4_idempotent_duplicate_vs_conflicting_duplicate(self):
        """INV J1 4: Identical authenticated duplicates are idempotent; conflicting duplicates fail without mutation."""
        index = OutcomeIndex(capacity=10)
        cmd = {"tick": 1, "seq": 1, "cmd": "pause", "args": {}}
        tk = Ticket(action_id="act_dup_test", handle=self.handle)

        att1 = index.add(
            attempt_id="att_dup_test",
            action_id="act_dup_test",
            stream_ref=self.stream_token,
            source_ticket=tk,
            command=cmd,
            local_status="admitted",
        )

        # Identical authenticated duplicate: idempotent return
        att2 = index.add(
            attempt_id="att_dup_test",
            action_id="act_dup_test",
            stream_ref=self.stream_token,
            source_ticket=tk,
            command=cmd,
            local_status="admitted",
        )
        self.assertEqual(att1, att2)
        self.assertEqual(len(index), 1)

        # Conflicting duplicate: same attempt_id with different command tick
        conflicting_cmd = {"tick": 99, "seq": 1, "cmd": "pause", "args": {}}
        with self.assertRaises(ConflictingDuplicateError):
            index.add(
                attempt_id="att_dup_test",
                action_id="act_dup_test",
                stream_ref=self.stream_token,
                source_ticket=tk,
                command=conflicting_cmd,
                local_status="admitted",
            )

        # Verify no partial mutation occurred: original entry is unchanged
        self.assertEqual(len(index), 1)
        stored = index.get("att_dup_test")
        self.assertEqual(stored.command.tick, 1)

        # Conflicting duplicate: sequence collision in same stream with different attempt_id
        cmd_seq_collision = {"tick": 1, "seq": 1, "cmd": "pause", "args": {}}
        with self.assertRaises(ConflictingDuplicateError):
            index.add(
                attempt_id="att_different_id",
                action_id="act_different",
                stream_ref=self.stream_token,
                source_ticket=Ticket(action_id="act_different", handle=self.handle),
                command=cmd_seq_collision,
                local_status="admitted",
            )
        self.assertEqual(len(index), 1)

    def test_inv_j1_4_no_pre_action_basis_hash_substitution(self):
        """INV J1 4: Never substitute pre-action basis hash for post-commit hash."""
        index = OutcomeIndex(capacity=10)
        cmd = {"tick": 1, "seq": 1, "cmd": "pause", "args": {}}
        outcome = {
            "seq": 1,
            "requested_tick": 1,
            "applied_tick": 1,
            "order_seq_id": 50,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }

        # Suppose caller provided committed_state_sha256 pointing to basis hash
        att = index.add(
            attempt_id="att_hash_test",
            action_id="act_hash_test",
            stream_ref=self.stream_token,
            source_ticket=Ticket(action_id="act_hash_test", handle=self.handle),
            command=cmd,
            local_status="admitted",
            outcome=outcome,
            committed_state_sha256=HASH_BASIS_PRE,  # pre-action basis hash
        )

        # Index MUST enforce post-commit hash from outcome, rejecting substitution
        self.assertEqual(
            att.committed_state_sha256,
            HASH_STATE_POST_1,
            "Must preserve post-commit hash from outcome, never pre-action basis hash",
        )


class TestBoundedCapacityAndLifecycle(unittest.TestCase):
    """Tests for explicit capacity bounds, producer-complete boundaries, and live entry protection."""

    def setUp(self):
        self.handle = Handle(kind="godot_window", canonical_id="win_cap", generation=1)
        self.stream_token = StreamToken(handle=self.handle, epoch=1)
        self.ticket = Ticket(action_id="act_cap", handle=self.handle)

    def test_explicit_capacity_is_required_no_inferred_defaults(self):
        """Explicit capacity is required; None or non-positive integers raise ValueError."""
        with self.assertRaises(ValueError):
            OutcomeIndex(capacity=0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            OutcomeIndex(capacity=-5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            OutcomeIndex()  # type: ignore[call-arg]

    def test_bounded_capacity_evicts_producer_complete_fifo(self):
        """Capacity limit evicts oldest producer-complete (settled) entry to accept new entry."""
        index = OutcomeIndex(capacity=2)

        # Entry 1: settled (ACCEPTED)
        outcome1 = {
            "seq": 1,
            "requested_tick": 1,
            "applied_tick": 1,
            "order_seq_id": 1,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }
        index.add(
            attempt_id="att_1",
            action_id="act_1",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
            local_status="admitted",
            outcome=outcome1,
        )

        # Entry 2: settled (REJECTED)
        outcome2 = {
            "seq": 2,
            "requested_tick": 2,
            "applied_tick": 2,
            "order_seq_id": 2,
            "status": "rejected",
            "reason": "FAILED_OP",
            "state_sha256": HASH_STATE_0,
        }
        index.add(
            attempt_id="att_2",
            action_id="act_2",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command={"tick": 2, "seq": 2, "cmd": "pause", "args": {}},
            local_status="admitted",
            outcome=outcome2,
        )
        self.assertEqual(len(index), 2)

        # Adding Entry 3 should evict the oldest settled entry (att_1)
        outcome3 = {
            "seq": 3,
            "requested_tick": 3,
            "applied_tick": 3,
            "order_seq_id": 3,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_2,
        }
        index.add(
            attempt_id="att_3",
            action_id="act_3",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command={"tick": 3, "seq": 3, "cmd": "pause", "args": {}},
            local_status="admitted",
            outcome=outcome3,
        )

        self.assertEqual(len(index), 2)
        self.assertNotIn("att_1", index, "Oldest completed entry should be evicted")
        self.assertIn("att_2", index)
        self.assertIn("att_3", index)

    def test_unmatched_live_entries_never_evicted(self):
        """Capacity reached with all live (PENDING) entries refuses new adds with CapacityExceededError."""
        index = OutcomeIndex(capacity=2)

        # Add 2 live (PENDING) entries
        index.add(
            attempt_id="att_live_1",
            action_id="act_l1",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
            local_status="admitted",
        )
        index.add(
            attempt_id="att_live_2",
            action_id="act_l2",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command={"tick": 2, "seq": 2, "cmd": "pause", "args": {}},
            local_status="admitted",
        )
        self.assertEqual(len(index), 2)

        # Attempt to add 3rd entry when all 2 entries are live: MUST FAIL rather than evict live entries
        with self.assertRaises(CapacityExceededError):
            index.add(
                attempt_id="att_live_3",
                action_id="act_l3",
                stream_ref=self.stream_token,
                source_ticket=self.ticket,
                command={"tick": 3, "seq": 3, "cmd": "pause", "args": {}},
                local_status="admitted",
            )

        # Both live entries must still be present and intact
        self.assertIn("att_live_1", index)
        self.assertIn("att_live_2", index)
        self.assertEqual(index.get("att_live_1").phase, PHASE_PENDING)
        self.assertEqual(index.get("att_live_2").phase, PHASE_PENDING)

    def test_outcome_only_addition_and_producer_complete_boundary(self):
        """OutcomeIndex.add with outcome and authenticated stream_ref protects unmatched live outcomes."""
        completed_seqs = set()
        index = OutcomeIndex(
            capacity=2,
            producer_complete_boundary=lambda att: att.seq in completed_seqs,
        )

        out1 = {
            "seq": 1,
            "requested_tick": 10,
            "applied_tick": 10,
            "order_seq_id": 101,
            "status": "rejected",
            "reason": "INSUFFICIENT_FUNDS",
            "state_sha256": HASH_STATE_0,
        }
        out2 = {
            "seq": 2,
            "requested_tick": 11,
            "applied_tick": 11,
            "order_seq_id": 102,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }
        out3 = {
            "seq": 3,
            "requested_tick": 12,
            "applied_tick": 12,
            "order_seq_id": 103,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_2,
        }

        r1 = index.add(outcome=out1, stream_ref=self.stream_token)
        self.assertEqual(r1.reason, "INSUFFICIENT_FUNDS")
        self.assertEqual(r1.committed_state_sha256, HASH_STATE_0)

        # Idempotent duplicate outcome
        r1_dup = index.add(out1, self.stream_token)
        self.assertEqual(r1, r1_dup)
        self.assertEqual(len(index), 1)

        r2 = index.add(out2, self.stream_token)
        self.assertEqual(r2.committed_state_sha256, HASH_STATE_POST_1)
        self.assertEqual(len(index), 2)

        # Conflicting duplicate outcome at seq=1 refuses without partial mutation or eviction
        bad_out1 = dict(out1, state_sha256=HASH_STATE_POST_2)
        with self.assertRaises(ConflictingDuplicateError):
            index.add(bad_out1, self.stream_token)
        self.assertEqual(index.get_by_correlation(self.stream_token, 1).committed_state_sha256, HASH_STATE_0)

        # While neither out1 nor out2 is producer-complete, adding out3 must fail without evicting live entries
        with self.assertRaises(CapacityExceededError):
            index.add(out3, self.stream_token)
        self.assertIsNotNone(index.get_by_correlation(self.stream_token, 1))
        self.assertIsNotNone(index.get_by_correlation(self.stream_token, 2))

        # Mark seq=1 complete via boundary and add out3; seq=1 is evicted and seq=2,3 remain
        completed_seqs.add(1)
        r3 = index.add(out3, self.stream_token)
        self.assertEqual(r3.committed_state_sha256, HASH_STATE_POST_2)
        self.assertIsNone(index.get_by_correlation(self.stream_token, 1))
        self.assertIsNotNone(index.get_by_correlation(self.stream_token, 2))
        self.assertIsNotNone(index.get_by_correlation(self.stream_token, 3))

    def test_conflicting_duplicate_refusal_causes_no_eviction(self):
        """Conflicting duplicate fails without evicting any live or settled entries."""
        index = OutcomeIndex(capacity=2)
        outcome1 = {
            "seq": 1,
            "requested_tick": 1,
            "applied_tick": 1,
            "order_seq_id": 1,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }
        index.add(
            attempt_id="att_settled",
            action_id="act_1",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
            local_status="admitted",
            outcome=outcome1,
        )
        index.add(
            attempt_id="att_live",
            action_id="act_2",
            stream_ref=self.stream_token,
            source_ticket=self.ticket,
            command={"tick": 2, "seq": 2, "cmd": "pause", "args": {}},
            local_status="admitted",
        )

        # Attempt conflicting duplicate on att_settled
        with self.assertRaises(ConflictingDuplicateError):
            index.add(
                attempt_id="att_settled",
                action_id="act_1",
                stream_ref=self.stream_token,
                source_ticket=self.ticket,
                command={"tick": 999, "seq": 1, "cmd": "pause", "args": {}},
                local_status="admitted",
            )

        # Neither att_settled nor att_live should be evicted
        self.assertIn("att_settled", index)
        self.assertIn("att_live", index)
        self.assertEqual(len(index), 2)


class TestBackendNeutralityAndSourcePolicies(unittest.TestCase):
    """Verify backend neutrality across Godot and CDP policies via fixtures and real SessionRegistry."""

    def test_godot_policy_fixture_with_session_registry(self):
        """Exercise Godot policy fixture with SessionRegistry: pause command and Holdings outcome records."""
        registry = SessionRegistry(clock=lambda: 1000)
        godot_handle = registry.register("godot_window", "godot_app_0", object(), owned=True)
        self.assertEqual(godot_handle.generation, 1)
        self.assertIn(godot_handle.kind, SUPPORTED_KINDS)
        epoch = registry.advance_epoch(godot_handle)
        godot_stream = StreamToken(handle=godot_handle, epoch=epoch)
        godot_ticket = Ticket(action_id="godot_act_1", handle=godot_handle)

        validated_calls = []

        def custom_outcome_validator(out):
            validated_calls.append(out)
            return validate_outcome(out)

        index = OutcomeIndex(
            capacity=5,
            validator_boundary=custom_outcome_validator,
            stream_authenticator=registry,
        )

        cmd = {"tick": 100, "seq": 1, "cmd": "pause", "args": {}}
        outcome = {
            "seq": 1,
            "requested_tick": 100,
            "applied_tick": 100,
            "order_seq_id": 500,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_1,
        }

        att = index.add(
            attempt_id="att_godot_1",
            action_id="godot_act_1",
            stream_ref=godot_stream,
            source_ticket=godot_ticket,
            command=cmd,
            local_status="admitted",
            outcome=outcome,
        )

        self.assertEqual(len(validated_calls), 1)
        self.assertEqual(att.command.cmd, "pause")
        self.assertEqual(att.phase, PHASE_ACCEPTED)
        self.assertEqual(att.committed_state_sha256, HASH_STATE_POST_1)

    def test_cdp_policy_fixture_with_session_registry(self):
        """Exercise CDP policy fixture with SessionRegistry: click/evaluate command and browser outcome records."""
        registry = SessionRegistry(clock=lambda: 2000)
        cdp_handle = registry.register("cdp_tab", "tab_target_abc", object(), owned=True)
        self.assertEqual(cdp_handle.generation, 1)
        self.assertIn(cdp_handle.kind, SUPPORTED_KINDS)
        cdp_stream = StreamToken(handle=cdp_handle, epoch=0)
        cdp_ticket = Ticket(action_id="cdp_act_1", handle=cdp_handle)

        index = OutcomeIndex(
            capacity=5,
            stream_authenticator=registry,
        )

        cdp_cmd = {
            "tick": 20,
            "seq": 1,
            "cmd": "click",
            "args": {"node_id": "n_submit_btn", "x": 100, "y": 200},
        }
        cdp_outcome = {
            "seq": 1,
            "requested_tick": 20,
            "applied_tick": 20,
            "order_seq_id": 700,
            "status": "accepted",
            "reason": None,
            "state_sha256": HASH_STATE_POST_2,
        }

        att = index.add(
            attempt_id="att_cdp_1",
            action_id="cdp_act_1",
            stream_ref=cdp_stream,
            source_ticket=cdp_ticket,
            command=cdp_cmd,
            local_status="admitted",
            outcome=cdp_outcome,
        )

        self.assertEqual(att.command.cmd, "click")
        self.assertEqual(att.phase, PHASE_ACCEPTED)
        self.assertEqual(att.committed_state_sha256, HASH_STATE_POST_2)

    def test_unauthenticated_stream_ref_refuses_addition(self):
        """Unauthenticated or stale stream reference raises UnauthenticatedStreamError using declared Handle kinds."""
        registry = SessionRegistry(clock=lambda: 3000)
        active_handle = registry.register("godot_window", "godot_win_auth", object(), owned=True)
        registry.advance_epoch(active_handle)  # active epoch is now 1

        # 1. Valid Handle kind ('godot_window') and generation=1, but stale epoch=0
        stale_epoch_token = StreamToken(handle=active_handle, epoch=0)
        ticket = Ticket(action_id="act_bad", handle=active_handle)

        index_with_registry = OutcomeIndex(
            capacity=5,
            stream_authenticator=registry,
        )
        with self.assertRaises(UnauthenticatedStreamError):
            index_with_registry.add(
                attempt_id="att_unauth_epoch",
                action_id="act_bad",
                stream_ref=stale_epoch_token,
                source_ticket=ticket,
                command={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
                local_status="admitted",
            )

        # 2. Unregistered Handle with valid kind ('cdp_tab') and generation=1
        unregistered_handle = Handle(kind="cdp_tab", canonical_id="rogue_tab", generation=1)
        unregistered_token = StreamToken(handle=unregistered_handle, epoch=0)
        unregistered_ticket = Ticket(action_id="act_unreg", handle=unregistered_handle)
        with self.assertRaises(UnauthenticatedStreamError):
            index_with_registry.add(
                attempt_id="att_unauth_unreg",
                action_id="act_unreg",
                stream_ref=unregistered_token,
                source_ticket=unregistered_ticket,
                command={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
                local_status="admitted",
            )

        # 3. Callable stream_authenticator rejecting epoch == 0 on valid Handle kind
        index_with_callable = OutcomeIndex(
            capacity=5,
            stream_authenticator=lambda s: getattr(s, "epoch", 0) > 0,
        )
        with self.assertRaises(UnauthenticatedStreamError):
            index_with_callable.add(
                attempt_id="att_unauth_callable",
                action_id="act_bad",
                stream_ref=stale_epoch_token,
                source_ticket=ticket,
                command={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
                local_status="admitted",
            )

        # 4. Also verify SessionRegistry.Handle itself rejects unsupported target kinds and generation < 1
        with self.assertRaises(RegistryError):
            Handle(kind="unauthenticated", canonical_id="rogue", generation=1)
        with self.assertRaises(RegistryError):
            Handle(kind="godot_window", canonical_id="rogue", generation=0)

    def test_in_memory_write_spy_no_frame_leakage(self):
        """Verify numpy frames in RAM remain volatile with no disk/encoded artifact leakage."""
        frame_rgb = np.zeros((64, 64, 3), dtype=np.uint8)
        h = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        frame_ref = FrameRef.from_rgb(h, epoch=1, frame_seq=0, rgb=frame_rgb)
        obs = Observation(
            handle=h,
            epoch=1,
            frame_seq=0,
            dpr_ppm=1000000,
            state={"schema": "wender.state/v1"},
            frame=frame_ref,
        )
        receipt = Receipt(action_id="act_1", handle=h, status="accepted", reason=None)
        spec = StreamSpec(width=64, height=64, slots=2, quota_bytes=64 * 64 * 3 * 2)
        self.assertEqual(len(frame_ref.digest), 64)
        self.assertEqual(obs.frame.digest, frame_ref.digest)
        self.assertEqual(receipt.status, "accepted")
        self.assertEqual(spec.slots, 2)

        open_calls = []
        real_open = builtins.open

        def write_spy(file, mode="r", *args, **kwargs):
            if any(m in str(mode) for m in ("w", "a", "x", "+")):
                open_calls.append((file, mode))
                raise AssertionError(f"Forbidden disk write attempted: {file!r} ({mode!r})")
            return real_open(file, mode, *args, **kwargs)

        builtins.open = write_spy
        try:
            index = OutcomeIndex(capacity=5)
            st = StreamToken(handle=h, epoch=1)
            tk = Ticket(action_id="act_1", handle=h)

            cmd = {"tick": 1, "seq": 1, "cmd": "pause", "args": {"frame_digest": frame_ref.digest}}
            att = index.add(
                attempt_id="att_ram_test",
                action_id="act_1",
                stream_ref=st,
                source_ticket=tk,
                command=cmd,
                local_status="admitted",
            )
            self.assertIn("frame_digest", att.command.args)
            dumped = dumps_canonical(att)
            self.assertIn(frame_ref.digest, dumped)
            self.assertEqual(open_calls, [])
        finally:
            builtins.open = real_open

    def test_owner_mutations_disagree_with_healthy_suite(self):
        """Verify named mutations (duplicate key, float, conflicting duplicate) disagree when owner is mutated."""
        # Mutation 1: disable duplicate-key check in _strict_pairs_hook
        orig_hook = t2_mod._strict_pairs_hook
        try:
            t2_mod._strict_pairs_hook = lambda pairs: dict(pairs)
            with self.assertRaises(AssertionError):
                self.assertRaises(
                    CanonicalFramingError,
                    parse_canonical_json,
                    '{"tick": 10, "seq": 1, "cmd": "pause", "seq": 2, "args": {}}',
                )
        finally:
            t2_mod._strict_pairs_hook = orig_hook

        # Mutation 2: disable float check in _ensure_no_float
        orig_no_float = t2_mod._ensure_no_float
        try:
            t2_mod._ensure_no_float = lambda obj, path="$": None
            with self.assertRaises(AssertionError):
                self.assertRaises(
                    CanonicalFramingError,
                    validate_command,
                    {"tick": 1, "seq": 1, "cmd": "pause", "args": {"speed": 2.0}},
                )
        finally:
            t2_mod._ensure_no_float = orig_no_float

        # Mutation 3: make _records_equal always True so conflicting duplicates are treated as idempotent
        orig_eq = t2_mod._records_equal
        try:
            t2_mod._records_equal = lambda a, b: True
            idx = OutcomeIndex(capacity=5)
            h = Handle(kind="godot_window", canonical_id="win_mut", generation=1)
            st = StreamToken(handle=h, epoch=1)
            tk = Ticket(action_id="act_mut", handle=h)
            idx.add(
                attempt_id="att_mut",
                action_id="act_mut",
                stream_ref=st,
                source_ticket=tk,
                command={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
                local_status="admitted",
            )
            with self.assertRaises(AssertionError):
                self.assertRaises(
                    ConflictingDuplicateError,
                    idx.add,
                    attempt_id="att_mut",
                    action_id="act_mut",
                    stream_ref=st,
                    source_ticket=tk,
                    command={"tick": 99, "seq": 1, "cmd": "pause", "args": {}},
                    local_status="admitted",
                )
        finally:
            t2_mod._records_equal = orig_eq


if __name__ == "__main__":
    unittest.main()
