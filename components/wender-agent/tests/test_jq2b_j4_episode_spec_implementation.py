# @module {"id":"tests.test_jq2b_j4_episode_spec_implementation","layer":3,"owner":"TESTS","deps":["wa.learn.episode_spec"]}
"""Public-path tests for wender.learn.episode_spec.

Exercises episode specification validation, trusted checkpoint verification,
authored calendar horizon enforcement, before-input resume indexing,
and deterministic simulation seal calculation under INV J4 1 through INV J4 4.
"""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

import numpy as np

from wender.learn.episode_spec import (
    GATE_G_J4_1,
    INV_J4_1,
    INV_J4_2,
    INV_J4_3,
    INV_J4_4,
    AusenteError,
    CheckpointMetadata,
    CheckpointValidationError,
    EpisodeLifecycleError,
    EpisodeLimits,
    EpisodeSession,
    EpisodeSpec,
    SpecValidationError,
    TrustedContext,
    WriteSpy,
    validate_checkpoint,
    validate_spec,
    verify_gate_g_j4_1,
)

INIT_CP_BYTES = b"initial_checkpoint_opaque_seed_state_bytes"
INIT_CP_SHA256 = hashlib.sha256(INIT_CP_BYTES).hexdigest()


class FakeCalendar:
    """Injected checked calendar converting committed progress into authored game days."""
    def __init__(self, ticks_per_day: int = 100):
        self.ticks_per_day = ticks_per_day

    def progress_to_days(self, committed_progress: int) -> int:
        if type(committed_progress) is not int or committed_progress < 0:
            raise ValueError(f"Committed progress must be non-negative integer, got {committed_progress!r}")
        return committed_progress // self.ticks_per_day


def _make_limits(
    max_ticks: int = 1000,
    max_actions: int = 50,
    max_orders: int = 100,
    max_streams: int = 10,
    max_wall_time_vm_s: int = 120,
) -> EpisodeLimits:
    return EpisodeLimits(
        max_ticks=max_ticks,
        max_actions=max_actions,
        max_orders=max_orders,
        max_streams=max_streams,
        max_wall_time_vm_s=max_wall_time_vm_s,
    )


def _make_spec(
    seed: int = 42,
    descriptor: dict | None = None,
    policy_id: str = "policy-holdings-godot-v1",
    duration_days: int = 7,
    mode: str = "headless",
    build_id: str = "build-2026-10-20-rev1",
    core_id: str = "core-wend-sim-01",
    content_id: str = "content-pack-alpha",
    schema_id: str = "schema-j4-v1",
    initial_checkpoint_sha256: str = INIT_CP_SHA256,
    limits: EpisodeLimits | dict | None = None,
) -> EpisodeSpec:
    desc = descriptor if descriptor is not None else {
        "scenario": "retail-migration",
        "authored_horizon_days": duration_days,
        "configured_limits": {"max_ticks": 1000, "max_actions": 50},
    }
    lims = limits if limits is not None else _make_limits()
    return EpisodeSpec(
        seed=seed,
        descriptor=desc,
        policy_id=policy_id,
        duration_days=duration_days,
        mode=mode,
        build_id=build_id,
        core_id=core_id,
        content_id=content_id,
        schema_id=schema_id,
        initial_checkpoint_sha256=initial_checkpoint_sha256,
        limits=lims,
    )


def _make_context(
    spec: EpisodeSpec,
    calendar: FakeCalendar | None = None,
    opaque_bytes: bytes = INIT_CP_BYTES,
    prerequisites: dict | None = None,
) -> TrustedContext:
    cal = calendar if calendar is not None else FakeCalendar(ticks_per_day=100)
    storage = {}
    if opaque_bytes is not None:
        cp_sha = hashlib.sha256(opaque_bytes).hexdigest()
        storage[cp_sha] = opaque_bytes
    return TrustedContext(
        core_id=spec.core_id,
        core_rng_ref="rng-seed-state-42",
        calendar=cal,
        content_id=spec.content_id,
        schema_id=spec.schema_id,
        build_id=spec.build_id,
        allocator_policy="allocator-deterministic-fifo",
        initial_checkpoint_sha256=spec.initial_checkpoint_sha256,
        trusted_session_ref="session-trusted-001",
        seed=spec.seed,
        trusted_storage=storage,
        prerequisites=prerequisites if prerequisites is not None else {
            "G-M1-1": {"status": "PASS", "evidence": "merkle_root_sealed", "expected_failure": False}
        },
    )


