# @module {"id":"tests.test_fx2_f4_j3_replay_version_lifecycle_and_store","layer":2,"owner":"LEARN","deps":["wa.learn.replay","wa.remember.store","wa.remember.skills","wa.state.schema"]}
"""Tests for replay version lifecycle, Store persistence, reopen, compact, and invalid run rejection."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from wender.learn.explore import Driver
from wender.learn.replay import replay_and_admit
from wender.remember import skills
from wender.remember.store import Store, StoreCorrupt, StoreRefused
from wender.state import schema


def _make_state(nodes):
    pruned_nodes = []
    boxes = {}
    actionable_roles = set()

    for i, n in enumerate(nodes):
        node_id = n.get("id", "%016x" % (i + 1))
        role = n.get("role", "button")
        is_actionable = n.get("actionable", False)
        if is_actionable:
            actionable_roles.add(role)

        states_dict = n.get("states", {})
        attrs = list(states_dict.items())

        pruned_nodes.append({
            "id": node_id,
            "role": role,
            "name": n.get("name", "Node %d" % i),
            "depth": n.get("depth", 0),
            "ordinal": n.get("ordinal", i),
            "attrs": attrs,
        })
        boxes[node_id] = n.get("box", [0, 0, 10, 10])

    return schema.build_state(
        pruned_nodes,
        kind="cdp",
        url="http://example.com",
        title="Test App",
        ax_sha256="0" * 64,
        ax_stable_sha256="0" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=len(pruned_nodes),
        boxes=boxes,
        actionable_roles=frozenset(actionable_roles),
    )


class TrackingDriver(Driver):
    """Driver tracking reset, observe, and act invocation counts for verification."""

    def __init__(self, pre_state, post_state):
        self.pre_state = pre_state
        self.post_state = post_state
        self.step_index = 0
        self.act_count = 0
        self.observe_count = 0
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1
        self.step_index = 0

    def observe(self):
        self.observe_count += 1
        return self.pre_state if self.step_index == 0 else self.post_state

    def candidate_actions(self, state):
        return []

    def act(self, action):
        self.act_count += 1
        self.step_index += 1


class TestFx2F4J3ReplayVersionLifecycleAndStore(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store_dir = Path(self.tmpdir.name)

        self.st0 = _make_state([
            {"id": "0000000000000001", "role": "button", "name": "Start", "actionable": True}
        ])
        self.st1 = _make_state([
            {"id": "0000000000000001", "role": "button", "name": "Start", "actionable": True},
            {"id": "0000000000000002", "role": "heading", "name": "Done"}
        ])

        self.draft = {
            "id": "skill-fx2-f4-j3-test",
            "intent": "execute action and confirm done heading",
            "params": {},
            "steps": [{"path": "ax", "op": "press", "target": "0000000000000001"}],
            "checkpoints": [
                {
                    "after_step": 0,
                    "expect": {"node_present": {"role": "heading", "name": "Done"}},
                }
            ],
            "provenance": {"source": "unit_test:fx2_f4_j3"},
        }

    # --- Baseline Store Controls (Undecorated Passing Tests) ---

    def test_store_identical_revision_retry_leaves_journal_bytes_unchanged(self):
        """Identical revision retries must leave journal bytes unchanged."""
        store = Store(self.store_dir)
        skill_v1 = {
            "id": "skill-fx2-f4-j3-retry",
            "intent": "initial intent",
            "params": {},
            "steps": [{"path": "ax"}],
            "checkpoints": [{"after": 0}],
            "provenance": {"source": "unit_test"},
            "evidence": {"reproduced": True, "verified_by": "test_runner", "version": 1},
        }

        store.append_skill(skill_v1)
        journal_path = self.store_dir / "skills.jsonl"
        bytes_before = journal_path.read_bytes()

        # Retry identical skill
        store.append_skill(skill_v1)
        bytes_after = journal_path.read_bytes()

        self.assertEqual(bytes_before, bytes_after)

    def test_store_stale_changed_revision_refused(self):
        """Stale changed revisions must be refused with StoreRefused without modifying journal."""
        store = Store(self.store_dir)
        skill_v1 = {
            "id": "skill-fx2-f4-j3-stale",
            "intent": "original intent",
            "params": {},
            "steps": [{"path": "ax"}],
            "checkpoints": [{"after": 0}],
            "provenance": {"source": "unit_test"},
            "evidence": {"reproduced": True, "verified_by": "test_runner", "version": 1},
        }

        store.append_skill(skill_v1)
        journal_path = self.store_dir / "skills.jsonl"
        bytes_before = journal_path.read_bytes()

        # Attempt stale revision with modified body but same or lower version
        stale_skill = copy.deepcopy(skill_v1)
        stale_skill["intent"] = "modified intent with stale version"

        with self.assertRaises(StoreRefused):
            store.append_skill(stale_skill)

        bytes_after = journal_path.read_bytes()
        self.assertEqual(bytes_before, bytes_after)

    def test_store_checksummed_semantic_corruption_rejected(self):
        """Checksummed semantic corruption in journal must reject without partial replacement."""
        journal_path = self.store_dir / "skills.jsonl"
        self.store_dir.mkdir(parents=True, exist_ok=True)

        valid_rec = {
            "schema": "wender.store/v1",
            "kind": "skill",
            "seq": 1,
            "body": {
                "id": "skill-corrupt",
                "intent": "intent",
                "params": {},
                "steps": [{"path": "ax"}],
                "checkpoints": [{"after": 0}],
                "provenance": {"source": "unit_test"},
                "evidence": {"reproduced": True, "verified_by": "test", "version": 1},
            },
        }
        unsigned = {k: v for k, v in valid_rec.items() if k != "line_sha256"}
        valid_rec["line_sha256"] = hashlib.sha256(schema.dumps(unsigned).encode("utf-8")).hexdigest()

        # Write valid line followed by tampered line
        corrupt_rec = copy.deepcopy(valid_rec)
        corrupt_rec["seq"] = 2
        corrupt_rec["line_sha256"] = "0" * 64

        journal_path.write_text(
            schema.dumps(valid_rec) + "\n" + schema.dumps(corrupt_rec) + "\n",
            encoding="utf-8",
        )

        store = Store(self.store_dir)
        with self.assertRaises(StoreCorrupt):
            store.load_skills()

    def test_store_strictly_newer_rule_preserves_non_consecutive_versions(self):
        """Store accepts strictly-newer versions without requiring consecutive integers."""
        store = Store(self.store_dir)
        base_skill = {
            "id": "skill-fx2-f4-j3-jump",
            "intent": "version 1",
            "params": {},
            "steps": [{"path": "ax"}],
            "checkpoints": [{"after": 0}],
            "provenance": {"source": "unit_test"},
            "evidence": {"reproduced": True, "verified_by": "test_runner", "version": 1},
        }

        store.append_skill(base_skill)

        # Non-consecutive version jump: 1 -> 5 -> 12
        skill_v5 = copy.deepcopy(base_skill)
        skill_v5["intent"] = "version 5"
        skill_v5["evidence"]["version"] = 5
        store.append_skill(skill_v5)

        loaded_v5 = store.load_skills()
        self.assertEqual(len(loaded_v5), 1)
        self.assertEqual(loaded_v5[0]["evidence"]["version"], 5)

        skill_v12 = copy.deepcopy(base_skill)
        skill_v12["intent"] = "version 12"
        skill_v12["evidence"]["version"] = 12
        store.append_skill(skill_v12)

        loaded_v12 = store.load_skills()
        self.assertEqual(len(loaded_v12), 1)
        self.assertEqual(loaded_v12[0]["evidence"]["version"], 12)
        self.assertEqual(loaded_v12[0]["intent"], "version 12")

    def test_store_reopen_and_compact_lifecycle(self):
        """Reopening Store and compacting preserves latest valid records across revisions."""
        store1 = Store(self.store_dir)
        skill_v1 = {
            "id": "skill-lifecycle",
            "intent": "intent v1",
            "params": {},
            "steps": [{"path": "ax"}],
            "checkpoints": [{"after": 0}],
            "provenance": {"source": "unit_test"},
            "evidence": {"reproduced": True, "verified_by": "test", "version": 1},
        }
        store1.append_skill(skill_v1)

        skill_v2 = copy.deepcopy(skill_v1)
        skill_v2["intent"] = "intent v2"
        skill_v2["evidence"]["version"] = 2
        store1.append_skill(skill_v2)

        # Reopen Store
        store2 = Store(self.store_dir)
        loaded = store2.load_skills()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["intent"], "intent v2")
        self.assertEqual(loaded[0]["evidence"]["version"], 2)

        # Compact log
        store2.compact()

        # Reopen Store again post-compaction
        store3 = Store(self.store_dir)
        loaded_compact = store3.load_skills()
        self.assertEqual(len(loaded_compact), 1)
        self.assertEqual(loaded_compact[0]["intent"], "intent v2")
        self.assertEqual(loaded_compact[0]["evidence"]["version"], 2)

    def test_tracking_driver_baseline_control(self):
        """Verify tracking driver operates reproducibly."""
        driver = TrackingDriver(self.st0, self.st1)
        obs0 = driver.observe()
        self.assertEqual(obs0, self.st0)
        self.assertEqual(driver.observe_count, 1)

        driver.act({"path": "ax", "op": "press"})
        obs1 = driver.observe()
        self.assertEqual(obs1, self.st1)
        self.assertEqual(driver.act_count, 1)

        driver.reset()
        self.assertEqual(driver.reset_count, 1)

    # --- Replay & Admit Composed with Store Version Lifecycle Defects ---

    def test_replay_and_admit_allocates_version_one_and_preserves_evidence(self):
        """replay_and_admit must allocate evidence.version = 1 on initial admission and preserve verifier, runs, sha256."""
        driver = TrackingDriver(self.st0, self.st1)
        res_json = replay_and_admit(copy.deepcopy(self.draft), driver, verified_by="verifier_v1", runs=2)
        admitted = json.loads(res_json)

        evidence = admitted.get("evidence", {})
        version = evidence.get("version")
        self.assertIsNotNone(version)
        self.assertIs(type(version), int)
        self.assertNotIsInstance(version, bool)
        self.assertEqual(version, 1)

        self.assertEqual(evidence.get("verified_by"), "verifier_v1")
        self.assertEqual(evidence.get("runs"), 2)
        self.assertTrue(evidence.get("reproduced"))
        self.assertTrue(isinstance(evidence.get("sha256"), str))
        self.assertEqual(len(evidence.get("sha256")), 64)

        store = Store(self.store_dir)
        store.append_skill(admitted)
        loaded = store.load_skills()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["evidence"]["version"], 1)

    def test_replay_and_admit_version_lifecycle_across_revisions_reopen_and_compact(self):
        """replay_and_admit must increment evidence.version across revised records and compose with Store append/reopen/compact."""
        driver = TrackingDriver(self.st0, self.st1)

        # Initial admission (version 1)
        draft_v1 = copy.deepcopy(self.draft)
        res_json_v1 = replay_and_admit(draft_v1, driver, verified_by="verifier_v1", runs=2)
        admitted_v1 = json.loads(res_json_v1)

        store = Store(self.store_dir)
        store.append_skill(admitted_v1)

        # Revision (version 2)
        draft_v2 = copy.deepcopy(admitted_v1)
        draft_v2["intent"] = "revised intent for version 2"

        res_json_v2 = replay_and_admit(draft_v2, driver, verified_by="verifier_v2", runs=2)
        admitted_v2 = json.loads(res_json_v2)

        ev2 = admitted_v2.get("evidence", {})
        self.assertEqual(ev2.get("version"), 2)
        self.assertEqual(ev2.get("verified_by"), "verifier_v2")

        store.append_skill(admitted_v2)

        # Reopen store and verify
        store_reopened = Store(self.store_dir)
        loaded = store_reopened.load_skills()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["intent"], "revised intent for version 2")
        self.assertEqual(loaded[0]["evidence"]["version"], 2)

        # Compact store and verify
        store_reopened.compact()
        store_compacted = Store(self.store_dir)
        loaded_compact = store_compacted.load_skills()
        self.assertEqual(len(loaded_compact), 1)
        self.assertEqual(loaded_compact[0]["intent"], "revised intent for version 2")
        self.assertEqual(loaded_compact[0]["evidence"]["version"], 2)

    def test_replay_and_admit_rejects_runs_zero_before_driver_contact(self):
        """runs=0 must raise SkillRejected before driver reset, act or observe contact."""
        driver = TrackingDriver(self.st0, self.st1)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(copy.deepcopy(self.draft), driver, verified_by="verifier", runs=0)

        self.assertEqual(driver.act_count, 0)
        self.assertEqual(driver.observe_count, 0)
        self.assertEqual(driver.reset_count, 0)

    def test_replay_and_admit_rejects_runs_negative_before_driver_contact(self):
        """runs=-1 must raise SkillRejected before driver reset, act or observe contact."""
        driver = TrackingDriver(self.st0, self.st1)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(copy.deepcopy(self.draft), driver, verified_by="verifier", runs=-1)

        self.assertEqual(driver.act_count, 0)
        self.assertEqual(driver.observe_count, 0)
        self.assertEqual(driver.reset_count, 0)

    def test_replay_and_admit_rejects_runs_one_before_driver_contact(self):
        """runs=1 must raise SkillRejected before driver reset, act or observe contact."""
        driver = TrackingDriver(self.st0, self.st1)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(copy.deepcopy(self.draft), driver, verified_by="verifier", runs=1)

        self.assertEqual(driver.act_count, 0)
        self.assertEqual(driver.observe_count, 0)
        self.assertEqual(driver.reset_count, 0)

    def test_replay_and_admit_rejects_non_integer_runs_before_driver_contact(self):
        """Non-integer runs ('2', 1.5) must raise SkillRejected before driver contact."""
        driver = TrackingDriver(self.st0, self.st1)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(copy.deepcopy(self.draft), driver, verified_by="verifier", runs="2")

        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(copy.deepcopy(self.draft), driver, verified_by="verifier", runs=1.5)

        self.assertEqual(driver.act_count, 0)
        self.assertEqual(driver.observe_count, 0)
        self.assertEqual(driver.reset_count, 0)


if __name__ == "__main__":
    unittest.main()
