"""ER-10: open_run(share_with_uid / share_group). Frames only under /dev/shm/vqa-ramws/ramws_share_ut/, removed in tearDown."""
import json
import os
import shutil
import stat
import subprocess
import unittest
from unittest import mock

from vadapt import ramws
from vadapt.ramws import RamError

TOOL = "ramws_share_ut"
UID = 20001


def getfacl(p):
    return subprocess.run(["getfacl", "-cpn", str(p)], capture_output=True, text=True, check=True).stdout.split()


class ShareTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.path.join(ramws.ROOT, TOOL), ignore_errors=True)
        self.ws = None

    def tearDown(self):
        if self.ws:
            self.ws.close()
        shutil.rmtree(os.path.join(ramws.ROOT, TOOL), ignore_errors=True)

    def test_default_unchanged(self):
        self.ws = ramws.open_run("c", "r1", tool=TOOL)
        self.assertNotIn("share", self.ws.receipt())
        self.assertEqual(set(self.ws.receipt()), {"ram_peak_bytes", "quota", "released_bytes", "swap_used_delta", "fstype"})
        for d in (self.ws.root, self.ws.root.parent, self.ws.root.parent.parent):  # run, campaign, tool (root pre-exists); F-03
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
            self.assertFalse(any(l.startswith("user:") and not l.startswith("user::") for l in getfacl(d)))
        self.assertEqual(getfacl(self.ws.root), ["user::rwx", "group::---", "other::---"])

    def test_acl_present_on_run_and_parents(self):
        self.ws = ramws.open_run("c", "r2", tool=TOOL, share_with_uid=UID)
        self.assertEqual(self.ws.receipt()["share"], {"uid": UID, "method": "acl", "files_read": [".lock", "owner.json"]})
        json.dumps(self.ws.receipt())
        run = self.ws.root
        self.assertIn("user:%d:rwx" % UID, getfacl(run))
        self.assertIn("default:user:%d:rwx" % UID, getfacl(run))
        for d in (run.parent, run.parent.parent, run.parent.parent.parent):
            self.assertIn("user:%d:--x" % UID, getfacl(d))
        self.assertEqual(stat.S_IMODE(run.stat().st_mode) & 0o077 & ~0o070, 0)  # no other bits
        self.assertIn("group::---", getfacl(run))
        sub = self.ws.path("a/b/")  # default ACL is inherited
        self.assertIn("user:%d:rwx" % UID, getfacl(sub))
        self.assertTrue(sub.is_dir())

    @unittest.skipUnless(os.getuid() == 0, "needs root to setuid")
    def test_other_uid_can_write(self):
        self.ws = ramws.open_run("c", "r3", tool=TOOL, share_with_uid=UID)
        pid = os.fork()
        if pid == 0:
            try:
                os.setgid(UID), os.setuid(UID)
                (self.ws.root / "x.bin").write_bytes(b"x")
                os._exit(0)
            except BaseException:
                os._exit(1)
        self.assertEqual(os.waitpid(pid, 0)[1], 0)

    def test_acl_unsupported_no_group_fails_closed(self):
        with mock.patch.object(ramws, "_setfacl", side_effect=OSError(95, "Operation not supported")):
            with self.assertRaises(RamError) as c:
                ramws.open_run("c", "r4", tool=TOOL, share_with_uid=UID)
        self.assertEqual(c.exception.code, "E_RAM_SHARE")
        self.assertFalse((ramws.Path(ramws.ROOT) / TOOL / "c" / "r4").exists())
        self.assertFalse(ramws._OPEN)

    def test_setfacl_missing_fails_closed(self):
        with mock.patch.object(ramws.subprocess, "run", side_effect=FileNotFoundError("setfacl")):
            with self.assertRaises(RamError) as c:
                ramws.open_run("c", "r5", tool=TOOL, share_with_uid=UID)
        self.assertEqual(c.exception.code, "E_RAM_SHARE")

    def test_group_fallback(self):
        gid = os.getgid()
        with mock.patch.object(ramws, "_setfacl", side_effect=OSError(95, "unsupported")):
            self.ws = ramws.open_run("c", "r6", tool=TOOL, share_with_uid=UID, share_group=gid)
        self.assertEqual(self.ws.receipt()["share"], {"gid": gid, "method": "group", "files_read": [".lock", "owner.json"]})
        run = self.ws.root
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o3770)
        self.assertEqual(run.stat().st_gid, gid)
        for d in (run.parent, run.parent.parent):  # created by this open_run
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o710)
            self.assertEqual(d.stat().st_gid, gid)

    def test_group_only(self):
        self.ws = ramws.open_run("c", "r7", tool=TOOL, share_group=os.getgid())
        self.assertEqual(self.ws.receipt()["share"]["method"], "group")

    def test_group_chown_denied_fails_closed(self):
        root = self._nested_root(0o700)
        with mock.patch.object(ramws.os, "chown", side_effect=PermissionError(1, "denied")):
            with self.assertRaises(RamError) as c:
                ramws.open_run("c", "r8", tool="t", root=str(root), share_group=12345)
        self.assertEqual(c.exception.code, "E_RAM_SHARE")
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)  # a refused share leaves pre-existing ancestors as they were

    def test_bad_ids(self):
        for bad in (-1, True, "1000", 1 << 40):
            with self.assertRaises(RamError) as c:
                ramws.open_run("c", "r9", tool=TOOL, share_with_uid=bad)
            self.assertEqual(c.exception.code, "E_RAM_SHARE")

    def test_janitor_and_close_remove_shared_dirs(self):
        self.ws = ramws.open_run("c", "r10", tool=TOOL, share_with_uid=UID)
        self.ws.path("f/x.bin").write_bytes(b"x" * 4096)
        plain = ramws.open_run("c", "plain", tool=TOOL)
        plain.path("f/x.bin").write_bytes(b"x" * 4096)
        self.assertEqual(self.ws.account(), plain.account())  # accounting unchanged
        plain.close()
        run = self.ws.root
        self.ws.close()
        self.assertFalse(run.exists())
        self.ws = None
        # janitor: a dead shared run (lock free) is swept
        r = ramws.open_run("c", "dead", tool=TOOL, share_with_uid=UID)
        dead = r.root
        os.close(r._fd)
        r._closed = True
        ramws._OPEN.pop(r, None)
        self.assertEqual(ramws.sweep(TOOL), ["c/dead"])
        self.assertFalse(dead.exists())

    # ---- FXER10 (ADV_ER10 F-01..F-05): each test fails on the ER10 code and passes after the fix ----
    def _nested_root(self, mode=0o700, gid=None):
        r = ramws.Path(ramws.ROOT) / TOOL / "nroot"
        r.mkdir(parents=True, mode=0o700)
        if gid is not None:
            os.chown(r, -1, gid)
        os.chmod(r, mode)
        return r

    def test_f01_default_acl_names_host_owner(self):
        # mutant: default ACL without u:<host uid>:rwx -> container-created files/subdirs are other::--- for the host
        self.ws = ramws.open_run("c", "f01", tool=TOOL, share_with_uid=UID)
        acl = getfacl(self.ws.root)
        self.assertIn("default:user:%d:rwx" % os.getuid(), acl)
        self.assertIn("default:user:%d:rwx" % UID, acl)
        sub = self.ws.path("d/")
        self.assertIn("user:%d:rwx" % os.getuid(), getfacl(sub))  # inherited: survives a container-owned subdir
        self.assertIn("default:user:%d:rwx" % os.getuid(), getfacl(sub))

    def test_f02_lock_owner_before_share_and_sticky(self):
        # mutant: _apply_share before .lock/owner.json, or no sticky bit on the run dir
        seen = {}
        real = ramws._apply_share

        def spy(run, *a, **k):
            seen["lock"] = (run / ".lock").is_file()
            seen["owner_mode"] = stat.S_IMODE((run / "owner.json").stat().st_mode)
            return real(run, *a, **k)
        with mock.patch.object(ramws, "_apply_share", spy):
            self.ws = ramws.open_run("c", "f02", tool=TOOL, share_with_uid=UID)
        self.assertEqual(seen, {"lock": True, "owner_mode": 0o600})
        self.assertTrue(self.ws.root.stat().st_mode & stat.S_ISVTX)
        self.ws.close()
        self.ws = ramws.open_run("c", "f02g", tool=TOOL, share_group=os.getgid())
        self.assertTrue(self.ws.root.stat().st_mode & stat.S_ISVTX)
        self.assertEqual(stat.S_IMODE((self.ws.root / "owner.json").stat().st_mode), 0o640)  # ER-011: group-readable, not writable

    def test_f02_forged_owner_cannot_make_janitor_kill_live_run(self):
        # mutant: _owner follows a planted owner.json symlink -> forged boot_id -> sweep rmtree's a LIVE locked run
        self.ws = ramws.open_run("c", "f02l", tool=TOOL, share_with_uid=UID)
        run = self.ws.root
        forged = ramws.Path(ramws.ROOT) / TOOL / "forged.json"
        forged.write_text(json.dumps({"boot_id": "forged", "tool": TOOL, "uid": os.getuid()}))
        os.chmod(run, 0o700)  # simulate a pre-fix swap of owner.json (sticky now forbids it for the sharing uid)
        (run / "owner.json").unlink()
        (run / "owner.json").symlink_to(forged)
        self.assertEqual(ramws.sweep(TOOL), [])
        self.assertTrue((run / ".lock").exists())

    def test_f02_lock_failure_rolls_back_run(self):
        # mutant: flock failure leaves the run dir behind
        with mock.patch.object(ramws.fcntl, "flock", side_effect=OSError(11, "busy")):
            with self.assertRaises(RamError) as c:
                ramws.open_run("c", "f02r", tool=TOOL, share_with_uid=UID)
        self.assertEqual(c.exception.code, "E_RAM_LOCKED")
        self.assertFalse((ramws.Path(ramws.ROOT) / TOOL / "c" / "f02r").exists())

    def test_f03_group_never_rewrites_preexisting_ancestor_gid(self):
        # mutant: chown/chmod 0o710 on every ancestor (breaks a concurrent run shared with another group)
        other = [g for g in os.getgroups() if g != os.getgid()]
        if not other:
            self.skipTest("needs a second group")
        a, b = os.getgid(), other[0]
        root = self._nested_root(0o710, a)
        self.ws = ramws.open_run("c", "f03", tool="t", root=str(root), share_group=b)
        st = root.stat()
        self.assertEqual((st.st_gid, stat.S_IMODE(st.st_mode)), (a, 0o711))  # gid kept, only o+x added
        self.assertEqual(self.ws.root.stat().st_gid, b)

    def test_f03_acl_skips_traversable_root_and_sweep_drops_tool_dir(self):
        # mutant: setfacl on an already-traversable root; sweep() never removes the empty tool dir
        root = self._nested_root(0o755)
        r = ramws.open_run("c", "dead", tool="t", root=str(root), share_with_uid=UID)
        self.assertFalse(any(l.startswith("user:%d" % UID) for l in getfacl(root)))
        os.close(r._fd)
        r._closed = True
        ramws._OPEN.pop(r, None)
        self.assertEqual(ramws.sweep("t", str(root)), ["c/dead"])
        self.assertFalse((root / "t").exists())

    def test_f04_parent_acl_failure_leaves_no_acl_for_group_fallback(self):
        # mutant: run ACL (d:g::---) applied before a parent fails, then group fallback without setfacl -b
        real = ramws._setfacl

        def fake(args, path):
            if ramws.Path(path).name != "f04a":
                raise OSError(95, "parent refuses ACL")
            real(args, path)
        with mock.patch.object(ramws, "_setfacl", fake):
            self.ws = ramws.open_run("c", "f04a", tool=TOOL, share_with_uid=UID, share_group=os.getgid())
        self.assertEqual(self.ws.receipt()["share"]["method"], "group")
        self.assertFalse([l for l in getfacl(self.ws.root) if l.startswith(("default:", "user:%d" % UID))])

    def test_f04_run_acl_failure_is_wiped_before_group_fallback(self):
        # mutant: no setfacl -b after a failure on the run dir itself
        real = ramws._setfacl

        def fake(args, path):
            real(args, path)
            if args != ["-b"] and ramws.Path(path).name == "f04b":
                raise OSError(1, "late failure")
        with mock.patch.object(ramws, "_setfacl", fake):
            self.ws = ramws.open_run("c", "f04b", tool=TOOL, share_with_uid=UID, share_group=os.getgid())
        self.assertEqual(self.ws.receipt()["share"]["method"], "group")
        self.assertFalse([l for l in getfacl(self.ws.root) if l.startswith(("default:", "user:%d" % UID))])
        self.assertEqual(stat.S_IMODE(self.ws.root.stat().st_mode), 0o3770)

    def test_f04_acl_rollback_failure_fails_closed(self):
        def fail_run_acl(args, path):
            if ramws.Path(path).name == "f04c":
                raise OSError(1, "ACL application refused")

        with (
            mock.patch.object(ramws, "_setfacl", fail_run_acl),
            mock.patch.object(
                ramws, "_restore_acl", side_effect=OSError(1, "ACL restoration refused")
            ) as restore,
            mock.patch.object(ramws, "_share_group") as fallback,
        ):
            with self.assertRaises(RamError) as caught:
                ramws.open_run("c", "f04c", tool=TOOL, share_with_uid=UID, share_group=os.getgid())
            restore.assert_called_once()
            fallback.assert_not_called()
        self.assertEqual(caught.exception.code, "E_RAM_SHARE")
        self.assertFalse((ramws.Path(ramws.ROOT) / TOOL / "c" / "f04c").exists())

    def test_f04_group_subdirs_group_writable(self):
        # mutant: ws.path() mkdir under the host umask -> 0o2755 subdirs in share_group mode
        old = os.umask(0o022)
        try:
            self.ws = ramws.open_run("c", "f04d", tool=TOOL, share_group=os.getgid())
            sub = self.ws.path("a/b/")
            self.ws.path("e/f.bin")
        finally:
            os.umask(old)
        for d in (sub, sub.parent, self.ws.root / "e"):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o2770)

    def test_f05_setfacl_double_dash_and_realpath_root(self):
        # mutants: no '--' before the path; unresolved root passed to mkdir/sweep/share
        with mock.patch.object(ramws.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as m:
            ramws._setfacl(["-m", "u:1:--x"], "/x")
        self.assertEqual(m.call_args[0][0][-2:], ["--", "/x"])
        real = self._nested_root(0o700)
        link = real.parent / "lroot"
        link.symlink_to(real)
        self.ws = ramws.open_run("c", "f05", tool="t", root=str(link), share_with_uid=UID)
        self.assertEqual(self.ws.root, real / "t" / "c" / "f05")

    # ---- ER-011: read-only ACL on .lock / owner.json, foreign LOCK_SH vs the janitor ----
    def test_er11_read_acl_on_lock_and_owner(self):
        self.ws = ramws.open_run("c", "e1", tool=TOOL, share_with_uid=UID)
        for n in (".lock", "owner.json"):
            acl = getfacl(self.ws.root / n)
            self.assertIn("user:%d:r--" % UID, acl)
            self.assertIn("mask::r--", acl)
            self.assertFalse([l for l in acl if l.startswith("user:%d:" % UID) and "w" in l])  # never a w bit for the uid
            self.assertEqual(stat.S_IMODE((self.ws.root / n).stat().st_mode) & 0o007, 0)
        self.assertEqual(self.ws.receipt()["share"]["files_read"], [".lock", "owner.json"])
        json.dumps(self.ws.receipt())

    def test_er11_group_files_640(self):
        gid = os.getgid()
        self.ws = ramws.open_run("c", "e2", tool=TOOL, share_group=gid)
        self.assertEqual(self.ws.receipt()["share"], {"gid": gid, "method": "group", "files_read": [".lock", "owner.json"]})
        for n in (".lock", "owner.json"):
            st = (self.ws.root / n).stat()
            self.assertEqual((stat.S_IMODE(st.st_mode), st.st_gid), (0o640, gid))

    def test_er11_default_files_untouched(self):
        self.ws = ramws.open_run("c", "e3", tool=TOOL)
        for n in (".lock", "owner.json"):
            self.assertFalse([l for l in getfacl(self.ws.root / n) if l.startswith(("user:", "mask")) and not l.startswith("user::")])
        self.assertNotIn("share", self.ws.receipt())

    def test_er11_file_acl_failure_fails_closed(self):
        real = ramws._setfacl

        def fake(args, path):
            if ramws.Path(path).name in (".lock", "owner.json"):
                raise OSError(95, "no file ACL")
            real(args, path)
        with mock.patch.object(ramws, "_setfacl", fake):
            with self.assertRaises(RamError) as c:
                ramws.open_run("c", "e4", tool=TOOL, share_with_uid=UID)
        self.assertEqual(c.exception.code, "E_RAM_SHARE")
        self.assertFalse((ramws.Path(ramws.ROOT) / TOOL / "c" / "e4").exists())
        self.assertFalse(ramws._OPEN)

    def test_er11_file_acl_failure_group_fallback_is_clean(self):
        real = ramws._setfacl

        def fake(args, path):
            if ramws.Path(path).name == ".lock" and args != ["-b"]:
                raise OSError(95, "no file ACL")
            real(args, path)
        with mock.patch.object(ramws, "_setfacl", fake):
            self.ws = ramws.open_run("c", "e5", tool=TOOL, share_with_uid=UID, share_group=os.getgid())
        self.assertEqual(self.ws.receipt()["share"]["method"], "group")
        for n in (".lock", "owner.json"):
            self.assertFalse([l for l in getfacl(self.ws.root / n) if l.startswith("user:%d" % UID)])
            self.assertEqual(stat.S_IMODE((self.ws.root / n).stat().st_mode), 0o640)

    def _reader(self, lock, mode):
        code = ("import fcntl,os,sys,time;fd=os.open(%r,os.O_RDONLY);fcntl.flock(fd,fcntl.%s|fcntl.LOCK_NB);"
                "print('held',flush=True);time.sleep(20)") % (str(lock), mode)
        p = subprocess.Popen(["python3", "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_er11_janitor_sweeps_dead_run_with_foreign_lock_sh(self):
        # mutant: _try_lock gives up on EX failure -> a reader's LOCK_SH pins a dead run "live forever"
        r = ramws.open_run("c", "dead", tool=TOOL, share_with_uid=UID)
        dead = r.root
        os.close(r._fd)  # host dies: its LOCK_EX is gone
        r._closed = True
        ramws._OPEN.pop(r, None)
        self._reader(dead / ".lock", "LOCK_SH")
        self.assertEqual(ramws.sweep(TOOL), ["c/dead"])
        self.assertFalse(dead.exists())

    def test_er11_live_run_not_swept_and_reader_probe_sees_host(self):
        self.ws = ramws.open_run("c", "live", tool=TOOL, share_with_uid=UID)
        lock = self.ws.root / ".lock"
        fd = os.open(lock, os.O_RDONLY)  # what the capsule guard does
        try:
            with self.assertRaises(BlockingIOError):
                ramws.fcntl.flock(fd, ramws.fcntl.LOCK_SH | ramws.fcntl.LOCK_NB)  # host LOCK_EX is visible
        finally:
            os.close(fd)
        self.assertEqual(ramws.sweep(TOOL), [])
        self.assertTrue(lock.exists())

    def test_er11_lock_sh_cannot_block_host_close(self):
        # a reader's LOCK_SH never blocks the host that already holds LOCK_EX: close() still removes the run
        self.ws = ramws.open_run("c", "cl", tool=TOOL, share_with_uid=UID)
        run = self.ws.root
        fd = os.open(run / ".lock", os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                ramws.fcntl.flock(fd, ramws.fcntl.LOCK_SH | ramws.fcntl.LOCK_NB)
        finally:
            os.close(fd)
        self.ws.close()
        self.assertFalse(run.exists())
        self.ws = None


if __name__ == "__main__":
    unittest.main()
