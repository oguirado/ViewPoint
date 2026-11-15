"""Tests for X3 py_s3 seed CDP Client and Query/Planner."""

import os
import sys
import json
import tempfile
import unittest

from bench.oracle.x3.py_s3.client import (
    CDPClient,
    LocalRefusalError,
    parse_ax_tree,
    parse_dom_snapshot,
    find_dom_node_by_id,
    find_focused_ax_node,
    plan_click,
)

FIXTURES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "tests", "fixtures")
)
AX_FOCUS_PATH = os.path.join(FIXTURES_DIR, "oracle_ax_focus.json")
SNAPSHOT_BASIC_PATH = os.path.join(FIXTURES_DIR, "oracle_snapshot_basic.json")


def load_fixture(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestClientAndPlanner(unittest.TestCase):
    def setUp(self):
        self.ax_focus_fixture = load_fixture(AX_FOCUS_PATH)
        self.snapshot_basic_fixture = load_fixture(SNAPSHOT_BASIC_PATH)

    def test_handshake_and_log(self):
        with tempfile.NamedTemporaryFile(delete=False) as log_file:
            log_path = log_file.name

        try:
            client = CDPClient(fake_log=log_path)
            self.assertEqual(client.session_id, "S1")

            # Call an allow-listed session method
            res = client.send_request("Accessibility.getFullAXTree")
            self.assertIsNotNone(res)

            client.close()

            with open(log_path, "rb") as f:
                log_content = f.read().decode("utf-8")

            logged_lines = [line for line in log_content.split("\n") if line.strip()]
            for line in logged_lines:
                msg = json.loads(line)
                method = msg.get("method")
                self.assertIsNotNone(method)
                self.assertNotIn("Input.", method)
                self.assertNotIn("Runtime.", method)
        finally:
            if os.path.exists(log_path):
                os.remove(log_path)

    def test_local_refusals_write_nothing(self):
        with tempfile.NamedTemporaryFile(delete=False) as log_file:
            log_path = log_file.name

        try:
            client = CDPClient(fake_log=log_path)

            # Local refusal for Input method
            with self.assertRaises(LocalRefusalError) as cm:
                client.send_request("Input.dispatchMouseEvent", {"type": "mousePressed"})
            self.assertEqual(cm.exception.code, 5)

            # Local refusal for Page.navigate
            with self.assertRaises(LocalRefusalError) as cm:
                client.send_request("Page.navigate", {"url": "http://example.com"})
            self.assertEqual(cm.exception.code, 5)

            client.close()

            with open(log_path, "rb") as f:
                log_content = f.read().decode("utf-8")

            logged_lines = [line for line in log_content.split("\n") if line.strip()]
            for line in logged_lines:
                msg = json.loads(line)
                method = msg.get("method", "")
                self.assertNotEqual(method, "Input.dispatchMouseEvent")
                self.assertNotEqual(method, "Page.navigate")
        finally:
            if os.path.exists(log_path):
                os.remove(log_path)

    def test_oversize_frame_refusal(self):
        # Using FAKE_MODE=oversize with FAKE_BYTES above max_frame_bytes limit
        with self.assertRaises((ValueError, TimeoutError, Exception)):
            CDPClient(fake_mode="oversize", max_frame_bytes=100)

    def test_snapshot_fixture_coordinates(self):
        def provider():
            return self.snapshot_basic_fixture, self.ax_focus_fixture

        # id 'box' centre (420, 440) document, (420, 376) viewport
        res = plan_click(provider, {"id": "box"})
        self.assertEqual(res["kind"], "click")
        self.assertEqual(res["xy"], [420, 440])
        self.assertEqual(res["viewport_xy"], [420, 376])
        self.assertEqual(res["attempts"], 1)

    def test_hidden_element_not_visible(self):
        def provider():
            return self.snapshot_basic_fixture, self.ax_focus_fixture

        # id 'cv' has computed styles hidden / display none / opacity 0
        res = plan_click(provider, {"id": "cv"}, max_retries=3)
        self.assertEqual(res["kind"], "abort")
        self.assertEqual(res["reason"], "not_visible")

    def test_retry_recovery(self):
        calls = 0

        def provider():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {}, {}
            return self.snapshot_basic_fixture, self.ax_focus_fixture

        res = plan_click(provider, {"id": "box"}, max_retries=3)
        self.assertEqual(res["kind"], "click")
        self.assertEqual(res["attempts"], 2)

    def test_ax_focused_node(self):
        parsed_ax = parse_ax_tree(self.ax_focus_fixture)
        focused_node = find_focused_ax_node(parsed_ax)
        self.assertIsNotNone(focused_node)
        self.assertEqual(focused_node.get("nodeId"), "7")
        role_val = focused_node.get("role", {}).get("value")
        self.assertEqual(role_val, "textbox")

    def test_sensitive_refusal_cases(self):
        # Test password type
        dom_password = {
            "documents": [{
                "contentHeight": 100, "contentWidth": 100,
                "nodes": {"backendNodeId": [1], "attributes": [[0, 1, 6, 7]], "nodeName": [2], "nodeType": [1]},
                "layout": {"nodeIndex": [0], "bounds": [[10, 10, 100, 20]], "styles": [[3, 4, 5]]},
                "scrollOffsetX": 0, "scrollOffsetY": 0
            }],
            "strings": ["type", "password", "INPUT", "visible", "1", "block", "id", "pass_field"]
        }
        res = plan_click(lambda: (dom_password, {}), {"id": "pass_field"})
        self.assertEqual(res, {"kind": "abort", "reason": "refused"})

        # Test card_number id
        dom_card = {
            "documents": [{
                "contentHeight": 100, "contentWidth": 100,
                "nodes": {"backendNodeId": [1], "attributes": [[0, 1]], "nodeName": [2], "nodeType": [1]},
                "layout": {"nodeIndex": [0], "bounds": [[10, 10, 100, 20]], "styles": [[3, 4, 5]]},
                "scrollOffsetX": 0, "scrollOffsetY": 0
            }],
            "strings": ["id", "card_number", "INPUT", "visible", "1", "block"]
        }
        res = plan_click(lambda: (dom_card, {}), {"id": "card_number"})
        self.assertEqual(res, {"kind": "abort", "reason": "refused"})

        # Test file input
        dom_file = {
            "documents": [{
                "contentHeight": 100, "contentWidth": 100,
                "nodes": {"backendNodeId": [1], "attributes": [[0, 1, 6, 7]], "nodeName": [2], "nodeType": [1]},
                "layout": {"nodeIndex": [0], "bounds": [[10, 10, 100, 20]], "styles": [[3, 4, 5]]},
                "scrollOffsetX": 0, "scrollOffsetY": 0
            }],
            "strings": ["type", "file", "INPUT", "visible", "1", "block", "id", "upload"]
        }
        res = plan_click(lambda: (dom_file, {}), {"id": "upload"})
        self.assertEqual(res, {"kind": "abort", "reason": "refused"})

    def test_pipe_empty_results_exhaust_retries(self):
        client = CDPClient()

        def pipe_provider():
            ax = client.send_request("Accessibility.getFullAXTree")
            snap = client.send_request("DOMSnapshot.captureSnapshot", {"computedStyles": ["visibility", "opacity", "display"]})
            return snap, ax

        try:
            res = plan_click(pipe_provider, {"id": "box"}, max_retries=2)
            self.assertEqual(res["kind"], "abort")
            self.assertEqual(res["reason"], "not_found")
        finally:
            client.close()

    def test_plan_determinism(self):
        def provider():
            return self.snapshot_basic_fixture, self.ax_focus_fixture

        plan1 = json.dumps(plan_click(provider, {"id": "box"}), sort_keys=True)
        plan2 = json.dumps(plan_click(provider, {"id": "box"}), sort_keys=True)
        self.assertEqual(plan1, plan2)


if __name__ == "__main__":
    unittest.main()
