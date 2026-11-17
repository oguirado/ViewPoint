"""Verified references to existing files under explicitly registered roots."""

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import stat
from threading import Lock
from urllib.parse import unquote, urlsplit


_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_SPEC = importlib.util.spec_from_file_location("vp_locator_schema_validation", _SCHEMAS / "validation.py")
_VALIDATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VALIDATION)

_CONTRACT = "vp.artifact_ref/v1"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CHUNK_BYTES = 1 << 20


class ArtifactError(ValueError):
    """The locator, declared identity or current file could not be verified."""


def _open_file(path):
    """Open a regular-file candidate without following links in any component."""
    directory = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, _FILE_FLAGS, dir_fd=directory)
    finally:
        os.close(directory)


def _file_identity(sample):
    return sample.st_dev, sample.st_ino, sample.st_size, sample.st_mtime_ns, sample.st_ctime_ns


def _read_identity(path):
    digest = hashlib.sha256()
    size = 0
    try:
        file_fd = _open_file(path)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactError("artifact must be a regular file")
            while size < before.st_size:
                chunk = os.read(file_fd, min(_CHUNK_BYTES, before.st_size - size))
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(file_fd)
            current_fd = _open_file(path)
            try:
                current = os.fstat(current_fd)
            finally:
                os.close(current_fd)
            if size != before.st_size or _file_identity(before) != _file_identity(after):
                raise ArtifactError("artifact changed during verification")
            if _file_identity(after) != _file_identity(current):
                raise ArtifactError("artifact path changed during verification")
        finally:
            os.close(file_fd)
    except OSError as error:
        raise ArtifactError("artifact is unavailable or has a symbolic link in its path") from error
    return digest.hexdigest(), size


class ArtifactLocator:
    """Implement the frozen vp_data_call resolve and put payloads.

    Each af://<root>/<path> names a file relative to a configured root. Local
    file:/// paths must be inside a configured root as well. Put binds verified
    descriptor metadata in this instance. Resolve requires that binding and
    rereads the current file. A binding is immutable for the instance lifetime.
    """

    def __init__(self, roots):
        self._roots = {}
        for name, directory in roots.items():
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None:
                raise ArtifactError("invalid artifact root name")
            root = Path(directory)
            if not root.is_absolute() or not root.is_dir():
                raise ArtifactError("artifact root must be an existing absolute directory")
            self._roots[name] = root.resolve(strict=True)
        self._refs = {}
        self._lock = Lock()

    def _path(self, locator):
        try:
            uri = urlsplit(locator)
            path = unquote(uri.path, errors="strict")
        except ValueError as error:
            raise ArtifactError("invalid artifact locator") from error
        if "?" in locator or "#" in locator or re.search(r"%(?![0-9a-fA-F]{2})", uri.path):
            raise ArtifactError("invalid artifact locator")
        parts = path[1:].split("/")
        if not path.startswith("/") or "\x00" in path or any(part in ("", ".", "..") for part in parts):
            raise ArtifactError("locator is outside registered roots or has an invalid path")
        if uri.scheme == "af":
            root = self._roots.get(uri.netloc)
            if root is None:
                raise ArtifactError("locator is outside registered roots")
            return root.joinpath(*parts)
        if uri.scheme == "file" and not uri.netloc:
            candidate = Path(path)
            if any(candidate != root and candidate.is_relative_to(root) for root in self._roots.values()):
                return candidate
        raise ArtifactError("locator is outside registered roots")

    def _verified(self, path, ref, expected_sha256):
        actual_sha256, size = _read_identity(path)
        if actual_sha256 != expected_sha256 or actual_sha256 != ref["sha256"]:
            raise ArtifactError("artifact digest mismatch")
        if size != ref["bytes"]:
            raise ArtifactError("artifact byte count mismatch")
        return dict(ref, sha256=actual_sha256, bytes=size, copy="none")

    def put(self, artifact_ref, *, dry_run=False):
        """Verify a descriptor before registering it and return the verified reference."""
        ref = dict(artifact_ref)
        _VALIDATION.validate_contract("vp_data_call.put.args/v1", {"op": "put", "artifact_ref": ref})
        locator = ref["locator"]
        receipt = self._verified(self._path(locator), ref, ref["sha256"])
        _VALIDATION.validate_contract("vp_data_call.put.result/v1", receipt)
        with self._lock:
            prior = self._refs.get(locator)
            if prior is not None and prior != receipt:
                raise ArtifactError("artifact locator already identifies a different descriptor")
            if not dry_run:
                self._refs[locator] = receipt.copy()
        return receipt

    def resolve(self, locator, sha256):
        """Return a fresh receipt only if the registered descriptor still matches."""
        args = {"op": "resolve", "locator": locator, "sha256": sha256}
        _VALIDATION.validate_contract("vp_data_call.resolve.args/v1", args)
        path = self._path(locator)
        with self._lock:
            ref = self._refs.get(locator)
        if ref is None:
            raise ArtifactError("artifact descriptor is not registered")
        receipt = self._verified(path, ref, sha256)
        _VALIDATION.validate_contract("vp_data_call.resolve.result/v1", receipt)
        return receipt

    def handle(self, args, *, dry_run=False):
        """Accept the closed adapter args for either D2 operation."""
        _VALIDATION.validate_contract("vp_data_call.args/v1", args)
        if args["op"] == "put":
            return self.put(args["artifact_ref"], dry_run=dry_run)
        if args["op"] == "resolve":
            return self.resolve(args["locator"], args["sha256"])
        raise ArtifactError("operation is outside the artifact locator")
