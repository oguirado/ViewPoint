"""Exercise the coordinator wrapper with private RAM and synthetic ramp ledgers."""

import os
from pathlib import Path
import secrets
import shutil
import tempfile
import unittest
from unittest import mock

from tools import mv_c01_launcher as launcher


class C01LauncherTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.surface = Path(scratch.name)
        self.coordinator = self.surface / "harness" / "live.py"
        self.coordinator.parent.mkdir()
        self.coordinator.touch()
        self.approval = self.surface / "approval.json"
        self.output = self.surface / "result"
        self.receipt = self.surface / "result.runtime-removal.json"
        self.runtime = Path("/dev/shm") / ("c01-test-" + secrets.token_hex(12))
        self.addCleanup(lambda: shutil.rmtree(self.runtime) if self.runtime.is_dir() else None)
        self.block = self.runtime / "cold-01"
        launcher.m7.setup.write_new(self.approval, {
            "schema": "viewpoint-c01-live-approval/v1", "decision": "ADMIT", "scope_unit": "fixture.scope",
            "display": ":90", "runtime_root": str(self.runtime), "profiles": {"cold": {"blocks": 1}},
        })

    def read_receipt(self):
        return launcher.m7.setup.decode(launcher.m7.setup.read(self.receipt, private=True))

    def invoke(self, populate, returncode=0):
        def coordinator(argv):
            self.assertEqual(argv[2:], [str(self.coordinator), "--approval", str(self.approval),
                                       "--out", str(self.output)])
            self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.block.stat().st_mode & 0o777, 0o700)
            self.assertFalse(list(self.block.iterdir()))
            self.output.mkdir(mode=0o700)
            (self.output / self.block.name).mkdir(mode=0o700)
            populate()
            return mock.Mock(wait=mock.Mock(return_value=returncode))

        environment = mock.patch.dict(os.environ, {"DISPLAY": ":90"})
        with (environment, mock.patch.object(launcher.ramp, "verify_scope"),
              mock.patch.object(launcher.m7, "verify_guardian_cpus"),
              mock.patch.object(Path, "read_text", return_value="0"),
              mock.patch.object(launcher.subprocess, "Popen", side_effect=coordinator)):
            return launcher.launch(self.coordinator, self.approval, self.output)

    def test_archives_settled_ledger_and_witnesses_root_removal(self):
        def settled():
            with launcher.ramp.locked_root(self.block) as fd:
                state = launcher.ramp.empty_state(fd)
                state["close_witnesses"].append({"session_id": "fixture", "process_absent": True,
                                                  "profile_absent": True, "m7": {"proof": "/fixture/proof"}})
                launcher.ramp.write_state(fd, state)

        self.assertEqual(self.invoke(settled), 0)
        receipt = self.read_receipt()
        self.assertEqual(receipt["disposition"], "removed")
        self.assertEqual(receipt["absence_witness"], "owned_directory_unlinked_and_path_enoent")
        self.assertEqual(receipt["archived_ledgers"], ["cold-01"])
        self.assertTrue((self.output / "cold-01" / launcher.ramp.STATE_NAME).is_file())
        self.assertFalse(self.runtime.exists())

    def test_residual_entries_preserve_root_even_after_zero_exit(self):
        with self.assertRaisesRegex(ValueError, "residual files"):
            self.invoke(lambda: (self.block / "unknown").touch())
        self.assertTrue((self.block / "unknown").exists())
        self.assertEqual(self.read_receipt()["disposition"], "preserved")

    def test_failed_coordinator_preserves_private_runtime(self):
        self.assertEqual(self.invoke(lambda: None, returncode=2), 2)
        self.assertTrue(self.block.is_dir())
        self.assertEqual(self.read_receipt()["coordinator_returncode"], 2)
        self.assertEqual(self.read_receipt()["disposition"], "preserved")

    def test_unsettled_ledger_is_preserved(self):
        def unsettled():
            with launcher.ramp.locked_root(self.block) as fd:
                state = launcher.ramp.empty_state(fd)
                state["close_witnesses"].append({"process_absent": True, "profile_absent": False})
                launcher.ramp.write_state(fd, state)

        with self.assertRaisesRegex(ValueError, "ledger is unsettled"):
            self.invoke(unsettled)
        self.assertTrue((self.block / launcher.ramp.STATE_NAME).is_file())
        self.assertEqual(self.read_receipt()["disposition"], "preserved")

    def test_existing_runtime_never_adopted(self):
        self.runtime.mkdir(mode=0o700)
        marker = self.runtime / "foreign"
        marker.touch()
        with self.assertRaises(FileExistsError):
            self.invoke(lambda: self.fail("coordinator started against pre-existing root"))
        self.assertTrue(marker.exists())
        self.assertFalse(self.receipt.exists())
