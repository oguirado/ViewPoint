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
from channel.atspi import _STATE_TYPE
from channel.core.tests.wl0_fixtures import receipt_cases
from focus import CoverageLost, FocusMonitor, Readback
from schemas.validation import validate_contract


APP = ObjectRef(":1.10", "/app")
WINDOW = ObjectRef(APP.bus, "/window")
BUTTON = ObjectRef(APP.bus, "/button")
BUTTON_REPLACEMENT = ObjectRef(APP.bus, "/button_new")
CHECKED_BIT = 1 << _STATE_TYPE["CHECKED"]
DEFUNCT_BIT = 1 << _STATE_TYPE["DEFUNCT"]
ENABLED_BIT = 1 << _STATE_TYPE["ENABLED"]
SELECTED_BIT = 1 << _STATE_TYPE["SELECTED"]
SENSITIVE_BIT = 1 << _STATE_TYPE["SENSITIVE"]
ENABLED = ENABLED_BIT | SENSITIVE_BIT


class StateTypeTests(unittest.TestCase):
    def test_state_type_table_matches_installed_typelib(self):
        try:
            import gi
        except ModuleNotFoundError as error:
            if error.name != "gi":
                raise
            self.skipTest("PyGObject (gi) is unavailable")
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        self.assertEqual(_STATE_TYPE, {name: int(getattr(Atspi.StateType, name)) for name in _STATE_TYPE})


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
            if self.post_error and self.states[BUTTON] & CHECKED_BIT:
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
            self.states[BUTTON] |= CHECKED_BIT
            if self.send_error:
                raise Refused("ATSPI_CALL_FAILED")
            return self.returned
        raise AssertionError(method)

    def close(self):
        self.closed = True


