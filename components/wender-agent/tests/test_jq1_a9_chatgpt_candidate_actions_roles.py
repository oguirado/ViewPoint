"""Test for ChatGPT candidate actions & roles contract, state validation, and driver role coverage.

Claim: ChromeDriver.candidate_actions(state) must expose actionable menuitem, slider, and
switch controls (or pin that wender/learn/chrome_driver.py:115 currently drops them by filtering
role in ("link", "button") despite state["nodes"][i]["actionable"] is True).
"""
import importlib
import os
from html.parser import HTMLParser
import pathlib
import unittest

from wender.learn.chrome_driver import ChromeDriver
from wender.perceive import ax
from wender.state import schema


def _has_symbol(module_name, symbol_name):
    try:
        mod = importlib.import_module(module_name)
        return hasattr(mod, symbol_name)
    except ImportError:
        return False


class ChatGPTFixtureHTMLParser(HTMLParser):
    """stdlib HTMLParser to verify the HTML fixture role and name contract."""

    def __init__(self):
        super().__init__()
        self.controls = []
        self.iframes = []
        self._current_tag = None
        self._current_attrs = {}

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        self._current_tag = tag
        self._current_attrs = attr_dict

        if tag == "iframe":
            self.iframes.append({
                "srcdoc": attr_dict.get("srcdoc"),
                "id": attr_dict.get("id"),
            })

        role = attr_dict.get("role")
        aria_label = attr_dict.get("aria-label") or attr_dict.get("placeholder")

        if tag == "button":
            role = role or "button"
        elif tag == "input" and attr_dict.get("type", "text") == "text":
            role = role or "textbox"

        if role:
            self.controls.append({
                "tag": tag,
                "role": role,
                "aria_label": aria_label,
                "id": attr_dict.get("id"),
                "text": "",
            })

    def handle_data(self, data):
        data_stripped = data.strip()
        if data_stripped and self.controls:
            # Append text to the last recorded control if relevant
            if not self.controls[-1]["text"]:
                self.controls[-1]["text"] = data_stripped


class FakeCDPAdapter:
    """In-memory stateful FakeCDP adapter supplying raw AX tree for ChatGPT replica."""

    def __init__(self, raw_nodes=None):
        self.raw_nodes = raw_nodes or []
        self.enabled = False

    def send(self, method, params=None, session_id=None):
        if method == "Accessibility.enable":
            self.enabled = True
            return {}
        elif method == "Accessibility.disable":
            self.enabled = False
            return {}
        elif method == "Accessibility.getFullAXTree":
            return {"nodes": self.raw_nodes}
        raise ValueError("Unsupported CDP method in FakeCDPAdapter: %s" % method)


class LocalWaitTextStopAdapter:
    """Proposed invocation contracts for absent modules WP1-02, WP1-04, and WP1-05.

    Clearly labelled local adapter to avoid claiming an invented signature in production.
    """

    def wait_done(self, cdp, session_id, timeout=10.0):
        """Proposed contract for WP1-02: wender.perceive.wait.wait_done."""
        return True

    def extract(self, state, query=""):
        """Proposed contract for WP1-04: wender.perceive.text.extract."""
        return {"text": ""}

    def detect(self, state):
        """Proposed contract for WP1-05: wender.perceive.stopstate.detect."""
        return {"stopped": True, "reason": "idle"}


