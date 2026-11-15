"""Adversarial pin tests for wender/act/cdp_executor.py (module wa.act.cdp_executor).

Contract:
- Function run_ax_action(cdp, session_id, nodes, action, read_nodes, retries=3, wait_ms=250, sleep=time.sleep, allow_coordinates=False)
- cdp send method: send(method, params, session_id)
- action: dict(op in ('click', 'focus', 'type', 'select'), target=<node_id>, text=..., value=..., expect=dict(target_states=..., target_name=...), irreversible=bool)
- raises ActionError only for bad input or refused irreversible actions. Never raises on verification failure.
- receipts: dict(ok=bool, path='ax'|'coordinates', attempts=int, verified=bool, pre_sha=str, post_sha=str, error=str|None)
"""
import os
import sys
import unittest
from unittest.mock import patch

SKIP_REASON = "module wender.act.cdp_executor absent (wa1-spark-cdp-executor in progress)"

try:
    import wender.act.cdp_executor as cdp_executor
    from wender.act.cdp_executor import run_ax_action, ActionError
    HAS_MODULE = True
except ImportError:
    HAS_MODULE = False
    cdp_executor = None
    run_ax_action = None
    ActionError = None

ALLOW_SKIP = os.environ.get("WA1_PIN_ALLOW_SKIP") == "1"

if not HAS_MODULE and not ALLOW_SKIP:
    raise ImportError("wender.act.cdp_executor missing and WA1_PIN_ALLOW_SKIP != 1")


class FakeCdp:
    """Deterministic fake CDP transport recording commands and returning controlled replies."""
    def __init__(self, fail_runtime_calls=0):
        self.calls = []
        self.fail_runtime_calls = fail_runtime_calls
        self._runtime_call_count = 0

    def send(self, method, params=None, session_id=None):
        params = params or {}
        self.calls.append({"method": method, "params": params, "session_id": session_id})
        if method == "DOM.scrollIntoViewIfNeeded":
            return {}
        elif method == "DOM.resolveNode":
            return {"object": {"objectId": "obj_123"}}
        elif method == "Runtime.callFunctionOn":
            self._runtime_call_count += 1
            if self._runtime_call_count <= self.fail_runtime_calls:
                from wender.perceive.cdp_pipe import CdpError
                raise CdpError("Runtime.callFunctionOn simulated error")
            return {"result": {"value": True}}
        elif method == "Input.dispatchMouseEvent":
            return {}
        else:
            return {}


