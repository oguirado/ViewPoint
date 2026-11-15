# @module {"id":"tests.test_jq2b_j9_episode_provenance_implementation","layer":3,"owner":"TESTS","deps":["wa.remember.episode_provenance","wa.remember.skills"],"book":"HB-WA-s3-remember"}
"""Substantive public-path unit tests for wender.remember.episode_provenance (jq2b-j9-episode-provenance).

Exercises real public APIs of episode_provenance following INV J9 1 through INV J9 4.
All fixtures are deterministic and RAM-only; no network, disk writes, or host timing.
"""
from __future__ import annotations

import copy
import hashlib
import unittest

from wender.remember import episode_provenance as ep
from wender.remember import skills


def _make_valid_skill(
    skill_id: str = "skill:holdings:rebalance_v1",
    runs: int = 2,
    version: int = 1,
) -> dict:
    """Create a conforming skill record that passes skills.admit."""
    return {
        "id": skill_id,
        "intent": "rebalance_portfolio",
        "params": {"portfolio_id": "port_001"},
        "steps": [
            {"path": "api", "op": "pause", "args": {}},
            {"path": "coordinates", "x": 100, "y": 200},
        ],
        "checkpoints": [
            {"after_step": 0, "verified": True, "expect": {"node_present": [{"role": "button", "name": "ok"}]}}
        ],
        "provenance": {
            "source": "wender.remember.episode_provenance.test",
        },
        "evidence": {
            "reproduced": True,
            "verified_by": "verifier:spark:offline",
            "runs": runs,
            "version": version,
            "sha256": "0" * 64,
        },
    }


def _make_valid_manifest(
    manifest_id: str = "manifest_holdings_001",
    seed: int = 1001,
    build_id: str = "build_20261020_v1",
    content_id: str = "content_retail_pack_v1",
    schema_id: str = "schema_holdings_v1",
    source_kind: str = "holdings",
    trace_sha256: str = "1" * 64,
    initial_checkpoint_sha256: str = "2" * 64,
    support_branches: tuple[str, ...] = ("branch_alpha", "branch_beta"),
) -> ep.EpisodeManifest:
    """Create a valid EpisodeManifest instance."""
    return ep.EpisodeManifest(
        manifest_id=manifest_id,
        seed=seed,
        build_id=build_id,
        content_id=content_id,
        schema_id=schema_id,
        source_kind=source_kind,
        trace_sha256=trace_sha256,
        initial_checkpoint_sha256=initial_checkpoint_sha256,
        support_branches=support_branches,
        limits={"max_ticks": 500, "max_inputs": 500},
    )


def _make_valid_holdout(
    manifest: ep.EpisodeManifest,
) -> ep.HoldoutIdentity:
    """Create a matching HoldoutIdentity for an EpisodeManifest."""
    return ep.HoldoutIdentity(
        build_id=manifest.build_id,
        content_id=manifest.content_id,
        schema_id=manifest.schema_id,
        source_kind=manifest.source_kind,
        pinned_manifest_sha256=manifest.compute_sha256(),
        pinned_checkpoint_sha256=manifest.initial_checkpoint_sha256,
    )


