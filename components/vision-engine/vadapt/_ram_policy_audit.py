"""proto1_ram_policy: ram policy audit."""
from __future__ import annotations
import builtins
import copy
from dataclasses import dataclass, fields
import hashlib
import io
import json
import os
import pathlib
from pathlib import Path
import sys
import threading
import zlib
import numpy as np
from vadapt import ramws, rtring, w8u_memory, window_stream_bank

from ._ram_policy_contract import (
    AUSENTE, PREPARED, ACCEPT, CLAIM_LABELS, CONFINED_ROOT, CONFINED_TOOL, CONFINED_CAMPAIGN,
    ACCEPTED_RAM_FSTYPES, CLOSED_ERROR_CODES, FRAME_IDENTITY_FIELDS, CLOSED_SEAT_RECORD_KEYS,
    FORBIDDEN_PIXEL_KEYS, AUTHORITATIVE_RECEIPT_EXTENSION_VALIDATOR, CONDUCTOR_LANE_GATES,
    PUBLIC_SIGNATURES, _HEX64, RamPolicyError, _require_exact_int, _require_hex64,
    _normalize_handle, translate_storage_error, FrameIdentity, BoundFrameReference,
    MeasurementRecord, AuthenticatedRunRoot, CleanupAuthorityWitness, make_immutable_rgb24,
    preflight_workspace_relpath, validate_ram_root_residency,
    _looks_like_reconstructible_pixels, validate_metadata_no_pixels,
    validate_closed_seat_record, join_bound_references,
)

def _is_write_mode(mode: object) -> bool:
    if type(mode) is not str:
        return False
    return any(ch in mode for ch in ("w", "a", "x", "+"))


def _is_write_flags(flags: object) -> bool:
    if type(flags) is not int:
        return False
    write_mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    return bool(flags & write_mask)


def _is_frame_like_path(target: object) -> bool:
    if target is None or isinstance(target, int):
        return False
    try:
        path_str = os.fspath(target)
    except TypeError:
        path_str = str(target)
    if isinstance(path_str, bytes):
        path_str = path_str.decode("utf-8", errors="replace")
    name = os.path.basename(path_str)
    return name.lower().endswith(ramws.FRAME_EXT) or name in ramws.FRAME_NAMES


def _is_allowed_ramws_control_path(target: object) -> bool:
    if target is None or isinstance(target, int):
        return False
    try:
        path_str = os.fspath(target)
    except TypeError:
        return False
    if isinstance(path_str, bytes):
        path_str = path_str.decode("utf-8", errors="replace")
    norm = os.path.normpath(path_str)
    base = os.path.basename(norm)
    return norm.startswith(CONFINED_ROOT + "/") and base in (".lock", "owner.json")


