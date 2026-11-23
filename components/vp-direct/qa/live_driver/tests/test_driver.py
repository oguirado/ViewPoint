from contextlib import nullcontext
import io
import json
from pathlib import Path
import runpy
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver
import ports
import runtime
import semantic
from PIL import Image
from privacy import PrivacyState
from vadapt import xfb


class DisplayTests(unittest.TestCase):
    def test_ambient_refused_before_socket_read(self):
        for value in (":0", ":0.0", "unix:0", "localhost:0.0", ":91"):
            with self.subTest(value=value), self.assertRaisesRegex(runtime.Refused, "ambient"):
                runtime.choose_display(None, value, unix=Path("/absent"))

    def test_requested_zero_and_nonlocal_refused(self):
        for value in (":0", ":0.0", "xpra:0", "remote:90", ":89", ":0090"):
            with self.subTest(value=value), self.assertRaises(runtime.Refused):
                runtime.display_number(value)
        self.assertEqual(runtime.display_number(":90"), 90)

    def test_occupied_display_is_skipped_or_refused_without_contact(self):
        with TemporaryDirectory() as directory:
            tmp = Path(directory)
            (tmp / ".X90-lock").write_text("foreign process\n")
            (tmp / ".X11-unix").mkdir()
            unix = tmp / "unix"
            unix.write_text(f"0: 0 0 00010000 0001 01 0 @{tmp}/.X11-unix/X91\n")
            self.assertEqual(runtime.choose_display(None, None, tmp=tmp, unix=unix), ":92")
            for value in (":90", ":91"):
                with self.subTest(value=value), self.assertRaisesRegex(runtime.Refused, "occupied"):
                    runtime.choose_display(value, None, tmp=tmp, unix=unix)

    def test_server_replacement_and_xpra_alias_refuse_before_connect(self):
        display = runtime.Display(":91")
        display.endpoint = (2, 3)
        with patch.object(display, "probe_server", return_value=(2, 4)), patch("runtime.run") as run:
            self.assertFalse(display.verify(":91"))
            self.assertFalse(display.verify("xpra:91"))
            with self.assertRaisesRegex(runtime.Refused, "X connection refused"):
                display.command("xdpyinfo")
            run.assert_not_called()

        with patch.object(display, "probe_server", return_value=(2, 3)):
            self.assertTrue(display.verify(":91"))
            self.assertFalse(display.verify("xpra:91"))
            display.xpra = SimpleNamespace(live=Mock(return_value=True))
            display.xpra_socket, display.xpra_endpoint = Path("/fake/xpra"), (2, 9)
            with patch("runtime.socket_identity", return_value=(2, 9)):
                self.assertTrue(display.verify("xpra:91"))
            with patch("runtime.socket_identity", return_value=(2, 10)):
                self.assertFalse(display.verify("xpra:91"))

    def test_sandbox_plan_removes_ambient_paths(self):
        cmd = driver.sandbox(Path("/fake/qd-runtime"), ["smoke"], None)
        for separation in ("--unshare-user", "--unshare-net", "--unshare-pid",
                           "--unshare-ipc", "--clearenv", "--die-with-parent"):
            self.assertIn(separation, cmd)
        self.assertIn("/work/run", cmd)
        self.assertNotIn("DISPLAY", cmd)
        self.assertNotIn("XAUTHORITY", cmd)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", cmd)
        self.assertNotIn(str(Path.home()), cmd)

    def test_private_atspi_bus_requires_owned_launcher_enabled_status_and_registry(self):
        session_address = "unix:path=/work/session-bus"
        atspi_address = "unix:path=/work/run/at-spi/bus"
        for failure in (None, "foreign launcher", "disabled status", "missing registry"):
            with self.subTest(failure=failure), patch.dict(runtime.os.environ, {}, clear=True):
                display = runtime.Display(":90")
                session = Mock(pid=42)
                launcher = Mock(pid=43)
                enabled = False
                calls = []

                def gdbus(argv, *, env):
                    nonlocal enabled
                    self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], session_address)
                    self.assertNotIn("AT_SPI_BUS_ADDRESS", env)
                    method, *args = argv[argv.index("--method") + 1:]
                    calls.append(method)
                    if method == "org.freedesktop.DBus.Peer.Ping":
                        self.assertEqual(argv[3], atspi_address)
                        self.assertIn("org.a11y.atspi.Registry", argv)
                        if failure == "missing registry":
                            raise subprocess.CalledProcessError(1, argv)
                        return Mock(stdout=b"()\n")
                    self.assertEqual(argv[3], session_address)
                    if method == "org.freedesktop.DBus.GetConnectionUnixProcessID":
                        self.assertEqual(args, ["org.a11y.Bus"])
                        pid = 44 if failure == "foreign launcher" else launcher.pid
                        return Mock(stdout=f"(uint32 {pid},)\n".encode())
                    if method == "org.a11y.Bus.GetAddress":
                        return Mock(stdout=f"('{atspi_address}',)\n".encode())
                    if method == "org.freedesktop.DBus.Properties.Set":
                        self.assertEqual(args, ["org.a11y.Status", "IsEnabled", "<true>"])
                        enabled = failure != "disabled status"
                        return Mock(stdout=b"()\n")
                    self.assertEqual(method, "org.freedesktop.DBus.Properties.Get")
                    self.assertEqual(args, ["org.a11y.Status", "IsEnabled"])
                    return Mock(stdout=b"(<true>,)\n" if enabled else b"(<false>,)\n")

                with patch.object(display, "verify", return_value=True), \
                     patch.object(Path, "is_socket", return_value=True), \
                     patch.object(runtime, "Child", side_effect=[session, launcher]), \
                     patch.object(runtime, "run", side_effect=gdbus):
                    if failure is None:
                        display.start_bus(Mock())
                        self.assertEqual(display.env["AT_SPI_BUS_ADDRESS"], atspi_address)
                    else:
                        reason = {
                            "foreign launcher": "owned launcher",
                            "disabled status": "could not be enabled",
                            "missing registry": "non-zero exit",
                        }[failure]
                        with self.assertRaisesRegex((runtime.Refused, subprocess.CalledProcessError), reason):
                            display.start_bus(Mock())
                        self.assertNotIn("AT_SPI_BUS_ADDRESS", display.env)

                expected = [
                    "org.freedesktop.DBus.GetConnectionUnixProcessID",
                    "org.a11y.Bus.GetAddress",
                    "org.freedesktop.DBus.Properties.Set",
                    "org.freedesktop.DBus.Properties.Get",
                    "org.freedesktop.DBus.Peer.Ping",
                ]
                limit = {"foreign launcher": 1, "disabled status": 4}.get(failure, len(expected))
                self.assertEqual(calls, expected[:limit])


