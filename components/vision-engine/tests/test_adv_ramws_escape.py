"""Adversarial tests for RAM workspace path guards in vadapt/ramws.py."""
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from vadapt import ramws
from vadapt.ramws import RamError, _name

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOL = "adv_escape_ut"
BASE = Path(ramws.ROOT)


def sv(free=ramws.FREE_MIN + (1 << 30)):
    return lambda p: types.SimpleNamespace(f_bavail=free, f_frsize=1)


class TestAdvRamwsEscape(unittest.TestCase):
    def setUp(self):
        (PROJECT_ROOT / "build").mkdir(exist_ok=True)
        if ramws.is_ram_fs("/dev/shm") is None:
            self.skipTest("no tmpfs available")

        self.ws = []
        self.outside_dir = Path(tempfile.mkdtemp(prefix="adv_outside_"))
        self.sentinel = self.outside_dir / "sentinel.txt"
        self.sentinel.write_text("SENTINEL_INITIAL_DATA", encoding="utf-8")
        self.sentinel_mtime = self.sentinel.stat().st_mtime_ns

    def tearDown(self):
        for w in self.ws:
            try:
                w.close()
            except Exception:
                pass
        shutil.rmtree(BASE / TOOL, ignore_errors=True)
        shutil.rmtree(self.outside_dir, ignore_errors=True)

    def assert_sentinel_intact(self):
        self.assertTrue(self.sentinel.exists(), "Sentinel file was deleted outside root")
        self.assertEqual(
            self.sentinel.read_text(encoding="utf-8"),
            "SENTINEL_INITIAL_DATA",
            "Sentinel content was altered outside root",
        )
        self.assertEqual(
            self.sentinel.stat().st_mtime_ns,
            self.sentinel_mtime,
            "Sentinel mtime was modified outside root",
        )

    def open_workspace(self, run_id="r1", camp="c", quota=ramws.RUN_DEFAULT, **kw):
        if "statvfs" not in kw:
            kw["statvfs"] = sv()
        w = ramws.open_run(camp, run_id, quota, tool=TOOL, **kw)
        self.ws.append(w)
        return w

    def test_dotdot_parts(self):
        ws = self.open_workspace("dotdot")
        before_used = ws.used
        for bad in ("../outside", "sub/../../outside", "..", "a/.."):
            with self.assertRaises(RamError) as ctx:
                ws.path(bad)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

            with self.assertRaises(RamError) as ctx:
                ws.release(bad)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_absolute_paths(self):
        ws = self.open_workspace("abs_path")
        before_used = ws.used
        for bad_abs in (str(self.sentinel), str(self.outside_dir), "/etc/passwd", "/tmp"):
            with self.assertRaises(RamError) as ctx:
                ws.path(bad_abs)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

            with self.assertRaises(RamError) as ctx:
                ws.release(bad_abs)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_nul_byte_path(self):
        ws = self.open_workspace("nul_path")
        before_used = ws.used
        for bad_nul in ("foo\0bar", "\0", "a/b\0c"):
            with self.assertRaises(RamError) as ctx:
                ws.path(bad_nul)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_nul_byte_release(self):
        """Bug family: release_nul_unhandled - ws.release on NUL-containing path raises ValueError instead of E_RAM_ESCAPE."""
        ws = self.open_workspace("nul_release")
        before_used = ws.used
        for bad_nul in ("foo\0bar", "\0", "a/b\0c"):
            with self.assertRaises(RamError) as ctx:
                ws.release(bad_nul)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")
        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_empty_string(self):
        ws = self.open_workspace("empty_str")
        before_used = ws.used

        with self.assertRaises(RamError) as ctx:
            ws.path("")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        with self.assertRaises(RamError) as ctx:
            ws.release("")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        with self.assertRaises(RamError) as ctx:
            _name("", "campaign")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_symlink_inside_root_pointing_outside(self):
        ws = self.open_workspace("sym_inside")
        before_used = ws.used
        sym_out = ws.root / "link_outside"
        os.symlink(self.outside_dir, sym_out)

        with self.assertRaises(RamError) as ctx:
            ws.path("link_outside/target.txt")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        with self.assertRaises(RamError) as ctx:
            ws.release("link_outside/sentinel.txt")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_symlinked_parent_directory(self):
        ws = self.open_workspace("sym_parent")
        before_used = ws.used
        sym_p = ws.root / "parent_sym"
        os.symlink(self.outside_dir, sym_p)

        with self.assertRaises(RamError) as ctx:
            ws.path("parent_sym/sub/file.txt")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        with self.assertRaises(RamError) as ctx:
            ws.release("parent_sym/sentinel.txt")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_symlink_swapped_between_calls(self):
        ws = self.open_workspace("sym_swap")
        p1 = ws.path("swap_dir/file1.txt")
        p1.write_bytes(b"DATA")
        ws.account()
        before_used = ws.used

        shutil.rmtree(ws.root / "swap_dir")
        os.symlink(self.outside_dir, ws.root / "swap_dir")

        with self.assertRaises(RamError) as ctx:
            ws.path("swap_dir/file2.txt")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assert_sentinel_intact()

    def test_release_root_itself(self):
        ws = self.open_workspace("release_root")
        before_used = ws.used
        for root_target in (ws.root, str(ws.root), str(ws.root) + "/", ".", "", "/", ws.root / "."):
            with self.assertRaises(RamError) as ctx:
                ws.release(root_target)
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertTrue(ws.root.is_dir(), "Workspace root was deleted")
        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_release_through_symlinked_parent(self):
        ws = self.open_workspace("rel_sym_p")
        before_used = ws.used
        sym_p = ws.root / "p_sym"
        os.symlink(self.outside_dir, sym_p)

        with self.assertRaises(RamError) as ctx:
            ws.release("p_sym/sentinel.txt")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_release_absolute_outside_path(self):
        ws = self.open_workspace("rel_abs_out")
        before_used = ws.used
        with self.assertRaises(RamError) as ctx:
            ws.release(str(self.sentinel))
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_unicode_lookalike_dots(self):
        ws = self.open_workspace("u_dots")
        before_used = ws.used
        for u_dot in ("\uff0e\uff0e", "\u2024\u2024", "\u3002\u3002"):
            with self.assertRaises(RamError) as ctx:
                _name(u_dot, "campaign")
            self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assertEqual(ws.used, before_used)
        self.assert_sentinel_intact()

    def test_names_matching_namere_symlink_escape(self):
        """Bug family: symlink_component_escape - open_run allows pre-existing symlinked directory components matching NAME_RE that resolve outside root."""
        sym_camp_dir = BASE / TOOL / "sym_camp"
        sym_camp_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(self.outside_dir, sym_camp_dir)

        with self.assertRaises(RamError) as ctx:
            self.open_workspace("r1", camp="sym_camp")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")
        self.assertFalse((self.outside_dir / "r1").exists())
        self.assert_sentinel_intact()

    def test_symlinked_tool_and_run_components(self):
        for component in ("tool", "run"):
            with self.subTest(component=component):
                shutil.rmtree(BASE / TOOL, ignore_errors=True)
                link = BASE / TOOL
                if component == "run":
                    link = link / "c" / "r1"
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(self.outside_dir, target_is_directory=True)
                try:
                    with self.assertRaises(RamError) as ctx:
                        self.open_workspace()
                    self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")
                    self.assert_sentinel_intact()
                finally:
                    link.unlink()

    def test_symlink_component_inside_ram_root(self):
        target = BASE / TOOL / "target"
        target.mkdir(parents=True, exist_ok=True)
        (BASE / TOOL / "sym_camp").symlink_to(target, target_is_directory=True)
        with self.assertRaises(RamError) as ctx:
            self.open_workspace(camp="sym_camp")
        self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")



if __name__ == "__main__":
    unittest.main()
