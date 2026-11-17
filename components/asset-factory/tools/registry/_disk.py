"""Descriptor-relative filesystem operations for the registry."""

import errno
import fcntl
import hashlib
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from uuid import uuid4

from asset_factory import core

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_PENDING = re.compile(r"[0-9a-f]{32}\.pending")
_EXISTING_WITNESS = re.compile(r"existing-[0-9a-f]{32}")
_INCOMING_WITNESS = re.compile(r"incoming-[0-9a-f]{32}")
_CHUNK = 1 << 20
RECORD_BYTE_LIMIT = 1 << 20


class RegistryError(Exception):
    """A refusal with a factory boundary return code."""

    def __init__(self, rc, reason):
        self.rc = rc
        self.status = core.STATUS[rc]
        self.verdict = core.VERDICT[rc]
        self.reason = reason
        super().__init__(reason)


def _entry_error(exc):
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO):
        return RegistryError(5, "unsafe_store_entry")
    return RegistryError(3, "store_io:" + str(exc.errno))


@contextmanager
def directory(parent, name):
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _entry_error(exc) from exc
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def root_directory(path):
    # Open the absolute path component by component without dereferencing links.
    fd = os.open("/", _DIR_FLAGS)
    try:
        for name in path.parts[1:]:
            with directory(fd, name) as child:
                next_fd = os.dup(child)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular(parent, name):
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _entry_error(exc) from exc
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RegistryError(5, "unsafe_store_entry")
        yield file


def ensure_directory(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    # Sync even after EEXIST: an interrupted writer may have created it.
    with directory(parent, name):
        os.fsync(parent)


def check_size(size, *, record, max_bytes):
    if record and size > RECORD_BYTE_LIMIT:
        raise RegistryError(6, "record_byte_limit")
    if max_bytes is not None and size > max_bytes:
        raise RegistryError(6, "byte_budget")


def read_checked(parent, name, expected, *, load, record=False, max_bytes=None):
    with regular(parent, name) as file:
        file_size = os.fstat(file.fileno()).st_size
        check_size(file_size, record=record, max_bytes=max_bytes)
        if load:
            data = file.read(file_size + 1)
            size = len(data)
            check_size(size, record=record, max_bytes=max_bytes)
            actual = hashlib.sha256(data).hexdigest()
        else:
            digest = hashlib.sha256()
            size = 0
            while chunk := file.read(_CHUNK):
                size += len(chunk)
                check_size(size, record=record, max_bytes=max_bytes)
                digest.update(chunk)
            actual = digest.hexdigest()
            data = None
        if actual != expected:
            raise RegistryError(4, "digest_mismatch:" + expected)
        return size, data


def _new_file(staging, content):
    name = uuid4().hex + ".pending"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, 0o400, dir_fd=staging)
    with os.fdopen(fd, "wb") as file:
        if isinstance(content, bytes):
            file.write(content)
        else:
            while chunk := content.read(_CHUNK):
                file.write(chunk)
        file.flush()
        os.fsync(file.fileno())
    return name


def _remove_pending(staging, name):
    os.unlink(name, dir_fd=staging)
    os.fsync(staging)


def recover_staging(staging):
    for name in os.listdir(staging):
        info = os.stat(name, dir_fd=staging, follow_symlinks=False)
        # An interrupted exclusive link may leave the staged inode with two names.
        if not _PENDING.fullmatch(name) or not stat.S_ISREG(info.st_mode):
            raise RegistryError(5, "unsafe_staging_entry")
        os.unlink(name, dir_fd=staging)
    os.fsync(staging)


def _same_bytes(file, data):
    view = memoryview(data)
    for start in range(0, len(data), _CHUNK):
        if file.read(_CHUNK) != view[start:start + _CHUNK]:
            return False
    return not file.read(1)


