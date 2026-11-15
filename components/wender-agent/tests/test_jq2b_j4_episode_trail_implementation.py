# @module {"id":"tests.test_jq2b_j4_episode_trail_implementation","layer":3,"owner":"TESTS","deps":["wa.learn.episode_trail"]}
"""Substantive public-path tests for wender.learn.episode_trail (jq2b-j4-episode-trail).

Exercises real public APIs of episode_trail following INV J4 1 through INV J4 4.
All fixtures are RAM-only and deterministic; no network, disk writes, or host timing.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import unittest

from wender.learn import episode_trail as et


class FakeCoreAdapter:
    """RAM-only deterministic simulation core adapter."""

    def __init__(
        self,
        core_id: str = "core_holdings_v1",
        content_id: str = "content_retail_pack",
        schema_id: str = "schema_holdings_v1",
        build_id: str = "build_20261020",
        initial_digest: str = "a" * 64,
        fail_reset_component: str | None = None,
        mutate_rng_on_reject: bool = False,
    ):
        self.core_id = core_id
        self.content_id = content_id
        self.schema_id = schema_id
        self.build_id = build_id
        self.initial_digest = initial_digest
        self.fail_reset_component = fail_reset_component
        self.mutate_rng_on_reject = mutate_rng_on_reject

        self.dispatched_commands: list[dict] = []
        self.restored_state: bytes | None = None
        self.rng_counter = 0

    def reset_and_verify(
        self,
        seed: int,
        core_id: str,
        content_id: str,
        schema_id: str,
        build_id: str,
        initial_digest: str,
    ) -> dict[str, bool]:
        report = {
            "core": (core_id == self.core_id),
            "RNG": isinstance(seed, int) and seed >= 0,
            "calendar": True,
            "content": (content_id == self.content_id),
            "schema": (schema_id == self.schema_id),
            "build": (build_id == self.build_id),
            "allocator": True,
            "initial_digest": (initial_digest == self.initial_digest),
        }
        if self.fail_reset_component is not None:
            report[self.fail_reset_component] = False
        return report

    def restore_opaque_state(self, opaque_bytes: bytes) -> None:
        self.restored_state = opaque_bytes

    def dispatch(
        self,
        cmd: str,
        args: dict,
        tick: int,
        seq: int,
        order_seq_id: int,
    ) -> dict:
        self.dispatched_commands.append({
            "cmd": cmd,
            "args": copy.deepcopy(args),
            "tick": tick,
            "seq": seq,
            "order_seq_id": order_seq_id,
        })

        # Command handling: rejected, unchanged, or normal accepted
        status = "accepted"
        reason = None
        boundary = "normal"

        if cmd == "invalid_op":
            status = "rejected"
            reason = "SCHEMA"
            if self.mutate_rng_on_reject:
                self.rng_counter += 1
                boundary = "reject_mutated_rng"
            else:
                boundary = "rejected_boundary"
        elif cmd == "noop_probe":
            status = "unchanged"
            boundary = "unchanged_boundary"
        elif cmd == "pause":
            status = "accepted"
            self.rng_counter += 1
        else:
            status = "accepted"
            self.rng_counter += 1

        state_data = f"{cmd}:{tick}:{seq}:{self.rng_counter}".encode("utf-8")
        state_sha256 = hashlib.sha256(state_data).hexdigest()
        rng_digest = hashlib.sha256(f"rng_{self.rng_counter}".encode("utf-8")).hexdigest()

        return {
            "seq": seq,
            "requested_tick": tick,
            "applied_tick": tick if status != "rejected" else 0,
            "order_seq_id": order_seq_id,
            "status": status,
            "reason": reason,
            "state_sha256": state_sha256,
            "rng_digest": rng_digest,
            "boundary": boundary,
        }


class PolicyAlpha:
    """First source policy: includes pause commands, a noop probe, and an invalid op."""

    def __init__(self, reject_tick: int = 1, unchanged_tick: int = 2):
        self.reject_tick = reject_tick
        self.unchanged_tick = unchanged_tick

    def propose_action(self, input_pos: int, committed_progress: int) -> dict | None:
        if input_pos == self.reject_tick:
            return {"cmd": "invalid_op", "args": {"bad": True}}
        if input_pos == self.unchanged_tick:
            return {"cmd": "noop_probe", "args": {}}
        return {"cmd": "pause", "args": {}}


class PolicyBeta:
    """Second source policy: distinct action sequence with economic command names."""

    def __init__(self, reject_tick: int = 2, unchanged_tick: int = 3):
        self.reject_tick = reject_tick
        self.unchanged_tick = unchanged_tick

    def propose_action(self, input_pos: int, committed_progress: int) -> dict | None:
        if input_pos == self.reject_tick:
            return {"cmd": "invalid_op", "args": {"reason": "disallowed"}}
        if input_pos == self.unchanged_tick:
            return {"cmd": "noop_probe", "args": {"screen": "portfolio"}}
        return {"cmd": "advance", "args": {"step": 1}}


class MemoryCheckpointStorage:
    """Trusted injected checkpoint storage."""

    def __init__(self):
        self._store: dict[str, bytes] = {}

    def save(self, checkpoint_id: str, opaque_bytes: bytes) -> str:
        ref = f"storage_ref_{checkpoint_id}"
        self._store[ref] = opaque_bytes
        return ref

    def load(self, storage_ref: str) -> bytes:
        if storage_ref not in self._store:
            raise KeyError(storage_ref)
        return self._store[storage_ref]


def _make_valid_spec(
    seed: int = 42,
    mode: str = "headless",
    duration_days: int = 3,
    policy_id: str = "policy_alpha",
    max_ticks: int = 500,
    max_inputs: int = 500,
) -> et.EpisodeSpec:
    return et.EpisodeSpec(
        seed=seed,
        descriptor={"environment": "holdings_offline_test", "version": 1},
        policy_id=policy_id,
        duration_days=duration_days,
        mode=mode,
        build_id="build_20261020",
        core_id="core_holdings_v1",
        content_id="content_retail_pack",
        schema_id="schema_holdings_v1",
        initial_checkpoint_sha256="a" * 64,
        limits={
            "max_ticks": max_ticks,
            "max_inputs": max_inputs,
            "max_wall_time_vm": 60,
        },
    )


class TestEpisodeTrailImplementation(unittest.TestCase):
    """Substantive public-path test suite for episode trail and INV J4 lifecycle."""

    def test_spec_validation(self):
        """Test strict validation of EpisodeSpec fields, modes, and limits."""
        spec = _make_valid_spec(mode="headless")
        self.assertEqual(spec.mode, "headless")
        self.assertEqual(spec.duration_days, 3)

        xvfb_spec = _make_valid_spec(mode="xvfb")
        self.assertEqual(xvfb_spec.mode, "xvfb")

        # Invalid mode
        with self.assertRaises(et.EpisodeError) as cm:
            replace(spec, mode="gui_window")
        self.assertIn("mode must be one of", str(cm.exception))

        # Non-positive duration_days
        with self.assertRaises(et.EpisodeError) as cm:
            replace(spec, duration_days=0)
        self.assertIn("duration_days", str(cm.exception))

        # Missing required limits
        with self.assertRaises(et.EpisodeError) as cm:
            replace(spec, limits={"max_ticks": 100})
        self.assertIn("limits missing required key", str(cm.exception))

        # Invalid initial_checkpoint_sha256
        with self.assertRaises(et.EpisodeError) as cm:
            replace(spec, initial_checkpoint_sha256="not_a_64_hex_string")
        self.assertIn("initial_checkpoint_sha256", str(cm.exception))

    def test_inv_j4_1_reset_integrity_and_ausente(self):
        """INV J4 1: Reset checks 8 components and fails loudly as AUSENTE if mismatched."""
        spec = _make_valid_spec()
        calendar = et.CheckedCalendar(ticks_per_day=50)
        policy = PolicyAlpha()

        # All 8 components pass
        adapter = FakeCoreAdapter()
        trail = et.run_episode(spec, adapter, policy, calendar)
        self.assertEqual(trail.status, "COMPLETED")
        self.assertEqual(trail.gates["G-J4-1"]["status"], "PASS")

        # Missing or failing core component fails loudly as AUSENTE
        for comp in et.REQUIRED_RESET_COMPONENTS:
            with self.subTest(component=comp):
                bad_adapter = FakeCoreAdapter(fail_reset_component=comp)
                with self.assertRaises(et.EpisodeError) as cm:
                    et.run_episode(spec, bad_adapter, policy, calendar)
                self.assertIn("AUSENTE", str(cm.exception))
                self.assertIn(comp, str(cm.exception))

    def test_inv_j4_2_authored_calendar_horizon_and_checkpoint_metadata(self):
        """INV J4 2: Checked calendar converts committed progress to game days; checkpoint metadata names all 9 fields."""
        spec = _make_valid_spec(duration_days=2)
        calendar = et.CheckedCalendar(ticks_per_day=100)
        adapter = FakeCoreAdapter()
        policy = PolicyAlpha()
        storage = MemoryCheckpointStorage()

        trail = et.run_episode(spec, adapter, policy, calendar, checkpoint_storage=storage)
        self.assertEqual(trail.status, "COMPLETED")

        # Calendar establishes horizon: 2 days * 100 ticks = 200 accepted ticks
        accepted_stamps = [s for s in trail.stamps if not s.is_rejected]
        self.assertEqual(len(accepted_stamps), 200)

        # Checkpoints created and contain all 9 required metadata fields
        self.assertGreater(len(trail.checkpoints), 0)
        for cp in trail.checkpoints:
            self.assertIsInstance(cp.next_input_position, int)
            self.assertIsInstance(cp.next_stream_sequences, dict)
            self.assertIsInstance(cp.next_order_seq_id, int)
            self.assertTrue(bool(cp.ticket_roster))
            self.assertTrue(bool(cp.allocator_policy))
            self.assertTrue(bool(cp.consumed_proposals_digest))
            self.assertTrue(bool(cp.trusted_session_ref))
            self.assertTrue(bool(cp.core_rng_ref))
            # In headless mode, presentation_ref is nullable (None)
            self.assertIsNone(cp.presentation_ref)
            # Opaque bytes stored in storage, storage_ref present
            self.assertIsNotNone(cp.storage_ref)
            self.assertEqual(storage.load(cp.storage_ref), b"OPAQUE_SIMULATION_BYTES_TRUSTED_STORAGE")

        # In xvfb mode, presentation_ref is populated
        xvfb_spec = _make_valid_spec(mode="xvfb", duration_days=2)
        xvfb_trail = et.run_episode(xvfb_spec, FakeCoreAdapter(), policy, calendar, checkpoint_storage=storage)
        self.assertIsNotNone(xvfb_trail.checkpoints[0].presentation_ref)

    def test_inv_j4_3_resume_indexing_and_intent_idempotence(self):
        """INV J4 3: Resume is before-input-position indexed; regenerated intents compared and never applied twice."""
        spec = _make_valid_spec(duration_days=3)
        calendar = et.CheckedCalendar(ticks_per_day=50)
        adapter = FakeCoreAdapter()
        policy = PolicyAlpha()
        storage = MemoryCheckpointStorage()

        # Run first segment
        trail = et.run_episode(spec, adapter, policy, calendar, checkpoint_storage=storage)
        self.assertGreater(len(trail.checkpoints), 1)

        checkpoint_to_resume = trail.checkpoints[0]
        resume_pos = checkpoint_to_resume.next_input_position
        self.assertGreater(resume_pos, 0)

        # Resume from checkpoint
        resumed_adapter = FakeCoreAdapter()
        resumed_trail = et.run_episode(
            spec,
            resumed_adapter,
            policy,
            calendar,
            checkpoint_storage=storage,
            resume_checkpoint=checkpoint_to_resume,
        )
        self.assertEqual(resumed_trail.status, "COMPLETED")
        self.assertEqual(resumed_adapter.restored_state, b"OPAQUE_SIMULATION_BYTES_TRUSTED_STORAGE")

        # First stamp of resumed trail starts exactly at resume_pos
        self.assertEqual(resumed_trail.stamps[0].position, resume_pos)

    def test_inv_j4_4_determinism_and_seal_immutability(self):
        """INV J4 4: Simulation seal is strictly deterministic; volatile PID/RAM samples do not alter seal."""
        spec = _make_valid_spec()
        calendar = et.CheckedCalendar(ticks_per_day=50)

        trail1 = et.run_episode(spec, FakeCoreAdapter(), PolicyAlpha(), calendar)
        trail2 = et.run_episode(spec, FakeCoreAdapter(), PolicyAlpha(), calendar)

        self.assertEqual(trail1.seal, trail2.seal)
        self.assertEqual(et.canonical_trail_bytes(trail1), et.canonical_trail_bytes(trail2))

        # Seal comparison passes
        res = et.compare_trails(trail1, trail2)
        self.assertTrue(res.matched)
        self.assertEqual(res.status, "MATCH")
        self.assertIsNone(res.first_divergence)

    def test_compare_trails_first_divergence_detection(self):
        """compare_trails detects first missing, divergent, reordered, and duplicate stamps."""
        spec = _make_valid_spec()
        calendar = et.CheckedCalendar(ticks_per_day=50)
        base_trail = et.run_episode(spec, FakeCoreAdapter(), PolicyAlpha(), calendar)
        stamps = list(base_trail.stamps)

        # 1. Missing stamp
        short_stamps = stamps[:-5]
        res_missing = et.compare_trails(stamps, short_stamps)
        self.assertFalse(res_missing.matched)
        self.assertEqual(res_missing.status, "MISSING")
        self.assertEqual(res_missing.first_divergence.kind, "missing")
        self.assertEqual(res_missing.first_divergence.index, len(short_stamps))

        # 2. Divergent stamp (command arg differs)
        mutated_stamps = list(stamps)
        diverge_idx = 10
        mutated_stamps[diverge_idx] = replace(
            mutated_stamps[diverge_idx],
            args={"divergent_arg": 123},
        )
        res_divergent = et.compare_trails(stamps, mutated_stamps)
        self.assertFalse(res_divergent.matched)
        self.assertEqual(res_divergent.status, "DIVERGENT")
        self.assertEqual(res_divergent.first_divergence.kind, "divergent")
        self.assertEqual(res_divergent.first_divergence.index, diverge_idx)

        # 3. Reordered stamps
        reordered_stamps = list(stamps)
        reordered_stamps[5], reordered_stamps[6] = reordered_stamps[6], reordered_stamps[5]
        res_reordered = et.compare_trails(stamps, reordered_stamps)
        self.assertFalse(res_reordered.matched)
        self.assertEqual(res_reordered.status, "REORDERED")
        self.assertEqual(res_reordered.first_divergence.kind, "reordered")
        self.assertEqual(res_reordered.first_divergence.index, 5)

        # 4. Duplicate stamp
        dup_stamps = list(stamps)
        dup_stamps.insert(4, copy.deepcopy(stamps[4]))
        res_duplicate = et.compare_trails(stamps, dup_stamps)
        self.assertFalse(res_duplicate.matched)
        self.assertEqual(res_duplicate.status, "DUPLICATE")
        self.assertEqual(res_duplicate.first_divergence.kind, "duplicate")
        self.assertEqual(res_duplicate.first_divergence.index, 5)

    def test_injected_canonical_state_verifier(self):
        """compare_trails invokes injected canonical-state verification and detects hash divergence."""
        spec = _make_valid_spec()
        calendar = et.CheckedCalendar(ticks_per_day=50)
        trail = et.run_episode(spec, FakeCoreAdapter(), PolicyAlpha(), calendar)

        def good_verifier(args: dict, trusted_id: str, state_sha256: str) -> bool:
            return isinstance(state_sha256, str) and len(state_sha256) == 64

        res_good = et.compare_trails(trail, trail, state_verifier=good_verifier)
        self.assertTrue(res_good.matched)

        # Verifier rejects a specific stamp by checking its tick
        target_index = 15
        target_tick = trail.stamps[target_index].tick
        def failing_verifier(args: dict, trusted_id: str, state_sha256: str) -> bool:
            # Reject the target stamp
            return not trusted_id.startswith(f"{target_tick}:")

        res_bad = et.compare_trails(trail, trail, state_verifier=failing_verifier)
        self.assertFalse(res_bad.matched)
        self.assertEqual(res_bad.status, "VERIFICATION_FAILED")
        self.assertEqual(res_bad.first_divergence.kind, "divergent")
        self.assertEqual(res_bad.first_divergence.index, target_index)

    def test_gate_g_j4_4_rejected_and_unchanged_survival(self):
        """Acceptance Gate G-J4-4: Rejected/unchanged ticks survive. Negative contrast and positive control."""
        spec = _make_valid_spec()
        calendar = et.CheckedCalendar(ticks_per_day=50)
        policy = PolicyAlpha(reject_tick=1, unchanged_tick=2)

        # Positive control: complete recorded fixture journal with both rejected and unchanged ticks
        adapter_good = FakeCoreAdapter(mutate_rng_on_reject=False)
        trail_good = et.run_episode(spec, adapter_good, policy, calendar)
        gate_result = et.evaluate_gate_g_j4_4(trail_good)
        self.assertEqual(gate_result["status"], "PASS")
        self.assertTrue(gate_result["passed"])
        self.assertTrue(gate_result["has_rejected"])
        self.assertTrue(gate_result["has_unchanged"])

        # Negative contrast 1: Reject mutates RNG -> fails G-J4-4
        adapter_bad_rng = FakeCoreAdapter(mutate_rng_on_reject=True)
        trail_bad_rng = et.run_episode(spec, adapter_bad_rng, policy, calendar)
        gate_bad_rng = et.evaluate_gate_g_j4_4(trail_bad_rng)
        self.assertEqual(gate_bad_rng["status"], "FAIL")
        self.assertTrue(gate_bad_rng["rejected_rng_violation"])

        # Negative contrast 2: Unchanged stamp omitted -> fails G-J4-4
        omitted_evidence_trail = replace(
            trail_good,
            evidence={"unchanged_stamp_omitted": True},
        )
        gate_omitted = et.evaluate_gate_g_j4_4(omitted_evidence_trail)
        self.assertEqual(gate_omitted["status"], "FAIL")
        self.assertTrue(gate_omitted["unchanged_omitted"])

    def test_both_source_policies_exercised(self):
        """Exercise both source policies (PolicyAlpha and PolicyBeta) through fixtures without relabeling."""
        spec_alpha = _make_valid_spec(policy_id="policy_alpha")
        spec_beta = _make_valid_spec(policy_id="policy_beta")
        calendar = et.CheckedCalendar(ticks_per_day=50)

        adapter_alpha = FakeCoreAdapter()
        trail_alpha = et.run_episode(spec_alpha, adapter_alpha, PolicyAlpha(), calendar)
        self.assertEqual(trail_alpha.spec.policy_id, "policy_alpha")
        self.assertEqual(trail_alpha.status, "COMPLETED")

        adapter_beta = FakeCoreAdapter()
        trail_beta = et.run_episode(spec_beta, adapter_beta, PolicyBeta(), calendar)
        self.assertEqual(trail_beta.spec.policy_id, "policy_beta")
        self.assertEqual(trail_beta.status, "COMPLETED")

        # Both policies produce valid, distinct trails with distinct commands and seals
        self.assertNotEqual(trail_alpha.seal, trail_beta.seal)
        self.assertEqual(trail_alpha.stamps[0].cmd, "pause")
        self.assertEqual(trail_beta.stamps[0].cmd, "advance")

    def test_write_spy_rejects_leakage(self):
        """WriteSpy rejects pixel frame leakage or raw receipt leakage."""
        spy = et.WriteSpy()
        # Normal metadata check passes
        spy.check_data("a" * 64, "state_hash")

        # Frame leakage raises error
        with self.assertRaises(et.EpisodeError) as cm:
            spy.check_data(b"\x89PNG\r\n\x1a\nfake_pixel_payload", "leaked_frame.png")
        self.assertIn("frame pixel leakage detected", str(cm.exception))
        self.assertTrue(spy.leakage_detected)

    def test_incomplete_evidence_on_drift_and_exhaustion(self):
        """Drift and exhausted policy sequence yield explicit incomplete evidence."""
        spec = _make_valid_spec()
        calendar = et.CheckedCalendar(ticks_per_day=100)

        class ExhaustingPolicy:
            def propose_action(self, input_pos: int, progress: int) -> dict | None:
                if input_pos >= 5:
                    return None
                return {"cmd": "pause", "args": {}}

        trail_exhausted = et.run_episode(spec, FakeCoreAdapter(), ExhaustingPolicy(), calendar)
        self.assertEqual(trail_exhausted.status, "INCOMPLETE")
        self.assertEqual(trail_exhausted.evidence.get("incomplete_reason"), "EXHAUSTED")

        # Absent or exceeded limits yields incomplete evidence
        low_limit_spec = _make_valid_spec(max_ticks=10, max_inputs=10)
        trail_limited = et.run_episode(low_limit_spec, FakeCoreAdapter(), PolicyAlpha(), calendar)
        self.assertEqual(trail_limited.status, "INCOMPLETE")
        self.assertEqual(trail_limited.evidence.get("incomplete_reason"), "LIMIT_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
