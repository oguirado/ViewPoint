# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable executable custody for REA slot host launches.

This module is the Linux execution-custody seam (U08, findings C08/C09/C20).
The runner never measures one file and then opens a different one to
execute: the source is opened exactly once with ``O_RDONLY | O_NOFOLLOW``,
its bytes are hashed from that descriptor, and only a verified byte copy
staged in a host-owned directory is launched.  The staged object itself is
re-verified from an open descriptor immediately before spawn, and the child
is exec'd through that descriptor (``/proc/self/fd/<fd>`` with
``pass_fds``), so the launched inode is the measured inode.

Throughput is bounded by streaming reads (constant memory).  Every failure
is fail-closed: no process is started for a mismatched digest, a symbolic
link, a non-regular file, a rejected typed argument, or an untrusted
staging directory.

Only the admitted ``cli_subcommand`` adapter derives argv: the transport
``argv_prefix``, the literal operation ``binding.argv``, and the
adapter-resolved ``typed_args`` values in declared ``position`` order.
Operations that declare no ``typed_args`` keep the legacy free-form argv
pass-through (existing fixtures), but once a binding declares
``typed_args`` the caller argv must match their count and declared types
exactly; raw extra argv is refused.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

_CHUNK = 1 << 20
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_INT_RE = re.compile(r"-?(0|[1-9][0-9]*)\Z")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ExecutableCustodyError(ValueError):
    """Fail-closed custody refusal; no process was started."""

    def __init__(self, reason: str, detail: str, observed_sha256: str | None = None):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.observed_sha256 = observed_sha256

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}"


def _require_sha(expected: object) -> str:
    if type(expected) is not str or len(expected) != 64 or _SHA256_RE.fullmatch(expected) is None:
        raise ExecutableCustodyError("pin_invalid", f"expected implementation_sha256 pin is invalid: {expected!r}")
    return expected


def _sha256_fd(fd: int) -> str:
    """SHA-256 over a regular-file descriptor read with pread (no seek)."""
    hasher = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, _CHUNK, offset)
        if not chunk:
            break
        hasher.update(chunk)
        offset += len(chunk)
    return hasher.hexdigest()


