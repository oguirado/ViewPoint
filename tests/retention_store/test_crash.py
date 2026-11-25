"""Terminate actual writers at persistence boundaries inside an unshared PID namespace."""

from datetime import timedelta
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from retention_service.store import Conflict, StorageFailure, Unavailable
from retention_service.store.files import digest
from tests.retention_store.test_store import (
    ADMIN, AUTHORITY, CLAIM_PROOF, INSPECTOR, PROFILE_BODY, RECOVER, RUNNER,
    START_PROOF, TENANT, Fixture,
)

ATTEMPT = "4" * 32
PHASES = ("before_file_sync", "after_file_sync", "before_rename", "after_rename",
          "after_dir_sync", "before_ack")


def isolated_pid_namespace():
    status = Path("/proc/self/status").read_text()
    ids = next(line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:"))
    pid = os.getpid()
    if len(ids) > 1:
        return int(ids[-1]) == pid and ids[0] != ids[-1]
    if pid != 1 or ids != ["1"] or os.getuid() != 0:
        return False
    uid_map = Path("/proc/self/uid_map").read_text().split()
    return len(uid_map) == 3 and uid_map[0] == "0" and uid_map[2] == "1"


class CrashTests(unittest.TestCase):
    def setUp(self):
        self.fix = Fixture()

    def prepare(self, root, label):
        admission, raw = self.fix.admission(ATTEMPT)
        ack = None
        with self.fix.store(root) as store:
            if label == "start":
                store.admit(AUTHORITY, admission)
            elif label not in {"start", "admission"}:
                _, ack = self.fix.start(store, ATTEMPT, claim=label != "claim")
            if label == "tombstone":
                self.fix.finish(store, ack)
                self.fix.now += timedelta(days=31)
        return admission, raw, ack

    def operation(self, store, label, admission, raw, ack):
        if label == "admission":
            return store.admit(AUTHORITY, admission)
        if label == "start":
            return store.start(RUNNER, ATTEMPT, raw, content_length=len(raw),
                               sha256=digest(raw), evidence=START_PROOF)
        if label == "claim":
            return store.claim(RUNNER, ATTEMPT, ack, evidence=CLAIM_PROOF)
        if label == "upload":
            return self.fix.upload(store, ATTEMPT, "bulk", b"fixture upload bytes")
        if label == "tombstone":
            return store.sweep()
        return self.fix.finish(store, ack)

    def check_restart(self, root, label, phase, admission, raw, ack):
        with self.fix.store(root) as store:
            if label in {"admission", "start", "upload"}:
                if label == "admission":
                    with self.assertRaises(Unavailable):
                        store.inspect(INSPECTOR, ATTEMPT)
                expected_new = phase in PHASES[:3]
                result, new = self.operation(store, label, admission, raw, ack)
                self.assertEqual(new, expected_new)
                self.assertEqual(self.operation(store, label, admission, raw, ack), (result, False))
            elif label == "claim":
                result = store.inspect(INSPECTOR, ATTEMPT)
                self.assertIsNone(result["terminal"])
                self.assertEqual(result["launch_claimed"], phase in PHASES[3:])
                if result["launch_claimed"]:
                    with self.assertRaises(Conflict):
                        self.operation(store, label, admission, raw, ack)
                else:
                    self.assertEqual(self.operation(store, label, admission, raw, ack), ack)
            elif label == "tombstone":
                if phase in PHASES[:3]:
                    self.assertEqual(store.inspect(INSPECTOR, ATTEMPT)["state"], "committed")
                    self.assertEqual(store.sweep(), 1)
                with self.assertRaises(Unavailable):
                    store.inspect(INSPECTOR, ATTEMPT)
                with self.assertRaises(Unavailable):
                    store.admit(AUTHORITY, admission)
                self.assertFalse((root / "tenants" / digest(TENANT.encode()) /
                                  "attempts" / ATTEMPT).exists())
                page = store.recovery_page(RECOVER, limit=5)
                tomb = next(item for item in page["items"] if item["kind"] == "tombstone")
                self.assertEqual(json.loads(store.recovery_item(RECOVER, tomb["item"]).data)
                                 ["deletion_seq"], 1)
            else:
                result = store.inspect(INSPECTOR, ATTEMPT)
                committed = label == "end" and phase in PHASES[3:]
                self.assertEqual(result["state"], "committed" if committed else "unknown")
                self.assertEqual(result["terminal"] is not None, committed)
                unpublished = label == "intent" and phase in PHASES[:3]
                if not committed:
                    page = store.recovery_page(RECOVER, limit=20)
                    states = {item["disposition"] for item in page["items"] if item["attempt_id"]}
                    self.assertEqual(states, {"unknown" if unpublished else "terminal_commit_interrupted"})
                    if unpublished:
                        self.assertFalse(any(item["kind"] == "commit_intent" for item in page["items"]))
                        self.assertTrue(any(item["kind"] == "staging" for item in page["items"]))
                if not unpublished:
                    with self.assertRaises(Conflict):
                        self.fix.finish(store, ack, PROFILE_BODY + b" ")
                finish, created = self.operation(store, label, admission, raw, ack)
                self.assertEqual(created, not committed)
                self.assertEqual(self.operation(store, label, admission, raw, ack), (finish, False))
                final = store.inspect(INSPECTOR, ATTEMPT)
                self.assertEqual(final["state"], "committed")
                with self.assertRaises(Conflict):
                    self.fix.finish(store, ack, PROFILE_BODY + b" ")
                if committed:
                    self.assertEqual(final["receipt_ref"], result["receipt_ref"])

    def test_remounted_proc_pid_one_detection(self):
        with patch("os.getpid", return_value=1), patch("os.getuid", return_value=0):
            with patch.object(Path, "read_text", side_effect=["NSpid:\t1\n", "0 1000 1\n"]):
                self.assertTrue(isolated_pid_namespace())
            with patch.object(Path, "read_text", side_effect=["NSpid:\t1\n", "0 0 4294967295\n"]):
                self.assertFalse(isolated_pid_namespace())

    def test_sigkill_and_replay_each_filesystem_publication(self):
        if not isolated_pid_namespace():
            if os.environ.get("_RETENTION_CRASH_REEXEC") == "1":
                self.fail("unshare -Urpf did not enter isolated PID namespace")
            try:
                proc = subprocess.run(
                    ["unshare", "-Urpf", sys.executable, "-B", "-m", "unittest",
                     "tests.retention_store.test_crash.CrashTests.test_sigkill_and_replay_each_filesystem_publication"],
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env={**os.environ, "_RETENTION_CRASH_REEXEC": "1"},
                    capture_output=True, text=True, timeout=120,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                self.fail(f"unshare -Urpf re-exec failed: {exc!r}")
            if (proc.returncode != 0 or "Ran 1 test" not in proc.stderr
                    or "\nOK\n" not in proc.stderr or "skipped=" in proc.stderr):
                self.fail(f"unshare -Urpf re-exec returned {proc.returncode}:\n{proc.stderr}")
            return
        units = ("admission", "start", "claim", "upload", "intent", "artifact", "end", "tombstone")
        for label in units:
            phases = PHASES if label not in {"intent", "artifact"} else PHASES[:-1]
            if label == "tombstone":
                phases = phases + ("before_purge",)
            for phase in phases:
                with self.subTest(label=label, phase=phase), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    admission, raw, ack = self.prepare(root, label)
                    pid = os.fork()
                    if pid == 0:
                        try:
                            def fault(current, checkpoint):
                                if (current, checkpoint) == (label, phase):
                                    signal.raise_signal(signal.SIGKILL)

                            with self.fix.store(root, fault=fault) as store:
                                self.operation(store, label, admission, raw, ack)
                        except BaseException:
                            import traceback
                            traceback.print_exc()
                            os._exit(2)
                        os._exit(3)
                    _, status = os.waitpid(pid, 0)
                    self.assertTrue(os.WIFSIGNALED(status))
                    self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                    self.check_restart(root, label, phase, admission, raw, ack)

    def test_failed_parent_sync_withholds_ack_and_requires_reopen(self):
        for label in ("admission", "start", "claim", "upload", "intent", "artifact", "end", "tombstone"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                admission, raw, ack = self.prepare(root, label)

                def fault(current, checkpoint):
                    if (current, checkpoint) == (label, "after_rename"):
                        raise OSError(errno.EIO, "injected failure before parent directory fsync")

                with self.fix.store(root, fault=fault) as store:
                    with self.assertRaises(StorageFailure):
                        self.operation(store, label, admission, raw, ack)
                    with self.assertRaises(StorageFailure):
                        store.ready(INSPECTOR, "inspect")
                self.check_restart(root, label, "after_rename", admission, raw, ack)
