"""Local immutable blob references and scoped read custody."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
from hashlib import sha256
import mmap
import os
from pathlib import Path
import re
import stat
from threading import Lock
from time import monotonic_ns
from uuid import uuid4


SCHEMA = "af.artifact-ref/v1"
PREFIX = "af://sha256/"
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ArtifactError(Exception):
    def __init__(self, rc: int, reason: str):
        super().__init__(reason)
        self.rc = rc
        self.reason = reason


def _nonblank(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class ArtifactRef:
    locator: str
    sha256: str
    size_bytes: int
    media_type: str
    read_authority: str
    lifetime_id: str

    def __post_init__(self):
        if (not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256)
                or self.locator != PREFIX + self.sha256
                or type(self.size_bytes) is not int or self.size_bytes < 0
                or not all(map(_nonblank, (self.media_type, self.read_authority, self.lifetime_id)))):
            raise ArtifactError(2, "reference_invalid")

    def to_record(self):
        return {"schema": SCHEMA, **asdict(self)}

    @classmethod
    def from_record(cls, record):
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise ArtifactError(2, "reference_invalid")
        fields = record.copy()
        del fields["schema"]
        try:
            return cls(**fields)
        except TypeError as exc:
            raise ArtifactError(2, "reference_invalid") from exc


@dataclass(frozen=True)
class Access:
    """Caller and namespace supplied by the authenticated read service."""

    principal: str
    namespace: str

    def __post_init__(self):
        if not _nonblank(self.principal) or not _nonblank(self.namespace):
            raise ArtifactError(2, "access_invalid")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
            info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


@dataclass
class _Grant:
    ref: ArtifactRef
    reader: Access
    deadline_ns: int
    backing: tuple
    borrows: int = 0
    broken: bool = False


@dataclass
class TransferAccounting:
    """Observed payload bytes at one publication or read boundary."""

    logical_bytes: int | None = None
    copy_bytes: int | None = None
    materialized_bytes: int | None = None
    mapped_bytes: int | None = None
    verified_bytes: int | None = None


@contextmanager
def _payload(source):
    if type(source) is int:
        if source < 0:
            raise ArtifactError(2, "source_fd_invalid")
        fd = os.dup(source)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactError(2, "source_fd_invalid")
            if info.st_size:
                with mmap.mmap(fd, info.st_size, access=mmap.ACCESS_READ) as mapping:
                    with memoryview(mapping) as view:
                        yield view
            else:
                with memoryview(b"") as view:
                    yield view
        finally:
            os.close(fd)
        return

    try:
        source_view = memoryview(source)
    except (TypeError, ValueError) as exc:
        raise ArtifactError(2, "source_buffer_invalid") from exc
    with source_view:
        if not source_view.c_contiguous:
            raise ArtifactError(2, "source_buffer_invalid")
        with source_view.cast("B") as octets:
            yield octets


class ArtifactStore:
    """Service-owned directory of immutable blobs, with process-local read grants.

    The service must own the private root and control writers under its user ID.
    Producers keep a supplied mapping or descriptor stable until put returns.
    """

    def __init__(self, root, *, clock=monotonic_ns):
        self._root = Path(root).absolute()
        self._clock = clock
        self._lock = Lock()
        self._grants = {}
        with self._directory() as directory:
            info = os.fstat(directory)
            self._root_identity = (info.st_dev, info.st_ino)

    @contextmanager
    def _directory(self):
        try:
            fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ArtifactError(4, "store_unavailable") from exc
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ArtifactError(5, "store_not_private")
            if (info.st_dev, info.st_ino) != getattr(self, "_root_identity", (info.st_dev, info.st_ino)):
                raise ArtifactError(4, "store_stale")
            yield fd
        finally:
            os.close(fd)

    def _deadline(self, deadline_ns):
        if self._clock() >= deadline_ns:
            raise ArtifactError(6, "lifetime_expired")

    @contextmanager
    def _map_blob(self, directory, digest, size, backing=None, *, accounting=None):
        try:
            fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as exc:
            raise ArtifactError(4, "backing_stale") from exc
        try:
            info = os.fstat(fd)
            identity = _identity(info)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o222 or info.st_nlink != 1 or info.st_size != size
                    or (backing is not None and backing != identity)):
                raise ArtifactError(4, "backing_stale")
            mapping = mmap.mmap(fd, size, access=mmap.ACCESS_READ) if size else None
            view = memoryview(mapping if mapping is not None else b"")
            try:
                if accounting is not None:
                    accounting.mapped_bytes = len(mapping) if mapping is not None else 0
                    # A view of this mapping borrows its pages. A new backing
                    # buffer at this boundary materializes its exposed bytes.
                    accounting.copy_bytes = 0 if view.obj is mapping else view.nbytes
                    accounting.materialized_bytes = accounting.copy_bytes
                if sha256(view).hexdigest() != digest:
                    raise ArtifactError(4, "digest_mismatch")
                self._check_backing(directory, digest, fd, identity)
                if accounting is not None:
                    accounting.verified_bytes = view.nbytes
                try:
                    yield view, identity
                finally:
                    self._check_backing(directory, digest, fd, identity)
            finally:
                view.release()
                if mapping is not None:
                    try:
                        mapping.close()
                    except BufferError as exc:
                        raise ArtifactError(5, "view_escaped") from exc
        finally:
            os.close(fd)

    def _check_backing(self, directory, digest, fd, identity):
        try:
            entry = os.stat(digest, dir_fd=directory, follow_symlinks=False)
            root = self._root.stat(follow_symlinks=False)
        except OSError as exc:
            raise ArtifactError(4, "backing_stale") from exc
        if (_identity(entry) != identity or _identity(os.fstat(fd)) != identity
                or (root.st_dev, root.st_ino) != self._root_identity):
            raise ArtifactError(4, "backing_stale")

    def put(self, payload, *, reader, deadline_ns, media_type, expected_sha256=None, accounting=None):
        """Copy an input buffer or borrowed regular fd into the durable store."""
        if not isinstance(reader, Access) or type(deadline_ns) is not int or not _nonblank(media_type):
            raise ArtifactError(2, "put_invalid")
        if expected_sha256 is not None and (
                not isinstance(expected_sha256, str) or not _DIGEST.fullmatch(expected_sha256)):
            raise ArtifactError(2, "digest_invalid")
        self._deadline(deadline_ns)
        with self._directory() as directory:
            fcntl.flock(directory, fcntl.LOCK_EX)
            self._deadline(deadline_ns)
            try:
                with _payload(payload) as view:
                    size = view.nbytes
                    if accounting is not None:
                        accounting.logical_bytes = size
                        accounting.copy_bytes = accounting.materialized_bytes = accounting.mapped_bytes = 0
                    digest, backing = self._install(directory, view, expected_sha256, deadline_ns, accounting)
            except OSError as exc:
                raise ArtifactError(3, "storage_io") from exc
        self._deadline(deadline_ns)
        ref = ArtifactRef(PREFIX + digest, digest, size, media_type, uuid4().hex, uuid4().hex)
        with self._lock:
            self._grants[ref.lifetime_id] = _Grant(ref, reader, deadline_ns, backing)
        return ref

    def _install(self, directory, view, expected_sha256, deadline_ns, accounting):
        staged = ".put-" + uuid4().hex
        fd = os.open(staged, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            written = 0
            while written < view.nbytes:
                self._deadline(deadline_ns)
                count = os.write(fd, view[written:written + (1 << 20)])
                if count <= 0:
                    raise ArtifactError(3, "storage_io")
                written += count
                if accounting is not None:
                    accounting.copy_bytes = accounting.materialized_bytes = written
            if written:
                with mmap.mmap(fd, written, access=mmap.ACCESS_READ) as mapping:
                    digest = sha256(mapping).hexdigest()
            else:
                digest = sha256(b"").hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                raise ArtifactError(4, "digest_mismatch")
            os.fchmod(fd, 0o400)
            os.fsync(fd)
            self._deadline(deadline_ns)
            try:
                os.link(staged, digest, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            os.close(fd)
            os.unlink(staged, dir_fd=directory)
        os.fsync(directory)
        with self._map_blob(directory, digest, written) as (verified, backing):
            if accounting is not None:
                accounting.verified_bytes = verified.nbytes
            return digest, backing

    def _grant(self, ref, access):
        if not isinstance(ref, ArtifactRef) or not isinstance(access, Access):
            raise ArtifactError(2, "open_invalid")
        grant = self._grants.get(ref.lifetime_id)
        if grant is None or grant.broken or grant.reader != access or grant.ref != ref:
            raise ArtifactError(5, "read_authority")
        return grant

    @contextmanager
    def open(self, ref, access, *, accounting=None):
        """Borrow verified bytes through a read-only view scoped to this context."""
        with self._lock:
            grant = self._grant(ref, access)
            self._deadline(grant.deadline_ns)
            grant.borrows += 1
        if accounting is not None:
            accounting.logical_bytes = ref.size_bytes
            accounting.copy_bytes = accounting.materialized_bytes = 0
        broken = False
        try:
            with self._directory() as directory:
                with self._map_blob(directory, ref.sha256, ref.size_bytes, grant.backing,
                                    accounting=accounting) as (view, _):
                    self._deadline(grant.deadline_ns)
                    try:
                        yield view
                    finally:
                        self._deadline(grant.deadline_ns)
        except ArtifactError as exc:
            broken = exc.reason in {"view_escaped", "backing_stale", "digest_mismatch", "store_stale"}
            raise
        finally:
            with self._lock:
                grant.broken |= broken
                grant.borrows -= 1
                if grant.broken and not grant.borrows:
                    del self._grants[ref.lifetime_id]

    def release(self, ref, access):
        """End the read grant after all borrows have returned."""
        with self._lock:
            grant = self._grant(ref, access)
            if grant.borrows:
                raise ArtifactError(5, "borrow_active")
            del self._grants[ref.lifetime_id]
