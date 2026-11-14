"""Unified offline 5-replica E2E harness test suite for web site workflows.

Workflows tested:
1. ChatGPT Radix Potencia slider
2. ChatGPT streaming turn
3. Perplexity L-path menu
4. Gemini model / Deep Research panel
5. Google cookie + Modo IA + /sorry/ wall

STORAGE LAW: frame and screenshot bytes remain in RAM as bytes objects and are never written to disk.
"""
import base64
from html.parser import HTMLParser
import os
import struct
import unittest
import zlib

from wender.act import cdp_executor
from wender.learn.chrome_driver import ChromeDriver
from wender.learn.replay import _eval_expect, replay_and_admit
from wender.perceive import ax, vision_adapter
from wender.state import schema


def make_minimal_png_bytes(width=1, height=1, color=(0, 255, 0)):
    """Synthesize valid minimal PNG bytes strictly in RAM."""
    raw_data = bytearray()
    r, g, b = color
    for _ in range(height):
        raw_data.append(0)  # No filter
        for _ in range(width):
            raw_data.extend([r, g, b])
    idat_data = zlib.compress(bytes(raw_data))

    def chunk(type_bytes, data):
        crc = zlib.crc32(type_bytes + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + type_bytes + data + struct.pack(">I", crc)

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_data)
        + chunk(b"IDAT", idat_data)
        + chunk(b"IEND", b"")
    )
    return png


class SiteFixtureParser(HTMLParser):
    """Stdlib HTMLParser pinning role and accessible name contracts from fixture HTML."""

    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        role = attr_dict.get("role")
        aria_label = attr_dict.get("aria-label")
        name_attr = attr_dict.get("name")
        self.elements.append({
            "tag": tag,
            "id": attr_dict.get("id"),
            "role": role,
            "aria_label": aria_label,
            "name": name_attr,
            "aria_expanded": attr_dict.get("aria-expanded"),
            "aria_pressed": attr_dict.get("aria-pressed"),
            "aria_checked": attr_dict.get("aria-checked"),
            "srcdoc": attr_dict.get("srcdoc"),
        })


def _raw_ax_node(nid, role, name="", kids=(), ignored=False, props=None, backend=None):
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


