"""Unit tests for private helpers in vadapt/ramws.py."""
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vadapt import ramws
from vadapt.ramws import RamError, _int_id, _name, _owner, _rollback, _tree_bytes, _try_lock, _unesc, is_ram_fs


class TestUnesc(unittest.TestCase):
    def test_unesc_octal_escapes(self):
        # Decodes octal escapes of /proc/mounts fields
        self.assertEqual(_unesc(r"/mnt/a\040b"), "/mnt/a b")
        self.assertEqual(_unesc(r"\011\134"), "\t\\")
        self.assertEqual(_unesc(r"foo\040bar\012baz"), "foo bar\nbaz")

    def test_unesc_plain_text_negative(self):
        # Plain text without octal escapes is returned unchanged
        self.assertEqual(_unesc("/tmp/plain_path"), "/tmp/plain_path")
        self.assertEqual(_unesc(r"\999"), r"\999")
        self.assertEqual(_unesc(""), "")


class TestIsRamFs(unittest.TestCase):
    def test_is_ram_fs_types(self):
        mount_text = (
            "100 1 8:1 / / sysfs sysfs rw\n"
            "101 1 0:5 / /dev/shm rw - tmpfs tmpfs rw\n"
            "102 1 0:6 / /mnt/ram rw - ramfs ramfs rw\n"
            "103 1 8:2 / /mnt/disk rw - ext4 /dev/sda1 rw\n"
            "104 1 0:7 / /dev/shm/overlay rw - overlay overlay rw\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write(mount_text)
            f_path = f.name
        self.addCleanup(lambda: os.unlink(f_path) if os.path.exists(f_path) else None)

        self.assertEqual(is_ram_fs("/dev/shm/ws", f_path), "tmpfs")
        self.assertEqual(is_ram_fs("/mnt/ram/dir", f_path), "ramfs")

    def test_is_ram_fs_negative(self):
        mount_text = (
            "100 1 8:1 / / sysfs sysfs rw\n"
            "101 1 0:5 / /dev/shm rw - tmpfs tmpfs rw\n"
            "102 1 8:2 / /mnt/disk rw - ext4 /dev/sda1 rw\n"
            "103 1 0:7 / /dev/shm/overlay rw - overlay overlay rw\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write(mount_text)
            f_path = f.name
        self.addCleanup(lambda: os.unlink(f_path) if os.path.exists(f_path) else None)

        # Longer mount prefix wins -> overlay on /dev/shm/overlay is not ramfs
        self.assertIsNone(is_ram_fs("/dev/shm/overlay/test", f_path))
        # Non-ramfs mount
        self.assertIsNone(is_ram_fs("/mnt/disk/sub", f_path))
        # Unmatched mount path
        self.assertIsNone(is_ram_fs("/nonexistent/path", f_path))


class TestName(unittest.TestCase):
    def test_name_accepts_safe(self):
        self.assertEqual(_name("valid_name-1.0", "tool"), "valid_name-1.0")
        self.assertEqual(_name("A" * 128, "campaign"), "A" * 128)
        self.assertEqual(_name("run0123", "run_id"), "run0123")

    def test_name_rejects_unsafe_negative(self):
        invalid_cases = [
            ("..", "dotdot"),
            (".", "dot"),
            ("", "empty"),
            (".hidden", "leading_dot"),
            ("a/b", "slash"),
            ("A" * 129, "overlong"),
            (None, "non_string_none"),
            (123, "non_string_int"),
        ]
        for val, what in invalid_cases:
            with self.assertRaises(RamError) as ctx:
                _name(val, what)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")
            self.assertEqual(str(ctx.exception), f"E_RAM_ESCAPE bad {what} {val!r}")


class TestTreeBytes(unittest.TestCase):
    def test_tree_bytes_file_and_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            f1 = p / "f1.bin"
            f1.write_bytes(b"hello world" * 100)
            d1 = p / "sub"
            d1.mkdir()
            f2 = d1 / "f2.bin"
            f2.write_bytes(b"sample content" * 100)

            # Symlink pointing to f1
            sym = p / "sym.link"
            sym.symlink_to(f1)

            tb = _tree_bytes(p)
            self.assertGreater(tb, 0)
            # st_blocks * 512 is divisible by 512
            self.assertEqual(tb % 512, 0)

            # Check individual file tree_bytes
            f1_bytes = _tree_bytes(f1)
            self.assertGreater(f1_bytes, 0)

            # Check symlink itself is stat'd without following
            sym_bytes = _tree_bytes(sym)
            self.assertGreaterEqual(sym_bytes, 0)
            # Symlink blocks size should be less than or equal to small link size
            self.assertLessEqual(sym_bytes, f1_bytes)

    def test_tree_bytes_nonexistent_negative(self):
        self.assertEqual(_tree_bytes("/nonexistent_path_for_unit_test"), 0)


class TestOwner(unittest.TestCase):
    def test_owner_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            owner_data = {"pid": 1234, "tool": "mytool", "uid": os.getuid(), "boot_id": "abc"}
            (p / "owner.json").write_text(json.dumps(owner_data), encoding="utf-8")

            res = _owner(p)
            self.assertEqual(res, owner_data)

    def test_owner_negative_cases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)

            # Missing owner.json file
            self.assertIsNone(_owner(p))

            # Non-regular file (directory named owner.json)
            (p / "owner.json").mkdir()
            self.assertIsNone(_owner(p))
            (p / "owner.json").rmdir()

            # Invalid JSON file
            (p / "owner.json").write_text("invalid json", encoding="utf-8")
            self.assertIsNone(_owner(p))

            # Wrong uid
            owner_data = {"pid": 1234, "tool": "mytool", "uid": os.getuid(), "boot_id": "abc"}
            (p / "owner.json").write_text(json.dumps(owner_data), encoding="utf-8")
            with mock.patch("os.fstat") as mock_fstat:
                st = os.stat(p / "owner.json")
                mock_st = mock.MagicMock(spec=st)
                mock_st.st_mode = st.st_mode
                mock_st.st_uid = os.getuid() + 9999
                mock_fstat.return_value = mock_st
                self.assertIsNone(_owner(p))


class TestTryLock(unittest.TestCase):
    def test_try_lock_free_held_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)

            # Missing lock file
            self.assertEqual(_try_lock(p), False)

            lock_file = p / ".lock"
            lock_file.write_bytes(b"")

            # Free lock -> returns open fd int
            fd = _try_lock(p)
            self.assertIsInstance(fd, int)
            self.assertGreaterEqual(fd, 0)

            # Held lock -> another open & flock in separate process or mocked flock
            # Test held lock by mocking flock to raise OSError
            with mock.patch("fcntl.flock", side_effect=OSError(11, "Resource temporarily unavailable")):
                res = _try_lock(p)
                self.assertIsNone(res)

            os.close(fd)


