"""Test suite for jq1-a9-google-ai-mode-combobox-enter (WP1-03).

Claim: Submitting a query in Google "Modo IA" requires insert_text into combobox
"¿Qué tienes en mente?" followed by press (key="Enter") in cdp_executor.run_ax_action,
transitioning the page URL to https://www.google.com/search?q=vision+engine&udm=50.
"""
from html.parser import HTMLParser
import importlib
import pathlib
import unittest

from wender.act.cdp_executor import ActionError, run_ax_action
from wender.perceive import ax


class GoogleAiHtmlParser(HTMLParser):
    """Stdlib HTMLParser to extract roles and accessible names from fixture HTML."""

    def __init__(self):
        super().__init__()
        self.headings = []
        self.comboboxes = []
        self.iframes = []
        self._current_tag = None
        self._current_attrs = {}
        self._text_buffer = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        self._current_tag = tag
        self._current_attrs = attr_dict
        self._text_buffer = []
        role = attr_dict.get("role")
        aria_label = attr_dict.get("aria-label")
        placeholder = attr_dict.get("placeholder")
        backend_id = attr_dict.get("data-backend-id")

        if role == "combobox" or tag == "input":
            name = aria_label or placeholder or ""
            self.comboboxes.append({
                "tag": tag,
                "role": role or "combobox",
                "name": name,
                "backend_id": int(backend_id) if backend_id else None,
            })
        elif tag == "iframe":
            self.iframes.append({
                "title": attr_dict.get("title"),
                "srcdoc": attr_dict.get("srcdoc"),
            })

    def handle_data(self, data):
        if self._current_tag in ("h1", "h2", "h3"):
            self._text_buffer.append(data)

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3") and self._text_buffer:
            heading_text = "".join(self._text_buffer).strip()
            if heading_text:
                self.headings.append({
                    "tag": tag,
                    "text": heading_text,
                })
            self._text_buffer = []


def _raw_google_ai_initial():
    """Initial pruned raw AX snapshot for Google AI mode."""
    return [
        {
            "nodeId": "1",
            "role": {"type": "role", "value": "RootWebArea"},
            "name": {"type": "computedString", "value": "Google - Modo IA"},
            "childIds": ["2"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"type": "role", "value": "main"},
            "name": {"type": "computedString", "value": ""},
            "childIds": ["10", "20"],
            "ignored": False,
        },
        {
            "nodeId": "10",
            "role": {"type": "role", "value": "heading"},
            "name": {"type": "computedString", "value": "¿Qué tienes en mente?"},
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "20",
            "role": {"type": "role", "value": "combobox"},
            "name": {"type": "computedString", "value": "¿Qué tienes en mente?"},
            "childIds": [],
            "ignored": False,
            "backendDOMNodeId": 610,
            "properties": [
                {"name": "disabled", "value": {"type": "value", "value": False}},
            ],
        },
    ]


def _raw_google_ai_submitted():
    """Post-submit pruned raw AX snapshot at https://www.google.com/search?q=vision+engine&udm=50."""
    return [
        {
            "nodeId": "1",
            "role": {"type": "role", "value": "RootWebArea"},
            "name": {"type": "computedString", "value": "Resultados para: vision engine"},
            "childIds": ["2"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"type": "role", "value": "main"},
            "name": {"type": "computedString", "value": ""},
            "childIds": ["10", "20", "30"],
            "ignored": False,
        },
        {
            "nodeId": "10",
            "role": {"type": "role", "value": "heading"},
            "name": {"type": "computedString", "value": "Resultados para: vision engine"},
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "20",
            "role": {"type": "role", "value": "combobox"},
            "name": {"type": "computedString", "value": "¿Qué tienes en mente?"},
            "childIds": [],
            "ignored": False,
            "backendDOMNodeId": 610,
        },
        {
            "nodeId": "30",
            "role": {"type": "role", "value": "region"},
            "name": {"type": "computedString", "value": "Resultados de búsqueda"},
            "childIds": ["40"],
            "ignored": False,
        },
        {
            "nodeId": "40",
            "role": {"type": "role", "value": "article"},
            "name": {"type": "computedString", "value": "Vision Engine Overview"},
            "childIds": [],
            "ignored": False,
        },
    ]


class FakeCdp:
    """Stateful fake CDP adapter recording CDP calls and modeling page state."""

    def __init__(self):
        self.calls = []
        self.url = "https://www.google.com/mode/ai"
        self.combobox_value = ""

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-%s" % payload.get("backendNodeId")}}
        if method == "DOM.scrollIntoViewIfNeeded":
            return {}
        if method == "Input.insertText":
            self.combobox_value = payload.get("text", "")
            return {}
        if method == "Input.dispatchKeyEvent":
            if payload.get("key") == "Enter":
                q = self.combobox_value or "vision+engine"
                self.url = "https://www.google.com/search?q=%s&udm=50" % (q,)
            return {}
        return {}


class ScriptedReadNodes:
    """Callable returning sequence of pruned snapshot node lists."""

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


class ProposedGoogleAiModeAdapter:
    """Clearly labelled local adapter outlining proposed invocation contract for insert_text and press."""

    def __init__(self, cdp):
        self.cdp = cdp

    def insert_text(self, target_node, text):
        # Proposed contract: Input.insertText over CDP
        backend_id = target_node.get("backend_id")
        self.cdp.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
        self.cdp.send("Input.insertText", {"text": text})

    def press(self, target_node, key):
        # Proposed contract: Input.dispatchKeyEvent over CDP
        self.cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        self.cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})


