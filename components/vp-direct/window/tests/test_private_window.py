from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import LIVE_private_window as live
from focus import Readback
from test_window import FakeDesktop
from window import WindowBinding, WindowController, WindowRefused
from window.x11 import WmctrlBackend


class PrivateWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        proc = self.root / "proc"
        self.enterContext(patch("window.x11._PROC", proc))
        self.enterContext(patch("window.x11._X11_SOCKETS", self.root))

        self.server = proc / "42"
        self.server.mkdir(parents=True)
        (self.server / "fd").mkdir()
        executable = self.root / "Xvfb"
        executable.touch()
        (self.server / "exe").symlink_to(executable)
        self.set_start(self.server, 16)
        (self.server / "fd" / "3").symlink_to("socket:[41]")
        self.endpoint = self.root / "X91"
        self.listener = self.enterContext(socket.socket(socket.AF_UNIX))
        self.listener.bind(str(self.endpoint))
        self.table = proc / "net" / "unix"
        self.table.parent.mkdir()
        self.listener_row = f"0000: 00000002 00000000 00010000 0001 01 41 {self.endpoint}\n"
        self.table.write_text(self.listener_row)

        self.desktop = FakeDesktop()
        self.desktop.display = ":91"
        self.authority = self.root / "fixture.xauthority"
        self.authority.touch()
        self.app = proc / str(self.desktop.pid)
        self.app.mkdir()
        self.set_start(self.app, 26)
        self.desktop.host_list += ("--display", self.desktop.display, "--xauthority", str(self.authority))
        self.child = Mock(side_effect=self.desktop.run)
        self.pointer = Mock(return_value=self.desktop.cursor)
        self.session = {
            "schema": "vp.session/v1", "session_id": "private-fixture", "kind": "app",
            "site_group": "fixture", "profile_id": "fixture", "display": "xpra:91",
            "backend": "atspi", "cdp_allowed": False, "capsule_id": "fixture-capsule",
            "incarnation": "fixture-incarnation", "state": "AGENT",
            "window": {"xid": "0x123", "pid": self.desktop.pid, "witness": self.desktop.witness},
            "lease": {"holder": "agent-instance", "authority_epoch": 5},
            "pace": {"min_interval_ms": 0}, "risk_ceiling": "observar",
        }
        record = self.root / "session.json"
        raw = json.dumps(self.session).encode()
        record.write_bytes(raw)
        self.args = SimpleNamespace(
            display=self.desktop.display, xvfb_pid=42, xvfb_start_ticks=16,
            window_start_ticks=26, xauthority=self.authority, srf_bin_dir="/test",
            session_record=record, session_sha256=sha256(raw).hexdigest(), surface_id="fixture",
        )

    @staticmethod
    def set_start(process, ticks):
        (process / "stat").write_text(f"1 (fixture process) S {'0 ' * 18}{ticks} 0\n")

    def backend(self, **overrides):
        options = {
            "display": self.args.display, "server_pid": self.args.xvfb_pid,
            "server_start_ticks": self.args.xvfb_start_ticks, "xauthority": self.authority,
            "run": self.child, "pointer": self.pointer,
        }
        return WmctrlBackend("/test", **(options | overrides))

    def test_private_round_trip_uses_selected_display_for_every_child(self):
        (self.server / "fd" / "4").symlink_to("socket:[42]")
        abstract = f"0000: 00000002 00000000 00010000 0001 01 42 @{self.endpoint}\n"
        self.table.write_text(self.listener_row + abstract)
        backend = self.backend()
        controller = WindowController(lambda _: self.session, {"wmctrl": backend})
        with patch.dict(os.environ, {"DISPLAY": ":0", "XAUTHORITY": "/ambient", "HOME": "/ambient"}):
            dismissed = controller.vp_window_dismiss("private-fixture")
            summoned = controller.vp_window_summon("private-fixture")
        self.assertEqual((dismissed["from_ws"], dismissed["to_ws"]), (0, 1))
        self.assertEqual((summoned["from_ws"], summoned["to_ws"]), (1, 0))
        self.assertEqual({call.args[0][0] for call in self.child.call_args_list},
                         {"xprop", "/test/srf-host-window", "wmctrl"})
        for call in self.child.call_args_list:
            self.assertEqual(call.kwargs["env"], {
                "DISPLAY": self.args.display, "XAUTHORITY": str(self.authority),
                "LC_ALL": "C", "PATH": os.defpath,
            })
        self.pointer.assert_called()

    def test_missing_or_invalid_private_parameters_refuse_before_io(self):
        for options in (
            {"display": None}, {"display": ":0"}, {"display": "remote:91"},
            {"display": ":91.0"}, {"server_pid": None}, {"server_start_ticks": None},
            {"server_start_ticks": 17}, {"xauthority": None}, {"pointer": None},
        ):
            with self.subTest(options=options), self.assertRaises(WindowRefused):
                self.backend(**options)
        self.child.assert_not_called()
        self.pointer.assert_not_called()

    def test_binding_display_mismatch_refuses_before_io(self):
        backend = self.backend()
        for display in (":0", "xpra:92", ""):
            binding = WindowBinding(0x123, self.desktop.pid, self.desktop.witness, "fixture", display)
            for action in (backend.workspace, backend.verify_window, partial(backend.move, workspace=1)):
                with self.subTest(display=display, action=action), self.assertRaises(WindowRefused):
                    action(binding)
        self.child.assert_not_called()
        self.pointer.assert_not_called()

    def test_reused_server_pid_refuses_before_io(self):
        backend = self.backend()
        self.set_start(self.server, 17)
        with self.assertRaisesRegex(WindowRefused, "identity changed"):
            backend.desktop()
        self.child.assert_not_called()
        self.pointer.assert_not_called()

    def test_missing_listener_and_foreign_abstract_listener_refuse_before_io(self):
        backend = self.backend()
        foreign = f"0000: 00000002 00000000 00010000 0001 01 99 @{self.endpoint}\n"
        for rows in ("", self.listener_row + foreign):
            self.table.write_text(rows)
            with self.subTest(rows=rows), self.assertRaises(WindowRefused):
                backend.desktop()
        self.child.assert_not_called()
        self.pointer.assert_not_called()

    def test_replaced_socket_node_refuses_before_io(self):
        backend = self.backend()
        self.endpoint.unlink()
        replacement = self.enterContext(socket.socket(socket.AF_UNIX))
        replacement.bind(str(self.endpoint))
        with self.assertRaisesRegex(WindowRefused, "identity changed"):
            backend.desktop()
        self.child.assert_not_called()
        self.pointer.assert_not_called()

    def test_pointer_refuses_if_private_identity_changes_after_root_read(self):
        backend = self.backend()

        def read_and_replace(*args, **kwargs):
            result = self.desktop.run(*args, **kwargs)
            self.set_start(self.server, 17)
            return result

        self.child.side_effect = read_and_replace
        with self.assertRaises(WindowRefused):
            backend.desktop()
        self.child.assert_called_once()
        self.pointer.assert_not_called()

    def test_live_runner_refuses_missing_or_changed_identity_before_entering_x(self):
        with (
            patch.object(live, "X11FocusSource", autospec=True) as source,
            patch.object(live, "WmctrlBackend", partial(WmctrlBackend, run=self.child)),
            patch.dict(os.environ),
        ):
            for overrides, error in (
                ({"display": ":0"}, ValueError),
                ({"session_sha256": ""}, WindowRefused),
                ({"session_sha256": "0" * 64}, WindowRefused),
                ({"xvfb_pid": 999}, WindowRefused),
                ({"xvfb_start_ticks": 17}, WindowRefused),
                ({"window_start_ticks": 27}, ValueError),
            ):
                args = SimpleNamespace(**(vars(self.args) | overrides))
                with self.subTest(overrides=overrides), self.assertRaises(error):
                    live.run(args)
            (self.app / "stat").unlink()
            with self.assertRaises(WindowRefused):
                live.run(self.args)
            self.args.session_record.unlink()
            with self.assertRaises(WindowRefused):
                live.run(self.args)
        source.return_value.__enter__.assert_not_called()
        self.child.assert_not_called()
        source.return_value.pointer.assert_not_called()

    def test_live_runner_arms_focus_before_dispatch_and_emits_traces(self):
        with (
            patch.object(live, "X11FocusSource", autospec=True) as make_source,
            patch.object(live, "WmctrlBackend", partial(WmctrlBackend, run=self.child)),
            patch.dict(os.environ),
            patch("builtins.print") as output,
        ):
            source = make_source.return_value
            source.__enter__.return_value = source
            source.pointer.return_value = self.desktop.cursor
            source.arm.return_value = "0x456"
            source.finish.return_value = Readback((), True)
            order = Mock()
            order.attach_mock(source.arm, "arm")
            order.attach_mock(source.finish, "finish")
            order.attach_mock(self.child, "child")
            live.run(self.args)

        make_source.assert_called_once_with(self.args.display, self.args.xvfb_pid)
        order_names = [call[0] for call in order.mock_calls]
        self.assertEqual([name for name in order_names if name != "child"], ["arm", "finish"] * 100)
        self.assertEqual(order_names[0], "arm")
        self.assertEqual(order_names[-1], "finish")
        self.assertEqual(len(self.desktop.moves), 200)
        self.assertEqual(output.call_count, 100)
        self.assertTrue(all(json.loads(call.args[0])["trace"]["continuous"] for call in output.call_args_list))

    def test_mutant_forcing_operator_display_breaks_endpoint_oracle(self):
        def redirect(*args, env, **kwargs):
            return self.desktop.run(*args, env=env | {"DISPLAY": ":0"}, **kwargs)

        self.child.side_effect = redirect
        with self.assertRaisesRegex(AssertionError, "escaped the selected display"):
            self.test_private_round_trip_uses_selected_display_for_every_child()

    def test_mutant_skipping_identity_recheck_breaks_reused_pid_oracle(self):
        with patch.object(WmctrlBackend, "check_private_display"):
            with self.assertRaisesRegex(AssertionError, "WindowRefused not raised"):
                self.test_reused_server_pid_refuses_before_io()
