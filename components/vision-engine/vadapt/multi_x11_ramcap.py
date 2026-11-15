"""RAM-only multiplexed crops from *trusted, injected* Xvfb RGB24 rings.

No X server, file, process, or native-window capture is opened here.  The
caller supplies a source with ``resolve(handle) -> TrustedScreen``.  A
TrustedScreen pins the full-screen geometry/epoch and exposes a byte buffer
containing an existing ``vadapt.rtring`` v2 ring.  Only xvfb_screen is accepted.

Delivery is an owned, contiguous, read-only uint8 RGB array; no raw ring view
escapes this module.  See docs/spec/jq2a_m4_c2_ram_capture.md for boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
import threading
from typing import Sequence
import zlib

import numpy as np

from vadapt import rtring, xfb

from vadapt.window_stream_bank import Handle, FrameRef, Drop, StreamToken


@dataclass(frozen=True)
class CropSpec:
    handle: Handle
    epoch: int
    x: int
    y: int
    width: int
    height: int
    frame_seq: int


@dataclass(frozen=True)
class TrustedScreen:
    """Operator-provided provenance, not constructible from an action request.

    ``ring`` is a bytearray, mmap or other byte-oriented buffer of a published
    rtring v2 ring.  The source retains the buffer for the whole capture call.
    Width, height, epoch and handle must come from trusted registration, not
    from a requested crop or an untrusted ring header.
    """

    handle: Handle
    epoch: int
    width: int
    height: int
    ring: object


class CaptureFailure(ValueError):
    """A fail-closed validation/admission failure with stable ``reason``."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_UINT64_MAX = (1 << 64) - 1
_DEFAULT_QUOTA = 256 * (1 << 20)
_RETRIES = 8
_GLOBAL_HEADER = struct.Struct("<8sIIIIQQQ")
_SLOT_PAIR = struct.Struct("<QQ")
_UINT32 = struct.Struct("<I")
_UINT64 = struct.Struct("<Q")


def _handle_key(handle: object) -> tuple[str, str, int]:
    try:
        kind, ident, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise CaptureFailure("invalid_handle") from exc
    if (type(kind) is not str or not kind or type(ident) is not str or not ident
            or type(generation) is not int or generation < 1):
        raise CaptureFailure("invalid_handle")
    return kind, ident, generation