class ReplicaFakeCdp:
    """Stateful fake CDP connection supporting 5 site replica workflows."""

    def __init__(self, url):
        self.url = url
        self.state_idx = 0
        self.calls = []
        self.png_bytes = make_minimal_png_bytes(16, 16, (10, 20, 30))

    def _get_raw_tree_for_replica(self):
        if "potencia" in self.url or "chatgpt-potencia" in self.url:
            valuenow = "50" if self.state_idx == 0 else "75"
            return [
                _raw_ax_node(1, "RootWebArea", "ChatGPT Radix Potencia", [2]),
                _raw_ax_node(2, "main", "Potencia Workspace", [10]),
                _raw_ax_node(
                    10,
                    "slider",
                    "Potencia",
                    props={"value": valuenow, "disabled": False},
                    backend=100,
                ),
            ]
        elif "streaming" in self.url or "chatgpt-streaming" in self.url:
            if self.state_idx == 0:
                return [
                    _raw_ax_node(1, "RootWebArea", "ChatGPT Streaming", [2]),
                    _raw_ax_node(2, "main", "Chat Interface", [10, 20, 30]),
                    _raw_ax_node(10, "log", "Chat Log", [11]),
                    _raw_ax_node(11, "article", "Assistant message", backend=101),
                    _raw_ax_node(20, "textbox", "Send a message", backend=200),
                    _raw_ax_node(30, "button", "Send", backend=300),
                ]
            else:
                return [
                    _raw_ax_node(1, "RootWebArea", "ChatGPT Streaming", [2]),
                    _raw_ax_node(2, "main", "Chat Interface", [10, 20, 40]),
                    _raw_ax_node(10, "log", "Chat Log", [11, 12, 13]),
                    _raw_ax_node(11, "article", "Assistant message", backend=101),
                    _raw_ax_node(12, "article", "User message", backend=102),
                    _raw_ax_node(
                        13,
                        "article",
                        "Assistant streaming response",
                        props={"busy": True},
                        backend=103,
                    ),
                    _raw_ax_node(20, "textbox", "Send a message", backend=200),
                    _raw_ax_node(40, "button", "Stop", backend=400),
                ]
        elif "lpath" in self.url or "perplexity" in self.url:
            expanded = False if self.state_idx == 0 else True
            nodes = [
                _raw_ax_node(1, "RootWebArea", "Perplexity L-path", [2]),
                _raw_ax_node(2, "navigation", "Perplexity Navigation", [10, 20]),
                _raw_ax_node(
                    10,
                    "button",
                    "Model selector",
                    props={"expanded": expanded},
                    backend=100,
                ),
            ]
            if self.state_idx == 0:
                nodes.append(_raw_ax_node(20, "menu", "L-path menu", ignored=True, backend=200))
            else:
                nodes.extend([
                    _raw_ax_node(20, "menu", "L-path menu", [21, 22, 23], backend=200),
                    _raw_ax_node(21, "menuitem", "Pro search", backend=201),
                    _raw_ax_node(22, "menuitem", "Sonar Medium", backend=202),
                    _raw_ax_node(23, "menuitem", "GPT-4o", backend=203),
                ])
            return nodes
        elif "gemini" in self.url:
            pressed = False if self.state_idx == 0 else True
            nodes = [
                _raw_ax_node(1, "RootWebArea", "Gemini Panel", [2]),
                _raw_ax_node(2, "toolbar", "Gemini Options", [10, 20]),
                _raw_ax_node(10, "button", "Gemini 1.5 Pro", backend=100),
                _raw_ax_node(
                    20,
                    "button",
                    "Deep Research",
                    props={"pressed": pressed},
                    backend=200,
                ),
            ]
            if self.state_idx > 0:
                nodes.extend([
                    _raw_ax_node(30, "region", "Deep Research Panel", [31], backend=300),
                    _raw_ax_node(31, "article", "Detailed synthesis enabled", backend=301),
                ])
                nodes[1]["childIds"].append("30")
            return nodes
        elif "google" in self.url:
            if self.state_idx == 0:
                return [
                    _raw_ax_node(1, "RootWebArea", "Google Search", [2]),
                    _raw_ax_node(2, "dialog", "Cookie Consent", [10]),
                    _raw_ax_node(10, "button", "Accept all", backend=100),
                ]
            else:
                checked = True if self.state_idx > 1 else False
                nodes = [
                    _raw_ax_node(1, "RootWebArea", "Google Search", [2]),
                    _raw_ax_node(2, "main", "Search Controls", [20]),
                    _raw_ax_node(
                        20,
                        "switch",
                        "Modo IA",
                        props={"checked": checked},
                        backend=200,
                    ),
                ]
                if self.state_idx > 1:
                    nodes.extend([
                        _raw_ax_node(30, "region", "Challenge Wall", [31], backend=300),
                        _raw_ax_node(31, "heading", "Unusual traffic", backend=301),
                    ])
                    nodes[1]["childIds"].append("30")
                return nodes
        else:
            return [
                _raw_ax_node(1, "RootWebArea", "Default Page", [2]),
                _raw_ax_node(2, "button", "Default Action", backend=100),
            ]

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})

        if method == "Accessibility.enable":
            return {}
        elif method == "Accessibility.disable":
            return {}
        elif method == "Accessibility.getFullAXTree":
            return {"nodes": self._get_raw_tree_for_replica()}
        elif method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        elif method == "DOM.getBoxModel":
            return {"model": {"border": [10, 10, 110, 10, 110, 50, 10, 50]}}
        elif method == "DOM.scrollIntoViewIfNeeded":
            return {}
        elif method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-%s" % payload.get("backendNodeId")}}
        elif method == "Runtime.callFunctionOn":
            # Simulate DOM state transition upon action execution
            self.state_idx += 1
            return {"result": {"type": "undefined"}}
        elif method == "Page.captureScreenshot":
            return {"data": base64.b64encode(self.png_bytes).decode("ascii")}
        return {}

    def evaluate(self, session_id, expression):
        if "location.href" in expression:
            return f'["{self.url}", "Replica Title"]'
        if "wenderViewport" in expression:
            return '{"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": 1000000}'
        return "{}"

    def navigate(self, session_id, url):
        self.url = url
        self.state_idx = 0


