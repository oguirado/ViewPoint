"""Bounded proto1 frame-digest evidence for one active run/target incarnation.

Public contracts (production Python, no seat-schema or external I/O changes)::

    Session(run_id: str, target_handle: Handle, epoch: int)
    FrameIdentity(frame_seq: int, width: int, height: int,
                  rgb24_sha256: str, binding_sha256: str)
    FrameIndex(session: Session, *, capacity: int)
    FrameIndex.admit(frame: FrameRef) -> FrameIdentity
    FrameIndex.lookup(session: Session, identity: FrameIdentity) -> FrameIdentity
    FrameIndex.pin(session: Session, identity: FrameIdentity) -> FrameIdentity
    FrameIndex.unpin(session: Session, identity: FrameIdentity) -> None
    FrameIndex.retire(session: Session) -> int
    FrameIndex.advance(session: Session) -> None
    compute_binding_sha256(session: Session, frame_seq: int, width: int,
                           height: int, rgb24_sha256: str) -> str

One FrameIndex holds at most ``capacity`` immutable metadata records, scoped to
one active Session. Admission is 1-based, consecutive and never evicts records.
Retirement refuses outstanding pins (reference obligations); callers must
explicitly unpin before the old incarnation may be discarded. An advance can
only follow successful retirement and must increase generation or, within a
generation, epoch. Thus retired incarnation references cannot be resurrected.

The binding SHA-256 is SHA-256 of the UTF-8 encoding of the compact JSON array
``[run_id, kind, canonical_id, generation, epoch, frame_seq, width, height,
  rgb24_sha256]`` (ordered as written, no whitespace, ``ensure_ascii=False``).
It binds the RGB hash to full structural source identity, *not* to residency or
freshness of a live image. ``FrameRef`` must come from the upstream bank's
independently validated immutable read; this adapter re-hashes the RGB buffer
and does not retain it. Caller credentials and pixel data are never stored.

``Session.run_id`` is a validated single child suitable for the expected RAM
workspace convention ``/dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run_id>``.
This validation does NOT prove tmpfs residency, canonical root ownership,
profile freshness, timing, or live provenance; those remain lane gates.
No closed seat schema is augmented, and missing measurement values are not
inferred from missing frames or turned into zeros.

``FrameIndexError.code`` is a stable, exact typed-refusal identifier. A refusal
is atomic under the index lock and never calls bank.publish, returns a receipt,
mutates a bank reservation, silently evicts a record, or grants cleanup rights.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import threading

import numpy as np

from vadapt.window_stream_bank import Drop, FrameRef, Handle, StreamError

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)


class FrameIndexError(ValueError):
    """A deterministic refusal; ``code`` can be safely used in evidence."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str):
    raise FrameIndexError(code, message)


def _positive_exact(value, name: str):
    if type(value) is not int or value <= 0:
        _refuse("E_FRAME_INTEGER", f"{name} must be a positive exact integer")
    return value


def _epoch_exact(value):
    if type(value) is not int or value < 0:
        _refuse("E_FRAME_EPOCH", "epoch must be a nonnegative exact integer")
    return value


def _digest(value, name):
    if value is None or (type(value) is str and value == ""):
        _refuse("E_FRAME_DIGEST_MISSING", f"{name} is required")
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _refuse("E_FRAME_DIGEST_FORMAT", f"{name} must be lowercase hex64")
    return value


def _handle_snapshot(handle):
    try:
        return Handle(handle.kind, handle.canonical_id, handle.generation)
    except (AttributeError, StreamError) as exc:
        raise FrameIndexError("E_FRAME_TARGET", "invalid structural target handle") from exc


@dataclass(frozen=True, slots=True)
class Session:
    run_id: str
    target_handle: Handle
    epoch: int

    def __post_init__(self):
        if (type(self.run_id) is not str or _RUN_ID_RE.fullmatch(self.run_id) is None
                or self.run_id in {".", ".."} or ".." in self.run_id):
            _refuse("E_FRAME_RUN_ID", "run_id must be a safe single path child")
        object.__setattr__(self, "target_handle", _handle_snapshot(self.target_handle))
        _epoch_exact(self.epoch)


