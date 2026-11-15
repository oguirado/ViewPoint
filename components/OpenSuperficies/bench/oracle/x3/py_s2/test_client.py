"""Tests for OpenSuperficies CDP client, AX query, and action planning."""

import json
import os
import tempfile
import unittest

from bench.oracle.x3.py_s2.client import (
    CDPClient,
    CDPError,
    find_ax_node_by_role,
    find_focused_ax_node,
    find_snapshot_node_by_id,
    is_sensitive,
    parse_ax_tree,
    parse_dom_snapshot,
    plan_click,
)

AX_FIXTURE_PATH = os.path.join("bench", "oracle", "tests", "fixtures", "oracle_ax_focus.json")
SNAPSHOT_FIXTURE_PATH = os.path.join("bench", "oracle", "tests", "fixtures", "oracle_snapshot_basic.json")


def load_fixture(path):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


class TestCDPClient(unittest.TestCase):
    def test_handshake_and_log(self):
        with tempfile.NamedTemporaryFile(delete=False) as log_file:
            log_path = log_file.name

        try:
            env = {
                "FAKE_MODE": "echo",
                "FAKE_URL": "file:///opt/cu/scenes/v3/ff_form_01.html",
                "FAKE_LOG": log_path,
            }
            with CDPClient(env=env) as client:
                self.assertEqual(client.session_id, "S1")

            with open(log_path, "rb") as f:
                log_lines = [line.strip() for line in f if line.strip()]

            methods = []
            for line in log_lines:
                msg = json.loads(line.decode("utf-8"))
                m = msg.get("method")
                if m:
                    methods.append(m)

            # Check that log contains only allowlisted methods and no Input or Runtime
            for m in methods:
                self.assertFalse(m.startswith("Input."))
                self.assertFalse(m.startswith("Runtime."))
                self.assertIn(m, ["Target.setAutoAttach"])
        finally:
            if os.path.exists(log_path):
                os.unlink(log_path)

    def test_local_refusal(self):
        with tempfile.NamedTemporaryFile(delete=False) as log_file:
            log_path = log_file.name

        try:
            env = {
                "FAKE_MODE": "echo",
                "FAKE_URL": "file:///opt/cu/scenes/v3/ff_form_01.html",
                "FAKE_LOG": log_path,
            }
            with CDPClient(env=env) as client:
                with self.assertRaises(CDPError) as cm_input:
                    client.send_request("Input.dispatchMouseEvent", params={})
                self.assertEqual(cm_input.exception.code, 5)

                with self.assertRaises(CDPError) as cm_nav:
                    client.send_request("Page.navigate", params={"url": "http://example.com"})
                self.assertEqual(cm_nav.exception.code, 5)

            with open(log_path, "rb") as f:
                log_content = f.read()

            self.assertNotIn(b"Input.dispatchMouseEvent", log_content)
            self.assertNotIn(b"Page.navigate", log_content)
        finally:
            if os.path.exists(log_path):
                os.unlink(log_path)

    def test_oversize_frame_refused(self):
        env = {
            "FAKE_MODE": "oversize",
            "FAKE_BYTES": "5000",
        }
        client = CDPClient(env=env, max_frame_bytes=1000)
        client.start()
        with self.assertRaises(CDPError) as cm:
            client.read_oversize()
        client.close()
        self.assertEqual(cm.exception.code, 5)

    def test_snapshot_fixture_bounds_math(self):
        snapshot_data = load_fixture(SNAPSHOT_FIXTURE_PATH)
        nodes = parse_dom_snapshot(snapshot_data)
        box_node = find_snapshot_node_by_id(nodes, "box")
        self.assertIsNotNone(box_node)
        self.assertTrue(box_node["visible"])

        # doc xy = [420, 440], viewport xy = [420, 376]
        res = plan_click({"id": "box"}, lambda: {"snapshot": snapshot_data})
        self.assertEqual(res["kind"], "click")
        self.assertEqual(res["xy"], [420, 440])
        self.assertEqual(res["viewport_xy"], [420, 376])
        self.assertEqual(res["attempts"], 1)

    def test_cv_not_visible_exhausts_retries(self):
        snapshot_data = load_fixture(SNAPSHOT_FIXTURE_PATH)
        res = plan_click({"id": "cv"}, lambda: {"snapshot": snapshot_data}, retries=3)
        self.assertEqual(res["kind"], "abort")
        self.assertEqual(res["reason"], "not_visible")
        self.assertEqual(res["attempts"], 3)

    def test_retry_succeeds_on_second_attempt(self):
        snapshot_data = load_fixture(SNAPSHOT_FIXTURE_PATH)
        calls = 0

        def provider():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"snapshot": {}}
            return {"snapshot": snapshot_data}

        res = plan_click({"id": "box"}, provider, retries=3)
        self.assertEqual(res["kind"], "click")
        self.assertEqual(res["attempts"], 2)

    def test_ax_fixture_focused_node(self):
        ax_data = load_fixture(AX_FIXTURE_PATH)
        ax_nodes = parse_ax_tree(ax_data)
        focused = find_focused_ax_node(ax_nodes)
        self.assertIsNotNone(focused)
        self.assertEqual(str(focused["nodeId"]), "7")
        self.assertEqual(focused["role"]["value"], "textbox")

    def test_refusal_cases(self):
        # type password
        node_pass = {"attributes": {"type": "password"}, "backendNodeId": 1}
        self.assertTrue(is_sensitive(node_pass))

        # card_number id
        node_card = {"attributes": {"id": "card_number"}, "backendNodeId": 2}
        self.assertTrue(is_sensitive(node_card))

        # input type file
        node_file = {"attributes": {"type": "file"}, "backendNodeId": 3}
        self.assertTrue(is_sensitive(node_file))

        # Plan click aborts with reason refused
        snapshot_mock = {
            "documents": [
                {
                    "nodes": {"backendNodeId": [1], "attributes": [[0, 1, 2, 3]]},
                    "layout": {"bounds": [[0, 0, 10, 10]], "nodeIndex": [0], "styles": [[0, 1, 2]]},
                }
            ],
            "strings": ["type", "password", "id", "pass_input", "visible", "1", "block"],
        }
        res = plan_click({"id": "pass_input"}, lambda: {"snapshot": snapshot_mock})
        self.assertEqual(res["kind"], "abort")
        self.assertEqual(res["reason"], "refused")

    def test_empty_results_over_pipe_exhaust_retries(self):
        with tempfile.NamedTemporaryFile(delete=False) as log_file:
            log_path = log_file.name

        try:
            env = {
                "FAKE_MODE": "echo",
                "FAKE_URL": "file:///opt/cu/scenes/v3/ff_form_01.html",
                "FAKE_LOG": log_path,
            }
            with CDPClient(env=env) as client:
                def provider():
                    ax = client.send_request("Accessibility.getFullAXTree")
                    snap = client.send_request(
                        "DOMSnapshot.captureSnapshot",
                        params={"computedStyles": ["visibility", "opacity", "display"]},
                    )
                    return {"ax": ax, "snapshot": snap}

                res = plan_click({"id": "box"}, provider, retries=3)
                self.assertEqual(res["kind"], "abort")
                self.assertEqual(res["reason"], "not_found")
                self.assertEqual(res["attempts"], 3)
        finally:
            if os.path.exists(log_path):
                os.unlink(log_path)

    def test_identical_runs_byte_identical_plans(self):
        snapshot_data = load_fixture(SNAPSHOT_FIXTURE_PATH)
        res1 = plan_click({"id": "box"}, lambda: {"snapshot": snapshot_data})
        res2 = plan_click({"id": "box"}, lambda: {"snapshot": snapshot_data})
        json1 = json.dumps(res1, sort_keys=True)
        json2 = json.dumps(res2, sort_keys=True)
        self.assertEqual(json1, json2)


if __name__ == "__main__":
    unittest.main()
