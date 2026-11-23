import ast
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import launch as s2


CHROME = "/opt/google/chrome/chrome"
OPERATOR = s2.Window("0x10", 100, 0)
SCHEMAS = s2.RECEIPT.parents[1] / "schemas"


class Child:
    def __init__(self, pid):
        self.pid = pid
        self.running = True
        self.stops = 0

    def alive(self):
        return self.running

    def stop(self):
        self.running = False
        self.stops += 1


class Host:
    def __init__(self, workspace, *, ports=(41001, 41002, 41003), collisions=()):
        self.workspace = workspace
        self.ports = iter(ports)
        self.collisions = set(collisions)
        self.children = []
        self.foreign = Child(OPERATOR.pid)
        self.commands = []
        self.events = []
        self.window = None
        self.extra_window = None
        self.emit_window = True
        self.initial_xids = [OPERATOR]
        self.version_ok = True
        self.focus_on_launch = True
        self.operator_workspace = OPERATOR.workspace
        self.exit_on_spawn = False

    def verify_binary(self, binary, package):
        self.events.append(("package", binary, package))

    def allocate_port(self):
        self.events.append(("allocate",))
        return next(self.ports)

    def spawn(self, argv):
        self.commands.append(argv)
        child = Child(900 + len(self.children))
        self.children.append(child)
        if self.emit_window:
            self.window = s2.Window("0x20", child.pid, self.operator_workspace)
        if self.exit_on_spawn:
            child.running = False
        return child

    def windows(self):
        result = list(self.initial_xids)
        if self.children and self.children[-1].alive() and self.window:
            result.append(self.window)
            if self.extra_window:
                result.append(s2.Window(self.extra_window, self.window.pid, self.window.workspace))
        return result

    def desktop(self):
        active = OPERATOR.xid
        if (self.window and self.children[-1].alive() and self.focus_on_launch
                and self.window.workspace != self.workspace):
            active = self.window.xid
        return active, self.operator_workspace

    def move(self, window, destination):
        self.events.append(("dismiss", window.xid, destination))
        self.window = s2.Window(window.xid, window.pid, destination)

    def listener(self, child, port):
        self.events.append(("listener", child.pid, port))
        return "busy" if port in self.collisions else "owned"

    def debug_version(self, port, timeout):
        self.events.append(("version", port))
        return self.version_ok


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "vp-profiles"
        self.root.mkdir(mode=0o700)
        (self.root / "synthetic").mkdir(mode=0o700)
        self.policy = s2.LaunchPolicy.load()

    def launcher(self, host, **options):
        return s2.Launcher(self.root, host=host, ready_timeout=0.05,
                           poll_interval=0.001, **options)

    def host(self, **options):
        return Host(self.policy.agent_workspace, **options)

    def test_launch_and_dismiss_records_focus_and_correlates_readiness(self):
        host = self.host()
        browser = self.launcher(host).launch(CHROME, "synthetic")
        self.addCleanup(browser.stop)
        self.assertEqual(host.commands, [(
            CHROME, f"--user-data-dir={self.root / 'synthetic'}",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=41001",
            "--force-renderer-accessibility", "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        )])
        self.assertEqual(browser.window_binding, {
            "pid": host.children[0].pid, "xid": "0x20", "witness": browser.witness,
        })
        self.assertTrue(browser.focus_take_observed)
        trace = browser.focus_trace("surface-s2-test")
        self.assertFalse(trace["continuous"])
        self.assertEqual(trace["events"][0]["xid"], OPERATOR.xid)
        self.assertEqual(trace["events"][-1]["xid"], OPERATOR.xid)
        self.assertTrue(all(trace["start_mono_ns"] <= event["mono_ns"] <= trace["end_mono_ns"]
                            for event in trace["events"]))
        self.assertEqual(browser.window.workspace, self.policy.agent_workspace)
        self.assertEqual(host.events.count(("dismiss", "0x20", self.policy.agent_workspace)), 1)
        dismiss = host.events.index(("dismiss", "0x20", self.policy.agent_workspace))
        version = host.events.index(("version", 41001))
        self.assertLess(dismiss, version)
        self.assertIn(("listener", browser.window.pid, 41001), host.events[version + 1:])

    def test_outputs_validate_against_frozen_schemas(self):
        browser = self.launcher(self.host()).launch(CHROME, "synthetic")
        self.addCleanup(browser.stop)
        session_schema = json.loads((SCHEMAS / "vp.session_v1.schema.json").read_text())
        focus_schema = json.loads((SCHEMAS / "vp.focus_trace_v1.schema.json").read_text())
        Draft202012Validator(session_schema["properties"]["window"]).validate(browser.window_binding)
        Draft202012Validator(focus_schema).validate(browser.focus_trace("surface-s2-test"))

    def test_focus_absence_is_limited_to_sampled_evidence(self):
        host = self.host()
        host.focus_on_launch = False
        browser = self.launcher(host).launch(CHROME, "synthetic")
        self.addCleanup(browser.stop)
        self.assertFalse(browser.focus_take_observed)
        self.assertFalse(browser.focus_trace("surface-s2-test")["continuous"])

    def test_allowlist_accepts_the_three_deb_paths(self):
        for binary, package in s2.BINARIES.items():
            with self.subTest(binary=binary):
                host = self.host()
                self.launcher(host).launch(binary, "synthetic", flags=()).stop()
                self.assertIn(("package", binary, package), host.events)
                self.assertEqual(len(host.commands[0]), 4)

    def test_receipt_requires_all_qualified_steps_and_pinned_allowlist(self):
        for key in ("L0-1", "L0-2", "L0-3", "allowlist", "dismiss"):
            with self.subTest(key=key):
                receipt = json.loads(s2.RECEIPT.read_text())
                if key.startswith("L0-"):
                    receipt["steps"][key]["status"] = "NOT RUN"
                elif key == "allowlist":
                    receipt["backend_rules"]["cdp"]["browser_allowed_flags"].pop()
                else:
                    receipt["focus_rules"]["launch"]["launch_then_dismiss_one_step"] = False
                path = Path(self.temp.name) / "receipt.json"
                path.write_text(json.dumps(receipt))
                host = self.host()
                with self.assertRaises(s2.LaunchError):
                    s2.Launcher(self.root, receipt=path, host=host)
                self.assertEqual(host.events, [])

    def test_readiness_requires_both_version_and_own_window(self):
        for fault in ("version", "window", "duplicate", "reused_xid", "dismiss"):
            with self.subTest(fault=fault):
                host = self.host()
                if fault == "version":
                    host.version_ok = False
                elif fault == "window":
                    host.emit_window = False
                elif fault == "duplicate":
                    host.extra_window = "0x21"
                elif fault == "reused_xid":
                    host.initial_xids.append(s2.Window("0x20", 310, 0))
                else:
                    host.move = lambda *_: None
                with self.assertRaises(s2.LaunchError):
                    self.launcher(host).launch(CHROME, "synthetic")
                self.assertEqual(host.children[0].stops, 1)
                self.assertEqual(host.foreign.stops, 0)

    def test_forbidden_flags_and_host_port_form_refused_before_spawn(self):
        for flag in ("--user-agent=spoof", "--no-sandbox", "/tmp/page",
                     "--remote-debugging-port=127.0.0.1:41001",
                     "--remote-debugging-address=0.0.0.0", "--user-data-dir=/tmp/other"):
            with self.subTest(flag=flag):
                host = self.host()
                with self.assertRaisesRegex(s2.LaunchError, "outside the allowlist"):
                    self.launcher(host).launch(CHROME, "synthetic", flags=(flag,))
                self.assertEqual(host.events, [])

    def test_snap_unknown_executable_and_default_profile_refused(self):
        for binary, profile, code in (
            ("/snap/chromium/current/usr/lib/chromium-browser/chrome", "synthetic",
             "SNAP_CDP_UNAVAILABLE"),
            ("/usr/bin/firefox", "synthetic", "BIDI_DEFERRED"),
            ("/usr/bin/chromedriver", "synthetic", None),
            (CHROME, "Default", "DEFAULT_PROFILE_REFUSED"),
            (CHROME, "../Default", "DEFAULT_PROFILE_REFUSED"),
        ):
            with self.subTest(binary=binary, profile=profile):
                host = self.host()
                with self.assertRaises(s2.LaunchError) as failure:
                    self.launcher(host).launch(binary, profile)
                self.assertEqual(failure.exception.code, code)
                self.assertEqual(host.events, [])

    def test_symlink_and_nonprivate_profile_refused(self):
        (self.root / "alias").symlink_to(self.root / "synthetic")
        public = self.root / "loose"
        public.mkdir()
        public.chmod(0o755)
        for profile in ("alias", "loose"):
            with self.subTest(profile=profile):
                host = self.host()
                with self.assertRaises(s2.LaunchError):
                    self.launcher(host).launch(CHROME, profile)
                self.assertEqual(host.events, [])

    def test_foreign_listener_retries_without_probing_or_signalling_it(self):
        host = self.host(collisions={41001})
        browser = self.launcher(host).launch(CHROME, "synthetic")
        self.assertEqual(browser.cdp_port, 41002)
        self.assertEqual([child.stops for child in host.children], [1, 0])
        self.assertNotIn(("version", 41001), host.events)
        browser.stop()
        self.assertEqual([child.stops for child in host.children], [1, 1])
        self.assertEqual(host.foreign.stops, 0)

    def test_port_busy_at_bound_even_if_a_child_already_exited(self):
        for exited in (False, True):
            with self.subTest(exited=exited):
                host = self.host(collisions={41001, 41002, 41003})
                host.exit_on_spawn = exited
                with self.assertRaises(s2.LaunchError) as failure:
                    self.launcher(host, port_attempts=2).launch(CHROME, "synthetic")
                self.assertEqual(failure.exception.code, "PORT_BUSY")
                self.assertEqual(host.events.count(("allocate",)), 2)
                self.assertEqual([child.stops for child in host.children], [1, 1])
                self.assertEqual(host.foreign.stops, 0)

    def test_never_reuses_the_receipt_or_previous_attempt_port(self):
        host = self.host(ports=(self.policy.observed_port, 41001, 41001, 41002),
                         collisions={41001})
        browser = self.launcher(host, port_attempts=4).launch(CHROME, "synthetic")
        self.addCleanup(browser.stop)
        self.assertEqual(browser.cdp_port, 41002)
        self.assertEqual(len(host.commands), 2)

    def test_allocator_bind_conflicts_exhaust_the_same_retry_bound(self):
        host = self.host()
        with patch.object(host, "allocate_port", side_effect=OSError(errno.EADDRINUSE, "busy")) as bind:
            with self.assertRaises(s2.LaunchError) as failure:
                self.launcher(host, port_attempts=2).launch(CHROME, "synthetic")
            self.assertEqual(failure.exception.code, "PORT_BUSY")
            self.assertEqual(bind.call_count, 2)
        self.assertEqual(host.children, [])

    def test_refuses_to_move_on_the_operator_workspace(self):
        host = self.host()
        host.operator_workspace = self.policy.agent_workspace
        with self.assertRaisesRegex(s2.LaunchError, "already on"):
            self.launcher(host).launch(CHROME, "synthetic")
        self.assertEqual(host.children, [])

    def assert_safe_launch(self):
        host = self.host()
        profile = self.root / "synthetic"
        real_open = Path.open
        allowed_argv = (
            CHROME, f"--user-data-dir={profile}", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=41001", *s2.OPTIONAL_FLAGS,
        )

        def audit_open(path, *args, **kwargs):
            if path == profile or profile in path.parents:
                raise AssertionError("profile opened")
            return real_open(path, *args, **kwargs)

        def audit_spawn(argv):
            self.assertEqual(argv, allowed_argv, "non-allowlisted command")
            return spawn(argv)

        spawn = host.spawn
        host.spawn = audit_spawn
        with patch.object(Path, "open", audit_open):
            self.launcher(host).launch(CHROME, "synthetic").stop()

    def test_safety_oracle_passes_on_production_launch(self):
        self.assert_safe_launch()

    def test_mutant_reads_devtools_active_port(self):
        argv = s2._argv

        def mutant(binary, profile, port, flags):
            (profile / "DevToolsActivePort").read_text()
            return argv(binary, profile, port, flags)

        with patch.object(s2, "_argv", mutant), self.assertRaisesRegex(AssertionError, "profile opened"):
            self.assert_safe_launch()

    def test_mutant_adds_user_agent_override(self):
        argv = s2._argv
        with patch.object(s2, "_argv", lambda *args: (*argv(*args), "--user-agent=spoof")):
            with self.assertRaisesRegex(AssertionError, "non-allowlisted command"):
                self.assert_safe_launch()

    def test_mutant_binds_non_loopback(self):
        argv = s2._argv

        def mutant(*args):
            command = list(argv(*args))
            command[2] = "--remote-debugging-address=0.0.0.0"
            return tuple(command)

        with patch.object(s2, "_argv", mutant), self.assertRaisesRegex(AssertionError, "non-allowlisted"):
            self.assert_safe_launch()

    def test_mutant_spawns_chromedriver(self):
        argv = s2._argv
        with patch.object(s2, "_argv", lambda *args: ("/usr/bin/chromedriver", *argv(*args)[1:])):
            with self.assertRaisesRegex(AssertionError, "non-allowlisted command"):
                self.assert_safe_launch()


class LinuxHostTests(unittest.TestCase):
    def test_linux_listener_distinguishes_owned_and_foreign_loopback(self):
        host = s2.LinuxHost()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertEqual(host.listener(Child(os.getpid()), port), "owned")
            dead_child = Child(os.getpid() + 1)
            dead_child.running = False
            self.assertEqual(host.listener(dead_child, port), "busy")

    def test_linux_listener_refuses_owned_wildcard_and_ipv6(self):
        for address in ("00000000", "00000000000000000000000000000000"):
            with self.subTest(address=address):
                with self.assertRaisesRegex(s2.LaunchError, "not IPv4 loopback"):
                    s2._listener_state([(address, True)])

    def test_linux_listener_detects_ipv4_race_with_owned_ipv6_fallback(self):
        self.assertEqual(s2._listener_state([
            ("0100007F", False), ("00000000000000000000000001000000", True),
        ]), "busy")

    def test_linux_isolated_probe_uses_exact_get_and_refuses_redirects(self):
        host = s2.LinuxHost()
        with patch.object(s2.http.client, "HTTPConnection") as connect:
            connection = connect.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({
                "Browser": "Chrome/test",
                "webSocketDebuggerUrl": "ws://127.0.0.1:41001/devtools/browser/test",
            }).encode()
            self.assertTrue(host.debug_version(41001, 0.1))
            connect.assert_called_once_with("127.0.0.1", 41001, timeout=0.1)
            connection.request.assert_called_once_with("GET", "/json/version")
            response.status = 302
            response.read.reset_mock()
            self.assertFalse(host.debug_version(41001, 0.1))
            response.read.assert_not_called()

    def test_linux_dismiss_uses_passive_move_and_refuses_changed_witness(self):
        host = s2.LinuxHost()
        window = s2.Window("0x20", 900, 0)
        with patch.object(host, "windows", return_value=[window]) as windows:
            with patch.object(host, "_run") as command:
                host.move(window, 1)
                command.assert_called_once_with(("/usr/bin/wmctrl", "-i", "-r", "0x20", "-t", "1"))
                windows.return_value = [s2.Window("0x20", 901, 0)]
                with self.assertRaisesRegex(s2.LaunchError, "changed"):
                    host.move(window, 1)
                self.assertEqual(command.call_count, 1)

    def test_stop_signals_only_the_owned_pidfd(self):
        host = s2.LinuxHost()
        argv = (sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)")
        foreign = subprocess.Popen(argv)
        self.addCleanup(foreign.wait)
        self.addCleanup(foreign.kill)
        owned = host.spawn(argv)
        self.addCleanup(owned.stop)
        pidfd = owned.pidfd
        with patch.object(signal, "pidfd_send_signal", wraps=signal.pidfd_send_signal) as send:
            owned.stop()
            owned.stop()
            send.assert_called_once_with(pidfd, signal.SIGTERM)
        self.assertIsNone(foreign.poll())
        self.assertFalse(owned.alive())

    def test_live_script_parses_without_execution(self):
        file = Path(__file__).with_name("LIVE_chrome_profile_restart.py")
        self.assertTrue(ast.get_docstring(ast.parse(file.read_bytes())).startswith("LIVE:"))


if __name__ == "__main__":
    unittest.main()
