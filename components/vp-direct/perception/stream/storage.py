"""Verify erasure of the private RGB ring before returning its reservation."""
import os
from pathlib import Path
import stat

from vadapt import ramws, rtring


_ZERO = bytes(64 * 1024)


def _overwrite(fd, size):
    for pos in range(0, size, len(_ZERO)):
        block = _ZERO[:min(size - pos, len(_ZERO))]
        if os.pwrite(fd, block, pos) != len(block):
            raise OSError("short ring scrub")


def _scrub(fd):
    size = os.fstat(fd).st_size
    _overwrite(fd, size)
    for pos in range(0, size, len(_ZERO)):
        length = min(size - pos, len(_ZERO))
        if os.pread(fd, length, pos) != _ZERO[:length]:
            raise OSError("ring scrub readback failed")


class ScrubWorkspace:
    """RamCapture first joins the producer and unmaps both sides, then calls release.

    Pin the inode at reserve, when RingWriter has created it exclusively and has
    yet to publish. No source framebuffer or caller-owned copy is scrubbed.
    """
    def __init__(self, workspace):
        self._ws = workspace
        self.root = workspace.root
        self.path = workspace.path
        self.account = workspace.account
        self._ring = Path(self.root) / rtring.REL
        self._fd = None
        self.allocated_bytes = 0
        self.scrubbed = False
        self.closed = False

    def reserve(self, size):
        fd = os.open(self._ring, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (self._fd is not None or info.st_size or info.st_nlink != 1
                    or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or ramws.is_ram_fs(f"/proc/self/fd/{fd}") not in ramws.RAM_FS):
                raise OSError("expected a new, private RAM ring")
            self._ws.reserve(size)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self.allocated_bytes = size

    def release(self, path):
        if Path(path) != self._ring:
            raise OSError("unexpected ring path")
        entry = self._ring.lstat()
        if self._fd is None:
            # RingWriter can roll back an empty inode after failed reservation.
            if entry.st_size or not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise OSError("unidentified ring")
        else:
            if not os.path.samestat(entry, os.fstat(self._fd)) or entry.st_nlink != 1:
                raise OSError("ring inode changed")
            _scrub(self._fd)
            self.scrubbed = True
        try:
            self._ws.release(path)
        finally:
            if self._fd is not None and not os.path.lexists(self._ring):
                os.close(self._fd)
                self._fd = None

    def close(self):
        if self._fd is not None:
            # A failed RingWriter constructor may have no writer object left
            # to retry release. Its private reservation is still ours.
            self.release(self._ring)
        if os.path.lexists(self._ring):
            raise OSError("unidentified ring remains in workspace")
        self._ws.close()
        self.closed = True
