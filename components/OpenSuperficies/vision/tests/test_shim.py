import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from vision.tests.helpers import REPO, SCRATCH

SHIM = str(REPO / "vision" / "ramws_shim.py")
ROOT = "/dev/shm/vqa-ramws"
TEST_ROOT = {"OS_CU_TEST_ROOT": "1"}
TEST_PREFIX = Path("/dev/shm/vqa-ramws-test")


def _keep(run, ttl_until=None, cap_mb=1):
    """synthetic KEEP.json as written by ramws.keep_frames."""
    keep = {"cap_mb": cap_mb, "bytes": 0, "ttl_until": int(time.time()) + 3600 if ttl_until is None else ttl_until,
            "reason": "test", "argv_sha": "0" * 64, "kept_truncated": False}
    (Path(run) / "KEEP.json").write_text(json.dumps(keep, sort_keys=True), encoding="utf-8")


def shim(args, stdin=None, timeout=60, extra_env=None):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra_env or {})
    return subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", SHIM] + args, cwd=str(REPO), env=env, input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def popen(args, extra_env=None):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra_env or {})
    return subprocess.Popen(["taskset", "-c", "0,1", sys.executable, "-B", SHIM] + args, cwd=str(REPO), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ask(p, obj):
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())


class ShimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from vision.tests.test_wheel_fixture import ensure_real_wheel
        ensure_real_wheel()

    def test_shim_open_path_under_ramws_root(self):
        p = popen(["session", "--run-id", "t-open-%d" % os.getpid()])
        try:
            hello = json.loads(p.stdout.readline())
            self.assertEqual(hello["code"], "OK")
            self.assertTrue(hello["root"].startswith(ROOT + "/opensuperficies/os-cu-0/"), hello)
            r = ask(p, {"cmd": "path", "rel": "frames.raw"})
            self.assertTrue(r["path"].startswith(hello["root"] + "/"), r)
            r = ask(p, {"cmd": "path", "rel": "../escape"})
            self.assertEqual(r["code"], "E_RAM_ESCAPE")
        finally:
            p.stdin.close()
            p.wait(timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(hello["root"]))

    def test_shim_session_holds_flock(self):
        rid = "t-lock-%d" % os.getpid()
        p = popen(["session", "--run-id", rid])
        try:
            hello = json.loads(p.stdout.readline())
            r = ask(p, {"cmd": "path", "rel": "a/frames.raw"})
            Path(r["path"]).write_bytes(b"x" * 4096)
            r = shim(["sweep"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(hello["root"]), "sweep deleted a live session run")
            r = ask(p, {"cmd": "account"})
            self.assertGreaterEqual(r["used"], 4096)
            r = ask(p, {"cmd": "receipt"})
            self.assertIn("ram_peak_bytes", r["receipt"])
            r = ask(p, {"cmd": "release", "rel": "a"})
            self.assertGreaterEqual(r["freed"], 4096)
            r = ask(p, {"cmd": "close"})
            self.assertEqual(r["code"], "OK")
        finally:
            p.stdin.close()
            p.wait(timeout=30)
        self.assertEqual(p.returncode, 0)

    def test_shim_session_closes_on_sigterm(self):
        p = popen(["session", "--run-id", "t-term-%d" % os.getpid()])
        hello = json.loads(p.stdout.readline())
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
        self.assertFalse(os.path.exists(hello["root"]))

    def test_shim_non_tmpfs_root_refused(self):
        d = SCRATCH / "disk_root"
        d.mkdir(parents=True, exist_ok=True)
        r = shim(["session", "--run-id", "t-nt-%d" % os.getpid(), "--root", str(d)], stdin="", extra_env=TEST_ROOT)
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))  # a disk root never reaches open_run (RJ-02 confinement)
        self.assertFalse((d / "opensuperficies").exists())

    def test_shim_sweep_called_on_open(self):
        TEST_PREFIX.mkdir(mode=0o700, exist_ok=True)
        root = TEST_PREFIX / ("l4test-%d" % os.getpid())
        stale = root / "opensuperficies" / "os-cu-0" / "stale-run"
        stale.mkdir(parents=True, mode=0o700)
        (stale / ".lock").write_text("", encoding="utf-8")
        (stale / "owner.json").write_text(json.dumps({"pid": 1, "tool": "opensuperficies", "uid": os.getuid(),
                                                      "boot_id": open("/proc/sys/kernel/random/boot_id").read().strip()}), encoding="utf-8")
        old = time.time() - 60
        os.utime(stale, (old, old))
        try:
            p = popen(["session", "--run-id", "t-sw-%d" % os.getpid(), "--root", str(root)], extra_env=TEST_ROOT)
            hello = json.loads(p.stdout.readline())
            self.assertEqual(hello["code"], "OK", hello)
            self.assertFalse(stale.exists(), "janitor did not run on open")
            p.stdin.close()
            p.wait(timeout=30)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_shim_no_rc1(self):
        for argv, stdin in ((["bogus"], None), ([], None), (["session"], ""), (["check-disk"], None),
                            (["session", "--run-id", "bad/id"], ""), (["session", "--run-id", "x", "--quota-bytes", "abc"], "")):
            r = shim(argv, stdin=stdin)
            self.assertIn(r.returncode, (2, 5), (argv, r.returncode, r.stderr))
            self.assertNotEqual(r.returncode, 1)

    def test_shim_check_disk_catches_png(self):
        d = SCRATCH / "cd"
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.png").write_bytes(b"x")
        r = shim(["check-disk", str(d)])
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn("x.png", r.stdout)
        (d / "x.png").unlink()
        (d / "frames.raw").write_bytes(b"x")
        (d / "Xvfb_screen0").write_bytes(b"x")
        r = shim(["check-disk", str(d)])
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn("frames.raw", r.stdout)
        self.assertIn("Xvfb_screen0", r.stdout)
        for f in d.iterdir():
            f.unlink()
        r = shim(["check-disk", str(d)])
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_shim_check_disk_catches_rgb24_and_linear(self):
        d = SCRATCH / "cd_rgb24"
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / "frames.rgb24").write_bytes(b"x")
            (d / "other.rgb24").write_bytes(b"x")
            r = shim(["check-disk", str(d)])
            self.assertEqual(r.returncode, 5, r.stdout)
            self.assertIn("frames.rgb24", r.stdout)
            self.assertIn("other.rgb24", r.stdout)
        finally:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()

    def test_shim_root_refused_without_test_env(self):
        for argv in (["session", "--run-id", "t-rf-%d" % os.getpid(), "--root", "/dev/shm"], ["sweep", "--root", "/dev/shm"],
                     ["status", "--root", "/dev/shm"]):
            r = shim(argv, stdin="")
            self.assertEqual(r.returncode, 2, (argv, r.stdout, r.stderr))
            self.assertIn("OS_CU_TEST_ROOT", r.stdout)
        self.assertFalse(os.path.exists("/dev/shm/opensuperficies"))

    def test_shim_check_disk_missing_dir_rc2(self):
        r = shim(["check-disk", "/nonexistent_fl4_dir"])
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("E_USAGE", r.stdout)

    def test_shim_check_disk_rejects_raw_in_kept(self):
        d = SCRATCH / "cd_kept"
        shutil.rmtree(d, ignore_errors=True)
        for sub in ("kept_frames", "sub/kept_frames", "fail_evidence/ep1", "sub/fail_evidence"):
            for name in ("leak.raw", "x.xwd", "frames.raw", "Xvfb_screen0"):
                (d / sub).mkdir(parents=True, exist_ok=True)
                (d / sub / name).write_bytes(b"x")
                r = shim(["check-disk", str(d)])
                self.assertEqual(r.returncode, 5, (sub, name, r.stdout))
                self.assertIn(name, r.stdout)
                (d / sub / name).unlink()
        r = shim(["check-disk", str(d)])
        self.assertEqual(r.returncode, 0, r.stdout)
        (d / "kept_frames" / "r1").mkdir(parents=True)
        _keep(d / "kept_frames" / "r1")
        (d / "kept_frames" / "r1" / "ok.png").write_bytes(b"x")  # png under kept_frames/<run> with live KEEP.json stays allowed
        self.assertEqual(shim(["check-disk", str(d)]).returncode, 0)

    def test_shim_check_disk_fail_evidence_caps(self):
        d = SCRATCH / "cd_fe"
        shutil.rmtree(d, ignore_errors=True)
        ep = d / "fail_evidence" / "ep1"
        ep.mkdir(parents=True)
        for i in range(6):
            (ep / ("f%d.png" % i)).write_bytes(b"x" * 1000)
        r = shim(["check-disk", str(d)])
        self.assertEqual(r.returncode, 0, r.stdout)  # 6 files, 6 KB: at the limits
        (ep / "f6.png").write_bytes(b"x")
        r = shim(["check-disk", str(d)])
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn("fail_evidence/ep1", r.stdout)
        (ep / "f6.png").unlink()
        (ep / "f0.png").write_bytes(b"x" * (100 * 1024 + 1))
        r = shim(["check-disk", str(d)])
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn("fail_evidence/ep1", r.stdout)
        (ep / "f0.png").write_bytes(b"x" * 1000)
        self.assertEqual(shim(["check-disk", str(d)]).returncode, 0)

    def test_shim_test_root_confined(self):
        """RJ-02 / ADC2-03(2): OS_CU_TEST_ROOT=1 alone confines nothing; --root must resolve under /dev/shm/vqa-ramws-test/."""
        TEST_PREFIX.mkdir(mode=0o700, exist_ok=True)
        link = TEST_PREFIX / ("esc-link-%d" % os.getpid())
        esc = Path("/dev/shm/x4esc-%d" % os.getpid())
        try:
            os.symlink(str(esc), str(link))
            for root in (str(esc), "/run/user/%d" % os.getuid(), str(TEST_PREFIX), str(link), str(TEST_PREFIX / ".." / "x4dotdot")):
                for argv in (["session", "--run-id", "t-cf-%d" % os.getpid(), "--root", root], ["sweep", "--root", root],
                             ["status", "--root", root]):
                    r = shim(argv, stdin="", extra_env=TEST_ROOT)
                    self.assertEqual(r.returncode, 2, (argv, r.stdout, r.stderr))
                    self.assertIn("vqa-ramws-test", r.stdout)
            self.assertFalse(esc.exists(), "session opened outside the test prefix")
            self.assertFalse(os.path.exists("/run/user/%d/opensuperficies" % os.getuid()))
        finally:
            if os.path.islink(str(link)):
                os.unlink(str(link))
            shutil.rmtree(esc, ignore_errors=True)

    def test_shim_check_disk_nested_kept_and_fail_evidence_rejected(self):
        """ADC2-03(1): frame kinds are allowed only in top-level kept_frames/<run> (live KEEP.json) and fail_evidence/<ep>."""
        d = SCRATCH / "cd_nested"
        shutil.rmtree(d, ignore_errors=True)
        try:
            for rel in ("sub/kept_frames/leak.png", "sub/kept_frames/r1/leak.mp4", "sub/fail_evidence/ep1/leak.npy",
                        "a/b/fail_evidence/leak.png", "kept_frames/loose.png", "fail_evidence/loose.png"):
                f = d / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                if "kept_frames" in rel:
                    _keep(f.parent)  # a live KEEP.json never legitimises a nested or loose kept_frames frame
                f.write_bytes(b"x")
                r = shim(["check-disk", str(d)])
                self.assertEqual(r.returncode, 5, (rel, r.stdout))
                self.assertIn(rel, json.loads(r.stdout)["offending"])
                f.unlink()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_shim_check_disk_kept_requires_live_keep(self):
        """ADC2-03(1): top-level kept_frames/<run> frames need a readable, unexpired KEEP.json and stay within cap_mb."""
        d = SCRATCH / "cd_keep"
        shutil.rmtree(d, ignore_errors=True)
        run = d / "kept_frames" / "r1"
        run.mkdir(parents=True)
        try:
            (run / "ok.png").write_bytes(b"x")
            r = shim(["check-disk", str(d)])
            self.assertEqual(r.returncode, 5, ("no KEEP.json", r.stdout))
            self.assertIn("kept_frames/r1/ok.png", r.stdout)
            (run / "KEEP.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(shim(["check-disk", str(d)]).returncode, 5)
            _keep(run, ttl_until=int(time.time()) - 10)
            self.assertEqual(shim(["check-disk", str(d)]).returncode, 5, "expired KEEP.json accepted")
            _keep(run)
            r = shim(["check-disk", str(d)])
            self.assertEqual(r.returncode, 0, ("live KEEP.json", r.stdout))
            _keep(run, cap_mb=1)
            (run / "big.npy").write_bytes(b"x" * ((1 << 20) + 1))
            r = shim(["check-disk", str(d)])
            self.assertEqual(r.returncode, 5, ("over cap_mb", r.stdout))
            self.assertIn("kept_frames/r1", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_shim_unforeseen_error_rc5_not_1(self):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        code = ("import sys\nsys.path.insert(0, %r)\nfrom vision import ramws_shim as s\n"
                "def boom(*a, **k):\n    raise KeyError('boom')\ns.ramws.status = boom\nsys.exit(s.main(['status']))\n") % str(REPO)
        r = subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", "-c", code], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 5, (r.stdout, r.stderr))
        self.assertIn("E_GATE", r.stdout)


if __name__ == "__main__":
    unittest.main()
