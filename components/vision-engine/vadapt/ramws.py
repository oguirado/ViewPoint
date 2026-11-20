"""vadapt.ramws - STORAGE LAW v1 RAM workspace: frames live on tmpfs, disk keeps evidence. Stdlib only.

Imports neither visioncore nor vmem (g_layer). CLI: python3 -m vadapt.ramws {sweep,sweep-kept,status,check-disk}; rc 0 ok, 2 usage, 5 LAW breach.
"""
import atexit
import errno
import fcntl
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = "/dev/shm/vqa-ramws"
RAM_FS = ("tmpfs", "ramfs")
GIB = 1 << 30
RUN_DEFAULT, RUN_MAX, HOST_MAX, FREE_MIN = 2 * GIB, 8 * GIB, 16 * GIB, 8 * GIB
SWAP_WARN = GIB
KEEP_CAP_MB, KEEP_TTL_H, KEEP_TTL_MAX_H = 256, 24, 72
FRAME_EXT = (".png", ".exr", ".npy", ".npz", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".webp", ".gif", ".mp4", ".blend", ".blend1", ".raw", ".xwd")
FRAME_NAMES = ("tiles.bin", "gops.bin", "frames.raw", "Xvfb_screen0")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPEN = {}
_SIG_DONE = []


class RamError(Exception):
    """code: E_RAM_NOT_TMPFS | E_RAM_QUOTA | E_RAM_ESCAPE | E_RAM_LOCKED | E_RAM_SHARE (CLI rc 5)."""

    def __init__(self, code, msg=""):
        super().__init__("%s %s" % (code, msg))
        self.code = code


