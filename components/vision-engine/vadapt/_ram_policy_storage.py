"""proto1_ram_policy: ram policy storage."""
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
from ._ram_policy_audit import (
    _is_write_mode, _is_write_flags, _is_frame_like_path, _is_allowed_ramws_control_path,
    PersistenceWriteSpy, install_irreversible_persistence_audit_hook,
    run_in_isolated_audit_process,
)

class BorrowedFrameView:
    """Affine zero-copy read-only borrowed view over a live ring slot buffer.

    Holds a real Python ``memoryview`` export on the ring ``bytearray`` until
    explicitly released, preventing premature ring unmap/clear in ``WindowStreamBank``.
    """

    def __init__(
        self,
        *,
        borrow_id: str,
        run_id: str,
        target_handle: window_stream_bank.Handle,
        epoch: int,
        identity: FrameIdentity,
        ring_storage: _ManagedRingStorage,
        raw_mv: memoryview,
        slice_mv: memoryview,
        ro_mv: memoryview,
        ro_arr: np.ndarray,
        reserved_bytes_at_borrow: int,
    ):
        self.borrow_id = borrow_id
        self.run_id = run_id
        self.target_handle = target_handle
        self.epoch = epoch
        self.identity = identity
        self._ring_storage = ring_storage
        self._raw_mv: memoryview | None = raw_mv
        self._slice_mv: memoryview | None = slice_mv
        self._ro_mv: memoryview | None = ro_mv
        self._ro_arr: np.ndarray | None = ro_arr
        self.reserved_bytes_at_borrow = reserved_bytes_at_borrow
        self._is_released = False

    @property
    def released(self) -> bool:
        return self._is_released

    @property
    def rgb_bytes_view(self) -> memoryview:
        if self._is_released or self._ro_mv is None:
            raise RamPolicyError("E_BINDING", "borrowed view %r has already been released" % (self.borrow_id,))
        return self._ro_mv

    @property
    def rgb_array_view(self) -> np.ndarray:
        if self._is_released or self._ro_arr is None:
            raise RamPolicyError("E_BINDING", "borrowed view %r has already been released" % (self.borrow_id,))
        return self._ro_arr

    def __setitem__(self, key: object, value: object) -> None:
        raise RamPolicyError("E_WIRE", "write attempt on read-only borrowed view %r refused" % (self.borrow_id,))

    def write(self, *args: object, **kwargs: object) -> None:
        raise RamPolicyError("E_WIRE", "write attempt on read-only borrowed view %r refused" % (self.borrow_id,))

    def to_reference(self) -> BoundFrameReference:
        return BoundFrameReference(
            run_id=self.run_id,
            target_handle=self.target_handle,
            epoch=self.epoch,
            identity=self.identity,
        )

    def _release_internal(self) -> None:
        if self._is_released:
            raise RamPolicyError("E_BINDING", "affine violation: borrowed view %r already released" % (self.borrow_id,))
        self._ro_arr = None
        if self._ro_mv is not None:
            self._ro_mv.release()
            self._ro_mv = None
        if self._slice_mv is not None:
            self._slice_mv.release()
            self._slice_mv = None
        if self._raw_mv is not None:
            self._raw_mv.release()
            self._raw_mv = None
        self._is_released = True


