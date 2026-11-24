"""Exercise ramp contracts without a live ViewPoint package."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import socket
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
    def setUp(self):
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("DISPLAY", None)

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
            for display, ambient, reason in ((":89", ":90", "display"),
                                             (":90", ":0", "display"),
                                             (":90", "", "display"),
                                             (":90", ":91.0", "display"),
                                             (":90", ":90.0", "display"),
                                             (":90", ":90", "scope")):
                with (mock.patch.dict(os.environ, {"DISPLAY": ambient}),
                      mock.patch.object(ramp, "verify_scope",
                                        side_effect=ramp.Refused("scope", "unverified scope")) as scope):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(ramp.main([*args, display]), 3)
                    self.assertEqual(json.loads(output.getvalue())["reason"], reason)
                    self.assertEqual(scope.called, reason == "scope")

    def test_verify_scope_reads_membership_and_both_effective_caps(self):
        unit = "ramp.scope"
        group = "/user.slice/" + unit
        cgroup = "/proc/self/cgroup"
        high = "/sys/fs/cgroup" + group + "/memory.high"
        maximum = "/sys/fs/cgroup" + group + "/memory.max"
        files = {cgroup: "0::" + group + "\n",
                 high: str(6 * 1024**3) + "\n", maximum: str(8 * 1024**3) + "\n"}

        def read(path):
            return files[str(path)]

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read):
            ramp.verify_scope(unit)
            for path, bad in ((cgroup, "0::/wrong.scope\n"),
                              (cgroup, "2:memory:" + group + "\n"),
                              (cgroup, "0::/../" + unit + "\n"),
                              (high, "max\n"), (maximum, str(9 * 1024**3) + "\n")):
                with self.subTest(path=path, bad=bad), mock.patch.dict(files, {path: bad}):
                    with self.assertRaises(ramp.Refused) as error:
                        ramp.verify_scope(unit)
                    self.assertEqual(error.exception.code, "scope")

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

    def test_open_reports_exact_missing_operator_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["open", "--n", "2", "--scope-unit", "ramp.scope",
                    "--profile-root", directory, "--display", ":90"]
            output = io.StringIO()
            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib",
                                    return_value=ramp.MEMORY_FLOOR_KIB + 2 * ramp.SESSION_RESERVATION_KIB),
                  mock.patch.object(ramp.m7.setup, "registry_path",
                                    return_value=Path(directory) / "missing-setup.json"),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(ramp.main(args), 3)
            response = json.loads(output.getvalue())
            self.assertEqual(response["reason"], "m7_registry_missing")
            self.assertEqual(response["registry"], str(Path(directory) / "missing-setup.json"))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_transport_display_is_checked_before_m7_ui_authority(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as bus:
            transport = Path(directory) / "transport.json"
            endpoint = Path(directory) / "fixture-bus"
            bus.bind(str(endpoint))
            args = ["open", "--n", "1", "--scope-unit", "ramp.scope",
                    "--profile-root", directory, "--display", ":90",
                    "--ui-transport", str(transport)]
            descriptor = {"schema": "metaviewer-arm64-ui-transport/v1",
                          "mode": "trusted-user-invocation", "display": "", "xauthority": None,
                          "dbus_session_bus_address": f"unix:path={endpoint}"}
            transport.touch(mode=0o600)
            for display in (":0", ":89.1", ":91", "remote:90", "", ":90.1"):
                with self.subTest(display=display):
                    descriptor["display"] = display
                    transport.write_text(json.dumps(descriptor))
                    output = io.StringIO()
                    dispatch = mock.Mock(side_effect=ramp.Refused("fixture", "dispatch reached"))
                    with (mock.patch.dict(os.environ, {"DISPLAY": ":90"}),
                          mock.patch.object(ramp, "verify_scope"),
                          mock.patch.object(ramp, "available_kib", return_value=64 * ramp.GIB_KIB),
                          mock.patch.object(ramp.m7, "ui_transport", wraps=ramp.m7.ui_transport) as authority,
                          contextlib.redirect_stdout(output)):
                        self.assertEqual(ramp.main(args, launch=dispatch), 3)
                    if display == ":90.1":
                        authority.assert_called_once_with(descriptor)
                        dispatch.assert_called_once()
                        self.assertEqual(json.loads(output.getvalue())["reason"], "fixture")
                    else:
                        authority.assert_not_called()
                        dispatch.assert_not_called()
                        self.assertEqual(json.loads(output.getvalue())["reason"], "transport_display")

    def test_ramp_never_self_binds_a_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "setup.json"
            ramp.m7.setup.write_new(registry, {})
            output = io.StringIO()
            args = ["open", "--n", "1", "--scope-unit", "ramp.scope",
                    "--profile-root", directory, "--display", ":90"]
            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib", return_value=32 * ramp.GIB_KIB),
                  mock.patch.object(ramp.m7.setup, "registry_path", return_value=registry),
                  mock.patch.object(ramp.m7, "launch") as launch,
                  mock.patch.object(ramp.m7, "bind_multi_grant") as bind,
                  contextlib.redirect_stdout(output)):
                self.assertEqual(ramp.main(args), 3)
                launch.assert_not_called()
                bind.assert_not_called()
            response = json.loads(output.getvalue())
            self.assertEqual(response["reason"], "launcher_grant")
            self.assertFalse((Path(directory) / ramp.STATE_NAME).exists())

    def test_default_dispatch_checkpoints_each_m7_outer_before_releasing_it(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as folder:
            directory = Path(folder)
            row_ids = iter(("a" * 24, "b" * 24))
            pids = iter((501, 502))
            attempts = []
            value = {"python": {"path": "/fixture/python"}, "package": {"app": {"path": "/fixture/app"}},
                     "source_root": "/fixture/source", "source_commit": "a" * 40,
                     "producer_pins": {"setup": {"path": "/fixture/arm64_capsule_session.py"}}}
            backend = (value, "b" * 64, directory, 2 * ramp.SESSION_RESERVATION_KIB)

            def dispatch(argv, **options):
                self.assertIn("--multi-run-id", argv)
                self.assertNotIn("--launcher-pid", argv)
                self.assertEqual(argv[argv.index("--multi-grant") + 1], "/fixture/operator-grant")
                self.assertTrue(options["start_new_session"])
                pid = next(pids)
                attempts.append(pid)

                class Gate(io.BytesIO):
                    def write(gate, ready):
                        state = json.loads((directory / ramp.STATE_NAME).read_text())
                        self.assertEqual(state["sessions"][-1]["pid"], pid)
                        self.assertEqual(state["sessions"][-1]["pgid"], pid)
                        self.assertEqual(ready, b"1")
                        return super().write(ready)

                return mock.Mock(pid=pid, stdin=Gate())

            ready = {"ready_witness": "m7_app_bound", "app_pid": 905, "app_rss_kib": 512}
            args = ["open", "--n", "2", "--scope-unit", "ramp.scope", "--profile-root", folder,
                    "--display", ":90", "--launcher-grant", "/fixture/operator-grant"]
            output = io.StringIO()
            with (mock.patch.object(ramp, "verify_scope"), mock.patch.object(ramp, "preflight_m7", return_value=backend),
                  mock.patch.object(ramp.m7, "preflight_multi"), mock.patch.object(ramp.m7.setup, "executable"),
                  mock.patch.object(ramp.m7.setup, "pin"), mock.patch.object(ramp.secrets, "token_hex",
                      side_effect=lambda size: next(row_ids) if size == 12 else "e" * (2 * size)),
                  mock.patch.object(ramp, "available_kib", return_value=36 * ramp.GIB_KIB),
                  mock.patch.object(ramp.subprocess, "Popen", side_effect=dispatch),
                  mock.patch.object(ramp.m7, "process_identity", return_value={"start_ticks": 7, "cgroup": "/fixture"}),
                  mock.patch.object(ramp, "process_identity", side_effect=lambda pid: (7, pid)),
                  mock.patch.object(ramp, "resident_kib", return_value=64),
                  mock.patch.object(ramp, "wait_m7_ready", return_value=ready),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(ramp.main(args), 0, output.getvalue())
            self.assertEqual(attempts, [501, 502])
            self.assertEqual([row["pgid"] for row in json.loads(output.getvalue())["sessions"]], attempts)

    def test_open_reserves_the_entire_request_and_reads_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["open", "--n", "2", "--scope-unit", "ramp.scope",
                    "--profile-root", directory, "--display", ":90"]
            available = ramp.MEMORY_FLOOR_KIB + ramp.SESSION_RESERVATION_KIB
            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib", return_value=available),
                  mock.patch.object(ramp.m7.setup, "registry_path") as registry):
                for existing, reason in ((None, "memory_floor"), ("invalid", "unavailable")):
                    with self.subTest(existing=existing):
                        if existing is not None:
                            path = Path(directory) / ramp.STATE_NAME
                            path.touch(mode=0o600)
                            path.write_text(existing)
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            self.assertEqual(ramp.main(args), 3)
                        self.assertEqual(json.loads(output.getvalue())["reason"], reason)
                registry.assert_not_called()

    def test_open_checkpoints_identity_before_readiness_and_rechecks_memory(self):
        floor, reserve = ramp.MEMORY_FLOOR_KIB, ramp.SESSION_RESERVATION_KIB
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = ramp.argparse.Namespace(n=2, scope_unit="ramp.scope", profile_root=directory,
                                           display=":90", ui_transport=None)
            rows = []
            events = []

            def launch(_args):
                ident = f"{len(rows) + 1:032x}"
                profile = root / ("session-" + ident)
                profile.mkdir(mode=0o700)
                pid = max(os.getpid(), os.getpgid(0)) + len(rows) + 1
                row = {"session_id": ident, "pid": pid, "starttime": 12, "pgid": pid,
                       "profile_inode": ramp.inode(profile.stat())}
                rows.append(row)
                events.append("start")

                def ready():
                    stored = json.loads((root / ramp.STATE_NAME).read_text())
                    self.assertEqual(stored["sessions"], rows)
                    events.append("ready")
                    return "fixture_ready"

                return row, ready

            enough = floor + 2 * reserve
            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib", side_effect=[enough, enough, enough - 1]),
                  mock.patch.object(ramp, "process_identity", side_effect=lambda pid: (12, pid)),
                  mock.patch.object(ramp, "resident_kib", return_value=64)):
                with self.assertRaises(ramp.Refused) as error:
                    ramp.open_ramp(args, launch)
            self.assertEqual(error.exception.code, "memory_floor")
            self.assertEqual(error.exception.fields["outstanding_reservation_kib"], reserve)
            self.assertEqual(events, ["start", "ready"])

            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib", return_value=enough),
                  mock.patch.object(ramp, "process_identity", side_effect=lambda pid: (12, pid)),
                  mock.patch.object(ramp, "resident_kib", return_value=64)):
                with self.assertRaises(ramp.Refused) as error:
                    ramp.open_ramp(args, launch)
                self.assertEqual(error.exception.code, "memory_floor")
                self.assertEqual(error.exception.fields["outstanding_reservation_kib"], 2 * reserve)
                self.assertEqual(events, ["start", "ready"])
                output = io.StringIO()
                with (mock.patch.object(ramp, "available_kib", return_value=enough + reserve),
                      contextlib.redirect_stdout(output)):
                    code = ramp.main(["open", "--n", "2", "--scope-unit", args.scope_unit,
                                      "--profile-root", directory, "--display", args.display],
                                     launch=launch)
            response = json.loads(output.getvalue())
            self.assertEqual(code, 0, response)
            self.assertEqual(response["peak_reservation_kib"], 3 * reserve)
            self.assertEqual(response["stop_owner"], os.getuid())
            self.assertEqual(events, ["start", "ready"] * 3)
            for session, row in zip(response["sessions"], rows[1:], strict=True):
                self.assertEqual(session, {"session_id": row["session_id"], "pid": row["pid"],
                                           "starttime": 12, "pgid": row["pgid"], "rss_kib": 64,
                                           "ready_ms": session["ready_ms"], "ready_witness": "fixture_ready"})
                self.assertGreaterEqual(session["ready_ms"], 0)

    def test_open_retains_a_session_when_readiness_is_unproven(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ident = "a" * 32
            profile = root / ("session-" + ident)
            profile.mkdir(mode=0o700)
            pid = max(os.getpid(), os.getpgid(0)) + 1
            row = {"session_id": ident, "pid": pid, "starttime": 12, "pgid": pid,
                   "profile_inode": ramp.inode(profile.stat())}
            args = ramp.argparse.Namespace(n=1, scope_unit="ramp.scope", profile_root=directory,
                                           display=":90", ui_transport=None)
            with (mock.patch.object(ramp, "verify_scope"),
                  mock.patch.object(ramp, "available_kib",
                                    return_value=ramp.MEMORY_FLOOR_KIB + ramp.SESSION_RESERVATION_KIB),
                  mock.patch.object(ramp, "process_identity", return_value=(12, pid))):
                with self.assertRaises(ramp.Refused) as error:
                    ramp.open_ramp(args, lambda _: (row, lambda: None))
            self.assertEqual(error.exception.code, "readiness")
            with ramp.locked_root(root) as fd:
                self.assertEqual(ramp.read_state(fd)["sessions"], [row])
            self.assertTrue(profile.is_dir())

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
            self.assertEqual(os.readlink("/proc/self"), "1")
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
            with mock.patch.object(ramp.signal, "pidfd_send_signal",
                                   side_effect=AssertionError("refusal attempted a signal")) as send:
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
            with mock.patch.object(ramp, "process_identity", return_value=(self.row["starttime"], 1)):
                self.assert_refused_without_effect(self.row, "process_group")

        def test_own_group_leader_pid_refuses_without_signal_or_deletion(self):
            with mock.patch.object(ramp.os, "getpgid", return_value=self.child.pid):
                self.assert_refused_without_effect(self.row, "state")

        def test_wrong_profile_refuses_without_signal_or_deletion(self):
            device, identity = self.row["profile_inode"]
            row = dict(self.row, profile_inode=[device, identity + 1])
            self.assert_refused_without_effect(row, "profile_identity")

        def test_multi_close_never_signals_foreign_or_reused_pid(self):
            run_id = "b" * 24
            unit = "metaviewer-arm64-" + run_id + ".scope"
            slot = ramp.m7.multi_slot(self.root, run_id)
            slot.mkdir(mode=0o700, parents=True)
            with tempfile.TemporaryDirectory(prefix=ramp.m7.session_root_prefix(run_id), dir="/dev/shm") as directory:
                root = Path(directory)
                marker = {"root": directory, "unit": unit, "status": "closure-unproven"}
                ramp.m7.setup.write_new(slot / "reservation.json", marker)
                ramp.m7.publish_held_marker(slot, marker)
                live = ramp.m7.process_identity(self.child.pid)
                sha = "c" * 64
                custody = {"registry": str(self.root), "registry_inode": ramp.inode(self.root.stat()),
                           "registry_sha256": sha, "outer_cgroup": live["cgroup"]}
                plan = {"schema": "metaviewer-arm64-session/v1", "run_id": run_id, "unit": unit,
                        "multi_registry": str(self.root), "registry_sha256": sha}
                row = dict(self.row, session_id=run_id, custody=custody)
                with (mock.patch.object(ramp.m7, "shm_base", return_value=Path("/dev/shm")),
                      mock.patch.object(ramp.signal, "pidfd_send_signal") as send):
                    for recorded, outer in ((row, dict(live, pid=live["pid"] + 1)),
                                            (dict(row, starttime=live["start_ticks"] + 1),
                                             dict(live, start_ticks=live["start_ticks"] + 1))):
                        (root / "PLAN.json").unlink(missing_ok=True)
                        ramp.m7.setup.write_new(root / "PLAN.json", dict(plan, outer=outer))
                        with self.assertRaises(ramp.Refused):
                            ramp.close_m7(recorded)
                    send.assert_not_called()
                    self.assertIsNone(self.child.poll())
                    self.assertTrue((slot / "held-session.json").is_file())

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
            self.assertEqual(witness["pgid"], self.row["pgid"])
            self.assertTrue(witness["profile_absent"])
            self.assertTrue(witness["profile_removed"])
            self.assertEqual(witness["signals"], ["TERM", "KILL"])
            self.assertEqual(witness["profile"], {
                "name": self.profile.name, "inode": self.row["profile_inode"],
                "disposition": "removed", "absence_witness": "directory_entry_enoent",
            })
            with ramp.locked_root(self.root) as fd:
                stored = ramp.read_state(fd)
            self.assertEqual(stored["sessions"], [])
            self.assertEqual(stored["close_witnesses"], [witness])

        def test_residual_profile_withholds_witness_and_retry_proves_removal(self):
            self.child.kill()
            self.child.wait(timeout=3)
            with mock.patch.object(ramp.shutil, "rmtree", autospec=True) as remove:
                remove.avoids_symlink_attacks = True
                code, result = self.close(self.row)
            self.assertEqual(code, 2, result)
            self.assertEqual(result["reason"], "profile_residual")
            self.assertTrue(self.profile.is_dir())
            with ramp.locked_root(self.root) as fd:
                stored = ramp.read_state(fd)
            self.assertEqual(stored["sessions"], [self.row])
            self.assertEqual(stored["close_witnesses"], [])

            output = io.StringIO()
            with (mock.patch.object(ramp.signal, "pidfd_send_signal") as send,
                  contextlib.redirect_stdout(output)):
                code = ramp.main(["close", "--all-recorded", "--state",
                                  str(self.root / ramp.STATE_NAME)])
            send.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0, result)
            witness, = result["close_witnesses"]
            self.assertEqual(witness["signals"], [])
            self.assertEqual(witness["profile"]["disposition"], "removed")
            self.assertFalse(self.profile.exists())

        def test_already_absent_profile_has_distinct_witness(self):
            ramp.shutil.rmtree(self.profile)
            self.child.kill()
            self.child.wait(timeout=3)
            code, result = self.close(self.row)
            self.assertEqual(code, 0, result)
            witness, = result["close_witnesses"]
            self.assertTrue(witness["profile_absent"])
            self.assertFalse(witness["profile_removed"])
            self.assertEqual(witness["profile"]["disposition"], "already_absent")
            self.assertEqual(witness["profile"]["absence_witness"], "directory_entry_enoent")
            self.assertEqual(witness["signals"], [])


if __name__ == "__main__":
    if sys.argv[1:] == ["NamespaceCloseTests"]:
        unittest.main()
    else:
        unittest.main(defaultTest="RampTests")
