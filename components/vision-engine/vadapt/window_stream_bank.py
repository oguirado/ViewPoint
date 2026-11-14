"""Bounded multi-target RGB24 stream bank (Python RAM seqlock model).

No actor/focus locks or disk capture are acquired here. One writer per stream
is a caller invariant; readers return owned, immutable pixel snapshots.
An injected ring_factory(handle, spec) must return fresh writable ring-format
storage: a bytearray, or an owner exposing ``buffer`` (or ``_mm``) and
``close() -> bool`` to prove a successful unmap. Unproven releases keep quota.
"""

from dataclasses import dataclass
import hashlib
import threading
import zlib

import numpy as np

from vadapt import rtring

KINDS = frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"})
_RETRIES = 8
_MAX_RENDER_INDEX = (1 << 64) - 2


class StreamError(ValueError):
    """Invalid identity, geometry, token, sequence, or storage contract."""


class QuotaError(StreamError):
    """Ring layout exceeds its per-stream or shared reservation budget."""


class StaleStreamError(StreamError):
    """The stream generation or geometry epoch is not active."""


def _positive_int(value, name, *, allow_zero=False, maximum=None):
    if (type(value) is not int or value < (0 if allow_zero else 1)
            or (maximum is not None and value > maximum)):
        raise StreamError("%s must be an exact integer in range" % name)
    return value


@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int

    def __post_init__(self):
        if self.kind not in KINDS or type(self.canonical_id) is not str or not self.canonical_id:
            raise StreamError("unsupported target kind or empty canonical_id")
        _positive_int(self.generation, "generation", allow_zero=True)


@dataclass(frozen=True)
class StreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int

    def __post_init__(self):
        for name in ("width", "height", "slots"):
            _positive_int(getattr(self, name), name, maximum=(1 << 32) - 1)
        _positive_int(self.quota_bytes, "quota_bytes")


@dataclass(frozen=True)
class StreamToken:
    handle: Handle
    epoch: int


@dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str


@dataclass(frozen=True)
class Drop:
    handle: Handle
    epoch: int
    frame_seq: int | None
    reason: str


def _handle(value):
    """Normalize structural values without requiring cross-module class identity."""
    try:
        return Handle(value.kind, value.canonical_id, value.generation)
    except AttributeError as exc:
        raise StreamError("expected handle.kind/canonical_id/generation") from exc


def _spec(value):
    try:
        return StreamSpec(value.width, value.height, value.slots, value.quota_bytes)
    except AttributeError as exc:
        raise StreamError("expected width/height/slots/quota_bytes") from exc


def _token(value):
    try:
        return StreamToken(_handle(value.handle), value.epoch)
    except AttributeError as exc:
        raise StaleStreamError("expected StreamToken(handle, epoch)") from exc


def _order_key(handle):
    handle = _handle(handle)
    return (handle.kind, handle.canonical_id, handle.generation)


def check_lock_order(handles):
    """Pure, nonblocking canonical-order guard; duplicates also fail closed."""
    keys = tuple(_order_key(h) for h in handles)
    if len(set(keys)) != len(keys) or tuple(sorted(keys)) != keys:
        raise StreamError("stream handles must be unique and in canonical order")
    return True


def _raw(owner):
    if hasattr(owner, "buffer"):
        return owner.buffer
    if hasattr(owner, "_mm"):
        return owner._mm
    return owner


def _storage_root(raw):
    """Find the exporter behind memoryviews and NumPy views for alias fencing.

    A wrapper or slice cannot evade live-storage identity checks. Disjoint
    slices of the same exporter are conservatively treated as shared storage.
    """
    root = raw
    seen = set()
    while id(root) not in seen:
        seen.add(id(root))
        if isinstance(root, memoryview):
            parent = root.obj
        elif isinstance(root, np.ndarray):
            parent = root.base
        else:
            parent = None
        if parent is None:
            break
        root = parent
    return root


def _bytes_view(owner):
    view = memoryview(_raw(owner))
    try:
        return view.cast("B")
    finally:
        view.release()


class _MemoryRing:
    """Default owned RAM ring; uses exactly the rtring on-wire byte layout."""

    def __init__(self, size):
        self.buffer = bytearray(size)

    def close(self):
        if self.buffer is None:
            return True
        try:
            self.buffer.clear()
        except BufferError:
            return False  # borrowed views still pin the actual allocation
        self.buffer = None
        return True


@dataclass
class _Entry:
    handle: Handle
    spec: StreamSpec
    epoch: int
    charge: int
    owner: object
    last_seq: int = -1
    readers: int = 0
    closing: bool = False
    releasing: bool = False
    released: bool = False
    storage_id: int = 0
    storage: object = None  # cached exporter; no owner callbacks under metadata lock