@dataclass(frozen=True, slots=True)
class FrameIdentity:
    frame_seq: int
    width: int
    height: int
    rgb24_sha256: str
    binding_sha256: str

    def __post_init__(self):
        for field in ("frame_seq", "width", "height"):
            _positive_exact(object.__getattribute__(self, field), field)
        _digest(self.rgb24_sha256, "rgb24_sha256")
        _digest(self.binding_sha256, "binding_sha256")


def _identity_snapshot(identity: FrameIdentity) -> FrameIdentity:
    return FrameIdentity(identity.frame_seq, identity.width, identity.height,
                         identity.rgb24_sha256, identity.binding_sha256)


def _session_snapshot(session):
    if type(session) is not Session:
        _refuse("E_FRAME_SESSION", "expected Session")
    return Session(session.run_id, session.target_handle, session.epoch)


def compute_binding_sha256(
    session: Session,
    frame_seq: int,
    width: int,
    height: int,
    rgb24_sha256: str,
) -> str:
    """Stable binding, independent of native ``FrameRef.digest`` naming."""
    session = _session_snapshot(session)
    _positive_exact(frame_seq, "frame_seq")
    _positive_exact(width, "width")
    _positive_exact(height, "height")
    _digest(rgb24_sha256, "rgb24_sha256")
    h = session.target_handle
    values = [session.run_id, h.kind, h.canonical_id, h.generation,
              session.epoch, frame_seq, width, height, rgb24_sha256]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class FrameIndex:
    """Thread-safe bounded store with explicit reference pins and epoch fences."""

    def __init__(self, session: Session, *, capacity: int):
        self._session = _session_snapshot(session)
        self.capacity = _positive_exact(capacity, "capacity")
        self._lock = threading.RLock()
        self._records: dict[int, FrameIdentity] = {}
        self._pins: dict[int, int] = {}
        self._last_seq = 0
        self._retired = False

    @property
    def session(self) -> Session:
        with self._lock:
            return _session_snapshot(self._session)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def outstanding(self) -> int:
        with self._lock:
            return sum(self._pins.values())

    def _require_current(self, session: Session):
        session = _session_snapshot(session)
        current = self._session
        if (session.run_id != current.run_id
                or session.target_handle != current.target_handle):
            _refuse("E_FRAME_FOREIGN_BINDING", "run or structural target mismatch")
        if session.epoch != current.epoch:
            _refuse("E_FRAME_STALE_EPOCH", "geometry epoch mismatch")
        if self._retired:
            _refuse("E_FRAME_RETIRED", "incarnation already retired")

    def _lookup(self, session: Session, identity: FrameIdentity) -> FrameIdentity:
        self._require_current(session)
        if type(identity) is not FrameIdentity:
            _refuse("E_FRAME_IDENTITY", "expected FrameIdentity")
        expected = compute_binding_sha256(self._session, identity.frame_seq,
                                          identity.width, identity.height,
                                          identity.rgb24_sha256)
        if identity.binding_sha256 != expected:
            _refuse("E_FRAME_FOREIGN_BINDING", "binding is not of active incarnation")
        stored = self._records.get(identity.frame_seq)
        if stored is None:
            _refuse("E_FRAME_ABSENT", "frame is not admitted")
        if stored.binding_sha256 != identity.binding_sha256:
            _refuse("E_FRAME_FOREIGN_BINDING", "binding digest mismatch")
        if stored != identity:
            _refuse("E_FRAME_IDENTITY", "frame metadata mismatch")
        return stored

    def admit(self, frame: FrameRef) -> FrameIdentity:
        """Admit exactly one successful bank read; no bank writes on any path."""
        if isinstance(frame, Drop):
            _refuse("E_FRAME_UNAVAILABLE", f"upstream read dropped: {frame.reason}")
        if type(frame) is not FrameRef:
            _refuse("E_FRAME_SOURCE", "expected bank FrameRef")
        _positive_exact(frame.frame_seq, "frame_seq")
        _positive_exact(frame.width, "width")
        _positive_exact(frame.height, "height")
        _epoch_exact(frame.epoch)
        digest = _digest(frame.digest, "FrameRef.digest")
        if (type(frame.rgb) is not np.ndarray
                or frame.rgb.dtype != np.uint8
                or frame.rgb.shape != (frame.height, frame.width, 3)):
            _refuse("E_FRAME_SOURCE", "expected exact uint8 HxWx3 bank RGB24")
        # Detached copy is transient only, then discarded. All retained values
        # are immutable scalars, not frame bytes, ndarray, or storage handles.
        rgb_hash = hashlib.sha256(frame.rgb.tobytes(order="C")).hexdigest()
        if rgb_hash != digest:
            _refuse("E_FRAME_DIGEST_MISMATCH", "source digest does not hash RGB24")
        handle = _handle_snapshot(frame.handle)
        with self._lock:
            self._require_current(self._session)
            if handle != self._session.target_handle:
                _refuse("E_FRAME_FOREIGN_BINDING", "frame target differs from session")
            if frame.epoch != self._session.epoch:
                _refuse("E_FRAME_STALE_EPOCH", "frame epoch differs from session")
            binding = compute_binding_sha256(self._session, frame.frame_seq,
                                             frame.width, frame.height, rgb_hash)
            identity = FrameIdentity(frame.frame_seq, frame.width, frame.height,
                                     rgb_hash, binding)
            prior = self._records.get(frame.frame_seq)
            if prior is not None:
                if prior == identity:
                    _refuse("E_FRAME_SEQUENCE_REUSE", "duplicate frame sequence")
                _refuse("E_FRAME_SEQUENCE_CONFLICT", "conflicting frame sequence")
            if frame.frame_seq != self._last_seq + 1:
                _refuse("E_FRAME_SEQUENCE", "sequence must start at one and be consecutive")
            if len(self._records) >= self.capacity:
                _refuse("E_FRAME_CAPACITY", "capacity exhausted without eviction")
            self._records[frame.frame_seq] = _identity_snapshot(identity)
            self._last_seq = frame.frame_seq
            return identity

    def lookup(self, session: Session, identity: FrameIdentity) -> FrameIdentity:
        """Exact identity match; digest alone never grants a reference."""
        with self._lock:
            return _identity_snapshot(self._lookup(session, identity))

    def pin(self, session: Session, identity: FrameIdentity) -> FrameIdentity:
        """Reserve a reference obligation that prevents silent retirement."""
        with self._lock:
            value = self._lookup(session, identity)
            self._pins[value.frame_seq] = self._pins.get(value.frame_seq, 0) + 1
            return _identity_snapshot(value)

    def unpin(self, session: Session, identity: FrameIdentity) -> None:
        """Explicitly discharge one reference obligation."""
        with self._lock:
            value = self._lookup(session, identity)
            count = self._pins.get(value.frame_seq, 0)
            if count == 0:
                _refuse("E_FRAME_NOT_PINNED", "no outstanding reference obligation")
            if count == 1:
                del self._pins[value.frame_seq]
            else:
                self._pins[value.frame_seq] = count - 1

    def retire(self, session: Session) -> int:
        """Discard all unpinned records, keeping the incarnation permanently fenced."""
        with self._lock:
            self._require_current(session)
            if self._pins:
                _refuse("E_FRAME_OUTSTANDING", "live reference obligations block retirement")
            count = len(self._records)
            self._records.clear()
            self._retired = True
            return count

    def advance(self, session: Session) -> None:
        """After retirement, bind a strictly newer generation or geometry epoch."""
        candidate = _session_snapshot(session)
        with self._lock:
            if not self._retired:
                _refuse("E_FRAME_NOT_RETIRED", "must retire active incarnation first")
            prior = self._session
            if (candidate.run_id != prior.run_id
                    or candidate.target_handle.kind != prior.target_handle.kind
                    or candidate.target_handle.canonical_id != prior.target_handle.canonical_id):
                _refuse("E_FRAME_FOREIGN_BINDING", "cannot advance to foreign source")
            old_gen = prior.target_handle.generation
            new_gen = candidate.target_handle.generation
            if (new_gen < old_gen
                    or (new_gen == old_gen and candidate.epoch <= prior.epoch)):
                _refuse("E_FRAME_STALE_EPOCH", "incarnation did not advance")
            self._session = candidate
            self._records.clear()
            self._pins.clear()
            self._last_seq = 0
            self._retired = False
