"""vadapt.ramws - STORAGE LAW v1 RAM workspace: frames live on tmpfs, disk keeps evidence. Stdlib only.

Imports neither visioncore nor vmem (g_layer). CLI: python3 -m vadapt.ramws {sweep,sweep-kept,status,check-disk}; rc 0 ok, 2 usage, 5 LAW breach.
"""
import atexit
import fcntl
import json
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
    if not isinstance(v, str) or not NAME_RE.match(v) or v in (".", ".."):
        raise RamError("E_RAM_ESCAPE", "bad %s %r" % (what, v))
    return v


def _tree_bytes(p):
    """sum(st_blocks*512) of p without following symlinks."""
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    n = st.st_blocks * 512
    if os.path.isdir(p) and not os.path.islink(p):
        for dp, dn, fn in os.walk(p):
            for x in dn + fn:
                try:
                    n += os.lstat(os.path.join(dp, x)).st_blocks * 512
                except OSError:
                    pass
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


def _host_used(root, skip=None):
    """sum(st_blocks*512) over everything under root except the skip dir (this run: counted from its own `used`)."""
    n = 0
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if os.path.join(dp, d) != str(skip)]
        for x in fn:
            try:
                n += os.lstat(os.path.join(dp, x)).st_blocks * 512
            except OSError:
                pass
    return n


class RamWorkspace:
    """One run dir <root>/<tool>/<campaign>/<run_id>/ : root, quota, used, peak."""

    def __init__(self, root, quota, tool, campaign, run_id, lock_fd, statvfs, host_root, fstype):
        self.root, self.quota, self.used, self.peak = Path(root), quota, 0, 0
        self.tool, self.campaign, self.run_id = tool, campaign, run_id
        self._fd, self._statvfs, self._host, self._fstype = lock_fd, statvfs, Path(host_root), fstype
        self._released, self._closed = 0, False
        self._share = None
        self._swap0 = _swap_used()
        self._real = os.path.realpath(self.root)

    def _live(self):
        if self._closed:
            raise RamError("E_RAM_LOCKED", "workspace closed")

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

    def close(self):
        """rmtree root, drop lock; idempotent."""
        if self._closed:
            return
        try:
            if self.root.is_dir():
                self.peak = max(self.peak, _tree_bytes(self.root))
        except OSError:
            pass
        self._closed = True
        _OPEN.pop(self, None)
        shutil.rmtree(self.root, ignore_errors=True)
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
        ws.close()


def _on_signal(sig, frame):
    _close_all()
    prev = _SIG_DONE_PREV.get(sig)
    if callable(prev):
        return prev(sig, frame)
    signal.signal(sig, signal.SIG_DFL)
    os.kill(os.getpid(), sig)


_SIG_DONE_PREV = {}


def _install_handlers():
    if _SIG_DONE or threading.current_thread() is not threading.main_thread():
        return
    _SIG_DONE.append(1)
    atexit.register(_close_all)
    for sig in (signal.SIGTERM, signal.SIGINT):
        _SIG_DONE_PREV[sig] = signal.signal(sig, _on_signal)