class TestRollback(unittest.TestCase):
    def test_rollback_removes_run_and_empty_parents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            tool_dir = base / "tool"
            camp_dir = tool_dir / "camp"
            run_dir = camp_dir / "run1"
            run_dir.mkdir(parents=True)
            (run_dir / "file.txt").write_text("data")

            _rollback(run_dir)

            self.assertFalse(run_dir.exists())
            self.assertFalse(camp_dir.exists())
            self.assertFalse(tool_dir.exists())
            self.assertTrue(base.exists())

    def test_rollback_leaves_nonempty_parents_negative(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            tool_dir = base / "tool"
            camp_dir = tool_dir / "camp"
            run1_dir = camp_dir / "run1"
            run2_dir = camp_dir / "run2"
            run1_dir.mkdir(parents=True)
            run2_dir.mkdir(parents=True)

            _rollback(run1_dir)

            self.assertFalse(run1_dir.exists())
            # Sibling run2 exists, so camp_dir and tool_dir must NOT be removed
            self.assertTrue(camp_dir.exists())
            self.assertTrue(run2_dir.exists())
            self.assertTrue(tool_dir.exists())


class TestIntId(unittest.TestCase):
    def test_int_id_valid(self):
        self.assertIsNone(_int_id(None, "uid"))
        self.assertIsNone(_int_id(0, "gid"))
        self.assertIsNone(_int_id(1000, "share_with_uid"))
        self.assertIsNone(_int_id((1 << 32) - 1, "share_group"))

    def test_int_id_invalid_negative(self):
        invalid_cases = [
            (True, "boolean_true"),
            (False, "boolean_false"),
            (-1, "negative_int"),
            (1 << 32, "overlong_int"),
            (3.14, "float_val"),
            ("1000", "string_val"),
        ]
        for val, what in invalid_cases:
            with self.assertRaises(RamError) as ctx:
                _int_id(val, what)
            self.assertEqual(ctx.exception.code, "E_RAM_SHARE")
            self.assertEqual(str(ctx.exception), f"E_RAM_SHARE bad {what} {val!r}")


class TestWorkspaceContract(unittest.TestCase):
    def setUp(self):
        from vadapt import ramws

        self.ramws = ramws
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_open_rolls_back_if_workspace_initialization_fails(self):
        ramws = self.ramws
        with mock.patch.object(ramws, "is_ram_fs", return_value="tmpfs"), \
                mock.patch.object(ramws, "_swap_used", side_effect=OSError("reading memory status")):
            with self.assertRaises(OSError):
                ramws.open_run("campaign", "run", tool="vision", root=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_close_preserves_ownership_on_failure_and_accounts_for_retry(self):
        ramws = self.ramws
        with mock.patch.object(ramws, "is_ram_fs", return_value="tmpfs"), \
                mock.patch.object(ramws, "_install_handlers"):
            ws = ramws.open_run("campaign", "run", tool="vision", root=self.root)
        with ws:
            (ws.path("payload/") / "frame").write_bytes(b"test")
            before = ws.account()
            with mock.patch.object(ramws.shutil, "rmtree", side_effect=PermissionError("removing run")):
                with self.assertRaises(PermissionError):
                    ws.close()
            self.assertTrue(ws.root.is_dir())
            self.assertEqual(ws.used, before)
            self.assertEqual(ws.receipt()["released_bytes"], 0)
            ws.close()
            self.assertFalse(ws.root.exists())
            self.assertEqual(ws.used, 0)
            self.assertEqual(ws.receipt()["released_bytes"], before)

    def test_close_accounts_for_partial_removal_before_retry(self):
        ramws = self.ramws
        with mock.patch.object(ramws, "is_ram_fs", return_value="tmpfs"), \
                mock.patch.object(ramws, "_install_handlers"):
            ws = ramws.open_run("campaign", "run", tool="vision", root=self.root)

        with ws:
            first = ws.path("payload/first")
            first.write_bytes(b"a" * 8192)
            ws.path("payload/second").write_bytes(b"b" * 4096)
            before = ws.account()

            def remove_part(path):
                self.assertEqual(path, first.parent)
                first.unlink()
                raise PermissionError("removing the remaining payload")

            with mock.patch.object(ramws.shutil, "rmtree", side_effect=remove_part):
                with self.assertRaises(PermissionError):
                    ws.close()

            remaining = ramws._tree_bytes(ws.root)
            self.assertLess(remaining, before)
            self.assertEqual(ws.used, remaining)
            self.assertEqual(ws.receipt()["released_bytes"], before - remaining)
            self.assertIn(ws, ramws._OPEN)
            self.assertIsNone(ramws._try_lock(ws.root))
            self.assertTrue((ws.root / "owner.json").is_file())

            ws.close()
            self.assertFalse(ws.root.exists())
            self.assertEqual(ws.used, 0)
            self.assertEqual(ws.receipt()["released_bytes"], before)
            ws.close()
            self.assertEqual(ws.receipt()["released_bytes"], before)

    def test_sweep_keeps_live_run_and_reports_only_successful_removal(self):
        ramws = self.ramws
        with mock.patch.object(ramws, "is_ram_fs", return_value="tmpfs"), mock.patch.object(ramws, "_install_handlers"):
            ws = ramws.open_run("campaign", "live", tool="vision", root=self.root)
        with ws, mock.patch.object(ramws, "_boot_id", return_value="different"):
            self.assertEqual(ramws.sweep("vision", self.root), [])
            self.assertTrue(ws.root.is_dir())
        run = self.root / "vision" / "campaign" / "stale"
        run.mkdir(parents=True)
        (run / ".lock").write_bytes(b"")
        owner = {"uid": os.getuid(), "tool": "vision", "boot_id": "old"}
        (run / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
        with mock.patch.object(ramws.shutil, "rmtree", side_effect=PermissionError("removing run")):
            self.assertEqual(ramws.sweep("vision", self.root), [])
        self.assertTrue(run.is_dir())
        self.assertEqual(ramws.sweep("vision", self.root), ["campaign/stale"])
        self.assertFalse(run.exists())

    def test_cli_validates_options_and_bounds_before_calling_operations(self):
        ramws = self.ramws
        with mock.patch.object(ramws.sys, "stdout"), mock.patch.object(ramws, "sweep") as sweep, \
                mock.patch.object(ramws, "sweep_kept") as sweep_kept, mock.patch.object(ramws, "check_disk") as check_disk:
            for args in (
                ["sweep", "--tool", "vision", "--unknown", "value"],
                ["sweep", "--tool", "vision", "--tool", "other"],
                ["sweep-kept", "--build", str(self.root), "--now", "nan"],
                ["check-disk", str(self.root), "--since", "-1"],
                ["check-disk", str(self.root), "--allow", "one,,two"],
            ):
                with self.subTest(args=args):
                    self.assertEqual(ramws.main(args), 2)
            for operation in (sweep, sweep_kept, check_disk):
                operation.assert_not_called()

    def test_cli_rejects_missing_roots_and_unreadable_disk_checks(self):
        ramws = self.ramws
        missing = str(self.root / "missing")
        for args in (
            ["check-disk", missing],
            ["status", "--root", missing],
            ["sweep", "--tool", "vision", "--root", missing],
            ["sweep-kept", "--build", missing],
        ):
            with self.subTest(args=args), mock.patch.object(ramws.sys, "stdout") as stdout:
                self.assertEqual(ramws.main(args), 5)
                self.assertEqual(json.loads(stdout.write.call_args.args[0])["code"], "E_IO")

        def unreadable(path, *, onerror):
            onerror(PermissionError("reading workspace evidence"))

        with mock.patch.object(ramws.os, "walk", side_effect=unreadable), \
                mock.patch.object(ramws.sys, "stdout") as stdout:
            self.assertEqual(ramws.main(["check-disk", str(self.root)]), 5)
            self.assertEqual(json.loads(stdout.write.call_args.args[0])["code"], "E_IO")

        with mock.patch.object(ramws.sys, "stdout") as stdout:
            self.assertEqual(ramws.main(["check-disk", str(self.root)]), 0)
            self.assertEqual(json.loads(stdout.write.call_args.args[0]), {"code": "OK", "offending": []})

    def test_acl_restores_changed_parents_and_run_files_before_group_fallback(self):
        ramws = self.ramws
        campaign = self.root / "campaign"
        run = campaign / "run"
        run.mkdir(mode=0o700, parents=True)
        owner = run / "owner.json"
        lock = run / ".lock"
        for path in (owner, lock):
            path.touch(mode=0o600)
        original = {path: ("original", path.name) for path in (self.root, campaign, run, owner, lock)}

        for refused in (campaign, owner):
            state = original.copy()

            def set_acl(args, path):
                state[path] += ("acl",)
                if path == refused:
                    raise PermissionError("applying ACL")

            def fallback(*args):
                self.assertEqual(state, original)

            with self.subTest(refused=refused), mock.patch.object(ramws, "_open_to_all", return_value=False), \
                    mock.patch.object(ramws.shutil, "which", return_value="/mock/setfacl"), \
                    mock.patch.object(ramws, "_acl_snapshot", side_effect=lambda path: (path, state[path])), \
                    mock.patch.object(ramws, "_restore_acl", side_effect=lambda saved: state.update([saved])), \
                    mock.patch.object(ramws, "_setfacl", side_effect=set_acl), \
                    mock.patch.object(ramws.os, "chmod", side_effect=lambda path, mode: state.update({path: ("mode", mode)})), \
                    mock.patch.object(ramws, "_share_group", side_effect=fallback) as share_group:
                self.assertEqual(ramws._apply_share(run, self.root, 1234, 5678)["method"], "group")
                share_group.assert_called_once()


if __name__ == "__main__":
    unittest.main()
