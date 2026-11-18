import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "wender-agent")]

import apps
from apps import AppLauncher, AppQualification, AppSessionRegistry, oauth_browser
from bus.broker.broker import Broker
from channel import cdp
from schemas.validation import validate_contract
from session.launch import Window
from session.registry import LaunchNotRunning, SessionRefused


class Child:
    pid = 12345

    def __init__(self):
        self.stops = 0

    def alive(self):
        return self.stops == 0

    def stop(self):
        self.stops += 1


class StubHost:
    def __init__(self, environment):
        self.environment = environment
        self.argv = None
        self.child = Child()
        self.operator = Window("0x10", 12344, 0)
        self.app = None
        self.active = self.operator.xid
        self.verified = None

    def verify_binary(self, binary, package):
        self.verified = binary, package

    def allocate_port(self):
        return 42031

    def spawn(self, argv):
        self.argv = argv
        self.app = Window("0x20", self.child.pid, 0)
        self.active = self.app.xid
        return self.child

    def desktop(self):
        return self.active, 0

    def windows(self):
        return [self.operator] + ([self.app] if self.app and self.child.alive() else [])

    def move(self, window, destination):
        self.app = Window(window.xid, window.pid, destination)
        self.active = self.operator.xid

    def listener(self, child, port):
        return "owned" if (child is self.child and child.alive() and port == 42031) else "busy"

    def debug_version(self, port, timeout):
        return port == 42031 and timeout > 0


