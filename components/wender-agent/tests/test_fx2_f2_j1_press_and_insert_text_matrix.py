"""@module tests.test_fx2_f2_j1_press_and_insert_text_matrix - Unit tests for press and insert_text matrix contracts."""
import unittest
from unittest.mock import patch

from wender.act import cdp_executor
from wender.act.cdp_executor import ActionError, run_ax_action

DOCUMENTED_KEYS = (
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
)

VIRTUAL_KEY_CODES = {
    "Enter": 13,
    "Escape": 27,
    "Tab": 9,
    "Backspace": 8,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
}


def make_node(**overrides):
    """Construct local pruned-style node with default editable textbox fields."""
    node = {
        "id": "target",
        "backend_id": 101,
        "role": "textbox",
        "name": "Prompt",
        "depth": 1,
        "ancestors": ("main",),
        "attrs": (("disabled", "false"),),
        "ordinal": 0,
        "cardinality": 1,
    }
    node.update(overrides)
    return node


class RecordingFakeCdp:
    """Recording fake CDP transport for testing actions and event dispatches."""

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
            return {"object": {"objectId": "obj_target_101"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": True}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        if method in ("Input.dispatchKeyEvent", "Input.dispatchMouseEvent", "Input.insertText"):
            return {}
        return {}


def noop_sleep(seconds):
    """No-op sleep implementation to avoid delay during testing."""
    pass


class TestFx2F2J1PressAndInsertTextMatrix(unittest.TestCase):
    """Matrix tests for press and insert_text contracts against run_ax_action."""

    def setUp(self):
        self.target = make_node()
        self.nodes = [self.target]

    # --- Nominal Passing Controls (no expectedFailure decorator) ---

    def test_nominal_control_click_succeeds(self):
        """Nominal control verifying existing click op succeeds with FakeCdp."""
        cdp = RecordingFakeCdp()
        nodes = [make_node(role="button", name="Submit")]
        receipt = run_ax_action(
            cdp,
            "sid-1",
            nodes,
            {"op": "click", "target": "target", "expect": {"target_name": "Submit"}},
            lambda: nodes,
            sleep=noop_sleep,
        )
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

    def test_nominal_control_unknown_op_raises_action_error(self):
        """Nominal control verifying unknown op is rejected before transport calls."""
        cdp = RecordingFakeCdp()
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sid-1",
                self.nodes,
                {"op": "frobnicate", "target": "target"},
                lambda: self.nodes,
                sleep=noop_sleep,
            )
        self.assertEqual(cdp.calls, [])

    # --- Press Key and Modifier Matrix ---

    def test_press_every_key_and_modifier_mask_matrix(self):
        # Defect fx2-f2-executor-actor-chromedriver: press op and key/modifier matrix handling missing.
        cdp = RecordingFakeCdp()
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            for key in DOCUMENTED_KEYS:
                for mask in range(16):
                    cdp.calls.clear()
                    receipt = run_ax_action(
                        cdp,
                        "sid-matrix",
                        self.nodes,
                        {
                            "op": "press",
                            "target": "target",
                            "key": key,
                            "modifiers": mask,
                            "expect": {"target_name": "Prompt"},
                        },
                        lambda: self.nodes,
                        sleep=noop_sleep,
                    )
                    self.assertEqual(receipt["ok"], 1)
                    key_events = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
                    self.assertEqual(len(key_events), 2)
                    down, up = key_events[0]["params"], key_events[1]["params"]

                    # Assert exact down/up fields
                    expected_down_type = "keyDown" if (key == "Enter" and False) else "rawKeyDown"
                    self.assertEqual(down.get("type"), expected_down_type)
                    self.assertEqual(up.get("type"), "keyUp")

                    self.assertEqual(down.get("key"), key)
                    self.assertEqual(down.get("code"), key)
                    self.assertEqual(up.get("key"), key)
                    self.assertEqual(up.get("code"), key)

                    self.assertEqual(down.get("modifiers"), mask)
                    self.assertEqual(up.get("modifiers"), mask)

                    self.assertEqual(down.get("windowsVirtualKeyCode"), VIRTUAL_KEY_CODES[key])
                    self.assertEqual(up.get("windowsVirtualKeyCode"), VIRTUAL_KEY_CODES[key])

                    # Assert absence of char events or keyUp text payload
                    char_events = [e for e in key_events if e["params"].get("type") == "char"]
                    self.assertEqual(len(char_events), 0)
                    self.assertFalse(up.get("text"))

    # --- Press Enter Profiles ---

    def test_press_enter_nontext_profile(self):
        # Defect fx2-f2-executor-actor-chromedriver: press Enter default nontext profile unsupported.
        cdp = RecordingFakeCdp()
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            receipt = run_ax_action(
                cdp,
                "sid-enter-nontext",
                self.nodes,
                {
                    "op": "press",
                    "target": "target",
                    "key": "Enter",
                    "profile": "nontext",
                    "expect": {"target_name": "Prompt"},
                },
                lambda: self.nodes,
                sleep=noop_sleep,
            )
            self.assertEqual(receipt["ok"], 1)
            key_events = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
            self.assertEqual(len(key_events), 2)
            down, up = key_events[0]["params"], key_events[1]["params"]
            self.assertEqual(down.get("type"), "rawKeyDown")
            self.assertEqual(up.get("type"), "keyUp")
            self.assertFalse(down.get("text"))
            self.assertFalse(up.get("text"))

    def test_press_enter_text_profile(self):
        # Defect fx2-f2-executor-actor-chromedriver: press Enter enter_text profile unsupported.
        cdp = RecordingFakeCdp()
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            receipt = run_ax_action(
                cdp,
                "sid-enter-text",
                self.nodes,
                {
                    "op": "press",
                    "target": "target",
                    "key": "Enter",
                    "profile": "enter_text",
                    "expect": {"target_name": "Prompt"},
                },
                lambda: self.nodes,
                sleep=noop_sleep,
            )
            self.assertEqual(receipt["ok"], 1)
            key_events = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
            self.assertEqual(len(key_events), 2)
            down, up = key_events[0]["params"], key_events[1]["params"]
            self.assertEqual(down.get("type"), "keyDown")
            self.assertEqual(down.get("text"), "\r")
            self.assertEqual(up.get("type"), "keyUp")
            self.assertFalse(up.get("text"))

    # --- Press Validation & Guard Rejections ---

    def test_press_rejects_key_aliases(self):
        # Defect fx2-f2-executor-actor-chromedriver: press key alias rejection missing.
        cdp = RecordingFakeCdp()
        aliases = ("Return", "Esc", "Left", "Right", "Up", "Down", "BS")
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            for alias in aliases:
                cdp.calls.clear()
                with self.assertRaises(ActionError):
                    run_ax_action(
                        cdp,
                        "sid-alias",
                        self.nodes,
                        {"op": "press", "target": "target", "key": alias},
                        lambda: self.nodes,
                        sleep=noop_sleep,
                    )
                self.assertEqual(cdp.calls, [])

    def test_press_rejects_chords(self):
        # Defect fx2-f2-executor-actor-chromedriver: press chord string rejection missing.
        cdp = RecordingFakeCdp()
        chords = ("Ctrl+Enter", "Alt+Tab", "Shift+ArrowDown")
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            for chord in chords:
                cdp.calls.clear()
                with self.assertRaises(ActionError):
                    run_ax_action(
                        cdp,
                        "sid-chord",
                        self.nodes,
                        {"op": "press", "target": "target", "key": chord},
                        lambda: self.nodes,
                        sleep=noop_sleep,
                    )
                self.assertEqual(cdp.calls, [])

    def test_press_rejects_non_string_keys(self):
        # Defect fx2-f2-executor-actor-chromedriver: press non-string key rejection missing.
        cdp = RecordingFakeCdp()
        non_strings = (123, True, None, ["Enter"])
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            for bad_key in non_strings:
                cdp.calls.clear()
                with self.assertRaises(ActionError):
                    run_ax_action(
                        cdp,
                        "sid-badkey",
                        self.nodes,
                        {"op": "press", "target": "target", "key": bad_key},
                        lambda: self.nodes,
                        sleep=noop_sleep,
                    )
                self.assertEqual(cdp.calls, [])

    def test_press_rejects_unknown_keys(self):
        # Defect fx2-f2-executor-actor-chromedriver: press unknown key rejection missing.
        cdp = RecordingFakeCdp()
        unknowns = ("Frobnicator", "Space", "KeyA")
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            for unk in unknowns:
                cdp.calls.clear()
                with self.assertRaises(ActionError):
                    run_ax_action(
                        cdp,
                        "sid-unk",
                        self.nodes,
                        {"op": "press", "target": "target", "key": unk},
                        lambda: self.nodes,
                        sleep=noop_sleep,
                    )
                self.assertEqual(cdp.calls, [])

    def test_press_rejects_malformed_modifier_masks(self):
        # Defect fx2-f2-executor-actor-chromedriver: press malformed modifier mask rejection missing.
        cdp = RecordingFakeCdp()
        bad_masks = (True, False, 1.5, -1, 16, "2", None)
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            for mask in bad_masks:
                cdp.calls.clear()
                with self.assertRaises(ActionError):
                    run_ax_action(
                        cdp,
                        "sid-badmask",
                        self.nodes,
                        {"op": "press", "target": "target", "key": "Enter", "modifiers": mask},
                        lambda: self.nodes,
                        sleep=noop_sleep,
                    )
                self.assertEqual(cdp.calls, [])

    # --- Insert Text Unicode & Controls ---

    def test_insert_text_literal_unicode_textbox(self):
        # Defect fx2-f2-executor-actor-chromedriver: insert_text op missing for textbox.
        cdp = RecordingFakeCdp()
        text_to_insert = "preguntá 你好 🌍\n"
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            receipt = run_ax_action(
                cdp,
                "sid-ins-tb",
                self.nodes,
                {
                    "op": "insert_text",
                    "target": "target",
                    "text": text_to_insert,
                    "expect": {"target_name": "Prompt"},
                },
                lambda: self.nodes,
                sleep=noop_sleep,
            )
            self.assertEqual(receipt["ok"], 1)

            insert_calls = [c for c in cdp.calls if c["method"] == "Input.insertText"]
            self.assertEqual(len(insert_calls), 1)
            self.assertEqual(insert_calls[0]["params"].get("text"), text_to_insert)

            # Assert no key events dispatched
            key_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
            self.assertEqual(len(key_calls), 0)

            # Assert no JS value assignment pretending to be insertion
            runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
            for call in runtime_calls:
                func = call["params"].get("functionDeclaration", "")
                self.assertNotIn("this.value =", func)

    def test_insert_text_literal_unicode_combobox(self):
        # Defect fx2-f2-executor-actor-chromedriver: insert_text op missing for combobox.
        cdp = RecordingFakeCdp()
        nodes = [make_node(role="combobox", name="Search")]
        text_to_insert = "preguntá 你好 🌍\n"
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            receipt = run_ax_action(
                cdp,
                "sid-ins-cb",
                nodes,
                {
                    "op": "insert_text",
                    "target": "target",
                    "text": text_to_insert,
                    "expect": {"target_name": "Search"},
                },
                lambda: nodes,
                sleep=noop_sleep,
            )
            self.assertEqual(receipt["ok"], 1)

            insert_calls = [c for c in cdp.calls if c["method"] == "Input.insertText"]
            self.assertEqual(len(insert_calls), 1)
            self.assertEqual(insert_calls[0]["params"].get("text"), text_to_insert)

    # --- Insert Text Preconditions & Guard Rejections ---

    def test_insert_text_rejects_disabled_target(self):
        # Defect fx2-f2-executor-actor-chromedriver: insert_text disabled target rejection missing.
        cdp = RecordingFakeCdp()
        disabled_node = make_node(attrs=(("disabled", "true"),))
        nodes = [disabled_node]
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-ins-dis",
                    nodes,
                    {"op": "insert_text", "target": "target", "text": "hello"},
                    lambda: nodes,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

    def test_insert_text_rejects_noneditable_role(self):
        # Defect fx2-f2-executor-actor-chromedriver: insert_text noneditable role rejection missing.
        cdp = RecordingFakeCdp()
        button_node = make_node(role="button", name="Send")
        nodes = [button_node]
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-ins-btn",
                    nodes,
                    {"op": "insert_text", "target": "target", "text": "hello"},
                    lambda: nodes,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

    def test_insert_text_rejects_absent_or_nonstring_text(self):
        # Defect fx2-f2-executor-actor-chromedriver: insert_text missing or non-string text rejection missing.
        cdp = RecordingFakeCdp()
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            # Missing text
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-ins-notext",
                    self.nodes,
                    {"op": "insert_text", "target": "target"},
                    lambda: self.nodes,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

            # Integer text
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-ins-inttext",
                    self.nodes,
                    {"op": "insert_text", "target": "target", "text": 123},
                    lambda: self.nodes,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

    # --- Focus / Insertion Ordering ---

    def test_insert_text_exact_focus_and_insertion_ordering(self):
        # Defect fx2-f2-executor-actor-chromedriver: focus ordering before insert_text missing.
        cdp = RecordingFakeCdp()
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            receipt = run_ax_action(
                cdp,
                "sid-order",
                self.nodes,
                {
                    "op": "insert_text",
                    "target": "target",
                    "text": "test",
                    "expect": {"target_name": "Prompt"},
                },
                lambda: self.nodes,
                sleep=noop_sleep,
            )
            self.assertEqual(receipt["ok"], 1)

            methods = [c["method"] for c in cdp.calls]
            self.assertIn("DOM.resolveNode", methods)
            self.assertIn("Input.insertText", methods)

            resolve_idx = methods.index("DOM.resolveNode")
            insert_idx = methods.index("Input.insertText")
            self.assertLess(resolve_idx, insert_idx)

    # --- Unmet Explicit Postconditions ---

    def test_press_unmet_explicit_postcondition_returns_unverified(self):
        """Unmet explicit postcondition returns unverified (passing contract)."""
        cdp = RecordingFakeCdp()
        busy_node = make_node(attrs=(("busy", "false"),))
        nodes = [busy_node]
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            receipt = run_ax_action(
                cdp,
                "sid-press-unmet",
                nodes,
                {
                    "op": "press",
                    "target": "target",
                    "key": "Enter",
                    "expect": {"target_states": {"busy": "true"}},
                },
                lambda: nodes,
                sleep=noop_sleep,
            )
            self.assertEqual(receipt["verified"], 0)

    def test_insert_text_unmet_explicit_postcondition_returns_unverified(self):
        """Unmet explicit postcondition returns unverified (passing contract)."""
        cdp = RecordingFakeCdp()
        busy_node = make_node(attrs=(("busy", "false"),))
        nodes = [busy_node]
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            receipt = run_ax_action(
                cdp,
                "sid-ins-unmet",
                nodes,
                {
                    "op": "insert_text",
                    "target": "target",
                    "text": "hello",
                    "expect": {"target_states": {"busy": "true"}},
                },
                lambda: nodes,
                sleep=noop_sleep,
            )
            self.assertEqual(receipt["verified"], 0)

    # --- Partial-Send Failures Without Replay ---

    @unittest.expectedFailure
    def test_press_partial_send_failure_no_replay(self):
        # Defect fx2-f2-executor-actor-chromedriver: press partial send failure handling without replay missing.
        send_count = 0

        def failing_send(method, params=None, session_id=None):
            nonlocal send_count
            if method == "Input.dispatchKeyEvent":
                send_count += 1
                if send_count > 1:
                    raise RuntimeError("CDP disconnect on keyUp")
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "obj"}}
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": True}}
            return {}

        cdp = RecordingFakeCdp()
        cdp.send = failing_send

        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            with self.assertRaises((ActionError, RuntimeError)):
                run_ax_action(
                    cdp,
                    "sid-press-fail",
                    self.nodes,
                    {"op": "press", "target": "target", "key": "Enter"},
                    lambda: self.nodes,
                    sleep=noop_sleep,
                )
            # Ensure no secondary retry/replay attempt was sent after failure
            self.assertEqual(send_count, 2)

    @unittest.expectedFailure
    def test_insert_text_partial_send_failure_no_replay(self):
        # Defect fx2-f2-executor-actor-chromedriver: insert_text partial send failure handling without replay missing.
        send_count = 0

        def failing_send(method, params=None, session_id=None):
            nonlocal send_count
            if method == "Input.insertText":
                send_count += 1
                raise RuntimeError("CDP disconnect on insertText")
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "obj"}}
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": True}}
            return {}

        cdp = RecordingFakeCdp()
        cdp.send = failing_send

        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("insert_text",)):
            with self.assertRaises((ActionError, RuntimeError)):
                run_ax_action(
                    cdp,
                    "sid-ins-fail",
                    self.nodes,
                    {"op": "insert_text", "target": "target", "text": "test"},
                    lambda: self.nodes,
                    sleep=noop_sleep,
                )
            # Ensure insertText was attempted once and not retried/replayed
            self.assertEqual(send_count, 1)


if __name__ == "__main__":
    unittest.main()