def _unesc(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def is_ram_fs(p, mountinfo="/proc/self/mountinfo"):
    """fstype of the longest mount-point prefix of realpath(p), or None when that fstype is not tmpfs/ramfs."""
    real = os.path.realpath(str(p))
    best, fst = "", None
    with open(mountinfo, encoding="utf-8") as f:
        for line in f:
            head, sep, tail = line.partition(" - ")
            parts = head.split()
            if not sep or len(parts) < 5:
                continue
            mp = _unesc(parts[4])
            if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                best, fst = mp, tail.split()[0]
    return fst if fst in RAM_FS else None


def _name(v, what):
    if not isinstance(v, str) or not NAME_RE.fullmatch(v) or v in (".", ".."):
        raise RamError("E_RAM_ESCAPE", "bad %s %r" % (what, v))
    return v


def _raise_walk_error(error):
    raise error


def _tree_bytes(p, *, missing=0):
    """sum(st_blocks*512) of p without following symlinks."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return missing
    n = st.st_blocks * 512
    if stat.S_ISDIR(st.st_mode):
        for dp, dn, fn in os.walk(p, onerror=_raise_walk_error):
            for x in dn + fn:
                n += os.lstat(os.path.join(dp, x)).st_blocks * 512
    return n


def _starttime():
    with open("/proc/self/stat", encoding="ascii") as f:
        return int(f.read().rsplit(")", 1)[1].split()[19])


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _swap_used():
    t = f = 0
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("SwapTotal:"):
                t = int(line.split()[1])
            elif line.startswith("SwapFree:"):
                f = int(line.split()[1])
    return (t - f) * 1024


def _host_used(root, skip=None, *, strict=False):
    """sum(st_blocks*512) over everything under root except the skip dir (this run: counted from its own `used`)."""
    n = 0
    onerror = _raise_walk_error if strict else None
    for dp, dn, fn in os.walk(root, onerror=onerror):
        dn[:] = [d for d in dn if os.path.join(dp, d) != str(skip)]
        for x in fn:
            try:
                n += os.lstat(os.path.join(dp, x)).st_blocks * 512
            except OSError:
                if strict:
                    raise
    return n


class RamWorkspace:
    """One run dir <root>/<tool>/<campaign>/<run_id>/ : root, quota, used, peak."""

    def __init__(self, root, quota, tool, campaign, run_id, lock_fd, statvfs, host_root, fstype):
        self.root, self.quota, self.used, self.peak = Path(root), quota, 0, 0
        self.tool, self.campaign, self.run_id = tool, campaign, run_id
        self._fd, self._statvfs, self._host, self._fstype = lock_fd, statvfs, Path(host_root), fstype
        self._released, self._closed = 0, False
        self._close_bytes = None
        self._close_lock, self._closing = threading.RLock(), False
        self._share = None
        self._swap0 = _swap_used()
        self._real = os.path.realpath(self.root)

    def _live(self):
        if self._closed:
            raise RamError("E_RAM_LOCKED", "workspace closed")
        if self._close_bytes is not None:
            remaining = _tree_bytes(self.root, missing=None)
            if remaining is None:
                raise RamError("E_RAM_LOCKED", "workspace directory missing")
            self._account_close(remaining)

    def path(self, rel):
        """Path inside root (parents created; a trailing '/' also creates the dir itself). Rejects abs, '..', symlink escape."""
        self._live()
        s = os.fspath(rel)
        if not s or "\0" in s or os.path.isabs(s) or ".." in Path(s).parts:
            raise RamError("E_RAM_ESCAPE", "bad rel %r" % s)
        cand = self.root / s
        group = bool(self._share) and self._share.get("method") == "group"
        for _ in range(2):
            real = os.path.realpath(cand)
            if real != self._real and not real.startswith(self._real + os.sep):
                raise RamError("E_RAM_ESCAPE", "escapes root: %r" % s)
            tgt, new = (cand if s.endswith("/") else cand.parent), []
            while group and tgt != self.root and not os.path.lexists(tgt):
                new.append(tgt)
                tgt = tgt.parent
            (cand if s.endswith("/") else cand.parent).mkdir(parents=True, exist_ok=True)
            for d in reversed(new):  # share_group: the host umask must not drop g+w on new subdirs
                st = os.lstat(d)
                if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
                    os.chmod(d, 0o2770)
        return cand

    def reserve(self, n):
        """run quota AND host sum <= 16 GiB AND statvfs free - n >= 8 GiB, else E_RAM_QUOTA (state unchanged)."""
        self._live()
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise RamError("E_RAM_QUOTA", "bad reserve %r" % (n,))
        if self.used + n > self.quota:
            raise RamError("E_RAM_QUOTA", "run quota %d: used %d + %d" % (self.quota, self.used, n))
        if _host_used(self._host, skip=self.root) + self.used + n > HOST_MAX:
            raise RamError("E_RAM_QUOTA", "host sum > %d" % HOST_MAX)
        sv = self._statvfs(str(self._host))
        if sv.f_bavail * sv.f_frsize - n < FREE_MIN:
            raise RamError("E_RAM_QUOTA", "statvfs free - %d < %d" % (n, FREE_MIN))
        self.used += n
        self.peak = max(self.peak, self.used)

    def account(self):
        """resync used from st_blocks (external writers: Blender, Godot); updates peak; over quota -> E_RAM_QUOTA."""
        self._live()
        self.used = _tree_bytes(self.root)
        self.peak = max(self.peak, self.used)
        if self.used > self.quota:
            raise RamError("E_RAM_QUOTA", "used %d > quota %d" % (self.used, self.quota))
        return self.used

    def release(self, p):
        """unlink/rmtree p (inside root only, never root itself); used -= freed; returns freed bytes."""
        self._live()
        s = os.fspath(p)
        if "\0" in s:
            raise RamError("E_RAM_ESCAPE", "bad release path %r" % s)
        cand = Path(s) if os.path.isabs(s) else self.root / s
        parent = os.path.realpath(cand.parent)
        tgt = Path(parent) / cand.name
        if cand.name in ("", ".", "..") or ".." in cand.parts or not str(tgt).startswith(self._real + os.sep):
            raise RamError("E_RAM_ESCAPE", "release outside root: %r" % s)
        try:
            st = os.lstat(tgt)  # classify the node itself, never its symlink target
        except FileNotFoundError:
            return 0  # already released; do not credit accounting again
        if stat.S_ISDIR(st.st_mode):
            freed = _tree_bytes(tgt)
            shutil.rmtree(tgt)
        else:
            freed = st.st_blocks * 512
            os.unlink(tgt)  # includes FIFOs, sockets and dangling symlinks; never open them
        self.used = max(0, self.used - freed)
        self._released += freed
        return freed

    def receipt(self):
        sw = _swap_used() - self._swap0
        out = {"ram_peak_bytes": self.peak, "quota": self.quota, "released_bytes": self._released,
               "swap_used_delta": sw, "fstype": self._fstype}
        if sw > SWAP_WARN:
            out["warnings"] = ["W_RAM_SWAP"]
        if self._share:
            out["share"] = dict(self._share)
        return out

    def _account_close(self, remaining):
        freed = max(0, self._close_bytes - remaining)
        self._released += freed
        self.used = max(remaining, self.used - freed)
        self.peak = max(self.peak, self.used)
        self._close_bytes = None

    def close(self):
        """Remove the workspace and release its lock. A failed removal can be retried."""
        with self._close_lock:
            if self._closed or self._closing:
                return
            self._closing = True
            try:
                self._remove()
            finally:
                self._closing = False

    def _remove(self):
        before = _tree_bytes(self.root)
        if self._close_bytes is not None:
            self._account_close(before)
        self._close_bytes = before
        self.peak = max(self.peak, before)
        try:
            # Keep the ownership files in place while deleting payload.
            for child in self.root.iterdir():
                if child.name in SHARED_FILES:
                    continue
                if stat.S_ISDIR(child.lstat().st_mode):
                    shutil.rmtree(child)
                else:
                    child.unlink()
            shutil.rmtree(self.root)
        except OSError as e:
            try:
                remaining = _tree_bytes(self.root, missing=None)
            except OSError:
                raise e from None
            if remaining is not None:
                self._account_close(remaining)
                raise
            self._finish_close()
            if not isinstance(e, FileNotFoundError):
                raise
        else:
            self._finish_close()

    def _finish_close(self):
        self._account_close(0)
        self.used = 0
        self._closed = True
        _OPEN.pop(self, None)
        for d in (self.root.parent, self.root.parent.parent):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _close_all():
    for ws in list(_OPEN):
        try:
            ws.close()
        except OSError:
            continue


atexit.register(_close_all)


def _on_signal(sig, frame):
    prev = _SIG_DONE_PREV.get(sig)
    if prev == signal.SIG_IGN:
        return
    _close_all()
    if callable(prev):
        return prev(sig, frame)
    signal.signal(sig, signal.SIG_DFL)
    os.kill(os.getpid(), sig)


_SIG_DONE_PREV = {}


def _install_handlers():
    if _SIG_DONE or threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        if sig not in _SIG_DONE_PREV and signal.getsignal(sig) != signal.SIG_IGN:
            _SIG_DONE_PREV[sig] = signal.signal(sig, _on_signal)
    _SIG_DONE.append(1)


def _owner(run):
    try:
        fd = os.open(run / "owner.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None  # planted by a sharing uid: not trusted (the flock test decides)
        with os.fdopen(fd, encoding="utf-8") as f:
            fd = None
            owner = json.loads(f.read())
            return owner if isinstance(owner, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _try_lock(run, *, strict=False):
    """Return the acquired lock fd, None if live or unavailable, or False for an absent lock file."""
    try:
        fd = os.open(run / ".lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if strict:
            raise
        return False
    except OSError:
        if strict:
            raise
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "expected a regular workspace lock", run / ".lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EAGAIN):
            os.close(fd)
            if strict:
                raise
            return None
    # EX refused: either the host session holds LOCK_EX (live) or a reader (ER-011 share) holds LOCK_SH. LOCK_SH|NB succeeds only
    # when nobody holds LOCK_EX, so it separates the two: the host's own exclusive lock alone decides liveness.
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return fd
    except OSError as e:
        os.close(fd)
        if strict and e.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return None


def _child_dirs(parent):
    if parent.is_symlink():
        return []
    try:
        return sorted(p for p in parent.iterdir() if not p.is_symlink() and p.is_dir())
    except (FileNotFoundError, NotADirectoryError):
        return []


def sweep(tool, root=ROOT):
    """Remove stale same-tool, same-uid runs. An unavailable lock always preserves the run."""
    base = Path(root) / _name(tool, "tool")
    gone = []
    boot = _boot_id()
    for camp in _child_dirs(base):
        for run in _child_dirs(camp):
            try:
                st = os.lstat(run)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
                continue
            own = _owner(run)
            if own is not None and (own.get("tool") != tool or own.get("uid") != os.getuid()):
                continue
            if own is None and time.time() - st.st_mtime < 5:
                continue
            stale_boot = bool(boot and own and own.get("boot_id") and own["boot_id"] != boot)
            fd = _try_lock(run)
            if fd is None or (fd is False and not stale_boot):
                continue
            try:
                current = os.lstat(run)
                if (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
                    continue
                shutil.rmtree(run)
            except OSError:
                continue
            finally:
                if fd is not False:
                    os.close(fd)
            gone.append("%s/%s" % (camp.name, run.name))
        try:
            camp.rmdir()
        except OSError:
            pass
    try:
        base.rmdir()
    except OSError:
        pass
    return gone


def _setfacl(args, path):
    """run setfacl <args> -- <path>; raises OSError when the tool is missing or the filesystem refuses the ACL."""
    r = subprocess.run(["setfacl", *args, "--", str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise OSError(r.returncode, (r.stderr or "setfacl failed").strip())


def _acl_snapshot(path):
    r = subprocess.run(["getfacl", "-pn", "--", str(path)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    if r.returncode:
        raise OSError(r.returncode, r.stderr.decode(errors="replace").strip() or "getfacl failed")
    return b"".join(line for line in r.stdout.splitlines(keepends=True)
                    if not line.startswith((b"# owner: ", b"# group: ")))


def _restore_acl(snapshot):
    r = subprocess.run(["setfacl", "--restore=-"], input=snapshot,
                       capture_output=True, timeout=10)
    if r.returncode:
        raise OSError(r.returncode, r.stderr.decode(errors="replace").strip() or "setfacl restore failed")


def _open_to_all(st):
    return st.st_mode & 0o011 == 0o011  # group and other may traverse: any non-owner uid can already pass


SHARED_FILES = (".lock", "owner.json")  # ER-011: the only files a sharing uid may read (never write)


def _share_files_acl(run, uid):
    """read-only ACL u:<uid>:r-- (mask r--) on .lock and owner.json: lets the capsule probe the session lock and read the owner."""
    for n in SHARED_FILES:
        _setfacl(["-m", "u:%d:r--,m::r--" % uid], run / n)


def _share_acl(run, chain, uid):
    """parents first (additive u:<uid>:--x, skipped when already traversable), then one setfacl on the run dir. The default ACL
    names the host owner too: files the container creates (owner <uid>) stay readable/deletable by the host (F-01)."""
    if shutil.which("setfacl") is None:
        raise FileNotFoundError(errno.ENOENT, "setfacl")
    parents = [d for d in chain if not _open_to_all(os.stat(d))]
    files = [run / n for n in SHARED_FILES]
    snapshots = {p: _acl_snapshot(p) for p in (*parents, run, *files)}
    attempted = []
    try:
        for d in parents:
            attempted.append(d)
            _setfacl(["-m", "u:%d:--x" % uid], d)
        attempted.append(run)
        os.chmod(run, 0o1700)  # sticky protects .lock and owner.json (F-02)
        _setfacl(["-m", "u:{0}:rwx,d:u::rwx,d:u:{1}:rwx,d:u:{0}:rwx,d:g::---,d:o::---".format(uid, os.getuid())], run)
        attempted.extend(files)
        _share_files_acl(run, uid)
    except (OSError, subprocess.SubprocessError):
        rollback_error = None
        for path in reversed(attempted):
            try:
                if _acl_snapshot(path) != snapshots[path]:
                    _restore_acl(snapshots[path])
            except (OSError, subprocess.SubprocessError) as e:
                rollback_error = e
        if rollback_error is not None:
            raise RamError("E_RAM_SHARE", "ACL rollback failed: %s" % rollback_error) from rollback_error
        raise


def _share_group(run, chain, gid, created):
    """dirs created by this open_run: chgrp + 0o710; pre-existing parents are never chgrp'ed (F-03), only o+x added when the
    group cannot traverse them yet. Run dir: chgrp + 0o3770 (setgid + sticky)."""
    for d in [*(c for c in chain if c in created), run, *(run / n for n in SHARED_FILES)]:  # every chown before any chmod
        os.chown(d, -1, gid)
    for d in chain:
        st = os.stat(d)
        if d in created:
            os.chmod(d, 0o710)
        elif not (st.st_mode & 0o001 or (st.st_gid == gid and st.st_mode & 0o010)):
            os.chmod(d, stat.S_IMODE(st.st_mode) | 0o001)
    os.chmod(run, 0o3770)
    for n in SHARED_FILES:
        os.chmod(run / n, 0o640)  # ER-011: group may read .lock/owner.json, never write


def _apply_share(run, root, uid, gid, created=()):
    """ER-010: share the run dir with one container uid (ACL) or group; touches only run and its parents under root."""
    chain, d = [], run.parent
    while True:
        chain.append(d)
        if d == root:
            break
        d = d.parent
    chain.reverse()
    errs = []
    if uid is not None:
        try:
            _share_acl(run, chain, uid)
            return {"uid": uid, "method": "acl", "files_read": list(SHARED_FILES)}
        except (OSError, subprocess.SubprocessError) as e:
            errs.append("acl: %s" % e)
            try:
                if os.stat(run).st_mode & 0o7077 != 0 or any(os.stat(run / n).st_mode & 0o177 for n in SHARED_FILES):  # the -b rollback itself failed: never mix ACL and group
                    raise RamError("E_RAM_SHARE", "cannot share run dir (%s; acl rollback failed)" % "; ".join(errs))
            except OSError:
                raise RamError("E_RAM_SHARE", "cannot share run dir (%s)" % "; ".join(errs))
    if gid is not None:
        try:
            _share_group(run, chain, gid, created)
            return {"gid": gid, "method": "group", "files_read": list(SHARED_FILES)}
        except OSError as e:
            errs.append("group: %s" % e)
    raise RamError("E_RAM_SHARE", "cannot share run dir (%s)" % "; ".join(errs))


def _rollback(run):
    shutil.rmtree(run, ignore_errors=True)
    for d in (run.parent, run.parent.parent):
        try:
            d.rmdir()
        except OSError:
            pass


def _int_id(v, what):
    if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 1 << 32):
        raise RamError("E_RAM_SHARE", "bad %s %r" % (what, v))


def open_run(campaign, run_id, quota_bytes=RUN_DEFAULT, *, tool, root=ROOT, statvfs=os.statvfs, share_with_uid=None, share_group=None):
    """Create <root>/<tool>/<campaign>/<run_id>/ on tmpfs/ramfs (janitor first) and lock it for this process.

    share_with_uid: POSIX ACL u:<uid>:rwx + default u:<host uid>/u:<uid>:rwx on the sticky run dir (1770), u:<uid>:--x on parents up to
    root that are not already traversable; share_group: fallback chgrp + 3770 on the run dir, 710 on parents created here (o+x on
    pre-existing ones, gid never rewritten). Applied after .lock/owner.json (0o600). Neither applicable -> E_RAM_SHARE (fail closed)."""
    _name(tool, "tool"), _name(campaign, "campaign"), _name(run_id, "run_id")
    _int_id(share_with_uid, "share_with_uid"), _int_id(share_group, "share_group")
    if not isinstance(quota_bytes, int) or isinstance(quota_bytes, bool) or not 0 < quota_bytes <= RUN_MAX:
        raise RamError("E_RAM_QUOTA", "run quota must be in (0, %d]" % RUN_MAX)
    root = os.path.realpath(root)  # F-05: one resolved root for the tmpfs check, mkdir, sweep and share
    anc = Path(root)
    while not anc.exists():
        anc = anc.parent
    fst = is_ram_fs(anc)
    if fst is None:
        raise RamError("E_RAM_NOT_TMPFS", "%s is not tmpfs/ramfs" % root)
    Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
    run = Path(root) / tool / campaign / run_id

    def check_run_path():
        for component in (run.parent.parent, run.parent, run):
            if component.is_symlink():
                raise RamError("E_RAM_ESCAPE", "symlink component: %s" % component)
        if not os.path.realpath(run).startswith(root + os.sep):
            raise RamError("E_RAM_ESCAPE", "run outside RAM root")

    check_run_path()  # reject aliases before the janitor can traverse them
    sweep(tool, root)
    created = []
    for _ in range(3):  # a concurrent close() may rmdir the empty parents under us
        check_run_path()
        try:
            for d in (run.parent.parent, run.parent):  # each level 0o700 (pathlib parents=True would use the umask mode)
                try:
                    d.mkdir(mode=0o700)
                    created.append(d)
                except FileExistsError:
                    pass
            run.mkdir(mode=0o700)
            check_run_path()
            break
        except FileExistsError:
            raise RamError("E_RAM_LOCKED", "run dir exists (live owner): %s/%s" % (campaign, run_id))
        except FileNotFoundError:
            continue
    else:
        raise RamError("E_RAM_LOCKED", "cannot create run dir")
    fd = None
    try:
        fd = os.open(run / ".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RamError("E_RAM_LOCKED", "lock held") from e
        own = {"pid": os.getpid(), "starttime": _starttime(), "boot_id": _boot_id(), "tool": tool, "uid": os.getuid()}
        with (run / "owner.json").open("x", encoding="utf-8") as f:
            json.dump(own, f, sort_keys=True)
        share = None
        if share_with_uid is not None or share_group is not None:
            try:
                os.chmod(run / "owner.json", 0o600)  # lock + owner exist before any share (F-02)
                share = _apply_share(run, Path(root), share_with_uid, share_group, created)
            except OSError as e:
                raise RamError("E_RAM_SHARE", "cannot share run dir (%s)" % e) from e
        ws = RamWorkspace(run, quota_bytes, tool, campaign, run_id, fd, statvfs, root, fst)
        ws._share = share
        _install_handlers()
    except BaseException:
        try:
            _rollback(run)
        finally:
            if fd is not None:
                os.close(fd)
        raise
    _OPEN[ws] = 1
    return ws


def keep_frames(src, build, run_id, cap_mb, *, now, reason, argv_sha, ttl_h=KEEP_TTL_H):
    """L7: copy files of src into <build>/kept_frames/<run_id>/ up to cap_mb (<=256); writes KEEP.json; returns it."""
    cap_mb, ttl_h, now = int(cap_mb), int(ttl_h), int(now)
    if min(cap_mb, ttl_h, now) < 0:
        raise RamError("E_RAM_QUOTA", "negative retention bound")
    cap = min(cap_mb, KEEP_CAP_MB) * (1 << 20)
    ttl = min(ttl_h, KEEP_TTL_MAX_H) * 3600
    dst = Path(build) / "kept_frames" / _name(run_id, "run_id")
    dst.mkdir(parents=True, exist_ok=True)
    got, trunc = 0, False
    for f in sorted(p for p in Path(src).rglob("*") if p.is_file() and not p.is_symlink()):
        sz = f.stat().st_size
        if got + sz > cap:
            trunc = True
            break
        t = dst / f.relative_to(src)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, t)
        got += sz
    keep = {"cap_mb": cap // (1 << 20), "bytes": got, "ttl_until": now + ttl, "reason": reason, "argv_sha": argv_sha,
            "kept_truncated": trunc}
    (dst / "KEEP.json").write_text(json.dumps(keep, sort_keys=True), encoding="utf-8")
    return keep


def sweep_kept(build, now):
    """delete <build>/kept_frames/* dirs whose KEEP.json ttl_until < now; dirs without a readable KEEP.json are never touched."""
    base = Path(build) / "kept_frames"
    gone = []
    for d in _child_dirs(base):
        try:
            receipt = d / "KEEP.json"
            if receipt.is_symlink() or not receipt.is_file():
                continue
            ttl = json.loads(receipt.read_text(encoding="utf-8"))["ttl_until"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            continue
        if (isinstance(ttl, float) and not math.isfinite(ttl)) or ttl < 0 or not ttl < now:
            continue
        try:
            shutil.rmtree(d)
        except OSError:
            continue
        gone.append(d.name)
    return gone


def _require_directory(p):
    p = Path(p)
    if not stat.S_ISDIR(p.stat().st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), p)
    with os.scandir(p):
        pass
    return p


def check_disk(dir, allow=("kept_frames", "fail_evidence"), since=None):
    """frame-like files (image/array/blend/mp4/tile buffers) under dir outside allow-listed dirs; since = min st_mtime_ns."""
    out = []
    base = _require_directory(dir)
    for dp, dn, fn in os.walk(base, onerror=_raise_walk_error):
        dn[:] = sorted(d for d in dn if d not in allow)
        for f in sorted(fn):
            if f.lower().endswith(FRAME_EXT) or f in FRAME_NAMES:
                p = os.path.join(dp, f)
                if since is None or os.lstat(p).st_mtime_ns > since:
                    out.append(os.path.relpath(p, base))
    return sorted(out)


def status(tool=None, root=ROOT):
    runs = []
    rb = _require_directory(root)
    for t in _child_dirs(rb.resolve()):
        if tool is not None and t.name != tool:
            continue
        for camp in _child_dirs(t):
            for run in _child_dirs(camp):
                fd = _try_lock(run, strict=True)
                if fd is not None:
                    os.close(fd)
                used = _tree_bytes(run, missing=None)
                if used is None:
                    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), run)
                runs.append({"tool": t.name, "campaign": camp.name, "run_id": run.name, "live": fd is None, "bytes": used})
    sv = os.statvfs(rb)
    return {"runs": runs, "host_bytes": _host_used(rb, strict=True), "free_bytes": sv.f_bavail * sv.f_frsize, "fstype": is_ram_fs(rb)}


def _cli_args(argv):
    cmd, *args = argv or [""]
    allowed = {
        "sweep": ("tool", "root"),
        "sweep-kept": ("build", "now"),
        "status": ("tool", "root"),
        "check-disk": ("allow", "since"),
    }.get(cmd)
    if allowed is None:
        raise ValueError("unknown command")
    opt, pos = {}, []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not arg.startswith("--"):
            pos.append(arg)
            continue
        name = arg[2:]
        if (name not in allowed or name in opt or i == len(args) or args[i].startswith("--")
                or ("\0" in args[i]) or (not args[i] and name != "allow")):
            raise ValueError("bad option %s" % arg)
        opt[name] = args[i]
        i += 1
    if len(pos) != (1 if cmd == "check-disk" else 0) or any(not p or "\0" in p for p in pos):
        raise ValueError("invalid positional arguments")
    return cmd, opt, pos


def main(argv):
    try:
        cmd, opt, pos = _cli_args(argv)
        if cmd == "sweep":
            tool, root = _name(opt["tool"], "tool"), opt.get("root", ROOT)
            _require_directory(root)
            out = {"removed": sweep(tool, root)}
        elif cmd == "sweep-kept":
            now = float(opt["now"]) if "now" in opt else time.time()
            if not math.isfinite(now) or now < 0:
                raise ValueError("--now must be finite and nonnegative")
            _require_directory(opt["build"])
            out = {"removed": sweep_kept(opt["build"], now)}
        elif cmd == "status":
            tool = _name(opt["tool"], "tool") if "tool" in opt else None
            out = status(tool, opt.get("root", ROOT))
        else:
            allow = tuple(opt["allow"].split(",")) if opt.get("allow") else (() if "allow" in opt else ("kept_frames", "fail_evidence"))
            if any(not d or d in (".", "..") or Path(d).name != d for d in allow):
                raise ValueError("--allow must contain directory names")
            since = int(opt["since"]) if "since" in opt else None
            if since is not None and since < 0:
                raise ValueError("--since must be nonnegative")
            out = {"offending": check_disk(pos[0], allow, since)}
            if out["offending"]:
                out["code"] = "E_DISK_FRAMES"
                sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
                return 5
    except (KeyError, ValueError, OverflowError) as e:
        sys.stdout.write(json.dumps({"code": "E_USAGE", "detail": str(e)}, sort_keys=True) + "\n")
        return 2
    except RamError as e:
        sys.stdout.write(json.dumps({"code": e.code, "detail": str(e)}, sort_keys=True) + "\n")
        return 5
    except OSError as e:
        sys.stdout.write(json.dumps({"code": "E_IO", "detail": str(e)}, sort_keys=True) + "\n")
        return 5
    out["code"] = "OK"
    sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
