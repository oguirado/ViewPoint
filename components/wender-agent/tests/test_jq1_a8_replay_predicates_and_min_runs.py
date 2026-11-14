"""Unit tests for wender.learn.replay (D-A8-08 predicate evaluations and minimum runs requirements)."""
import copy
import hashlib
import json
import unittest

from wender.learn.explore import Driver
from wender.learn.replay import _eval_expect, replay_and_admit
from wender.remember import skills
from wender.state import schema


def _make_state_with_done():
    pruned_nodes = [
        {"id": "0000000000000001", "role": "button", "name": "Start", "depth": 0, "ordinal": 0, "attrs": []},
        {"id": "0000000000000002", "role": "heading", "name": "Done", "depth": 0, "ordinal": 1, "attrs": []},
    ]
    boxes = {
        "0000000000000001": [0, 0, 10, 10],
        "0000000000000002": [0, 10, 10, 10],
    }
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
        actionable_roles=frozenset({"button"}),
    )


def _make_state_without_done():
    pruned_nodes = [
        {"id": "0000000000000001", "role": "button", "name": "Start", "depth": 0, "ordinal": 0, "attrs": []},
    ]
    boxes = {
        "0000000000000001": [0, 0, 10, 10],
    }
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
        actionable_roles=frozenset({"button"}),
    )


def _make_state_wrong_role():
    pruned_nodes = [
        {"id": "0000000000000003", "role": "button", "name": "Done", "depth": 0, "ordinal": 0, "attrs": []},
    ]
    boxes = {
        "0000000000000003": [0, 0, 10, 10],
    }
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
        actionable_roles=frozenset({"button"}),
    )


def _make_state_missing_text():
    pruned_nodes = [
        {"id": "0000000000000004", "role": "heading", "name": "Welcome", "depth": 0, "ordinal": 0, "attrs": []},
    ]
    boxes = {
        "0000000000000004": [0, 0, 10, 10],
    }
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
        actionable_roles=frozenset(),
    )


class BareStateDriver(Driver):
    """Driver whose observe() returns a bare state dictionary rather than a tuple."""

    def __init__(self, pre_state, post_state):
        self.pre_state = pre_state
        self.post_state = post_state
        self.current_run = 0
        self.step_index = 0
        self.act_count = 0

    def reset(self):
        self.current_run += 1
        self.step_index = 0

    def observe(self):
        if self.step_index == 0:
            return self.pre_state
        return self.post_state

    def candidate_actions(self, state):
        return []

    def act(self, action):
        self.act_count += 1
        self.step_index += 1


class DivergingDriver(Driver):
    """Driver that diverges in run 2."""

    def __init__(self, pre_state, post_state_run1, post_state_run2):
        self.pre_state = pre_state
        self.post_state_run1 = post_state_run1
        self.post_state_run2 = post_state_run2
        self.current_run = 0
        self.step_index = 0
        self.act_count = 0

    def reset(self):
        self.current_run += 1
        self.step_index = 0

    def observe(self):
        if self.step_index == 0:
            return self.pre_state
        if self.current_run == 1:
            return self.post_state_run1
        return self.post_state_run2

    def candidate_actions(self, state):
        return []

    def act(self, action):
        self.act_count += 1
        self.step_index += 1


