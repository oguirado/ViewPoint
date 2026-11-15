"""Deterministic seqlock and ring-buffer schedule explorer (G-R4-2).

Drives real ``vadapt.rtring``, ``vadapt.window_stream_bank``, ``vadapt.ramws``,
and ``vadapt.multi_x11_ramcap`` APIs using:
- existing one-argument ``before_copy`` / ``after_copy`` ``step_hook`` callbacks
  for ``WindowStreamBank.read``,
- local in-memory writer wrappers and ``bytearray`` metadata interceptors
  (without adding keywords to production classes), and
- an explicit file-free ``InMemoryRingSeam`` + ``FakeRamWorkspace`` +
  ``FakeRingBacking`` seam so ``rtring.RingWriter``, ``rtring.RingReader``, and
  ``WindowStreamBank`` operate strictly in RAM without any mapped pixel file.

Enumerates writer preemption between ``begin``, ``payload``, ``slot_seal``,
``head_update``, and ``copied_read_revalidation``. Rejects odd, unequal, or
overwritten sequences and corrupt CRC while preserving the last valid immutable
frame in explorer state. Tracks a bounded schedule trace, reproducible seed,
and per-target reservation deltas across open, resize, close, retained views,
and ``retry_cleanup``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import os
import posixpath
import random
import struct
import sys
import threading
import time
import types
from typing import Callable, Sequence
from unittest.mock import patch
import zlib

import numpy as np

from vadapt import multi_x11_ramcap, ramws, rtring
from vadapt.multi_x11_ramcap import (
    CaptureFailure,
    CropSpec,
    MemoryStreamBank,
    TrustedScreen,
    capture_many,
)
from vadapt.window_stream_bank import (
    Drop,
    FrameRef,
    Handle,
    KINDS,
    QuotaError,
    StaleStreamError,
    StreamError,
    StreamSpec,
    StreamToken,
    WindowStreamBank,
    check_lock_order,
)

AUSENTE = "AUSENTE"
TIMING_ENV = "VM"
DEFAULT_SEED = 0x5E010C42
DEFAULT_TRACE_CAPACITY = 64

PREEMPTION_STAGES: tuple[str, ...] = (
    "begin",
    "payload",
    "slot_seal",
    "head_update",
    "copied_read_revalidation",
)

FAULT_MODES: tuple[str, ...] = (
    "clean",
    "odd_sequence",
    "unequal_sequence",
    "overwritten_sequence",
    "corrupt_crc",
    "payload_tear",
    "unadvanced_head",
    "head_regressed",
    "stale_epoch_resize",
)


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an exact integer >= {minimum}")
    return value


def canonical_handle_key(handle: Handle) -> tuple[str, str, int]:
    """Return canonical ordering tuple (kind, canonical_id, generation)."""
    if not isinstance(handle, Handle):
        handle = Handle(
            getattr(handle, "kind", None),
            getattr(handle, "canonical_id", None),
            getattr(handle, "generation", None),
        )
    return (handle.kind, handle.canonical_id, handle.generation)


def make_deterministic_rgb(
    width: int, height: int, *, frame_seq: int, seed: int = DEFAULT_SEED
) -> np.ndarray:
    """Build a deterministic uint8 HxWx3 RGB24 frame in RAM."""
    _exact_int(width, "width", minimum=1)
    _exact_int(height, "height", minimum=1)
    _exact_int(frame_seq, "frame_seq", minimum=0)
    _exact_int(seed, "seed", minimum=0)
    total = height * width * 3
    base = np.arange(total, dtype=np.uint32).reshape(height, width, 3)
    mix = ((base * 31) + ((frame_seq + 1) * 17) + (seed & 0xFF)) & 0xFF
    arr = mix.astype(np.uint8, copy=True)
    arr[0, 0, 0] = (frame_seq * 29 + (seed & 0x7F)) & 0xFF
    arr[0, 0, 1] = ((frame_seq >> 3) ^ 0x5A) & 0xFF
    arr[0, 0, 2] = (frame_seq + 101) & 0xFF
    return arr


def verify_immutable_frame_ref(
    ref: FrameRef,
    *,
    expected_handle: Handle | None = None,
    expected_epoch: int | None = None,
    expected_seq: int | None = None,
    expected_rgb: np.ndarray | None = None,
) -> bool:
    """Verify that a FrameRef is coherent, digest-valid, and backed by immutable RGB bytes."""
    if not isinstance(ref, FrameRef):
        return False
    if expected_handle is not None and canonical_handle_key(ref.handle) != canonical_handle_key(expected_handle):
        return False
    if expected_epoch is not None and ref.epoch != expected_epoch:
        return False
    if expected_seq is not None and ref.frame_seq != expected_seq:
        return False
    rgb = ref.rgb
    if not isinstance(rgb, np.ndarray):
        return False
    if (
        rgb.dtype != np.uint8
        or rgb.shape != (ref.height, ref.width, 3)
        or not rgb.flags.c_contiguous
        or rgb.flags.writeable
    ):
        return False
    raw = rgb.tobytes()
    if hashlib.sha256(raw).hexdigest() != ref.digest:
        return False
    if expected_rgb is not None and not np.array_equal(rgb, expected_rgb):
        return False
    return True


def snapshot_valid_rtring_frame(
    frame: rtring.Frame,
    *,
    handle: Handle,
    epoch: int = 0,
    expected_crc: int | None = None,
) -> FrameRef | None:
    """Explicitly validate a zero-copy rtring.Frame and copy it into an immutable FrameRef.

    Raw rtring views alias ring memory and are only trustworthy if ``frame.valid()``
    holds both before and after copying, and CRC matches the slot header.
    """
    if frame is None or not hasattr(frame, "valid") or not hasattr(frame, "view"):
        return None
    if not frame.valid():
        return None
    view = frame.view
    if not isinstance(view, np.ndarray) or view.dtype != np.uint8 or view.ndim != 3 or view.shape[2] != 3:
        return None
    raw = bytes(view.tobytes(order="C"))
    if not frame.valid():
        return None
    reader = frame._reader
    off = rtring.HDR + frame._k * rtring.SLOT_HDR
    seq_after, ri_after = rtring._QQ.unpack_from(reader._mm, off)
    crc_slot = rtring._I.unpack_from(reader._mm, off + rtring.CRC_OFF)[0]
    if seq_after != frame._seq or (seq_after & 1) or ri_after != frame.render_index:
        return None
    if expected_crc is not None and crc_slot != expected_crc:
        return None
    if getattr(reader, "verify", True) and zlib.crc32(raw) != crc_slot:
        return None
    height, width, _ = view.shape
    owned = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
    return FrameRef(
        handle=handle,
        epoch=epoch,
        frame_seq=frame.render_index,
        width=width,
        height=height,
        rgb=owned,
        digest=hashlib.sha256(raw).hexdigest(),
    )


@dataclass(frozen=True)
class ReservationDelta:
    handle: Handle
    epoch: int
    operation: str
    before_bytes: int
    after_bytes: int
    delta_bytes: int
    target_reserved_bytes: int
    retained_bytes: int


@dataclass(frozen=True)
class ScheduleStep:
    step_index: int
    seed: int
    surface: str
    handle: Handle
    epoch: int
    stage: str
    fault: str
    hook_phases: tuple[str, ...]
    hook_outside_lock: bool
    outcome: str
    drop_reason: str | None
    accepted_seq: int | None
    preserved_seq: int | None
    preserved_digest: str | None
    torn_observed: int
    reserved_bytes: int


@dataclass(frozen=True)
class ExplorationReport:
    seed: int
    steps_executed: int
    accepted_count: int
    rejected_count: int
    torn_reads_delivered: int
    trace: tuple[ScheduleStep, ...]
    trace_capacity: int
    trace_total_recorded: int
    trace_evicted: int
    per_target_deltas: dict[tuple[str, str, int], tuple[ReservationDelta, ...]]
    per_target_net_bytes: dict[tuple[str, str, int], int]
    last_valid_frames: dict[tuple[str, str, int], FrameRef | None]
    final_reserved_bytes: int
    timing_env: str
    vm_elapsed_ns: int
    host_timing_ns: str
    smallest_counterexample: dict[str, object] | None


class BoundedScheduleTrace:
    """Fixed-capacity ring trace of ScheduleStep records."""

    def __init__(self, capacity: int = DEFAULT_TRACE_CAPACITY) -> None:
        self._capacity = _exact_int(capacity, "capacity", minimum=1)
        self._items: deque[ScheduleStep] = deque(maxlen=self._capacity)
        self._total_recorded = 0
        self._evicted_count = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_recorded(self) -> int:
        return self._total_recorded

    @property
    def evicted_count(self) -> int:
        return self._evicted_count

    def append(self, step: ScheduleStep) -> None:
        if not isinstance(step, ScheduleStep):
            raise TypeError("step must be a ScheduleStep")
        if len(self._items) == self._capacity:
            self._evicted_count += 1
        self._items.append(step)
        self._total_recorded += 1

    def steps(self) -> tuple[ScheduleStep, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)


class FakeMmap(bytearray):
    """In-RAM bytearray backing with CPython buffer-export enforcement and struct hooks.

    Any live ``np.frombuffer(mm, ...)`` or ``memoryview(mm)`` export causes
    ``close()`` to raise ``BufferError`` natively via CPython's buffer protocol,
    matching OS ``mmap.close()`` with live views without touching disk.
    """

    def __init__(self, size: int = 0) -> None:
        super().__init__(size)
        self.writer_closed = False
        self.reader_closed = False
        self.pinned_views = 0
        self.read_interceptor: Callable[[struct.Struct, FakeMmap, int], tuple | None] | None = None
        self.write_interceptor: Callable[[struct.Struct, FakeMmap, int, tuple], bool | None] | None = None

    def resize_buffer(self, size: int) -> None:
        _exact_int(size, "size", minimum=0)
        if size > len(self):
            self.extend(b"\x00" * (size - len(self)))
        elif size < len(self):
            del self[size:]

    def check_no_exports(self) -> None:
        if self.pinned_views > 0:
            raise BufferError("cannot close FakeMmap with pinned views")
        # Mutating length by +1/-1 triggers CPython's ob_exports > 0 BufferError
        # when any numpy array or memoryview is still exporting this buffer.
        self.append(0)
        self.pop()

    def close(self) -> bool:
        self.check_no_exports()
        self.writer_closed = True
        return True


class _StructSeamProxy:
    """Wraps rtring struct objects so closed reader mappings and stage interceptors fire."""

    def __init__(self, real_struct: struct.Struct) -> None:
        self._real = real_struct

    def unpack_from(self, buffer: object, offset: int = 0) -> tuple:
        if getattr(buffer, "reader_closed", False):
            raise ValueError("mmap closed or invalid")
        interceptor = getattr(buffer, "read_interceptor", None)
        if interceptor is not None:
            override = interceptor(self._real, buffer, offset)
            if override is not None:
                return override
        return self._real.unpack_from(buffer, offset)

    def pack_into(self, buffer: object, offset: int, *values: object) -> None:
        interceptor = getattr(buffer, "write_interceptor", None)
        if interceptor is not None:
            suppress = interceptor(self._real, buffer, offset, values)
            if suppress is True:
                return
        self._real.pack_into(buffer, offset, *values)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class VirtualRingPath:
    """File-free path handle bound to an InMemoryRingSeam."""

    def __init__(self, seam: InMemoryRingSeam, path_str: str) -> None:
        self._seam = seam
        self._path = seam.norm(path_str)

    def __fspath__(self) -> str:
        return self._path

    def __str__(self) -> str:
        return self._path

    def __repr__(self) -> str:
        return f"VirtualRingPath({self._path!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (str, os.PathLike)):
            return self._path == self._seam.norm(os.fspath(other))
        return False

    def __hash__(self) -> int:
        return hash(self._path)

    @property
    def parent(self) -> VirtualRingPath:
        return VirtualRingPath(self._seam, posixpath.dirname(self._path))

    def mkdir(self, parents: bool = False, exist_ok: bool = False, mode: int = 0o700) -> None:
        self._seam.dirs.add(self._path)

    def exists(self) -> bool:
        return self._path in self._seam.files

    def write_bytes(self, data: bytes) -> int:
        raw = bytes(data)
        mm = FakeMmap(len(raw))
        mm[:] = raw
        self._seam.files[self._path] = mm
        return len(raw)


class InMemoryRingSeam:
    """Explicit in-memory mmap and OS file-table seam for file-free rtring testing."""

    def __init__(self, target_module: types.ModuleType | None = None) -> None:
        self.files: dict[str, FakeMmap] = {}
        self.dirs: set[str] = {"/ramws", "/ramws/virtual"}
        self.fds: dict[int, str] = {}
        self._next_fd = 20_000
        self._target_module = target_module
        self._active_module: types.ModuleType | None = None
        self._patches: list[object] = []
        self._orig_structs: dict[str, struct.Struct] = {}

    @staticmethod
    def norm(path: str | os.PathLike) -> str:
        return posixpath.normpath(os.fspath(path))

    def _os_open(self, path: str | os.PathLike, flags: int, mode: int = 0o600) -> int:
        p = self.norm(path)
        if flags & os.O_CREAT:
            if (flags & os.O_EXCL) and p in self.files:
                raise FileExistsError(f"in-memory ring path already exists: {p}")
            if p not in self.files:
                self.files[p] = FakeMmap(0)
        elif p not in self.files:
            raise FileNotFoundError(f"in-memory ring path not found: {p}")
        fd = self._next_fd
        self._next_fd += 1
        self.fds[fd] = p
        return fd

    def _os_ftruncate(self, fd: int, size: int) -> None:
        p = self.fds[fd]
        self.files[p].resize_buffer(size)

    def _os_fstat(self, fd: int) -> types.SimpleNamespace:
        p = self.fds[fd]
        return types.SimpleNamespace(st_size=len(self.files[p]))

    def _os_close(self, fd: int) -> None:
        self.fds.pop(fd, None)

    def _map(self, fd: int, size: int, write: bool) -> tuple[FakeMmap, str]:
        p = self.fds[fd]
        mm = self.files[p]
        if write:
            mm.writer_closed = False
        else:
            mm.reader_closed = False
        return mm, "in_memory_seam"

    def _unmap(self, mm: object) -> None:
        if isinstance(mm, FakeMmap):
            try:
                mm.check_no_exports()
            except BufferError:
                return
            mm.reader_closed = True
            return
        close_fn = getattr(mm, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except BufferError:
                pass

    def __enter__(self) -> InMemoryRingSeam:
        mod = self._target_module or sys.modules.get("vadapt.rtring", rtring)
        self._active_module = mod
        self._orig_structs = {
            name: getattr(mod, name) for name in ("_G", "_Q", "_QQ", "_I")
        }
        for name, st in self._orig_structs.items():
            setattr(mod, name, _StructSeamProxy(st))
        self._patches = [
            patch.object(mod.os, "open", self._os_open),
            patch.object(mod.os, "ftruncate", self._os_ftruncate),
            patch.object(mod.os, "fstat", self._os_fstat),
            patch.object(mod.os, "close", self._os_close),
            patch.object(mod.os.path, "realpath", self.norm),
            patch.object(mod, "_map", self._map),
            patch.object(mod, "_unmap", self._unmap),
        ]
        for pt in self._patches:
            pt.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        for pt in reversed(self._patches):
            pt.stop()
        self._patches = []
        if self._active_module is not None:
            for name, st in self._orig_structs.items():
                setattr(self._active_module, name, st)
        self._orig_structs = {}
        self._active_module = None
        return False


class FakeRamWorkspace:
    """In-memory workspace enforcing ramws path escape and quota conservation laws."""

    def __init__(
        self,
        seam: InMemoryRingSeam,
        *,
        root: str = "/ramws/virtual",
        quota_bytes: int = 1 << 20,
    ) -> None:
        self.seam = seam
        self.root = seam.norm(root)
        self.quota = _exact_int(quota_bytes, "quota_bytes", minimum=1)
        self.used = 0
        self.peak = 0
        self.released_bytes = 0
        self._closed = False
        self.history: list[tuple[str, int, int]] = []

    def _check_live(self) -> None:
        if self._closed:
            raise ramws.RamError("E_RAM_LOCKED", "workspace closed")

    def path(self, rel: str | os.PathLike) -> VirtualRingPath:
        self._check_live()
        s = os.fspath(rel)
        if not isinstance(s, str) or not s or "\0" in s or posixpath.isabs(s):
            raise ramws.RamError("E_RAM_ESCAPE", f"bad rel {s!r}")
        parts = s.split("/")
        if ".." in parts or "." in parts:
            raise ramws.RamError("E_RAM_ESCAPE", f"bad rel {s!r}")
        cand = posixpath.normpath(posixpath.join(self.root, s))
        if cand != self.root and not cand.startswith(self.root + "/"):
            raise ramws.RamError("E_RAM_ESCAPE", f"escapes root: {s!r}")
        return VirtualRingPath(self.seam, cand)

    def reserve(self, n: int) -> None:
        self._check_live()
        if type(n) is not int or n < 0:
            raise ramws.RamError("E_RAM_QUOTA", f"bad reserve {n!r}")
        if self.used + n > self.quota:
            raise ramws.RamError(
                "E_RAM_QUOTA", f"run quota {self.quota}: used {self.used} + {n}"
            )
        before = self.used
        self.used += n
        self.peak = max(self.peak, self.used)
        self.history.append(("reserve", before, self.used))

    def account(self) -> int:
        self._check_live()
        self.used = sum(
            len(mm)
            for p, mm in self.seam.files.items()
            if p.startswith(self.root + "/")
        )
        self.peak = max(self.peak, self.used)
        if self.used > self.quota:
            raise ramws.RamError("E_RAM_QUOTA", f"used {self.used} > quota {self.quota}")
        return self.used

    def release(self, p: str | os.PathLike) -> int:
        self._check_live()
        s = os.fspath(p)
        if "\0" in s:
            raise ramws.RamError("E_RAM_ESCAPE", f"bad release path {s!r}")
        cand = s if posixpath.isabs(s) else posixpath.join(self.root, s)
        norm_cand = posixpath.normpath(cand)
        if not norm_cand.startswith(self.root + "/"):
            raise ramws.RamError("E_RAM_ESCAPE", f"release outside root: {s!r}")
        mm = self.seam.files.pop(norm_cand, None)
        if mm is None:
            return 0
        freed = len(mm)
        before = self.used
        self.used = max(0, self.used - freed)
        self.released_bytes += freed
        self.history.append(("release", before, self.used))
        return freed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for k in [p for p in self.seam.files if p.startswith(self.root + "/")]:
            self.seam.files.pop(k, None)
        self.used = 0


class FakeRingBacking:
    """File-free ring backing owner for WindowStreamBank.

    Exposes ``buffer`` (a ``FakeMmap`` bytearray subclass) and ``close() -> bool``.
    When a consumer retains a numpy view (via ``retain_slot_view()``) or pins
    the backing (via ``pin_view()``), ``close()`` raises ``BufferError`` or
    returns ``False``, causing ``WindowStreamBank`` to retain the quota charge
    until the view is dropped and ``retry_cleanup()`` succeeds.
    """

    def __init__(
        self,
        handle: Handle,
        spec: StreamSpec,
        *,
        factory_outside_lock: bool = True,
    ) -> None:
        self.handle = handle
        self.spec = spec
        self.size = rtring.ring_bytes(spec.slots, spec.width, spec.height)
        self.buffer = FakeMmap(self.size)
        self._released = False
        self.close_calls = 0
        self.factory_outside_lock = factory_outside_lock

    def retain_slot_view(self, slot: int = 0) -> np.ndarray:
        """Create an aliasing numpy view over ``slot`` that pins ``self.buffer``."""
        k = slot % self.spec.slots
        slot_bytes = self.spec.width * self.spec.height * 3
        off = rtring.data_off(self.spec.slots) + k * slot_bytes
        return np.frombuffer(
            self.buffer, dtype=np.uint8, count=slot_bytes, offset=off
        ).reshape(self.spec.height, self.spec.width, 3)

    def pin_view(self) -> None:
        self.buffer.pinned_views += 1

    def unpin_view(self) -> None:
        if self.buffer.pinned_views > 0:
            self.buffer.pinned_views -= 1

    def close(self) -> bool:
        self.close_calls += 1
        if self._released:
            return True
        self.buffer.close()
        self._released = True
        return True


class LocalRingWriterWrapper:
    """Local wrapper around rtring.RingWriter exposing stage-by-stage preemption pauses.

    Does not modify or add keyword arguments to ``rtring.RingWriter``.
    """

    def __init__(self, writer: rtring.RingWriter) -> None:
        self.writer = writer

    def stage_begin(self, render_index: int | None = None) -> int:
        """Execute only the begin stage (odd sequence + render_index)."""
        r = self.writer.index + 1 if render_index is None else render_index
        return self.writer._begin(r)

    def stage_payload(self, k: int, frame: np.ndarray, *, partial_bytes: int | None = None) -> None:
        """Write full or partial payload bytes while sequence remains odd."""
        arr = np.asarray(frame, dtype=np.uint8)
        if partial_bytes is None:
            self.writer._copy(k, arr)
        else:
            raw = arr.tobytes(order="C")
            slot_off = self.writer.data_off + k * self.writer.slot_bytes
            n = max(0, min(len(raw), int(partial_bytes)))
            self.writer._mm[slot_off : slot_off + n] = raw[:n]

    def stage_slot_seal(
        self,
        *,
        corrupt_crc: bool = False,
        override_even_seq: int | None = None,
        override_render_index: int | None = None,
    ) -> tuple[int, int, int]:
        """Write slot CRC and even sequence WITHOUT advancing global HEAD_OFF."""
        if self.writer._open_slot is None:
            raise ValueError("no open slot to seal")
        k, s, r = self.writer._open_slot
        off = rtring.HDR + k * rtring.SLOT_HDR
        crc = zlib.crc32(self.writer._slot_view(k))
        if corrupt_crc:
            crc ^= 0xFFFFFFFF
        rtring._I.pack_into(self.writer._mm, off + rtring.CRC_OFF, crc & 0xFFFFFFFF)
        if override_render_index is not None:
            rtring._Q.pack_into(self.writer._mm, off + 8, override_render_index & rtring.MASK)
        even_seq = ((s + 2) & rtring.MASK) if override_even_seq is None else (override_even_seq & rtring.MASK)
        rtring._Q.pack_into(self.writer._mm, off, even_seq)
        return (k, s, r)

    def stage_head_update(self, r: int | None = None) -> int:
        """Advance global HEAD_OFF and close the open slot."""
        if self.writer._open_slot is not None:
            _, _, open_r = self.writer._open_slot
            target_r = open_r if r is None else r
            self.writer.index = open_r
            self.writer._open_slot = None
        else:
            if r is None:
                raise ValueError("render_index required when no open slot")
            target_r = r
            self.writer.index = max(self.writer.index, target_r)
        rtring._Q.pack_into(self.writer._mm, rtring.HEAD_OFF, target_r + 1)
        return target_r

    def publish_with_pause(
        self,
        frame: np.ndarray,
        render_index: int | None = None,
        *,
        pause_after: str,
        on_pause: Callable[[str, int], None],
        corrupt_crc: bool = False,
    ) -> int:
        """Run staged publication and invoke ``on_pause(pause_after, k)`` mid-frame."""
        if pause_after not in ("begin", "payload", "slot_seal", "head_update"):
            raise ValueError(f"unsupported pause_after stage: {pause_after!r}")
        k = self.stage_begin(render_index)
        if pause_after == "begin":
            on_pause("begin", k)
        self.stage_payload(k, frame)
        if pause_after == "payload":
            on_pause("payload", k)
        _, _, r = self.stage_slot_seal(corrupt_crc=corrupt_crc)
        if pause_after == "slot_seal":
            on_pause("slot_seal", k)
        self.stage_head_update(r)
        if pause_after == "head_update":
            on_pause("head_update", k)
        return r


class LocalBankWriterWrapper:
    """Local wrapper exposing mid-frame stage mutations on a WindowStreamBank ring buffer.

    The production ``WindowStreamBank.publish`` writes odd seq, render_index,
    payload, CRC, even seq, and head inline without hooks. This wrapper exposes
    exact stage snapshots on the underlying ``FakeMmap`` buffer and pairs with
    the one-argument ``step_hook(phase)`` in ``WindowStreamBank.read``.
    """

    def __init__(
        self,
        bank: WindowStreamBank,
        token: StreamToken,
        backing: FakeRingBacking,
    ) -> None:
        self.bank = bank
        self.token = token
        self.backing = backing

    @property
    def buffer(self) -> FakeMmap:
        return self.backing.buffer

    def slot_offset(self, frame_seq: int) -> int:
        k = frame_seq % self.backing.spec.slots
        return rtring.HDR + k * rtring.SLOT_HDR

    def payload_offset(self, frame_seq: int) -> int:
        k = frame_seq % self.backing.spec.slots
        slot_bytes = self.backing.spec.width * self.backing.spec.height * 3
        return rtring.data_off(self.backing.spec.slots) + k * slot_bytes

    def inject_begin_odd_seq(self, frame_seq: int) -> int:
        off = self.slot_offset(frame_seq)
        prev_seq = rtring._Q.unpack_from(self.buffer, off)[0]
        odd_seq = (prev_seq | 1) if (prev_seq & 1) else ((prev_seq + 1) & rtring.MASK)
        rtring._Q.pack_into(self.buffer, off, odd_seq)
        rtring._Q.pack_into(self.buffer, off + 8, frame_seq)
        return odd_seq

    def inject_payload_overwrite(self, frame_seq: int, rgb: np.ndarray) -> bytes:
        raw = np.asarray(rgb, dtype=np.uint8).tobytes(order="C")
        p_off = self.payload_offset(frame_seq)
        self.buffer[p_off : p_off + len(raw)] = raw
        return raw

    def inject_slot_seal(
        self,
        frame_seq: int,
        rgb: np.ndarray,
        *,
        seq_value: int,
        render_index: int | None = None,
        corrupt_crc: bool = False,
    ) -> None:
        off = self.slot_offset(frame_seq)
        raw = self.inject_payload_overwrite(frame_seq, rgb)
        crc = zlib.crc32(raw)
        if corrupt_crc:
            crc ^= 0xFFFFFFFF
        rtring._Q.pack_into(
            self.buffer, off + 8, frame_seq if render_index is None else render_index
        )
        rtring._I.pack_into(self.buffer, off + rtring.CRC_OFF, crc & 0xFFFFFFFF)
        rtring._Q.pack_into(self.buffer, off, seq_value & rtring.MASK)

    def inject_head_update(self, head_value: int) -> None:
        rtring._Q.pack_into(self.buffer, rtring.HEAD_OFF, head_value & rtring.MASK)


def _probe_lock_free_from_other_thread(lock: threading.RLock, timeout_s: float = 1.5) -> bool:
    """Verify from a helper thread that ``lock`` is NOT currently held by the caller thread."""
    acquired = [False]
    err: list[BaseException] = []

    def _try_acquire() -> None:
        try:
            got = lock.acquire(blocking=True, timeout=timeout_s)
            if got:
                acquired[0] = True
                lock.release()
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    worker = threading.Thread(target=_try_acquire, name="lock-probe", daemon=False)
    worker.start()
    worker.join(timeout=timeout_s)
    if worker.is_alive() or err:
        return False
    return acquired[0]


class SeqlockScheduleExplorer:
    """Deterministic schedule explorer for WindowStreamBank, rtring, and multi_x11_ramcap."""

    def __init__(
        self,
        *,
        quota_bytes: int,
        seed: int = DEFAULT_SEED,
        trace_capacity: int = DEFAULT_TRACE_CAPACITY,
    ) -> None:
        self.quota_bytes = _exact_int(quota_bytes, "quota_bytes", minimum=1)
        self.seed = _exact_int(seed, "seed", minimum=0)
        self.trace = BoundedScheduleTrace(capacity=trace_capacity)
        self.last_valid_frames: dict[tuple[str, str, int], FrameRef | None] = {}
        self.per_target_deltas: dict[tuple[str, str, int], list[ReservationDelta]] = {}
        self.per_target_reserved: dict[tuple[str, str, int], int] = {}
        self.per_target_retained: dict[tuple[str, str, int], int] = {}
        self.active_tokens: dict[tuple[str, str, int], StreamToken] = {}
        self.backings: dict[tuple[tuple[str, str, int], int], FakeRingBacking] = {}
        self.epoch_counters: dict[tuple[str, str, int], int] = {}
        self.factory_lock_checks: list[bool] = []
        self.hook_lock_checks: list[bool] = []
        self.torn_reads_delivered = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self._step_counter = 0

        self.bank = WindowStreamBank(
            quota_bytes=self.quota_bytes,
            ring_factory=self._ring_factory,
        )

    def _ring_factory(self, handle: Handle, spec: StreamSpec) -> FakeRingBacking:
        key = canonical_handle_key(handle)
        outside_lock = _probe_lock_free_from_other_thread(self.bank._lock)
        self.factory_lock_checks.append(outside_lock)
        epoch = self.epoch_counters.get(key, 0)
        backing = FakeRingBacking(handle, spec, factory_outside_lock=outside_lock)
        self.backings[(key, epoch)] = backing
        return backing

    def _record_delta(
        self,
        handle: Handle,
        epoch: int,
        operation: str,
        before_bytes: int,
        after_bytes: int,
        target_reserved_bytes: int,
        retained_bytes: int,
    ) -> ReservationDelta:
        key = canonical_handle_key(handle)
        delta = ReservationDelta(
            handle=handle,
            epoch=epoch,
            operation=operation,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            delta_bytes=after_bytes - before_bytes,
            target_reserved_bytes=target_reserved_bytes,
            retained_bytes=retained_bytes,
        )
        self.per_target_deltas.setdefault(key, []).append(delta)
        self.per_target_reserved[key] = target_reserved_bytes
        self.per_target_retained[key] = retained_bytes
        return delta

    def open_stream(self, handle: Handle, spec: StreamSpec) -> StreamToken:
        return self.open_many(((handle, spec),))[0]

    def open_many(
        self, requests: Sequence[tuple[Handle, StreamSpec]]
    ) -> tuple[StreamToken, ...]:
        req_list = list(requests)
        before = self.bank.reserved_bytes
        for h, _ in req_list:
            key = canonical_handle_key(h)
            self.epoch_counters.setdefault(key, 0)
        try:
            tokens = self.bank.open_many(req_list)
        except BaseException:
            after = self.bank.reserved_bytes
            for h, _ in req_list:
                key = canonical_handle_key(h)
                self._record_delta(
                    h,
                    self.epoch_counters.get(key, 0),
                    "failed_open",
                    before,
                    after,
                    self.per_target_reserved.get(key, 0),
                    self.per_target_retained.get(key, 0),
                )
            raise
        running = before
        for tok, (_, s) in zip(tokens, req_list):
            key = canonical_handle_key(tok.handle)
            charge = rtring.ring_bytes(s.slots, s.width, s.height)
            next_bytes = running + charge
            self.last_valid_frames.setdefault(key, None)
            self.active_tokens[key] = tok
            self._record_delta(
                tok.handle,
                tok.epoch,
                "open",
                running,
                next_bytes,
                charge,
                self.per_target_retained.get(key, 0),
            )
            running = next_bytes
        return tokens

    def resize_stream(
        self,
        token: StreamToken,
        new_spec: StreamSpec,
        *,
        pin_old_view: bool = False,
    ) -> StreamToken:
        key = canonical_handle_key(token.handle)
        old_backing = self.backings.get((key, token.epoch))
        if pin_old_view and old_backing is not None:
            old_backing.pin_view()
        before = self.bank.reserved_bytes
        self.epoch_counters[key] = token.epoch + 1
        try:
            new_token = self.bank.resize(token, new_spec)
        except BaseException:
            self.epoch_counters[key] = token.epoch
            after = self.bank.reserved_bytes
            self._record_delta(
                token.handle,
                token.epoch,
                "failed_resize",
                before,
                after,
                self.per_target_reserved.get(key, 0),
                self.per_target_retained.get(key, 0),
            )
            raise
        after = self.bank.reserved_bytes
        new_charge = rtring.ring_bytes(new_spec.slots, new_spec.width, new_spec.height)
        old_retained = (
            old_backing.size
            if (old_backing is not None and not old_backing._released)
            else 0
        )
        retained_total = self.per_target_retained.get(key, 0) + old_retained
        target_total = new_charge + retained_total
        self.active_tokens[key] = new_token
        self._record_delta(
            new_token.handle,
            new_token.epoch,
            "resize_retained" if old_retained > 0 else "resize",
            before,
            after,
            target_total,
            retained_total,
        )
        return new_token

    def close_stream(self, token: StreamToken, *, pin_view: bool = False) -> None:
        key = canonical_handle_key(token.handle)
        backing = self.backings.get((key, token.epoch))
        if pin_view and backing is not None:
            backing.pin_view()
        before = self.bank.reserved_bytes
        self.bank.close(token)
        after = self.bank.reserved_bytes
        self.active_tokens.pop(key, None)
        if backing is not None and not backing._released:
            retained = self.per_target_retained.get(key, 0) + backing.size
            self._record_delta(
                token.handle,
                token.epoch,
                "close_retained",
                before,
                after,
                retained,
                retained,
            )
        else:
            retained = self.per_target_retained.get(key, 0)
            self._record_delta(
                token.handle,
                token.epoch,
                "close",
                before,
                after,
                retained,
                retained,
            )

    def retry_cleanup(self) -> int:
        before = self.bank.reserved_bytes
        unreleased_before = {
            (key, epoch): b
            for (key, epoch), b in self.backings.items()
            if not b._released
        }
        freed_count = self.bank.retry_cleanup()
        after = self.bank.reserved_bytes
        running = before
        for (key, epoch), b in unreleased_before.items():
            if b._released:
                next_running = running - b.size
                prev_ret = self.per_target_retained.get(key, 0)
                new_ret = max(0, prev_ret - b.size)
                prev_res = self.per_target_reserved.get(key, 0)
                new_res = max(0, prev_res - b.size)
                self._record_delta(
                    b.handle,
                    epoch,
                    "retry_cleanup",
                    running,
                    next_running,
                    new_res,
                    new_ret,
                )
                running = next_running
        assert running == after
        return freed_count

    def publish_valid_frame(
        self, token: StreamToken, rgb: np.ndarray, *, frame_seq: int
    ) -> FrameRef:
        """Publish a valid frame and read it back into the explorer's last_valid_frames store."""
        self.bank.publish(token, rgb, frame_seq=frame_seq)
        got = self.bank.read(token)
        if not isinstance(got, FrameRef) or not verify_immutable_frame_ref(
            got,
            expected_handle=token.handle,
            expected_epoch=token.epoch,
            expected_seq=frame_seq,
            expected_rgb=rgb,
        ):
            self.torn_reads_delivered += 1
            raise AssertionError(f"failed to read back valid published frame {frame_seq}: {got!r}")
        key = canonical_handle_key(token.handle)
        self.last_valid_frames[key] = got
        return got

    def explore_bank_preemption(
        self,
        token: StreamToken,
        candidate_rgb: np.ndarray,
        *,
        frame_seq: int,
        stage: str,
        fault: str,
        resize_spec: StreamSpec | None = None,
    ) -> ScheduleStep:
        """Execute one deterministic bank preemption schedule and record a ScheduleStep."""
        if stage not in PREEMPTION_STAGES:
            raise ValueError(f"unsupported stage {stage!r}; expected one of {PREEMPTION_STAGES}")
        if fault not in FAULT_MODES:
            raise ValueError(f"unsupported fault {fault!r}; expected one of {FAULT_MODES}")

        key = canonical_handle_key(token.handle)
        prior_valid = self.last_valid_frames.get(key)
        prior_bytes = prior_valid.rgb.tobytes() if prior_valid is not None else None
        prior_digest = prior_valid.digest if prior_valid is not None else None

        backing = self.backings[(key, token.epoch)]
        wrapper = LocalBankWriterWrapper(self.bank, token, backing)
        saved_ring = bytes(backing.buffer)

        hook_phases: list[str] = []
        hook_outside: list[bool] = []
        resized_tokens: list[StreamToken] = []
        pinned_before_read_exit: list[int] = []

        if fault == "clean":
            self.bank.publish(token, candidate_rgb, frame_seq=frame_seq)
        elif stage == "head_update" and fault == "unadvanced_head":
            if prior_valid is None:
                wrapper.inject_slot_seal(frame_seq, candidate_rgb, seq_value=2)
                wrapper.inject_head_update(0)
            else:
                k = frame_seq % backing.spec.slots
                off = rtring.HDR + k * rtring.SLOT_HDR
                rtring._QQ.pack_into(backing.buffer, off, 2, 0)
                wrapper.inject_head_update(frame_seq + 1)

        def _step_hook(phase: str) -> None:
            hook_phases.append(phase)
            if len(hook_phases) <= 2:
                outside = _probe_lock_free_from_other_thread(self.bank._lock)
                hook_outside.append(outside)
                self.hook_lock_checks.append(outside)

            active_seq = rtring._Q.unpack_from(backing.buffer, rtring.HEAD_OFF)[0] - 1
            if active_seq < 0:
                return

            if stage == "begin" and fault == "odd_sequence" and phase == "before_copy":
                wrapper.inject_begin_odd_seq(active_seq)
            elif stage == "payload" and fault == "payload_tear" and phase == "before_copy":
                wrapper.inject_payload_overwrite(active_seq, candidate_rgb)
            elif stage == "slot_seal" and fault == "unequal_sequence" and phase == "after_copy":
                cur_seq = rtring._Q.unpack_from(backing.buffer, wrapper.slot_offset(active_seq))[0]
                wrapper.inject_slot_seal(
                    active_seq,
                    candidate_rgb,
                    seq_value=(cur_seq + 2) & rtring.MASK,
                    render_index=active_seq,
                )
            elif stage == "slot_seal" and fault == "overwritten_sequence" and phase == "after_copy":
                cur_seq = rtring._Q.unpack_from(backing.buffer, wrapper.slot_offset(active_seq))[0]
                lapped_seq = active_seq + backing.spec.slots
                wrapper.inject_slot_seal(
                    active_seq,
                    candidate_rgb,
                    seq_value=(cur_seq + 2) & rtring.MASK,
                    render_index=lapped_seq,
                )
            elif stage == "head_update" and fault == "head_regressed":
                after_count = sum(1 for p in hook_phases if p == "after_copy")
                if phase == "after_copy":
                    if after_count < 8:
                        cur_seq = rtring._Q.unpack_from(backing.buffer, wrapper.slot_offset(active_seq))[0]
                        rtring._Q.pack_into(backing.buffer, wrapper.slot_offset(active_seq), (cur_seq + 2) & rtring.MASK)
                    else:
                        wrapper.inject_head_update(0)
            elif stage == "copied_read_revalidation" and fault == "corrupt_crc" and phase in ("before_copy", "after_copy"):
                if phase == "before_copy":
                    wrapper.inject_payload_overwrite(active_seq, candidate_rgb)
                else:
                    off = wrapper.slot_offset(active_seq)
                    orig_crc = rtring._I.unpack_from(backing.buffer, off + rtring.CRC_OFF)[0]
                    rtring._I.pack_into(backing.buffer, off + rtring.CRC_OFF, orig_crc ^ 0xFFFFFFFF)
            elif stage == "copied_read_revalidation" and fault == "stale_epoch_resize" and phase == "after_copy":
                if not resized_tokens:
                    target_spec = resize_spec or backing.spec
                    new_tok = self.resize_stream(token, target_spec)
                    resized_tokens.append(new_tok)
                    pinned_before_read_exit.append(self.bank.reserved_bytes)

        result = self.bank.read(token, step_hook=_step_hook)

        # When resize occurred during after_copy, read()'s finally block unpinned and released the old backing.
        if fault == "stale_epoch_resize" and resized_tokens and backing._released:
            before_unpin = pinned_before_read_exit[0]
            after_unpin = self.bank.reserved_bytes
            new_ret = max(0, self.per_target_retained.get(key, 0) - backing.size)
            new_res = max(0, self.per_target_reserved.get(key, 0) - backing.size)
            self._record_delta(
                token.handle,
                token.epoch,
                "reader_pin_release",
                before_unpin,
                after_unpin,
                new_res,
                new_ret,
            )

        if fault != "clean" and fault != "stale_epoch_resize":
            backing.buffer[:] = saved_ring

        all_outside = all(hook_outside) if hook_outside else True
        torn_flag = 0

        if isinstance(result, FrameRef):
            coherent = verify_immutable_frame_ref(
                result,
                expected_handle=token.handle,
                expected_epoch=token.epoch,
                expected_seq=frame_seq if fault == "clean" else None,
                expected_rgb=candidate_rgb if fault == "clean" else None,
            )
            if not coherent or fault != "clean":
                torn_flag = 1
                self.torn_reads_delivered += 1
            else:
                self.last_valid_frames[key] = result
                self.accepted_count += 1
            outcome = "accepted"
            drop_reason = None
            accepted_seq = result.frame_seq
        elif isinstance(result, Drop):
            outcome = "dropped"
            drop_reason = result.reason
            accepted_seq = None
            self.rejected_count += 1
            current_preserved = self.last_valid_frames.get(key)
            if current_preserved is not prior_valid:
                torn_flag = 1
                self.torn_reads_delivered += 1
            if prior_valid is not None:
                if (
                    current_preserved is None
                    or current_preserved.rgb.tobytes() != prior_bytes
                    or current_preserved.digest != prior_digest
                    or current_preserved.rgb.flags.writeable
                ):
                    torn_flag = 1
                    self.torn_reads_delivered += 1
        else:
            raise AssertionError(f"unexpected bank.read return value: {result!r}")

        preserved = self.last_valid_frames.get(key)
        step = ScheduleStep(
            step_index=self._step_counter,
            seed=self.seed,
            surface="window_stream_bank",
            handle=token.handle,
            epoch=token.epoch,
            stage=stage,
            fault=fault,
            hook_phases=tuple(hook_phases),
            hook_outside_lock=all_outside,
            outcome=outcome,
            drop_reason=drop_reason,
            accepted_seq=accepted_seq,
            preserved_seq=None if preserved is None else preserved.frame_seq,
            preserved_digest=None if preserved is None else preserved.digest,
            torn_observed=torn_flag,
            reserved_bytes=self.bank.reserved_bytes,
        )
        self._step_counter += 1
        self.trace.append(step)
        return step

    def explore_rtring_schedules(
        self,
        *,
        slots: int = 2,
        width: int = 4,
        height: int = 2,
        handle: Handle | None = None,
    ) -> list[ScheduleStep]:
        """Explore file-free rtring writer preemption, Frame.valid() revalidation, and retained-view close."""
        target_handle = handle or Handle("x11_window", "win/rtring-0", 0)
        key = canonical_handle_key(target_handle)
        self.last_valid_frames.setdefault(key, None)
        recorded: list[ScheduleStep] = []

        with InMemoryRingSeam() as seam:
            total_ring_bytes = rtring.ring_bytes(slots, width, height)
            ws = FakeRamWorkspace(seam, quota_bytes=2 * total_ring_bytes)
            wr = rtring.RingWriter(ws, slots, width, height, rel="ring/frames.rgb24")
            rd = rtring.RingReader(wr.path, root=ws.root, verify=True)
            wrapper = LocalRingWriterWrapper(wr)

            self._record_delta(
                target_handle, 0, "rtring_open", 0, ws.used, ws.used, 0
            )

            # 1. Baseline coherent publication and immutable snapshot
            f0_rgb = make_deterministic_rgb(width, height, frame_seq=0, seed=self.seed)
            wr.publish(f0_rgb, 0)
            raw_f0 = rd.poll()
            snap0 = snapshot_valid_rtring_frame(raw_f0, handle=target_handle, epoch=0)
            del raw_f0
            assert snap0 is not None and verify_immutable_frame_ref(snap0, expected_rgb=f0_rgb)
            self.last_valid_frames[key] = snap0
            self.accepted_count += 1
            s0 = ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="rtring",
                handle=target_handle,
                epoch=0,
                stage="head_update",
                fault="clean",
                hook_phases=("rtring_poll", "frame_valid"),
                hook_outside_lock=True,
                outcome="accepted",
                drop_reason=None,
                accepted_seq=0,
                preserved_seq=snap0.frame_seq,
                preserved_digest=snap0.digest,
                torn_observed=0,
                reserved_bytes=ws.used,
            )
            self._step_counter += 1
            self.trace.append(s0)
            recorded.append(s0)

            # 2. Preemption after begin (odd sequence on slot 1)
            f1_rgb = make_deterministic_rgb(width, height, frame_seq=1, seed=self.seed)
            k1 = wrapper.stage_begin(1)
            rtring._Q.pack_into(wr._mm, rtring.HEAD_OFF, 2)
            acq_odd = rd.acquire(1, retries=2)
            assert acq_odd is None and 1 in rd.torn
            wrapper.stage_payload(k1, f1_rgb)
            wrapper.stage_slot_seal()
            wrapper.stage_head_update(1)
            off1 = rtring.HDR + k1 * rtring.SLOT_HDR
            good_crc1 = rtring._I.unpack_from(wr._mm, off1 + rtring.CRC_OFF)[0]
            self.rejected_count += 1
            s1 = ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="rtring",
                handle=target_handle,
                epoch=0,
                stage="begin",
                fault="odd_sequence",
                hook_phases=("stage_begin", "rtring_acquire"),
                hook_outside_lock=True,
                outcome="dropped",
                drop_reason="torn",
                accepted_seq=None,
                preserved_seq=self.last_valid_frames[key].frame_seq,
                preserved_digest=self.last_valid_frames[key].digest,
                torn_observed=0,
                reserved_bytes=ws.used,
            )
            self._step_counter += 1
            self.trace.append(s1)
            recorded.append(s1)

            # 3. Corrupt CRC rejection on rtring.RingReader.acquire
            rtring._I.pack_into(wr._mm, off1 + rtring.CRC_OFF, good_crc1 ^ 0xFFFFFFFF)
            acq_crc = rd.acquire(1, retries=2)
            assert acq_crc is None
            rtring._I.pack_into(wr._mm, off1 + rtring.CRC_OFF, good_crc1)
            self.rejected_count += 1
            s2 = ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="rtring",
                handle=target_handle,
                epoch=0,
                stage="slot_seal",
                fault="corrupt_crc",
                hook_phases=("stage_slot_seal", "rtring_acquire"),
                hook_outside_lock=True,
                outcome="dropped",
                drop_reason="torn",
                accepted_seq=None,
                preserved_seq=self.last_valid_frames[key].frame_seq,
                preserved_digest=self.last_valid_frames[key].digest,
                torn_observed=0,
                reserved_bytes=ws.used,
            )
            self._step_counter += 1
            self.trace.append(s2)
            recorded.append(s2)

            # 4. Zero-copy aliasing Frame invalidated mid-use when writer preempts same slot
            valid_f1 = rd.acquire(1, retries=2)
            assert valid_f1 is not None and valid_f1.valid()
            next_same_slot_r = 1 + slots
            k_wrap = wrapper.stage_begin(next_same_slot_r)
            assert not valid_f1.valid()
            snap_invalid = snapshot_valid_rtring_frame(valid_f1, handle=target_handle, epoch=0)
            assert snap_invalid is None
            assert self.last_valid_frames[key] is snap0
            assert verify_immutable_frame_ref(snap0, expected_rgb=f0_rgb)

            f_wrap_rgb = make_deterministic_rgb(width, height, frame_seq=next_same_slot_r, seed=self.seed)
            wrapper.stage_payload(k_wrap, f_wrap_rgb)
            wrapper.stage_slot_seal()
            wrapper.stage_head_update(next_same_slot_r)

            self.rejected_count += 1
            s3 = ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="rtring",
                handle=target_handle,
                epoch=0,
                stage="copied_read_revalidation",
                fault="overwritten_sequence",
                hook_phases=("rtring_acquire", "writer_wrap_begin", "frame_valid_false"),
                hook_outside_lock=True,
                outcome="dropped",
                drop_reason="lapped",
                accepted_seq=None,
                preserved_seq=self.last_valid_frames[key].frame_seq,
                preserved_digest=self.last_valid_frames[key].digest,
                torn_observed=0,
                reserved_bytes=ws.used,
            )
            self._step_counter += 1
            self.trace.append(s3)
            recorded.append(s3)

            # 5. Close with live numpy view (valid_f1 still alive) keeps reservation until view drops
            before_close = ws.used
            wr.close()
            assert not wr._released and ws.used == before_close
            self._record_delta(
                target_handle, 0, "rtring_close_retained", before_close, ws.used, ws.used, ws.used
            )
            del valid_f1
            wr.close()
            assert wr._released and ws.used == 0
            self._record_delta(
                target_handle, 0, "rtring_close_released", before_close, ws.used, 0, 0
            )
            rd.close()

        return recorded

    def explore_ramcap_schedules(
        self,
        *,
        width: int = 6,
        height: int = 4,
        slots: int = 3,
        handle: Handle | None = None,
    ) -> list[ScheduleStep]:
        """Explore multi_x11_ramcap seqlock schedules in RAM without X server or disk files."""
        target_handle = handle or Handle("xvfb_screen", "screen/ramcap-0", 1)
        key = canonical_handle_key(target_handle)
        self.last_valid_frames.setdefault(key, None)
        recorded: list[ScheduleStep] = []

        def _make_ring_buf(
            pixels: np.ndarray,
            *,
            render_index: int,
            slot_seq: int = 2,
            head_override: int | None = None,
            corrupt_crc: bool = False,
        ) -> bytearray:
            h, w, _ = pixels.shape
            buf = bytearray(rtring.ring_bytes(slots, w, h))
            head_val = (render_index + 1) if head_override is None else head_override
            struct.pack_into(
                "<8sIIIIQQQ",
                buf,
                0,
                rtring.MAGIC,
                rtring.VERSION,
                slots,
                w,
                h,
                w * h * 3,
                rtring.data_off(slots),
                head_val,
            )
            slot = render_index % slots
            hdr_off = rtring.HDR + slot * rtring.SLOT_HDR
            struct.pack_into("<QQ", buf, hdr_off, slot_seq, render_index)
            data_off = rtring.data_off(slots) + slot * w * h * 3
            payload = pixels.tobytes(order="C")
            buf[data_off : data_off + len(payload)] = payload
            crc = zlib.crc32(payload)
            if corrupt_crc:
                crc ^= 0xFFFFFFFF
            struct.pack_into("<I", buf, hdr_off + rtring.CRC_OFF, crc & 0xFFFFFFFF)
            return buf

        class _StaticSource:
            def __init__(self, screen: TrustedScreen) -> None:
                self.screen = screen

            def resolve(self, h: Handle) -> TrustedScreen:
                return self.screen

        crop_bank = MemoryStreamBank(quota_bytes=1 << 16, slots=2)
        base_pixels = make_deterministic_rgb(width, height, frame_seq=2, seed=self.seed)
        expected_crop = base_pixels[1:3, 1:4].copy()

        # 1. Clean capture
        clean_ring = _make_ring_buf(base_pixels, render_index=2, slot_seq=2)
        screen_clean = TrustedScreen(target_handle, 1, width, height, clean_ring)
        spec_clean = CropSpec(target_handle, 1, 1, 1, 3, 2, 2)
        before_c = crop_bank.reserved_bytes
        (res_clean,) = capture_many([spec_clean], source=_StaticSource(screen_clean), bank=crop_bank)
        after_c = crop_bank.reserved_bytes
        assert isinstance(res_clean, FrameRef) and verify_immutable_frame_ref(
            res_clean, expected_handle=target_handle, expected_epoch=1, expected_seq=2, expected_rgb=expected_crop
        )
        self.last_valid_frames[key] = res_clean
        self.accepted_count += 1
        self._record_delta(
            target_handle, 1, "ramcap_admit", before_c, after_c, after_c, 0
        )
        recorded.append(
            ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="multi_x11_ramcap",
                handle=target_handle,
                epoch=1,
                stage="head_update",
                fault="clean",
                hook_phases=("capture_many",),
                hook_outside_lock=True,
                outcome="accepted",
                drop_reason=None,
                accepted_seq=2,
                preserved_seq=res_clean.frame_seq,
                preserved_digest=res_clean.digest,
                torn_observed=0,
                reserved_bytes=crop_bank.reserved_bytes,
            )
        )
        self._step_counter += 1
        self.trace.append(recorded[-1])

        # 2. Odd sequence -> Drop("torn"), last_valid_frames[key] preserved
        odd_ring = _make_ring_buf(base_pixels, render_index=3, slot_seq=3)
        screen_odd = TrustedScreen(target_handle, 1, width, height, odd_ring)
        (res_odd,) = capture_many(
            [CropSpec(target_handle, 1, 1, 1, 3, 2, 3)],
            source=_StaticSource(screen_odd),
            bank=crop_bank,
        )
        assert isinstance(res_odd, Drop) and res_odd.reason == "torn"
        assert self.last_valid_frames[key] is res_clean
        self.rejected_count += 1
        recorded.append(
            ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="multi_x11_ramcap",
                handle=target_handle,
                epoch=1,
                stage="begin",
                fault="odd_sequence",
                hook_phases=("capture_many",),
                hook_outside_lock=True,
                outcome="dropped",
                drop_reason=res_odd.reason,
                accepted_seq=None,
                preserved_seq=res_clean.frame_seq,
                preserved_digest=res_clean.digest,
                torn_observed=0,
                reserved_bytes=crop_bank.reserved_bytes,
            )
        )
        self._step_counter += 1
        self.trace.append(recorded[-1])

        # 3. Corrupt CRC -> Drop("torn"), last_valid_frames[key] preserved
        crc_ring = _make_ring_buf(base_pixels, render_index=3, slot_seq=2, corrupt_crc=True)
        screen_crc = TrustedScreen(target_handle, 1, width, height, crc_ring)
        (res_crc,) = capture_many(
            [CropSpec(target_handle, 1, 1, 1, 3, 2, 3)],
            source=_StaticSource(screen_crc),
            bank=crop_bank,
        )
        assert isinstance(res_crc, Drop) and res_crc.reason == "torn"
        assert self.last_valid_frames[key] is res_clean
        self.rejected_count += 1
        recorded.append(
            ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="multi_x11_ramcap",
                handle=target_handle,
                epoch=1,
                stage="copied_read_revalidation",
                fault="corrupt_crc",
                hook_phases=("capture_many",),
                hook_outside_lock=True,
                outcome="dropped",
                drop_reason=res_crc.reason,
                accepted_seq=None,
                preserved_seq=res_clean.frame_seq,
                preserved_digest=res_clean.digest,
                torn_observed=0,
                reserved_bytes=crop_bank.reserved_bytes,
            )
        )
        self._step_counter += 1
        self.trace.append(recorded[-1])

        # 4. Overwritten sequence (lapped) -> Drop("lapped"), last_valid_frames[key] preserved
        lapped_ring = _make_ring_buf(base_pixels, render_index=6, slot_seq=4)
        screen_lapped = TrustedScreen(target_handle, 1, width, height, lapped_ring)
        (res_lapped,) = capture_many(
            [CropSpec(target_handle, 1, 1, 1, 3, 2, 3)],
            source=_StaticSource(screen_lapped),
            bank=crop_bank,
        )
        assert isinstance(res_lapped, Drop) and res_lapped.reason == "lapped"
        assert self.last_valid_frames[key] is res_clean
        self.rejected_count += 1
        recorded.append(
            ScheduleStep(
                step_index=self._step_counter,
                seed=self.seed,
                surface="multi_x11_ramcap",
                handle=target_handle,
                epoch=1,
                stage="slot_seal",
                fault="overwritten_sequence",
                hook_phases=("capture_many",),
                hook_outside_lock=True,
                outcome="dropped",
                drop_reason=res_lapped.reason,
                accepted_seq=None,
                preserved_seq=res_clean.frame_seq,
                preserved_digest=res_clean.digest,
                torn_observed=0,
                reserved_bytes=crop_bank.reserved_bytes,
            )
        )
        self._step_counter += 1
        self.trace.append(recorded[-1])

        before_close = crop_bank.reserved_bytes
        crop_bank.close()
        self._record_delta(
            target_handle, 1, "ramcap_close", before_close, crop_bank.reserved_bytes, 0, 0
        )
        return recorded

    def build_report(
        self,
        *,
        vm_elapsed_ns: int = 0,
        smallest_counterexample: dict[str, object] | None = None,
    ) -> ExplorationReport:
        deltas_frozen = {
            k: tuple(v) for k, v in sorted(self.per_target_deltas.items())
        }
        net_bytes = {
            k: int(self.per_target_reserved.get(k, 0))
            for k in sorted(self.per_target_deltas)
        }
        return ExplorationReport(
            seed=self.seed,
            steps_executed=self._step_counter,
            accepted_count=self.accepted_count,
            rejected_count=self.rejected_count,
            torn_reads_delivered=self.torn_reads_delivered,
            trace=self.trace.steps(),
            trace_capacity=self.trace.capacity,
            trace_total_recorded=self.trace.total_recorded,
            trace_evicted=self.trace.evicted_count,
            per_target_deltas=deltas_frozen,
            per_target_net_bytes=net_bytes,
            last_valid_frames=dict(self.last_valid_frames),
            final_reserved_bytes=self.bank.reserved_bytes,
            timing_env=TIMING_ENV,
            vm_elapsed_ns=_exact_int(vm_elapsed_ns, "vm_elapsed_ns", minimum=0),
            host_timing_ns=AUSENTE,
            smallest_counterexample=smallest_counterexample,
        )


