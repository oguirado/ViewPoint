# @module {"id":"tests.test_jq1_a8_replay_store_version_roundtrip","layer":2,"owner":"LEARN","deps":["wa.learn.replay","wa.remember.store","wa.state.schema"]}
"""Integration regressions for R-A8-01 and D-A8-07 across replay_and_admit and Store.append_skill."""
import copy
import json
import tempfile
import unittest

from wender.learn.explore import Driver
from wender.learn.replay import replay_and_admit
from wender.remember.store import Store
from wender.remember.skills import SkillRejected
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


class FakeDriver(Driver):
    def __init__(self, state_before, state_after):
        self.state_before = state_before
        self.state_after = state_after
        self.step_index = 0

    def reset(self):
        self.step_index = 0

    def observe(self):
        st = self.state_before if self.step_index == 0 else self.state_after
        return st, "0123456789abcdef"

    def candidate_actions(self, state):
        return []

    def act(self, action):
        self.step_index += 1


class TestJQ1A8ReplayStoreVersionRoundtrip(unittest.TestCase):

    def setUp(self):
        self.st0 = _make_state([
            {"id": "0000000000000001", "role": "button", "name": "Start", "actionable": True}
        ])
        self.st1 = _make_state([
            {"id": "0000000000000001", "role": "button", "name": "Start", "actionable": True},
            {"id": "0000000000000002", "role": "heading", "name": "Done"}
        ])
        self.draft = {
            "id": "skill-jq1-a8-roundtrip",
            "intent": "execute ax action and reach done state",
            "params": {},
            "steps": [{"path": "ax", "op": "press", "target": "0000000000000001"}],
            "checkpoints": [
                {
                    "after_step": 0,
                    "expect": {"node_present": {"role": "heading", "name": "Done"}},
                }
            ],
            "provenance": {"source": "demo:roundtrip"},
        }

    def test_fake_driver_reproducible_trace(self):
        """Passing control test: verifies fake driver transitions predictably and resets reproducibly."""
        driver = FakeDriver(self.st0, self.st1)
        obs0, _ = driver.observe()
        self.assertEqual(obs0, self.st0)

        driver.act({"path": "ax", "op": "press", "target": "0000000000000001"})
        obs1, _ = driver.observe()
        self.assertEqual(obs1, self.st1)

        driver.reset()
        obs_reset, _ = driver.observe()
        self.assertEqual(obs_reset, self.st0)

    def test_replay_and_admit_initial_version_one(self):
        # Promoted R-A8-01: the first replay allocates exact integer version one.
        driver = FakeDriver(self.st0, self.st1)
        res_json = replay_and_admit(copy.deepcopy(self.draft), driver, verified_by="v1", runs=2)
        admitted = json.loads(res_json)

        evidence = admitted.get("evidence", {})
        version = evidence.get("version")
        self.assertIsNotNone(version)
        self.assertIs(type(version), int)
        self.assertNotIsInstance(version, bool)
        self.assertEqual(version, 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(tmpdir)
            store.append_skill(admitted)
            loaded = store.load_skills()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0], admitted)

    def test_replay_and_admit_increment_version_two_and_store_update(self):
        # Promoted D-A8-07: a new replay revision increments the prior version.
        driver = FakeDriver(self.st0, self.st1)
        initial_draft = copy.deepcopy(self.draft)
        initial_draft["evidence"] = {
            "reproduced": True,
            "verified_by": "v1",
            "runs": 2,
            "sha256": "0" * 64,
            "version": 1,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(tmpdir)
            store.append_skill(initial_draft)

            updated_draft = copy.deepcopy(initial_draft)
            updated_draft["intent"] = "updated intent for version 2"

            res_json_v2 = replay_and_admit(updated_draft, driver, verified_by="v2", runs=2)
            admitted_v2 = json.loads(res_json_v2)

            ev2 = admitted_v2.get("evidence", {})
            v2 = ev2.get("version")
            self.assertIsNotNone(v2)
            self.assertIs(type(v2), int)
            self.assertNotIsInstance(v2, bool)
            self.assertEqual(v2, 2)

            store.append_skill(admitted_v2)
            loaded = store.load_skills()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["intent"], "updated intent for version 2")
            self.assertEqual(loaded[0]["evidence"]["version"], 2)

    def test_invalid_prior_version_refused_before_driver_use(self):
        class NoCalls:
            def reset(self):
                self.fail_if_called()

            def observe(self):
                self.fail_if_called()

            def act(self, action):
                self.fail_if_called()

            def fail_if_called(self):
                raise AssertionError("invalid revision called the driver")

        for version in (None, False, True, 0, -1, "1", 1.0):
            with self.subTest(version=version):
                draft = copy.deepcopy(self.draft)
                draft["evidence"] = {"version": version}
                with self.assertRaises(SkillRejected):
                    replay_and_admit(draft, NoCalls(), "verifier")

    def test_evidence_metadata_and_input_survive_new_revision(self):
        draft = copy.deepcopy(self.draft)
        draft["evidence"] = {"version": 2, "verified_by": "prior",
                             "receipt": {"source": "metadata-only", "number": 8}}
        original = copy.deepcopy(draft)
        admitted = json.loads(replay_and_admit(draft, FakeDriver(self.st0, self.st1), "current"))
        self.assertEqual(admitted["evidence"]["version"], 3)
        self.assertEqual(admitted["evidence"]["verified_by"], "current")
        self.assertEqual(admitted["evidence"]["receipt"], original["evidence"]["receipt"])
        self.assertEqual(draft, original)


if __name__ == "__main__":
    unittest.main()