class TestReplayPredicatesAndMinRuns(unittest.TestCase):

    def setUp(self):
        self.st_with_done = _make_state_with_done()
        self.st_without_done = _make_state_without_done()
        self.st_wrong_role = _make_state_wrong_role()
        self.st_missing_text = _make_state_missing_text()

        self.valid_draft = {
            "id": "test-skill-jq1-a8",
            "intent": "click start to reach done",
            "params": {},
            "steps": [{"path": "ax", "op": "press", "target": "0000000000000001"}],
            "checkpoints": [
                {
                    "after": 0,
                    "expect": {"text_contains": {"role": "heading", "name": "Done"}},
                }
            ],
            "provenance": {"source": "demo:test"},
        }

    # --- Predicate Evaluation Tests (_eval_expect) ---

    def test_node_absent_dict_matching_and_absent(self):
        expect = {"node_absent": {"role": "heading", "name": "Done"}}
        self.assertFalse(_eval_expect(expect, self.st_with_done))
        self.assertTrue(_eval_expect(expect, self.st_without_done))

    def test_node_absent_list_matching_and_absent(self):
        expect = {"node_absent": [{"role": "heading", "name": "Done"}]}
        self.assertFalse(_eval_expect(expect, self.st_with_done))
        self.assertTrue(_eval_expect(expect, self.st_without_done))

    def test_text_contains_string_done(self):
        expect = {"text_contains": "Done"}
        self.assertTrue(_eval_expect(expect, self.st_with_done))
        self.assertFalse(_eval_expect(expect, self.st_without_done))

    def test_text_contains_role_heading_with_text_done(self):
        expect = {"text_contains": {"role": "heading", "text": "Done"}}
        self.assertTrue(_eval_expect(expect, self.st_with_done))
        self.assertFalse(_eval_expect(expect, self.st_without_done))

    def test_text_contains_role_heading_with_contains_done(self):
        expect = {"text_contains": {"role": "heading", "contains": "Done"}}
        self.assertTrue(_eval_expect(expect, self.st_with_done))
        self.assertFalse(_eval_expect(expect, self.st_without_done))

    def test_text_contains_role_heading_with_name_done(self):
        expect = {"text_contains": {"role": "heading", "name": "Done"}}
        self.assertTrue(_eval_expect(expect, self.st_with_done))
        self.assertFalse(_eval_expect(expect, self.st_without_done))

    def test_text_contains_role_only_predicate(self):
        expect = {"text_contains": {"role": "heading"}}
        self.assertTrue(_eval_expect(expect, self.st_with_done))
        self.assertFalse(_eval_expect(expect, self.st_without_done))

    def test_text_contains_wrong_role_negative(self):
        expect = {"text_contains": {"role": "heading", "text": "Done"}}
        self.assertFalse(_eval_expect(expect, self.st_wrong_role))

    def test_text_contains_missing_text_negative(self):
        expect = {"text_contains": {"role": "heading", "text": "Done"}}
        self.assertFalse(_eval_expect(expect, self.st_missing_text))

    def test_non_dict_predicate(self):
        expect_absent_non_dict = {"node_absent": "not_a_dict"}
        self.assertTrue(_eval_expect(expect_absent_non_dict, self.st_with_done))

        expect_present_non_dict = {"node_present": 12345}
        self.assertFalse(_eval_expect(expect_present_non_dict, self.st_with_done))

    # --- Checkpoint & Replay Admission Controls ---

    def test_checkpoint_after_zero_and_bare_state_dict(self):
        driver = BareStateDriver(self.st_without_done, self.st_with_done)
        res_json = replay_and_admit(dict(self.valid_draft), driver, verified_by="verifier", runs=2)
        self.assertIsInstance(res_json, str)
        self.assertIn('"reproduced":true', res_json)

    def test_failed_checkpoint_predicate_raises_skill_rejected(self):
        driver = BareStateDriver(self.st_without_done, self.st_without_done)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(dict(self.valid_draft), driver, verified_by="verifier", runs=2)

    # --- Promoted regression tests (D-A8-08 Minimum Runs) ---

    def test_min_runs_zero_rejected(self):
        # Zero runs must reject before driver actions.
        driver = BareStateDriver(self.st_without_done, self.st_with_done)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(dict(self.valid_draft), driver, verified_by="verifier", runs=0)
        self.assertEqual(driver.act_count, 0)

    def test_min_runs_negative_one_rejected(self):
        # Negative runs must reject before driver actions.
        driver = BareStateDriver(self.st_without_done, self.st_with_done)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(dict(self.valid_draft), driver, verified_by="verifier", runs=-1)
        self.assertEqual(driver.act_count, 0)

    def test_min_runs_one_rejected_single_run_stability_hole(self):
        # One run cannot establish stability against a second-run divergence.
        driver = DivergingDriver(self.st_without_done, self.st_with_done, self.st_without_done)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(dict(self.valid_draft), driver, verified_by="verifier", runs=1)
        self.assertEqual(driver.act_count, 0)

    def test_run_count_requires_exact_integer_before_reset(self):
        for runs in (False, True, 2.0, "2", None):
            with self.subTest(runs=runs):
                driver = BareStateDriver(self.st_without_done, self.st_with_done)
                with self.assertRaises(skills.SkillRejected):
                    replay_and_admit(copy.deepcopy(self.valid_draft), driver, "verifier", runs=runs)
                self.assertEqual(driver.act_count, 0)
                self.assertEqual(driver.current_run, 0)

    def test_unmatched_or_unevaluated_checkpoints_cannot_create_evidence(self):
        bad_checkpoints = [
            {"after_step": True, "expect": {"node_present": {"role": "heading"}}},
            {"after_step": 1, "expect": {"node_present": {"role": "heading"}}},
            {"after_step": 0, "after": 1, "expect": {"node_present": {"role": "heading"}}},
            {"expect": {"node_present": {"role": "heading"}}},
            {"after_step": 0, "expect": {}},
            {"after_step": 0, "expect": {"unknown": True}},
            {"after_step": 0, "expect": {"node_absent": "not-a-predicate"}},
            {"after_step": 0, "expect": {"text_contains": []}},
            {"after_step": 0, "expected_post": {"screen": "Done"}},
        ]
        for checkpoint in bad_checkpoints:
            with self.subTest(checkpoint=checkpoint):
                draft = copy.deepcopy(self.valid_draft)
                draft["checkpoints"] = [checkpoint]
                driver = BareStateDriver(self.st_without_done, self.st_with_done)
                with self.assertRaises(skills.SkillRejected):
                    replay_and_admit(draft, driver, "verifier")
                self.assertEqual(driver.act_count, 0)
                self.assertEqual(driver.current_run, 0)

    def test_driver_action_mutation_does_not_rewrite_draft_or_admitted_steps(self):
        class MutatingDriver(BareStateDriver):
            def act(self, action):
                action["target"] = "driver-owned-change"
                action["nested"]["label"] = "driver-owned-change"
                super().act(action)

        draft = copy.deepcopy(self.valid_draft)
        draft["steps"][0]["nested"] = {"label": "original"}
        before = copy.deepcopy(draft)
        driver = MutatingDriver(self.st_without_done, self.st_with_done)
        admitted = json.loads(replay_and_admit(draft, driver, "verifier"))
        self.assertEqual(driver.act_count, 2)
        self.assertEqual(draft, before)
        self.assertEqual(admitted["steps"], before["steps"])

    def test_failed_replay_does_not_attach_or_rewrite_evidence(self):
        draft = copy.deepcopy(self.valid_draft)
        draft["evidence"] = {"version": 3, "verified_by": "previous", "note": {"value": "keep"}}
        before = copy.deepcopy(draft)
        driver = BareStateDriver(self.st_without_done, self.st_without_done)
        with self.assertRaises(skills.SkillRejected):
            replay_and_admit(draft, driver, "verifier")
        self.assertEqual(draft, before)

    def test_reused_driver_state_retains_the_observed_pre_action_snapshot(self):
        class SharedStateDriver(BareStateDriver):
            def __init__(self, pre_state, post_state):
                super().__init__(pre_state, post_state)
                self.shared = {}

            def reset(self):
                super().reset()
                self.shared.clear()
                self.shared.update(copy.deepcopy(self.pre_state))

            def observe(self):
                return self.shared

            def act(self, action):
                super().act(action)
                self.shared.clear()
                self.shared.update(copy.deepcopy(self.post_state))

        admitted = json.loads(replay_and_admit(
            copy.deepcopy(self.valid_draft),
            SharedStateDriver(self.st_without_done, self.st_with_done), "verifier"))
        expected_bytes = b'["plus 1 minus 0 changed 0"]'
        self.assertEqual(admitted["evidence"]["sha256"], hashlib.sha256(expected_bytes).hexdigest())


if __name__ == "__main__":
    unittest.main()
