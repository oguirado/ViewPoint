"""Descriptor-relative local publisher storage on Linux."""

from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import time


class LocalPathRefusal(ValueError):
    pass


class CredentialPathRefusal(LocalPathRefusal):
    pass


class LocalBudgetExceeded(Exception):
    pass


_CREDENTIAL_NAMES = frozenset((
    "gemini", "ssh", "aws", "gnupg", "azure", "kube", "config",
    "netrc", "npmrc", "git-credentials", "pypirc", "docker", "dockercfg", "env",
    "auth.json", "rclone.conf", "operator.token",
    "launch.token", "token", "credentials.json", "credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk", "identity",
))
_BACKUP_SUFFIXES = re.compile(r"(?:~+|\.(?:bak|backup|old|orig|save|swp))+\Z")
_CREDENTIAL_EXTENSIONS = (".key", ".pem", ".p12", ".pfx")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_WRITE_TEMP = re.compile(r"\.publish-[0-9a-f]{32}\Z")
_RENAME_NOREPLACE = 1
_AT_SYMLINK_NOFOLLOW = 0x100
_AT_EMPTY_PATH = 0x1000
_STATX_MNT_ID = 0x1000
# Linux UAPI struct statx is 256 bytes, with stx_mask at 0 and stx_mnt_id at 0x90.
_STATX = struct.Struct("=I140xQ104x")


def check_names(parts, *, allow_config_directory=False):
    for part in parts:
        low = part.casefold()
        if allow_config_directory and low == ".config":
            continue
        normalized = _BACKUP_SUFFIXES.sub("", low.lstrip("._"))
        if (low.lstrip("_").startswith(".env")
                or ("." + normalized).endswith(_CREDENTIAL_EXTENSIONS)
                or normalized in _CREDENTIAL_NAMES
                or any(normalized.startswith(name + ".") for name in _CREDENTIAL_NAMES
                       if name != "config")):
            raise CredentialPathRefusal()


def mount_id(parent, name=""):
    """Read Linux's mount ID for an open descriptor or a no-follow entry."""
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "statx"):
        raise LocalPathRefusal()
    call = libc.statx
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p)
    call.restype = ctypes.c_int
    info = ctypes.create_string_buffer(_STATX.size)
    flags = _AT_SYMLINK_NOFOLLOW if name else _AT_EMPTY_PATH
    if call(parent, os.fsencode(name), flags, _STATX_MNT_ID, ctypes.byref(info)):
        raise OSError(ctypes.get_errno(), "mount identity unavailable")
    mask, identity = _STATX.unpack(info)
    if not mask & _STATX_MNT_ID:
        raise LocalPathRefusal()
    return identity


def directory_identity(info):
    return info.st_dev, info.st_ino


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def bound_directory(parent, name, child):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or directory_identity(info) != directory_identity(os.fstat(child)):
        raise LocalPathRefusal()
    expected = mount_id(parent)
    if mount_id(child) != expected or mount_id(parent, name) != expected:
        raise LocalPathRefusal()


