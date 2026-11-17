"""Publish an exclusive generation directory atomically on Linux."""
import ctypes
import errno
import os

from .common import Refusal


def publish_directory(scratch, target):
    """Commit visibility exclusively; return whether parent durability is proven.

    All preparation failures precede rename. Once rename succeeds, a directory
    fsync/close error cannot turn the visible generation into a refusal. False
    means committed bytes with unproven crash durability, never a failed commit.
    """
    # os.replace alone can overwrite an empty directory created by a racing caller.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise Refusal(3, "exclusive_publication_unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    # Persist staged names and acquire every descriptor before the commit.
    stage_fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(stage_fd)
    finally:
        os.close(stage_fd)
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    committed, durable = False, False
    try:
        if rename(-100, os.fsencode(scratch), -100, os.fsencode(target), 1):  # RENAME_NOREPLACE
            code = ctypes.get_errno()
            if code in (errno.ENOSYS, errno.EINVAL):
                raise Refusal(3, "exclusive_publication_unavailable")
            raise OSError(code, os.strerror(code))
        committed = True
        try:
            os.fsync(fd)
            durable = True
        except OSError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            if not committed:
                raise
            durable = False
    return durable
