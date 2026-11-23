"""File-backed frame sequences for ViewPoint renders and decoded video frames."""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import sys
from tempfile import TemporaryFile
from threading import Lock
from typing import Callable, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from schemas.validation import validate_contract


_ZEROES = bytes(64 * 1024)


class FrameSequenceError(RuntimeError):
    """A local frame-sequence operation was refused or its evidence failed."""


@dataclass(frozen=True)
class FrameInfo:
    index: int
    size_bytes: int
    sha256: str
    copy: Literal["file"] = "file"


@dataclass(frozen=True)
class FrameBorrow:
    frame: FrameInfo
    read: Callable[[], bytes]


@dataclass(frozen=True)
class SequenceStatus:
    reserved_bytes: int
    retained_bytes: int  # Subset of reserved_bytes, never an additional charge.
    outstanding_borrows: int
    accepting: bool

    @property
    def pending_bytes(self) -> int:
        """Capacity promised by a sparse file without verified resident backing."""
        return self.reserved_bytes


@dataclass
class _Slot:
    info: FrameInfo
    borrows: int = 0


def _write(fd, data, offset):
    if os.pwrite(fd, data, offset) != len(data):
        raise FrameSequenceError("short frame storage write")


def _scrub(fd, offset, size):
    end = offset + size
    for position in range(offset, end, len(_ZEROES)):
        zeroes = _ZEROES[:min(len(_ZEROES), end - position)]
        _write(fd, zeroes, position)
    for position in range(offset, end, len(_ZEROES)):
        zeroes = _ZEROES[:min(len(_ZEROES), end - position)]
        if os.pread(fd, len(zeroes), position) != zeroes:
            raise FrameSequenceError("frame storage scrub readback failed")


class FileFrameSequence:
    """Bounded local fallback for already encoded frames.

    The caller supplies a broker-admitted binding, authenticated owner on each
    call, and a provisioned scratch directory. The backing file has no public
    path. A borrow pins its slot, and reading it returns a caller-owned copy.
    Call close again after outstanding borrowers exit to settle the reservation.
    The charge is logical storage capacity for admission, measured separately
    from the process's resident memory. Use PerceptionStream.open_file_sequence
    to include this charge in the accounting for a P3 surface.
    """

    def __init__(self, binding, *, directory, slots: int, max_frame_bytes: int):
        validate_contract("vp.surface_binding/v1", binding)
        if (type(slots) is not int or slots <= 0
                or type(max_frame_bytes) is not int or max_frame_bytes <= 0):
            raise ValueError("frame sequence limits must be positive integers")
        self._owner = binding["owner"]
        self._slot_count = slots
        self._slot_size = max_frame_bytes
        self._reserved_bytes = slots * max_frame_bytes
        self._storage = TemporaryFile(mode="w+b", buffering=0, dir=directory)
        self._slots: dict[int, _Slot] = {}
        self._dirty_sizes: dict[int, int] = {}
        self._next_index = 0
        self._accepting = True
        self._lock = Lock()

    def _require_owner(self, owner):
        if type(owner) is not str or owner != self._owner:
            raise FrameSequenceError("frame sequence owner mismatch")

    def _require_accepting(self):
        if not self._accepting:
            raise FrameSequenceError("frame sequence is closed to new work")

    def _borrow_count(self):
        return sum(slot.borrows for slot in self._slots.values())

    def status(self, owner: str) -> SequenceStatus:
        self._require_owner(owner)
        with self._lock:
            retained = 0 if self._accepting else self._reserved_bytes
            return SequenceStatus(self._reserved_bytes, retained, self._borrow_count(),
                                  self._accepting)

    def append(self, owner: str, frame: bytes) -> FrameInfo:
        """Copy a frame into the next slot, retiring its prior index on success."""
        self._require_owner(owner)
        if type(frame) is not bytes or not 0 < len(frame) <= self._slot_size:
            raise FrameSequenceError("frame must be nonempty bytes within the slot limit")
        with self._lock:
            self._require_accepting()
            position = self._next_index % self._slot_count
            previous = self._slots.get(position)
            if previous is not None and previous.borrows:
                raise FrameSequenceError("frame slot still has outstanding borrows")

            info = FrameInfo(self._next_index, len(frame), sha256(frame).hexdigest())
            fd = self._storage.fileno()
            offset = position * self._slot_size
            previous_size = self._dirty_sizes.get(position, 0)
            # Track attempted writes as well as published frames for cleanup.
            self._dirty_sizes[position] = max(previous_size, info.size_bytes)
            try:
                if previous_size:
                    _scrub(fd, offset, previous_size)
                _write(fd, frame, offset)
            except BaseException:
                # A partial scrub or write makes the slot unsafe to publish.
                # Close can retry scrub of every touched region.
                self._accepting = False
                raise
            self._dirty_sizes[position] = info.size_bytes
            self._slots[position] = _Slot(info)
            self._next_index += 1
            return info

    @contextmanager
    def borrow(self, owner: str, index: int):
        self._require_owner(owner)
        with self._lock:
            self._require_accepting()
            if type(index) is not int or index < 0:
                raise FrameSequenceError("frame index is not published")
            position = index % self._slot_count
            slot = self._slots.get(position)
            if slot is None or slot.info.index != index:
                raise FrameSequenceError("frame index is not published")
            slot.borrows += 1
        active = True

        def read():
            with self._lock:
                if not active:
                    raise FrameSequenceError("frame borrow has ended")
                data = os.pread(self._storage.fileno(), slot.info.size_bytes,
                                position * self._slot_size)
                if len(data) != slot.info.size_bytes or sha256(data).hexdigest() != slot.info.sha256:
                    raise FrameSequenceError("frame storage no longer matches publication")
                return data

        try:
            yield FrameBorrow(slot.info, read)
        finally:
            with self._lock:
                active = False
                slot.borrows -= 1

    def close(self, owner: str) -> bool:
        """Fence new work and settle only after borrows drain and scrub verifies."""
        self._require_owner(owner)
        with self._lock:
            self._accepting = False
            if self._borrow_count():
                return False
            if self._reserved_bytes:
                fd = self._storage.fileno()
                extent = os.fstat(fd).st_size
                for position, size in self._dirty_sizes.items():
                    offset = position * self._slot_size
                    if offset < extent:
                        _scrub(fd, offset, min(size, extent - offset))
                self._storage.close()
                self._slots.clear()
                self._dirty_sizes.clear()
                self._reserved_bytes = 0
            return True
