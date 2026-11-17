"""U1c fixture oracles, refusal checks and deliberately broken executor variants."""

from dataclasses import replace
import json
import os
from pathlib import Path
from select import select
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent
DIRECT = HERE.parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "wender-agent")]

from channel.atspi import AtspiExecutor, HostIdentity, ObjectRef, PrivateIdentity, Refused, SessionBus
from schemas.validation import validate_contract


APP = ObjectRef(":1.10", "/app")
WINDOW = ObjectRef(APP.bus, "/window")
BUTTON = ObjectRef(APP.bus, "/button")
BUTTON_REPLACEMENT = ObjectRef(APP.bus, "/button_new")
ENABLED = (1 << 8) | (1 << 23)


class FakeBus:
    def __init__(self):
        self.owners = {APP.bus: 24}
        self.apps = [APP]
        self.children = {APP: [WINDOW], WINDOW: [BUTTON], BUTTON: []}
        self.roles = {WINDOW: "frame", BUTTON: "check box"}
        self.names = {WINDOW: "Synthetic window", BUTTON: "Fixture toggle"}
        self.states = {WINDOW: ENABLED, BUTTON: ENABLED}
        self.trace = []
        self.send_error = False
        self.post_error = False
        self.returned = True
        self.closed = False

    def call(self, bus, path, method, sig, args, reply, deadline):
        name = method.rsplit(".", 1)[1]
        if name == "GetConnectionUnixProcessID":
            return self.owners[args[0]]
        if bus == "org.a11y.atspi.Registry":
            return [(app.bus, app.path) for app in self.apps]
        ref = ObjectRef(bus, path)
        if name == "GetChildren":
            return [(child.bus, child.path) for child in self.children[ref]]
        if name == "GetState":
            self.trace.append((name, ref))
            if self.post_error and self.states[BUTTON] & (1 << 4):
                raise Refused("ATSPI_CALL_FAILED")
            return [self.states[ref], 0]
        if name == "GetRoleName":
            return self.roles[ref]
        if name == "Get":
            return 2 if args[-1] == "CaretOffset" else self.names[ref]
        if name == "GetInterfaces":
            return ["org.a11y.atspi." + iface for iface in ("Action", "Component", "EditableText", "Selection")]
        if name in {"DoAction", "GrabFocus", "InsertText", "SelectChild"}:
            self.trace.append((name, ref, sig, args))
            self.states[BUTTON] |= 1 << 4
            if self.send_error:
                raise Refused("ATSPI_CALL_FAILED")
            return self.returned
        raise AssertionError(method)

    def close(self):
        self.closed = True


