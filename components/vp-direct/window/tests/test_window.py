from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

VP_DIRECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VP_DIRECT))

from window import DesktopState, PresentationBackend, WindowController, WindowRefused
from window.x11 import WmctrlBackend


class FakeDesktop:
    def __init__(self):
        self.window_workspace = 0
        self.current_workspace = 0
        self.active = "0x456"
        self.cursor = (223, 418)
        self.witness = "window-incarnation-evidence"
        self.pid = 314
        self.commands = []
        self.after_move = lambda: None
        self.apply_move = True

    def run(self, argv, *, env, check, capture_output, text, timeout):
        if env["DISPLAY"] != ":0" or not (check and capture_output and text and timeout > 0):
            raise AssertionError("command escaped the observed host display contract")
        self.commands.append(argv)
        if argv == ("xprop", "-root", "_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS",
                    "_NET_ACTIVE_WINDOW"):
            output = (
                f"_NET_CURRENT_DESKTOP(CARDINAL) = {self.current_workspace}\n"
                "_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 4\n"
                f"_NET_ACTIVE_WINDOW(WINDOW): window id # {self.active}\n"
            )
        elif argv == ("xprop", "-id", "0x123", "_NET_WM_DESKTOP"):
            output = f"_NET_WM_DESKTOP(CARDINAL) = {self.window_workspace}\n"
        elif argv == ("xprop", "-id", "0x123", "_NET_WM_PID"):
            output = f"_NET_WM_PID(CARDINAL) = {self.pid}\n"
        elif argv == ("/test/srf-host-window", "list"):
            output = json.dumps([{"xid": 0x123, "witness": self.witness}])
        elif argv[:5] == ("wmctrl", "-i", "-r", "0x123", "-t") and len(argv) == 6:
            if self.apply_move:
                self.window_workspace = int(argv[5])
            self.after_move()
            output = ""
        else:
            raise AssertionError(f"unexpected command: {argv}")
        return SimpleNamespace(stdout=output)

    @property
    def moves(self):
        return [argv for argv in self.commands if argv[0] == "wmctrl"]


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.session = {
            "schema": "vp.session/v1",
            "session_id": "s-example",
            "kind": "app",
            "site_group": "fixture",
            "profile_id": "fixture",
            "display": ":0",
            "backend": "atspi",
            "cdp_allowed": False,
            "window": {"pid": self.desktop.pid, "xid": "0x00000123",
                       "witness": self.desktop.witness},
            "capsule_id": "fixture-capsule",
            "incarnation": "fixture-incarnation",
            "state": "AGENT",
            "lease": {"holder": "agent-instance", "authority_epoch": 5},
            "pace": {"min_interval_ms": 0},
            "risk_ceiling": "observar",
        }
        self.backend = WmctrlBackend("/test", run=self.desktop.run,
                                     pointer=lambda: self.desktop.cursor)
        self.controller = WindowController(
            {"s-example": self.session}.get, {"wmctrl": self.backend}
        )

    def assertRefused(self, verb, code):
        with self.assertRaises(WindowRefused) as result:
            verb("s-example")
        self.assertEqual(result.exception.code, code)

    def test_round_trip_is_passive_idempotent_and_preserves_authority(self):
        before = deepcopy(self.session)
        original_desktop = self.backend.desktop()
        schema = json.loads(
            (VP_DIRECT / "schemas" / "vp.window.reply_v1.schema.json").read_text()
        )
        validate = Draft202012Validator(schema).validate
        for verb, source, target in (
            (self.controller.vp_window_dismiss, 0, 1),
            (self.controller.vp_window_summon, 1, 0),
        ):
            for moved, from_ws in ((True, source), (False, target)):
                result = verb("s-example")
                validate(result)
                self.assertEqual(result, {"moved": moved, "from_ws": from_ws,
                                          "to_ws": target, "backend": "wmctrl",
                                          "witness_ok": True})
                self.assertEqual(self.session, before)
                self.assertEqual(self.backend.desktop(), original_desktop)
        self.assertEqual(self.desktop.moves, [
            ("wmctrl", "-i", "-r", "0x123", "-t", "1"),
            ("wmctrl", "-i", "-r", "0x123", "-t", "0"),
        ])

    def test_summon_uses_live_operator_workspace(self):
        self.desktop.current_workspace = 2
        self.assertEqual(self.controller.vp_window_summon("s-example")["to_ws"], 2)

    def test_frozen_control_args_call_both_window_verbs(self):
        schemas = VP_DIRECT / "schemas"
        validator = Draft202012Validator(json.loads(
            (schemas / "vp.control.req_v1.schema.json").read_text()
        ))
        request = json.loads(
            (schemas / "tests/examples/vp.control.req_v1.valid.json").read_text()
        )
        request["args"]["session_id"] = request["context"]["session_id"] = "s-example"
        request["dry_run"] = False
        for op in ("vp_window_dismiss", "vp_window_summon"):
            request["op"] = op
            validator.validate(request)
            self.assertTrue(getattr(self.controller, op)(**request["args"])["moved"])
            request["args"]["xid"] = "0x123"
            self.assertFalse(validator.is_valid(request))
            del request["args"]["xid"]

    def test_missing_registry_entry_refuses_before_host_io(self):
        with self.assertRaises(WindowRefused) as result:
            self.controller.vp_window_summon("s-unregistered")
        self.assertEqual(result.exception.code, "host_window_unavailable")
        self.assertEqual(self.desktop.commands, [])

    def test_reused_xid_refused_even_for_no_op(self):
        self.desktop.witness = "later-window-with-same-xid"
        for verb in (self.controller.vp_window_summon, self.controller.vp_window_dismiss):
            self.assertRefused(verb, "host_window_unavailable")
        self.assertEqual(self.desktop.moves, [])

    def test_pid_mismatch_refuses(self):
        self.desktop.pid += 1
        self.assertRefused(self.controller.vp_window_dismiss, "host_window_unavailable")
        self.assertEqual(self.desktop.moves, [])

    def test_active_target_refuses_move_that_would_drop_focus(self):
        self.desktop.active = "0x123"
        self.assertRefused(self.controller.vp_window_dismiss, "host_window_unavailable")
        self.assertEqual(self.desktop.moves, [])

    def test_unqualified_receipt_or_missing_backend_refuses(self):
        for step in ({}, {"backend": "wmctrl", "status": "FAIL", "agent_workspace": 1},
                     {"backend": "xpra", "status": "PASS", "agent_workspace": 1},
                     {"backend": "wmctrl", "status": "PASS"}):
            with self.subTest(step=step), self.assertRaises(WindowRefused):
                WindowController(lambda _: self.session, {"wmctrl": self.backend},
                                 receipt={"schema": "vp.l0_receipt/v1",
                                          "steps": {"L0-3": step}})

    def test_qualified_xpra_selected_through_backend_port(self):
        receipt = {"schema": "vp.l0_receipt/v1", "steps": {"L0-3": {
            "backend": "xpra", "status": "PASS", "agent_workspace": 1,
        }}}
        xpra = Mock(spec=PresentationBackend)
        xpra.desktop.return_value = DesktopState(0, 2, 0x456, (223, 418))
        xpra.workspace.side_effect = [0, 0, 1]
        controller = WindowController(
            lambda _: self.session, {"xpra": xpra}, receipt=receipt,
        )
        self.assertEqual(controller.vp_window_dismiss("s-example")["backend"], "xpra")
        xpra.move.assert_called_once()
        self.assertEqual(self.desktop.commands, [])

    def test_no_workspace_ack_is_uncertain_without_retry(self):
        self.desktop.apply_move = False
        with patch("window.monotonic", side_effect=[0, 2]):
            self.assertRefused(self.controller.vp_window_dismiss, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(self.desktop.moves), 1)

    def test_command_timeout_is_uncertain_without_retry(self):
        def timeout():
            raise subprocess.TimeoutExpired(self.desktop.moves[-1], 2)

        self.desktop.after_move = timeout
        self.assertRefused(self.controller.vp_window_dismiss, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(self.desktop.moves), 1)

    def test_mutant_activate_on_move_is_detected(self):
        self.desktop.after_move = lambda: setattr(self.desktop, "active", "0x123")
        self.assertRefused(self.controller.vp_window_dismiss, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(self.desktop.moves), 1)

    def test_mutant_switch_workspace_is_detected(self):
        self.desktop.after_move = lambda: setattr(self.desktop, "current_workspace", 1)
        self.assertRefused(self.controller.vp_window_dismiss, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(self.desktop.moves), 1)

    def test_cursor_change_is_detected(self):
        self.desktop.after_move = lambda: setattr(self.desktop, "cursor", (0, 0))
        self.assertRefused(self.controller.vp_window_dismiss, "UNCERTAIN_OUTCOME")

    def test_mutant_key_by_xid_only_fails_reused_xid_oracle(self):
        with patch.object(self.backend, "verify_window"):
            with self.assertRaises(AssertionError):
                self.test_reused_xid_refused_even_for_no_op()

    def test_mutant_bump_incarnation_is_detected(self):
        self.desktop.after_move = lambda: self.session.update(incarnation="mutated")
        self.assertRefused(self.controller.vp_window_dismiss, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(self.desktop.moves), 1)

    def test_xdotool_command_fails_command_oracle(self):
        def inject(binding, workspace):
            self.backend._command("xdotool", "getactivewindow")

        with patch.object(self.backend, "move", side_effect=inject):
            with self.assertRaisesRegex(AssertionError, "unexpected command"):
                self.controller.vp_window_dismiss("s-example")