class TestFiveReplicasHarness(unittest.TestCase):
    """Passing fixture and production integration assertions across all 5 site replicas."""

    @classmethod
    def setUpClass(cls):
        fixture_path = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "sites",
            "jq1_a9_five_replicas_e2e_harness.html",
        )
        with open(fixture_path, "r", encoding="utf-8") as f:
            cls.fixture_html = f.read()

    def test_fixture_contract_with_html_parser(self):
        """Pin role and accessible name contracts from HTML fixture using stdlib HTMLParser."""
        parser = SiteFixtureParser()
        parser.feed(self.fixture_html)

        roles_and_labels = {
            (elem.get("role"), elem.get("aria_label"))
            for elem in parser.elements
            if elem.get("role") or elem.get("aria_label")
        }

        expected_contracts = [
            ("slider", "Potencia"),
            ("log", "Chat Log"),
            ("textbox", "Send a message"),
            ("button", "Send message"),
            ("navigation", "Perplexity Navigation"),
            ("button", "Model selector"),
            ("menu", "L-path menu"),
            ("menuitem", "Pro search"),
            ("toolbar", "Gemini Options"),
            ("button", "Deep Research"),
            ("region", "Deep Research Panel"),
            ("dialog", "Cookie Consent"),
            ("button", "Accept all"),
            ("switch", "Modo IA"),
        ]

        for role, label in expected_contracts:
            self.assertIn(
                (role, label),
                roles_and_labels,
                f"Missing contract in HTML fixture: role={role}, aria-label={label}",
            )

        srcdocs = [e["srcdoc"] for e in parser.elements if e.get("srcdoc")]
        self.assertTrue(len(srcdocs) > 0)
        self.assertIn("Unusual traffic", srcdocs[0])

    def _test_single_replica_workflow(self, url, step_action, expected_role, expected_name, expect_dict):
        fake_cdp = ReplicaFakeCdp(url)
        fake_vision = vision_adapter.FakeVisionEngine()
        driver = ChromeDriver(fake_cdp, "sid-test", url, vision=fake_vision)

        driver.reset()
        state1, screen_fp1 = driver.observe()

        # 1. Verify schema validity
        self.assertEqual(schema.validate_state(state1), [])

        # 2. Verify 16-hex RAM-only screen_fp
        self.assertIsNotNone(screen_fp1)
        self.assertEqual(len(screen_fp1), 16)
        self.assertRegex(screen_fp1, r"^[0-9a-f]{16}$")

        # 3. Verify reload stability on reset
        driver.reset()
        state1_reset, screen_fp1_reset = driver.observe()
        self.assertEqual(state1["ax_sha256"], state1_reset["ax_sha256"])
        self.assertEqual(state1["ax_stable_sha256"], state1_reset["ax_stable_sha256"])
        self.assertEqual(screen_fp1, screen_fp1_reset)

        # 4. Verify candidate actions
        actions = driver.candidate_actions(state1)
        self.assertIsInstance(actions, list)

        # Find target node id
        target_node = None
        for node in state1.get("nodes", []):
            if node.get("role") == expected_role and node.get("name") == expected_name:
                target_node = node
                break
        self.assertIsNotNone(target_node, f"Target node ({expected_role}, {expected_name}) not found")

        draft = {
            "id": f"skill_{expected_role}_{expected_name.lower().replace(' ', '_')}",
            "intent": f"Interact with {expected_name}",
            "params": {},
            "provenance": {"source": "e2e_harness"},
            "steps": [
                {
                    "path": "ax",
                    "op": step_action,
                    "target": target_node["id"],
                    "irreversible": False,
                    "text": "Hello" if step_action == "type" else None,
                    "value": "75" if step_action == "select" else None,
                }
            ],
            "checkpoints": [
                {
                    "after_step": 0,
                    "expect": expect_dict,
                }
            ],
        }
        # Clean null values
        draft["steps"][0] = {k: v for k, v in draft["steps"][0].items() if v is not None}

        # 5. Verify deterministic replay_and_admit
        admit_json = replay_and_admit(draft, driver, verified_by="jules-e2e-harness", runs=2)
        import json
        admitted = json.loads(admit_json)
        self.assertIn("evidence", admitted)
        self.assertTrue(admitted["evidence"]["reproduced"])
        self.assertEqual(admitted["evidence"]["runs"], 2)

    def test_replica_1_chatgpt_potencia_slider(self):
        self._test_single_replica_workflow(
            url="file:///app/jq1_a9_five_replicas_e2e_harness.html#chatgpt-potencia",
            step_action="click",
            expected_role="slider",
            expected_name="Potencia",
            expect_dict={"node_present": {"role": "slider", "name": "Potencia"}},
        )

    def test_replica_2_chatgpt_streaming_turn(self):
        self._test_single_replica_workflow(
            url="file:///app/jq1_a9_five_replicas_e2e_harness.html#chatgpt-streaming",
            step_action="click",
            expected_role="button",
            expected_name="Send",
            expect_dict={"node_present": {"role": "button", "name": "Stop"}},
        )

    def test_replica_3_perplexity_lpath_menu(self):
        self._test_single_replica_workflow(
            url="file:///app/jq1_a9_five_replicas_e2e_harness.html#perplexity-lpath",
            step_action="click",
            expected_role="button",
            expected_name="Model selector",
            expect_dict={"node_present": {"role": "menuitem", "name": "Pro search"}},
        )

    def test_replica_4_gemini_deep_research_panel(self):
        self._test_single_replica_workflow(
            url="file:///app/jq1_a9_five_replicas_e2e_harness.html#gemini-panel",
            step_action="click",
            expected_role="button",
            expected_name="Deep Research",
            expect_dict={"node_present": {"role": "region", "name": "Deep Research Panel"}},
        )

    def test_replica_5_google_cookie_modo_ia_wall(self):
        self._test_single_replica_workflow(
            url="file:///app/jq1_a9_five_replicas_e2e_harness.html#google-cookie",
            step_action="click",
            expected_role="button",
            expected_name="Accept all",
            expect_dict={"node_present": {"role": "switch", "name": "Modo IA"}},
        )

    def test_negative_state_predicate_assertion(self):
        """Verify negative state assertions that would fool an overly permissive predicate."""
        fake_cdp = ReplicaFakeCdp("file:///app/jq1_a9_five_replicas_e2e_harness.html#chatgpt-potencia")
        driver = ChromeDriver(fake_cdp, "sid-neg", "file:///app/jq1_a9_five_replicas_e2e_harness.html#chatgpt-potencia")
        driver.reset()
        state, _ = driver.observe()

        # 1. _eval_expect returns False when node_present expects a non-existent role
        bad_expect = {"node_present": {"role": "nonexistent_role", "name": "Potencia"}}
        self.assertFalse(_eval_expect(bad_expect, state))

        # 2. _eval_expect returns False when node_absent contains a present role
        bad_absent = {"node_absent": {"role": "slider", "name": "Potencia"}}
        self.assertFalse(_eval_expect(bad_absent, state))

        # 3. schema.validate_state detects float in integer field
        invalid_state = dict(state)
        invalid_state["raw_count"] = 12.5
        errs = schema.validate_state(invalid_state)
        self.assertTrue(len(errs) > 0)
        self.assertIn("raw_count", errs[0])


