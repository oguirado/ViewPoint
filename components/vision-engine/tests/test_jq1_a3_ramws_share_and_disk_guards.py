"""Adversarial tests for ramws _apply_share fail-closed rollback, sweep_kept symlink safety, and check_disk."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vadapt import ramws
from vadapt.ramws import RamError


def _check_ram_available():
    if not os.path.exists("/dev/shm"):
        return False
    return ramws.is_ram_fs("/dev/shm") is not None


class TestRamwsShareAndDiskGuards(unittest.TestCase):
    def setUp(self):
        if not _check_ram_available():
            self.skipTest("Verified RAM storage (/dev/shm) unavailable")
        self.tmp_obj = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.ram_root = self.tmp_obj.name

    def tearDown(self):
        if hasattr(self, "tmp_obj"):
            self.tmp_obj.cleanup()

    def test_apply_share_rollback_failed_run_mode(self):
        """Mock _share_acl to raise OSError after leaving mode 01770 on run dir.
        Assert E_RAM_SHARE with 'acl rollback failed', group fallback not called, run removed.
        """
        def fake_share_acl(run, chain, uid):
            os.chmod(run, 0o1770)
            raise OSError("simulated setfacl error")

        with mock.patch.object(ramws, "_share_acl", side_effect=fake_share_acl), \
             mock.patch.object(ramws, "_share_group") as mock_group:
            with self.assertRaises(RamError) as ctx:
                ramws.open_run(
                    "camp1", "run1",
                    tool="testtool",
                    root=self.ram_root,
                    share_with_uid=20001,
                    share_group=os.getgid()
                )
            self.assertEqual(ctx.exception.code, "E_RAM_SHARE")
            self.assertIn("acl rollback failed", str(ctx.exception))
            mock_group.assert_not_called()
            run_path = Path(self.ram_root) / "testtool" / "camp1" / "run1"
            self.assertFalse(run_path.exists())

    def test_apply_share_rollback_failed_lock_mode(self):
        """Mock _share_acl to raise OSError after leaving mode 0640 on .lock.
        Assert E_RAM_SHARE with 'acl rollback failed', group fallback not called, run removed.
        """
        def fake_share_acl(run, chain, uid):
            os.chmod(run / ".lock", 0o640)
            raise OSError("simulated setfacl error")

        with mock.patch.object(ramws, "_share_acl", side_effect=fake_share_acl), \
             mock.patch.object(ramws, "_share_group") as mock_group:
            with self.assertRaises(RamError) as ctx:
                ramws.open_run(
                    "camp1", "run2",
                    tool="testtool",
                    root=self.ram_root,
                    share_with_uid=20001,
                    share_group=os.getgid()
                )
            self.assertEqual(ctx.exception.code, "E_RAM_SHARE")
            self.assertIn("acl rollback failed", str(ctx.exception))
            mock_group.assert_not_called()
            run_path = Path(self.ram_root) / "testtool" / "camp1" / "run2"
            self.assertFalse(run_path.exists())

    def test_apply_share_rollback_stat_oserror(self):
        """Independently inject an OSError specifically at the rollback stat inspection in _apply_share.
        Assert E_RAM_SHARE, without breaking pathlib or cleanup.
        """
        in_share_acl = False
        stat_failed = False

        def fake_share_acl(run, chain, uid):
            nonlocal in_share_acl
            in_share_acl = True
            raise OSError("simulated acl failure")

        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            nonlocal stat_failed
            if in_share_acl and not stat_failed:
                stat_failed = True
                raise OSError("simulated stat error during rollback check")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(ramws, "_share_acl", side_effect=fake_share_acl), \
             mock.patch("os.stat", side_effect=fake_stat):
            with self.assertRaises(RamError) as ctx:
                ramws.open_run(
                    "camp1", "run3",
                    tool="testtool",
                    root=self.ram_root,
                    share_with_uid=20001,
                    share_group=os.getgid()
                )
            self.assertEqual(ctx.exception.code, "E_RAM_SHARE")
            self.assertIn("simulated acl failure", str(ctx.exception))
            run_path = Path(self.ram_root) / "testtool" / "camp1" / "run3"
            self.assertFalse(run_path.exists())

    def test_sweep_kept_symlink_safety(self):
        """RAM-only build/kept_frames symlink to outside directory with expired KEEP.json (ttl_until=1).
        sweep_kept with now=1000 must preserve symlink and outside sentinel, removing ordinary expired sibling.
        """
        build_dir = Path(self.ram_root) / "build"
        outside_dir = Path(self.ram_root) / "outside_kept"
        outside_dir.mkdir(parents=True)
        (outside_dir / "KEEP.json").write_text(json.dumps({"ttl_until": 1}), encoding="utf-8")
        sentinel = outside_dir / "sentinel.txt"
        sentinel.write_text("sentinel_data", encoding="utf-8")

        kept_frames = build_dir / "kept_frames"
        kept_frames.mkdir(parents=True)

        symlink_entry = kept_frames / "symlink_kept"
        symlink_entry.symlink_to(outside_dir)

        normal_expired = kept_frames / "normal_kept"
        normal_expired.mkdir(parents=True)
        (normal_expired / "KEEP.json").write_text(json.dumps({"ttl_until": 1}), encoding="utf-8")
        (normal_expired / "data.txt").write_text("normal_data", encoding="utf-8")

        gone = ramws.sweep_kept(build_dir, now=1000)

        self.assertEqual(gone, ["normal_kept"])
        self.assertFalse(normal_expired.exists())
        self.assertTrue(symlink_entry.is_symlink())
        self.assertTrue(outside_dir.exists())
        self.assertTrue(sentinel.exists())

    def test_check_disk_mocked_extensions_names_and_since(self):
        """Test check_disk using mocks for os.walk and os.lstat without writing frame files to disk.
        Covers all FRAME_EXT, FRAME_NAMES, mixed-case extensions, exemptions, and since boundary.
        """
        base_dir = self.ram_root

        files_sub1 = []
        for ext in ramws.FRAME_EXT:
            files_sub1.append(f"frame_file{ext}")
            files_sub1.append(f"frame_file_mixed{ext.upper()}")

        for name in ramws.FRAME_NAMES:
            files_sub1.append(name)

        files_sub1.append("notes.txt")

        def fake_walk(top, topdown=True, onerror=None, followlinks=False):
            dirnames = ["kept_frames", "fail_evidence", "sub1"]
            yield (top, dirnames, ["root_file.txt"])

            if "sub1" in dirnames:
                yield (os.path.join(top, "sub1"), [], files_sub1)

        def fake_lstat(path):
            p_str = str(path)
            mtime = 500 if "frame_file.png" in p_str else 2000
            st = mock.MagicMock()
            st.st_mtime_ns = mtime
            return st

        with mock.patch("os.walk", side_effect=fake_walk), \
             mock.patch("os.lstat", side_effect=fake_lstat):

            result_all = ramws.check_disk(base_dir, allow=("kept_frames", "fail_evidence"), since=None)

            expected_files = set()
            for ext in ramws.FRAME_EXT:
                expected_files.add(f"sub1/frame_file{ext}")
                expected_files.add(f"sub1/frame_file_mixed{ext.upper()}")
            for name in ramws.FRAME_NAMES:
                expected_files.add(f"sub1/{name}")

            self.assertEqual(set(result_all), expected_files)
            self.assertNotIn("sub1/notes.txt", result_all)
            self.assertNotIn("root_file.txt", result_all)

            result_since = ramws.check_disk(base_dir, allow=("kept_frames", "fail_evidence"), since=1000)
            self.assertNotIn("sub1/frame_file.png", result_since)
            self.assertIn("sub1/frame_file.jpg", result_since)
            self.assertEqual(len(result_since), len(expected_files) - 1)


if __name__ == "__main__":
    unittest.main()
