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


if __name__ == "__main__":
    unittest.main()