def request(session, op="observe", observation_id=None, action="click", **fields):
    value = {"schema": "vp.channel.req/v1", "request_id": "request-1", "backend": "atspi",
             "op": op, "session_id": session["session_id"], "capsule_id": session["capsule_id"],
             "incarnation": session["incarnation"], "authority_epoch": session["lease"]["authority_epoch"],
             "dry_run": False, "risk_class": "preparar", "require_vision_witness": False}
    if op == "act":
        value.update(observation_id=observation_id, action=action, target={"window_id": "0x18"})
    return value | fields


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.session = json.loads((DIRECT / "schemas/tests/examples/vp.session_v1.valid.json").read_text())
        self.session.update(display="xpra:42", state="AGENT", backend="atspi",
                            window={"xid": "0x18", "pid": 24, "witness": "synthetic-window"},
                            lease={"holder": "agent-fixture", "authority_epoch": 1})
        self.identity = PrivateIdentity("xpra:42", 24, "0x18", "synthetic-window",
                                        self.session["incarnation"], 1, WINDOW, True,
                                        frozenset({"click", "focus", "insert_text", "select"}))
        self.receiver = self.executor()

    def executor(self, cls=AtspiExecutor, **kwargs):
        return cls(lambda _session_id: self.identity, session_env={}, transport=self.bus, **kwargs)

    def host(self):
        self.session.update(browser="google-chrome", display=":0")
        self.identity = HostIdentity(":0", 24, "0x18", "synthetic-window", self.session["incarnation"],
                                     1, WINDOW, True, hidden=True, qualified_tab=BUTTON)
        self.bus.roles[BUTTON] = "page tab"
        self.bus.names[BUTTON] = "Google Gemini"

    def observe(self, receiver=None):
        reply, snapshot = (receiver or self.receiver).observe(request(self.session), session=self.session)
        self.assertEqual(reply["code"], "OK")
        validate_contract("vp.channel.reply/v1", reply)
        return snapshot

    def act(self, snapshot, receiver=None, **fields):
        value = request(self.session, "act", snapshot["observation_id"], **fields)
        return (receiver or self.receiver).act(value, session=self.session, node_id=snapshot["nodes"][1]["id"])

    def require_refusal(self, result, expected):
        reply, evidence = result
        self.assertEqual(reply["code"], expected)
        self.assertIsNone(evidence)
        self.assertFalse(any(call[0] in {"DoAction", "GrabFocus", "InsertText", "SelectChild"}
                             for call in self.bus.trace))

    def require_reread(self, receiver):
        before = self.observe(receiver)
        reply, after = self.act(before, receiver)
        send = self.bus.trace.index(("DoAction", BUTTON, "(i)", (0,)))
        self.assertIn(("GetState", BUTTON), self.bus.trace[send + 1:])
        self.assertEqual(reply["code"], "OK")
        self.assertNotEqual(before["observation_id"], after["observation_id"])
        self.assertNotEqual(before["ax_sha256"], after["ax_sha256"])
        self.assertIn(("checked", "true"), after["nodes"][1]["attrs"])
        for snapshot in (before, after):
            self.assertEqual(snapshot["atspi_capture"], "polling_only")

    def test_action_rereads_and_marks_both_snapshots_polling_only(self):
        with patch("subprocess.run") as native:
            self.require_reread(self.receiver)
            native.assert_not_called()

    def test_host_only_qualified_chrome_tab_click(self):
        self.host()
        after = self.act(self.observe())[1]
        self.assertIsNotNone(after)
        self.assertIn(("DoAction", BUTTON, "(i)", (0,)), self.bus.trace)

    def test_focus_insert_and_selection_use_closed_atspi_methods(self):
        self.bus.roles[BUTTON] = "entry"
        for action, fields, effect in (
            ("focus", {}, ("GrabFocus", BUTTON, "", ())),
            ("insert_text", {"input_text": "é"}, ("InsertText", BUTTON, "(isi)", (2, "é", 2))),
            ("select", {}, ("SelectChild", WINDOW, "(i)", (0,))),
        ):
            with self.subTest(action=action):
                reply, snapshot = self.act(self.observe(), action=action, **fields)
                self.assertEqual(reply["code"], "OK")
                self.assertIn(effect, self.bus.trace)
                self.assertNotIn("é", json.dumps(snapshot, ensure_ascii=False))

    def test_ambiguous_pid_is_refused(self):
        other = ObjectRef(":1.11", "/app")
        self.bus.apps.append(other)
        self.bus.owners[other.bus] = 24
        self.require_refusal(self.receiver.observe(request(self.session), session=self.session), "ATSPI_AMBIGUOUS_PID")

    def test_defunct_removed_and_replaced_objects_are_refused(self):
        for mode in ("defunct", "removed", "replaced"):
            with self.subTest(mode=mode):
                self.bus.children[WINDOW] = [BUTTON]
                self.bus.states[BUTTON] = ENABLED
                before = self.observe()
                if mode == "defunct":
                    self.bus.states[BUTTON] |= 1 << 6
                elif mode == "removed":
                    self.bus.children[WINDOW] = []
                else:
                    self.bus.children[WINDOW] = [BUTTON_REPLACEMENT]
                    self.bus.children[BUTTON_REPLACEMENT] = []
                    self.bus.roles[BUTTON_REPLACEMENT] = self.bus.roles[BUTTON]
                    self.bus.names[BUTTON_REPLACEMENT] = self.bus.names[BUTTON]
                    self.bus.states[BUTTON_REPLACEMENT] = ENABLED
                self.require_refusal(self.act(before), "ATSPI_RETIRED_OBJECT")
                self.require_refusal(self.act(before), "ATSPI_STALE_OBSERVATION")

    def test_truncated_tree_is_refused_on_observe_and_act(self):
        receiver = self.executor(max_nodes=2, max_depth=1)
        before = self.observe(receiver)
        self.bus.children[BUTTON] = [BUTTON_REPLACEMENT]
        self.require_refusal(self.act(before, receiver), "ATSPI_TREE_TRUNCATED")
        self.require_refusal(receiver.observe(request(self.session), session=self.session), "ATSPI_TREE_TRUNCATED")

    def test_unqualified_host_actions_and_tab_substitution_are_refused(self):
        self.host()
        receiver = self.receiver
        with patch("subprocess.run") as native:
            self.require_refusal(self.act(self.observe(receiver), receiver, action="focus"), "ATSPI_ACTION_UNQUALIFIED")
            self.identity = replace(self.identity, qualified_tab=None)
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
            native.assert_not_called()

    def test_stale_identity_and_human_lease_are_refused(self):
        before = self.observe()
        self.identity = replace(self.identity, witness="replacement")
        self.require_refusal(self.act(before), "identity_mismatch")
        self.session["state"] = "HUMAN"
        self.session["lease"]["holder"] = None
        self.require_refusal(self.act(before), "NO_LEASE")

    def test_private_display_cannot_alias_display_zero(self):
        self.session["display"] = "xpra:00"
        self.identity = replace(self.identity, display="xpra:00")
        self.require_refusal(self.receiver.observe(request(self.session), session=self.session), "DISPLAY_UNSUPPORTED")

    def test_timeout_does_not_resend_and_still_polls(self):
        before = self.observe()
        self.bus.send_error = True
        reply, evidence = self.act(before)
        self.assertEqual(reply["code"], "UNCERTAIN_OUTCOME")
        self.assertEqual(evidence["atspi_capture"], "polling_only")
        self.assertEqual(self.act(before)[0]["code"], "ATSPI_STALE_OBSERVATION")
        self.assertEqual(self.bus.trace.count(("DoAction", BUTTON, "(i)", (0,))), 1)
        self.assertEqual(self.bus.trace[-1], ("GetState", BUTTON))

    def test_backend_false_still_polls_and_missing_post_state_is_uncertain(self):
        self.bus.returned = False
        reply, evidence = self.act(self.observe())
        self.assertEqual(reply["code"], "UNCERTAIN_OUTCOME")
        self.assertIsNotNone(evidence)
        self.bus.states[BUTTON] = ENABLED
        self.bus.returned = True
        self.bus.post_error = True
        reply, evidence = self.act(self.observe())
        self.assertEqual(reply["code"], "UNCERTAIN_OUTCOME")
        self.assertIsNone(evidence)

    def test_fake_dry_run_has_no_effect_and_detach_retires_observation(self):
        before = self.observe()
        self.assertEqual(self.act(before, dry_run=True), ({"schema": "vp.channel.reply/v1", "ok": True, "code": "OK"}, None))
        self.receiver.detach(self.session["session_id"])
        self.require_refusal(self.act(before), "ATSPI_STALE_OBSERVATION")
        self.receiver.close()
        self.assertTrue(self.bus.closed)

    def test_mutant_no_post_action_reread_is_killed(self):
        class NoReadback(AtspiExecutor):
            def _capture(self, *args):
                if any(call[0] == "DoAction" for call in self._transport.trace):
                    return self.before
                self.before = super()._capture(*args)
                return self.before

        with self.assertRaises(AssertionError):
            self.require_reread(self.executor(NoReadback))

    def test_failed_receipt_blocks_host_but_keeps_private_action_and_kills_mutant(self):
        self.host()
        receipt = json.loads((DIRECT / "config/wl0.json").read_text())
        receipt["steps"]["L0-8"]["status"] = "BLOCKED"

        class IgnoreReceipt(AtspiExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._host_qualified = True

        with TemporaryDirectory(dir=HERE) as temp:
            receipt_path = Path(temp) / "blocked.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            receiver = self.executor(receipt_path=receipt_path)
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
            mutant = self.executor(IgnoreReceipt, receipt_path=receipt_path)
            with self.assertRaises(AssertionError):
                self.require_refusal(self.act(self.observe(mutant), mutant), "ATSPI_ACTION_UNQUALIFIED")
            self.setUp()
            receiver = self.executor(receipt_path=receipt_path)
            self.assertEqual(self.act(self.observe(receiver), receiver)[0]["code"], "OK")

    def test_mutant_native_fallback_is_killed(self):
        class NativeFallback(AtspiExecutor):
            def act(self, *args, **kwargs):
                response = super().act(*args, **kwargs)
                if response[0]["code"] == "ATSPI_ACTION_UNQUALIFIED":
                    subprocess.run(["xdotool", "click", "1"], check=True)
                return response

        def without_native(receiver):
            with patch("subprocess.run") as native:
                self.require_refusal(self.act(self.observe(receiver), receiver, action="focus"),
                                     "ATSPI_ACTION_UNQUALIFIED")
                native.assert_not_called()

        self.host()
        without_native(self.receiver)
        with self.assertRaises(AssertionError):
            without_native(self.executor(NativeFallback))


class TransportTests(unittest.TestCase):
    def test_session_bus_discovery_is_once_and_accessibility_bus_is_retained(self):
        transport = SessionBus({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/session"})
        session, accessibility = Mock(), Mock()
        accessibility.is_closed.return_value = False
        endpoint = ("org.freedesktop.DBus", "/org/freedesktop/DBus")
        with (patch.object(transport, "_connect", side_effect=[session, accessibility]) as connect,
              patch.object(transport, "_call", side_effect=["unix:path=/synthetic/atspi", "id", "id"]) as call):
            deadline = monotonic() + 2
            for _ in range(2):
                self.assertEqual(transport.call(*endpoint, "org.freedesktop.DBus.GetId", "", (), "(s)", deadline), "id")
            self.assertEqual([args.args for args in connect.call_args_list],
                             [("unix:path=/synthetic/session", deadline), ("unix:path=/synthetic/atspi", deadline)])
            self.assertEqual([args.args[0] for args in call.call_args_list], [session, accessibility, accessibility])
            self.assertEqual(call.call_args_list[0].args[3], "org.a11y.Bus.GetAddress")
        session.close_sync.assert_called_once_with(None)
        transport.close()
        accessibility.close_sync.assert_called_once_with(None)

    def test_tenant_ax_socket_and_nonlocal_addresses_refused_before_connect(self):
        for address in ("unix:path=/run/newton-tenant/ax.sock", "unix:path=/run/newton-tenant/%61x.sock",
                        "unix:path=/run/newton-tenant/../newton-tenant/ax.sock",
                        "tcp:host=127.0.0.1,port=3", "unix:path=/tmp/private;autolaunch:"):
            with self.subTest(address=address):
                bus = SessionBus({"AT_SPI_BUS_ADDRESS": address})
                with self.assertRaisesRegex(Refused, "ATSPI_BUS_UNAVAILABLE"):
                    bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.GetId",
                             "", (), "(s)", monotonic() + 2)

    def test_persistent_connection_uses_the_provisioned_bus(self):
        with TemporaryDirectory(prefix="dbus-", dir=HERE) as temp:
            # The real Unix socket path is shorter through /proc, which avoids the sockaddr limit.
            directory = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, directory)
            address = f"unix:path=/proc/{os.getpid()}/fd/{directory}/bus"
            with subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1",
                                   "--address=" + address], stdout=subprocess.PIPE, text=True) as daemon:
                try:
                    self.assertTrue(select([daemon.stdout], [], [], 5)[0])
                    self.assertIn(address, daemon.stdout.readline())
                    transport = SessionBus({"AT_SPI_BUS_ADDRESS": address})
                    with patch.object(transport, "_connect", wraps=transport._connect) as connect:
                        endpoint = ("org.freedesktop.DBus", "/org/freedesktop/DBus")
                        method = "org.freedesktop.DBus.GetId"
                        first = transport.call(*endpoint, method, "", (), "(s)", monotonic() + 2)
                        second = transport.call(*endpoint, method, "", (), "(s)", monotonic() + 2)
                        self.assertEqual(first, second)
                        self.assertRegex(first, r"^[0-9a-f]{32}$")
                        pid = transport.call(*endpoint, "org.freedesktop.DBus.GetConnectionUnixProcessID",
                                             "(s)", (transport._connection.get_unique_name(),), "(u)", monotonic() + 2)
                        self.assertEqual(pid, os.getpid())
                        connect.assert_called_once()
                        transport.close()
                        with self.assertRaisesRegex(Refused, "ATSPI_BUS_UNAVAILABLE"):
                            transport.call(*endpoint, method, "", (), "(s)", monotonic() + 2)
                finally:
                    daemon.terminate()
                    daemon.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