def _open_source(path: str | os.PathLike) -> tuple[int, int]:
    """Open an executable O_NOFOLLOW; require a regular, executable file.

    Returns ``(fd, mode)``; the caller closes the descriptor.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ExecutableCustodyError("cannot_open", f"cannot open executable: {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise ExecutableCustodyError("cannot_open", f"cannot stat executable: {path}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise ExecutableCustodyError("not_regular_file", f"executable is not a regular file: {path}")
    if not (st.st_mode & 0o111):
        os.close(fd)
        raise ExecutableCustodyError("not_executable", f"executable has no execute bit: {path}")
    return fd, st.st_mode


def measure_verified_executable(
    path: str | os.PathLike, expected_sha256: str,
) -> tuple[str, tuple[int, int, int]]:
    """Hash and verify an executable from a single O_NOFOLLOW open.

    Returns the verified digest and file identity. Raises :class:`ExecutableCustodyError`
    on symlink, non-regular file, missing execute bit, I/O failure, or
    digest mismatch.  The descriptor is closed before return.
    """
    pin = _require_sha(expected_sha256)
    fd, _mode = _open_source(path)
    try:
        identity = executable_identity(fd)
        digest = _sha256_fd(fd)
        if executable_identity(fd) != identity:
            raise ExecutableCustodyError("source_identity_mismatch", "source changed during measurement")
    except OSError as exc:
        raise ExecutableCustodyError("hash_failed", f"cannot hash executable: {path}: {exc}") from exc
    finally:
        os.close(fd)
    if digest != pin:
        raise ExecutableCustodyError(
            "digest_mismatch",
            f"observed {digest} != pinned {pin}",
            observed_sha256=digest,
        )
    return digest, identity


def hash_verified_executable(path: str | os.PathLike, expected_sha256: str) -> str:
    """Return the digest of the verified executable."""
    return measure_verified_executable(path, expected_sha256)[0]


def _sanitize_name(name: str) -> str:
    if type(name) is not str or not name or name in (".", ".."):
        raise ExecutableCustodyError("bad_name", f"invalid staging file name: {name!r}")
    if "/" in name or "\\" in name or "\0" in name or Path(name).name != name:
        raise ExecutableCustodyError("bad_name", f"invalid staging file name: {name!r}")
    return name


def executable_identity(fd: int) -> tuple[int, int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_ctime_ns


def stage_verified_executable(
    src_path: str | os.PathLike,
    expected_sha256: str,
    stage_dir: str | os.PathLike,
    name: str,
    *,
    source_identity: tuple[int, int, int] | None = None,
) -> Path:
    """Hash-and-copy a verified executable into a host-owned stage dir.

    The source is opened once with ``O_RDONLY | O_NOFOLLOW``; bytes are
    streamed from that descriptor to a new file created with
    ``O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW`` inside *stage_dir*
    (which the runner creates with :func:`tempfile.mkdtemp`, mode 0700).
    The digest of the copied byte stream is verified against
    *expected_sha256*, and the staged file is re-verified from its own
    descriptor.  No pre-existing or symlinked destination is ever followed
    or overwritten.  Returns the snapshot path or raises
    :class:`ExecutableCustodyError` fail-closed.
    """
    pin = _require_sha(expected_sha256)
    safe_name = _sanitize_name(name)
    _stage = str(stage_dir)
    if os.path.islink(_stage) or not os.path.isdir(_stage):
        raise ExecutableCustodyError("bad_stage_dir", f"staging directory is not trusted: {_stage}")
    sfd, _s_mode = _open_source(src_path)
    dest = os.path.join(_stage, safe_name)
    try:
        if source_identity is not None and executable_identity(sfd) != source_identity:
            raise ExecutableCustodyError("source_identity_mismatch", "source object changed")
        dfd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o500)
    except OSError as exc:
        os.close(sfd)
        raise ExecutableCustodyError("cannot_stage", f"cannot create snapshot {dest}: {exc}") from exc
    except BaseException:
        os.close(sfd)
        raise
    try:
        hasher = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(sfd, _CHUNK, offset)
            if not chunk:
                break
            hasher.update(chunk)
            view = memoryview(chunk)
            while view:
                done = os.write(dfd, view)
                if done <= 0:
                    raise OSError("short write while staging executable")
                view = view[done:]
            offset += len(chunk)
        if source_identity is not None and executable_identity(sfd) != source_identity:
            raise ExecutableCustodyError("source_identity_mismatch", "source object changed during staging")
        os.fsync(dfd)
        os.fchmod(dfd, 0o500)
        copied_sha = hasher.hexdigest()
        if copied_sha != pin:
            raise ExecutableCustodyError(
                "digest_mismatch",
                f"staged {copied_sha} != pinned {pin}",
                observed_sha256=copied_sha,
            )
    except BaseException:
        os.close(sfd)
        try:
            os.close(dfd)
        except OSError:
            pass
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    try:
        os.close(sfd)
    except OSError:
        pass
    try:
        os.close(dfd)
    except OSError:
        pass
    try:
        rfd = os.open(dest, os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ExecutableCustodyError("cannot_stage", f"cannot reopen snapshot {dest}: {exc}") from exc
    try:
        vst = os.fstat(rfd)
        if not stat.S_ISREG(vst.st_mode):
            raise ExecutableCustodyError("not_regular_file", f"snapshot is not a regular file: {dest}")
        snap_sha = _sha256_fd(rfd)
    finally:
        os.close(rfd)
    if snap_sha != pin:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise ExecutableCustodyError(
            "digest_mismatch",
            f"snapshot {snap_sha} != pinned {pin}",
            observed_sha256=snap_sha,
        )
    return Path(dest)


def open_launch_snapshot(snapshot_path: str | os.PathLike, expected_sha256: str) -> int:
    """Return a sealed, digest-verified object for execution through /proc/self/fd."""
    pin = _require_sha(expected_sha256)
    source, mode = _open_source(snapshot_path)
    sealed = None
    try:
        sealed = os.memfd_create("rea-launch", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        with os.fdopen(source, "rb", closefd=False) as src:
            with os.fdopen(sealed, "wb", closefd=False) as dst:
                shutil.copyfileobj(src, dst, _CHUNK)
        os.fchmod(sealed, mode & 0o500)
        fcntl.fcntl(sealed, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
                    | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        digest = _sha256_fd(sealed)
        if digest != pin:
            raise ExecutableCustodyError(
                "digest_mismatch", f"snapshot {digest} != pinned {pin}", observed_sha256=digest,
            )
        return os.open(f"/proc/self/fd/{sealed}", os.O_RDONLY | os.O_CLOEXEC)
    finally:
        if sealed is not None:
            os.close(sealed)
        os.close(source)


def filtered_env(
    manifest_allowlist,
    host_allowlist,
    overrides: dict | None = None,
) -> dict[str, str]:
    """Intersect manifest and host environment allowlists (C20).

    Values come from the host process environment; explicit *overrides*
    are honoured only for names in the intersection.  No other name can
    enter the launched environment.
    """
    manifest_names = {n for n in (manifest_allowlist or ()) if type(n) is str}
    host_names = {n for n in (host_allowlist or ()) if type(n) is str}
    allowed = manifest_names & host_names
    env: dict[str, str] = {}
    for name in sorted(allowed):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if overrides:
        for key, value in overrides.items():
            if key in allowed:
                env[key] = str(value)
    return env


def _render_typed(declared_type: object, value: str) -> str:
    if type(value) is not str or "\0" in value:
        raise ExecutableCustodyError("typed_arg_invalid", "typed argument must be a string without NUL bytes")
    if declared_type == "string":
        return value
    if declared_type == "path":
        if not value:
            raise ExecutableCustodyError("typed_arg_invalid", "path typed argument must be nonempty")
        return value
    if declared_type == "integer":
        if _INT_RE.fullmatch(value) is None:
            raise ExecutableCustodyError("typed_arg_invalid", f"integer typed argument is invalid: {value!r}")
        return value
    if declared_type == "boolean":
        if value not in ("true", "false"):
            raise ExecutableCustodyError("typed_arg_invalid", f"boolean typed argument must be true/false: {value!r}")
        return value
    raise ExecutableCustodyError("typed_arg_unsupported", f"unsupported typed argument type: {declared_type!r}")


def build_typed_tail(binding, argv: list[str]) -> list[str]:
    """Derive the typed argument tail for *argv* per the admitted adapter.

    A ``cli_subcommand`` binding that declares ``typed_args`` requires
    *argv* to supply exactly those positional values in declared
    ``position`` order, each matching its declared type; anything else
    (wrong arity, wrong type, raw extra argv) raises
    :class:`ExecutableCustodyError`.  Bindings without declared
    ``typed_args`` keep the legacy pass-through.
    """
    if type(argv) is not list or any(type(w) is not str for w in argv):
        raise ExecutableCustodyError("argv_invalid", "argv must be a list of strings")
    bnd = getattr(binding, "operation_binding", None)
    if not isinstance(bnd, dict):
        bnd = {}
    if bnd.get("kind") != "cli_subcommand":
        return list(argv)
    if "typed_args" not in bnd:
        return list(argv)
    typed = bnd["typed_args"]
    if not isinstance(typed, list) or any(not isinstance(e, dict) for e in typed):
        raise ExecutableCustodyError("argv_invalid", "typed_args must be a list of objects")
    if len(argv) != len(typed):
        raise ExecutableCustodyError(
            "argv_arity",
            f"expected {len(typed)} typed arguments, got {len(argv)}",
        )
    ordered = sorted(
        typed,
        key=lambda e: e.get("position") if type(e.get("position")) is int else 0,
    )
    tail: list[str] = []
    for entry, value in zip(ordered, argv):
        tail.append(_render_typed(entry.get("type"), value))
    return tail


def build_launch_argv(
    binding,
    launch_target: str,
    argv: list[str],
    typed_tail: list[str] | None = None,
) -> list[str]:
    """Assemble ``[launch_target, *argv_prefix, *subcommand, *typed]``.

    When *typed_tail* is omitted it is derived from *argv* by
    :func:`build_typed_tail`; when given it must be the value that
    function returned for the same *binding* and *argv*.
    """
    if type(launch_target) is not str or not launch_target or "\0" in launch_target:
        raise ExecutableCustodyError("launch_target_invalid", "launch target must be a nonempty string")
    if typed_tail is None:
        typed_tail = build_typed_tail(binding, argv)
    if type(typed_tail) is not list or any(type(w) is not str for w in typed_tail):
        raise ExecutableCustodyError("argv_invalid", "typed argument tail must be a list of strings")
    prefix = list(getattr(binding, "argv_prefix", ()) or ())
    if any(type(w) is not str for w in prefix):
        raise ExecutableCustodyError("argv_invalid", "transport argv_prefix must be a list of strings")
    bnd = getattr(binding, "operation_binding", None)
    if not isinstance(bnd, dict):
        bnd = {}
    sub: list[str] = []
    if bnd.get("kind") == "cli_subcommand":
        raw_sub = bnd.get("argv") or []
        if type(raw_sub) is not list or any(type(w) is not str for w in raw_sub):
            raise ExecutableCustodyError("argv_invalid", "operation binding argv must be a list of strings")
        sub = list(raw_sub)
    return [launch_target, *prefix, *sub, *typed_tail]
