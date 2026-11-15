"""Unit tests pinning _dispatch_ax_op and run_ax_action resolution shapes and CDP replies."""
import unittest

from wender.act.cdp_executor import _dispatch_ax_op, run_ax_action


def _make_button_node(**overrides):
    node = {
        "id": "target",
        "backend_id": 101,
        "role": "button",
        "name": "Send",
        "depth": 1,
        "ancestors": ("main",),
        "attrs": (),
        "ordinal": 0,
        "cardinality": 1,
    }
    node.update(overrides)
    return node


class FakeCdp:
    """Recording transport double accepting send(method, params, session_id)."""

    def __init__(self, scripted=None):
        self.calls = []
        self.scripted = dict(scripted or {})

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        queue = self.scripted.get(method)
        if queue is not None:
            if isinstance(queue, list):
                item = queue.pop(0) if queue else {}
            else:
                item = queue
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item(payload, session_id)
            return dict(item)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "Runtime.callFunctionOn":
            return {}
        if method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        return {}


class TestResolveShapes(unittest.TestCase):
    def setUp(self):
        self.node = _make_button_node()
        self.nodes = [self.node]
        self.action = {"op": "click", "target": "target"}
        self.session_id = "sid-resolve-test"

    def test_explicit_object_null_raises_runtime_error_and_no_runtime_call(self):
        cdp = FakeCdp({"DOM.resolveNode": [{"object": None}]})
        with self.assertRaises(RuntimeError) as cm:
            _dispatch_ax_op(cdp, self.session_id, self.node, self.action)
        self.assertIn("null object", str(cm.exception))
        methods = [c["method"] for c in cdp.calls]
        self.assertIn("DOM.resolveNode", methods)
        self.assertNotIn("Runtime.callFunctionOn", methods)

    def test_explicit_object_empty_dict_raises_runtime_error_and_no_runtime_call(self):
        cdp = FakeCdp({"DOM.resolveNode": [{"object": {}}]})
        with self.assertRaises(RuntimeError) as cm:
            _dispatch_ax_op(cdp, self.session_id, self.node, self.action)
        self.assertIn("no objectId", str(cm.exception))
        methods = [c["method"] for c in cdp.calls]
        self.assertIn("DOM.resolveNode", methods)
        self.assertNotIn("Runtime.callFunctionOn", methods)

    def test_nonempty_top_level_reply_lacking_object_id_raises_runtime_error_and_no_runtime_call(self):
        cdp = FakeCdp({"DOM.resolveNode": [{"unrelated": "field"}]})
        with self.assertRaises(RuntimeError) as cm:
            _dispatch_ax_op(cdp, self.session_id, self.node, self.action)
        self.assertIn("no objectId", str(cm.exception))
        methods = [c["method"] for c in cdp.calls]
        self.assertIn("DOM.resolveNode", methods)
        self.assertNotIn("Runtime.callFunctionOn", methods)

    def test_empty_top_level_reply_synthesizes_object_id_compatibility_risk(self):
        """Preserve current empty top-level reply compatibility behavior.

        An empty top-level reply ({}) synthesizes object-101 for backwards compatibility,
        unlike an explicit empty object ({'object': {}}) which is rejected.
        Note: Synthesizing object-101 on an empty top-level reply is a compatibility risk
        rather than an explicit contract.
        """
        cdp = FakeCdp({"DOM.resolveNode": [{}]})
        _dispatch_ax_op(cdp, self.session_id, self.node, self.action)
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 1)
        self.assertEqual(runtime_calls[0]["params"]["objectId"], "object-101")

    def test_top_level_object_id_success_uses_supplied_identifier(self):
        cdp = FakeCdp({"DOM.resolveNode": [{"objectId": "top-obj-456"}]})
        _dispatch_ax_op(cdp, self.session_id, self.node, self.action)
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 1)
        self.assertEqual(runtime_calls[0]["params"]["objectId"], "top-obj-456")

    def test_nested_object_id_success_uses_supplied_identifier(self):
        cdp = FakeCdp({"DOM.resolveNode": [{"object": {"objectId": "nested-obj-789"}}]})
        _dispatch_ax_op(cdp, self.session_id, self.node, self.action)
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 1)
        self.assertEqual(runtime_calls[0]["params"]["objectId"], "nested-obj-789")

    def test_runtime_exception_details_text_boom_raises_runtime_error_boom_after_one_call(self):
        cdp = FakeCdp({"Runtime.callFunctionOn": [{"exceptionDetails": {"text": "boom"}}]})
        with self.assertRaises(RuntimeError) as cm:
            _dispatch_ax_op(cdp, self.session_id, self.node, self.action)
        self.assertIn("boom", str(cm.exception))
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 1)

    def test_run_ax_action_integration_with_no_op_sleep_and_scripted_read_nodes(self):
        cdp = FakeCdp({"DOM.resolveNode": [{"object": {"objectId": "obj-101"}}]})

        def deterministic_read_nodes():
            return [self.node]

        def no_op_sleep(seconds):
            pass

        receipt = run_ax_action(
            cdp,
            self.session_id,
            self.nodes,
            self.action,
            deterministic_read_nodes,
            retries=1,
            sleep=no_op_sleep,
        )
        self.assertEqual(receipt["ok"], 1)


if __name__ == "__main__":
    unittest.main()
