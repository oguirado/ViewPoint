"""Pinned coordinate fallback failure receipt tests for wender.act.cdp_executor."""
import unittest
from wender.act.cdp_executor import run_ax_action
from wender.perceive import ax
from wender.state import schema


class FakeCdp:
    """Recording FakeCDP transport for testing coordinate fallback failures."""

    def __init__(self, box_model_reply=None, raise_on_mouse_pressed=False):
        self.calls = []
        self.box_model_reply = box_model_reply if box_model_reply is not None else {}
        self.raise_on_mouse_pressed = raise_on_mouse_pressed

    def send(self, method, params=None, session_id=None):
        params = params or {}
        self.calls.append({"method": method, "params": params, "session_id": session_id})
        if method == "DOM.scrollIntoViewIfNeeded":
            return {}
        elif method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        elif method == "Runtime.callFunctionOn":
            raise RuntimeError("blocked")
        elif method == "DOM.getBoxModel":
            return self.box_model_reply
        elif method == "Input.dispatchMouseEvent":
            if params.get("type") == "mousePressed" and self.raise_on_mouse_pressed:
                raise RuntimeError("mousePressed failed")
            return {}
        return {}


class TestJq1A7FallbackFailures(unittest.TestCase):

    def _sample_target_node(self, pressed="false"):
        return {
            "id": "target",
            "backend_id": 101,
            "role": "button",
            "name": "Send",
            "depth": 1,
            "ancestors": ("main",),
            "attrs": (("pressed", pressed),),
            "ordinal": 0,
            "cardinality": 1,
        }

    def test_fallback_failure_empty_box(self):
        """Missing usable box with DOM.getBoxModel returning {} results in coordinate fallback failure."""
        nodes = [self._sample_target_node(pressed="false")]
        action = {
            "op": "click",
            "target": "target",
            "expect": {"target_states": {"pressed": "true"}},
        }
        cdp = FakeCdp(box_model_reply={})

        read_nodes_sequence = [nodes]
        read_nodes_idx = 0

        def read_nodes():
            nonlocal read_nodes_idx
            res = read_nodes_sequence[min(read_nodes_idx, len(read_nodes_sequence) - 1)]
            read_nodes_idx += 1
            return res

        receipt = run_ax_action(
            cdp,
            "sess_123",
            nodes,
            action,
            read_nodes,
            retries=1,
            wait_ms=250,
            sleep=lambda s: None,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)
        self.assertEqual(receipt["path"], "coordinates")
        self.assertEqual(receipt["attempts"], 2)
        self.assertTrue(isinstance(receipt["error"], str) and len(receipt["error"]) > 0)
        self.assertEqual(receipt["pre_sha"], ax.snapshot_sha(nodes, True))
        self.assertEqual(receipt["post_sha"], ax.snapshot_sha(nodes, True))

        self.assertEqual(
            set(receipt.keys()),
            {"ok", "path", "attempts", "verified", "pre_sha", "post_sha", "error"},
        )
        for k, v in receipt.items():
            self.assertNotIsInstance(v, float)
            if k in ("ok", "attempts", "verified"):
                self.assertIsInstance(v, int)
                self.assertNotIsInstance(v, bool)

        serialized = schema.dumps(receipt)
        self.assertIsInstance(serialized, str)

    def test_fallback_failure_mousePressed_exception(self):
        """Exception on mousePressed results in fallback failure without emitting mouseReleased.

        Note on cleanup risk: Under the current implementation, an exception raised during
        mousePressed immediately aborts execution without calling mouseReleased. This leaves
        mouse event state unreleased, posing a cleanup risk rather than serving as a recommended
        universal release policy.
        """
        nodes = [self._sample_target_node(pressed="false")]
        action = {
            "op": "click",
            "target": "target",
            "expect": {"target_states": {"pressed": "true"}},
        }
        box_reply = {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        cdp = FakeCdp(box_model_reply=box_reply, raise_on_mouse_pressed=True)

        read_nodes_sequence = [nodes]
        read_nodes_idx = 0

        def read_nodes():
            nonlocal read_nodes_idx
            res = read_nodes_sequence[min(read_nodes_idx, len(read_nodes_sequence) - 1)]
            read_nodes_idx += 1
            return res

        receipt = run_ax_action(
            cdp,
            "sess_123",
            nodes,
            action,
            read_nodes,
            retries=1,
            wait_ms=250,
            sleep=lambda s: None,
            allow_coordinates=True,
        )

        mouse_events = [
            c["params"].get("type")
            for c in cdp.calls
            if c["method"] == "Input.dispatchMouseEvent"
        ]
        self.assertIn("mouseMoved", mouse_events)
        self.assertIn("mousePressed", mouse_events)
        self.assertNotIn("mouseReleased", mouse_events)

        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)
        self.assertEqual(receipt["path"], "coordinates")
        self.assertEqual(receipt["attempts"], 2)
        self.assertTrue(isinstance(receipt["error"], str) and len(receipt["error"]) > 0)
        self.assertEqual(receipt["pre_sha"], ax.snapshot_sha(nodes, True))
        self.assertEqual(receipt["post_sha"], ax.snapshot_sha(nodes, True))

        self.assertEqual(
            set(receipt.keys()),
            {"ok", "path", "attempts", "verified", "pre_sha", "post_sha", "error"},
        )
        for k, v in receipt.items():
            self.assertNotIsInstance(v, float)
            if k in ("ok", "attempts", "verified"):
                self.assertIsInstance(v, int)
                self.assertNotIsInstance(v, bool)

        serialized = schema.dumps(receipt)
        self.assertIsInstance(serialized, str)

    def test_fallback_failure_complete_pointer_triple_unchanged_state(self):
        """Complete pointer triple dispatched but state remains unchanged (pressed=false), failing expectation."""
        nodes = [self._sample_target_node(pressed="false")]
        action = {
            "op": "click",
            "target": "target",
            "expect": {"target_states": {"pressed": "true"}},
        }
        box_reply = {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        cdp = FakeCdp(box_model_reply=box_reply)

        read_nodes_sequence = [nodes]
        read_nodes_idx = 0

        def read_nodes():
            nonlocal read_nodes_idx
            res = read_nodes_sequence[min(read_nodes_idx, len(read_nodes_sequence) - 1)]
            read_nodes_idx += 1
            return res

        receipt = run_ax_action(
            cdp,
            "sess_123",
            nodes,
            action,
            read_nodes,
            retries=1,
            wait_ms=250,
            sleep=lambda s: None,
            allow_coordinates=True,
        )

        mouse_events = [
            c["params"].get("type")
            for c in cdp.calls
            if c["method"] == "Input.dispatchMouseEvent"
        ]
        self.assertEqual(mouse_events, ["mouseMoved", "mousePressed", "mouseReleased"])

        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)
        self.assertEqual(receipt["path"], "coordinates")
        self.assertEqual(receipt["attempts"], 2)
        self.assertEqual(receipt["error"], "expectation not satisfied")
        self.assertEqual(receipt["pre_sha"], ax.snapshot_sha(nodes, True))
        self.assertEqual(receipt["post_sha"], ax.snapshot_sha(nodes, True))

        self.assertEqual(
            set(receipt.keys()),
            {"ok", "path", "attempts", "verified", "pre_sha", "post_sha", "error"},
        )
        for k, v in receipt.items():
            self.assertNotIsInstance(v, float)
            if k in ("ok", "attempts", "verified"):
                self.assertIsInstance(v, int)
                self.assertNotIsInstance(v, bool)

        serialized = schema.dumps(receipt)
        self.assertIsInstance(serialized, str)


if __name__ == "__main__":
    unittest.main()
