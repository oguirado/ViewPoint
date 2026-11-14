"""Unit test pinning stale-geometry defense in reversible click fallback."""
import unittest

from wender.act.cdp_executor import run_ax_action


class FakeCdp:
    """Recording FakeCdp double with scripted DOM.getBoxModel support."""

    def __init__(self, scripted_box_model=None):
        self.calls = []
        self.scripted_box_model = list(scripted_box_model or [])

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "Runtime.callFunctionOn":
            return {}
        if method == "DOM.getBoxModel":
            if self.scripted_box_model:
                return self.scripted_box_model.pop(0)
            return {}
        return {}


def _make_node(*, box, pressed="false"):
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
        "box": list(box),
    }


class TestFreshGeometry(unittest.TestCase):
    def test_fresh_geometry_in_reversible_click_fallback(self):
        initial_target = _make_node(box=[0, 0, 10, 10], pressed="false")
        read1_target = _make_node(box=[40, 60, 20, 10], pressed="false")
        read2_target = _make_node(box=[40, 60, 20, 10], pressed="false")
        read3_target = _make_node(box=[40, 60, 20, 10], pressed="true")

        # Retry and coordinate fallback each re-read identity before dispatch
        # (29ee5dd). Keep both AX post-states unpressed to reach fallback.
        read_sequence = [[read1_target], [read2_target], [read2_target],
                         [read2_target], [read3_target]]

        def deterministic_read_nodes():
            if not read_sequence:
                raise IndexError("no more scripted snapshots")
            return list(read_sequence.pop(0))

        cdp = FakeCdp()

        receipt = run_ax_action(
            cdp,
            "session-fresh-geom",
            [initial_target],
            {
                "op": "click",
                "target": "target",
                "expect": {"target_states": {"pressed": "true"}},
                "allow_coordinates": True,
            },
            deterministic_read_nodes,
            retries=2,
            allow_coordinates=True,
            sleep=lambda seconds: None,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["path"], "coordinates")
        self.assertEqual(receipt["attempts"], 3)
        self.assertEqual(receipt["verified"], 1)
        self.assertIsNone(receipt["error"])

        # Ensure DOM.getBoxModel was NOT called because refreshed node has valid box
        methods_called = [c["method"] for c in cdp.calls]
        self.assertNotIn("DOM.getBoxModel", methods_called)

        # Check pointer triple coordinates at new centre (50, 65) instead of stale (5, 5)
        mouse_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_calls), 3)
        self.assertEqual([c["params"]["type"] for c in mouse_calls], ["mouseMoved", "mousePressed", "mouseReleased"])

        for call in mouse_calls:
            self.assertEqual(call["params"]["x"], 50)
            self.assertEqual(call["params"]["y"], 65)


if __name__ == "__main__":
    unittest.main()