def shrink_schedule_counterexample(
    operations: Sequence[tuple[str, str]],
    predicate: Callable[[Sequence[tuple[str, str]]], bool],
    *,
    replay_seed: int,
    case_index: int,
) -> dict[str, object]:
    """Shrink a failing schedule sequence to its smallest failing prefix/subsequence."""
    current = list(operations)
    # 1. Prefix shrinking
    while len(current) > 1 and predicate(current[:-1]):
        current = current[:-1]
    # 2. Single-step deletion shrinking
    idx = 0
    while idx < len(current) and len(current) > 1:
        candidate = current[:idx] + current[idx + 1 :]
        if predicate(candidate):
            current = candidate
        else:
            idx += 1
    return {
        "replay_seed": int(replay_seed),
        "case_index": int(case_index),
        "original_length": len(operations),
        "minimal_length": len(current),
        "minimal_schedule": tuple(current),
    }


def run_deterministic_exploration(
    *,
    seed: int = DEFAULT_SEED,
    trace_capacity: int = DEFAULT_TRACE_CAPACITY,
    random_steps: int = 16,
) -> ExplorationReport:
    """Run a deterministic seqlock and ring-buffer schedule exploration in RAM.

    Enumerates all preemption stages (``begin``, ``payload``, ``slot_seal``,
    ``head_update``, ``copied_read_revalidation``), verifies canonical handle
    admission, stale-token refusal, close/resize/retained-view accounting,
    and zero torn reads across ``WindowStreamBank``, ``rtring``, and
    ``multi_x11_ramcap``.
    """
    t0 = time.monotonic_ns()
    _exact_int(seed, "seed", minimum=0)
    _exact_int(trace_capacity, "trace_capacity", minimum=1)
    _exact_int(random_steps, "random_steps", minimum=0)

    unit = rtring.ring_bytes(2, 4, 2)
    explorer = SeqlockScheduleExplorer(
        quota_bytes=4 * unit,
        seed=seed,
        trace_capacity=trace_capacity,
    )

    h_a = Handle("cdp_tab", "tab/alpha", 1)
    h_b = Handle("godot_window", "godot/beta", 1)
    spec = StreamSpec(4, 2, 2, unit)

    tok_a, tok_b = explorer.open_many(((h_a, spec), (h_b, spec)))

    # Head-not-updated (absent) before any frame is published on tok_a
    explorer.explore_bank_preemption(
        tok_a,
        make_deterministic_rgb(4, 2, frame_seq=0, seed=seed),
        frame_seq=0,
        stage="head_update",
        fault="unadvanced_head",
    )

    # Publish initial valid frame 0 on both streams
    explorer.explore_bank_preemption(
        tok_a,
        make_deterministic_rgb(4, 2, frame_seq=0, seed=seed),
        frame_seq=0,
        stage="head_update",
        fault="clean",
    )
    explorer.explore_bank_preemption(
        tok_b,
        make_deterministic_rgb(4, 2, frame_seq=0, seed=seed ^ 0x11),
        frame_seq=0,
        stage="head_update",
        fault="clean",
    )

    # Enumerate every preemption stage on tok_a while preserving frame 0
    stage_fault_pairs = (
        ("begin", "odd_sequence"),
        ("payload", "payload_tear"),
        ("slot_seal", "unequal_sequence"),
        ("slot_seal", "overwritten_sequence"),
        ("head_update", "unadvanced_head"),
        ("head_update", "head_regressed"),
        ("copied_read_revalidation", "corrupt_crc"),
    )
    for idx, (stage, fault) in enumerate(stage_fault_pairs, start=1):
        rgb = make_deterministic_rgb(4, 2, frame_seq=idx, seed=seed)
        explorer.explore_bank_preemption(
            tok_a,
            rgb,
            frame_seq=idx,
            stage=stage,
            fault=fault,
        )

    # Seeded deterministic schedule steps across tok_a and tok_b
    rng = random.Random(seed)
    seq_a = 1
    seq_b = 1
    for _ in range(random_steps):
        use_a = rng.random() < 0.5
        cur_tok = tok_a if use_a else tok_b
        cur_seq = seq_a if use_a else seq_b
        stage, fault = rng.choice(
            (
                ("head_update", "clean"),
                ("begin", "odd_sequence"),
                ("payload", "payload_tear"),
                ("slot_seal", "unequal_sequence"),
                ("slot_seal", "overwritten_sequence"),
                ("head_update", "unadvanced_head"),
                ("head_update", "head_regressed"),
                ("copied_read_revalidation", "corrupt_crc"),
            )
        )
        rgb = make_deterministic_rgb(4, 2, frame_seq=cur_seq, seed=seed)
        step = explorer.explore_bank_preemption(
            cur_tok,
            rgb,
            frame_seq=cur_seq,
            stage=stage,
            fault=fault,
        )
        if step.outcome == "accepted":
            if use_a:
                seq_a += 1
            else:
                seq_b += 1

    # Copied-read revalidation with concurrent resize -> Drop("stale_epoch")
    step_stale = explorer.explore_bank_preemption(
        tok_a,
        make_deterministic_rgb(4, 2, frame_seq=seq_a, seed=seed),
        frame_seq=seq_a,
        stage="copied_read_revalidation",
        fault="stale_epoch_resize",
        resize_spec=spec,
    )
    assert step_stale.drop_reason == "stale_epoch"
    tok_a = explorer.active_tokens[canonical_handle_key(h_a)]

    # Close tok_b with a pinned view -> retained charge, then unpin + retry_cleanup -> 0
    backing_b = explorer.backings[(canonical_handle_key(h_b), tok_b.epoch)]
    explorer.close_stream(tok_b, pin_view=True)
    assert explorer.bank.reserved_bytes == 2 * unit
    backing_b.unpin_view()
    freed = explorer.retry_cleanup()
    assert freed == 1 and explorer.bank.reserved_bytes == unit

    # Close tok_a cleanly
    explorer.close_stream(tok_a, pin_view=False)
    assert explorer.bank.reserved_bytes == 0

    # Also explore file-free rtring and multi_x11_ramcap schedules
    explorer.explore_rtring_schedules()
    explorer.explore_ramcap_schedules()

    elapsed_ns = max(0, time.monotonic_ns() - t0)
    return explorer.build_report(vm_elapsed_ns=elapsed_ns)


__all__ = [
    "AUSENTE",
    "TIMING_ENV",
    "DEFAULT_SEED",
    "DEFAULT_TRACE_CAPACITY",
    "PREEMPTION_STAGES",
    "FAULT_MODES",
    "ReservationDelta",
    "ScheduleStep",
    "ExplorationReport",
    "BoundedScheduleTrace",
    "FakeMmap",
    "VirtualRingPath",
    "InMemoryRingSeam",
    "FakeRamWorkspace",
    "FakeRingBacking",
    "LocalRingWriterWrapper",
    "LocalBankWriterWrapper",
    "SeqlockScheduleExplorer",
    "canonical_handle_key",
    "make_deterministic_rgb",
    "verify_immutable_frame_ref",
    "snapshot_valid_rtring_frame",
    "shrink_schedule_counterexample",
    "run_deterministic_exploration",
]
