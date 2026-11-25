"""Owner-private filesystem operations, rooted at an open directory descriptor."""

from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
from io import BytesIO
import os
from pathlib import Path
import stat
from uuid import uuid4

from ..contracts.validation import strict_json
from ..contracts.wire import record_bytes

BLOCK = 1 << 20
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
RENAME_NOREPLACE = 1
_renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
_renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                      ctypes.c_uint)
_renameat2.restype = ctypes.c_int


class StoreError(Exception):
    """Base type for errors mapped by the transport."""


class Unavailable(StoreError):
    """A resource is absent or outside current authorization."""


class StorageFailure(StoreError):
    """Storage is full or a publication could not be confirmed."""


class IntegrityError(StoreError):
    """Stored evidence did not verify."""


class Conflict(StoreError):
    """A durable binding conflicts with the proposed write."""


class RangeError(StoreError):
    """A verified object cannot serve the requested offset."""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class Files:
    def __init__(self, root, fault, *, wait_for_lock=False):
        root = Path(root)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("store root must be a canonical absolute path")
        self.fd = os.open(root, DIR_FLAGS)
        self.fault = fault
        try:
            self._private(self.fd, directory=True)
            flags = fcntl.LOCK_EX if wait_for_lock else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(self.fd, flags)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def _private(fd, *, directory=False):
        info = os.fstat(fd)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
            raise IntegrityError("unexpected store entry type, owner or mode")

    @contextmanager
    def directory(self, path):
        fd = os.dup(self.fd)
        try:
            for part in path:
                if part in {"", ".", ".."} or "/" in part:
                    raise IntegrityError("invalid internal path")
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                self._private(fd, directory=True)
            yield fd
        finally:
            os.close(fd)

    def sync(self, path):
        with self.directory(path) as fd:
            os.fsync(fd)

    def mkdir(self, path):
        with self.directory(path[:-1]) as fd:
            try:
                os.mkdir(path[-1], mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            os.fsync(fd)
        with self.directory(path):
            pass

    def exists(self, path):
        try:
            with self.directory(path[:-1]) as fd:
                os.stat(path[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def names(self, path):
        with self.directory(path) as fd:
            return sorted(os.listdir(fd))

    def isdir(self, path):
        with self.directory(path[:-1]) as fd:
            return stat.S_ISDIR(os.stat(path[-1], dir_fd=fd, follow_symlinks=False).st_mode)

    @contextmanager
    def open(self, path):
        with self.directory(path[:-1]) as fd:
            entry = os.open(path[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(entry, "rb") as stream:
                self._private(stream.fileno())
                yield stream

    def read(self, path, bound):
        with self.open(path) as stream:
            if os.fstat(stream.fileno()).st_size > bound:
                raise IntegrityError("stored file exceeds its byte limit")
            data = stream.read(bound + 1)
            if len(data) > bound:
                raise IntegrityError("stored file exceeds its byte limit")
            return data

    def json(self, path, bound):
        raw = self.read(path, bound)
        result = strict_json(raw)
        if record_bytes(result) != raw:
            raise IntegrityError("stored metadata is not canonical")
        return result

    def metadata(self, path, *, bound, page=None):
        with self.open(path) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > bound:
                raise IntegrityError("stored file exceeds its byte limit")
            hasher = hashlib.sha256()
            total = 0
            while block := stream.read(BLOCK):
                total += len(block)
                if total > size:
                    raise IntegrityError("stored file changed during verification")
                hasher.update(block)
            if total != size:
                raise IntegrityError("stored file changed during verification")
            data = None
            if page is not None:
                offset, limit = page
                if (size and offset >= size) or (not size and offset):
                    raise RangeError("page offset is outside verified bytes")
                stream.seek(offset)
                data = stream.read(limit)
                if len(data) != min(limit, size - offset):
                    raise IntegrityError("stored file changed during verification")
            return {"bytes": size, "sha256": hasher.hexdigest()}, data

    def write(self, path, data, *, length, sha, label):
        """Write an exclusive staging file and verify streamed octets before sync."""
        with self.directory(path[:-1]) as fd:
            entry = os.open(path[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=fd)
            with os.fdopen(entry, "wb") as stream:
                hasher = hashlib.sha256()
                total = 0
                while total < length:
                    block = data.read(min(BLOCK, length - total))
                    if not isinstance(block, bytes) or not block or len(block) > length - total:
                        raise ValueError("stream byte count mismatch")
                    stream.write(block)
                    hasher.update(block)
                    total += len(block)
                if data.read(1) != b"" or hasher.hexdigest() != sha:
                    raise ValueError("stream length or digest mismatch")
                stream.flush()
                self.fault(label, "before_file_sync")
                os.fsync(stream.fileno())
                self.fault(label, "after_file_sync")
            os.fsync(fd)

    def rename(self, source, dest, label, *, replace=False):
        with self.directory(source[:-1]) as origin, self.directory(dest[:-1]) as target:
            self.fault(label, "before_rename")
            if replace:
                os.replace(source[-1], dest[-1], src_dir_fd=origin, dst_dir_fd=target)
            elif _renameat2(origin, os.fsencode(source[-1]), target, os.fsencode(dest[-1]),
                            RENAME_NOREPLACE):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            self.fault(label, "after_rename")
            os.fsync(target)
            if source[:-1] != dest[:-1]:
                os.fsync(origin)
            self.fault(label, "after_dir_sync")

    def publish(self, dest, raw, label, *, staging=None, replace=False):
        if staging is None:
            staging = dest[:-1]
        temp = staging + (f".{dest[-1]}.{uuid4().hex}",)
        self.write(temp, BytesIO(raw), length=len(raw), sha=digest(raw), label=label)
        self.rename(temp, dest, label, replace=replace)

    def remove(self, path):
        with self.directory(path[:-1]) as parent:
            try:
                entry = os.stat(path[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISDIR(entry.st_mode):
                for name in self.names(path):
                    self.remove(path + (name,))
                os.rmdir(path[-1], dir_fd=parent)
            else:
                os.unlink(path[-1], dir_fd=parent)
            os.fsync(parent)

    def size(self, path):
        if self.isdir(path):
            return sum(self.size(path + (name,)) for name in self.names(path))
        with self.open(path) as stream:
            return os.fstat(stream.fileno()).st_size