class FocusSource:
    def __init__(self, bus, *, events=(), continuous=True, unavailable=False, on_arm=None):
        self.bus = bus
        self.readback = Readback(events, continuous)
        self.unavailable = unavailable
        self.on_arm = on_arm

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def arm(self):
        self.bus.trace.append(("focus-arm",))
        if self.unavailable:
            raise CoverageLost("fixture has no event subscription")
        if self.on_arm is not None:
            self.on_arm()
        return "0x20"

    def finish(self):
        self.bus.trace.append(("focus-finish",))
        return self.readback


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

    def focus_provider(self, **kwargs):
        source = FocusSource(self.bus, **kwargs)
        monitor = FocusMonitor(source, clock=iter((100, 200)).__next__)
        return lambda identity: ("surface-atspi-fixture", monitor) if identity == self.identity else None

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

    def test_action_requires_enabled_and_sensitive_at_preflight_and_dispatch(self):
        for state in (ENABLED_BIT | SELECTED_BIT, SENSITIVE_BIT):
            with self.subTest(state=state, stage="preflight"), patch.dict(self.bus.states, {BUTTON: state}):
                focus_provider = Mock()
                receiver = self.executor(focus_monitor_for=focus_provider)
                self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
                focus_provider.assert_not_called()

            with self.subTest(state=state, stage="dispatch"), patch.dict(self.bus.states):
                change_state = Mock(side_effect=lambda: self.bus.states.update({BUTTON: state}))
                receiver = self.executor(focus_monitor_for=self.focus_provider(on_arm=change_state))
                self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
                change_state.assert_called_once_with()

    def test_mutant_treating_selected_as_sensitive_is_killed(self):
        self.bus.states[BUTTON] = ENABLED_BIT | SELECTED_BIT
        self.require_refusal(self.act(self.observe()), "ATSPI_ACTION_UNQUALIFIED")

        with patch("channel.atspi._SENSITIVE", SELECTED_BIT):
            result = self.act(self.observe())
        with self.assertRaises(AssertionError):
            self.require_refusal(result, "ATSPI_ACTION_UNQUALIFIED")
        self.assertIn(("DoAction", BUTTON, "(i)", (0,)), self.bus.trace)

    def test_host_only_qualified_chrome_tab_click(self):
        self.host()
        self.bus.names[BUTTON] = "Title changed on the independently bound tab"
        receiver = self.executor(focus_monitor_for=self.focus_provider())
        reply, after = self.act(self.observe(receiver), receiver)
        self.assertTrue(reply["ok"])
        self.assertEqual(after["atspi_capture"], "polling_only")
        arm = self.bus.trace.index(("focus-arm",))
        send = self.bus.trace.index(("DoAction", BUTTON, "(i)", (0,)))
        finish = self.bus.trace.index(("focus-finish",))
        self.assertLess(arm, send)
        self.assertIn(("GetState", BUTTON), self.bus.trace[send + 1:finish])

    def test_host_refuses_missing_or_unarmed_focus_binding(self):
        self.host()
        self.require_refusal(self.act(self.observe()), "ATSPI_ACTION_UNQUALIFIED")
        receiver = self.executor(focus_monitor_for=self.focus_provider(unavailable=True))
        self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
        self.assertIn(("focus-arm",), self.bus.trace)

    def test_focus_loss_after_send_is_uncertain_and_retires_observation(self):
        self.host()
        for trace in ({"continuous": False}, {"events": (
            {"kind": "_NET_ACTIVE_WINDOW", "xid": "0x18", "mono_ns": 130},
            {"kind": "_NET_ACTIVE_WINDOW", "xid": "0x20", "mono_ns": 150},
        )}):
            with self.subTest(trace=trace):
                self.bus.trace.clear()
                receiver = self.executor(focus_monitor_for=self.focus_provider(**trace))
                before = self.observe(receiver)
                reply, snapshot = self.act(before, receiver)
                self.assertEqual(reply["code"], "UNCERTAIN_OUTCOME")
                self.assertIsNone(snapshot)
                self.assertEqual(self.act(before, receiver)[0]["code"], "ATSPI_STALE_OBSERVATION")
                self.assertEqual(self.bus.trace.count(("DoAction", BUTTON, "(i)", (0,))), 1)
                send = self.bus.trace.index(("DoAction", BUTTON, "(i)", (0,)))
                finish = self.bus.trace.index(("focus-finish",))
                self.assertIn(("GetState", BUTTON), self.bus.trace[send + 1:finish])

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
                    self.bus.states[BUTTON] |= DEFUNCT_BIT
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

    def test_last_action_binding_refuses_changed_owner_pid_or_object(self):
        for field, table, key, replacement in (
            ("pid", self.bus.owners, BUTTON.bus, 25),
            ("window", self.bus.children, APP, []),
            ("path", self.bus.children, WINDOW, []),
            ("defunct", self.bus.states, BUTTON, ENABLED | DEFUNCT_BIT),
            ("name", self.bus.names, BUTTON, "Substitute toggle"),
        ):
            with self.subTest(field=field), patch.dict(table):
                receiver = self.executor(focus_monitor_for=self.focus_provider(
                    on_arm=lambda: table.__setitem__(key, replacement)))
                prior = self.observe(receiver)
                self.require_refusal(self.act(prior, receiver), "ATSPI_RETIRED_OBJECT")
                self.require_refusal(self.act(prior, receiver), "ATSPI_STALE_OBSERVATION")
                self.assertEqual(table[key], replacement)

    def test_selection_refuses_index_change_after_preflight(self):
        def reorder():
            self.bus.children[WINDOW] = [BUTTON_REPLACEMENT, BUTTON]

        receiver = self.executor(focus_monitor_for=self.focus_provider(on_arm=reorder))
        self.require_refusal(self.act(self.observe(receiver), receiver, action="select"),
                             "ATSPI_STALE_OBSERVATION")

    def test_mutant_missing_final_binding_check_is_killed(self):
        class NoFinalBinding(AtspiExecutor):
            def _verify_action_binding(self, *_):
                pass

        def retire():
            self.bus.states[BUTTON] |= DEFUNCT_BIT

        def refuses_retirement(cls):
            self.bus.states[BUTTON] = ENABLED
            receiver = self.executor(cls, focus_monitor_for=self.focus_provider(on_arm=retire))
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_RETIRED_OBJECT")

        refuses_retirement(AtspiExecutor)
        with self.assertRaises(AssertionError):
            refuses_retirement(NoFinalBinding)
        self.assertIn(("DoAction", BUTTON, "(i)", (0,)), self.bus.trace)

    def test_mutant_bypassing_focus_monitor_is_killed(self):
        class NoFocusMonitor(AtspiExecutor):
            def _dispatch(self, request, session, identity, node, effect, deadline, focus):
                return super()._dispatch(request, session, identity, node, effect, deadline, None)

        def refuses_unarmed_host(cls):
            receiver = self.executor(cls, focus_monitor_for=self.focus_provider(unavailable=True))
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")

        self.host()
        refuses_unarmed_host(AtspiExecutor)
        with self.assertRaises(AssertionError):
            refuses_unarmed_host(NoFocusMonitor)
        self.assertIn(("DoAction", BUTTON, "(i)", (0,)), self.bus.trace)

    def test_truncated_tree_is_refused_on_observe_and_act(self):
        receiver = self.executor(max_nodes=2, max_depth=1)
        before = self.observe(receiver)
        self.bus.children[BUTTON] = [BUTTON_REPLACEMENT]
        self.require_refusal(self.act(before, receiver), "ATSPI_TREE_TRUNCATED")
        self.require_refusal(receiver.observe(request(self.session), session=self.session), "ATSPI_TREE_TRUNCATED")

    def test_unqualified_host_actions_and_tab_substitution_are_refused(self):
        self.host()
        receiver = self.executor(focus_monitor_for=self.focus_provider())
        with patch("subprocess.run") as native:
            self.require_refusal(self.act(self.observe(receiver), receiver, action="focus"), "ATSPI_ACTION_UNQUALIFIED")
            self.identity = replace(self.identity, qualified_tab=None)
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
            self.identity = replace(self.identity, qualified_tab=BUTTON_REPLACEMENT)
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
            self.identity = replace(self.identity, qualified_tab=BUTTON)
            self.bus.roles[BUTTON] = "push button"
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
            native.assert_not_called()

    def test_atspi_consumes_shared_wl0_fixtures(self):
        self.host()
        with TemporaryDirectory(dir=HERE) as directory:
            for index, (name, data, qualified) in enumerate(receipt_cases()):
                with self.subTest(name=name):
                    self.bus.trace.clear()
                    path = Path(directory) / f"receipt-{index}.json"
                    if data is not None:
                        path.write_bytes(data)
                    receiver = self.executor(receipt_path=path, focus_monitor_for=self.focus_provider())
                    result = self.act(self.observe(receiver), receiver)
                    validate_contract("vp.channel.reply/v1", result[0])
                    if qualified:
                        self.assertEqual(result[0]["code"], "OK")
                        self.assertEqual(self.bus.trace.count(("DoAction", BUTTON, "(i)", (0,))), 1)
                    else:
                        self.require_refusal(result, "ATSPI_ACTION_UNQUALIFIED")

    def test_mutant_treating_any_tab_as_qualified_reaches_dispatch(self):
        class IgnoreTabBinding(AtspiExecutor):
            def _qualified(self, action, identity, node, session):
                return super()._qualified(action, replace(identity, qualified_tab=node.ref), node, session)

        def reject_unbound_tab(cls):
            receiver = self.executor(cls, focus_monitor_for=self.focus_provider())
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")

        self.host()
        self.identity = replace(self.identity, qualified_tab=BUTTON_REPLACEMENT)
        reject_unbound_tab(AtspiExecutor)
        with self.assertRaises(AssertionError):
            reject_unbound_tab(IgnoreTabBinding)
        self.assertIn(("DoAction", BUTTON, "(i)", (0,)), self.bus.trace)

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
            receiver = self.executor(receipt_path=receipt_path, focus_monitor_for=self.focus_provider())
            self.require_refusal(self.act(self.observe(receiver), receiver), "ATSPI_ACTION_UNQUALIFIED")
            mutant = self.executor(IgnoreReceipt, receipt_path=receipt_path, focus_monitor_for=self.focus_provider())
            with self.assertRaises(AssertionError):
                self.require_refusal(self.act(self.observe(mutant), mutant), "ATSPI_ACTION_UNQUALIFIED")
            self.assertIn(("DoAction", BUTTON, "(i)", (0,)), self.bus.trace)
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
        without_native(self.executor(focus_monitor_for=self.focus_provider()))
        with self.assertRaises(AssertionError):
            without_native(self.executor(NativeFallback, focus_monitor_for=self.focus_provider()))


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
                        with self.assertRaisesRegex(Refused, "ATSPI_RETIRED_OBJECT"):
                            transport.call(*endpoint, "org.freedesktop.DBus.GetConnectionUnixProcessID",
                                           "(s)", (":1.999999",), "(u)", monotonic() + 2)
                        connect.assert_called_once()
                        transport.close()
                        with self.assertRaisesRegex(Refused, "ATSPI_BUS_UNAVAILABLE"):
                            transport.call(*endpoint, method, "", (), "(s)", monotonic() + 2)
                finally:
                    daemon.terminate()
                    daemon.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