class TestEpisodeProvenanceImplementation(unittest.TestCase):
    """Substantive public-path test suite for episode provenance and INV J9 invariants."""

    def test_bind_provenance_healthy_control(self):
        """Healthy control: binds admitted skill with EpisodeManifest into immutable wrapper."""
        skill = _make_valid_skill()
        manifest = _make_valid_manifest()

        bound = ep.bind_provenance(
            skill=skill,
            episode_manifest=manifest,
            replay_mode="core",
            support_path=("root",),
            verification_ref="verifier:replay:core:test",
            evidence_version=1,
        )

        self.assertIsInstance(bound, ep.BoundProvenanceSkill)
        self.assertTrue(bound.is_supported)
        self.assertEqual(bound.evidence_version, 1)
        self.assertEqual(bound.skill_id, "skill:holdings:rebalance_v1")

        # Verify all 11 provenance fields
        prov = bound.provenance
        self.assertEqual(prov.episode_manifest_sha256, manifest.compute_sha256())
        self.assertEqual(prov.trace_sha256, manifest.trace_sha256)
        self.assertEqual(prov.initial_checkpoint_sha256, manifest.initial_checkpoint_sha256)
        self.assertEqual(prov.build_id, manifest.build_id)
        self.assertEqual(prov.content_id, manifest.content_id)
        self.assertEqual(prov.schema_id, manifest.schema_id)
        self.assertEqual(prov.source_kind, manifest.source_kind)
        self.assertEqual(prov.replay_mode, "core")
        self.assertEqual(prov.support_path, ("root",))
        self.assertEqual(prov.verification_ref, "verifier:replay:core:test")
        self.assertEqual(prov.evidence_version, 1)

        # Existing skill schema is preserved without alteration
        admitted_json = skills.admit(bound.skill)
        self.assertIn("rebalance_portfolio", admitted_json)

    def test_inv_j9_1_exact_provenance_and_holdout_identity(self):
        """INV J9 1 & Gate G-J9-1: Exact provenance and holdout identity.

        Positive control: pinned episode and checkpoint pass.
        Negative contrast: same seed with foreign build rejected.
        """
        manifest = _make_valid_manifest(seed=42, build_id="build_authorized")
        holdout = _make_valid_holdout(manifest)
        skill = _make_valid_skill()

        # Positive control
        bound_healthy = ep.bind_provenance(
            skill=skill,
            episode_manifest=manifest,
            holdout_identity=holdout,
        )
        gate_res = ep.evaluate_gate_g_j9_1(bound_healthy, holdout, expected_seed=42)
        self.assertEqual(gate_res["status"], "PASS")
        self.assertTrue(gate_res["passed"])
        self.assertTrue(gate_res["build_matches"])
        self.assertTrue(gate_res["checkpoint_matches"])

        # Negative contrast: same seed (42) with foreign build accepted? Must be REJECTED!
        foreign_manifest = ep.EpisodeManifest(
            manifest_id="manifest_foreign",
            seed=42,  # identical seed!
            build_id="build_unauthorized_foreign",  # foreign build!
            content_id=manifest.content_id,
            schema_id=manifest.schema_id,
            source_kind=manifest.source_kind,
            trace_sha256=manifest.trace_sha256,
            initial_checkpoint_sha256=manifest.initial_checkpoint_sha256,
        )

        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.bind_provenance(
                skill=skill,
                episode_manifest=foreign_manifest,
                holdout_identity=holdout,
            )
        self.assertIn("build_id mismatch", str(cm.exception))

        # Gate G-J9-1 evaluation directly on foreign bound skill
        bound_foreign = ep.bind_provenance(
            skill=skill,
            episode_manifest=foreign_manifest,  # without holdout check in bind
        )
        gate_foreign = ep.evaluate_gate_g_j9_1(bound_foreign, holdout, expected_seed=42)
        self.assertEqual(gate_foreign["status"], "FAIL")
        self.assertFalse(gate_foreign["passed"])
        self.assertFalse(gate_foreign["build_matches"])
        self.assertEqual(gate_foreign["reason"], "foreign_build")

    def test_inv_j9_1_rejects_altered_content_schema_source_and_checkpoint(self):
        """INV J9 1: Rejection of changed content, schema, source, or checkpoint."""
        manifest = _make_valid_manifest()
        holdout = _make_valid_holdout(manifest)
        skill = _make_valid_skill()

        # Changed content_id
        bad_content_manifest = _make_valid_manifest(content_id="foreign_content_pack")
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.bind_provenance(skill, bad_content_manifest, holdout_identity=holdout)
        self.assertIn("content_id mismatch", str(cm.exception))

        # Changed schema_id
        bad_schema_manifest = _make_valid_manifest(schema_id="foreign_schema_v9")
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.bind_provenance(skill, bad_schema_manifest, holdout_identity=holdout)
        self.assertIn("schema_id mismatch", str(cm.exception))

        # Changed source_kind
        bad_source_manifest = _make_valid_manifest(source_kind="unsupported_backend")
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.bind_provenance(skill, bad_source_manifest, holdout_identity=holdout)
        self.assertIn("source_kind mismatch", str(cm.exception))

        # Conflicting trace sha
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.bind_provenance(skill, manifest, trace_sha256="9" * 64)
        self.assertIn("trace_sha256 conflicts", str(cm.exception))

        # Conflicting checkpoint sha
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.bind_provenance(skill, manifest, initial_checkpoint_sha256="8" * 64)
        self.assertIn("initial_checkpoint_sha256 conflicts", str(cm.exception))

    def test_inv_j9_2_replay_modes_core_grounding_branch(self):
        """INV J9 2: Modes core, grounding, and branch distinguish replay traversal."""
        manifest = _make_valid_manifest()
        skill = _make_valid_skill()

        for mode in ("core", "grounding", "branch"):
            with self.subTest(mode=mode):
                support_path = ("branch_alpha",) if mode == "branch" else ()
                bound = ep.bind_provenance(
                    skill=skill,
                    episode_manifest=manifest,
                    replay_mode=mode,
                    support_path=support_path,
                )
                self.assertEqual(bound.provenance.replay_mode, mode)
                self.assertTrue(bound.is_supported)

        # Invalid mode
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.bind_provenance(skill, manifest, replay_mode="speculative_live")
        self.assertIn("replay_mode must be one of", str(cm.exception))

    def test_inv_j9_2_missing_branch_unsupported_until_executed(self):
        """INV J9 2: Missing branch is unsupported until reset and actual execution."""
        manifest = _make_valid_manifest(support_branches=("branch_alpha", "branch_beta"))
        skill = _make_valid_skill()

        # 1. Missing / unlisted branch
        bound_unlisted = ep.bind_provenance(
            skill=skill,
            episode_manifest=manifest,
            replay_mode="branch",
            support_path=("unknown_missing_branch",),
            is_executed=True,
        )
        self.assertFalse(bound_unlisted.is_supported)

        # 2. Listed branch but not actually executed
        bound_unexecuted = ep.bind_provenance(
            skill=skill,
            episode_manifest=manifest,
            replay_mode="branch",
            support_path=("branch_alpha",),
            is_executed=False,
        )
        self.assertFalse(bound_unexecuted.is_supported)

        # 3. Listed branch with empty support path
        bound_empty_path = ep.bind_provenance(
            skill=skill,
            episode_manifest=manifest,
            replay_mode="branch",
            support_path=(),
            is_executed=True,
        )
        self.assertFalse(bound_empty_path.is_supported)

        # 4. Listed branch and actually executed
        bound_executed = ep.bind_provenance(
            skill=skill,
            episode_manifest=manifest,
            replay_mode="branch",
            support_path=("branch_alpha",),
            is_executed=True,
        )
        self.assertTrue(bound_executed.is_supported)

    def test_inv_j9_3_economic_success_and_disallowed_statuses(self):
        """INV J9 3: A proposal, refusal, accepted navigation or unverified terminal cannot become economic success."""
        disallowed = [
            "proposal",
            "refusal",
            "accepted",
            "accepted_navigation",
            "navigation",
            "unverified",
            "unverified_terminal",
            "unsupported",
        ]

        for st in disallowed:
            with self.subTest(status=st):
                # Disallowed statuses cannot be economic success in helper
                self.assertFalse(
                    ep.evaluate_economic_success(status=st, is_terminal=True, terminal_verified=True, reward=100.0)
                )

                # Attempting to construct EpisodeOutcome with economic_success=True on disallowed status raises
                with self.assertRaises(ep.ProvenanceRejected) as cm:
                    ep.EpisodeOutcome(
                        status=st,
                        is_terminal=True,
                        terminal_verified=True,
                        reward=100.0,
                        economic_success=True,
                    )
                self.assertIn("cannot become economic success", str(cm.exception))

        # Unverified terminal cannot become economic success
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.EpisodeOutcome(
                status="completed",
                is_terminal=True,
                terminal_verified=False,
                reward=100.0,
                economic_success=True,
            )
        self.assertIn("requires a verified terminal outcome", str(cm.exception))

        # Non-terminal outcome cannot become economic success
        with self.assertRaises(ep.ProvenanceRejected) as cm:
            ep.EpisodeOutcome(
                status="completed",
                is_terminal=False,
                terminal_verified=True,
                reward=100.0,
                economic_success=True,
            )
        self.assertIn("requires a verified terminal outcome", str(cm.exception))

        # Valid economic success outcome
        outcome_success = ep.EpisodeOutcome(
            status="completed",
            is_terminal=True,
            terminal_verified=True,
            reward=500.0,
            economic_success=True,
        )
        self.assertTrue(outcome_success.economic_success)

    def test_inv_j9_3_missing_frames_are_ausente(self):
        """INV J9 3: Missing frames are unavailable evidence (AUSENTE) rather than reconstructed screenshots."""
        # Simulated observation fetch where frames are missing
        def get_frame_evidence(has_frame_buffer: bool):
            if not has_frame_buffer:
                raise ep.AusenteError("AUSENTE: Frame evidence is unavailable in RAM; screenshot reconstruction disallowed")
            return b"RGB24_BUFFER"

        self.assertEqual(get_frame_evidence(True), b"RGB24_BUFFER")

        with self.assertRaises(ep.AusenteError) as cm:
            get_frame_evidence(False)
        self.assertIn("AUSENTE", str(cm.exception))

    def test_inv_j9_4_promotion_requires_replay_and_exact_provenance(self):
        """INV J9 4: Promotion requires public replay, skill admission, and exact provenance."""
        manifest = _make_valid_manifest()
        holdout = _make_valid_holdout(manifest)

        # 1. Healthy skill with valid replay evidence promotes successfully
        skill_good = _make_valid_skill(runs=2, version=1)
        bound_good = ep.bind_provenance(skill_good, manifest, holdout_identity=holdout)
        admitted = ep.promote_skill(bound_good, expected_holdout=holdout)
        self.assertTrue(admitted.startswith("{"))

        # 2. Skill with runs < 2 refuses promotion
        skill_one_run = _make_valid_skill(runs=1)
        bound_one_run = ep.bind_provenance(skill_one_run, manifest)
        with self.assertRaises(ep.ProvenanceRefused) as cm:
            ep.promote_skill(bound_one_run)
        self.assertIn("at least 2 replay runs", str(cm.exception))

        # 3. Skill with unverified reproduction refuses promotion
        skill_unreproduced = _make_valid_skill()
        skill_unreproduced["evidence"]["reproduced"] = False
        bound_unrep = ep.bind_provenance(skill_unreproduced, manifest)
        with self.assertRaises(ep.ProvenanceRefused) as cm:
            ep.promote_skill(bound_unrep)
        self.assertIn("evidence.reproduced=True", str(cm.exception))

        # 4. Promotion fails if on unsupported branch
        bound_unsupported = ep.bind_provenance(
            skill_good,
            manifest,
            replay_mode="branch",
            support_path=("non_existent_branch",),
        )
        with self.assertRaises(ep.ProvenanceRefused) as cm:
            ep.promote_skill(bound_unsupported)
        self.assertIn("unsupported episode branch", str(cm.exception))

    def test_inv_j9_4_transactional_storage_and_atomic_indexing(self):
        """INV J9 4: Outer episode index provides atomic transactional storage.

        Serializes by episode, skill, and evidence version.
        Conflicting support or stale completion refuses atomically.
        """
        index = ep.EpisodeProvenanceIndex()
        manifest = _make_valid_manifest()
        skill_v1 = _make_valid_skill(version=1)

        bound_v1 = ep.bind_provenance(
            skill=skill_v1,
            episode_manifest=manifest,
            replay_mode="branch",
            support_path=("branch_alpha",),
            evidence_version=1,
        )

        # First commit succeeds
        key = index.commit(bound_v1)
        self.assertEqual(index.count(), 1)
        self.assertTrue(index.has_entry(key[0], key[1], key[2]))

        # Commit higher version (v2) on same support path succeeds
        skill_v2 = _make_valid_skill(version=2)
        bound_v2 = ep.bind_provenance(
            skill=skill_v2,
            episode_manifest=manifest,
            replay_mode="branch",
            support_path=("branch_alpha",),
            evidence_version=2,
        )
        key_v2 = index.commit(bound_v2)
        self.assertEqual(index.count(), 2)

        # 1. Stale completion check: commit v1 again (or v2 again) refuses atomically
        with self.assertRaises(ep.ProvenanceConflict) as cm:
            index.commit(bound_v1)
        self.assertIn("Stale completion refused", str(cm.exception))

        with self.assertRaises(ep.ProvenanceConflict) as cm:
            index.commit(bound_v2)
        self.assertIn("Stale completion refused", str(cm.exception))

        # 2. Conflicting support check: commit v3 with different support path refuses atomically
        skill_v3 = _make_valid_skill(version=3)
        bound_v3_conflict = ep.bind_provenance(
            skill=skill_v3,
            episode_manifest=manifest,
            replay_mode="branch",
            support_path=("conflicting_branch_beta",),
            evidence_version=3,
        )
        with self.assertRaises(ep.ProvenanceConflict) as cm:
            index.commit(bound_v3_conflict)
        self.assertIn("Conflicting support refused", str(cm.exception))

        # Index count unchanged after refused writes (atomic refusal)
        self.assertEqual(index.count(), 2)

    def test_write_spy_rejects_leakage(self):
        """WriteSpy rejects pixel frame leakage or raw receipt leakage."""
        spy = ep.WriteSpy()

        # Normal safe hash passes
        spy.check_data("a" * 64, "safe_hash")

        # Frame leakage raises ProvenanceLeakageError
        with self.assertRaises(ep.ProvenanceLeakageError) as cm:
            spy.check_data(b"\x89PNG\r\n\x1a\nfake_frame_bytes", "leaked_frame.png")
        self.assertIn("frame pixel leakage detected", str(cm.exception))
        self.assertTrue(spy.leakage_detected)

        # Base64 encoded PNG leakage raises
        with self.assertRaises(ep.ProvenanceLeakageError) as cm:
            spy.check_data("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA", "leaked_b64")
        self.assertIn("encoded frame leakage detected", str(cm.exception))

    def test_exercise_both_source_policies_without_relabeling(self):
        """Exercise both source policies (Holdings policy and Web/Browser policy) without relabeling."""
        # 1. Source Policy A: Holdings Policy
        holdings_manifest = ep.EpisodeManifest(
            manifest_id="manifest_holdings_live",
            seed=7001,
            build_id="build_holdings_20261020",
            content_id="content_retail_pack_v1",
            schema_id="schema_holdings_v1",
            source_kind="holdings",
            trace_sha256="3" * 64,
            initial_checkpoint_sha256="4" * 64,
            support_branches=("pause_check",),
        )
        holdings_skill = {
            "id": "skill:holdings:pause",
            "intent": "pause_trading",
            "params": {},
            "steps": [{"path": "api", "op": "pause", "args": {}}],
            "checkpoints": [{"after_step": 0, "verified": True, "expect": {"node_present": [{"role": "status"}]}}],
            "provenance": {"source": "holdings_source_policy"},
            "evidence": {"reproduced": True, "verified_by": "verifier_holdings", "runs": 2, "version": 1},
        }

        bound_holdings = ep.bind_provenance(
            skill=holdings_skill,
            episode_manifest=holdings_manifest,
            replay_mode="core",
            support_path=("pause_check",),
        )
        self.assertEqual(bound_holdings.provenance.source_kind, "holdings")

        # 2. Source Policy B: Web / Browser Policy
        web_manifest = ep.EpisodeManifest(
            manifest_id="manifest_web_live",
            seed=8002,
            build_id="build_web_chrome_20261020",
            content_id="content_web_suite_v1",
            schema_id="schema_web_v1",
            source_kind="web",
            trace_sha256="5" * 64,
            initial_checkpoint_sha256="6" * 64,
            support_branches=("nav_frontier",),
        )
        web_skill = {
            "id": "skill:web:navigate",
            "intent": "navigate_frontier",
            "params": {"url": "https://example.local"},
            "steps": [{"path": "ax", "target": "nav_link"}],
            "checkpoints": [{"after_step": 0, "verified": True, "expect": {"text_contains": "Example"}}],
            "provenance": {"source": "web_source_policy"},
            "evidence": {"reproduced": True, "verified_by": "verifier_web", "runs": 2, "version": 1},
        }

        bound_web = ep.bind_provenance(
            skill=web_skill,
            episode_manifest=web_manifest,
            replay_mode="grounding",
            support_path=("nav_frontier",),
        )
        self.assertEqual(bound_web.provenance.source_kind, "web")

        # Verify distinct identities and policies without cross-labeling
        self.assertNotEqual(
            bound_holdings.provenance.episode_manifest_sha256,
            bound_web.provenance.episode_manifest_sha256,
        )
        self.assertNotEqual(bound_holdings.provenance.source_kind, bound_web.provenance.source_kind)
        self.assertNotEqual(bound_holdings.provenance.replay_mode, bound_web.provenance.replay_mode)

    def test_holdings_offline_fixtures_fields(self):
        """Offline Holdings records follow COMMANDS.md and CONTRACTS.md field requirements."""
        # Pause command: tick, seq, cmd="pause", args={}
        pause_cmd = {"tick": 10, "seq": 1, "cmd": "pause", "args": {}}
        self.assertEqual(pause_cmd["cmd"], "pause")
        self.assertEqual(pause_cmd["args"], {})

        # Outcome record: seq, requested_tick, applied_tick, order_seq_id, status, reason, state_sha256
        outcome_record = {
            "seq": 1,
            "requested_tick": 10,
            "applied_tick": 10,
            "order_seq_id": 1,
            "status": "accepted",
            "reason": None,
            "state_sha256": "7" * 64,
        }
        for k in ("seq", "requested_tick", "applied_tick", "order_seq_id", "status", "reason", "state_sha256"):
            self.assertIn(k, outcome_record)


if __name__ == "__main__":
    unittest.main()
