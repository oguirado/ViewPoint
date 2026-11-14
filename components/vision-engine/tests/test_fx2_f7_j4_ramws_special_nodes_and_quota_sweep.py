"""Tests for RamWorkspace special nodes (bound sockets, mixed socket/FIFO directories with symlinks),
quota refusal-release-retry composition, keep_frames capped sorted-prefix accounting, and
sweep_kept TTL boundary conditions.

Owner ID for failing contracts: fx2-f7-rtring-ramws-retry-and-release
VM timing: VM
"""
import json
import os
import shutil
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from vadapt import ramws
from vadapt.ramws import RamError, keep_frames, open_run, sweep_kept

TOOL = "fx2_f7_j4_ramws_ut"
RAM_BASE = Path("/dev/shm/vqa-ramws-j4-ut")


def fake_statvfs(free_bytes=ramws.FREE_MIN + (1 << 30)):
    return lambda p: types.SimpleNamespace(f_bavail=free_bytes, f_frsize=1)


class BoundSocketFixture:
    """Offline bind/close facade with socket metadata and an inert empty node.

    Closing a UNIX socket does not unlink its bound path. The marker has socket
    mode in both stat and lstat, so isfile/isdir/islink cannot accidentally make
    the former regular-file-only release implementation succeed.
    """

    def __init__(self, nodes):
        self.nodes = nodes
        self.bound = None
        self.closed = False

    def bind(self, path):
        if self.closed or self.bound is not None:
            raise RuntimeError("socket fixture is closed or already bound")
        path = os.path.abspath(os.fspath(path))
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        self.bound = path
        self.nodes.add(path)

    def close(self):
        self.closed = True