def _index(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and (value > 0 if positive else value >= 0)


def _crop_key(spec: CropSpec) -> tuple:
    return (*_handle_key(spec.handle), spec.epoch,
            spec.x, spec.y, spec.width, spec.height)


def _token(spec: CropSpec) -> StreamToken:
    # Length prefix makes the canonical source ID unambiguous, including IDs
    # containing colons. Coordinates are encoded without lossy hashing.
    kind, ident, generation = _handle_key(spec.handle)
    identifier = f"{len(ident)}:{ident}:crop:{spec.x}:{spec.y}:{spec.width}:{spec.height}"
    return StreamToken(Handle(kind, identifier, generation), spec.epoch)


def _check_request(spec: CropSpec) -> tuple[str, str, int]:
    """Reject malformed requests before resolving a source or reserving RAM."""
    key = _handle_key(spec.handle)
    if key[0] != "xvfb_screen":
        raise CaptureFailure("unsupported_mode")
    if not _index(spec.epoch):
        raise CaptureFailure("stale_epoch")
    if not _index(spec.frame_seq) or spec.frame_seq >= _UINT64_MAX:
        raise CaptureFailure("invalid_sequence")
    if not (_index(spec.x) and _index(spec.y)
            and _index(spec.width, positive=True) and _index(spec.height, positive=True)):
        raise CaptureFailure("invalid_crop")
    return key


def _check_geometry(spec: CropSpec, screen: TrustedScreen) -> None:
    """Check trusted identity/epoch/geometry before touching untrusted ring bytes."""
    key = _check_request(spec)
    if not isinstance(screen, TrustedScreen):
        raise CaptureFailure("source_identity")
    try:
        source_key = _handle_key(screen.handle)
    except CaptureFailure as exc:
        raise CaptureFailure("source_identity") from exc
    if source_key != key:
        raise CaptureFailure("source_identity")
    if not _index(screen.epoch) or spec.epoch != screen.epoch:
        raise CaptureFailure("stale_epoch")
    if (not _index(screen.width, positive=True) or not _index(screen.height, positive=True)
            or screen.width > xfb.MAX_SIDE or screen.height > xfb.MAX_SIDE):
        raise CaptureFailure("source_geometry")
    if (spec.x + spec.width > screen.width
            or spec.y + spec.height > screen.height):
        raise CaptureFailure("invalid_crop")

def _ring_layout(buffer: object, width: int, height: int):
    """Validate the source ring's layout against *trusted* source geometry."""
    try:
        view = memoryview(buffer)
    except TypeError as exc:
        raise CaptureFailure("invalid_source_ring") from exc
    if view.ndim != 1 or view.itemsize != 1 or not view.c_contiguous:
        raise CaptureFailure("invalid_source_ring")
    if view.nbytes < rtring.HDR:
        raise CaptureFailure("invalid_source_ring")
    try:
        magic, ver, slots, w, h, slot_bytes, data_off, _ = _GLOBAL_HEADER.unpack_from(view)
    except (struct.error, ValueError) as exc:
        raise CaptureFailure("invalid_source_ring") from exc
    if (magic != rtring.MAGIC or ver != rtring.VERSION or slots == 0
            or w != width or h != height or slot_bytes != w * h * 3
            or data_off != rtring.data_off(slots)
            or rtring.ring_bytes(slots, w, h) != view.nbytes):
        raise CaptureFailure("invalid_source_ring")
    return view, slots, data_off, slot_bytes


def _read_owned(buffer: object, width: int, height: int, requested: int,
                *, retries: int = _RETRIES) -> np.ndarray:
    """Read one stable even v2 slot, compare index/CRC, copy, then recheck.

    The source is a trusted injected holder of ring bytes, *not* an arbitrary
    native X window.  A mismatch is never represented as a success/empty frame.
    """
    view, slots, data_off, slot_bytes = _ring_layout(buffer, width, height)
    if not _index(requested) or requested >= _UINT64_MAX:
        raise CaptureFailure("invalid_sequence")
    if not _index(retries, positive=True):
        raise CaptureFailure("invalid_retries")
    offset = rtring.HDR + (requested % slots) * rtring.SLOT_HDR
    payload = data_off + (requested % slots) * slot_bytes
    for _ in range(retries):
        head_before = _UINT64.unpack_from(view, rtring.HEAD_OFF)[0]
        if head_before == 0 or requested >= head_before:
            raise CaptureFailure("absent")
        if head_before - 1 - requested >= slots:
            raise CaptureFailure("lapped")
        seq, index = _SLOT_PAIR.unpack_from(view, offset)
        if seq & 1:
            continue  # a writer owns this slot
        crc = _UINT32.unpack_from(view, offset + rtring.CRC_OFF)[0]
        if index != requested:
            # A scalar sequence check alone can read a stale equal value;
            # recheck the adjacent pair before classifying a mismatch.
            if (_UINT64.unpack_from(view, offset)[0] != seq
                    or _SLOT_PAIR.unpack_from(view, offset) != (seq, index)):
                continue
            raise CaptureFailure("lapped" if index > requested else "absent")
        # A distinct immutable bytes allocation is taken before the post-copy
        # seqlock/CRC checks. NumPy cannot re-enable writes to bytes-backed RGB.
        pixels = bytes(view[payload:payload + slot_bytes])
        if (_SLOT_PAIR.unpack_from(view, offset) != (seq, index)
                or _UINT32.unpack_from(view, offset + rtring.CRC_OFF)[0] != crc):
            continue
        # A producer may have advanced the head while we copied; only reject
        # if this requested frame is now provably out of the retained window.
        head_after = _UINT64.unpack_from(view, rtring.HEAD_OFF)[0]
        if head_after == 0 or head_after <= requested:
            continue
        if head_after - 1 - requested >= slots:
            raise CaptureFailure("lapped")
        if zlib.crc32(pixels) != crc:
            continue
        # Check again *after* CRC. Neither header nor checksum alone acts as a
        # native acquire/release fence; this is the bounded Python check.
        if (_SLOT_PAIR.unpack_from(view, offset) != (seq, index)
                or _UINT32.unpack_from(view, offset + rtring.CRC_OFF)[0] != crc
                or _GLOBAL_HEADER.unpack_from(view)[:7] !=
                   (rtring.MAGIC, rtring.VERSION, slots, width, height,
                    slot_bytes, data_off)):
            continue
        return np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
    raise CaptureFailure("torn")


@dataclass
class _MemoryRing:
    token: StreamToken
    width: int
    height: int
    size: int
    raw: bytearray
    last_seq: int = -1


class MemoryStreamBank:
    """Bounded independent in-RAM output rings; no workspace or disk writes.

    The bank is a small, conservative stand-in until the separately owned
    WindowStreamBank integration lands. ``admit`` is all-or-nothing: no new
    reservation is visible until every new bytearray has been constructed.
    Existing streams are never debited by an unsuccessful admission.
    """

    def __init__(self, *, quota_bytes: int = _DEFAULT_QUOTA, slots: int = 2):
        if not _index(quota_bytes, positive=True):
            raise ValueError("quota_bytes must be a positive exact integer")
        if not _index(slots, positive=True) or slots > (1 << 32) - 1:
            raise ValueError("slots must fit an exact positive u32")
        self.quota_bytes = quota_bytes
        self.slots = slots
        self._rings: dict[tuple, _MemoryRing] = {}
        self._geometry: dict[tuple, tuple[int, int]] = {}
        self._charged = 0
        self._lock = threading.Lock()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._charged

    def token_for(self, spec: CropSpec) -> StreamToken | None:
        with self._lock:
            ring = self._rings.get(_crop_key(spec))
            return None if ring is None else ring.token

    def admit(self, requests: Sequence[tuple[CropSpec, int, int]]) -> None:
        """Atomically account all *new* output rings, including metadata.

        Each request holds (crop, trusted_screen_width, trusted_screen_height).
        This method does not call the capture source or a bank callback.
        """
        with self._lock:
            planned: dict[tuple, tuple[CropSpec, int]] = {}
            geometry: dict[tuple, tuple[int, int]] = {}
            for spec, width, height in requests:
                _check_request(spec)
                if (not _index(width, positive=True) or not _index(height, positive=True)
                        or width > xfb.MAX_SIDE or height > xfb.MAX_SIDE):
                    raise CaptureFailure("source_geometry")
                if spec.x + spec.width > width or spec.y + spec.height > height:
                    raise CaptureFailure("invalid_crop")
                k = _crop_key(spec)
                source_key = (*_handle_key(spec.handle), spec.epoch)
                trusted_size = (width, height)
                if (source_key in self._geometry
                        and self._geometry[source_key] != trusted_size):
                    raise CaptureFailure("geometry_epoch")
                if source_key in geometry and geometry[source_key] != trusted_size:
                    raise CaptureFailure("geometry_epoch")
                geometry[source_key] = trusted_size
                if k in self._rings or k in planned:
                    continue
                planned[k] = (spec, rtring.ring_bytes(self.slots, spec.width, spec.height))
            total = sum(size for _, size in planned.values())
            if self._charged + total > self.quota_bytes:
                raise CaptureFailure("quota")
            # Build first: allocation failure cannot debit old live reservations.
            created = {}
            for k, (spec, size) in planned.items():
                buf = bytearray(size)
                w, h = spec.width, spec.height
                _GLOBAL_HEADER.pack_into(buf, 0, rtring.MAGIC, rtring.VERSION,
                                         self.slots, w, h, w * h * 3,
                                         rtring.data_off(self.slots), 0)
                created[k] = _MemoryRing(_token(spec), w, h, size, buf)
            self._rings.update(created)
            self._geometry.update(geometry)
            self._charged += total

    def publish(self, spec: CropSpec, image: np.ndarray) -> FrameRef:
        """Single-writer publication for one crop and owned revalidated read."""
        with self._lock:
            stream = self._rings.get(_crop_key(spec))
            if stream is None:
                raise CaptureFailure("not_admitted")
            if spec.frame_seq <= stream.last_seq:
                raise CaptureFailure("stale_sequence")
            if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                    or image.shape != (spec.height, spec.width, 3)
                    or not image.flags.c_contiguous):
                raise CaptureFailure("invalid_rgb")
            raw = stream.raw
            slot = spec.frame_seq % self.slots
            header_offset = rtring.HDR + slot * rtring.SLOT_HDR
            pixel_offset = rtring.data_off(self.slots) + slot * spec.width * spec.height * 3
            prev = _UINT64.unpack_from(raw, header_offset)[0]
            if prev & 1:
                raise CaptureFailure("busy_writer")
            # Commit order: odd seq -> render index -> payload -> CRC -> even
            # seq -> head. No Python native memory-order guarantee is claimed.
            _UINT64.pack_into(raw, header_offset, (prev + 1) & _UINT64_MAX)
            _UINT64.pack_into(raw, header_offset + 8, spec.frame_seq)
            pixels = image.tobytes(order="C")
            raw[pixel_offset:pixel_offset + len(pixels)] = pixels
            _UINT32.pack_into(raw, header_offset + rtring.CRC_OFF, zlib.crc32(pixels))
            _UINT64.pack_into(raw, header_offset, (prev + 2) & _UINT64_MAX)
            _UINT64.pack_into(raw, rtring.HEAD_OFF, spec.frame_seq + 1)
            stream.last_seq = spec.frame_seq
            rgb = _read_owned(raw, spec.width, spec.height, spec.frame_seq)
        return FrameRef(spec.handle, spec.epoch, spec.frame_seq, spec.width,
                        spec.height, rgb, hashlib.sha256(rgb.tobytes()).hexdigest())

    def close(self) -> None:
        """Drop only this bank's owned rings; returned FrameRefs stay owned."""
        with self._lock:
            self._rings.clear()
            self._geometry.clear()
            self._charged = 0


