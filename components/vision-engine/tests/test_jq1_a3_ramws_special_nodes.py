"""Tests for RamWorkspace.release and keep_frames special node handling and path safety.

VM timing: VM
"""
import multiprocessing
import os
import queue
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from vadapt import ramws
from vadapt.ramws import RamError, keep_frames, open_run

TOOL = "special_nodes_ut"
RAM_BASE = Path("/dev/shm/vqa-ramws-special-ut")


def fake_statvfs(free_bytes=ramws.FREE_MIN + (1 << 30)):
    return lambda p: types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1)


def _run_in_child_with_timeout(target_fn, args=(), timeout=5.0):
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()

    def wrapper(*a):
        try:
            res = target_fn(*a)
            q.put(("OK", res))
        except Exception as e:
            q.put(("ERR", e))

    p = ctx.Process(target=wrapper, args=args)
    p.start()
    try:
        p.join(timeout=timeout)
        if p.is_alive():
            p.terminate()
            p.join(timeout=1.0)
            if p.is_alive():
                p.kill()
                p.join()
            raise TimeoutError("Child process timed out after %s seconds" % timeout)
        if p.exitcode != 0:
            raise AssertionError("Child process exited with status code %s" % p.exitcode)
        try:
            status, val = q.get(timeout=1.0)
        except queue.Empty:
            raise AssertionError("Child process did not report its assertions") from None
        if status == "OK":
            return val
        if status == "ERR":
            raise val
        raise AssertionError("Unknown child result %r" % status)
    finally:
        q.close()
        q.join_thread()
        p.close()


