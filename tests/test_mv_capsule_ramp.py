"""Exercise ramp contracts without a live ViewPoint package."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/mv_capsule_ramp.py"
sys.path.insert(0, str(TOOL.parent))
import mv_capsule_ramp as ramp


class RampTests(unittest.TestCase):
    def test_reservation_must_fit_above_the_floor(self):
        floor, reserve = ramp.MEMORY_FLOOR_KIB, ramp.SESSION_RESERVATION_KIB
        self.assertEqual(ramp.admit(floor + 2 * reserve, reserve), 2 * reserve)
        for available, outstanding in ((floor - 1, 0), (floor + reserve - 1, 0),
                                         (floor + 2 * reserve - 1, reserve)):
            with self.subTest(available=available, outstanding=outstanding):
                with self.assertRaises(ramp.Refused) as error:
                    ramp.admit(available, outstanding)
                self.assertEqual(error.exception.code, "memory_floor")
        for value in ("MemFree: 99 kB", "MemAvailable: 99 MB",
                      "MemAvailable: 99 kB\nMemAvailable: 1 kB"):
            with self.assertRaises(ramp.Refused):
                ramp.mem_available(value)

    def test_state_round_trip_and_owner_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ramp.STATE_NAME
            with ramp.locked_root(path.parent) as fd:
                state = ramp.empty_state(fd)
                ramp.write_state(fd, state)
                self.assertEqual(ramp.read_state(fd), state)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.chmod(0o644)
                with self.assertRaisesRegex(ramp.Refused, "private"):
                    ramp.read_state(fd)

    def test_status_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ramp.STATE_NAME
            with ramp.locked_root(path.parent) as fd:
                ramp.write_state(fd, ramp.empty_state(fd))
            result = subprocess.run([sys.executable, "-B", str(TOOL),
                                     "status", "--state", str(path)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["sessions"], [])
            path.unlink()
            result = subprocess.run(result.args, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(result.stdout)["ok"])

    def test_scope_and_display_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["open", "--n", "2", "--scope-unit", "ramp.scope",
                    "--profile-root", directory, "--display"]
            for display, reason in ((":89", "display"),
                                    (":90", "scope")):
                with mock.patch.object(ramp, "verify_scope",
                                       side_effect=ramp.Refused("scope", "unverified scope")) as scope:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(ramp.main([*args, display]), 3)
                    self.assertEqual(json.loads(output.getvalue())["reason"], reason)
                    self.assertEqual(scope.called, display == ":90")

    def test_open_refuses_memory_shortfall_before_registry_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            args = ["open", "--n", "1", "--scope-unit", "ramp.scope",
                    "--profile-root", directory, "--display", ":90"]
            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib", return_value=ramp.MEMORY_FLOOR_KIB),
                  mock.patch.object(ramp.m7.setup, "registry_path") as registry,
                  contextlib.redirect_stdout(output)):
                self.assertEqual(ramp.main(args), 3)
            registry.assert_not_called()
            response = json.loads(output.getvalue())
            self.assertEqual(response["reason"], "memory_floor")
            self.assertEqual(response["next_reservation_kib"], ramp.SESSION_RESERVATION_KIB)

    def test_open_reports_the_pinned_launcher_blockers(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["open", "--n", "2", "--scope-unit", "ramp.scope",
                    "--profile-root", directory, "--display", ":90"]
            output = io.StringIO()
            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib",
                                    return_value=ramp.MEMORY_FLOOR_KIB + ramp.SESSION_RESERVATION_KIB),
                  mock.patch.object(ramp.m7.setup, "registry_path",
                                    return_value=Path(directory) / "missing-setup.json"),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(ramp.main(args), 3)
            response = json.loads(output.getvalue())
            self.assertEqual(response["reason"], "m7_launch_unavailable")
            self.assertIn("m7_registry_missing", response["blockers"])
            self.assertIn("m7_single_held_session", response["blockers"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_close_with_fake_children_in_namespace(self):
        subprocess.run(["unshare", "-Urpf", "--mount-proc", "--", sys.executable,
                        "-B", str(Path(__file__).resolve()), "NamespaceCloseTests"],
                       check=True, timeout=15)


if __name__ == "__main__" and sys.argv[1:] == ["NamespaceCloseTests"]:
    class NamespaceCloseTests(unittest.TestCase):
        def setUp(self):
            # unshare uses the test runner as PID 1. The explicitly mapped UID
            # is zero here even when the invoking test worker is unprivileged.
            self.assertEqual((os.getpid(), os.getuid()), (1, 0))
            self.directory = tempfile.TemporaryDirectory()
            self.addCleanup(self.directory.cleanup)
            self.root = Path(self.directory.name)
            self.child = subprocess.Popen(
                [sys.executable, "-c", "import signal, time; "
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "print('ready', flush=True); time.sleep(60)"],
                start_new_session=True, stdout=subprocess.PIPE, text=True,
            )
            self.addCleanup(self.reap)
            self.assertEqual(self.child.stdout.readline(), "ready\n")
            self.session_id = "a" * 32
            self.profile = self.root / ("session-" + self.session_id)
            self.profile.mkdir(mode=0o700)
            (self.profile / "disposable").write_text("fixture")
            starttime, pgid = ramp.process_identity(self.child.pid)
            self.row = {"session_id": self.session_id, "pid": self.child.pid,
                        "starttime": starttime, "pgid": pgid,
                        "profile_inode": ramp.inode(self.profile.stat())}

        def reap(self):
            if self.child.poll() is None:
                self.child.kill()
            self.child.wait()
            self.child.stdout.close()

        def close(self, row):
            output = io.StringIO()
            with ramp.locked_root(self.root) as fd:
                state = ramp.empty_state(fd)
                state["sessions"].append(row)
                ramp.write_state(fd, state)
            with contextlib.redirect_stdout(output):
                code = ramp.main(["close", "--all-recorded", "--state",
                                  str(self.root / ramp.STATE_NAME)])
            return code, json.loads(output.getvalue())

        def assert_refused_without_effect(self, row, reason):
            with mock.patch.object(ramp.signal, "pidfd_send_signal") as send:
                code, result = self.close(row)
            send.assert_not_called()
            self.assertNotEqual(code, 0)
            self.assertEqual(result["reason"], reason)
            self.assertIsNone(self.child.poll())
            self.assertTrue(self.profile.is_dir())

        def test_starttime_mismatch_refuses_without_signal_or_deletion(self):
            row = dict(self.row, starttime=self.row["starttime"] + 1)
            self.assert_refused_without_effect(row, "process_identity")

        def test_invalid_group_refuses_without_signal_or_deletion(self):
            self.assert_refused_without_effect(dict(self.row, pgid=1), "process_group")

        def test_wrong_profile_refuses_without_signal_or_deletion(self):
            device, identity = self.row["profile_inode"]
            row = dict(self.row, profile_inode=[device, identity + 1])
            self.assert_refused_without_effect(row, "profile_identity")

        def test_term_escalates_to_kill_and_persists_close_witness(self):
            reaper = threading.Thread(target=self.child.wait, daemon=True)
            reaper.start()
            with mock.patch.object(ramp, "TERM_SECONDS", 0.05):
                code, result = self.close(self.row)
            reaper.join(timeout=3)
            self.assertEqual(code, 0, result)
            self.assertEqual(self.child.returncode, -signal.SIGKILL)
            self.assertFalse(self.profile.exists())
            self.assertFalse(Path(f"/proc/{self.child.pid}").exists())
            witness, = result["close_witnesses"]
            self.assertTrue(witness["process_absent"])
            self.assertTrue(witness["profile_removed"])
            self.assertEqual(witness["signals"], ["TERM", "KILL"])
            with ramp.locked_root(self.root) as fd:
                stored = ramp.read_state(fd)
            self.assertEqual(stored["sessions"], [])
            self.assertEqual(stored["close_witnesses"], [witness])


if __name__ == "__main__":
    if sys.argv[1:] == ["NamespaceCloseTests"]:
        unittest.main()
    else:
        unittest.main(defaultTest="RampTests")
