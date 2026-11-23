import ast
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from jsonschema import ValidationError


SURFACE = Path(__file__).resolve().parents[1]
VP_DIRECT = SURFACE.parents[1]
sys.path[:0] = [str(VP_DIRECT), str(VP_DIRECT.parent / "wender-agent")]

from channel import cdp
from schemas.validation import validate_contract


EXAMPLES = VP_DIRECT / "schemas" / "tests" / "examples"


def example(name):
    return json.loads((EXAMPLES / f"{name}_v1.valid.json").read_text(encoding="utf-8"))


def raw_node(node_id, role, name, backend_id=None, children=()):
    node = {
        "nodeId": node_id, "role": {"value": role}, "name": {"value": name},
        "childIds": list(children),
    }
    if backend_id is not None:
        node["backendDOMNodeId"] = backend_id
    return node


class FakePort:
    def __init__(self):
        self.calls = []
        self.revision = 0
        self.loader = "document-one"
        self.url = "http://127.0.0.1/"
        self.target = "tab-fixture"
        self.node = 17
        self.clicked = False
        self.effect = True
        self.duplicate = False
        self.timeout_after_send = False
        self.closed = False
        self.before_resolve = lambda: None
        self.after_click = lambda: None

    def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, params, session_id))
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"type": "page", "targetId": self.target}}
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "page", "targetId": "tab-decoy", "title": "Fixture", "url": "http://127.0.0.1/"},
                {"type": "page", "targetId": self.target, "title": "Fixture", "url": "http://127.0.0.1/"},
            ]}
        if method == "Target.attachToTarget":
            return {"sessionId": "cdp-owned-session"}
        if method == "Target.createTarget":
            return {"targetId": "new-tab"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {
                "id": "root", "loaderId": self.loader, "url": self.url,
            }}}
        if method == "Accessibility.getFullAXTree":
            nodes = [raw_node("button", "button", "Send", self.node)]
            if self.duplicate:
                nodes.append(raw_node("button-two", "button", "Send", self.node + 1))
            if self.clicked:
                nodes.append(raw_node("status", "status", "Fixture response"))
            return {"nodes": [
                raw_node("root", "RootWebArea", "Fixture", children=[n["nodeId"] for n in nodes]),
                *nodes,
            ]}
        if method == "DOM.resolveNode":
            self.before_resolve()
            return {"object": {"objectId": "document-bound-object"}}
        if method == "Runtime.callFunctionOn":
            self.clicked = self.effect
            self.after_click()
            if self.timeout_after_send:
                raise cdp.CdpError("simulated timeout after send")
            return {"result": {"value": True}}
        if method in {"Page.enable", "Accessibility.enable", "Target.detachFromTarget"}:
            return {}
        raise AssertionError(f"Unexpected command {method}")

    def document_revision(self, session_id):
        return self.revision

    def close(self):
        self.closed = True


def reply_visible(nodes):
    return any(node["name"] == "Fixture response" for node in nodes)


def mutant(old, new):
    path = SURFACE / "__init__.py"
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise AssertionError(f"Expected a unique mutation site for {old!r}")
    module = ModuleType("_u1b_mutant")
    with patch.dict(sys.modules, {module.__name__: module}):
        exec(compile(source.replace(old, new), str(path), "exec"), vars(module))
    return module