class TestRamwsSpecialNodesAndQuotaSweep(unittest.TestCase):
    def setUp(self):
        self.assertIn(
            ramws.is_ram_fs("/dev/shm"), ramws.RAM_FS,
            "special-node fixture requires verified RAM storage",
        )

        self.ram_root = RAM_BASE / str(os.getpid())
        shutil.rmtree(self.ram_root, ignore_errors=True)
        self.ram_root.mkdir(parents=True, exist_ok=True)

        self.outside_dir = Path(tempfile.mkdtemp(prefix="j4_special_outside_"))
        self.sentinel = self.outside_dir / "sentinel.txt"
        self.sentinel.write_text("SENTINEL_INTACT", encoding="utf-8")
        self.sentinel_mtime = self.sentinel.stat().st_mtime_ns
        self.ws_list = []
        self.socket_nodes = set()
        real_stat, real_lstat = os.stat, os.lstat

        def socket_metadata(real, path, *args, **kwargs):
            observed = real(path, *args, **kwargs)
            if isinstance(path, (str, bytes, os.PathLike)):
                key = os.path.abspath(os.fsdecode(os.fspath(path)))
                if key in self.socket_nodes:
                    values = {
                        name: getattr(observed, name)
                        for name in dir(observed) if name.startswith("st_")
                    }
                    values.update(st_mode=stat.S_IFSOCK | 0o600, st_size=0, st_blocks=0)
                    return types.SimpleNamespace(**values)
            return observed

        for name, real in (("stat", real_stat), ("lstat", real_lstat)):
            patched = mock.patch.object(
                os, name,
                side_effect=lambda path, *a, _real=real, **kw: socket_metadata(
                    _real, path, *a, **kw
                ),
            )
            patched.start()
            self.addCleanup(patched.stop)

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
        self.assertTrue(self.sentinel.exists(), "External sentinel file was deleted")
        self.assertEqual(
            self.sentinel.read_text(encoding="utf-8"),
            "SENTINEL_INTACT",
            "External sentinel content mutated",
        )
        self.assertEqual(
            self.sentinel.stat().st_mtime_ns,
            self.sentinel_mtime,
            "External sentinel mtime modified",
        )

    def open_workspace(self, run_id="r1", camp="c", quota=ramws.RUN_DEFAULT, **kw):
        if "statvfs" not in kw:
            kw["statvfs"] = fake_statvfs()
        if "root" not in kw:
            kw["root"] = str(self.ram_root)
        w = open_run(camp, run_id, quota, tool=TOOL, **kw)
        self.ws_list.append(w)
        return w

    def test_release_bound_socket_node_removal_defect(self):
        # Owner ID: fx2-f7-rtring-ramws-retry-and-release
        # Regression: the former islink/isfile branch skipped a bound socket.
        # Current release classifies the node itself with lstat and unlinks it.
        ws = self.open_workspace("socket_release_run")
        sock_path = ws.path("active_domain.sock")
        srv = BoundSocketFixture(self.socket_nodes)
        self.addCleanup(srv.close)
        srv.bind(str(sock_path))
        self.assertTrue(stat.S_ISSOCK(os.lstat(sock_path).st_mode))
        self.assertTrue(stat.S_ISSOCK(os.stat(sock_path).st_mode))
        self.assertFalse(sock_path.is_file())
        self.assertFalse(sock_path.is_dir())
        self.assertFalse(sock_path.is_symlink())
        self.assertTrue(os.path.lexists(sock_path))

        try:
            ws.release("active_domain.sock")
            self.assertFalse(
                os.path.lexists(sock_path),
                "fx2-f7-rtring-ramws-retry-and-release: release failed to remove bound UNIX socket node",
            )
        finally:
            srv.close()

    def test_release_mixed_special_node_directory_and_sentinel_safety(self):
        ws = self.open_workspace("mixed_release_run")
        mix_dir = ws.path("mix/")

        # 1. Bound socket inside mix_dir
        sock_path = mix_dir / "service.sock"
        srv = BoundSocketFixture(self.socket_nodes)
        self.addCleanup(srv.close)
        srv.bind(str(sock_path))
        self.assertTrue(stat.S_ISSOCK(os.lstat(sock_path).st_mode))
        self.assertTrue(stat.S_ISSOCK(os.stat(sock_path).st_mode))
        self.assertFalse(sock_path.is_file())
        self.assertFalse(sock_path.is_dir())
        self.assertFalse(sock_path.is_symlink())

        # 2. FIFO inside mix_dir
        fifo_path = mix_dir / "channel.fifo"
        os.mkfifo(fifo_path)

        # 3. External sentinel symlink inside mix_dir
        symlink_path = mix_dir / "external_sentinel.link"
        os.symlink(self.sentinel, symlink_path)

        # 4. Regular data file inside mix_dir
        data_path = mix_dir / "payload.dat"
        data_path.write_bytes(b"A" * 4096)

        try:
            ws.account()
            used_before = ws.used

            freed = ws.release("mix")
            self.assertFalse(os.path.lexists(mix_dir), "Mixed directory was not removed by release")
            self.assert_sentinel_intact()

            # Ensure single accounting adjustment with no double credit
            self.assertEqual(
                ws.used,
                max(0, used_before - freed),
                "Accounting mismatch after releasing mixed special-node directory",
            )
        finally:
            srv.close()

    def test_quota_refusal_release_and_successful_retry(self):
        quota = 10_000
        ws = self.open_workspace("quota_retry_run", quota=quota)

        # Initial reservation
        ws.reserve(7_000)
        self.assertEqual(ws.used, 7_000)
        self.assertEqual(ws.peak, 7_000)

        # Quota refusal: 7,000 + 4,000 > 10,000
        with self.assertRaises(RamError) as ctx:
            ws.reserve(4_000)
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")
        # Refusal must keep state atomic and unchanged
        self.assertEqual(ws.used, 7_000)

        # Create a file and release it to reclaim workspace accounting space
        junk_path = ws.path("temp_junk.bin")
        junk_path.write_bytes(b"X" * 3_000)
        ws.account()
        used_peak = ws.used

        freed = ws.release("temp_junk.bin")
        self.assertGreater(freed, 0)
        self.assertLess(ws.used, used_peak)

        # Retry reservation now succeeds after release
        ws.reserve(2_000)
        self.assertLessEqual(ws.used, quota)

    def test_keep_frames_capped_sorted_prefix_and_inert_metadata(self):
        src_dir = self.ram_root / "src_keep"
        src_dir.mkdir(parents=True, exist_ok=True)
        build_dir = self.ram_root / "build_keep"
        build_dir.mkdir(parents=True, exist_ok=True)

        # Subdirectory with inert text metadata files
        sub_dir = src_dir / "meta"
        sub_dir.mkdir(parents=True, exist_ok=True)

        file_a = sub_dir / "01_hdr.json"
        file_a.write_text('{"type": "header"}', encoding="utf-8")  # 18 bytes
        file_b = sub_dir / "02_log.txt"
        file_b.write_text("Log line metadata entry\n", encoding="utf-8")  # 23 bytes
        file_c = sub_dir / "03_tail.json"
        file_c.write_text('{"type": "tail"}', encoding="utf-8")  # 16 bytes

        # Mock shutil.copyfile to trace sorted-prefix copies without requiring frame files
        copied_files = []
        real_copyfile = shutil.copyfile

        def mock_copyfile(src, dst, *args, **kwargs):
            copied_files.append(Path(src).name)
            return real_copyfile(src, dst, *args, **kwargs)

        with mock.patch("shutil.copyfile", side_effect=mock_copyfile):
            # cap_mb=1 (1 MiB cap)
            keep_res = keep_frames(
                src=src_dir,
                build=build_dir,
                run_id="run_keep_1",
                cap_mb=1,
                now=5000,
                reason="unit_test_keep",
                argv_sha="sha256_mock_123",
                ttl_h=12,
            )

        # Verify sorted order prefix copying
        self.assertEqual(copied_files, ["01_hdr.json", "02_log.txt", "03_tail.json"])
        dst_dir = build_dir / "kept_frames" / "run_keep_1"
        self.assertTrue((dst_dir / "KEEP.json").exists())

        keep_meta = json.loads((dst_dir / "KEEP.json").read_text(encoding="utf-8"))
        self.assertEqual(keep_meta["cap_mb"], 1)
        self.assertFalse(keep_meta["kept_truncated"])
        self.assertEqual(keep_meta["reason"], "unit_test_keep")
        self.assertEqual(keep_meta["argv_sha"], "sha256_mock_123")
        self.assertEqual(keep_meta["ttl_until"], 5000 + 12 * 3600)

    def test_sweep_kept_expiry_equality_and_immediately_afterward(self):
        build_dir = self.ram_root / "build_sweep_kept"
        kept_base = build_dir / "kept_frames"

        # 1. Directory with ttl_until = 1000
        run_1000 = kept_base / "run_ttl_1000"
        run_1000.mkdir(parents=True, exist_ok=True)
        (run_1000 / "KEEP.json").write_text(
            json.dumps({"ttl_until": 1000, "reason": "test"}), encoding="utf-8"
        )

        # 2. Directory with ttl_until = 2000
        run_2000 = kept_base / "run_ttl_2000"
        run_2000.mkdir(parents=True, exist_ok=True)
        (run_2000 / "KEEP.json").write_text(
            json.dumps({"ttl_until": 2000, "reason": "test"}), encoding="utf-8"
        )

        # At now = 1000 (expiry equality):
        # Contract is `ttl_until < now`. Since 1000 < 1000 is False, run_ttl_1000 must NOT be swept.
        gone_equal = sweep_kept(build_dir, now=1000)
        self.assertEqual(gone_equal, [], "sweep_kept deleted run at ttl_until == now")
        self.assertTrue(run_1000.exists())
        self.assertTrue(run_2000.exists())

        # At now = 1000.001 / 1001 (immediately afterward):
        # Contract 1000 < 1001 is True, so run_ttl_1000 MUST be swept.
        gone_after = sweep_kept(build_dir, now=1001)
        self.assertEqual(gone_after, ["run_ttl_1000"])
        self.assertFalse(run_1000.exists())
        self.assertTrue(run_2000.exists())


if __name__ == "__main__":
    unittest.main()
