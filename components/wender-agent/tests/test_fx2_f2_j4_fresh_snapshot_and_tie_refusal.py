"""Unit tests for fresh snapshot, tie refusal, and authorization gates (fx2-f2-j4).

Tests irreversible authorization before initial dispatch and before retries using
ax.match on pruned fixtures. Verifies that initial-gate and retry-gate refusals
require no transport after refusal, and tests fresh backend substitution, post-dispatch
rename recovery, and reversible controls.
"""
import unittest

from wender.act.cdp_executor import ActionError, run_ax_action
from wender.perceive import ax


class FakeCdp:
    """Recording CDP fake transport for testing cdp_executor without browser interaction."""

    def __init__(self):
        self.calls = []

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "Runtime.callFunctionOn":
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 10, 50, 10, 50, 30, 10, 30]}}
        return {}


class ScriptedReader:
    """Sequence callable returning pruned node lists or raising exceptions."""

    def __init__(self, sequence):
        self.sequence = [list(s) if isinstance(s, (list, tuple)) else s for s in sequence]

    def __call__(self):
        if not self.sequence:
            raise IndexError("no more scripted snapshots")
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, type) and issubclass(item, Exception):
            raise item()
        return item


class TestFreshSnapshotAndTieRefusal(unittest.TestCase):

    def _make_target_node(self, name="Submit", backend_id=101, ordinal=0, ancestors=("main",), attrs=()):
        n = {
            "id": "t1",
            "backend_id": backend_id,
            "role": "button",
            "name": name,
            "depth": 1,
            "ancestors": ancestors,
            "attrs": attrs,
            "ordinal": ordinal,
            "cardinality": 1,
        }
        n["id"] = ax.stable_id(n)
        return n

    # --- Initial-gate Refusal Tests ---

    def test_initial_gate_refuses_when_fresh_reader_raises_exception(self):
        """Initial gate must refuse irreversible action when fresh reader raises exception without dispatching transport."""
        cdp = FakeCdp()
        target = self._make_target_node()
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        def bad_reader():
            raise StopIteration("fresh snapshot unreadable")

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", [target], action, bad_reader, retries=2, sleep=lambda s: None)

        self.assertEqual(cdp.calls, [])

    def test_initial_gate_refuses_when_fresh_snapshot_is_malformed(self):
        """Initial gate must refuse irreversible action when fresh reader returns a malformed snapshot."""
        cdp = FakeCdp()
        target = self._make_target_node()
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        def malformed_reader():
            return None

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp, "sess1", [target], action, malformed_reader, retries=2, sleep=lambda s: None
            )

        self.assertEqual(cdp.calls, [])

    def test_initial_gate_refuses_when_nodes_contain_tie(self):
        """Initial gate refuses when initial node snapshot contains a tie."""
        cdp = FakeCdp()
        target1 = self._make_target_node()
        target2 = self._make_target_node()
        action = {
            "op": "click",
            "target": target1["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp, "sess1", [target1, target2], action, lambda: [target1], retries=2, sleep=lambda s: None
            )

        self.assertEqual(cdp.calls, [])

    def test_initial_gate_refuses_when_fresh_snapshot_contains_tie(self):
        """Initial gate refuses when fresh snapshot introduces a tie."""
        cdp = FakeCdp()
        target1 = self._make_target_node()
        target2 = self._make_target_node()
        action = {
            "op": "click",
            "target": target1["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp, "sess1", [target1], action, lambda: [target1, target2], retries=2, sleep=lambda s: None
            )

        self.assertEqual(cdp.calls, [])

    def test_initial_gate_preserves_recorded_identity_rather_than_minting_from_fresh(self):
        """Initial gate must refuse when descriptor lacks recorded fp_exact, rather than minting from fresh."""
        cdp = FakeCdp()
        target = self._make_target_node()
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
            "describe": {"fp_exact": None},
        }

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp, "sess1", [target], action, lambda: [target], retries=2, sleep=lambda s: None
            )

        self.assertEqual(cdp.calls, [])

    # --- Pre-Retry Authorization Gate Refusal Tests ---

    def test_retry_gate_refuses_when_pre_retry_snapshot_contains_tie(self):
        """After recorded first attempt failure, retry must re-validate fresh gate and refuse on tie without further transport."""
        cdp = FakeCdp()
        target = self._make_target_node()
        tie1 = self._make_target_node()
        tie2 = self._make_target_node()

        # 1: initial fresh gate (ok), 2: post-attempt 1 snapshot (expectation target_name='Submitted' fails),
        # 3: pre-attempt 2 fresh snapshot (contains tie)
        reader = ScriptedReader([[target], [target], [tie1, tie2]])
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", [target], action, reader, retries=2, sleep=lambda s: None)

        # Attempt 1 made 3 transport calls (resolveNode, callFunctionOn, callFunctionOn). Attempt 2 must be refused before transport.
        self.assertEqual(len(cdp.calls), 3)

    def test_retry_gate_refuses_when_pre_retry_snapshot_downgrades_identity(self):
        """After recorded first attempt failure, retry must refuse when fresh snapshot match downgrades below L1."""
        cdp = FakeCdp()
        target = self._make_target_node()
        downgraded = self._make_target_node(ordinal=5)  # Shifts ordinal so exact/stable match fails, dropping to L2_NAME

        reader = ScriptedReader([[target], [target], [downgraded]])
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", [target], action, reader, retries=2, sleep=lambda s: None)

        self.assertEqual(len(cdp.calls), 3)

    def test_retry_gate_refuses_when_pre_retry_snapshot_backend_disappears(self):
        """After recorded first attempt failure, retry must refuse when target backend_id disappears in fresh snapshot."""
        cdp = FakeCdp()
        target = self._make_target_node()
        disappeared_backend = self._make_target_node(backend_id=None)

        reader = ScriptedReader([[target], [target], [disappeared_backend]])
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", [target], action, reader, retries=2, sleep=lambda s: None)

        self.assertEqual(len(cdp.calls), 3)

    def test_retry_gate_refuses_when_pre_retry_snapshot_reader_raises_exception(self):
        """After recorded first attempt failure, retry must refuse when pre-retry reader raises exception."""
        cdp = FakeCdp()
        target = self._make_target_node()

        reader = ScriptedReader([[target], [target], StopIteration()])
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", [target], action, reader, retries=2, sleep=lambda s: None)

        self.assertEqual(len(cdp.calls), 3)

    def test_retry_gate_refuses_when_pre_retry_snapshot_is_empty_or_malformed(self):
        """After recorded first attempt failure, retry must refuse when pre-retry reader returns an empty or malformed snapshot."""
        cdp = FakeCdp()
        target = self._make_target_node()

        reader = ScriptedReader([[target], [target], []])
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", [target], action, reader, retries=2, sleep=lambda s: None)

        self.assertEqual(len(cdp.calls), 3)

    # --- Passing Controls and Nominal Operations ---

    def test_fresh_backend_substitution_at_initial_gate(self):
        """Nominal control: fresh gate updates target_node with fresh backend_id when target node backend ID changed."""
        cdp = FakeCdp()
        initial_target = self._make_target_node(backend_id=101)
        fresh_target = self._make_target_node(backend_id=202)

        reader = ScriptedReader([[fresh_target], [fresh_target]])
        action = {
            "op": "click",
            "target": initial_target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submit"},
        }

        receipt = run_ax_action(cdp, "sess1", [initial_target], action, reader, retries=1, sleep=lambda s: None)

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        # Verify CDP call received fresh backend_id 202
        self.assertEqual(cdp.calls[0]["params"].get("backendNodeId"), 202)

    def test_post_dispatch_rename_recovery(self):
        """Nominal control: post-dispatch rename recovery satisfies expectation when element name changes."""
        cdp = FakeCdp()
        initial_target = self._make_target_node(name="Submit")

        renamed_name = ax.normalize_name("Submitted")
        post_target = self._make_target_node(name=renamed_name, attrs=(("pressed", "true"),))
        post_target["id"] = ax.stable_id(post_target)

        reader = ScriptedReader([[post_target]])
        action = {
            "op": "click",
            "target": initial_target["id"],
            "expect": {
                "target_name": "Submitted",
                "target_states": {"pressed": "true"},
            },
        }

        receipt = run_ax_action(
            cdp, "sess1", [initial_target], action, reader, retries=1, sleep=lambda s: None
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

    def test_reversible_control_succeeds(self):
        """Nominal control: reversible action succeeds without requiring irreversible match gates."""
        cdp = FakeCdp()
        target = self._make_target_node()

        reader = ScriptedReader([[target]])
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": False,
            "expect": {"target_name": "Submit"},
        }

        receipt = run_ax_action(
            cdp, "sess1", [target], action, reader, retries=1, sleep=lambda s: None
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)


if __name__ == "__main__":
    unittest.main()