def child_directory(parent, name, *, create=False, same_mount=True):
    if not name or name in (".", "..") or "/" in name:
        raise LocalPathRefusal()
    try:
        child = os.open(name, _DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        child = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        if same_mount and mount_id(child) != mount_id(parent):
            raise LocalPathRefusal()
        return child
    except BaseException:
        os.close(child)
        raise


def remove_tree(parent, name):
    """Remove private staging entries without descending into another mount."""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        if mount_id(parent, name) != mount_id(parent):
            raise LocalPathRefusal()
        os.unlink(name, dir_fd=parent)
        return
    child = child_directory(parent, name)
    try:
        bound_directory(parent, name, child)
        for entry in os.listdir(child):
            remove_tree(child, entry)
        bound_directory(parent, name, child)
        os.rmdir(name, dir_fd=parent)
    finally:
        os.close(child)


def rename_noreplace(source_parent, source, target_parent, target):
    """Move a named directory between pinned parents, without replacement."""
    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        raise NotImplementedError
    call = libc.renameat2
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    if call(source_parent, os.fsencode(source), target_parent, os.fsencode(target), _RENAME_NOREPLACE):
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(code, "destination already exists")
        raise OSError(code, "atomic import failed")


class LocalStore:
    def __init__(self, root, timeout_s, max_entries):
        self.root = Path(root)
        if self.root.anchor != "/" or self.root == Path("/") or ".." in self.root.parts:
            raise LocalPathRefusal()
        self.deadline = time.monotonic() + timeout_s
        self.max_entries = max_entries
        self.entries = 0

    def _tick(self, *, entry=False):
        self.entries += int(entry)
        if self.entries > self.max_entries or time.monotonic() >= self.deadline:
            raise LocalBudgetExceeded()

    def _descend(self, fd, parts, *, create=False, same_mount=True):
        try:
            for part in parts:
                self._tick()
                child = child_directory(fd, part, create=create, same_mount=same_mount)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _directory(self, path, *, create=False):
        if path.anchor != "/" or ".." in path.parts:
            raise LocalPathRefusal()
        # The trusted configured root may itself live on a separate mount.
        return self._descend(os.open("/", _DIRECTORY), path.parts[1:], create=create, same_mount=False)

    @staticmethod
    def parts(relative):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise LocalPathRefusal()
        check_names(path.parts)
        return path.parts

    @contextmanager
    def directory(self, root_fd, relative="", *, create=False):
        parts = self.parts(relative)
        fd = self._descend(os.dup(root_fd), parts, create=create)
        try:
            yield fd
        finally:
            os.close(fd)

    def assert_root(self, fd):
        current = self._directory(self.root)
        try:
            if (directory_identity(os.fstat(current)) != directory_identity(os.fstat(fd))
                    or mount_id(current) != mount_id(fd)):
                raise LocalPathRefusal()
        finally:
            os.close(current)

    def _file(self, parent, name):
        check_names((name,))
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        expected_mount = mount_id(parent)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or mount_id(parent, name) != expected_mount):
            raise LocalPathRefusal()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if signature(info) != signature(os.fstat(fd)) or mount_id(fd) != expected_mount:
                raise LocalPathRefusal()
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def _bound_parent(self, root_fd, parts, parent):
        if parts:
            with self.directory(root_fd, Path(*parts[:-1])) as ancestor:
                bound_directory(ancestor, parts[-1], parent)

    @contextmanager
    def open_file(self, relative, *, root_fd=None):
        parts = self.parts(relative)
        if not parts:
            raise LocalPathRefusal()
        owned = root_fd is None
        if owned:
            root_fd = self._directory(self.root)
        try:
            with self.directory(root_fd, Path(*parts[:-1])) as parent:
                self._tick()
                with self._file(parent, parts[-1]) as stream:
                    before = signature(os.fstat(stream.fileno()))
                    yield stream
                    after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    if (before != signature(after) or before != signature(os.fstat(stream.fileno()))
                            or mount_id(parent, parts[-1]) != mount_id(parent)):
                        raise LocalPathRefusal()
                if owned:
                    self.assert_root(root_fd)
                self._bound_parent(root_fd, parts[:-1], parent)
        finally:
            if owned:
                os.close(root_fd)

    def walk(self, fd, *, prefix="", ignore_write_temps=False, directories=None):
        """Yield regular file names and their open parent; keep ancestor fds live."""
        names = sorted(os.listdir(fd))
        check_names(names)
        if directories is not None:
            directories[prefix] = signature(os.fstat(fd)), tuple(names)
        for name in names:
            self._tick(entry=True)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + "/" + name if prefix else name
            if stat.S_ISDIR(info.st_mode):
                child = child_directory(fd, name)
                try:
                    bound_directory(fd, name, child)
                    yield from self.walk(child, prefix=relative, ignore_write_temps=ignore_write_temps,
                                         directories=directories)
                    bound_directory(fd, name, child)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if mount_id(fd, name) != mount_id(fd):
                    raise LocalPathRefusal()
                # A killed publisher can leave its private same-directory temp.
                if ignore_write_temps and _WRITE_TEMP.fullmatch(name):
                    continue
                if info.st_nlink != 1:
                    raise LocalPathRefusal()
                yield relative, fd, name
            else:
                raise LocalPathRefusal()

    def digest(self, stream, *, capture_limit=None):
        sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
        before = signature(os.fstat(stream.fileno()))
        chunks = []
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            self._tick()
            size += len(block)
            sha.update(block)
            md5.update(block)
            if capture_limit is not None:
                if size > capture_limit:
                    raise LocalBudgetExceeded()
                chunks.append(block)
        if before != signature(os.fstat(stream.fileno())):
            raise LocalPathRefusal()
        return {"size": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}, b"".join(chunks)

    def put(self, root_fd, relative, incoming, *, mutable=False):
        """Sync private bytes, install the entry, then sync its owning directory."""
        parts = self.parts(relative)
        if not parts:
            raise LocalPathRefusal()
        with self.directory(root_fd, Path(*parts[:-1]), create=True) as parent:
            name = parts[-1]
            temporary = ".publish-" + secrets.token_hex(16)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    for block in iter(lambda: incoming.read(1024 * 1024), b""):
                        self._tick()
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                self._bound_parent(root_fd, parts[:-1], parent)
                if mutable:
                    try:
                        with self._file(parent, name):
                            pass
                    except FileNotFoundError:
                        pass
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                    temporary = None
                    self._bound_parent(root_fd, parts[:-1], parent)
                else:
                    try:
                        rename_noreplace(parent, temporary, parent, name)
                    except FileExistsError:
                        with self._file(parent, name) as existing, self._file(parent, temporary) as candidate:
                            if self.digest(existing)[0] != self.digest(candidate)[0]:
                                raise LocalPathRefusal()
                            os.fsync(existing.fileno())
                    else:
                        temporary = None
                os.fsync(parent)
            finally:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=parent)

    def read_tree(self, fd, *, capture_limit, capture_name, expected):
        """Read and authenticate file bytes on a pinned directory descriptor."""
        self.entries = 0
        records, identities, directories = {}, {}, {}
        for relative, parent, name in self.walk(fd, directories=directories):
            if relative not in expected:
                raise LocalPathRefusal()
            with self._file(parent, name) as stream:
                identity = signature(os.fstat(stream.fileno()))
                capture = capture_limit if capture_name(relative) else None
                info, raw = self.digest(stream, capture_limit=capture)
            identities[relative] = identity
            if capture is not None:
                capture_limit -= len(raw)
            records[relative] = info, raw
        self.entries = 0
        current_directories = {}
        current = {relative: signature(os.stat(name, dir_fd=parent, follow_symlinks=False))
                   for relative, parent, name in self.walk(fd, directories=current_directories)}
        expected_dirs = {""}
        for name in expected:
            expected_dirs.update(str(parent) for parent in Path(name).parents if parent != Path("."))
        if set(directories) != expected_dirs or directories != current_directories or current != identities:
            raise LocalPathRefusal()
        return records