class WindowStreamBank:
    """One RAM ring per target generation/epoch, with an exact shared ledger.

    ``open_many`` and ``resize_many`` stage *all* new reservations before
    publication, call ring factories outside metadata locks, and refuse
    reversed lock order. Resize momentarily charges both old and new rings.
    A failed close/unmap keeps the old charge until ``retry_cleanup`` proves
    release. No callback or user step hook executes under the registry mutex.
    """

    def __init__(self, *, quota_bytes: int, ring_factory=None, step_hook=None):
        self.quota_bytes = _positive_int(quota_bytes, "quota_bytes")
        if ring_factory is not None and not callable(ring_factory):
            raise StreamError("ring_factory must be callable")
        if step_hook is not None and not callable(step_hook):
            raise StreamError("step_hook must be callable")
        self._factory = ring_factory or (lambda handle, spec: _MemoryRing(
            rtring.ring_bytes(spec.slots, spec.width, spec.height)))
        self._step_hook = step_hook
        self._lock = threading.RLock()
        self._active = {}
        self._seen = set()  # a closed incarnation cannot be re-opened
        self._latest_generation = {}  # (kind, canonical_id) -> latest committed generation
        self._pending = set()
        self._retained = []
        self._storage_ids = set()
        self._reserved = 0

    @property
    def reserved_bytes(self):
        with self._lock:
            return self._reserved

    def check_lock_order(self, handles):
        return check_lock_order(handles)

    def _validate_spec(self, spec):
        size = rtring.ring_bytes(spec.slots, spec.width, spec.height)
        if size > spec.quota_bytes:
            raise QuotaError("ring_bytes exceeds StreamSpec.quota_bytes")
        return size

    def _require(self, token):
        token = _token(token)
        _positive_int(token.epoch, "epoch", allow_zero=True)
        entry = self._active.get(token.handle)
        if entry is None or entry.epoch != token.epoch or entry.closing:
            raise StaleStreamError("closed generation or stale geometry epoch")
        return entry

    def _reserve(self, handles, charges, *, require_open=False, tokens=()):
        """Called under registry lock. No user callback is invoked here."""
        if any(h in self._pending for h in handles):
            raise StreamError("stream admission already in progress")
        if require_open:
            for t in tokens:
                self._require(t)
        else:
            # Wire Handle permits generation zero for legacy serialization,
            # but registration of runtime stream incarnations does not.
            for h in handles:
                _positive_int(h.generation, "generation")
            lanes = [(h.kind, h.canonical_id) for h in handles]
            active_lanes = {(h.kind, h.canonical_id) for h in self._active}
            pending_lanes = {(h.kind, h.canonical_id) for h in self._pending}
            if (len(set(lanes)) != len(lanes)
                    or any(h in self._seen for h in handles)
                    or any(lane in active_lanes or lane in pending_lanes
                           or h.generation <= self._latest_generation.get(lane, 0)
                           for h, lane in zip(handles, lanes))):
                raise StaleStreamError("generation already opened, including retired streams")
        amount = sum(charges)
        if amount > self.quota_bytes - self._reserved:
            raise QuotaError("transaction exceeds remaining shared quota")
        self._reserved += amount
        self._pending.update(handles)

    def _initialize(self, entry):
        view = _bytes_view(entry.storage)
        try:
            if view.readonly or len(view) != entry.charge:
                raise StreamError("factory must return exact-sized writable ring storage")
            rtring._G.pack_into(view, 0, rtring.MAGIC, rtring.VERSION,
                                entry.spec.slots, entry.spec.width, entry.spec.height,
                                entry.spec.width * entry.spec.height * 3,
                                rtring.data_off(entry.spec.slots), 0)
            for k in range(entry.spec.slots):
                off = rtring.HDR + k * rtring.SLOT_HDR
                view[off:off + rtring.SLOT_HDR] = bytes(rtring.SLOT_HDR)
        finally:
            view.release()

    def _staging(self, entries, *, old_entries=None):
        """All entries have already been fully reserved and marked pending."""
        staged = []
        try:
            for entry in entries:
                owner = self._factory(entry.handle, entry.spec)  # outside metadata locks
                if owner is None:
                    raise StreamError("ring_factory returned no ring")
                raw = _raw(owner)
                if raw is None:
                    raise StreamError("ring_factory returned no writable buffer")
                with self._lock:
                    identity = id(_storage_root(raw))
                    if identity in self._storage_ids:
                        # Factory returned a previously owned live mapping: never close it.
                        raise StreamError("ring_factory reused a live ring buffer")
                    self._storage_ids.add(identity)
                entry.owner, entry.storage_id, entry.storage = owner, identity, raw
                staged.append(entry)
                self._initialize(entry)
        except BaseException:
            with self._lock:
                self._pending.difference_update(e.handle for e in entries)
                # A created buffer must prove its own cleanup. Uncreated charges
                # have no mapping and can be returned without touching old ones.
                self._reserved -= sum(e.charge for e in entries[len(staged):])
                for entry in staged:
                    entry.closing = True
                    self._retained.append(entry)
            for entry in staged:
                try:
                    self._release_entry(entry)
                except OSError:
                    pass  # failed unmap remains retained for retry_cleanup
            raise
        with self._lock:
            for entry in entries:
                if old_entries is not None:
                    old = old_entries[entry.handle]
                    old.closing = True
                    self._retained.append(old)
                else:
                    self._seen.add(entry.handle)
                    lane = (entry.handle.kind, entry.handle.canonical_id)
                    self._latest_generation[lane] = entry.handle.generation
                self._active[entry.handle] = entry
            self._pending.difference_update(e.handle for e in entries)
        # Committed new entries are never rolled back because old cleanup failed.
        if old_entries is not None:
            for old in old_entries.values():
                try:
                    self._release_entry(old)
                except OSError:
                    pass  # retained charge; retry_cleanup after restoring owner
        return tuple(StreamToken(e.handle, e.epoch) for e in entries)

    def open(self, handle: Handle, spec: StreamSpec) -> StreamToken:
        return self.open_many(((handle, spec),))[0]

    def open_many(self, requests):
        pairs = [(_handle(h), _spec(s)) for h, s in requests]
        if not pairs:
            return ()
        check_lock_order(h for h, _ in pairs)
        charges = [self._validate_spec(s) for _, s in pairs]
        with self._lock:
            self._reserve([h for h, _ in pairs], charges)
        entries = [_Entry(h, s, 0, charge, None) for (h, s), charge in zip(pairs, charges)]
        return self._staging(entries)

    def resize(self, token: StreamToken, spec: StreamSpec) -> StreamToken:
        return self.resize_many(((token, spec),))[0]

    def resize_many(self, requests):
        pairs = [(_token(t), _spec(s)) for t, s in requests]
        if not pairs:
            return ()
        check_lock_order(t.handle for t, _ in pairs)
        charges = [self._validate_spec(s) for _, s in pairs]
        with self._lock:
            old = {t.handle: self._require(t) for t, _ in pairs}
            self._reserve([t.handle for t, _ in pairs], charges,
                          require_open=True, tokens=[t for t, _ in pairs])
            entries = [_Entry(t.handle, s, old[t.handle].epoch + 1, charge, None,
                              last_seq=old[t.handle].last_seq)
                       for (t, s), charge in zip(pairs, charges)]
        return self._staging(entries, old_entries=old)

    def publish(self, token: StreamToken, rgb: np.ndarray, *, frame_seq: int) -> None:
        _positive_int(frame_seq, "frame_seq", allow_zero=True, maximum=_MAX_RENDER_INDEX)
        token = _token(token)
        arr = np.asarray(rgb)
        with self._lock:
            entry = self._require(token)
            if arr.dtype != np.uint8 or arr.shape != (entry.spec.height, entry.spec.width, 3):
                raise StreamError("source must be exact uint8 HxWx3 RGB24")
            if entry.handle in self._pending:
                raise StreamError("cannot publish during pending resize")
            if frame_seq <= entry.last_seq:
                raise StreamError("frame_seq must increase monotonically per stream")
            # Single writer per stream: serialize metadata and payload publication.
            view = _bytes_view(entry.storage)
            try:
                k = frame_seq % entry.spec.slots
                off = rtring.HDR + k * rtring.SLOT_HDR
                sequence = rtring._Q.unpack_from(view, off)[0]
                if sequence & 1:
                    raise StreamError("slot already has in-flight writer")
                rtring._Q.pack_into(view, off, (sequence + 1) & rtring.MASK)
                rtring._Q.pack_into(view, off + 8, frame_seq)
                begin = rtring.data_off(entry.spec.slots) + k * entry.spec.width * entry.spec.height * 3
                payload = arr.tobytes(order="C")
                view[begin:begin + len(payload)] = payload
                rtring._I.pack_into(view, off + rtring.CRC_OFF, zlib.crc32(payload))
                rtring._Q.pack_into(view, off, (sequence + 2) & rtring.MASK)
                rtring._Q.pack_into(view, rtring.HEAD_OFF, frame_seq + 1)
                entry.last_seq = frame_seq
            finally:
                view.release()
        return None

    def read(self, token: StreamToken, *, step_hook=None) -> FrameRef | Drop:
        token = _token(token)
        hook = self._step_hook if step_hook is None else step_hook
        if hook is not None and not callable(hook):
            raise StreamError("step_hook must be callable")
        with self._lock:
            entry = self._require(token)
            entry.readers += 1  # pins mapping against close/resize
        seq_candidate = None
        reason = "torn"
        try:
            for _ in range(_RETRIES):
                view = _bytes_view(entry.storage)
                try:
                    head = rtring._Q.unpack_from(view, rtring.HEAD_OFF)[0]
                    if head == 0:
                        return Drop(token.handle, token.epoch, None, "absent")
                    seq_candidate = head - 1
                    k = seq_candidate % entry.spec.slots
                    off = rtring.HDR + k * rtring.SLOT_HDR
                    seq1 = rtring._Q.unpack_from(view, off)[0]
                    if seq1 & 1:
                        reason = "torn"
                        continue
                    render1 = rtring._Q.unpack_from(view, off + 8)[0]
                    crc1 = rtring._I.unpack_from(view, off + rtring.CRC_OFF)[0]
                    if render1 != seq_candidate:
                        seq2, render2 = rtring._QQ.unpack_from(view, off)
                        if seq2 != seq1 or render2 != render1:
                            reason = "torn"
                        else:
                            reason = "lapped" if render1 > seq_candidate else "absent"
                        continue
                finally:
                    view.release()
                if hook is not None:
                    hook("before_copy")  # never under the metadata lock
                view = _bytes_view(entry.storage)
                try:
                    begin = rtring.data_off(entry.spec.slots) + k * entry.spec.width * entry.spec.height * 3
                    raw = bytes(view[begin:begin + entry.spec.width * entry.spec.height * 3])
                finally:
                    view.release()
                if hook is not None:
                    hook("after_copy")  # external adversarial mutation point
                view = _bytes_view(entry.storage)
                try:
                    seq2, render2 = rtring._QQ.unpack_from(view, off)
                    crc2 = rtring._I.unpack_from(view, off + rtring.CRC_OFF)[0]
                    head2 = rtring._Q.unpack_from(view, rtring.HEAD_OFF)[0]
                finally:
                    view.release()
                if (seq2 != seq1 or seq2 & 1 or render2 != seq_candidate
                        or crc2 != crc1 or head2 < seq_candidate + 1
                        or zlib.crc32(raw) != crc1):
                    reason = "lapped" if render2 > seq_candidate else "torn"
                    continue
                with self._lock:
                    if self._active.get(token.handle) is not entry or entry.closing:
                        return Drop(token.handle, token.epoch, seq_candidate, "stale_epoch")
                # Immutable bytes backing: cannot flip writeable back on via NumPy.
                owned = np.frombuffer(raw, dtype=np.uint8).reshape(
                    entry.spec.height, entry.spec.width, 3)
                return FrameRef(token.handle, token.epoch, seq_candidate,
                                entry.spec.width, entry.spec.height, owned,
                                hashlib.sha256(raw).hexdigest())
            return Drop(token.handle, token.epoch, seq_candidate, reason)
        finally:
            with self._lock:
                entry.readers -= 1
                cleanup = entry.closing and entry.readers == 0
            if cleanup:
                self._release_entry(entry)

    def _release_entry(self, entry):
        with self._lock:
            if not entry.closing or entry.released or entry.releasing or entry.readers:
                return False
            entry.releasing = True
            # No active reader/writer remains. Drop cached storage before the
            # owner attempts an unmap; retaining our view could itself block it.
            entry.storage = None
        try:
            owner = entry.owner
            if type(owner) is bytearray:
                try:
                    owner.clear()  # reclaim bytes only when borrowed views are gone
                    clean = True
                except BufferError:
                    clean = False
            else:
                close = getattr(owner, "close", None)
                if close is None:
                    clean = False  # unknown live buffer lifetime
                else:
                    try:
                        clean = close() is True or getattr(owner, "_released", False) is True
                    except BufferError:
                        clean = False
        except BaseException:
            with self._lock:
                entry.releasing = False
            raise
        with self._lock:
            entry.releasing = False
            if clean:
                entry.released = True
                self._reserved -= entry.charge
                self._storage_ids.discard(entry.storage_id)
                self._retained.remove(entry)
        return clean

    def close(self, token: StreamToken) -> None:
        token = _token(token)
        with self._lock:
            entry = self._require(token)
            if entry.handle in self._pending:
                raise StreamError("cannot close during pending resize")
            del self._active[entry.handle]
            entry.closing = True
            self._retained.append(entry)
        self._release_entry(entry)
        return None

    def retry_cleanup(self):
        """Retry conservatively retained, now-unmapped rings. Returns count freed."""
        with self._lock:
            candidates = tuple(self._retained)
        freed = 0
        for entry in candidates:
            freed += bool(self._release_entry(entry))
        return freed