def _owner(run):
    try:
        fd = os.open(run / "owner.json", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None  # planted by a sharing uid: not trusted (the flock test decides)
        with os.fdopen(fd, encoding="utf-8") as f:
            fd = None
            return json.loads(f.read())
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _try_lock(run):
    """fd holding LOCK_EX on run/.lock, None when held elsewhere, False when there is no lock file."""
    try:
        fd = os.open(run / ".lock", os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        pass
    # EX refused: either the host session holds LOCK_EX (live) or a reader (ER-011 share) holds LOCK_SH. LOCK_SH|NB succeeds only
    # when nobody holds LOCK_EX, so it separates the two: the host's own exclusive lock alone decides liveness.
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def sweep(tool, root=ROOT):
    """janitor: remove same-tool same-uid runs whose lock is free (or whose boot_id differs). Returns removed run ids."""
    base = Path(root) / _name(tool, "tool")
    gone = []
    for camp in sorted(base.iterdir()) if base.is_dir() else ():
        for run in sorted(camp.iterdir()) if camp.is_dir() else ():
            try:
                st = os.lstat(run)
            except OSError:
                continue
            if not run.is_dir() or run.is_symlink() or st.st_uid != os.getuid():
                continue
            own = _owner(run)
            if own is not None and (own.get("tool") != tool or own.get("uid") != os.getuid()):
                continue
            fd = _try_lock(run)
            if own is None and time.time() - st.st_mtime < 5:
                fd = None  # run dir still being created
            stale_boot = own is not None and own.get("boot_id") not in (None, _boot_id())
            if fd is None and not stale_boot:
                continue  # live owner: never touched (a reused pid cannot fool the flock test)
            if fd is False and not stale_boot:
                continue
            shutil.rmtree(run, ignore_errors=True)
            if isinstance(fd, int):
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
    for d in chain:
        if not _open_to_all(os.stat(d)):
            _setfacl(["-m", "u:%d:--x" % uid], d)
    os.chmod(run, 0o1700)  # sticky: <uid> cannot unlink/replace .lock or owner.json (F-02); setfacl then sets mask rwx
    try:
        _setfacl(["-m", "u:{0}:rwx,d:u::rwx,d:u:{1}:rwx,d:u:{0}:rwx,d:g::---,d:o::---".format(uid, os.getuid())], run)
        _share_files_acl(run, uid)
    except (OSError, subprocess.SubprocessError):
        _setfacl(["-b"], run)  # no half-applied ACL may survive into the group fallback (F-04); raises -> fail closed
        os.chmod(run, 0o700)
        for n in SHARED_FILES:
            _setfacl(["-b"], run / n)
            os.chmod(run / n, 0o600)
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
    fd = os.open(run / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        _rollback(run)
        raise RamError("E_RAM_LOCKED", "lock held")
    own = {"pid": os.getpid(), "starttime": _starttime(), "boot_id": _boot_id(), "tool": tool, "uid": os.getuid()}
    (run / "owner.json").write_text(json.dumps(own, sort_keys=True), encoding="utf-8")
    share = None
    if share_with_uid is not None or share_group is not None:
        try:
            os.chmod(run / "owner.json", 0o600)  # lock + owner exist before any share (F-02)
            share = _apply_share(run, Path(root), share_with_uid, share_group, created)
        except (RamError, OSError) as e:
            os.close(fd)
            _rollback(run)
            if isinstance(e, RamError):
                raise
            raise RamError("E_RAM_SHARE", "cannot share run dir (%s)" % e)
    ws = RamWorkspace(run, quota_bytes, tool, campaign, run_id, fd, statvfs, root, fst)
    ws._share = share
    _OPEN[ws] = 1
    _install_handlers()
    return ws


def keep_frames(src, build, run_id, cap_mb, *, now, reason, argv_sha, ttl_h=KEEP_TTL_H):
    """L7: copy files of src into <build>/kept_frames/<run_id>/ up to cap_mb (<=256); writes KEEP.json; returns it."""
    cap = min(int(cap_mb), KEEP_CAP_MB) * (1 << 20)
    ttl = min(int(ttl_h), KEEP_TTL_MAX_H) * 3600
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
    keep = {"cap_mb": cap // (1 << 20), "bytes": got, "ttl_until": int(now) + ttl, "reason": reason, "argv_sha": argv_sha,
            "kept_truncated": trunc}
    (dst / "KEEP.json").write_text(json.dumps(keep, sort_keys=True), encoding="utf-8")
    return keep


def sweep_kept(build, now):
    """delete <build>/kept_frames/* dirs whose KEEP.json ttl_until < now; dirs without a readable KEEP.json are never touched."""
    base = Path(build) / "kept_frames"
    gone = []
    for d in sorted(base.iterdir()) if base.is_dir() else ():
        try:
            ttl = json.loads((d / "KEEP.json").read_text(encoding="utf-8"))["ttl_until"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if d.is_dir() and not d.is_symlink() and isinstance(ttl, (int, float)) and ttl < now:
            shutil.rmtree(d)
            gone.append(d.name)
    return gone


def check_disk(dir, allow=("kept_frames", "fail_evidence"), since=None):
    """frame-like files (image/array/blend/mp4/tile buffers) under dir outside allow-listed dirs; since = min st_mtime_ns."""
    out = []
    base = Path(dir)
    for dp, dn, fn in os.walk(base):
        dn[:] = sorted(d for d in dn if d not in allow)
        for f in sorted(fn):
            if f.lower().endswith(FRAME_EXT) or f in FRAME_NAMES:
                p = os.path.join(dp, f)
                if since is None or os.lstat(p).st_mtime_ns > since:
                    out.append(os.path.relpath(p, base))
    return sorted(out)


def status(tool=None, root=ROOT):
    runs = []
    rb = Path(root)
    for t in sorted(rb.iterdir()) if rb.is_dir() else ():
        if tool is not None and t.name != tool:
            continue
        for camp in sorted(t.iterdir()) if t.is_dir() else ():
            for run in sorted(camp.iterdir()) if camp.is_dir() else ():
                fd = _try_lock(run)
                if isinstance(fd, int):
                    os.close(fd)
                runs.append({"tool": t.name, "campaign": camp.name, "run_id": run.name, "live": fd is None, "bytes": _tree_bytes(run)})
    sv = os.statvfs(str(rb if rb.exists() else "/dev/shm"))
    return {"runs": runs, "host_bytes": _host_used(rb), "free_bytes": sv.f_bavail * sv.f_frsize, "fstype": is_ram_fs(rb if rb.exists() else "/dev/shm")}


def main(argv):
    a = list(argv)
    cmd = a.pop(0) if a else None
    opt, pos = {}, []
    while a:
        x = a.pop(0)
        if x.startswith("--") and a:
            opt[x[2:]] = a.pop(0)
        else:
            pos.append(x)
    try:
        if cmd == "sweep" and "tool" in opt:
            out = {"removed": sweep(opt["tool"], opt.get("root", ROOT))}
        elif cmd == "sweep-kept" and "build" in opt:
            out = {"removed": sweep_kept(opt["build"], float(opt["now"]) if "now" in opt else time.time())}
        elif cmd == "status":
            out = status(opt.get("tool"), opt.get("root", ROOT))
        elif cmd == "check-disk" and len(pos) == 1:
            allow = tuple(opt["allow"].split(",")) if "allow" in opt else ("kept_frames", "fail_evidence")
            out = {"offending": check_disk(pos[0], allow, int(opt["since"]) if "since" in opt else None)}
            if out["offending"]:
                out["code"] = "E_DISK_FRAMES"
                sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
                return 5
        else:
            sys.stdout.write('{"code":"E_USAGE","detail":"ramws {sweep --tool T|sweep-kept --build B [--now S]|status [--tool T]|check-disk DIR [--allow a,b] [--since NS]} [--root R]"}\n')
            return 2
    except RamError as e:
        sys.stdout.write(json.dumps({"code": e.code, "detail": str(e)}, sort_keys=True) + "\n")
        return 5
    out["code"] = "OK"
    sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