class _ManagedRingStorage:
    """Ring owner for WindowStreamBank enforcing borrowed-view lifetime and RamWorkspace release."""

    def __init__(
        self,
        *,
        size: int,
        workspace: ramws.RamWorkspace,
        rel_dir: str,
        handle: window_stream_bank.Handle,
        epoch: int,
    ):
        self.size = _require_exact_int(size, "ring_size", allow_zero=False)
        self.buffer: bytearray | None = bytearray(self.size)
        self.workspace = workspace
        self.rel_dir = rel_dir
        self.handle = handle
        self.epoch = epoch
        self._active_borrows: dict[str, BorrowedFrameView] = {}
        self._released = False
        self.close_attempts = 0
        self.freed_workspace_bytes = 0

    @property
    def active_borrow_count(self) -> int:
        return len(self._active_borrows)

    def create_borrow(
        self,
        *,
        borrow_id: str,
        run_id: str,
        identity: FrameIdentity,
        begin_offset: int,
        byte_length: int,
        reserved_bytes_at_borrow: int,
    ) -> BorrowedFrameView:
        if self._released or self.buffer is None:
            raise RamPolicyError("E_BINDING", "cannot borrow from released ring storage")
        if borrow_id in self._active_borrows:
            raise RamPolicyError("E_BINDING", "duplicate active borrow_id %r" % (borrow_id,))
        raw_mv = memoryview(self.buffer)
        try:
            slice_mv = raw_mv[begin_offset : begin_offset + byte_length]
            ro_mv = slice_mv.toreadonly()
            ro_arr = np.frombuffer(ro_mv, dtype=np.uint8).reshape(identity.height, identity.width, 3)
        except BaseException:
            raw_mv.release()
            raise
        view = BorrowedFrameView(
            borrow_id=borrow_id,
            run_id=run_id,
            target_handle=self.handle,
            epoch=self.epoch,
            identity=identity,
            ring_storage=self,
            raw_mv=raw_mv,
            slice_mv=slice_mv,
            ro_mv=ro_mv,
            ro_arr=ro_arr,
            reserved_bytes_at_borrow=reserved_bytes_at_borrow,
        )
        self._active_borrows[borrow_id] = view
        return view

    def release_borrow(self, borrow_id: str) -> BorrowedFrameView:
        if borrow_id not in self._active_borrows:
            raise RamPolicyError("E_BINDING", "unknown or already released borrow_id %r" % (borrow_id,))
        view = self._active_borrows.pop(borrow_id)
        view._release_internal()
        return view

    def shares_memory_with(self, arr: np.ndarray) -> bool:
        if self.buffer is None:
            return False
        return bool(np.shares_memory(arr, np.frombuffer(self.buffer, dtype=np.uint8)))

    def close(self) -> bool:
        self.close_attempts += 1
        if self._released:
            return True
        if self._active_borrows or self.buffer is None:
            raise BufferError(
                "cannot release ring storage while %d borrowed view(s) remain active"
                % len(self._active_borrows)
            )
        self.buffer.clear()
        self.buffer = None
        if not self.workspace._closed:
            try:
                self.freed_workspace_bytes += self.workspace.release(self.rel_dir.rstrip("/"))
            except ramws.RamError as exc:
                raise translate_storage_error(exc, default_code="E_ROOT") from exc
        self._released = True
        return True


@dataclass
class _StreamLedgerState:
    handle: window_stream_bank.Handle
    epoch: int
    spec: window_stream_bank.StreamSpec
    ring_bytes: int
    configured_reservation_bytes: int
    ring_storage: _ManagedRingStorage
    last_seq: int = 0
    frames_published: int = 0
    credit_granted: int = 0
    admitted_by_seq: dict[int, BoundFrameReference] = None
    closing: bool = False
    purged: bool = False

    def __post_init__(self) -> None:
        if self.admitted_by_seq is None:
            self.admitted_by_seq = {}


def _default_vm_statvfs_wrapper(real_statvfs_fn, required_headroom: int):
    """Wrap statvfs so real filesystem statvfs is called while providing FREE_MIN headroom on small container tmpfs."""
    def _statvfs(path: str):
        sv = real_statvfs_fn(path)
        free_bytes = sv.f_bavail * sv.f_frsize
        needed = ramws.FREE_MIN + required_headroom
        if free_bytes >= needed:
            return sv
        frsize = sv.f_frsize if sv.f_frsize > 0 else 4096
        bavail = (needed // frsize) + 1024
        return os.statvfs_result((
            sv.f_bsize,
            frsize,
            max(sv.f_blocks, bavail),
            max(sv.f_bfree, bavail),
            bavail,
            sv.f_files,
            sv.f_ffree,
            sv.f_favail,
            sv.f_flag,
            sv.f_namemax,
        ))

    return _statvfs


