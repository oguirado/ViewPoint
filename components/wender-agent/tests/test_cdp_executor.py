"""Unit tests for wender.act.cdp_executor using FakeCdp and scripted read_nodes."""
import json
import unittest

from wender.act.cdp_executor import ActionError, JS_BY_OP, run_ax_action
from wender.perceive import ax
from wender.perceive.cdp_pipe import CdpError
from wender.state.schema import dumps


def _raw_node(nid, role, name="", kids=(), ignored=False, props=None, backend=None):
    node = {
        "nodeId": str(nid),
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "childIds": [str(k) for k in kids],
        "ignored": ignored,
    }
    if props:
        node["properties"] = [
            {"name": k, "value": {"type": "value", "value": v}}
            for k, v in props.items()
        ]
    if backend is not None:
        node["backendDOMNodeId"] = backend
    return node


def _form_raw(
    *,
    container_role="main",
    btn_pressed=False,
    btn_name="Send",
    text_name="Query",
    text_busy=False,
    select_expanded=False,
):
    return [
        _raw_node(1, "RootWebArea", "App", [2]),
        _raw_node(2, container_role, "Workspace", [10, 20, 30]),
        _raw_node(
            10,
            "button",
            btn_name,
            props={"pressed": btn_pressed, "disabled": False},
            backend=100,
        ),
        _raw_node(
            20,
            "textbox",
            text_name,
            props={"busy": text_busy, "disabled": False},
            backend=200,
        ),
        _raw_node(
            30,
            "combobox",
            "Format",
            props={"expanded": select_expanded, "disabled": False},
            backend=300,
        ),
    ]


class FakeCdp:
    """Transport double recording every send(method, params, session_id) and scripting replies."""

    def __init__(self, scripted=None):
        self.calls = []
        self.scripted = dict(scripted or {})

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        queue = self.scripted.get(method)
        if queue:
            item = queue.pop(0) if isinstance(queue, list) else queue
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item(payload, session_id)
            return dict(item)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-%s" % payload.get("backendNodeId")}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "undefined"}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        return {}


class ScriptedReadNodes:
    """Callable returning scripted prune node lists in sequence."""

    def __init__(self, snapshots):
        self.snapshots = [list(s) for s in snapshots]
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.snapshots:
            raise IndexError("no more scripted snapshots")
        if len(self.snapshots) == 1:
            return list(self.snapshots[0])
        return list(self.snapshots.pop(0))