class TestChatGPTCandidateActionsRoles(unittest.TestCase):
    """Test suite for ChatGPT candidate actions, state validation, and role coverage."""

    def setUp(self):
        self.fixture_path = (
            pathlib.Path(__file__).resolve().parent
            / "fixtures"
            / "sites"
            / "jq1_a9_chatgpt_candidate_actions_roles.html"
        )

    def test_html_fixture_parser_and_contract(self):
        """Passing assertion: Read HTML fixture using stdlib HTMLParser to verify role/name contract."""
        self.assertTrue(self.fixture_path.exists(), "HTML fixture file must exist")
        html_text = self.fixture_path.read_text(encoding="utf-8")

        parser = ChatGPTFixtureHTMLParser()
        parser.feed(html_text)

        roles_found = {c["role"]: c for c in parser.controls}
        self.assertIn("button", roles_found)
        self.assertIn("menuitem", roles_found)
        self.assertIn("slider", roles_found)
        self.assertIn("switch", roles_found)
        self.assertIn("textbox", roles_found)

        # Check inert iframe srcdoc
        self.assertEqual(len(parser.iframes), 1)
        self.assertIsNotNone(parser.iframes[0]["srcdoc"])
        self.assertIn("Inert Challenge Content", parser.iframes[0]["srcdoc"])

    def test_fake_cdp_adapter_and_ax_prune_integration(self):
        """Passing assertion: Exercise production ax.raw_tree and ax.prune via FakeCDPAdapter."""
        raw_nodes = [
            {
                "nodeId": "1",
                "ignored": False,
                "role": {"value": "RootWebArea"},
                "name": {"value": "ChatGPT"},
                "childIds": ["2", "3", "4", "5", "6"],
            },
            {
                "nodeId": "2",
                "ignored": False,
                "role": {"value": "button"},
                "name": {"value": "Enviar"},
                "backendDOMNodeId": 101,
            },
            {
                "nodeId": "3",
                "ignored": False,
                "role": {"value": "menuitem"},
                "name": {"value": "Potencia"},
                "backendDOMNodeId": 102,
            },
            {
                "nodeId": "4",
                "ignored": False,
                "role": {"value": "slider"},
                "name": {"value": "Alta"},
                "backendDOMNodeId": 103,
            },
            {
                "nodeId": "5",
                "ignored": False,
                "role": {"value": "switch"},
                "name": {"value": "Razona"},
                "backendDOMNodeId": 104,
            },
            {
                "nodeId": "6",
                "ignored": False,
                "role": {"value": "textbox"},
                "name": {"value": "Pregunta lo que quieras"},
                "backendDOMNodeId": 105,
            },
        ]
        adapter = FakeCDPAdapter(raw_nodes)

        fetched_raw = ax.raw_tree(adapter, "s1")
        self.assertEqual(len(fetched_raw), len(raw_nodes))

        pruned = ax.prune(fetched_raw)
        roles = [n["role"] for n in pruned]
        self.assertEqual(
            roles, ["RootWebArea", "button", "menuitem", "slider", "switch", "textbox"]
        )

        released = ax.release(adapter, "s1")
        self.assertTrue(released)

    def test_chatgpt_replica_state_build_and_validation(self):
        """Passing assertion: Build state via schema.build_state from pruned nodes and validate.

        Verifies that menuitem, slider, switch, and textbox nodes have node["actionable"] is True
        and schema.validate_state(state) == []. Also tests negative state behavior for non-actionable nodes.
        """
        raw_nodes = [
            {
                "nodeId": "1",
                "ignored": False,
                "role": {"value": "RootWebArea"},
                "name": {"value": "ChatGPT"},
                "childIds": ["2", "3", "4", "5", "6", "7"],
            },
            {
                "nodeId": "2",
                "ignored": False,
                "role": {"value": "button"},
                "name": {"value": "Enviar"},
                "backendDOMNodeId": 101,
            },
            {
                "nodeId": "3",
                "ignored": False,
                "role": {"value": "menuitem"},
                "name": {"value": "Potencia"},
                "backendDOMNodeId": 102,
            },
            {
                "nodeId": "4",
                "ignored": False,
                "role": {"value": "slider"},
                "name": {"value": "Alta"},
                "backendDOMNodeId": 103,
            },
            {
                "nodeId": "5",
                "ignored": False,
                "role": {"value": "switch"},
                "name": {"value": "Razona"},
                "backendDOMNodeId": 104,
            },
            {
                "nodeId": "6",
                "ignored": False,
                "role": {"value": "textbox"},
                "name": {"value": "Pregunta lo que quieras"},
                "backendDOMNodeId": 105,
            },
            {
                "nodeId": "7",
                "ignored": False,
                "role": {"value": "button"},
                "name": {"value": "Desactivado"},
                "properties": [{"name": "disabled", "value": {"value": True}}],
                "backendDOMNodeId": 106,
            },
        ]

        pruned = ax.prune(raw_nodes)

        boxes = {
            n["id"]: [10, 10 + i * 40, 120, 30] for i, n in enumerate(pruned)
        }

        state = schema.build_state(
            pruned,
            kind="cdp",
            url="file:///chatgpt.html",
            title="ChatGPT Replica",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=len(raw_nodes),
            boxes=boxes,
            viewport={
                "w": 1024,
                "h": 768,
                "scroll_x": 0,
                "scroll_y": 0,
                "doc_w": 1024,
                "doc_h": 768,
                "dpr_ppm": 1000000,
            },
            actionable_roles=ax.ACTIONABLE_ROLES,
        )

        validation_errors = schema.validate_state(state)
        self.assertEqual(
            validation_errors, [], "state validation must produce zero errors"
        )

        nodes_by_name = {n["name"]: n for n in state["nodes"]}

        # Assert button, menuitem, slider, switch, and textbox all have node["actionable"] is True
        self.assertTrue(nodes_by_name["Enviar"]["actionable"])
        self.assertTrue(nodes_by_name["Potencia"]["actionable"])
        self.assertTrue(nodes_by_name["Alta"]["actionable"])
        self.assertTrue(nodes_by_name["Razona"]["actionable"])
        self.assertTrue(nodes_by_name["Pregunta lo que quieras"]["actionable"])

        # Verify negative state (disabled button) is NOT actionable
        self.assertFalse(
            nodes_by_name["Desactivado"]["actionable"],
            "disabled node must have actionable == False",
        )

    def test_candidate_actions_role_coverage_starvation(self):
        """Expected failure: ChromeDriver.candidate_actions role starvation defect.

        Pinning that wender/learn/chrome_driver.py:115 currently drops menuitem, slider, and switch
        controls by filtering role in ("link", "button") despite state["nodes"][i]["actionable"] is True.
        """
        raw_nodes = [
            {
                "nodeId": "1",
                "ignored": False,
                "role": {"value": "RootWebArea"},
                "name": {"value": "ChatGPT"},
                "childIds": ["2", "3", "4", "5", "6"],
            },
            {
                "nodeId": "2",
                "ignored": False,
                "role": {"value": "button"},
                "name": {"value": "Enviar"},
                "backendDOMNodeId": 101,
            },
            {
                "nodeId": "3",
                "ignored": False,
                "role": {"value": "menuitem"},
                "name": {"value": "Potencia"},
                "backendDOMNodeId": 102,
            },
            {
                "nodeId": "4",
                "ignored": False,
                "role": {"value": "slider"},
                "name": {"value": "Alta"},
                "backendDOMNodeId": 103,
            },
            {
                "nodeId": "5",
                "ignored": False,
                "role": {"value": "switch"},
                "name": {"value": "Razona"},
                "backendDOMNodeId": 104,
            },
            {
                "nodeId": "6",
                "ignored": False,
                "role": {"value": "textbox"},
                "name": {"value": "Pregunta lo que quieras"},
                "backendDOMNodeId": 105,
            },
        ]
        pruned = ax.prune(raw_nodes)
        boxes = {n["id"]: [10, 10 + i * 40, 100, 30] for i, n in enumerate(pruned)}

        state = schema.build_state(
            pruned,
            kind="cdp",
            url="file:///chatgpt.html",
            title="ChatGPT Replica",
            ax_sha256="c" * 64,
            ax_stable_sha256="d" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=len(raw_nodes),
            boxes=boxes,
            viewport={
                "w": 1024,
                "h": 768,
                "scroll_x": 0,
                "scroll_y": 0,
                "doc_w": 1024,
                "doc_h": 768,
                "dpr_ppm": 1000000,
            },
            actionable_roles=ax.ACTIONABLE_ROLES,
        )

        driver = ChromeDriver(None, None, None)
        actions = driver.candidate_actions(state)

        # Target node IDs for menuitem, slider, switch
        id_menuitem = [n["id"] for n in state["nodes"] if n["role"] == "menuitem"][0]
        id_slider = [n["id"] for n in state["nodes"] if n["role"] == "slider"][0]
        id_switch = [n["id"] for n in state["nodes"] if n["role"] == "switch"][0]

        target_ids = {a["target"] for a in actions}

        # This assertion fails because wender/learn/chrome_driver.py:115 filters role in ("link", "button")
        self.assertIn(
            id_menuitem,
            target_ids,
            "candidate_actions must include actionable menuitem node",
        )
        self.assertIn(
            id_slider,
            target_ids,
            "candidate_actions must include actionable slider node",
        )
        self.assertIn(
            id_switch,
            target_ids,
            "candidate_actions must include actionable switch node",
        )

    def test_absent_production_modules_wp1_02_04_05(self):
        """Expected failure for production modules AUSENTE in supplied corpus.

        WP1-02: wender.perceive.wait.wait_done
        WP1-04: wender.perceive.text.extract
        WP1-05: wender.perceive.stopstate.detect

        Proposed contracts are demonstrated in LocalWaitTextStopAdapter.
        """
        adapter = LocalWaitTextStopAdapter()
        # Verify local adapter contract works
        self.assertTrue(adapter.wait_done(None, "s1"))
        self.assertEqual(adapter.extract({}, "")["text"], "")
        self.assertTrue(adapter.detect({})["stopped"])

        # Production check: assert presence of missing modules (fails as modules are absent)
        self.assertTrue(
            _has_symbol("wender.perceive.wait", "wait_done"),
            "WP1-02 wait_done must exist in wender.perceive.wait",
        )
        self.assertTrue(
            _has_symbol("wender.perceive.text", "extract"),
            "WP1-04 extract must exist in wender.perceive.text",
        )
        self.assertTrue(
            _has_symbol("wender.perceive.stopstate", "detect"),
            "WP1-05 detect must exist in wender.perceive.stopstate",
        )


if __name__ == "__main__":
    unittest.main()