class S7Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.request = {
            "site_group": "fixture-app", "browser": "google-chrome", "kind": "app",
            "pace": {"min_interval_ms": 500}, "risk_ceiling": "leer",
        }

    def launcher(self, name="Codex Desktop", **options):
        def make_host(environment):
            self.host = StubHost(environment)
            return self.host

        return AppLauncher(name, "fixture-oauth", "google-chrome", "fixture-capsule",
                           host_factory=make_host, **{"read_target": lambda port: "fixture-page", **options})

    def registry(self, app):
        return AppSessionRegistry({("fixture-app", "google-chrome"): app}, data_home=self.home)

    def assert_launch_and_oauth(self, registry, app):
        session = registry.vp_session_open(self.request)
        try:
            validate_contract("vp.session/v1", session)
            self.assertEqual(session, registry.get_session(session["session_id"]))
            self.assertEqual(session["state"], "LOGIN_PENDING")
            self.assertEqual(session["kind"], "app")
            self.assertNotIn("browser", session)
            self.assertEqual(session["window"]["pid"], self.host.child.pid)
            self.assertEqual(session["window"]["xid"], self.host.app.xid)
            self.assertEqual(session["backend"], "cdp")
            self.assertEqual(session["cdp_target_id"], "fixture-page")
            self.assertEqual(session["cdp_port"], 42031)
            self.assertEqual(session["capsule_id"], "fixture-capsule")
            registry.probe_owned_login_target(session["session_id"])
            self.assertEqual(self.host.argv, (
                str(app.site.executable), "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=42031", "--force-renderer-accessibility",
                "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            ))
            profile = registry.profile_root / "fixture-oauth"
            env = self.host.environment
            self.assertEqual(env["HOME"], pwd.getpwuid(os.geteuid()).pw_dir)
            self.assertTrue(os.access(env["BROWSER"], os.X_OK))
            self.assertEqual(self.host.verified, ("/opt/google/chrome/chrome", "google-chrome-stable"))
            url = "http://127.0.0.1:42032/oauth-fixture?state=synthetic&step=callback"
            command = ("/opt/google/chrome/chrome", f"--user-data-dir={profile}", "--", url)
            with patch.dict(os.environ, env), patch.object(sys, "argv", [env["BROWSER"], url]), \
                    patch.object(os, "execv") as execute:
                oauth_browser.main()
                execute.assert_called_once_with(command[0], command)
        finally:
            registry.vp_session_close(session["session_id"])
        self.assertEqual(self.host.child.stops, 1)
        self.assertTrue(profile.is_dir())

    def test_both_apps_launch_bind_and_route_oauth_to_provisioned_profile(self):
        for name in apps.APP_BINARIES:
            with self.subTest(app=name):
                app = self.launcher(name)
                self.assert_launch_and_oauth(self.registry(app), app)

    def test_wl0_qualifies_cdp_and_all_descendants_for_both_apps(self):
        codex = self.launcher("Codex Desktop").qualification
        antigravity = self.launcher("Antigravity 2").qualification
        self.assertEqual(codex, AppQualification("cdp", True, True))
        self.assertEqual(antigravity, AppQualification("cdp", False, True))

    def test_incomplete_qualification_and_unknown_app_fail_closed(self):
        receipt = json.loads(apps.RECEIPT.read_bytes())
        receipt["apps"]["Codex Desktop"]["cdp"]["children_inherit_socket"] = None
        path = self.home / "unqualified.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        for name, source in (("Codex Desktop", path), ("unknown", apps.RECEIPT)):
            with self.subTest(app=name), self.assertRaises(LaunchNotRunning) as raised:
                self.launcher(name, receipt=source)
            self.assertEqual(raised.exception.code, "DISPLAY_UNSUPPORTED")
            self.assertFalse(hasattr(self, "host"))

    def test_host_port_form_and_unlisted_flags_refuse_before_spawn(self):
        for flag in ("--remote-debugging-port=127.0.0.1:42031", "--remote-debugging-port=42031",
                     "--remote-debugging-address=0.0.0.0", "--user-data-dir=/fixture", "--no-sandbox"):
            with self.subTest(flag=flag), self.assertRaises(LaunchNotRunning) as raised:
                self.launcher(flags=(flag,))
            self.assertEqual(raised.exception.code, "-32602")
            self.assertFalse(hasattr(self, "host"))

    def test_app_registry_refuses_wrong_kind(self):
        app = self.launcher()
        with self.assertRaises(SessionRefused) as raised:
            self.registry(app).vp_session_open({**self.request, "kind": "browser"})
        self.assertEqual(raised.exception.code, "-32602")
        self.assertFalse(hasattr(self, "host"))

    def test_target_swap_refuses_binding_and_releases_owned_child(self):
        targets = iter(("first-page", "replacement-page"))
        registry = self.registry(self.launcher(read_target=lambda _port: next(targets)))
        with self.assertRaises(LaunchNotRunning) as raised:
            registry.vp_session_open(self.request)
        self.assertEqual(raised.exception.code, "identity_mismatch")
        self.assertEqual(self.host.child.stops, 1)
        self.assertEqual(registry.vp_session_list(), [])

    def test_probe_only_accepts_a_unique_page_under_the_owned_listener(self):
        port = Mock(spec=["send", "close"])
        worker = {"type": "worker", "targetId": "worker"}
        page = {"type": "page", "targetId": "page"}
        for targets, expected in (([worker, page], "page"), ([worker], None), ([page, page], None)):
            with self.subTest(targets=targets), patch.object(cdp, "_connect", return_value=port) as connect:
                port.reset_mock()
                port.send.return_value = {"targetInfos": targets}
                self.assertEqual(apps._page_target(42031), expected)
                connect.assert_called_once_with(42031)
                port.send.assert_called_once_with("Target.getTargets")
                port.close.assert_called_once_with()

    def test_login_probe_refuses_when_listener_changes(self):
        read_target = Mock(return_value="fixture-page")
        registry = self.registry(self.launcher(read_target=read_target))
        sid = registry.vp_session_open(self.request)["session_id"]
        self.addCleanup(registry.vp_session_close, sid)
        read_target.reset_mock()
        with patch.object(self.host, "listener", return_value="busy"), self.assertRaises(SessionRefused) as raised:
            registry.probe_owned_login_target(sid)
        self.assertEqual(raised.exception.code, "identity_mismatch")
        read_target.assert_not_called()

    def test_wrapper_refuses_non_web_urls_arguments_and_symlink_profile(self):
        root = self.home / "viewpoint" / "profiles"
        root.mkdir(parents=True)
        profile = root / "fixture-oauth"
        profile.symlink_to(self.home, target_is_directory=True)
        env = {"VP_DIRECT_OAUTH_BROWSER": "google-chrome", "VP_DIRECT_OAUTH_PROFILE": str(profile)}
        for argv in ([], ["--no-sandbox"], ["file:///fixture"], ["http://outside.example/"],
                     ["https://example.test/", "--no-sandbox"], ["https://example.test/"]):
            with self.subTest(argv=argv), patch.dict(os.environ, env), \
                    patch.object(sys, "argv", [str(apps.WRAPPER), *argv]), patch.object(os, "execv") as execute:
                self.assertEqual(oauth_browser.main(), 2)
                execute.assert_not_called()

    def test_backend_selection_requires_current_exact_broker_lock(self):
        registry = self.registry(self.launcher())
        session = registry.vp_session_open(self.request)
        sid = session["session_id"]
        scope = {"surface_id": "surface-fixture", "agent_id": "agent-fixture", "request_id": "request-fixture"}
        self.addCleanup(registry.vp_session_close, sid)
        runtime = self.home / "rt"
        runtime.mkdir()
        with Broker(runtime) as broker:
            with self.assertRaises(SessionRefused) as login:
                registry.observation_backend(sid, broker=broker, **scope, lock_id="placeholder")
            self.assertEqual(login.exception.code, "NO_LEASE")
            broker.bind(sid, {
                "schema": "vp.surface_binding/v1", "endpoint": session["cdp_target_id"],
                "surface_id": scope["surface_id"], "owner": scope["agent_id"], "purpose": "app-observe",
                "incarnation": session["incarnation"], "authority_epoch": 1, "obligations": [],
            }, lease_state="AGENT")
            session.update(state="AGENT", lease={"holder": scope["agent_id"], "authority_epoch": 1})
            with patch.object(registry, "get_session", return_value=session):
                lock_id = broker.try_acquire(scope["agent_id"], scope["surface_id"], session["incarnation"],
                                             1, scope["request_id"])
                self.assertEqual(registry.observation_backend(sid, broker=broker, **scope, lock_id=lock_id), "cdp")
                for replacements, code in (({"lock_id": None}, "NO_LEASE"),
                                           ({"lock_id": "borrowed"}, "NO_LEASE"),
                                           ({"request_id": "another-request"}, "NO_LEASE"),
                                           ({"agent_id": "another-agent"}, "NO_LEASE"),
                                           ({"operation": "act"}, "DISPLAY_UNSUPPORTED")):
                    options = {**scope, "lock_id": lock_id, **replacements}
                    with self.subTest(replacements=replacements), self.assertRaises(SessionRefused) as raised:
                        registry.observation_backend(sid, broker=broker, **options)
                    self.assertEqual(raised.exception.code, code)
                broker.release(scope["agent_id"], lock_id)
                with self.assertRaises(SessionRefused) as released:
                    registry.observation_backend(sid, broker=broker, **scope, lock_id=lock_id)
                self.assertEqual(released.exception.code, "NO_LEASE")

    def test_mutant_app_profile_flag_is_detected(self):
        app = self.launcher()
        normal_argv = apps._app_argv

        def mutated_argv(*args):
            return (*normal_argv(*args), "--user-data-dir=/fixture-isolated")

        with patch.object(apps, "_app_argv", side_effect=mutated_argv) as mutation:
            with self.assertRaisesRegex(AssertionError, "--user-data-dir=/fixture-isolated"):
                self.assert_launch_and_oauth(self.registry(app), app)
            mutation.assert_called_once()

    def assert_no_sensitive_io(self, app):
        checked = []
        opener = Path.open

        def watched_open(path, *args, **kwargs):
            if ".codex" in path.parts or path.name == "auth.json" or "profiles" in path.parts:
                checked.append("protected read")
                raise AssertionError("protected read attempted")
            return opener(path, *args, **kwargs)

        def watched_command(*args, **kwargs):
            checked.append("desktop-default command")
            raise AssertionError("desktop-default command attempted")

        with patch.object(Path, "open", watched_open), patch.object(subprocess, "run", watched_command):
            try:
                self.assert_launch_and_oauth(self.registry(app), app)
            finally:
                self.assertEqual(checked, [], "guard saw forbidden IO")

    def test_launch_and_wrapper_use_no_protected_reads_or_desktop_default_commands(self):
        self.assert_no_sensitive_io(self.launcher())

    def test_mutant_desktop_default_change_is_detected(self):
        app = self.launcher()

        def mutated_environment(_profile):
            subprocess.run(("xdg-settings", "set", "default-web-browser", "fixture.desktop"), check=True)

        with patch.object(app, "_environment", side_effect=mutated_environment) as mutation:
            with self.assertRaisesRegex(AssertionError, "desktop-default command"):
                self.assert_no_sensitive_io(app)
            mutation.assert_called_once()

    def test_mutant_auth_read_is_detected(self):
        app = self.launcher()

        def mutated_environment(_profile):
            (self.home / ".codex" / "auth.json").read_text(encoding="utf-8")

        with patch.object(app, "_environment", side_effect=mutated_environment) as mutation:
            with self.assertRaisesRegex(AssertionError, "protected read"):
                self.assert_no_sensitive_io(app)
            mutation.assert_called_once()
