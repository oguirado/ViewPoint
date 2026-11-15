"""Unit tests for CDP executor box resolution (_resolve_box) provenance and fallback behavior."""
import unittest

from wender.act.cdp_executor import _resolve_box


class FakeCdp:
    """Recording transport for CDP methods."""

    def __init__(self, scripted=None):
        self.calls = []
        self.scripted = dict(scripted or {})

    def send(self, method, params=None, session_id="test-session"):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method in self.scripted:
            item = self.scripted[method]
            if isinstance(item, list):
                reply = item.pop(0) if item else {}
            else:
                reply = item
            if isinstance(reply, Exception):
                raise reply
            return reply
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method in ("Runtime.callFunctionOn", "Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
            return {}
        return {}


def _make_node(**overrides):
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


class TestResolveBoxGeometryProvenance(unittest.TestCase):
    def test_valid_action_box_wins_over_node_box_and_makes_no_cdp_call(self):
        cdp = FakeCdp()
        node = _make_node(box=[100, 100, 50, 50])
        action = {"op": "click", "target": "target", "box": [10, 20, 30, 40]}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertEqual(res, [10, 20, 30, 40])
        self.assertEqual(cdp.calls, [])

    def test_zero_width_action_box_falls_through_to_valid_node_box_without_cdp_call(self):
        cdp = FakeCdp()
        node = _make_node(box=[100, 100, 50, 50])
        action = {"op": "click", "target": "target", "box": [10, 20, 0, 40]}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertEqual(res, [100, 100, 50, 50])
        self.assertEqual(cdp.calls, [])

    def test_invalid_action_and_node_box_requires_cdp_get_box_model(self):
        cdp = FakeCdp({"DOM.getBoxModel": {"box": [4, 8, 6, 10]}})
        node = _make_node(box=[0, 0, 0, 0])
        action = {"op": "click", "target": "target", "box": [0, 0, 0, 0]}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertEqual(res, [4, 8, 6, 10])
        self.assertEqual(len(cdp.calls), 1)
        self.assertEqual(cdp.calls[0]["method"], "DOM.getBoxModel")
        self.assertEqual(cdp.calls[0]["params"], {"backendNodeId": 101})
        self.assertEqual(cdp.calls[0]["session_id"], "sid-1")

    def test_direct_integer_box_in_cdp_reply(self):
        cdp = FakeCdp({"DOM.getBoxModel": {"box": [4, 8, 6, 10]}})
        node = _make_node()
        action = {"op": "click", "target": "target"}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertEqual(res, [4, 8, 6, 10])

    def test_content_quad_without_border_in_cdp_reply(self):
        cdp = FakeCdp({
            "DOM.getBoxModel": {
                "model": {
                    "content": [10, 20, 30, 20, 30, 40, 10, 40]
                }
            }
        })
        node = _make_node()
        action = {"op": "click", "target": "target"}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertEqual(res, [10, 20, 20, 20])

    def test_degenerate_quad_returns_none(self):
        cdp = FakeCdp({
            "DOM.getBoxModel": {
                "model": {
                    "content": [10, 20, 10, 20, 10, 20, 10, 20]
                }
            }
        })
        node = _make_node()
        action = {"op": "click", "target": "target"}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertIsNone(res)

    def test_wrong_length_quad_returns_none(self):
        cdp = FakeCdp({
            "DOM.getBoxModel": {
                "model": {
                    "content": [10, 20, 30, 40]
                }
            }
        })
        node = _make_node()
        action = {"op": "click", "target": "target"}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertIsNone(res)

    def test_non_dict_cdp_reply_returns_none(self):
        cdp = FakeCdp({"DOM.getBoxModel": "invalid_reply"})
        node = _make_node()
        action = {"op": "click", "target": "target"}
        res = _resolve_box(cdp, "sid-1", node, action)
        self.assertIsNone(res)

    def test_boolean_and_float_values_in_direct_box_are_invalid(self):
        invalid_boxes = [
            [4.0, 8, 6, 10],
            [4, 8.0, 6, 10],
            [4, 8, 6.0, 10],
            [4, 8, 6, 10.0],
            [True, 8, 6, 10],
            [4, False, 6, 10],
            [4, 8, True, 10],
            [4, 8, 6, False],
        ]
        for bad_box in invalid_boxes:
            # 1. Action box bad -> should not return bad_box directly
            cdp1 = FakeCdp({"DOM.getBoxModel": None})
            node1 = _make_node()
            action1 = {"op": "click", "target": "target", "box": bad_box}
            res1 = _resolve_box(cdp1, "sid-1", node1, action1)
            self.assertIsNone(res1)

            # 2. Direct CDP box reply bad -> should return None
            cdp2 = FakeCdp({"DOM.getBoxModel": {"box": bad_box}})
            node2 = _make_node()
            action2 = {"op": "click", "target": "target"}
            res2 = _resolve_box(cdp2, "sid-1", node2, action2)
            self.assertIsNone(res2)


if __name__ == "__main__":
    unittest.main()