class TestRamwsSpecialNodes(unittest.TestCase):
    def setUp(self):
        if not ramws.is_ram_fs("/dev/shm"):
            self.skipTest("verified RAM storage unavailable on /dev/shm")

        self.ram_root = RAM_BASE / str(os.getpid())
        shutil.rmtree(self.ram_root, ignore_errors=True)
        self.ram_root.mkdir(parents=True, exist_ok=True)

        self.outside_dir = Path(tempfile.mkdtemp(prefix="outside_", dir=self.ram_root))
        self.sentinel = self.outside_dir / "sentinel.txt"
        self.sentinel.write_text("SENTINEL_OK", encoding="utf-8")
        self.sentinel_mtime = self.sentinel.stat().st_mtime_ns
        self.ws_list = []

    def tearDown(self):
        try:
            for w in self.ws_list:
                try:
                    w.close()
                except Exception:
                    pass
        finally:
            shutil.rmtree(self.ram_root, ignore_errors=True)
            shutil.rmtree(self.outside_dir, ignore_errors=True)

    def assert_sentinel_intact(self):
        self.assertTrue(self.sentinel.exists(), "External sentinel file deleted")
        self.assertEqual(
            self.sentinel.read_text(encoding="utf-8"),
            "SENTINEL_OK",
            "External sentinel file modified",
        )
        self.assertEqual(
            self.sentinel.stat().st_mtime_ns,
            self.sentinel_mtime,
            "External sentinel file mtime changed",
        )

    def open_workspace(self, run_id="r1", camp="c", quota=ramws.RUN_DEFAULT, **kw):
        if "statvfs" not in kw:
            kw["statvfs"] = fake_statvfs()
        if "root" not in kw:
            kw["root"] = str(self.ram_root)
        w = open_run(camp, run_id, quota, tool=TOOL, **kw)
        self.ws_list.append(w)
        return w

    def test_release_fifo_special_node_removal_defect(self):
        # B12/B13-FIFO: remove the node without opening either end of the pipe.
        def _target():
            with self.open_workspace("fifo_release_run") as ws:
                sub_dir = ws.path("sub/")
                fifo_path = sub_dir / "pipe.fifo"
                os.mkfifo(fifo_path)
                self.assertTrue(os.path.lexists(fifo_path))
                expected = os.lstat(fifo_path).st_blocks * 512
                before = ws.account()
                released = ws.receipt()["released_bytes"]
                with mock.patch.object(ramws.os, "open", side_effect=AssertionError("release opened a FIFO")), \
                     mock.patch("builtins.open", side_effect=AssertionError("release opened a FIFO")):
                    freed = ws.release("sub/pipe.fifo")
                self.assertFalse(
                    os.path.lexists(fifo_path),
                    "B12/B13-FIFO: release failed to remove FIFO node",
                )
                self.assertEqual(freed, expected)
                self.assertEqual(ws.used, max(0, before - expected))
                self.assertEqual(ws.receipt()["released_bytes"], released + expected)
                self.assertEqual(ws.release("sub/pipe.fifo"), 0)
                self.assertEqual(ws.used, max(0, before - expected))
                self.assertEqual(ws.receipt()["released_bytes"], released + expected)
                self.assertTrue(ws.root.is_dir())
            return "FIFO assertions completed"

        self.assertEqual(
            _run_in_child_with_timeout(_target, timeout=5.0),
            "FIFO assertions completed",
        )

    def test_release_terminal_symlinks_never_follows_targets(self):
        ws = self.open_workspace("release_links")
        for name, target in (("external_file", self.sentinel),
                             ("external_directory", self.outside_dir),
                             ("broken", self.outside_dir / "missing")):
            with self.subTest(name=name):
                link = ws.root / name
                link.symlink_to(target)
                expected = os.lstat(link).st_blocks * 512
                before = ws.account()
                released = ws.receipt()["released_bytes"]
                with mock.patch.object(ramws.os, "stat", side_effect=AssertionError("release followed a symlink")), \
                     mock.patch.object(ramws.os, "open", side_effect=AssertionError("release opened a symlink")), \
                     mock.patch("builtins.open", side_effect=AssertionError("release opened a symlink")):
                    self.assertEqual(ws.release(name), expected)
                self.assertFalse(os.path.lexists(link))
                self.assertEqual(ws.used, max(0, before - expected))
                self.assertEqual(ws.receipt()["released_bytes"], released + expected)
                self.assertEqual(ws.release(name), 0)
                self.assertEqual(ws.receipt()["released_bytes"], released + expected)
                self.assert_sentinel_intact()
        self.assertTrue(self.outside_dir.is_dir())
        self.assertFalse((self.outside_dir / "missing").exists())

    def test_release_regular_metadata_and_directory_accounting(self):
        ws = self.open_workspace("release_accounting")
        regular = ws.path("notes.txt")
        regular.write_text("inert metadata\n" * 128, encoding="utf-8")
        subtree = ws.path("sub/")
        child = subtree / "child.txt"
        child.write_text("nested inert metadata\n", encoding="utf-8")
        fifo = subtree / "pipe.fifo"
        os.mkfifo(fifo)
        link = subtree / "outside_link"
        link.symlink_to(self.sentinel)
        regular_bytes = os.lstat(regular).st_blocks * 512
        subtree_bytes = sum(os.lstat(p).st_blocks * 512 for p in (subtree, child, fifo, link))
        self.assertGreater(regular_bytes, 0)
        self.assertGreater(subtree_bytes, 0)
        before = ws.account()
        peak = ws.peak
        self.assertEqual(ws.release(regular), regular_bytes)
        self.assertFalse(regular.exists())
        self.assertEqual(ws.used, before - regular_bytes)
        self.assertEqual(ws.release("sub"), subtree_bytes)
        self.assertFalse(subtree.exists())
        self.assertEqual(ws.used, before - regular_bytes - subtree_bytes)
        self.assertEqual(ws.receipt()["released_bytes"], regular_bytes + subtree_bytes)
        self.assertEqual(ws.peak, peak)
        self.assertEqual(ws.release("sub"), 0)
        self.assertEqual(ws.release(regular), 0)
        self.assertEqual(ws.receipt()["released_bytes"], regular_bytes + subtree_bytes)
        self.assertEqual(ws.account(), before - regular_bytes - subtree_bytes)
        self.assertTrue(ws.root.is_dir())
        self.assert_sentinel_intact()

    def test_release_unlink_failure_keeps_accounting_and_node(self):
        ws = self.open_workspace("release_failure")
        regular = ws.path("notes.txt")
        regular.write_text("inert metadata\n", encoding="utf-8")
        expected = os.lstat(regular).st_blocks * 512
        self.assertGreater(expected, 0)
        before = ws.account()
        receipt = ws.receipt()
        with mock.patch.object(ramws.os, "unlink", side_effect=PermissionError("injected unlink refusal")):
            with self.assertRaisesRegex(PermissionError, "injected unlink refusal"):
                ws.release(regular)
        self.assertTrue(regular.exists())
        self.assertEqual(ws.used, before)
        self.assertEqual(ws.receipt()["released_bytes"], receipt["released_bytes"])
        self.assertEqual(ws.peak, receipt["ram_peak_bytes"])
        self.assertEqual(ws.release(regular), expected)
        self.assertFalse(regular.exists())
        self.assertEqual(ws.used, before - expected)
        self.assertEqual(ws.receipt()["released_bytes"], receipt["released_bytes"] + expected)

    def test_keep_frames_special_nodes(self):
        def _target():
            src_dir = self.ram_root / "src_ram"
            src_dir.mkdir(parents=True, exist_ok=True)
            build_dir = self.ram_root / "build_ram"
            build_dir.mkdir(parents=True, exist_ok=True)

            sub_dir = src_dir / "sub"
            sub_dir.mkdir(parents=True, exist_ok=True)

            fifo_path = sub_dir / "pipe.fifo"
            os.mkfifo(fifo_path)

            broken_symlink = src_dir / "broken_link"
            os.symlink(src_dir / "nonexistent", broken_symlink)

            ext_symlink = src_dir / "ext_link"
            os.symlink(self.sentinel, ext_symlink)

            regular_file = src_dir / "text_doc.txt"
            regular_file.write_text("eligible regular non-frame text", encoding="utf-8")

            res = keep_frames(
                src=src_dir,
                build=build_dir,
                run_id="run1",
                cap_mb=1,
                now=100,
                reason="test",
                argv_sha="00",
            )

            dst_dir = build_dir / "kept_frames" / "run1"
            self.assertTrue((dst_dir / "text_doc.txt").exists())
            self.assertEqual(
                (dst_dir / "text_doc.txt").read_text(encoding="utf-8"),
                "eligible regular non-frame text",
            )

            self.assertFalse(os.path.lexists(dst_dir / "sub" / "pipe.fifo"))
            self.assertFalse(os.path.lexists(dst_dir / "broken_link"))
            self.assertFalse(os.path.lexists(dst_dir / "ext_link"))

            self.assertEqual(res["cap_mb"], 1)
            self.assertFalse(res["kept_truncated"])
            self.assertEqual(res["reason"], "test")
            self.assertEqual(res["argv_sha"], "00")

        _run_in_child_with_timeout(_target, timeout=5.0)
        self.assert_sentinel_intact()

    def test_keep_frames_run_id_escape(self):
        src_dir = self.ram_root / "src_escape"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "dummy.txt").write_text("data", encoding="utf-8")
        build_dir = self.ram_root / "build_escape"

        unsafe_run_ids = [
            "../escape",
            "../../outside",
            "sub/run1",
            "a/b/c",
            "..",
            "/absolute",
            "run_id\0nul",
        ]

        for bad_id in unsafe_run_ids:
            with self.subTest(run_id=bad_id):
                with self.assertRaises(RamError) as ctx:
                    keep_frames(
                        src=src_dir,
                        build=build_dir,
                        run_id=bad_id,
                        cap_mb=1,
                        now=100,
                        reason="test",
                        argv_sha="00",
                    )
                self.assertEqual(ctx.exception.code, "E_RAM_ESCAPE")

        self.assert_sentinel_intact()


if __name__ == "__main__":
    unittest.main()