class FramebufferTests(unittest.TestCase):
    def test_xwd_budget_rejects_rgb_limit_and_oversize_mutants(self):
        width, height, stride = 1920, 1080, 1920 * 4
        header_size, colors = 160, 256
        fields = [0] * xfb.HEADER_FIELDS
        fields[:6] = header_size, xfb.XWD_VERSION, xfb.ZPIXMAP, 24, width, height
        fields[11:17] = 32, stride, xfb.TRUECOLOR, 0xFF0000, 0xFF00, 0xFF
        fields[19] = colors
        raw_size = header_size + colors * xfb.COLOR_ENTRY + stride * height
        rgb_limit = width * height * 3
        with TemporaryFile() as frame:
            frame.write(struct.pack(f">{xfb.HEADER_FIELDS}I", *fields))
            frame.flush()
            frame.truncate(raw_size)
            captured = xfb.read_screen_fd(frame.fileno(), max_bytes=runtime.MAX_XWD_BYTES,
                                          width=width, height=height)
            self.assertEqual(len(captured), raw_size)
            with self.assertRaisesRegex(xfb.XwdError, "E_XWD_LIMIT"):
                xfb.read_screen_fd(frame.fileno(), max_bytes=rgb_limit, width=width, height=height)
            frame.truncate(runtime.MAX_XWD_BYTES + 1)
            with self.assertRaisesRegex(xfb.XwdError, "E_XWD_LIMIT"):
                xfb.read_screen_fd(frame.fileno(), max_bytes=runtime.MAX_XWD_BYTES, width=width, height=height)

    def test_perception_dispatch_supplies_raw_xwd_budget(self):
        args_by_kind = {
            "stream": "--max-frame-bytes", "demand": "--max-frame-bytes", "pool": "--max-xwd-bytes",
            "lifecycle": "--max-frame-bytes", "receipts": "--max-frame-bytes",
        }
        fixture = Mock(profile=Path("/work/test-browser"))
        with TemporaryDirectory() as work, patch.object(ports, "WORK", Path(work)), \
             patch.object(ports, "fixture_site", return_value=nullcontext(6100)), \
             patch.object(ports, "open_browser", return_value=nullcontext(fixture)), \
             patch.object(ports, "binding_for", return_value={"surface_id": "qd-test"}), \
             patch.object(ports, "session_for", return_value={}), patch.object(driver, "enter_runner"), \
             patch.object(ports, "call_entry") as call:
            for script, kind in driver.CHECKS.items():
                if kind in args_by_kind:
                    with self.subTest(script=script):
                        ports.run_check(script, kind, Mock(), "cdp")
                        entry, args = call.call_args.args
                        self.assertEqual(entry, script)
                        self.assertEqual(args[args.index(args_by_kind[kind]) + 1], runtime.MAX_XWD_BYTES)
            self.assertEqual(call.call_count, len(args_by_kind))