class TestWP1PipelineExpectedFailure(unittest.TestCase):
    """End-to-end WP1 pipeline expectedFailure test chaining WP1-02..WP1-05."""

    def test_wp1_02_to_05_chained_pipeline(self):
        """Chaining wait_done -> press/insert_text -> text.extract -> stopstate.detect.

        WP1-02: wait_done API in wender.perceive.wait
        WP1-03: press and insert_text in cdp_executor.OPS
        WP1-04: text.extract API in wender.perceive.text
        WP1-05: stopstate.detect API in wender.perceive.stopstate
        """
        # 1. WP1-02: perceive.wait.wait_done
        try:
            from wender.perceive import wait
            has_wait_done = hasattr(wait, "wait_done")
        except ImportError:
            has_wait_done = False
        self.assertTrue(has_wait_done, "WP1-02: wender.perceive.wait.wait_done is absent in current corpus")

        # 2. WP1-03: keyboard ops in cdp_executor
        self.assertIn("press", cdp_executor.OPS, "WP1-03: 'press' missing in cdp_executor.OPS")
        self.assertIn("insert_text", cdp_executor.OPS, "WP1-03: 'insert_text' missing in cdp_executor.OPS")

        # 3. WP1-04: perceive.text.extract
        try:
            from wender.perceive import text
            has_extract = hasattr(text, "extract")
        except ImportError:
            has_extract = False
        self.assertTrue(has_extract, "WP1-04: wender.perceive.text.extract is absent in current corpus")

        # 4. WP1-05: perceive.stopstate.detect
        try:
            from wender.perceive import stopstate
            has_detect = hasattr(stopstate, "detect")
        except ImportError:
            has_detect = False
        self.assertTrue(has_detect, "WP1-05: wender.perceive.stopstate.detect is absent in current corpus")


if __name__ == "__main__":
    unittest.main()