class TestCdpExecutor(unittest.TestCase):
    def setUp(self):
        self.initial_nodes = ax.prune(_form_raw())
        self.btn = self.initial_nodes[2]
        self.textbox = self.initial_nodes[3]
        self.select = self.initial_nodes[4]
        self.waits = []

    def fake_sleep(self, seconds):
        self.waits.append(seconds)

    def _assert_canonical_receipt(self, receipt):
        serialized = dumps(receipt)
        parsed = json.loads(serialized)
        self.assertEqual(parsed, receipt)
        self.assertEqual(
            sorted(receipt.keys()),
            ["attempts", "error", "ok", "path", "post_sha", "pre_sha", "verified"],
        )
        for key in ("ok", "attempts", "verified"):
            self.assertIsInstance(receipt[key], int)
            self.assertNotIsInstance(receipt[key], bool)
        self.assertIn(receipt["path"], ("ax", "coordinates"))
        self.assertEqual(len(receipt["pre_sha"]), 64)
        self.assertEqual(len(receipt["post_sha"]), 64)
        if receipt["error"] is not None:
            self.assertIsInstance(receipt["error"], str)

    def test_each_op_dispatches_fixed_js_and_verifies(self):
        # 1. click
        cdp = FakeCdp()
        after_click = ax.prune(_form_raw(btn_pressed=True))
        reader = ScriptedReadNodes([after_click])
        receipt = run_ax_action(
            cdp,
            "sid-1",
            self.initial_nodes,
            {
                "op": "click",
                "target": self.btn["id"],
                "expect": {"target_states": {"pressed": "true"}, "target_name": "Send"},
                "irreversible": False,
            },
            reader,
            wait_ms=250,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt)
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        self.assertEqual(receipt["path"], "ax")
        self.assertEqual(receipt["attempts"], 1)
        self.assertIsNone(receipt["error"])
        self.assertEqual(receipt["pre_sha"], ax.snapshot_sha(self.initial_nodes, True))
        self.assertEqual(receipt["post_sha"], ax.snapshot_sha(after_click, True))
        self.assertNotEqual(receipt["pre_sha"], receipt["post_sha"])
        self.assertEqual(
            [c["method"] for c in cdp.calls],
            ["DOM.scrollIntoViewIfNeeded", "DOM.resolveNode", "Runtime.callFunctionOn"],
        )
        self.assertEqual(cdp.calls[0]["params"], {"backendNodeId": 100})
        self.assertEqual(cdp.calls[1]["params"], {"backendNodeId": 100})
        self.assertEqual(cdp.calls[2]["params"]["objectId"], "obj-100")
        self.assertEqual(cdp.calls[2]["params"]["functionDeclaration"], JS_BY_OP["click"])
        self.assertIn("this.click", cdp.calls[2]["params"]["functionDeclaration"])
        self.assertEqual(cdp.calls[2]["params"]["arguments"], [])
        self.assertEqual(self.waits, [0.25])

        # 2. focus
        cdp_focus = FakeCdp()
        reader_focus = ScriptedReadNodes([self.initial_nodes])
        receipt_focus = run_ax_action(
            cdp_focus,
            "sid-1",
            self.initial_nodes,
            {
                "op": "focus",
                "target": self.textbox["id"],
                "expect": {"target_name": "Query"},
                "irreversible": False,
            },
            reader_focus,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt_focus)
        self.assertEqual(receipt_focus["ok"], 1)
        self.assertEqual(
            cdp_focus.calls[2]["params"]["functionDeclaration"], JS_BY_OP["focus"]
        )
        self.assertIn("this.focus", cdp_focus.calls[2]["params"]["functionDeclaration"])

        # 3. type
        cdp_type = FakeCdp()
        after_type = ax.prune(_form_raw(text_busy=True))
        reader_type = ScriptedReadNodes([after_type])
        receipt_type = run_ax_action(
            cdp_type,
            "sid-1",
            self.initial_nodes,
            {
                "op": "type",
                "target": self.textbox["id"],
                "text": "hello world",
                "expect": {"target_states": {"busy": "true"}},
                "irreversible": False,
            },
            reader_type,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt_type)
        self.assertEqual(receipt_type["ok"], 1)
        self.assertEqual(
            cdp_type.calls[2]["params"]["functionDeclaration"], JS_BY_OP["type"]
        )
        self.assertEqual(
            cdp_type.calls[2]["params"]["arguments"], [{"value": "hello world"}]
        )
        for token in ("this.focus", "this.value", "input", "change"):
            self.assertIn(token, cdp_type.calls[2]["params"]["functionDeclaration"])

        # 4. select
        cdp_select = FakeCdp()
        after_select = ax.prune(_form_raw(select_expanded=True))
        reader_select = ScriptedReadNodes([after_select])
        receipt_select = run_ax_action(
            cdp_select,
            "sid-1",
            self.initial_nodes,
            {
                "op": "select",
                "target": self.select["id"],
                "value": "json",
                "expect": {"target_states": {"expanded": "true"}},
                "irreversible": False,
            },
            reader_select,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt_select)
        self.assertEqual(receipt_select["ok"], 1)
        self.assertEqual(
            cdp_select.calls[2]["params"]["functionDeclaration"], JS_BY_OP["select"]
        )
        self.assertEqual(
            cdp_select.calls[2]["params"]["arguments"], [{"value": "json"}]
        )
        for token in ("this.value", "change"):
            self.assertIn(token, cdp_select.calls[2]["params"]["functionDeclaration"])

    def test_retry_after_transient_cdp_error(self):
        cdp = FakeCdp(
            {
                "Runtime.callFunctionOn": [
                    CdpError("transient pipe glitch"),
                    {"result": {"type": "undefined"}},
                ]
            }
        )
        after_click = ax.prune(_form_raw(btn_pressed=True))
        reader = ScriptedReadNodes([after_click])
        receipt = run_ax_action(
            cdp,
            "sid-retry",
            self.initial_nodes,
            {
                "op": "click",
                "target": self.btn["id"],
                "expect": {"target_states": {"pressed": "true"}},
                "irreversible": False,
            },
            reader,
            retries=3,
            wait_ms=100,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt)
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        self.assertEqual(receipt["attempts"], 2)
        self.assertEqual(receipt["path"], "ax")
        self.assertIsNone(receipt["error"])
        self.assertEqual(reader.calls, 2)

    def test_verification_failure_exhausts_retries_without_raising(self):
        cdp = FakeCdp()
        unchanged = ax.prune(_form_raw(btn_pressed=False))
        reader = ScriptedReadNodes([unchanged, unchanged, unchanged])
        receipt = run_ax_action(
            cdp,
            "sid-fail",
            self.initial_nodes,
            {
                "op": "click",
                "target": self.btn["id"],
                "expect": {"target_states": {"pressed": "true"}},
                "irreversible": False,
            },
            reader,
            retries=3,
            wait_ms=50,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt)
        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)
        self.assertEqual(receipt["attempts"], 3)
        self.assertEqual(receipt["path"], "ax")
        self.assertIsInstance(receipt["error"], str)
        self.assertTrue(receipt["error"])
        self.assertEqual(receipt["pre_sha"], receipt["post_sha"])
        self.assertEqual(reader.calls, 5)

    def test_navigation_without_expect_succeeds_once_and_never_redispatches(self):
        # A link click with no expectation that leaves the page: the target vanishes and the snapshot changes.
        cdp = FakeCdp()
        other_page = ax.prune([_raw_node(1, "RootWebArea", "Next", [2]), _raw_node(2, "heading", "Next page")])
        reader = ScriptedReadNodes([other_page, other_page, other_page])
        receipt = run_ax_action(cdp, "sid-nav", self.initial_nodes,
                                {"op": "click", "target": self.btn["id"], "irreversible": False},
                                reader, retries=3, wait_ms=50, sleep=self.fake_sleep)
        self._assert_canonical_receipt(receipt)
        self.assertEqual((receipt["ok"], receipt["verified"], receipt["attempts"]), (1, 1, 1))
        self.assertNotEqual(receipt["pre_sha"], receipt["post_sha"])
        self.assertEqual(reader.calls, 1)

    def test_vanished_target_with_expect_still_fails(self):
        cdp = FakeCdp()
        other_page = ax.prune([_raw_node(1, "RootWebArea", "Next", [2]), _raw_node(2, "heading", "Next page")])
        reader = ScriptedReadNodes([other_page, other_page])
        with self.assertRaisesRegex(ActionError, "fresh AX target tie or identity downgrade"):
            run_ax_action(cdp, "sid-nav2", self.initial_nodes,
                          {"op": "click", "target": self.btn["id"], "irreversible": False,
                           "expect": {"target_states": {"pressed": "true"}}},
                          reader, retries=2, wait_ms=50, sleep=self.fake_sleep)
        # The initial dispatch precedes disappearance; the fresh gate forbids retry.
        self.assertEqual([call["method"] for call in cdp.calls],
                         ["DOM.scrollIntoViewIfNeeded", "DOM.resolveNode", "Runtime.callFunctionOn"])

    def test_irreversible_refused_when_matched_only_at_l2_name(self):
        cdp = FakeCdp()
        drifted = ax.prune(_form_raw(container_role="dialog"))
        match_info = ax.match(drifted, ax.describe(self.btn))
        self.assertEqual(match_info["level"], "L2_NAME")
        self.assertFalse(ax.level_allows_irreversible(match_info["level"]))

        reader = ScriptedReadNodes([drifted])
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sid-irr",
                self.initial_nodes,
                {
                    "op": "click",
                    "target": self.btn["id"],
                    "expect": {"target_states": {"pressed": "true"}},
                    "irreversible": True,
                },
                reader,
                sleep=self.fake_sleep,
            )
        self.assertEqual(cdp.calls, [])

    def test_irreversible_allowed_at_l0_and_never_uses_coordinates(self):
        cdp = FakeCdp()
        unchanged = ax.prune(_form_raw(btn_pressed=False))
        reader = ScriptedReadNodes([unchanged, unchanged, unchanged, unchanged])
        receipt = run_ax_action(
            cdp,
            "sid-irr-ok",
            self.initial_nodes,
            {
                "op": "click",
                "target": self.btn["id"],
                "expect": {"target_states": {"pressed": "true"}},
                "irreversible": True,
            },
            reader,
            retries=2,
            allow_coordinates=True,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt)
        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["path"], "ax")
        self.assertEqual(receipt["attempts"], 2)
        mouse_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(mouse_calls, [])

    def test_coordinates_fallback_off_by_default_and_on_when_allowed(self):
        # Off by default
        cdp_default = FakeCdp(
            {"Runtime.callFunctionOn": [CdpError("ax click blocked"), CdpError("ax click blocked")]}
        )
        after_click = ax.prune(_form_raw(btn_pressed=True))
        reader_default = ScriptedReadNodes([after_click])
        receipt_default = run_ax_action(
            cdp_default,
            "sid-coord-off",
            self.initial_nodes,
            {
                "op": "click",
                "target": self.btn["id"],
                "expect": {"target_states": {"pressed": "true"}},
                "irreversible": False,
            },
            reader_default,
            retries=2,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt_default)
        self.assertEqual(receipt_default["ok"], 0)
        self.assertEqual(receipt_default["path"], "ax")
        self.assertEqual(receipt_default["attempts"], 2)
        self.assertEqual(
            [c for c in cdp_default.calls if c["method"] == "Input.dispatchMouseEvent"],
            [],
        )

        # Enabled for reversible click
        cdp_coord = FakeCdp(
            {"Runtime.callFunctionOn": [CdpError("ax click blocked"), CdpError("ax click blocked")]}
        )
        reader_coord = ScriptedReadNodes([after_click])
        receipt_coord = run_ax_action(
            cdp_coord,
            "sid-coord-on",
            self.initial_nodes,
            {
                "op": "click",
                "target": self.btn["id"],
                "expect": {"target_states": {"pressed": "true"}},
                "irreversible": False,
            },
            reader_coord,
            retries=2,
            allow_coordinates=True,
            sleep=self.fake_sleep,
        )
        self._assert_canonical_receipt(receipt_coord)
        self.assertEqual(receipt_coord["ok"], 1)
        self.assertEqual(receipt_coord["verified"], 1)
        self.assertEqual(receipt_coord["path"], "coordinates")
        self.assertEqual(receipt_coord["attempts"], 3)
        self.assertIsNone(receipt_coord["error"])
        mouse_calls = [c for c in cdp_coord.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(
            [c["params"]["type"] for c in mouse_calls],
            ["mouseMoved", "mousePressed", "mouseReleased"],
        )
        for call in mouse_calls:
            self.assertEqual((call["params"]["x"], call["params"]["y"]), (60, 40))

    def test_bad_inputs_raise_action_error(self):
        cdp = FakeCdp()
        reader = ScriptedReadNodes([self.initial_nodes])
        with self.assertRaises(ActionError):
            run_ax_action(cdp, "s", self.initial_nodes, {"op": "click", "target": "0" * 16}, reader)
        with self.assertRaises(ActionError):
            run_ax_action(cdp, "s", self.initial_nodes, {"op": "drag", "target": self.btn["id"]}, reader)
        with self.assertRaises(ActionError):
            run_ax_action(cdp, "s", self.initial_nodes, {"op": "type", "target": self.textbox["id"]}, reader)
        with self.assertRaises(ActionError):
            run_ax_action(cdp, "s", self.initial_nodes, {"op": "select", "target": self.select["id"]}, reader)


    def test_irreversible_retry_fresh_gate_refuses_drift_before_second_dispatch(self):
        cdp = FakeCdp()
        unchanged = ax.prune(_form_raw(btn_pressed=False))
        drifted = ax.prune(_form_raw(container_role="dialog", btn_pressed=False))
        reader = ScriptedReadNodes([unchanged, unchanged, drifted])
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sid-irr-retry-drift",
                self.initial_nodes,
                {
                    "op": "click",
                    "target": self.btn["id"],
                    "expect": {"target_states": {"pressed": "true"}},
                    "irreversible": True,
                },
                reader,
                retries=2,
                sleep=self.fake_sleep,
            )
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(len(runtime_calls), 1)


if __name__ == "__main__":
    unittest.main()