class EndpointTests(unittest.TestCase):
    def test_refuses_non_fixture_urls_without_http(self):
        for value in ("https://127.0.0.1:6000/timer.html", "http://localhost:6000/timer.html",
                      "http://127.0.0.1:6000/timer.html?x=1", "http://127.0.0.1:6000/%74imer.html",
                      "http://127.0.0.1:6000@example.invalid/timer.html", "http://127.0.0.1:6000/../timer.html",
                      "http://127.0.0.1:6000/unknown.html", "http://127.0.0.1:65536/timer.html"):
            with self.subTest(value=value), patch("runtime.get_http") as get:
                with self.assertRaises(runtime.Refused):
                    runtime.verify_fixture(value)
                get.assert_not_called()
        self.assertEqual(runtime.loopback_url("http://127.0.0.1:6000/timer.html"), (6000, "/timer.html"))

    def test_fixture_bytes_witness_rejects_same_length_substitution(self):
        url = "http://127.0.0.1:6100/timer.html"
        actual = (runtime.SITE / "timer.html").read_bytes()
        with patch("runtime.get_http", return_value=actual):
            self.assertEqual(len(runtime.verify_fixture(url)), 64)
        mutant = bytes([actual[0] ^ 1]) + actual[1:]
        with patch("runtime.get_http", return_value=mutant):
            with self.assertRaisesRegex(runtime.Refused, "fixture bytes changed"):
                runtime.verify_fixture(url)

    def test_listener_requires_unique_loopback_owner(self):
        def row(endpoint, pid):
            return f'LISTEN 0 2 {endpoint}:6100 0.0.0.0:* users:(("chrome",pid={pid},fd=42))\n'

        for result, expected in (
            (row("127.0.0.1", 8), True), (row("0.0.0.0", 8), False),
            (row("127.0.0.1", 9), False), (row("127.0.0.1", 8) + row("0.0.0.0", 9), False),
            (row("127.0.0.1", 8).strip() + ',("other",pid=9,fd=43)\n', False),
        ):
            with self.subTest(expected=expected, result=result), patch(
                "runtime.run", return_value=SimpleNamespace(stdout=result.encode())
            ):
                self.assertEqual(runtime.listener_owned(6100, 8), expected)

    def test_window_witness_checks_xid_and_rejects_rebound_or_ambiguous_pid(self):
        browser = runtime.Browser.__new__(runtime.Browser)
        browser.child = SimpleNamespace(pid=18)
        browser.display = Mock()

        def xprop(*argv):
            if argv[1] == "-root":
                return b"_NET_CLIENT_LIST: window id # 0x200001, 0x200002"
            pid = pids[argv[2]]
            return f"_NET_WM_PID = {pid}\n_NET_WM_WINDOW_TYPE = _NET_WM_WINDOW_TYPE_NORMAL\n".encode()

        browser.display.command.side_effect = xprop
        pids = {"0x200001": 12, "0x200002": 18}
        self.assertEqual(browser.find_window(), "0x200002")
        pids["0x200001"] = 18
        with self.assertRaisesRegex(runtime.Refused, "ambiguous"):
            browser.find_window()
        pids.update({"0x200001": 13, "0x200002": 12})
        self.assertIsNone(browser.find_window())

    def test_retired_or_navigated_target_refuses(self):
        browser = SimpleNamespace(url="http://127.0.0.1:6100/timer.html")
        page = runtime.Page(browser, "ws://127.0.0.1:6100/devtools/page/T", "T")
        page.document = "frame", "loader"
        frame = dict(id="frame", loaderId="loader", url=browser.url)
        tree = {"frame": frame}
        with patch.object(page, "call", return_value={"frameTree": tree}):
            self.assertEqual(page.frame(), page.document)
            page.changed = True
            with self.assertRaisesRegex(runtime.Refused, "navigation detected"):
                page.frame()
            page.changed = False
            tree["childFrames"] = [{"frame": frame}]
            with self.assertRaisesRegex(runtime.Refused, "subframe"):
                page.frame()
            del tree["childFrames"]
            frame["loaderId"] = "replacement"
            with self.assertRaisesRegex(runtime.Refused, "loader changed"):
                page.frame()

    def test_retires_document_even_if_navigation_returns_to_same_url(self):
        browser = SimpleNamespace(url="http://127.0.0.1:6100/timer.html")
        page = runtime.Page(browser, "ws://127.0.0.1:6100/devtools/page/T", "T")
        page.document = "frame", "loader"
        page.wire = Mock()
        page.wire.recv.side_effect = [
            '{"method":"Page.frameNavigated","params":{"frame":{"url":"about:blank"}}}',
            '{"id":1,"result":{"frameTree":{"frame":{"url":"http://127.0.0.1:6100/timer.html",'
            '"id":"frame","loaderId":"loader"}}}}',
        ]
        with self.assertRaisesRegex(runtime.Refused, "navigation detected"):
            page.frame()


