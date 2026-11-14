"""STORAGE LAW v1 V1: vadapt.ramws contract. Frames only under unique /dev/shm/vqa-ramws/ramws_ut_* namespaces, removed in tearDown."""
import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from vadapt import ramws
from vadapt.ramws import RamError

ROOT = Path(__file__).resolve().parent.parent
TOOL = "ramws_ut"
BASE = Path(ramws.ROOT)
CHILD = ("import sys,time\nfrom vadapt import ramws\nws=ramws.open_run('c','live',tool=%r)\n"
         "ws.path('f/x.bin').write_bytes(b'x'*4096)\nprint('ready',flush=True)\nif len(sys.argv)>1: import os,signal; os.kill(os.getpid(),signal.SIGTERM)\ntime.sleep(60)\n")


def sv(free):
    return lambda p: types.SimpleNamespace(f_bavail=free, f_frsize=1)


def child(*extra):
    p = subprocess.Popen([sys.executable, "-B", "-c", CHILD % TOOL, *extra], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if p.stdout.readline().strip() != "ready":
        p.kill()
        raise AssertionError("child did not start")
    return p


class RamCase(unittest.TestCase):
    def setUp(self):
        namespace = "ramws_ut_" + uuid.uuid4().hex
        patcher = mock.patch(__name__ + ".TOOL", namespace)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ws = []
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
        for w in self.ws:
            w.close()
        shutil.rmtree(BASE / TOOL, ignore_errors=True)
        try:
            BASE.rmdir()
        except OSError:
            pass

    def open(self, run_id="r1", camp="c", quota=ramws.RUN_DEFAULT, **kw):
        w = ramws.open_run(camp, run_id, quota, tool=TOOL, **kw)
        self.ws.append(w)
        return w


class TestLocation(RamCase):
    def test_non_tmpfs_refused(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as t:
            self.assertIsNone(ramws.is_ram_fs(t))
            with self.assertRaises(RamError) as c:
                ramws.open_run("c", "r", tool=TOOL, root=t + "/ws")
            self.assertEqual(c.exception.code, "E_RAM_NOT_TMPFS")
            self.assertFalse(os.path.exists(t + "/ws"))

    def test_is_ram_fs_longest_prefix(self):
        mi = ("1 0 8:1 / / rw - ext4 /dev/sda1 rw\n2 1 0:5 / /dev/shm rw - tmpfs tmpfs rw\n"
              "3 2 8:2 / /dev/shm/disk rw - ext4 /dev/sdb rw\n4 1 0:6 / /mnt/a\\040b rw - ramfs none rw\n")
        with tempfile.NamedTemporaryFile("w", suffix=".mi") as f:
            f.write(mi)
            f.flush()
            self.assertEqual(ramws.is_ram_fs("/dev/shm/x/y", f.name), "tmpfs")
            self.assertIsNone(ramws.is_ram_fs("/dev/shm/disk/x", f.name))
            self.assertIsNone(ramws.is_ram_fs("/dev/shmx", f.name))
            self.assertEqual(ramws.is_ram_fs("/mnt/a b/z", f.name), "ramfs")

    def test_real_dev_shm_is_ram(self):
        self.assertIn(ramws.is_ram_fs("/dev/shm"), ("tmpfs", "ramfs"))
        w = self.open()
        self.assertTrue(str(w.root).startswith("/dev/shm/vqa-ramws/%s/c/r1" % TOOL))
        self.assertEqual(w.receipt()["fstype"], "tmpfs")
        owner = json.loads((w.root / "owner.json").read_text())
        self.assertEqual((owner["pid"], owner["tool"], owner["uid"]), (os.getpid(), TOOL, os.getuid()))
        self.assertTrue(owner["boot_id"] and owner["starttime"] > 0)

    def test_no_forbidden_imports(self):
        tree = ast.parse((ROOT / "vadapt" / "ramws.py").read_text())
        tops = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        tops |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and not n.level}
        self.assertFalse(tops & {"visioncore", "vmem", "numpy"})
        self.assertTrue(tops <= set(sys.stdlib_module_names))

    def test_bad_names_refused(self):
        for bad in ("..", "a/b", "", ".hidden"):
            with self.assertRaises(RamError):
                ramws.open_run(bad, "r", tool=TOOL)


class TestQuota(RamCase):
    def test_run_quota(self):
        w = self.open(quota=1000)
        w.reserve(600)
        with self.assertRaises(RamError) as c:
            w.reserve(401)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(w.used, 600)
        w.reserve(400)
        self.assertEqual(w.peak, 1000)

    def test_quota_hard_max(self):
        with self.assertRaises(RamError) as c:
            ramws.open_run("c", "big", (8 << 30) + 1, tool=TOOL)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.open("ok", quota=8 << 30)

    def test_host_sum(self):
        other = self.open("other")
        other.path("f.bin").write_bytes(os.urandom(300000))
        w = self.open("mine")
        old = ramws.HOST_MAX
        ramws.HOST_MAX = 250000
        try:
            with self.assertRaises(RamError) as c:
                w.reserve(1000)
            self.assertEqual(c.exception.code, "E_RAM_QUOTA")
            self.assertEqual(w.used, 0)
        finally:
            ramws.HOST_MAX = old
        w.reserve(1000)

    def test_statvfs_free(self):
        w = self.open(statvfs=sv(ramws.FREE_MIN + 999))
        w.reserve(999)
        with self.assertRaises(RamError) as c:
            w.reserve(1000)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertEqual(w.used, 999)

    def test_account_resync_and_quota(self):
        w = self.open(quota=200000)
        w.path("a.bin").write_bytes(b"\1" * 100000)
        self.assertGreaterEqual(w.account(), 100000)
        self.assertGreaterEqual(w.peak, 100000)
        w.path("b.bin").write_bytes(b"\1" * 150000)
        with self.assertRaises(RamError) as c:
            w.account()
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")


class TestPathRelease(RamCase):
    def test_escape(self):
        w = self.open()
        for bad in ("../x", "a/../../x", "/etc/passwd", "", "a\0b"):
            with self.assertRaises(RamError) as c:
                w.path(bad)
            self.assertEqual(c.exception.code, "E_RAM_ESCAPE", bad)
        out = Path(tempfile.mkdtemp(dir=ROOT / "build"))
        try:
            os.symlink(out, w.root / "link")
            with self.assertRaises(RamError) as c:
                w.path("link/x.bin")
            self.assertEqual(c.exception.code, "E_RAM_ESCAPE")
            self.assertEqual(os.listdir(out), [])
            (out / "keep.txt").write_text("x")
            with self.assertRaises(RamError):
                w.release("link/keep.txt")
            with self.assertRaises(RamError):
                w.release(out / "keep.txt")
            self.assertTrue((out / "keep.txt").exists())
        finally:
            shutil.rmtree(out)

    def test_path_makes_parents_and_dir_slash(self):
        w = self.open()
        p = w.path("t/a/s/B1/f.bin")
        self.assertTrue(p.parent.is_dir() and not p.exists())
        self.assertTrue(w.path("u/v/").is_dir())

    def test_release_accounting(self):
        w = self.open()
        w.path("u/a.bin").write_bytes(b"x" * 100000)
        w.path("u/b.bin").write_bytes(b"x" * 100000)
        w.account()
        before = w.used
        freed = w.release("u/a.bin")
        self.assertGreaterEqual(freed, 100000)
        self.assertEqual(w.used, before - freed)
        self.assertFalse((w.root / "u" / "a.bin").exists())
        freed2 = w.release(w.root / "u")
        self.assertGreater(freed2, 0)
        self.assertEqual(w.used, max(0, before - freed - freed2))
        self.assertEqual(w.release("nothing"), 0)
        self.assertEqual(w.receipt()["released_bytes"], freed + freed2)
        self.assertEqual(w.receipt()["ram_peak_bytes"], w.peak)
        with self.assertRaises(RamError):
            w.release(".")
        self.assertTrue(w.root.is_dir())

    def test_close_idempotent(self):
        w = self.open()
        w.path("a/b.bin").write_bytes(b"x" * 8192)
        root = w.root
        w.close()
        w.close()
        self.assertFalse(root.exists())
        self.assertFalse(root.parent.exists())
        self.assertGreaterEqual(w.receipt()["ram_peak_bytes"], 8192)
        with self.assertRaises(RamError):
            w.path("x")
        w2 = self.open()  # same ids can be reopened after close
        self.assertTrue(w2.root.is_dir())

    def test_context_manager(self):
        with ramws.open_run("c", "cm", tool=TOOL) as w:
            root = w.root
        self.assertFalse(root.exists())

    def test_swap_warning(self):
        w = self.open()
        self.assertNotIn("warnings", w.receipt())
        real = ramws._swap_used
        ramws._swap_used = lambda: w._swap0 + (2 << 30)
        try:
            self.assertEqual(w.receipt()["warnings"], ["W_RAM_SWAP"])
            self.assertEqual(w.receipt()["swap_used_delta"], 2 << 30)
        finally:
            ramws._swap_used = real


class TestJanitor(RamCase):
    def test_stale_vs_live_distinct_runs(self):
        stale = BASE / TOOL / "c" / "stale"
        stale.mkdir(parents=True)
        (stale / "f.bin").write_bytes(b"x" * 4096)
        (stale / ".lock").write_bytes(b"")
        (stale / "owner.json").write_text(json.dumps({"pid": 1, "starttime": 1, "boot_id": ramws._boot_id(), "tool": TOOL, "uid": os.getuid()}))
        os.utime(stale, (1, 1))
        live = child()
        self.procs.append(live)
        w = self.open("fresh")  # janitor runs first
        self.assertFalse(stale.exists())
        self.assertTrue((BASE / TOOL / "c" / "live" / "f" / "x.bin").exists())
        self.assertEqual(ramws.sweep(TOOL), [])
        self.assertTrue((BASE / TOOL / "c" / "live").is_dir())
        with self.assertRaises(RamError) as c:
            ramws.open_run("c", "live", tool=TOOL)
        self.assertEqual(c.exception.code, "E_RAM_LOCKED")
        live.kill()
        live.wait(timeout=10)
        self.assertEqual(ramws.sweep(TOOL), ["c/live"])
        self.assertTrue(w.root.is_dir())

    def test_sigkilled_owner_swept_by_cli(self):
        p = child()
        p.kill()
        p.wait(timeout=10)
        self.assertTrue((BASE / TOOL / "c" / "live").is_dir())
        r = subprocess.run([sys.executable, "-B", "-m", "vadapt.ramws", "sweep", "--tool", TOOL], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["removed"], ["c/live"])
        self.assertFalse((BASE / TOOL / "c" / "live").exists())

    def test_other_tool_and_uid_untouched(self):
        mk = lambda tool, name, owner: self._stale(tool, name, owner)  # noqa: E731
        try:
            # Simulate a prior interrupted setup inside this test's namespace.
            (BASE / (TOOL + "_other") / "k" / "a").mkdir(parents=True)
            a = mk(TOOL + "_other", "a", {"tool": TOOL + "_other", "uid": os.getuid()})
            b = mk(TOOL, "b", {"tool": TOOL, "uid": os.getuid() + 1})
            c = mk(TOOL, "c", {"tool": TOOL + "_other", "uid": os.getuid()})
            d = mk(TOOL, "d", {"tool": TOOL, "uid": os.getuid()})
            self.assertEqual(ramws.sweep(TOOL), ["k/d"])
            self.assertTrue(a.exists() and b.exists() and c.exists())
            self.assertFalse(d.exists())
        finally:
            shutil.rmtree(BASE / (TOOL + "_other"), ignore_errors=True)

    def _stale(self, tool, name, owner):
        d = BASE / tool / "k" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".lock").write_bytes(b"")
        (d / "owner.json").write_text(json.dumps(dict(owner, pid=1, starttime=1, boot_id=ramws._boot_id())))
        return d

    def test_stale_boot_removed(self):
        d = BASE / TOOL / "k" / "old"
        d.mkdir(parents=True)
        (d / "owner.json").write_text(json.dumps({"tool": TOOL, "uid": os.getuid(), "boot_id": "previous-boot", "pid": 1, "starttime": 1}))
        self.assertEqual(ramws.sweep(TOOL), ["k/old"])

    def test_sigterm_cleans_workspace(self):
        p = subprocess.Popen([sys.executable, "-B", "-c", CHILD % TOOL, "term"], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.procs.append(p)
        p.wait(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGTERM)
        self.assertFalse((BASE / TOOL / "c" / "live").exists())


class TestKeepAndDisk(RamCase):
    def test_sweep_kept_ttl(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as b:
            kf = Path(b) / "kept_frames"
            for name, ttl in (("old", 100), ("new", 10_000)):
                (kf / name).mkdir(parents=True)
                (kf / name / "KEEP.json").write_text(json.dumps({"ttl_until": ttl}))
            (kf / "nokeep").mkdir()
            (kf / "nokeep" / "a.png").write_bytes(b"x")
            (kf / "bad").mkdir()
            (kf / "bad" / "KEEP.json").write_text("{")
            self.assertEqual(ramws.sweep_kept(b, 5000), ["old"])
            self.assertEqual(sorted(os.listdir(kf)), ["bad", "new", "nokeep"])
            self.assertEqual(ramws.sweep_kept(b, 5000), [])
            self.assertEqual(ramws.sweep_kept(b + "/absent", 5000), [])

    def test_keep_frames_cap_ttl(self):
        w = self.open()
        for k in range(3):
            w.path("f/%d.npy" % k).write_bytes(b"\0" * (600 << 10))
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as b:
            k = ramws.keep_frames(w.root / "f", b, "r1", 1, now=1000, reason="t", argv_sha="ab", ttl_h=999)
            self.assertTrue(k["kept_truncated"] and k["bytes"] == 600 << 10 and k["cap_mb"] == 1)
            self.assertEqual(k["ttl_until"], 1000 + 72 * 3600)
            self.assertEqual(json.loads((Path(b) / "kept_frames/r1/KEEP.json").read_text()), k)
            self.assertEqual(ramws.sweep_kept(b, 1000 + 72 * 3600 + 1), ["r1"])
            self.assertEqual(ramws.keep_frames(w.root / "f", b, "r2", 9999, now=0, reason="t", argv_sha="")["cap_mb"], 256)

    def test_check_disk_allow_list(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as b:
            b = Path(b)
            for rel in ("rows.jsonl", "calib_report.json", "kept_frames/r/a.png", "fail_evidence/e/c.png", "x/y/f.PNG", "t/z.npy",
                        "m.mp4", "s.blend", "tiles.bin", "a.exr", "d/gops.bin"):
                (b / rel).parent.mkdir(parents=True, exist_ok=True)
                (b / rel).write_bytes(b"x")
            self.assertEqual(ramws.check_disk(b), ["a.exr", "d/gops.bin", "m.mp4", "s.blend", "t/z.npy", "tiles.bin", "x/y/f.PNG"])
            self.assertEqual(len(ramws.check_disk(b, allow=())), 9)
            stamp = os.stat(b / "a.exr").st_mtime_ns
            self.assertEqual(ramws.check_disk(b, since=stamp + 10**12), [])
            r = subprocess.run([sys.executable, "-B", "-m", "vadapt.ramws", "check-disk", str(b)], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 5)
            self.assertEqual(json.loads(r.stdout)["code"], "E_DISK_FRAMES")
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as b:
            (Path(b) / "rows.jsonl").write_text("{}")
            r = subprocess.run([sys.executable, "-B", "-m", "vadapt.ramws", "check-disk", b], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0)

    def test_cli_usage_and_status(self):
        w = self.open()
        r = subprocess.run([sys.executable, "-B", "-m", "vadapt.ramws", "status", "--tool", TOOL], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(r.stdout)
        self.assertEqual([(x["run_id"], x["live"]) for x in st["runs"]], [("r1", True)])  # flock held by this process
        self.assertEqual(st["fstype"], "tmpfs")
        self.assertTrue(w.root.is_dir())
        r = subprocess.run([sys.executable, "-B", "-m", "vadapt.ramws", "bogus"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
