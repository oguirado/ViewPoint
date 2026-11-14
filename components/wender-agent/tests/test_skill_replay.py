"""Unit tests for wender.learn.replay (skills admission with verified replay)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from wender.remember import skills
from wender.state import schema

from wender.learn.explore import Driver
from wender.learn.replay import replay_and_admit


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


class FakeDriver(Driver):
    def __init__(self, states_sequence, flaky_run=None, flaky_step=None, flaky_state=None):
        self.states_sequence = list(states_sequence)
        self.flaky_run = flaky_run
        self.flaky_step = flaky_step
        self.flaky_state = flaky_state
        self.current_run = 0
        self.step_index = 0

    def reset(self):
        self.current_run += 1
        self.step_index = 0

    def observe(self):
        if (self.flaky_run == self.current_run and
                self.flaky_step == self.step_index and
                self.flaky_state is not None):
            st = self.flaky_state
        else:
            idx = min(self.step_index, len(self.states_sequence) - 1)
            st = self.states_sequence[idx]
        return st, "0123456789abcdef"

    def candidate_actions(self, state):
        return []

    def act(self, action):
        self.step_index += 1


class TestSkillReplay(unittest.TestCase):

    def setUp(self):
        self.st0 = _make_state([{"id": "0000000000000001", "role": "button", "name": "Start"}])
        self.st1 = _make_state([
            {"id": "0000000000000001", "role": "button", "name": "Start"},
            {"id": "0000000000000002", "role": "heading", "name": "Success"},
        ])
        self.valid_draft = {
            "id": "test-skill",
            "intent": "click start to see success",
            "params": {},
            "steps": [{"path": "ax", "op": "press", "target": "0000000000000001"}],
            "checkpoints": [
                {
                    "after_step": 0,
                    "expect": {"node_present": {"role": "heading", "name": "Success"}},
                }
            ],
            "provenance": {"source": "demo:test"},
        }

    def test_correct_draft_admitted(self):
        """Kills mutant: correct draft fail to admit or evidence.reproduced not set to True in returned canonical JSON."""
        driver = FakeDriver([self.st0, self.st1])
        res_json = replay_and_admit(dict(self.valid_draft), driver, verified_by="test-verifier", runs=2)
        self.assertIsInstance(res_json, str)
        self.assertIn('"reproduced":true', res_json)
        self.assertIn('"verified_by":"test-verifier"', res_json)

    def test_failing_checkpoint_in_run_2(self):
        """Kills mutant: failing checkpoint in run 2 ignored or SkillRejected exception reason missing run 2 identification."""
        flaky_st = _make_state([{"id": "0000000000000001", "role": "button", "name": "Start"}])
        driver = FakeDriver([self.st0, self.st1], flaky_run=2, flaky_step=1, flaky_state=flaky_st)
        with self.assertRaises(skills.SkillRejected) as cm:
            replay_and_admit(dict(self.valid_draft), driver, verified_by="test-verifier", runs=2)
        self.assertIn("run 2", str(cm.exception))

    def test_diff_summaries_mismatch_rejected(self):
        """Kills mutant: diff summary mismatch across runs undetected or allowing inconsistent state transition diffs."""
        alt_st1 = _make_state([
            {"id": "0000000000000001", "role": "button", "name": "Start"},
            {"id": "0000000000000002", "role": "heading", "name": "Success"},
            {"id": "0000000000000003", "role": "status", "name": "Extra"},
        ])
        driver = FakeDriver([self.st0, self.st1], flaky_run=2, flaky_step=1, flaky_state=alt_st1)
        with self.assertRaises(skills.SkillRejected) as cm:
            replay_and_admit(dict(self.valid_draft), driver, verified_by="test-verifier", runs=2)
        self.assertIn("diff summaries mismatch", str(cm.exception))

    def test_irreversible_step_refused(self):
        """Kills mutant: irreversible step executed without allow_irreversible true flag on draft."""
        draft = dict(self.valid_draft)
        draft["steps"] = [{"path": "ax", "op": "delete", "target": "0000000000000001", "irreversible": True}]
        driver = FakeDriver([self.st0, self.st1])
        with self.assertRaises(skills.SkillRejected) as cm:
            replay_and_admit(draft, driver, verified_by="test-verifier", runs=2)
        self.assertIn("irreversible", str(cm.exception))

        # Allowed when allow_irreversible is True
        draft_allowed = dict(draft)
        draft_allowed["allow_irreversible"] = True
        res_json = replay_and_admit(draft_allowed, driver, verified_by="test-verifier", runs=2)
        self.assertIn('"reproduced":true', res_json)

    def test_no_checkpoints_rejected(self):
        """Kills mutant: draft with empty or missing checkpoints accepted without validation."""
        draft = dict(self.valid_draft)
        draft["checkpoints"] = []
        driver = FakeDriver([self.st0, self.st1])
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(draft, driver, verified_by="test-verifier", runs=2)

    def test_evidence_sha256_stable(self):
        """Kills mutant: sha256 in evidence non-deterministic or unstable across multiple admission calls."""
        driver1 = FakeDriver([self.st0, self.st1])
        res1 = replay_and_admit(dict(self.valid_draft), driver1, verified_by="test-verifier", runs=2)

        driver2 = FakeDriver([self.st0, self.st1])
        res2 = replay_and_admit(dict(self.valid_draft), driver2, verified_by="test-verifier", runs=2)

        import json
        d1 = json.loads(res1)
        d2 = json.loads(res2)
        sha1 = d1["evidence"]["sha256"]
        sha2 = d2["evidence"]["sha256"]
        self.assertEqual(sha1, sha2)
        self.assertEqual(len(sha1), 64)

    def _assert_replay_writes_nothing(self, driver):
        # Replay/admission has no configured output directories. Observe its
        # private temp root recursively, never the shared system temp directory.
        def snapshot(root):
            return {
                os.path.relpath(os.path.join(parent, name), root)
                for parent, dirs, files in os.walk(root)
                for name in dirs + files
            }

        original_tempdir = tempfile.tempdir
        root = tempfile.mkdtemp(prefix="skill-replay-")
        try:
            with mock.patch.dict(os.environ, {"TMPDIR": root}):
                with mock.patch.object(tempfile, "tempdir", root):
                    before_files = snapshot(root)
                    replay_and_admit(dict(self.valid_draft), driver,
                                     verified_by="test-verifier", runs=2)
                    after_files = snapshot(root)
                    self.assertEqual(before_files, after_files)
        finally:
            # mkdtemp may populate tempfile's previously empty global cache.
            tempfile.tempdir = original_tempdir
            shutil.rmtree(root)

    def test_nothing_written_to_disk(self):
        """Kills mutant: replay or admission operation writing state, cache, or trace files to disk."""
        self._assert_replay_writes_nothing(FakeDriver([self.st0, self.st1]))

    def test_disk_write_check_rejects_injected_writer(self):
        """The same disk assertion must fail when replay's driver writes."""
        driver = FakeDriver([self.st0, self.st1])
        original_act = driver.act

        def writing_act(action):
            with open(os.path.join(tempfile.gettempdir(), "injected-write"), "w") as stream:
                stream.write("unexpected replay output")
            original_act(action)

        with mock.patch.object(driver, "act", side_effect=writing_act):
            with self.assertRaisesRegex(AssertionError, "injected-write"):
                self._assert_replay_writes_nothing(driver)


if __name__ == "__main__":
    unittest.main()
