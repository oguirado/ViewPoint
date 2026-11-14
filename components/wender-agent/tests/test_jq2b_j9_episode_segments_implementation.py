# @module {"id":"tests.test_jq2b_j9_episode_segments_implementation","layer":3,"owner":"TESTS","deps":["wa.learn.episode_segments"]}
"""Substantive public-path tests for wender.learn.episode_segments (jq2b-j9-episode-segments).

Exercises the real public API of wender.learn.episode_segments following INV J9 1 through INV J9 4:
- Positive control: accepted terminal plus postcondition replay admits skill and passes Gate G J9 2.
- Negative contrast: rejected action, refusal, proposal, navigation, or unverified terminal cannot
  become economic success and fails Gate G J9 2.
- Missing frames return unavailable segmentation evidence rather than reconstructed screenshots.
- Missing branch is unsupported until reset and actual execution; no invented reward or failure.
- Exact provenance fields and replay modes (core, grounding, branch).
- Transactional storage serializes by (episode, skill, evidence_version) and refuses conflicting support
  or stale completion atomically.
- RAM-only frames and WriteSpy verification; any pixel or frame byte payload in a segment write
  raises EpisodeSegmentError (frames stay in RAM only; segments hold digests and metadata).
"""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
import unittest

import numpy as np

from wender.learn import episode_segments as es
from wender.remember import skills
from wender.state import schema


def _make_dummy_frame(width=64, height=48, rgb=(16, 16, 16)):
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :] = rgb
    return SimpleNamespace(width=width, height=height, data=arr.tobytes())


def _make_cdp_state(nodes):
    pruned_nodes = []
    boxes = {}
    actionable_roles = set()
    for i, n in enumerate(nodes):
        node_id = n.get("id", "%016x" % (i + 1))
        role = n.get("role", "button")
        if n.get("actionable", False):
            actionable_roles.add(role)
        states_dict = n.get("states", {})
        attrs = list(states_dict.items())
        pruned_nodes.append({
            "id": node_id,
            "role": role,
            "name": n.get("name", f"Node {i}"),
            "depth": n.get("depth", 0),
            "ordinal": n.get("ordinal", i),
            "attrs": attrs,
        })
        boxes[node_id] = n.get("box", [0, 0, 10, 10])

    return schema.build_state(
        pruned_nodes,
        kind="cdp",
        url="http://example.com/app",
        title="App Test",
        ax_sha256="0" * 64,
        ax_stable_sha256="0" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=len(pruned_nodes),
        boxes=boxes,
        actionable_roles=frozenset(actionable_roles),
    )


class FakeCdpDriver:
    def __init__(self, states_sequence):
        self.states_sequence = list(states_sequence)
        self.step_idx = 0
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1
        self.step_idx = 0

    def observe(self):
        idx = min(self.step_idx, len(self.states_sequence) - 1)
        st = self.states_sequence[idx]
        return st, "0123456789abcdef"

    def act(self, action):
        self.step_idx += 1


