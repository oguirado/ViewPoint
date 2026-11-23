"""Exercise display lifetimes with simulated X11 and real owned child processes."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acceptance
import driver
import runtime


class PrivateDisplayStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.sockets = self.directory / ".X11-unix"
        self.sockets.mkdir()
        self.inventory = self.directory / "unix"
        self.inventory.touch()
        self.work = self.directory / "work"
        self.work.mkdir()
        self.enterContext(patch.multiple(runtime, WORK=self.work, SHM=self.directory,
                                         X_SOCKET_DIR=self.sockets, X11_INVENTORY=self.inventory))
        self.real_child = runtime.Child
        self.reap = runtime.Child.close
        self.children = {}
        self.servers = {}
        self.failing_wms = set()
        self.authorities = set()
        self.commands = []
        self.supporting_window = b"0x21"

    def simulate_desktop(self):
        readlink = os.readlink

        def executable(path):
            if path in {Path(f"/proc/{pid}/exe") for pid in self.servers.values()}:
                return "/usr/bin/Xvfb"
            return readlink(path)

        for directory in ("run", "config", "cache", "data"):
            (self.work / directory).mkdir()
        self.enterContext(patch.object(runtime.os, "readlink", executable))
        self.enterContext(patch.multiple(runtime, Child=self.spawn_child, run=self.run_command))
        self.enterContext(redirect_stderr(io.StringIO()))

    def assert_clean(self, label):
        for child in self.children[label]:
            self.assertIsNotNone(child.proc.poll(), f"child still running on {label}")
            self.assertFalse(Path(f"/proc/{child.pid}").exists(), f"child still in /proc on {label}")
        display = runtime.Display(label)
        for path in (display.lock, display.socket, display.frame_dir, display.runtime_dir,
                     self.work / "config" / display.runtime_dir.name,
                     self.work / "cache" / display.runtime_dir.name,
                     self.work / "data" / display.runtime_dir.name):
            self.assertFalse(os.path.lexists(path), f"leftover {path.name}")
        self.assertNotIn(label, self.authorities)

    def spawn_child(self, argv, env, stack):
        tool, label = argv[0], env["DISPLAY"]
        early_exit = tool == "openbox" and label in self.failing_wms
        if tool == "Xvfb":
            for previous in self.children:
                if previous in self.failing_wms:
                    self.assert_clean(previous)
        snippet = "raise SystemExit(17)" if early_exit else "from time import sleep; sleep(60)"
        child = self.real_child([sys.executable, "-IB", "-c", snippet], env, stack)
        self.addCleanup(self.reap, child)
        self.children.setdefault(label, []).append(child)
        if tool == "Xvfb":
            self.servers[label] = child.pid
            display = runtime.Display(label)
            display.lock.write_text(str(child.pid))
            endpoint = stack.enter_context(socket.socket(socket.AF_UNIX))
            endpoint.bind(str(display.socket))
            if label in self.failing_wms:
                (display.frame_dir / "Xvfb_screen0").touch()
        if early_exit:
            child.proc.wait(timeout=3)
        return child

    def run_command(self, argv, *, env, input=None, timeout=5):
        self.commands.append((tuple(argv), env["DISPLAY"], timeout))
        tool, *args = argv
        output = b""
        if tool == "xauth":
            Path(env["XAUTHORITY"]).touch()
            if input:
                self.authorities.add(env["DISPLAY"])
            else:
                self.authorities.discard(env["DISPLAY"])
        elif tool == "xdpyinfo":
            output = b"authenticated"
        elif tool == "xprop":
            if args[-1] == "_NET_NUMBER_OF_DESKTOPS":
                output = b"_NET_NUMBER_OF_DESKTOPS = 2\n"
            else:
                window = b"0x21" if args[0] == "-root" else self.supporting_window
                output = b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # " + window + b"\n"
        return SimpleNamespace(stdout=output)

    def test_occupied_worker_lock_and_socket_are_skipped_before_spawn(self):
        self.simulate_desktop()
        occupied_lock = self.directory / ".X90-lock"
        occupied_lock.write_text("foreign\n")
        occupied_socket = self.sockets / "X91"
        with socket.socket(socket.AF_UNIX) as endpoint, ExitStack() as stack:
            endpoint.bind(str(occupied_socket))
            displays = runtime.PrivateDisplays([":90", ":92"])
            display = displays.start(stack, ":90")
            self.assertEqual(display.name, ":93")
            self.assertEqual(list(self.children), [":93"])
            self.assertEqual({label for _, label, _ in self.commands}, {":93"})
        self.assertEqual(occupied_lock.read_text(), "foreign\n")
        self.assertTrue(occupied_socket.is_socket())
        self.assert_clean(":93")

    def test_early_wm_exit_reaps_and_cleans_before_fresh_display(self):
        self.simulate_desktop()
        self.failing_wms.add(":90")
        displays = runtime.PrivateDisplays([":90", ":91"])
        with ExitStack() as stack:
            display = displays.start(stack, ":90", framebuffer=True, private_paths=True)
            self.assertEqual(display.name, ":92")
            self.assertTrue(display.verify(":92"))
            self.assert_clean(":90")
            second = displays.start(stack, ":91", framebuffer=True, private_paths=True)
            self.assertEqual(second.name, ":91")
        self.assert_clean(":92")
        self.assert_clean(":91")
        self.assertEqual(displays.cleanup_status(), {
            "x11_lock_removed": True, "x11_socket_removed": True,
            "x11_displays_attempted": [":90", ":92", ":91"], "x11_displays_started": [":92", ":91"],
        })

    def test_retry_exhaustion_reaps_every_owned_process(self):
        self.simulate_desktop()
        self.failing_wms = {":91", ":92", ":93"}
        displays = runtime.PrivateDisplays([":90", ":91"])
        with self.assertRaisesRegex(runtime.Refused, "startup exhausted.*Openbox exited"), ExitStack() as stack:
            displays.start(stack, ":90", framebuffer=True, private_paths=True)
            displays.start(stack, ":91", framebuffer=True, private_paths=True)
        self.assertEqual(list(self.children), [":90", ":91", ":92", ":93"])
        for name, children in self.children.items():
            self.assertEqual(len(children), 2)
            self.assert_clean(name)
        self.assertEqual(displays.cleanup_status()["x11_displays_started"], [":90"])

    def test_retry_stops_if_endpoint_cleanup_cannot_be_verified(self):
        self.simulate_desktop()
        self.failing_wms.add(":90")
        displays = runtime.PrivateDisplays([":90"])
        with patch.object(runtime.Display, "remove_endpoints", side_effect=runtime.Refused("endpoint changed")):
            with ExitStack() as stack, self.assertRaisesRegex(runtime.Refused, "endpoint changed"):
                displays.start(stack, ":90", framebuffer=True, private_paths=True)
        self.assertEqual(list(self.children), [":90"])
        self.assertTrue(all(child.proc.poll() is not None for child in self.children[":90"]))

    def test_wm_ready_times_out_without_a_self_referential_supporting_window(self):
        self.simulate_desktop()
        self.supporting_window = b"0x22"
        display = runtime.Display(":90")
        with patch.object(runtime, "WM_START_TIMEOUT", 0.2):
            with ExitStack() as stack, self.assertRaisesRegex(runtime.DisplayStartupError, "did not become ready"):
                display.start(stack)
        probes = [(argv, timeout) for argv, _, timeout in self.commands if argv[0] in {"xprop", "wmctrl"}]
        self.assertTrue(probes)
        self.assertTrue(all(argv[0] == "xprop" and 0 < timeout <= 0.2 for argv, timeout in probes))
        self.assert_clean(":90")

    def test_retry_oracle_catches_mutant_that_leaves_x11_endpoints(self):
        self.simulate_desktop()
        self.failing_wms.add(":90")
        displays = runtime.PrivateDisplays([":90"])
        with patch.object(runtime.Display, "remove_endpoints", return_value=None):
            with ExitStack() as stack, self.assertRaisesRegex(AssertionError, "leftover .X90-lock"):
                displays.start(stack, ":90", framebuffer=True, private_paths=True)

    def test_retained_framebuffer_after_ready_is_removed_and_refused(self):
        display = runtime.Display(":90")
        display.ready = True
        display.frame_dir.mkdir()
        (display.frame_dir / "Xvfb_screen0").touch()
        with self.assertRaisesRegex(runtime.Refused, "left framebuffer behind"):
            display.remove_framebuffer()
        self.assertFalse(display.frame_dir.exists())

    def test_worker_normalizes_exit_status_and_checks_real_endpoints(self):
        read_text = Path.read_text
        proc = {Path("/proc/1/comm"): "bwrap\n", Path("/proc/net/dev"): "header\nheader\nlo: 0\n",
                Path("/proc/net/route"): "header\n"}

        def read(path, *args, **kwargs):
            return proc[path] if path in proc else read_text(path, *args, **kwargs)

        args = SimpleNamespace(check="accept/t1/LIVE_t1.py", display=":90", fixture_display=[":90", ":91"])
        (self.directory / ".X90-lock").touch()
        self.inventory.write_text(f"0: 0 0 00010000 0001 01 0 @{self.sockets}/X91\n")
        for code, expected in ((None, 0), (7, 7), ("failure message", 1)):
            with self.subTest(code=code), TemporaryDirectory() as work, patch.dict(os.environ, {}, clear=True), \
                    patch.object(Path, "read_text", read), patch.object(driver, "WORK", Path(work)), \
                    patch.object(driver, "acceptance_args", return_value=2), patch.object(driver, "move_private_worker"), \
                    patch.object(driver, "ports", create=True), \
                    patch.object(acceptance, "run_acceptance", side_effect=SystemExit(code)), \
                    redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                result = driver.worker(args)
                self.assertIs(type(result), int)
                self.assertEqual(result, expected)
                self.assertEqual(json.loads(stdout.getvalue()), {
                    "x11_lock_removed": False, "x11_socket_removed": False,
                    "x11_displays_attempted": [], "x11_displays_started": [],
                })
                self.assertEqual(stderr.getvalue(), "failure message\n" if expected == 1 else "")