class TestJq1A9GoogleAiModeComboboxEnterFixture(unittest.TestCase):
    """Passing tests verifying the HTML fixture contract and in-memory AX snapshot construction."""

    def test_fixture_html_parse_contract(self):
        fixture_path = (
            pathlib.Path(__file__).parent
            / "fixtures"
            / "sites"
            / "jq1_a9_google_ai_mode_combobox_enter.html"
        )
        self.assertTrue(fixture_path.exists(), "Fixture HTML file must exist")
        html_content = fixture_path.read_text(encoding="utf-8")

        parser = GoogleAiHtmlParser()
        parser.feed(html_content)

        # Pin heading "¿Qué tienes en mente?"
        heading_texts = [h["text"] for h in parser.headings]
        self.assertIn("¿Qué tienes en mente?", heading_texts)

        # Pin combobox "¿Qué tienes en mente?" with backendDOMNodeId 610
        comboboxes = parser.comboboxes
        self.assertTrue(len(comboboxes) > 0)
        target_cb = next((c for c in comboboxes if c["name"] == "¿Qué tienes en mente?"), None)
        self.assertIsNotNone(target_cb)
        self.assertEqual(target_cb["backend_id"], 610)

        # Pin challenge iframe with inert srcdoc
        self.assertTrue(len(parser.iframes) > 0)
        self.assertIn("Inert Challenge", parser.iframes[0]["srcdoc"])

    def test_pruned_ax_snapshot_structure(self):
        raw_initial = _raw_google_ai_initial()
        pruned_initial = ax.prune(raw_initial)

        heading_node = next((n for n in pruned_initial if n["role"] == "heading"), None)
        self.assertIsNotNone(heading_node)
        self.assertEqual(heading_node["name"], "¿Qué tienes en mente?")

        combobox_node = next((n for n in pruned_initial if n["role"] == "combobox"), None)
        self.assertIsNotNone(combobox_node)
        self.assertEqual(combobox_node["name"], "¿Qué tienes en mente?")
        self.assertEqual(combobox_node["backend_id"], 610)

        raw_submitted = _raw_google_ai_submitted()
        pruned_submitted = ax.prune(raw_submitted)

        article_node = next((n for n in pruned_submitted if n["role"] == "article"), None)
        self.assertIsNotNone(article_node)
        self.assertEqual(article_node["name"], "Vision Engine Overview")

    def test_negative_state_rejections(self):
        """Verify negative states that would fool an overly permissive predicate."""
        cdp = FakeCdp()
        initial_nodes = ax.prune(_raw_google_ai_initial())
        reader = ScriptedReadNodes([initial_nodes])

        # 1. Non-existent target ID raises ActionError
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "s1",
                initial_nodes,
                {"op": "click", "target": "invalid_node_id"},
                reader,
            )

        # 2. Unsupported op raises ActionError
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "s1",
                initial_nodes,
                {"op": "invalid_op", "target": initial_nodes[0]["id"]},
                reader,
            )

        # 3. Expectation mismatch fails verification
        combobox = next(n for n in initial_nodes if n["role"] == "combobox")
        receipt = run_ax_action(
            cdp,
            "s1",
            initial_nodes,
            {
                "op": "focus",
                "target": combobox["id"],
                "expect": {"target_states": {"busy": "true"}},
            },
            reader,
            retries=1,
            sleep=lambda s: None,
        )
        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)


class TestJq1A9GoogleAiModeComboboxEnterExpectedFailures(unittest.TestCase):
    """Expected failure tests for missing ops and absent modules."""

    def test_wp1_03_google_ai_mode_combobox_insert_text_and_enter(self):
        # WP1-03: wender/act/cdp_executor.py:15 lacks "insert_text" and "press" ops.
        # Calling run_ax_action with insert_text or press raises ActionError: unsupported op
        # until "insert_text" and "press" are added to cdp_executor.OPS and JS_BY_OP.
        cdp = FakeCdp()
        initial_nodes = ax.prune(_raw_google_ai_initial())
        post_nodes = ax.prune(_raw_google_ai_submitted())
        combobox = next(n for n in initial_nodes if n["role"] == "combobox")

        reader_insert = ScriptedReadNodes([initial_nodes])
        # Attempt insert_text op
        receipt_insert = run_ax_action(
            cdp,
            "s-ai-1",
            initial_nodes,
            {
                "op": "insert_text",
                "target": combobox["id"],
                "text": "vision engine",
            },
            reader_insert,
            sleep=lambda s: None,
        )
        # Once implemented, this line will be reached and pass:
        self.assertEqual(receipt_insert["ok"], 1)

        # Attempt press op
        reader_press = ScriptedReadNodes([post_nodes])
        receipt_press = run_ax_action(
            cdp,
            "s-ai-2",
            initial_nodes,
            {
                "op": "press",
                "target": combobox["id"],
                "key": "Enter",
            },
            reader_press,
            sleep=lambda s: None,
        )
        self.assertEqual(receipt_press["ok"], 1)
        self.assertIn("Input.insertText", [c["method"] for c in cdp.calls])
        self.assertIn("Input.dispatchKeyEvent", [c["method"] for c in cdp.calls])

    def test_wp1_02_ax_completion_waiter_exists(self):
        # WP1-02: wender.perceive.wait.wait_done is absent in current corpus
        mod = importlib.import_module("wender.perceive.wait")
        self.assertTrue(hasattr(mod, "wait_done"))

    def test_wp1_04_answer_text_extractor_exists(self):
        # WP1-04: wender.perceive.text.extract is absent in current corpus
        mod = importlib.import_module("wender.perceive.text")
        self.assertTrue(hasattr(mod, "extract"))

    def test_wp1_05_stop_state_detector_exists(self):
        # WP1-05: wender.perceive.stopstate.detect is absent in current corpus
        mod = importlib.import_module("wender.perceive.stopstate")
        self.assertTrue(hasattr(mod, "detect"))


if __name__ == "__main__":
    unittest.main()