class PersistenceWriteSpy:
    """Intercepts real persistence entry points (builtins.open, os.open, io, pathlib, NumPy writers)."""

    def __init__(self, *, allow_ramws_control_files: bool = False, refusal_code: str = "E_ROOT"):
        self.allow_ramws_control_files = bool(allow_ramws_control_files)
        self.refusal_code = refusal_code
        self.intercepted_attempts: list[dict[str, object]] = []
        self._active = False
        self._orig_builtins_open = None
        self._orig_os_open = None
        self._orig_io_open = None
        self._orig_io_fileio = None
        self._orig_path_open = None
        self._orig_path_write_bytes = None
        self._orig_path_write_text = None
        self._orig_np_save = None
        self._orig_np_savez = None
        self._orig_np_savez_compressed = None
        self._orig_np_savetxt = None

    def _record_and_refuse(self, entry_point: str, target: object, detail: str) -> None:
        record = {
            "entry_point": entry_point,
            "target": str(target),
            "detail": detail,
        }
        self.intercepted_attempts.append(record)
        raise RamPolicyError(
            self.refusal_code,
            "persistence write intercepted at %s on %r (%s)" % (entry_point, target, detail),
            detail=record,
        )

    def __enter__(self) -> PersistenceWriteSpy:
        if self._active:
            raise RamPolicyError("E_BINDING", "PersistenceWriteSpy is already active")
        self._active = True
        spy = self

        self._orig_builtins_open = builtins.open
        self._orig_os_open = os.open
        self._orig_io_open = io.open
        self._orig_io_fileio = io.FileIO
        self._orig_path_open = pathlib.Path.open
        self._orig_path_write_bytes = pathlib.Path.write_bytes
        self._orig_path_write_text = pathlib.Path.write_text
        self._orig_np_save = np.save
        self._orig_np_savez = np.savez
        self._orig_np_savez_compressed = np.savez_compressed
        self._orig_np_savetxt = np.savetxt

        orig_b_open = self._orig_builtins_open
        orig_o_open = self._orig_os_open
        orig_i_open = self._orig_io_open
        orig_fileio = self._orig_io_fileio
        orig_p_open = self._orig_path_open
        orig_p_write_text = self._orig_path_write_text

        def _guarded_builtins_open(file, mode="r", *args, **kwargs):
            if _is_frame_like_path(file) or _is_write_mode(mode):
                if not (spy.allow_ramws_control_files and _is_allowed_ramws_control_path(file) and not _is_frame_like_path(file)):
                    spy._record_and_refuse("builtins.open", file, "mode=%r" % (mode,))
            return orig_b_open(file, mode, *args, **kwargs)

        def _guarded_os_open(path, flags, mode=0o777, *args, **kwargs):
            if _is_frame_like_path(path) or _is_write_flags(flags):
                if not (spy.allow_ramws_control_files and _is_allowed_ramws_control_path(path) and not _is_frame_like_path(path)):
                    spy._record_and_refuse("os.open", path, "flags=0x%x" % (flags if type(flags) is int else 0))
            return orig_o_open(path, flags, mode, *args, **kwargs)

        def _guarded_io_open(file, mode="r", *args, **kwargs):
            if _is_frame_like_path(file) or _is_write_mode(mode):
                if not (spy.allow_ramws_control_files and _is_allowed_ramws_control_path(file) and not _is_frame_like_path(file)):
                    spy._record_and_refuse("io.open", file, "mode=%r" % (mode,))
            return orig_i_open(file, mode, *args, **kwargs)

        class _GuardedFileIO(orig_fileio):
            def __init__(self, file, mode="r", *args, **kwargs):
                if _is_frame_like_path(file) or _is_write_mode(mode):
                    if not (spy.allow_ramws_control_files and _is_allowed_ramws_control_path(file) and not _is_frame_like_path(file)):
                        spy._record_and_refuse("io.FileIO", file, "mode=%r" % (mode,))
                super().__init__(file, mode, *args, **kwargs)

        def _guarded_path_open(self_path, mode="r", *args, **kwargs):
            if _is_frame_like_path(self_path) or _is_write_mode(mode):
                if not (spy.allow_ramws_control_files and _is_allowed_ramws_control_path(self_path) and not _is_frame_like_path(self_path)):
                    spy._record_and_refuse("pathlib.Path.open", self_path, "mode=%r" % (mode,))
            return orig_p_open(self_path, mode, *args, **kwargs)

        def _guarded_write_bytes(self_path, data):
            spy._record_and_refuse("pathlib.Path.write_bytes", self_path, "len=%d" % (len(data) if isinstance(data, (bytes, bytearray, memoryview)) else -1))

        def _guarded_write_text(self_path, data, *args, **kwargs):
            if spy.allow_ramws_control_files and _is_allowed_ramws_control_path(self_path) and not _is_frame_like_path(self_path):
                return orig_p_write_text(self_path, data, *args, **kwargs)
            spy._record_and_refuse("pathlib.Path.write_text", self_path, "text_write")

        def _guarded_np_save(file, arr, *args, **kwargs):
            spy._record_and_refuse("numpy.save", file, "ndarray_save")

        def _guarded_np_savez(file, *args, **kwargs):
            spy._record_and_refuse("numpy.savez", file, "ndarray_savez")

        def _guarded_np_savez_compressed(file, *args, **kwargs):
            spy._record_and_refuse("numpy.savez_compressed", file, "ndarray_savez_compressed")

        def _guarded_np_savetxt(fname, X, *args, **kwargs):
            spy._record_and_refuse("numpy.savetxt", fname, "ndarray_savetxt")

        builtins.open = _guarded_builtins_open
        os.open = _guarded_os_open
        io.open = _guarded_io_open
        io.FileIO = _GuardedFileIO
        pathlib.Path.open = _guarded_path_open
        pathlib.Path.write_bytes = _guarded_write_bytes
        pathlib.Path.write_text = _guarded_write_text
        np.save = _guarded_np_save
        np.savez = _guarded_np_savez
        np.savez_compressed = _guarded_np_savez_compressed
        np.savetxt = _guarded_np_savetxt
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        builtins.open = self._orig_builtins_open
        os.open = self._orig_os_open
        io.open = self._orig_io_open
        io.FileIO = self._orig_io_fileio
        pathlib.Path.open = self._orig_path_open
        pathlib.Path.write_bytes = self._orig_path_write_bytes
        pathlib.Path.write_text = self._orig_path_write_text
        np.save = self._orig_np_save
        np.savez = self._orig_np_savez
        np.savez_compressed = self._orig_np_savez_compressed
        np.savetxt = self._orig_np_savetxt
        self._active = False
        return False


def install_irreversible_persistence_audit_hook(*, refusal_code: str = "E_ROOT") -> list[tuple[str, object, object]]:
    """Install a global irreversible CPython sys.addaudithook blocking persistence writes and frame files.

    Never weakened with disable flags; must only be invoked inside an isolated child process.
    """
    intercepted: list[tuple[str, object, object]] = []

    def _audit_hook(event: str, args: tuple) -> None:
        if event == "open":
            path_or_fd = args[0] if len(args) > 0 else None
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            if _is_frame_like_path(path_or_fd) or _is_write_mode(mode) or _is_write_flags(flags):
                intercepted.append((str(path_or_fd), mode, flags))
                raise RamPolicyError(
                    refusal_code,
                    "irreversible sys.addaudithook blocked open(%r, mode=%r, flags=%r)"
                    % (path_or_fd, mode, flags),
                )

    sys.addaudithook(_audit_hook)
    return intercepted


def run_in_isolated_audit_process(fn, *args, **kwargs) -> dict[str, object]:
    """Run ``fn(*args, **kwargs)`` in a forked child process with the irreversible audit hook active."""
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r_fd)
        payload: dict[str, object]
        try:
            res = fn(*args, **kwargs)
            payload = {"ok": True, "result": res}
        except RamPolicyError as exc:
            payload = {"ok": False, "error_code": exc.code, "error": str(exc)}
        except BaseException as exc:
            payload = {"ok": False, "error_code": type(exc).__name__, "error": str(exc)}
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        os.write(w_fd, raw)
        os.close(w_fd)
        os._exit(0)
    else:
        os.close(w_fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(r_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r_fd)
        _, status = os.waitpid(pid, 0)
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            raise RamPolicyError("E_WIRE", "isolated audit child process exited abnormally: %d" % status)
        return json.loads(b"".join(chunks).decode("utf-8"))


# ---------------------------------------------------------------------------
# Affine Borrowed View & Managed Ring Storage
# ---------------------------------------------------------------------------