class FrameTests(unittest.TestCase):
    @staticmethod
    def browser(page):
        return SimpleNamespace(
            url=f"http://127.0.0.1:6400/{page}.html", incarnation="synthetic", witness="window-witness",
            target={"id": "target-qd"}, xid="0x2a", check=Mock(), page=Mock(),
            display=SimpleNamespace(label="xpra:90", hidden=Mock(return_value=False)),
            geometry=Mock(return_value=((0, 0, 4, 4), (4, 4))),
        )

    def test_login_is_blocked_before_any_page_or_raster_read(self):
        browser = self.browser("login")
        frame = ports.FramePorts(browser)
        state = frame.current()
        self.assertIs(frame.metadata(state.privacy_binding()).state, PrivacyState.LOGIN)
        with self.assertRaisesRegex(runtime.Refused, "does not authorize"):
            frame.acquire(state)
        browser.page.read_bool.assert_not_called()
        browser.page.call.assert_not_called()
        browser.page.screenshot.assert_not_called()

    def test_privacy_mutation_latches_through_a_reversed_edit(self):
        browser = self.browser("timer")
        browser.page.read_bool.side_effect = [True, False, True, True]
        frame = ports.FramePorts(browser)
        state = frame.current()
        first = frame.metadata(state.privacy_binding())
        after = frame.metadata(state.privacy_binding())
        self.assertEqual(first, after)
        self.assertIs(after.state, PrivacyState.UNKNOWN)
        self.assertEqual(after.revision, 1)
        with self.assertRaises(runtime.Refused):
            frame.acquire(state)
        browser.page.screenshot.assert_not_called()

    def test_acquire_revalidates_geometry_and_refuses_stale_state(self):
        browser = self.browser("timer")
        frame = ports.FramePorts(browser)
        state = frame.current()
        browser.geometry.return_value = (1, 0, 4, 4), (4, 4)
        with self.assertRaisesRegex(runtime.Refused, "geometry or workspace changed"):
            frame.acquire(state)
        browser.page.screenshot.assert_not_called()

    def test_capture_returns_real_png_with_ax_and_rejects_flat_or_wrong_size(self):
        from frames.source import FrameUnavailable, inspect_png

        browser = self.browser("timer")
        browser.page.call.return_value = {"nodes": [{"nodeId": "1"}]}
        frame = ports.FramePorts(browser)
        state = frame.current()

        def png(size, speckle):
            image = Image.new("RGB", size, "white")
            if speckle:
                image.putpixel((0, 0), (15, 16, 17))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

        browser.page.screenshot.return_value = png((4, 4), True)
        raster = frame.acquire(state)
        self.assertEqual(raster.state, state)
        self.assertEqual(raster.kind, "cdp_png")
        self.assertEqual(len(raster.ax_sha256), 64)
        self.assertEqual(len(inspect_png(raster.png, (4, 4))), 64)
        for data in (png((4, 4), False), png((3, 3), True)):
            browser.page.screenshot.return_value = data
            with self.assertRaises(FrameUnavailable):
                frame.acquire(state)

    def test_identical_capture_mutant_fails_distinctness_oracle(self):
        ports.distinct_pixels("a" * 64, "b" * 64)
        with self.assertRaisesRegex(runtime.Refused, "distinct"):
            ports.distinct_pixels("a" * 64, "a" * 64)