def compare(parent, name, data, on_conflict, *, record=False):
    """Return None for an absent file, True for equal bytes, or False for repairable damage."""
    with ExitStack() as stack:
        try:
            existing = stack.enter_context(regular(parent, name))
        except FileNotFoundError:
            return None
        check_size(os.fstat(existing.fileno()).st_size, record=record, max_bytes=None)
        if _same_bytes(existing, data):
            return True
        existing.seek(0)
        on_conflict(existing, data)
        return False


def install(staging, parent, name, data, on_conflict, *, record=False):
    state = compare(parent, name, data, on_conflict, record=record)
    if state is not True:
        pending = _new_file(staging, data)
        if state is False:
            os.replace(pending, name, src_dir_fd=staging, dst_dir_fd=parent)
            os.fsync(parent)
            os.fsync(staging)
            return
        try:
            os.link(pending, name, src_dir_fd=staging, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError:
            if compare(parent, name, data, on_conflict, record=record) is not True:
                raise RegistryError(4, "immutable_entry_changed")
        _remove_pending(staging, pending)
    os.fsync(parent)


def quarantine(staging, quarantine_root, digest, existing, incoming):
    # Persist the marker first. An interrupted capture still blocks reads.
    _save_witnesses(staging, quarantine_root, digest, (("existing", existing), ("incoming", incoming)))


def preserve_corrupt(staging, quarantine_root, digest, existing):
    ensure_directory(quarantine_root, "corrupt")
    with directory(quarantine_root, "corrupt") as damaged:
        _save_witnesses(staging, damaged, digest, (("existing", existing),))


def legacy_corruption(quarantine_root, digest, data):
    """Verify the complete witness pair before unblocking a legacy marker."""
    with ExitStack() as stack:
        try:
            witnesses = stack.enter_context(directory(quarantine_root, digest))
        except FileNotFoundError:
            return False
        names = sorted(os.listdir(witnesses))
        if (len(names) == 2 and _EXISTING_WITNESS.fullmatch(names[0])
                and _INCOMING_WITNESS.fullmatch(names[1])):
            with regular(witnesses, names[0]) as existing, regular(witnesses, names[1]) as incoming:
                if hashlib.file_digest(existing, "sha256").hexdigest() != digest and _same_bytes(incoming, data):
                    return True
        raise RegistryError(5, "quarantined_digest:sha256:" + digest)


def archive_legacy_corruption(quarantine_root, digest):
    ensure_directory(quarantine_root, "corrupt")
    with directory(quarantine_root, "corrupt") as damaged:
        ensure_directory(damaged, digest)
        with directory(damaged, digest) as archive:
            os.rename(digest, "legacy-" + uuid4().hex, src_dir_fd=quarantine_root, dst_dir_fd=archive)
            os.fsync(archive)
    os.fsync(quarantine_root)


def _save_witnesses(staging, quarantine_root, digest, contents):
    ensure_directory(quarantine_root, digest)
    with directory(quarantine_root, digest) as witnesses:
        for label, content in contents:
            pending = _new_file(staging, content)
            os.link(pending, label + "-" + uuid4().hex, src_dir_fd=staging, dst_dir_fd=witnesses,
                    follow_symlinks=False)
            _remove_pending(staging, pending)
            os.fsync(witnesses)


def select_current(root, staging, digest):
    _replace_pointer(root, staging, "CURRENT", digest + "\n", "replace_current")


def select_high_water(root, staging, digest, depth):
    _replace_pointer(root, staging, "HIGH_WATER", f"{digest} {depth}\n", "replace_high_water")


def _replace_pointer(root, staging, name, content, replace_error):
    pending = _new_file(staging, content.encode("ascii"))
    os.fsync(staging)
    try:
        os.replace(pending, name, src_dir_fd=staging, dst_dir_fd=root)
    except OSError as exc:
        raise RegistryError(3, replace_error) from exc
    try:
        os.fsync(root)
        os.fsync(staging)
    except OSError as exc:
        raise RegistryError(3, "commit_unconfirmed") from exc


@contextmanager
def writer(root):
    try:
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RegistryError(6, "writer_busy") from exc
    try:
        yield
    finally:
        fcntl.flock(root, fcntl.LOCK_UN)
