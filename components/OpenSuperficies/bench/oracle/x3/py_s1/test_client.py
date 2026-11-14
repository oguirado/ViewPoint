#!/usr/bin/env python3
"""Tests for CDP client, query functions, and action planner."""

import json
import os
import tempfile
import unittest

from bench.oracle.x3.py_s1.client import (
    CDPClient,
    OversizeFrameError,
    RefusalError,
    find_focused_ax_node,
    find_node_by_id,
    parse_ax_tree,
    parse_dom_snapshot,
    plan_click,
)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
SNAPSHOT_FIXTURE_PATH = os.path.join(
    REPO_ROOT, "bench", "oracle", "tests", "fixtures", "oracle_snapshot_basic.json"
)
AX_FIXTURE_PATH = os.path.join(
    REPO_ROOT, "bench", "oracle", "tests", "fixtures", "oracle_ax_focus.json"
)


def load_json_fixture(path):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


class TestCDPClientAndPlanner(unittest.TestCase):
    def test_handshake_and_fake_log_allowlist_only(self):
        log_file = tempfile.NamedTemporaryFile(delete=False, prefix="fake_log_")
        log_path = log_file.name
        log_file.close()

        try:
            with CDPClient(fake_log=log_path) as client:
                sid = client.connect()
                self.assertEqual(sid, "S1")
                res = client.send("Accessibility.getFullAXTree")
                self.assertIsNotNone(res)

            with open(log_path, "rb") as f:
                log_data = f.read()

            self.assertNotIn(b"Input.", log_data)
            self.assertNotIn(b"Runtime.", log_data)
            lines = [l for l in log_data.splitlines() if l.strip()]
            for line in lines:
                msg = json.loads(line.decode("utf-8"))
                method = msg.get("method")
                self.assertIn(
                    method, ["Target.setAutoAttach", "Accessibility.getFullAXTree"]
                )
        finally:
            if os.path.exists(log_path):
                os.unlink(log_path)

    def test_local_refusal_writes_nothing_to_fake_log(self):
        log_file = tempfile.NamedTemporaryFile(delete=False, prefix="fake_log_")
        log_path = log_file.name
        log_file.close()

        try:
            with CDPClient(fake_log=log_path) as client:
                client.connect()

                with self.assertRaises(RefusalError) as cm1:
                    client.send("Input.dispatchMouseEvent", {"type": "mousePressed"})
                self.assertEqual(cm1.exception.code, 5)

                with self.assertRaises(RefusalError) as cm2:
                    client.send("Page.navigate", {"url": "file:///etc/passwd"})
                self.assertEqual(cm2.exception.code, 5)

            with open(log_path, "rb") as f:
                log_data = f.read()

            self.assertNotIn(b"Input.dispatchMouseEvent", log_data)
            self.assertNotIn(b"Page.navigate", log_data)
        finally:
            if os.path.exists(log_path):
                os.unlink(log_path)

    def test_oversize_frame_refused(self):
        env_backup = os.environ.get("FAKE_BYTES")
        os.environ["FAKE_BYTES"] = "5000"
        try:
            with self.assertRaises((OversizeFrameError, RefusalError)) as cm:
                client = CDPClient(fake_mode="oversize", max_frame_bytes=256)
                try:
                    client.connect(timeout=2.0)
                finally:
                    client.close()
            self.assertEqual(cm.exception.code, 5)
        finally:
            if env_backup is None:
                os.environ.pop("FAKE_BYTES", None)
            else:
                os.environ["FAKE_BYTES"] = env_backup

    def test_snapshot_fixture_box_center_and_viewport(self):
        snapshot = load_json_fixture(SNAPSHOT_FIXTURE_PATH)
        dom_nodes = parse_dom_snapshot(snapshot)
        box_node = find_node_by_id(dom_nodes, "box")
        self.assertIsNotNone(box_node)
        self.assertEqual(box_node["center_doc"], [420, 440])
        self.assertEqual(box_node["center_viewport"], [420, 376])

    def test_id_cv_not_visible_exhausts_retries(self):
        snapshot = load_json_fixture(SNAPSHOT_FIXTURE_PATH)
        provider = lambda: ({}, snapshot)
        plan = plan_click({"id": "cv"}, provider, retries=3)
        self.assertEqual(plan, {"kind": "abort", "reason": "not_visible"})

    def test_provider_retry_success(self):
        snapshot = load_json_fixture(SNAPSHOT_FIXTURE_PATH)
        calls = 0

        def provider():
            nonlocal calls
            calls += 1
            if calls == 1:
                return ({}, {})
            return ({}, snapshot)

        plan = plan_click({"id": "box"}, provider, retries=3)
        self.assertEqual(
            plan,
            {
                "kind": "click",
                "xy": [420, 440],
                "viewport_xy": [420, 376],
                "attempts": 2,
            },
        )

    def test_ax_fixture_focused_node(self):
        ax_tree = load_json_fixture(AX_FIXTURE_PATH)
        ax_nodes = parse_ax_tree(ax_tree)
        focused = find_focused_ax_node(ax_nodes)
        self.assertIsNotNone(focused)
        self.assertEqual(focused["nodeId"], "7")
        self.assertEqual(focused["role"], "textbox")

    def test_refusal_cases(self):
        # type password refusal
        dom_pass = {
            "documents": [
                {
                    "scrollOffsetX": 0,
                    "scrollOffsetY": 0,
                    "nodes": {
                        "backendNodeId": [1],
                        "attributes": [[0, 1, 2, 3]],
                    },
                    "layout": {
                        "nodeIndex": [0],
                        "bounds": [[10, 10, 100, 30]],
                        "styles": [[4, 5, 6]],
                    },
                }
            ],
            "strings": [
                "id",
                "secret_field",
                "type",
                "password",
                "visible",
                "1",
                "block",
            ],
        }
        plan_pass = plan_click({"id": "secret_field"}, lambda: ({}, dom_pass))
        self.assertEqual(plan_pass, {"kind": "abort", "reason": "refused"})

        # card_number id refusal
        dom_card = {
            "documents": [
                {
                    "scrollOffsetX": 0,
                    "scrollOffsetY": 0,
                    "nodes": {
                        "backendNodeId": [1],
                        "attributes": [[0, 1]],
                    },
                    "layout": {
                        "nodeIndex": [0],
                        "bounds": [[10, 10, 100, 30]],
                        "styles": [[2, 3, 4]],
                    },
                }
            ],
            "strings": ["id", "card_number", "visible", "1", "block"],
        }
        plan_card = plan_click({"id": "card_number"}, lambda: ({}, dom_card))
        self.assertEqual(plan_card, {"kind": "abort", "reason": "refused"})

        # input type file refusal
        dom_file = {
            "documents": [
                {
                    "scrollOffsetX": 0,
                    "scrollOffsetY": 0,
                    "nodes": {
                        "backendNodeId": [1],
                        "attributes": [[0, 1, 2, 3]],
                    },
                    "layout": {
                        "nodeIndex": [0],
                        "bounds": [[10, 10, 100, 30]],
                        "styles": [[4, 5, 6]],
                    },
                }
            ],
            "strings": ["id", "upload_btn", "type", "file", "visible", "1", "block"],
        }
        plan_file = plan_click({"id": "upload_btn"}, lambda: ({}, dom_file))
        self.assertEqual(plan_file, {"kind": "abort", "reason": "refused"})

    def test_over_fake_pipe_empty_results_exhaust_retries(self):
        with CDPClient() as client:
            client.connect()

            def provider():
                ax = client.send("Accessibility.getFullAXTree")
                snap = client.send(
                    "DOMSnapshot.captureSnapshot",
                    {"computedStyles": ["visibility", "opacity", "display"]},
                )
                return (ax, snap)

            plan = plan_click({"id": "box"}, provider, retries=3)
            self.assertEqual(plan, {"kind": "abort", "reason": "not_found"})

    def test_identical_runs_byte_identical(self):
        snapshot = load_json_fixture(SNAPSHOT_FIXTURE_PATH)
        provider = lambda: ({}, snapshot)
        p1 = plan_click({"id": "box"}, provider, retries=3)
        p2 = plan_click({"id": "box"}, provider, retries=3)
        self.assertEqual(
            json.dumps(p1, sort_keys=True), json.dumps(p2, sort_keys=True)
        )


if __name__ == "__main__":
    unittest.main()
