# @module {"id":"vm.blobs","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors"],"book":"HB-VC-s7-vmem"}
"""@module vm.blobs - content-addressed immutable blob store (SPEC_VMEM §2, §5 step 3)."""
import os
import re
import stat

from visioncore.canon import sha256_bytes
from visioncore.errors import VqaError

_SHA = re.compile(r"^[0-9a-f]{64}$")
EXTS = ("png", "bin", "mp4")


def fsync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd, data):
    """Complete a possibly short POSIX write; a non-progressing write is an I/O failure."""
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise OSError("write made no progress")
        offset += written


def _check(sha, ext):
    if not isinstance(sha, str) or not _SHA.match(sha) or ext not in EXTS:
        raise VqaError("E_USAGE", {"reason": "bad_blob_ref", "sha": str(sha)[:70], "ext": str(ext)})


def blob_path(root, sha, ext):
    """Path built from the sha only, never from an id."""
    _check(sha, ext)
    return os.path.join(str(root), "blobs", "sha256", sha[:2], "%s.%s" % (sha, ext))


def clean_tmps(root):
    """Remove stale `<sha>.<ext>.tmp` files (a crash between fchmod 0o444 and rename leaves one read-only)."""
    base = os.path.join(str(root), "blobs", "sha256")
    n = 0
    if os.path.isdir(base):
        for sub in os.listdir(base):
            d = os.path.join(base, sub)
            for name in os.listdir(d) if os.path.isdir(d) else ():
                if name.endswith(".tmp"):
                    try:
                        os.unlink(os.path.join(d, name))
                        n += 1
                    except FileNotFoundError:
                        pass
    return n


def put_blob(root, data, ext="bin", point=None):  # @anchor:vm.blobs.put_blob
    """tmp -> fsync -> rename -> fsync dir; existing blob is skipped. Returns sha256.
    `point` is a test seam called with blob_tmp_written / blob_chmod."""
    sha = sha256_bytes(data)
    path = blob_path(root, sha, ext)
    if os.path.exists(path):
        if not verify_blob(root, sha, ext):
            raise VqaError("E_GATE", {"reason": "g_blob_integrity", "sha": sha})
        return sha
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)  # stale read-only tmp of a killed writer would wedge O_TRUNC
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        write_all(fd, data)
        if point:
            point("blob_tmp_written")
        os.fchmod(fd, 0o444)
        if point:
            point("blob_chmod")
        os.fsync(fd)
    except BaseException:
        os.unlink(tmp)
        raise
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(d)
    return sha


def get_blob(root, sha, ext="bin"):
    path = blob_path(root, sha, ext)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        raise VqaError("E_ID", {"reason": "blob_missing", "sha": sha})


def verify_blob(root, sha, ext="bin"):
    """Re-hash; True iff present and content matches its name."""
    try:
        return sha256_bytes(get_blob(root, sha, ext)) == sha
    except VqaError:
        return False


def iter_blobs(root):
    """Yield (sha, ext) sorted by sha."""
    base = os.path.join(str(root), "blobs", "sha256")
    out = []
    if os.path.isdir(base):
        for sub in os.listdir(base):
            for name in os.listdir(os.path.join(base, sub)):
                stem, _, ext = name.partition(".")
                if _SHA.match(stem) and ext in EXTS:
                    out.append((stem, ext))
    return sorted(out)


def delete_blob(root, sha, ext="bin"):
    """Only for gc after a durable flip (caller proves unreferenced, §7)."""
    path = blob_path(root, sha, ext)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def is_immutable(root, sha, ext="bin"):
    return not (os.stat(blob_path(root, sha, ext)).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