class TestEpisodeSpecImplementation(unittest.TestCase):
    """Substantive test coverage for episode specification production owner."""

    def test_gate_g_j4_1_positive_control(self):
        """Positive control: pinned descriptor and valid reset digest passes Gate G-J4-1."""
        spec = _make_spec()
        ctx = _make_context(spec)
        gate_res = verify_gate_g_j4_1(spec, ctx)
        self.assertTrue(gate_res["passed"], f"Gate failed: {gate_res}")
        self.assertEqual(gate_res["gate"], GATE_G_J4_1)
        self.assertEqual(gate_res["evidence"], "PASS")
        self.assertEqual(gate_res["spec_core_id"], spec.core_id)
        self.assertEqual(gate_res["checkpoint_sha256"], spec.initial_checkpoint_sha256)
        self.assertRegex(gate_res["reset_digest"], r"^[0-9a-f]{64}$")

    def test_gate_g_j4_1_negative_contrast_seed_drift(self):
        """Negative contrast: seed drift between spec and execution context fails Gate G-J4-1."""
        spec = _make_spec(seed=42)
        ctx = _make_context(spec)
        ctx.seed = 999  # Seed mutated/drifted in execution context
        gate_res = verify_gate_g_j4_1(spec, ctx)
        self.assertFalse(gate_res["passed"])
        self.assertEqual(gate_res["reason"], "seed_drift")
        self.assertEqual(gate_res["evidence"], "REJECTED")

    def test_gate_g_j4_1_negative_contrast_build_drift(self):
        """Negative contrast: build ID drift fails Gate G-J4-1."""
        spec = _make_spec(build_id="build-alpha")
        ctx = _make_context(spec)
        ctx.build_id = "build-beta"  # Mutated build ID
        gate_res = verify_gate_g_j4_1(spec, ctx)
        self.assertFalse(gate_res["passed"])
        self.assertEqual(gate_res["reason"], "build_drift")
        self.assertEqual(gate_res["evidence"], "REJECTED")

    def test_gate_g_j4_1_negative_contrast_absent_calendar_conversion(self):
        """Negative contrast: absent calendar conversion fails validate_spec and Gate G-J4-1."""
        spec = _make_spec()
        ctx = _make_context(spec)
        ctx.calendar = None  # Calendar absent
        gate_res = verify_gate_g_j4_1(spec, ctx)
        self.assertFalse(gate_res["passed"])
        self.assertIn("spec_validation_failure", gate_res["reason"])
        self.assertEqual(gate_res["evidence"], "REJECTED")

    def test_inv_j4_1_reset_authentication_checks_all_factors(self):
        """INV J4 1: Reset checks core, RNG, calendar, content, schema, build, allocator, digest."""
        spec = _make_spec()
        ctx = _make_context(spec)
        session = EpisodeSession(spec, ctx)
        res = session.authenticate_reset()
        self.assertTrue(res["authenticated"])
        self.assertEqual(res["status"], "RESET")
        self.assertEqual(res["next_input_position"], 0)

        # A seed alone or a raw JSON context does not authenticate a reset
        raw_json_context = {"seed": spec.seed}
        with self.assertRaises(SpecValidationError):
            validate_spec(raw_json_context, calendar=None)

        # Core ID drift fails reset authentication
        spec_core_drift = _make_spec(core_id="corrupt-core")
        session_drift = EpisodeSession(spec_core_drift, ctx)
        with self.assertRaises(SpecValidationError) as cm:
            session_drift.authenticate_reset()
        self.assertIn("core_id drift", str(cm.exception))

        # Schema ID drift fails reset authentication
        spec_schema_drift = _make_spec(schema_id="corrupt-schema")
        session_schema_drift = EpisodeSession(spec_schema_drift, ctx)
        with self.assertRaises(SpecValidationError) as cm:
            session_schema_drift.authenticate_reset()
        self.assertIn("schema_id drift", str(cm.exception))

    def test_inv_j4_1_mode_capabilities_headless_and_xvfb(self):
        """INV J4 1: Mode must be headless or xvfb; all others rejected."""
        cal = FakeCalendar()
        spec_headless = _make_spec(mode="headless")
        self.assertEqual(validate_spec(spec_headless, cal).mode, "headless")

        spec_xvfb = _make_spec(mode="xvfb")
        self.assertEqual(validate_spec(spec_xvfb, cal).mode, "xvfb")

        spec_invalid = _make_spec(mode="live-window")
        with self.assertRaises(SpecValidationError) as cm:
            validate_spec(spec_invalid, cal)
        self.assertIn("mode must be one of", str(cm.exception))

    def test_inv_j4_1_explicit_descriptor_limits_required(self):
        """INV J4 1: Configured limits are required authored targets, never inferred defaults."""
        cal = FakeCalendar()
        # Empty limits dict
        with self.assertRaises(SpecValidationError) as cm:
            validate_spec(_make_spec(limits={}), cal)
        self.assertIn("Explicit limits mapping is required", str(cm.exception))

        # Partial limits missing required fields
        with self.assertRaises(SpecValidationError) as cm:
            validate_spec(_make_spec(limits={"max_ticks": 100}), cal)
        self.assertIn("Missing required limit keys", str(cm.exception))

    def test_inv_j4_2_authored_calendar_horizon_converts_progress_to_game_days(self):
        """INV J4 2: Checked calendar converts progress into game days; host time/actions cannot."""
        cal = FakeCalendar(ticks_per_day=50)  # 50 ticks per authored day
        spec = _make_spec(duration_days=3)  # Horizon is 3 game days = 150 ticks
        ctx = _make_context(spec, calendar=cal)
        session = EpisodeSession(spec, ctx)
        session.authenticate_reset()

        adv1 = session.advance_progress(50)
        self.assertEqual(adv1["elapsed_days"], 1)
        self.assertFalse(adv1["horizon_reached"])

        adv2 = session.advance_progress(50)
        self.assertEqual(adv2["elapsed_days"], 2)
        self.assertFalse(adv2["horizon_reached"])

        adv3 = session.advance_progress(50)
        self.assertEqual(adv3["elapsed_days"], 3)
        self.assertTrue(adv3["horizon_reached"])
        self.assertEqual(session.status, "HORIZON_REACHED")

    def test_inv_j4_3_before_input_resume_indexing_and_deduplication(self):
        """INV J4 3: Resume is before-input-position indexed; ambiguous sent work is not resent.

        Regenerated intents are compared and never applied twice.
        """
        spec = _make_spec()
        ctx = _make_context(spec)
        session = EpisodeSession(spec, ctx)
        session.authenticate_reset()

        intent_0 = {"cmd": "pause", "seq": 1, "args": {}}
        out_0 = session.submit_intent(0, intent_0)
        self.assertTrue(out_0["applied"])
        self.assertEqual(out_0["input_position"], 0)
        self.assertEqual(out_0["next_input_position"], 1)

        # Submitting duplicate intent at already-applied position 0
        out_dup = session.submit_intent(0, intent_0)
        self.assertFalse(out_dup["applied"])
        self.assertEqual(out_dup["reason"], "already_applied_prior_work")

        # Submitting conflicting intent at prior position 0 raises drift error
        intent_drift = {"cmd": "resume", "seq": 1, "args": {}}
        with self.assertRaises(EpisodeLifecycleError) as cm:
            session.submit_intent(0, intent_drift)
        self.assertIn("Drift at input_position 0", str(cm.exception))

        # Sequence gap at future position raises gap error
        with self.assertRaises(EpisodeLifecycleError) as cm:
            session.submit_intent(5, {"cmd": "step"})
        self.assertIn("Sequence gap", str(cm.exception))

    def test_inv_j4_4_simulation_seal_isolated_from_host_metrics(self):
        """INV J4 4: PID, elapsed time and RAM samples never influence the simulation seal."""
        spec = _make_spec()
        ctx = _make_context(spec)

        session1 = EpisodeSession(spec, ctx)
        session1.authenticate_reset()
        session1.submit_intent(0, {"cmd": "pause", "seq": 1, "args": {}})
        session1.advance_progress(20)

        # Run 1 has host metrics: PID 100, 15.2s elapsed, 250MB RAM
        seal1 = session1.compute_simulation_seal(host_metrics={"pid": 100, "elapsed_s": 15.2, "ram_mb": 250})

        session2 = EpisodeSession(spec, ctx)
        session2.authenticate_reset()
        session2.submit_intent(0, {"cmd": "pause", "seq": 1, "args": {}})
        session2.advance_progress(20)

        # Run 2 has different host metrics: PID 9999, 120.5s elapsed, 4000MB RAM
        seal2 = session2.compute_simulation_seal(host_metrics={"pid": 9999, "elapsed_s": 120.5, "ram_mb": 4000})

        # Seals must match byte-for-byte despite completely different host telemetry
        self.assertEqual(seal1, seal2)
        self.assertRegex(seal1, r"^[0-9a-f]{64}$")

    def test_opaque_checkpoint_bytes_remain_in_trusted_storage(self):
        """Opaque checkpoint bytes remain in trusted injected storage, checked by hash identity."""
        raw_bytes = b"\x00\x01\x02\x03\x04opaque_simulation_memory_state"
        cp_hash = hashlib.sha256(raw_bytes).hexdigest()
        spec = _make_spec(initial_checkpoint_sha256=cp_hash)
        ctx = _make_context(spec, opaque_bytes=raw_bytes)
        ctx.trusted_storage = {cp_hash: raw_bytes}

        cp = CheckpointMetadata(
            next_input_position=10,
            next_stream_sequences={"stream_0": 5},
            next_order_seq_id=11,
            ticket_roster=["ticket-1", "ticket-2"],
            allocator_policy=ctx.allocator_policy,
            consumed_proposals_digest="b" * 64,
            trusted_session_ref=ctx.trusted_session_ref,
            core_rng_ref=ctx.core_rng_ref,
            presentation_ref=None,
            checkpoint_sha256=cp_hash,
        )
        validated = validate_checkpoint(cp, ctx)
        self.assertEqual(validated.checkpoint_sha256, cp_hash)

        # Tampered storage bytes fails validation
        ctx.trusted_storage[cp_hash] = b"tampered_bytes"
        with self.assertRaises(CheckpointValidationError) as cm:
            validate_checkpoint(cp, ctx)
        self.assertIn("Stored checkpoint bytes digest mismatch", str(cm.exception))

    def test_write_spy_rejects_pixel_and_receipt_leakage(self):
        """Write spy rejects pixel/receipt leakage, keeping raw frames and dumps volatile."""
        spy = WriteSpy()
        # Approved bounded metadata persists cleanly
        approved_meta = {"input_position": 1, "order_seq_id": 2, "status": "APPLIED"}
        spy.persist(approved_meta)
        self.assertEqual(len(spy.persisted_records), 1)

        # Pixel / frame leakage is rejected
        frame_in_ram = np.zeros((64, 64, 3), dtype=np.uint8)
        with self.assertRaises(RuntimeError) as cm:
            spy.persist({"pixels": frame_in_ram.tobytes()})
        self.assertIn("rejected forbidden leakage", str(cm.exception))
        self.assertTrue(spy.leak_detected)

        # Raw receipt or tree dump leakage is rejected
        with self.assertRaises(RuntimeError) as cm:
            spy.persist({"raw_receipt": {"trace": "cdp_dump"}})
        self.assertIn("rejected forbidden leakage", str(cm.exception))

    def test_source_policy_holdings_pause_and_outcomes(self):
        """Exercise Holdings source policy with pause command and engine outcome records inline."""
        spec = _make_spec(policy_id="policy-holdings-godot-v1")
        ctx = _make_context(spec)
        session = EpisodeSession(spec, ctx)
        session.authenticate_reset()

        # Offline Holdings pause command record
        pause_cmd = {
            "tick": 10,
            "seq": 1,
            "cmd": "pause",
            "args": {},
        }
        res = session.submit_intent(0, pause_cmd)
        self.assertTrue(res["applied"])

        # Offline Holdings outcome record
        outcome = {
            "seq": 1,
            "requested_tick": 10,
            "applied_tick": 10,
            "order_seq_id": 1,
            "status": "accepted",
            "reason": None,
            "state_sha256": "c" * 64,
        }
        self.assertEqual(outcome["status"], "accepted")
        self.assertEqual(outcome["seq"], 1)

    def test_source_policy_cdp_web_actions(self):
        """Exercise CDP source policy without relabeling as Holdings."""
        spec = _make_spec(policy_id="policy-cdp-web-v1")
        ctx = _make_context(spec)
        session = EpisodeSession(spec, ctx)
        session.authenticate_reset()

        cdp_action = {
            "path": "ax",
            "op": "click",
            "target": "button-01",
            "seq": 1,
        }
        res = session.submit_intent(0, cdp_action)
        self.assertTrue(res["applied"])
        self.assertEqual(session.spec.policy_id, "policy-cdp-web-v1")

    def test_missing_prerequisites_fail_loudly_as_ausente(self):
        """Missing prerequisites or evidence fail loudly as AUSENTE."""
        spec = _make_spec()
        ctx = _make_context(spec)
        # Prerequisite marked failed
        ctx.prerequisites = {
            "G-M1-1": {"status": "FAIL", "evidence": "AUSENTE", "expected_failure": False}
        }
        session = EpisodeSession(spec, ctx)
        with self.assertRaises(AusenteError) as cm:
            session.authenticate_reset()
        self.assertIn("AUSENTE", str(cm.exception))

        # Expected failure is pending contract, not accepted implementation evidence
        ctx.prerequisites = {
            "G-M1-1": {"status": "PASS", "evidence": "pending_contract", "expected_failure": True}
        }
        with self.assertRaises(AusenteError) as cm:
            session.authenticate_reset()
        self.assertIn("expected failure", str(cm.exception))

    def test_exhausted_sequence_or_limits_yields_incomplete_evidence(self):
        """Exhausted sequence or absent limits yields explicit incomplete evidence."""
        tight_limits = _make_limits(max_ticks=20, max_actions=1)
        spec = _make_spec(limits=tight_limits)
        ctx = _make_context(spec)
        session = EpisodeSession(spec, ctx)
        session.authenticate_reset()

        session.submit_intent(0, {"cmd": "pause", "seq": 1, "args": {}})
        # Second action exceeds max_actions=1
        with self.assertRaises(EpisodeLifecycleError) as cm:
            session.submit_intent(1, {"cmd": "step", "seq": 2, "args": {}})
        self.assertIn("Exhausted action limit", str(cm.exception))
        self.assertEqual(session.status, "INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