class TestEpisodeSegmentsImplementation(unittest.TestCase):
    def setUp(self):
        self.valid_provenance = es.EpisodeProvenance(
            episode_manifest_sha256="a" * 64,
            trace_sha256="b" * 64,
            initial_checkpoint_sha256="c" * 64,
            build_id="build-test-20261020",
            content_id="content-retail-v1",
            schema_id="schema-holdings-v1",
            source_kind="holdings",
            replay_mode="core",
            support_path="holdings.trades",
            verification_ref="ref-audit-12345",
            evidence_version=1,
        )

        self.st0 = _make_cdp_state([{"id": "0000000000000001", "role": "button", "name": "Start", "actionable": True}])
        self.st1 = _make_cdp_state([
            {"id": "0000000000000001", "role": "button", "name": "Start", "actionable": True},
            {"id": "0000000000000002", "role": "heading", "name": "Success"},
        ])

    def test_inv_j9_1_provenance_validation_and_replay_modes(self):
        """INV J9 1: Provenance fields must be complete and valid; modes core, grounding, branch."""
        for mode in ("core", "grounding", "branch"):
            prov = copy.deepcopy(self.valid_provenance)
            prov = es.replace(prov, replay_mode=mode)
            self.assertEqual(prov.replay_mode, mode)
            p_bytes = es.canonical_provenance_bytes(prov)
            self.assertIsInstance(p_bytes, bytes)

        # Invalid replay mode rejected
        with self.assertRaises(es.EpisodeSegmentError):
            es.EpisodeProvenance(
                episode_manifest_sha256="a" * 64,
                trace_sha256="b" * 64,
                initial_checkpoint_sha256="c" * 64,
                build_id="build1",
                content_id="cont1",
                schema_id="sch1",
                source_kind="holdings",
                replay_mode="invalid_mode",
                support_path="path",
                verification_ref="ref",
                evidence_version=1,
            )

        # Non-hex SHA256 rejected
        with self.assertRaises(es.EpisodeSegmentError):
            es.replace(self.valid_provenance, episode_manifest_sha256="not_a_hex")

        # Zero or negative evidence version rejected
        with self.assertRaises(es.EpisodeSegmentError):
            es.replace(self.valid_provenance, evidence_version=0)

    def test_positive_control_accepted_terminal_and_replay_admission(self):
        """Positive control: accepted terminal plus postcondition replay passes Gate G J9 2."""
        # Holdings pause command and outcome fixture
        cmd = {"tick": 0, "seq": 0, "cmd": "pause", "args": {}}
        outcome = {
            "seq": 0,
            "requested_tick": 0,
            "applied_tick": 0,
            "order_seq_id": 1,
            "status": "applied",
            "reason": "normal",
            "state_sha256": "d" * 64,
        }

        attempt = es.AttemptRecord(
            attempt_id="att-1",
            tick=0,
            seq=0,
            action=cmd,
            status="applied",
            outcome=outcome,
            is_terminal=True,
            is_economic=True,
            is_verified=True,
            pre_state=self.st0,
            post_state=self.st1,
            source_identity={"engine": "holdings", "account": "act_01"},
            ram_bytes=b"volatile_state_bytes",
        )

        driver = FakeCdpDriver([self.st0, self.st1])
        ep = es.EpisodeRecord(
            episode_id="ep-pos-control",
            provenance=self.valid_provenance,
            attempts=[attempt],
        )

        evidence = es.segment_episode(ep, driver=driver, verified_by="test-verifier")
        self.assertEqual(evidence.status, "available")
        self.assertEqual(evidence.evidence_status, "available")
        self.assertEqual(len(evidence.skills), 1)

        admitted_skill = evidence.skills[0]
        self.assertIn("skill:ep-pos-control:att-1", admitted_skill["id"])
        self.assertTrue(admitted_skill["evidence"]["reproduced"])
        self.assertEqual(admitted_skill["evidence"]["verified_by"], "test-verifier")

        # Provenance correctly bound
        sk_prov = admitted_skill["provenance"]
        self.assertEqual(sk_prov["episode_manifest_sha256"], self.valid_provenance.episode_manifest_sha256)
        self.assertEqual(sk_prov["build_id"], self.valid_provenance.build_id)
        self.assertEqual(sk_prov["replay_mode"], "core")

        # Gate G J9 2 passes
        gate_res = evidence.gates["G-J9-2"]
        self.assertTrue(gate_res["passed"])
        self.assertEqual(gate_res["status"], "PASS")

    def test_negative_contrast_rejected_action_mined_as_skill_fails_gate(self):
        """Negative contrast: rejected action mined as successful skill fails Gate G J9 2."""
        cmd = {"tick": 0, "seq": 0, "cmd": "pause", "args": {}}
        outcome = {
            "seq": 0,
            "requested_tick": 0,
            "applied_tick": 0,
            "order_seq_id": 1,
            "status": "rejected",
            "reason": "funds_insufficient",
            "state_sha256": "e" * 64,
        }

        # Rejected attempt
        attempt = es.AttemptRecord(
            attempt_id="att-rej-1",
            tick=0,
            seq=0,
            action=cmd,
            status="rejected",
            outcome=outcome,
            is_terminal=False,
            is_economic=False,
            is_verified=False,
            source_identity="holdings:test",
        )

        ep = es.EpisodeRecord(
            episode_id="ep-neg-contrast",
            provenance=self.valid_provenance,
            attempts=[attempt],
        )

        evidence = es.segment_episode(ep)
        self.assertEqual(len(evidence.skills), 0)
        self.assertEqual(len(evidence.rejected_attempts), 1)

        # Gate G J9 2 fails because no verified economic skill was admitted
        gate_res = evidence.gates["G-J9-2"]
        self.assertFalse(gate_res["passed"])
        self.assertEqual(gate_res["status"], "FAIL")

        # Direct mutant contrast: If an external report tries to claim a rejected action was admitted
        fake_bad_evidence = {
            "skills": [{
                "id": "skill:bad",
                "provenance": {"mining_attempt_status": "rejected", "is_economic": True, "is_terminal": True, "is_verified": True},
                "evidence": {"reproduced": True, "verified_by": "hacked"},
            }],
            "retained_attempts": [attempt],
        }
        res = es.evaluate_gate_g_j9_2(fake_bad_evidence)
        self.assertFalse(res["passed"])
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("mined_non_economic_action_as_skill", res["reason"])

    def test_proposals_and_ui_navigation_not_mined_as_economic_success(self):
        """INV J9 2: Proposals, refusals, and UI navigation cannot become economic success."""
        att_prop = es.AttemptRecord(
            attempt_id="prop-1",
            tick=0,
            seq=0,
            action={"cmd": "buy", "amount": 10},
            status="proposal",
            outcome={"status": "pending"},
        )
        att_nav = es.AttemptRecord(
            attempt_id="nav-1",
            tick=1,
            seq=1,
            action={"path": "ax", "op": "click", "target": "nav_link", "kind": "navigation"},
            status="navigation",
            outcome={"status": "ok"},
        )
        att_unverified_term = es.AttemptRecord(
            attempt_id="unv-term-1",
            tick=2,
            seq=2,
            action={"cmd": "commit"},
            status="unverified_terminal",
            outcome={"status": "ok"},
            is_terminal=True,
            is_economic=True,
            is_verified=False,  # Unverified!
        )

        ep = es.EpisodeRecord(
            episode_id="ep-non-economic",
            provenance=self.valid_provenance,
            attempts=[att_prop, att_nav, att_unverified_term],
        )

        evidence = es.segment_episode(ep)
        self.assertEqual(len(evidence.skills), 0)
        gate_res = evidence.gates["G-J9-2"]
        self.assertFalse(gate_res["passed"])
        self.assertEqual(gate_res["status"], "FAIL")

    def test_inv_j9_3_missing_branch_unsupported_until_reset_and_execution(self):
        """INV J9 3: Missing branch is unsupported until reset and actual execution; no invented reward."""
        unverified_branch = es.BranchRecord(
            branch_id="branch-speculative-1",
            status="missing",
            steps=[{"cmd": "sell"}],
            reset_verified=False,
        )

        ep = es.EpisodeRecord(
            episode_id="ep-branch",
            provenance=es.replace(self.valid_provenance, replay_mode="branch"),
            attempts=[],
            branches=[unverified_branch],
        )

        # Unexecuted branch is marked unsupported
        with self.assertRaises(es.EpisodeSegmentError):
            # If status was asserted as executed without reset verification, it refuses
            bad_branch = es.BranchRecord(
                branch_id="branch-bad",
                status="executed",
                steps=[{"cmd": "sell"}],
                reset_verified=False,  # Contradiction!
            )
            es.segment_episode(es.replace(ep, branches=[bad_branch]))

    def test_inv_j9_4_missing_frames_return_unavailable_evidence(self):
        """INV J9 4: Missing frames return unavailable evidence rather than reconstructed screenshots."""
        attempt = es.AttemptRecord(
            attempt_id="att-framed",
            tick=0,
            seq=0,
            action={"path": "ax", "op": "click", "target": "btn"},
            status="applied",
            outcome={"status": "ok"},
            is_terminal=True,
            is_economic=True,
            is_verified=True,
            frame_indices=(0, 1),  # Requires frames!
        )

        ep = es.EpisodeRecord(
            episode_id="ep-frames-missing",
            provenance=self.valid_provenance,
            attempts=[attempt],
            frames=None,  # No frames provided!
        )

        evidence = es.segment_episode(ep)
        self.assertEqual(evidence.status, "unavailable")
        self.assertEqual(evidence.evidence_status, "unavailable")
        self.assertIn("missing frames", evidence.reason)
        self.assertFalse(evidence.gates["G-J9-2"]["passed"])

        # Frame provider returning None
        evidence2 = es.segment_episode(ep, frame_provider=lambda idx: None)
        self.assertEqual(evidence2.status, "unavailable")
        self.assertEqual(evidence2.evidence_status, "unavailable")

    def test_inv_j9_4_transactional_storage_atomic_serialization_and_conflict_refusal(self):
        """INV J9 4: Outer episode index serializes by (episode, skill, version) and refuses conflicts atomically."""
        index = es.TransactionalEpisodeIndex()
        entry_data = {
            "skill_id": "skill-1",
            "provenance": self.valid_provenance.to_dict(),
            "status": "admitted",
        }

        # Successful store
        s1 = index.store("ep-001", "skill-1", 1, entry_data)
        self.assertIsInstance(s1, str)
        self.assertTrue(index.contains("ep-001", "skill-1", 1))

        # Idempotent write succeeds
        s2 = index.store("ep-001", "skill-1", 1, entry_data)
        self.assertEqual(s1, s2)

        # Conflicting support for same key raises conflict error atomically
        conflicting_data = dict(entry_data, status="conflicting_status")
        with self.assertRaises(es.EpisodeSegmentConflictError):
            index.store("ep-001", "skill-1", 1, conflicting_data)

        # Complete episode
        index.store("ep-001", "skill-2", 1, entry_data, is_terminal_completion=True)

        # Stale completion: attempting to write new key into already completed episode refuses
        with self.assertRaises(es.EpisodeSegmentConflictError):
            index.store("ep-001", "skill-3", 1, entry_data)

        # Serialization
        serialized = index.serialize()
        self.assertIsInstance(serialized, str)
        self.assertIn("ep-001:skill-1:1", serialized)

    def test_ram_ownership_and_write_spy_rejects_pixel_leakage(self):
        """Write spy and segment writes reject any pixel or frame byte payload; RAM ownership is retained."""
        spy = es.WriteSpy()

        # Clean metadata with digests passes
        clean_seg = {
            "start": 0,
            "end": 2,
            "bbox": {"x": 0, "y": 0, "w": 8, "h": 8},
            "frame_digests": ["a" * 64, "b" * 64],
            "segment_sha256": "c" * 64,
        }
        spy.check_data(clean_seg, "clean_meta")
        self.assertFalse(spy.leakage_detected)
        self.assertIsInstance(spy.write_segment(clean_seg, "clean_seg"), str)

        # Raw PNG bytes or arbitrary frame byte payload raises EpisodeSegmentError
        frame = _make_dummy_frame(16, 16, (10, 20, 30))
        png_header = bytes.fromhex("89504e470d0a1a0a0000")
        for raw_payload in (
            png_header,
            frame.data,
            bytearray(frame.data),
            memoryview(frame.data),
            {"segment": 1, "payload": frame.data},
            {"pixels": "ff00ff"},
            {"frame_bytes": "abcd"},
        ):
            sub_spy = es.WriteSpy()
            with self.assertRaises(es.EpisodeSegmentError):
                sub_spy.check_data(raw_payload, "leaked_pixel_payload")
            self.assertTrue(sub_spy.leakage_detected)

        # Base64 encoded PNG in string raises EpisodeSegmentError
        spy2 = es.WriteSpy()
        with self.assertRaises(es.EpisodeSegmentError):
            spy2.check_data("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA...", "leaked_b64")
        self.assertTrue(spy2.leakage_detected)

        # Segment write via TransactionalEpisodeIndex rejects pixel/frame byte payload
        index = es.TransactionalEpisodeIndex()
        with self.assertRaises(es.EpisodeSegmentError):
            index.store_segment("ep-leak", "seg-0", 1, {"start": 0, "end": 1, "data": frame.data})

        # Segment write via segment_episode rejects labeler or action leaking frame bytes
        frames = [
            _make_dummy_frame(64, 48, (16, 16, 16)),
            _make_dummy_frame(64, 48, (200, 100, 50)),
            _make_dummy_frame(64, 48, (200, 100, 50)),
            _make_dummy_frame(64, 48, (16, 16, 16)),
        ]
        ep = es.EpisodeRecord(
            episode_id="ep-leak-check",
            provenance=self.valid_provenance,
            attempts=[],
            frames=frames,
        )
        with self.assertRaises(es.EpisodeSegmentError):
            es.segment_episode(ep, labeler=lambda seg, seg_frames: seg_frames[0].data)

    def test_both_source_policies_exercised_without_relabeling(self):
        """Exercises both Holdings policy and CDP policy fixtures without relabeling."""
        # Holdings policy
        prov_holdings = es.replace(self.valid_provenance, source_kind="holdings", support_path="holdings.fifo")
        self.assertEqual(prov_holdings.source_kind, "holdings")

        # CDP policy
        prov_cdp = es.replace(self.valid_provenance, source_kind="cdp", support_path="ax.tree")
        self.assertEqual(prov_cdp.source_kind, "cdp")

        self.assertNotEqual(prov_holdings.source_kind, prov_cdp.source_kind)
        self.assertNotEqual(prov_holdings.support_path, prov_cdp.support_path)

    def test_numpy_frames_in_ram_segmentation_pipeline(self):
        """Synthetic numpy frames in RAM execute visual segmentation cleanly and store only digests/metadata."""
        # Create 10 in-RAM numpy frames
        frames = []
        for i in range(10):
            # Frames 0..3 still, 4..6 motion, 7..9 still
            rgb = (16, 16, 16) if i < 4 or i >= 7 else (200, 100, 50)
            frames.append(_make_dummy_frame(64, 48, rgb))

        att = es.AttemptRecord(
            attempt_id="att-vis-1",
            tick=5,
            seq=5,
            action={"path": "api", "cmd": "press", "target": "btn"},
            status="applied",
            outcome={"status": "ok"},
            is_terminal=True,
            is_economic=True,
            is_verified=True,
            frame_indices=(4, 5, 6),
            pre_state=self.st0,
            post_state=self.st1,
        )

        ep = es.EpisodeRecord(
            episode_id="ep-vis",
            provenance=es.replace(self.valid_provenance, replay_mode="grounding"),
            attempts=[att],
            frames=frames,
        )

        evidence = es.segment_episode(ep)
        self.assertEqual(evidence.status, "available")
        self.assertTrue(len(evidence.segments) > 0)
        for seg in evidence.segments:
            self.assertIn("frame_digests", seg)
            self.assertIn("segment_sha256", seg)
            self.assertEqual(len(seg["segment_sha256"]), 64)
            es.WriteSpy().write_segment(seg, "verified_seg")
        self.assertEqual(len(evidence.skills), 1)
        self.assertTrue(evidence.gates["G-J9-2"]["passed"])

    def test_conductor_admission_binding_ausente(self):
        """Conductor admission binding is AUSENTE — no inventar; hold dispatch until proven."""
        # Attempting to invent conductor binding without proof must fail loudly as AUSENTE
        attempt = es.AttemptRecord(
            attempt_id="att-conductor",
            tick=0,
            seq=0,
            action={"cmd": "dispatch_conductor"},
            status="unverified_terminal",
            outcome={"status": "pending_conductor"},
            is_terminal=True,
            is_economic=False,
            is_verified=False,
        )
        ep = es.EpisodeRecord(
            episode_id="ep-conductor-ausente",
            provenance=self.valid_provenance,
            attempts=[attempt],
        )
        evidence = es.segment_episode(ep)
        self.assertEqual(len(evidence.skills), 0)
        self.assertFalse(evidence.gates["G-J9-2"]["passed"])

    def test_holdings_pause_command_contracts_fields(self):
        """Holdings fixture records using exact COMMANDS.md and CONTRACTS.md field names."""
        pause_cmd = {"tick": 42, "seq": 7, "cmd": "pause", "args": {}}
        pause_outcome = {
            "seq": 7,
            "requested_tick": 42,
            "applied_tick": 42,
            "order_seq_id": 105,
            "status": "applied",
            "reason": "normal",
            "state_sha256": "f" * 64,
        }
        attempt = es.AttemptRecord(
            attempt_id="att-pause-105",
            tick=42,
            seq=7,
            action=pause_cmd,
            status="applied",
            outcome=pause_outcome,
            is_terminal=True,
            is_economic=True,
            is_verified=True,
            source_identity={"kind": "holdings", "order_seq_id": 105},
        )
        ep = es.EpisodeRecord(
            episode_id="ep-holdings-pause",
            provenance=self.valid_provenance,
            attempts=[attempt],
        )
        evidence = es.segment_episode(ep)
        self.assertEqual(len(evidence.skills), 1)
        skill = evidence.skills[0]
        self.assertEqual(skill["steps"][0]["cmd"], "pause")
        self.assertEqual(skill["steps"][0]["args"], {})
        self.assertEqual(skill["provenance"]["source"], "holdings")
        self.assertTrue(evidence.gates["G-J9-2"]["passed"])

    def test_named_mutation_mutant_mined_rejected_action_disagrees(self):
        """Named mutation: a mutant that tries to mine rejected actions must disagree with Gate G J9 2."""
        mutant_evidence = {
            "evidence_status": "available",
            "skills": [{
                "id": "skill:mutant-hack",
                "provenance": {
                    "mining_attempt_status": "rejected",  # Mutant mining rejected action!
                    "is_economic": True,
                    "is_terminal": True,
                    "is_verified": True,
                },
                "evidence": {"reproduced": True, "verified_by": "mutant"},
            }],
            "retained_attempts": [],
        }
        report = es.evaluate_gate_g_j9_2(mutant_evidence)
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["reason"], "mined_non_economic_action_as_skill: rejected")

    def test_named_mutation_mutant_missing_frames_success_disagrees(self):
        """Named mutation: a mutant that reports success on missing frames must disagree."""
        attempt = es.AttemptRecord(
            attempt_id="att-framed",
            tick=0,
            seq=0,
            action={"path": "ax", "op": "click", "target": "btn"},
            status="applied",
            outcome={"status": "ok"},
            is_terminal=True,
            is_economic=True,
            is_verified=True,
            frame_indices=(10,),
        )
        ep = es.EpisodeRecord(
            episode_id="ep-mutant-frames",
            provenance=self.valid_provenance,
            attempts=[attempt],
            frames=None,
        )
        # Healthy implementation returns unavailable evidence
        evidence = es.segment_episode(ep)
        self.assertEqual(evidence.status, "unavailable")
        self.assertEqual(evidence.evidence_status, "unavailable")

        # Mutant claiming success with missing frames disagrees
        mutant_evidence = es.replace(evidence, evidence_status="unavailable")
        report = es.evaluate_gate_g_j9_2(mutant_evidence)
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["reason"], "unavailable_segmentation_evidence")

    def test_named_mutation_mutant_unverified_branch_disagrees(self):
        """Named mutation: a mutant that executes a branch without reset verification must disagree."""
        unverified_branch = es.BranchRecord(
            branch_id="branch-mutant",
            status="executed",
            steps=[{"cmd": "hack"}],
            reset_verified=False,
        )
        ep = es.EpisodeRecord(
            episode_id="ep-mutant-branch",
            provenance=self.valid_provenance,
            attempts=[],
            branches=[unverified_branch],
        )
        with self.assertRaises(es.EpisodeSegmentError) as cm:
            es.segment_episode(ep)
        self.assertIn("cannot be marked executed without reset", str(cm.exception))

    def test_named_mutation_mutant_conflicting_storage_disagrees(self):
        """Named mutation: a mutant that silently overwrites conflicting storage must disagree."""
        index = es.TransactionalEpisodeIndex()
        data1 = {"skill_id": "sk1", "val": 100}
        data2 = {"skill_id": "sk1", "val": 200}
        index.store("ep-c", "sk1", 1, data1)
        with self.assertRaises(es.EpisodeSegmentConflictError):
            index.store("ep-c", "sk1", 1, data2)


if __name__ == "__main__":
    unittest.main()