class CdpTests(unittest.TestCase):
    def setUp(self):
        self.session = example("vp.session")
        self.request = dict(example("vp.channel.req"), dry_run=False, require_vision_witness=False)
        self.port = FakePort()
        self.connect = Mock(return_value=self.port)

    def backend(self, module=cdp, **kwargs):
        return module.CdpBackend(
            self.session["session_id"], lambda _session_id: self.session,
            connect=self.connect, effect_guard=lambda _: nullcontext(), **kwargs,
        )

    def request_for(self, op, **kwargs):
        return dict(self.request, op=op, **kwargs)

    def prepared(self, backend, **act_fields):
        backend.attach_target(self.request_for("attach"))
        observation = backend.observe(self.request_for("observe"))
        node = next(node for node in observation.nodes if node["role"] == "button")
        request = self.request_for("act", action="click")
        request["observation_id"] = observation.id
        request["target"]["ax_stable_sha256"] = observation.ax_stable_sha256
        return observation, dict(request, **act_fields), node["id"]

    def assert_exact_attachment(self, backend):
        backend.attach_target(self.request_for("attach", url="http://127.0.0.1/"))
        self.connect.assert_called_once_with(self.session["cdp_port"])
        self.assertEqual(self.port.calls, [
            ("Target.getTargetInfo", {"targetId": "tab-fixture"}, None),
            ("Target.attachToTarget", {"targetId": "tab-fixture", "flatten": True}, None),
            ("Page.enable", None, "cdp-owned-session"),
            ("Accessibility.enable", None, "cdp-owned-session"),
        ])

    def test_exact_registry_port_and_target_without_navigation(self):
        self.assert_exact_attachment(self.backend())

    def test_every_act_reads_pruned_ax_again_and_asserts_effect(self):
        backend = self.backend()
        _, request, node_id = self.prepared(backend)
        offset = len(self.port.calls)
        reply = backend.act(request, node_id=node_id, verify=reply_visible)
        self.assertTrue(reply["ok"])
        validate_contract("vp.channel.reply/v1", reply)
        methods = [method for method, _, _ in self.port.calls[offset:]]
        self.assertLess(methods.index("Accessibility.getFullAXTree"), methods.index("Runtime.callFunctionOn"))
        self.assertIn("Accessibility.getFullAXTree", methods[methods.index("Runtime.callFunctionOn") + 1:])
        self.assertTrue(backend.assert_observation(self.request_for("assert"), verify=reply_visible)["ok"])

    def test_expected_navigation_uses_new_ax_and_retires_old_observation(self):
        backend = self.backend()
        _, request, node_id = self.prepared(backend)
        destination = "http://127.0.0.1/chat.html"

        def navigate():
            self.port.url = destination
            self.port.loader = "document-two"
            self.port.revision += 1

        self.port.after_click = navigate
        reply = backend.act(
            request, node_id=node_id, verify=reply_visible, expected_document_url=destination,
        )
        self.assertTrue(reply["ok"])
        with self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
            backend.validate_observation(request)
        assertion = backend.assert_observation(
            self.request_for("assert"), verify=reply_visible, expected_document_url=destination,
        )
        self.assertTrue(assertion["ok"])

    def test_background_create_only(self):
        backend = self.backend()
        self.assertEqual(backend.create_target(self.request_for("attach")), "new-tab")
        self.assertEqual(self.port.calls, [
            ("Target.createTarget", {"url": "about:blank", "background": True}, None),
        ])
        self.assertTrue(self.port.closed)

    def test_non_loopback_and_browser_close_refused(self):
        with self.assertRaises(cdp.Refused):
            self.backend().attach_target(self.request_for("attach"), host="0.0.0.0")
        self.connect.assert_not_called()
        with self.assertRaises(cdp.Refused):
            cdp._BrowserPort(self.session["cdp_port"]).send("Browser.close")

    def test_snap_firefox_and_disabled_cdp_refused_before_connect(self):
        with self.assertRaisesRegex(cdp.Refused, "^SNAP_CDP_UNAVAILABLE$"):
            self.backend(executable="/snap/chromium/current/chrome").attach_target(self.request_for("attach"))
        self.session["browser"] = "firefox"
        with self.assertRaisesRegex(cdp.Refused, "^BIDI_DEFERRED$"):
            self.backend().attach_target(self.request_for("attach"))
        self.session.update(browser="google-chrome", cdp_allowed=False)
        with self.assertRaisesRegex(cdp.Refused, "^NO_LEASE$"):
            self.backend().attach_target(self.request_for("attach"))
        self.connect.assert_not_called()

    def test_target_hint_and_peer_target_mismatch_refused(self):
        backend = self.backend()
        request = self.request_for("attach")
        request["target"] = {"cdp_target_id": "tab-decoy"}
        with self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
            backend.attach_target(request)
        self.connect.assert_not_called()
        self.port.target = "tab-decoy"
        with self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
            backend.attach_target(self.request_for("attach"))
        self.assertTrue(self.port.closed)

    def test_document_loader_or_navigation_events_invalidate_observation(self):
        for change in ("loader", "revision"):
            with self.subTest(change=change):
                backend = self.backend()
                _, request, node_id = self.prepared(backend)
                setattr(self.port, change, "document-two" if change == "loader" else 1)
                with self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
                    backend.act(request, node_id=node_id, verify=reply_visible)
                self.assertNotIn("Runtime.callFunctionOn", [call[0] for call in self.port.calls])
                backend.detach()

    def test_changed_registry_target_detaches_and_retires_observation(self):
        backend = self.backend()
        _, request, _ = self.prepared(backend)
        self.session["cdp_target_id"] = "other-target"
        with self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
            backend.validate_observation(request)
        self.assertTrue(self.port.closed)

    def test_replaced_backend_node_and_duplicate_names_refused(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                self.port.duplicate = duplicate
                backend = self.backend()
                _, request, node_id = self.prepared(backend)
                if not duplicate:
                    self.port.node += 10
                with self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
                    backend.act(request, node_id=node_id, verify=reply_visible)
                self.assertNotIn("Runtime.callFunctionOn", [call[0] for call in self.port.calls])
                backend.detach()

    def test_document_changes_after_node_resolution(self):
        backend = self.backend()
        _, request, node_id = self.prepared(backend)
        self.port.before_resolve = lambda: setattr(self.port, "revision", 1)
        with self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
            backend.act(request, node_id=node_id, verify=reply_visible)
        self.assertNotIn("Runtime.callFunctionOn", [call[0] for call in self.port.calls])

    def assert_human_take_detaches(self, backend):
        backend.attach_target(self.request_for("attach"))
        self.session["state"] = "HUMAN"
        self.session["lease"]["holder"] = None
        backend.sync_authority()
        self.assertEqual(self.port.calls[-1], (
            "Target.detachFromTarget", {"sessionId": "cdp-owned-session"}, None,
        ))
        self.assertTrue(self.port.closed)
        with self.assertRaisesRegex(cdp.Refused, "^NO_LEASE$"):
            backend.observe(self.request_for("observe"))

    def test_human_take_detaches(self):
        self.assert_human_take_detaches(self.backend())

    def test_frozen_schema_rejects_title_key(self):
        request = self.request_for("attach")
        request["target"] = {"title": "Fixture"}
        with self.assertRaises(ValidationError):
            self.backend().attach_target(request)
        self.connect.assert_not_called()

    def test_transport_ack_without_effect_and_timeout_remain_uncertain(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.port = FakePort()
                self.connect.return_value = self.port
                backend = self.backend()
                _, request, node_id = self.prepared(backend)
                self.port.effect = False
                self.port.timeout_after_send = timeout
                with patch.object(cdp.time, "monotonic", side_effect=[0, 2]):
                    reply = backend.act(request, node_id=node_id, verify=reply_visible)
                self.assertEqual(reply["code"], "UNCERTAIN_OUTCOME")
                methods = [call[0] for call in self.port.calls]
                self.assertEqual(methods.count("Runtime.callFunctionOn"), 1)
                self.assertIn("Accessibility.getFullAXTree", methods[methods.index("Runtime.callFunctionOn") + 1:])
                backend.detach()

    def test_dry_run_and_missing_effect_guard_do_not_write(self):
        backend = cdp.CdpBackend(self.session["session_id"], lambda _: self.session, connect=self.connect)
        _, request, node_id = self.prepared(backend)
        with self.assertRaisesRegex(cdp.Refused, "^NO_LEASE$"):
            backend.act(request, node_id=node_id, verify=reply_visible)
        self.assertTrue(backend.act(dict(request, dry_run=True), node_id=node_id, verify=reply_visible)["ok"])
        self.assertNotIn("Runtime.callFunctionOn", [call[0] for call in self.port.calls])

    def test_mutant_attachment_by_title_or_url(self):
        for field in ("title", "url"):
            with self.subTest(field=field):
                broken = mutant(
                    '"Target.attachToTarget", {"targetId": target_id, "flatten": True},',
                    '"Target.attachToTarget", {"targetId": next('
                    'item["targetId"] for item in self._port.send("Target.getTargets")["targetInfos"] '
                    f'if item["{field}"] == ' + ('"Fixture"' if field == "title" else 'request["url"]')
                    + '), "flatten": True},',
                )
                self.port.calls.clear()
                self.connect.reset_mock()
                with self.assertRaises(AssertionError):
                    self.assert_exact_attachment(self.backend(broken))
                self.assertIn(("Target.attachToTarget", {"targetId": "tab-decoy", "flatten": True}, None),
                              self.port.calls)

    def test_mutant_stay_attached_during_human_mode(self):
        broken = mutant("                self._live_binding()\n            except Refused:",
                        "                return\n            except Refused:")
        with self.assertRaises(AssertionError):
            self.assert_human_take_detaches(self.backend(broken))
        self.assertFalse(self.port.closed)

    def test_live_fixture_script_is_marked_without_running_it(self):
        script = Path(__file__).with_name("LIVE_s2_fixture_click.py")
        self.assertTrue(ast.get_docstring(ast.parse(script.read_text(encoding="utf-8"))).startswith("LIVE:"))


class TransportTests(unittest.TestCase):
    def assert_loopback_discovery(self, module, debugger_url=None):
        port = 49152
        url = debugger_url or f"ws://127.0.0.1:{port}/devtools/browser/test-id"
        reply = Mock(status=200, **{"read.return_value": json.dumps({"webSocketDebuggerUrl": url}).encode()})
        client = Mock(**{"getresponse.return_value": reply})
        stream = Mock()
        ws = Mock(**{"getstatus.return_value": 101})
        with (patch.object(module, "HTTPConnection", return_value=client) as http,
              patch.object(module.socket, "create_connection", return_value=stream) as connect,
              patch.object(module, "Thread"),
              patch("websocket.create_connection", return_value=ws) as open_ws,
              patch("builtins.open") as file_open,
              patch("subprocess.Popen") as process):
            try:
                module._BrowserPort(port).start()
            except module.Refused:
                connect.assert_not_called()
                open_ws.assert_not_called()
                raise
            http.assert_called_once_with("127.0.0.1", port, timeout=5)
            client.request.assert_called_once_with("GET", "/json/version")
            connect.assert_called_once_with(("127.0.0.1", port), timeout=5)
            open_ws.assert_called_once_with(
                url, socket=stream, timeout=5, suppress_origin=True, redirect_limit=0,
            )
            file_open.assert_not_called()
            process.assert_not_called()

    def test_strict_loopback_discovery_without_profiles_or_subprocesses(self):
        self.assert_loopback_discovery(cdp)

    def test_websocket_host_port_and_target_path_are_pinned(self):
        for endpoint in (
            "ws://192.0.2.10:49152/devtools/browser/id",
            "ws://127.0.0.1:49153/devtools/browser/id",
            "ws://127.0.0.1:49152/devtools/page/id",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(cdp.Refused, "^identity_mismatch$"):
                self.assert_loopback_discovery(cdp, endpoint)

    def test_page_events_invalidate_only_their_attachment(self):
        port = cdp._BrowserPort(49152)
        port._session_incarnations.update(target=1, other=1)
        port._store_event_locked({"method": "Page.frameNavigated", "sessionId": "target"})
        port._store_event_locked({"method": "Page.navigatedWithinDocument", "sessionId": "target"})
        port._store_event_locked({"method": "Page.frameNavigated", "sessionId": "unregistered"})
        self.assertEqual(port.document_revision("target"), 2)
        self.assertEqual(port.document_revision("other"), 0)
        self.assertNotIn("unregistered", port._document_revisions)

    def test_mutants_discovery_and_prohibited_fallbacks(self):
        marker = '            http.request("GET", "/json/version")'
        for name, broken_line in {
            "wildcard bind": '            http = HTTPConnection("0.0.0.0", self.port, timeout=self.timeout)\n' + marker,
            "PUT /json/new": '            http.request("PUT", "/json/new")',
            "DevToolsActivePort": '            open("DevToolsActivePort").close()\n' + marker,
            "chromedriver": '            __import__("subprocess").Popen(["chromedriver"])\n' + marker,
        }.items():
            with self.subTest(name=name):
                broken = mutant(marker, broken_line)
                with self.assertRaises(AssertionError):
                    self.assert_loopback_discovery(broken)