def _valid_ref(value: object, spec: CropSpec, expected: np.ndarray) -> bool:
    """Require a real FrameRef holding independently owned, immutable RGB24."""
    if not isinstance(value, FrameRef):
        return False
    try:
        if (_handle_key(value.handle) != _handle_key(spec.handle)
                or type(value.epoch) is not int or value.epoch != spec.epoch
                or type(value.frame_seq) is not int or value.frame_seq != spec.frame_seq
                or type(value.width) is not int or value.width != spec.width
                or type(value.height) is not int or value.height != spec.height):
            return False
        rgb = value.rgb
        if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
                or rgb.shape != expected.shape or not rgb.flags.c_contiguous
                or rgb.flags.writeable):
            return False
        # A writable owned ndarray with setflags(write=False) is NOT immutable:
        # callers can re-enable writes. Follow base arrays to a bytes owner.
        root = rgb
        while isinstance(root, np.ndarray) and root.base is not None:
            root = root.base
        if type(root) is not bytes:
            return False
        pixels = rgb.tobytes()
        return (pixels == expected.tobytes()
                and value.digest == hashlib.sha256(pixels).hexdigest())
    except (AttributeError, CaptureFailure, TypeError, ValueError):
        return False

def capture_many(specs: Sequence[CropSpec], *, source: object,
                 bank: object = None) -> list[FrameRef | Drop]:
    """Capture matching stable Xvfb ring indices and publish independent crops.

    ``source.resolve(handle)`` must return a trusted ``TrustedScreen`` with
    matching handle, epoch, width, height and byte-oriented v2 ring. The source
    owns its input rings. An optional bank implements ``admit`` and ``publish``
    as MemoryStreamBank does; ``None`` makes an ephemeral RAM-only bank.

    Unsupported modes, bounds, epoch, sequence and torn/lapped conditions are
    explicit Drops, never empty successful frames. Admission of the surviving
    valid requests is transactional across new output rings.
    """
    if not callable(getattr(source, "resolve", None)):
        raise TypeError("source must expose trusted resolve(handle)")
    if bank is None:
        bank = MemoryStreamBank()
    if not callable(getattr(bank, "admit", None)) or not callable(getattr(bank, "publish", None)):
        raise TypeError("bank must expose admit(requests) and publish(spec, image)")
    inputs = list(specs)
    for spec in inputs:
        if not isinstance(spec, CropSpec):
            raise TypeError("specs must contain CropSpec")

    results: list[FrameRef | Drop | None] = [None] * len(inputs)
    ready: list[tuple[int, CropSpec, np.ndarray, int, int]] = []
    screens: dict[tuple, TrustedScreen | CaptureFailure] = {}
    full_frames: dict[tuple, np.ndarray | CaptureFailure] = {}

    for pos, spec in enumerate(inputs):
        def drop(reason: str) -> None:
            results[pos] = Drop(spec.handle, spec.epoch, spec.frame_seq, reason)

        try:
            key = _check_request(spec)
            if key not in screens:
                try:
                    screen = source.resolve(spec.handle)
                except LookupError:
                    screen = CaptureFailure("source_unavailable")
                if screen is None:
                    screen = CaptureFailure("source_unavailable")
                screens[key] = screen
            screen = screens[key]
            if isinstance(screen, CaptureFailure):
                raise screen
            _check_geometry(spec, screen)
            fkey = (key, spec.epoch, spec.frame_seq)
            if fkey not in full_frames:
                try:
                    full_frames[fkey] = _read_owned(screen.ring, screen.width,
                                                    screen.height, spec.frame_seq)
                except CaptureFailure as exc:
                    full_frames[fkey] = exc
            frame = full_frames[fkey]
            if isinstance(frame, CaptureFailure):
                raise frame
            # Snapshot each crop into a distinct bytes owner. A NumPy array
            # created writable and then set read-only can be made writable
            # again by an injected bank; bytes-backed data cannot.
            crop_bytes = frame[spec.y:spec.y + spec.height,
                               spec.x:spec.x + spec.width].tobytes(order="C")
            crop = np.frombuffer(crop_bytes, dtype=np.uint8).reshape(
                spec.height, spec.width, 3)
            ready.append((pos, spec, crop, screen.width, screen.height))
        except CaptureFailure as exc:
            drop(exc.reason)

    if ready:
        try:
            bank.admit([(s, w, h) for _, s, _, w, h in ready])
        except CaptureFailure as exc:
            for pos, spec, _, _, _ in ready:
                results[pos] = Drop(spec.handle, spec.epoch, spec.frame_seq, exc.reason)
        else:
            for pos, spec, crop, _, _ in ready:
                try:
                    ref = bank.publish(spec, crop)
                    if not _valid_ref(ref, spec, crop):
                        raise CaptureFailure("bank_invalid_receipt")
                    results[pos] = ref
                except CaptureFailure as exc:
                    results[pos] = Drop(spec.handle, spec.epoch, spec.frame_seq, exc.reason)

    if any(r is None for r in results):
        raise RuntimeError("capture_many omitted a result")
    return results


__all__ = ["Handle", "FrameRef", "Drop", "StreamToken", "CropSpec",
           "TrustedScreen", "CaptureFailure", "MemoryStreamBank", "capture_many"]