class CustodyTests(unittest.TestCase):
    def test_browser_lifecycle_uses_existing_tab_and_owned_child_cleanup(self):
        port = 6100
        url = f"http://127.0.0.1:{port}/timer.html"
        page_address = f"ws://127.0.0.1:{port}/devtools/page/T"
        browser_address = f"ws://127.0.0.1:{port}/devtools/browser/B"
        display = Mock(label=":90", endpoint=(2, 3), env={})
        display.name = ":90"
        display.server = SimpleNamespace(pid=9, start=10)
        child = Mock(pid=18, start=20)
        frame_tree = {"frameTree": {"frame": {"id": "frame", "loaderId": "loader", "url": url}}}
        results = {
            "Page.enable": {},
            "Page.getFrameTree": frame_tree,
            "Runtime.evaluate": {"result": {"value": True}},
            "Browser.getWindowForTarget": {"windowId": 7},
        }
        page_wire, browser_wire, connection = Mock(), Mock(), Mock()
        http_results = {
            "/json/list": [{"type": "page", "url": url, "id": "T", "webSocketDebuggerUrl": page_address}],
            "/json/version": {"webSocketDebuggerUrl": browser_address},
            "/json/new": {},
        }
        wires = {page_address: page_wire, browser_address: browser_wire}

        def reply():
            message = json.loads(page_wire.send.call_args.args[0])
            return json.dumps({"id": message["id"], "result": results[message["method"]]})

        def response_body(_limit):
            path = connection.request.call_args.args[1].split("?", 1)[0]
            return json.dumps(http_results[path]).encode()

        page_wire.recv.side_effect = reply
        connection.getresponse.return_value = Mock(status=200)
        connection.getresponse.return_value.read.side_effect = response_body

        with TemporaryDirectory() as work, patch.object(runtime, "WORK", Path(work)), \
             patch.object(runtime, "verify_fixture", return_value="fixture"), \
             patch.object(runtime.Browser, "free_port", return_value=port), \
             patch.object(runtime.Browser, "find_window", return_value="0x123"), \
             patch.object(runtime, "listener_owned", return_value=True), \
             patch.object(runtime, "HTTPConnection", return_value=connection), \
             patch.object(runtime, "BrowserProcess", return_value=child) as spawn, \
             patch("websocket.create_connection", side_effect=lambda address, **_: wires[address]) as connect:
            with runtime.open_browser(display, url) as browser:
                self.assertEqual(browser.target["id"], "T")
                self.assertEqual(spawn.call_args.args[0][-2:], ["--new-window", url])
                child.identify.assert_called_once_with()
                child.close.assert_not_called()
            self.assertFalse(browser.profile.exists())

        requests = [call.args for call in connection.request.call_args_list]
        commands = [json.loads(call.args[0])["method"] for wire in (page_wire, browser_wire)
                    for call in wire.send.call_args_list]
        self.assertNotIn("Browser.close", commands)
        self.assertEqual(requests, [("GET", "/json/list")])
        self.assertEqual([call.args[0] for call in connect.call_args_list], [page_address])
        page_wire.close.assert_called_once_with(timeout=0.2)
        child.close.assert_called_once_with()

    def test_default_discovery_includes_worker_import_probe_and_mutant(self):
        suite = unittest.TestLoader().discover(str(Path(__file__).resolve().parent))
        pending = list(suite)
        discovered = set()
        while pending:
            test = pending.pop()
            if isinstance(test, unittest.TestSuite):
                pending.extend(test)
            else:
                discovered.add(test.id())
        for method in ("test_worker_imports_system_numpy_and_wender",
                       "test_worker_import_fails_without_each_native_alternative"):
            self.assertIn(f"test_worker_imports.WorkerImportSmoke.{method}", discovered)

    def test_cleanup_only_signals_unreaped_recorded_child(self):
        child = runtime.Child.__new__(runtime.Child)
        child.proc = Mock(pid=12002)
        child.proc.poll.return_value = None
        child.proc.wait.side_effect = [subprocess.TimeoutExpired("own-child", 3), 0]
        with patch("os.kill") as direct_kill:
            child.close()
        direct_kill.assert_not_called()
        child.proc.terminate.assert_called_once_with()
        child.proc.kill.assert_called_once_with()
        child.proc.reset_mock()
        child.proc.poll.return_value = 0
        child.close()
        child.proc.terminate.assert_not_called()
        child.proc.kill.assert_not_called()

    def test_private_session_and_demand_validate_against_frozen_schemas(self):
        from schemas.validation import validate_contract

        browser = FrameTests.browser("timer")
        browser.display.verify = Mock(return_value=True)
        browser.child, browser.port = SimpleNamespace(pid=33), 6100
        for backend in ("cdp", "atspi"):
            with self.subTest(backend=backend):
                session = ports.session_for(browser, backend)
                validate_contract("vp.session/v1", session)
                self.assertEqual(session["display"], "xpra:90")
        binding = ports.binding_for(browser)
        validate_contract("vp.surface_binding/v1", binding)
        validate_contract("vp.demand/v1", ports.demand_for(binding, "agent"))
        browser.display.label = ":90"
        with self.assertRaisesRegex(runtime.Refused, "Xpra"):
            ports.session_for(browser)

    def test_driver_loaded_with_runpy_uses_the_active_owned_runtime(self):
        hooks = runpy.run_path(driver.__file__)
        self.assertFalse(hooks["verify_private_display"](":90"))
        fake = Mock()
        fake.display.verify.side_effect = lambda s: s == ":90"
        with patch.dict(sys.modules, {"_vp_qd_invocation": SimpleNamespace(ports=fake)}):
            self.assertTrue(hooks["verify_private_display"](":90"))
            self.assertFalse(hooks["verify_private_display"](":0"))
            self.assertIs(hooks["open_login_fixture"]("test-url", ":90"), fake.open_login_fixture.return_value)
            self.assertIs(hooks["prepare"](), fake.prepare_attachment.return_value)
        with self.assertRaisesRegex(runtime.Refused, "multi-combination"):
            hooks["prepare"]({"fake": "T2"})

    def test_check_dispatch_preserves_real_entry_points_and_arguments(self):
        fake = MagicMock()
        fake.display.label = "xpra:92"
        with patch.object(ports, "call_entry") as call:
            for script, args in {
                "privacy/tests/LIVE_login_fixture.py": ["--driver", ports.DRIVER, "--display", "xpra:92"],
                "frames/tests/LIVE_hidden_fixture.py": ["--driver", ports.DRIVER, "--display", "xpra:92"],
                "vision/tests/LIVE_vision_fixture.py": ["--driver", ports.DRIVER, "--display", "xpra:92"],
                "channel/core/LIVE_channel.py": [ports.DRIVER, "--backend", "atspi"],
                "attach/live_check.py": ["--driver", ports.DRIVER],
            }.items():
                ports.run_check(script, driver.CHECKS[script], fake, "atspi")
                call.assert_called_with(script, args)

    def test_missing_h1_refuses_before_display_or_browser_start(self):
        args = SimpleNamespace(check="recorder/timeline/tests/LIVE_record_fixture.py", display=None)
        with patch.object(driver, "choose_display", return_value=":90"), patch.object(driver, "require_scope"), \
             patch.object(driver.subprocess, "Popen") as process:
            with self.assertRaisesRegex(runtime.Refused, "H1 DemoEvent"):
                driver.launch(args)
            process.assert_not_called()
        hooks = runpy.run_path(driver.__file__)
        with self.assertRaisesRegex(runtime.Refused, "workflow transport"):
            hooks["replay"]("fixture", None, dry_run=True)

    def test_semantic_selector_rejects_duplicate_checkbox(self):
        checkbox = {"role": "checkbox", "name": ports.CHECKBOX}
        self.assertEqual(semantic.fixture_checkbox([checkbox]), checkbox)
        for candidates in ([], [checkbox, checkbox]):
            with self.assertRaisesRegex(runtime.Refused, "ambiguous"):
                semantic.fixture_checkbox(candidates)

    def test_cdp_observe_reply_keeps_observation_id_in_action_binding(self):
        from jsonschema.exceptions import ValidationError
        from schemas.validation import validate_contract

        fixture = semantic.SemanticFixture.__new__(semantic.SemanticFixture)
        fixture.backend = "cdp"
        fixture.session_id = "fixture-session"
        fixture.session = {"cdp_target_id": "fixture-target", "capsule_id": "fixture-capsule",
                           "incarnation": "fixture-incarnation"}
        fixture.act_request_id = None
        fixture.executor = Mock()
        snapshot = SimpleNamespace(id="fixture-observation", ax_sha256="a" * 64, ax_stable_sha256="b" * 64,
                                   nodes=[{"role": "checkbox", "name": ports.CHECKBOX,
                                           "id": "checkbox", "attrs": {"checked": "false"}}])
        fixture.executor.observe.return_value = snapshot
        reply = fixture.cdp(fixture.request_for("observe"), None)

        validate_contract("vp.channel.reply/v1", reply)
        self.assertEqual(reply["ax_sha256"], snapshot.ax_sha256)
        self.assertEqual(reply["ax_stable_sha256"], snapshot.ax_stable_sha256)
        target = SimpleNamespace(surface_id="fixture-surface")
        click = fixture.request_for("act")
        observation = fixture.observation_for(click, target)
        self.assertEqual(click["observation_id"], observation.observation_id)
        self.assertEqual(click["target"]["ax_stable_sha256"], observation.ax_stable_sha256)
        self.assertEqual(observation.node_matches, ("checkbox",))

        # Mutant: expose the request-only observation ID in the reply envelope.
        with self.assertRaisesRegex(ValidationError, "observation_id"):
            validate_contract("vp.channel.reply/v1", dict(reply, observation_id=snapshot.id))


if __name__ == "__main__":
    unittest.main()