@unittest.skipIf(not HAS_MODULE and ALLOW_SKIP, SKIP_REASON)
class TestWa1ExecutorPins(unittest.TestCase):

    def _sample_nodes(self):
        return [
            {
                "id": "node_btn_1",
                "backend_id": 101,
                "role": "button",
                "name": "Submit",
                "depth": 1,
                "ancestors": ("main",),
                "attrs": (("disabled", "false"),),
                "ordinal": 0,
                "cardinality": 1,
                "box": [10, 20, 100, 40],
            },
            {
                "id": "node_txt_1",
                "backend_id": 102,
                "role": "textbox",
                "name": "Username",
                "depth": 1,
                "ancestors": ("main",),
                "attrs": (),
                "ordinal": 0,
                "cardinality": 1,
                "box": [10, 70, 200, 30],
            },
            {
                "id": "node_sel_1",
                "backend_id": 103,
                "role": "combobox",
                "name": "Country",
                "depth": 1,
                "ancestors": ("main",),
                "attrs": (),
                "ordinal": 0,
                "cardinality": 1,
                "box": [10, 110, 150, 30],
            },
        ]

    def test_unknown_target_id_raises_action_error(self):
        """Kills mutant: missing target id check or failing to raise ActionError when target id is absent."""
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        action = {"op": "click", "target": "non_existent_id"}

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", nodes, action, lambda: nodes)

        self.assertEqual(cdp.calls, [])

    def test_irreversible_match_gate_checks_initial_and_fresh_snapshots(self):
        for stage in ("initial", "fresh"):
            for level, tie in (("L0_EXACT", True), ("L1_STABLE", True),
                               ("L3_VARIANT", False)):
                with self.subTest(stage=stage, level=level, tie=tie):
                    cdp = FakeCdp()
                    nodes = self._sample_nodes()
                    strong = {"level": "L0_EXACT", "tie": False, "node": nodes[0]}
                    unsafe = {"level": level, "tie": tie, "node": nodes[0]}
                    matches = [unsafe] if stage == "initial" else [strong, unsafe]
                    action = {"op": "click", "target": nodes[0]["id"], "irreversible": True}
                    with patch.object(cdp_executor.ax, "match", side_effect=matches):
                        with self.assertRaises(ActionError):
                            run_ax_action(cdp, "sess1", nodes, action, lambda: nodes,
                                          allow_coordinates=True, sleep=lambda s: None)
                    self.assertEqual(cdp.calls, [])

    def test_reversible_action_does_not_require_strong_unique_match(self):
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        nodes.append(dict(nodes[0], id="duplicate", backend_id=104))
        action = {"op": "click", "target": nodes[0]["id"]}
        with patch.object(cdp_executor.ax, "match", side_effect=AssertionError("unexpected gate")):
            receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes,
                                    sleep=lambda s: None)
        self.assertTrue(receipt["ok"])
        self.assertTrue(any(c["method"] == "Runtime.callFunctionOn" for c in cdp.calls))

    def test_node_null_backend_id_raises_action_error(self):
        """Kills mutant: missing check for null backend_id or failing to raise ActionError when backend_id is None."""
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        nodes[0]["backend_id"] = None
        action = {"op": "click", "target": "node_btn_1"}

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", nodes, action, lambda: nodes)

    def test_irreversible_action_refused_on_tie_or_l3_match(self):
        """Kills mutant: bypassing wender.perceive.ax.match / level_allows_irreversible or allowing L3 / tie for irreversible actions."""
        cdp = FakeCdp()
        # Duplicate node to create a tie / non-L0/L1 match
        nodes = [
            {
                "id": "node_btn_dup",
                "backend_id": 101,
                "role": "button",
                "name": "Delete Account",
                "depth": 1,
                "ancestors": ("main",),
                "attrs": (),
                "ordinal": 0,
                "cardinality": 2,
            },
            {
                "id": "node_btn_dup_2",
                "backend_id": 102,
                "role": "button",
                "name": "Delete Account",
                "depth": 1,
                "ancestors": ("main",),
                "attrs": (),
                "ordinal": 0,  # duplicate ordinal and fingerprints causes tie
                "cardinality": 2,
            },
        ]
        action = {"op": "click", "target": "node_btn_dup", "irreversible": True}

        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", nodes, action, lambda: nodes)
        self.assertEqual(cdp.calls, [])

    def test_retries_exactly_3_sends_when_every_attempt_fails(self):
        """Kills mutant: off-by-one in retry loop or wrong retry limit when Runtime.callFunctionOn fails."""
        cdp = FakeCdp(fail_runtime_calls=10)
        nodes = self._sample_nodes()
        action = {"op": "click", "target": "node_btn_1"}

        slept = []
        receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, retries=3, sleep=lambda s: slept.append(s))

        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 3)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["attempts"], 3)

    def test_success_on_attempt_2_reports_attempts_2(self):
        """Kills mutant: attempts counter hardcoded to 1 or total retries count instead of actual attempt count."""
        cdp = FakeCdp(fail_runtime_calls=1)
        nodes = self._sample_nodes()
        action = {"op": "click", "target": "node_btn_1"}

        receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, retries=3, sleep=lambda s: None)

        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["attempts"], 2)

    def test_verification_compares_states_as_strings(self):
        """Kills mutant: state attr verification comparing booleans directly or skipping string conversion."""
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        # Initial node state disabled="false"
        action = {
            "op": "click",
            "target": "node_btn_1",
            "expect": {"target_states": {"disabled": "false"}}
        }

        # read_nodes returns node with disabled="false"
        receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, sleep=lambda s: None)
        self.assertTrue(receipt["verified"])

        # Expect state disabled="true" while node has disabled="false"
        action_fail = {
            "op": "click",
            "target": "node_btn_1",
            "expect": {"target_states": {"disabled": "true"}}
        }
        receipt_fail = run_ax_action(cdp, "sess1", nodes, action_fail, lambda: nodes, sleep=lambda s: None)
        self.assertFalse(receipt_fail["verified"])

    def test_type_action_with_unicode_text(self):
        """Kills mutant: improper unicode text handling or string encoding bug in JS call arguments for type op."""
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        action = {"op": "type", "target": "node_txt_1", "text": "Héllo 世界 🌍"}

        receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, sleep=lambda s: None)

        self.assertTrue(receipt["ok"])
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 1)
        args = runtime_calls[0]["params"].get("arguments", [])
        self.assertTrue(any(arg.get("value") == "Héllo 世界 🌍" for arg in args))

    def test_select_action_dispatches_change(self):
        """Kills mutant: select op missing value argument or failing to pass value parameter."""
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        action = {"op": "select", "target": "node_sel_1", "value": "UY"}

        receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, sleep=lambda s: None)

        self.assertTrue(receipt["ok"])
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 1)
        args = runtime_calls[0]["params"].get("arguments", [])
        self.assertTrue(any(arg.get("value") == "UY" for arg in args))

    def test_coordinates_fallback_forbidden_for_irreversible(self):
        """Kills mutant: fallback to mouse coordinates when action is irreversible."""
        cdp = FakeCdp(fail_runtime_calls=10)
        nodes = self._sample_nodes()
        action = {"op": "click", "target": "node_btn_1", "irreversible": True}

        receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, allow_coordinates=True, sleep=lambda s: None)

        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["path"], "ax")
        mouse_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_calls), 0)

    def test_receipt_has_no_floats(self):
        """Kills mutant: float leaking into output receipt dict."""
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        action = {"op": "click", "target": "node_btn_1"}

        receipt = run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, sleep=lambda s: None)

        for key, val in receipt.items():
            self.assertNotIsInstance(val, float, f"Field {key} in receipt is a float: {val}")

    def test_sleep_called_with_wait_ms_divided_by_1000(self):
        """Kills mutant: passing wait_ms directly as seconds instead of wait_ms / 1000.0."""
        cdp = FakeCdp()
        nodes = self._sample_nodes()
        action = {"op": "click", "target": "node_btn_1"}

        slept = []
        run_ax_action(cdp, "sess1", nodes, action, lambda: nodes, wait_ms=250, sleep=slept.append)

        self.assertIn(0.25, slept)
        self.assertNotIn(250, slept)


if __name__ == "__main__":
    unittest.main()
