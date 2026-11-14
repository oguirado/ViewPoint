"""Single-producer, N-reader immutable frame ring backed by atomic filesystem snapshots.

Track P reference implementation (standalone, standard library only).
``swarm/`` must never import this module.

Layout of a ring directory::

    frame_<seq>.png    immutable image bytes, written once (tmp + rename)
    frame_<seq>.json   immutable sidecar: sha256, size, epochs, changed_regions
    latest.json        atomic pointer to the head frame (rename-replaced)
    .producer.lock     flock held by the one producer for its lifetime

Publication order is png -> sidecar -> latest.json -> eviction, so a frame is
never announced before both of its files are complete, and no reader ever
takes a lock.  A reader verifies size and SHA-256 of every image it returns.

Ring retention ``K``: with head ``H`` the retained frames are ``H-K+1 .. H``.
A frame ``seq <= H-K`` is evicted; asking for it raises ``FrameEvicted``
(code ``FRAME_EVICTED``).  That decision is made from ``latest.json`` (the
logical ring), never from the mere absence of a file, so it does not depend on
how far the producer's unlink pass has progressed.
"""
import fcntl
import hashlib
import json
import os
import struct
import time
import zlib

FRAME_EVICTED = "FRAME_EVICTED"
FRAME_NOT_PUBLISHED = "FRAME_NOT_PUBLISHED"
FRAME_CORRUPT = "FRAME_CORRUPT"
PRODUCER_BUSY = "PRODUCER_BUSY"

SCHEMA_VERSION = 1
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_LATEST = "latest.json"
_LOCK = ".producer.lock"
_TMP_PREFIX = ".tmp-"


class FanoutError(Exception):
    """Base class; ``code`` is the typed, machine-readable refusal."""

    code = "FANOUT_ERROR"


class FrameEvicted(FanoutError):
    code = FRAME_EVICTED

    def __init__(self, requested, oldest, head):
        super().__init__(
            "frame %d evicted from ring (retained %d..%d)" % (requested, oldest, head))
        self.requested = requested
        self.oldest = oldest
        self.head = head


class FrameNotPublished(FanoutError):
    code = FRAME_NOT_PUBLISHED

    def __init__(self, requested, head):
        super().__init__("frame %d not published yet (head %d)" % (requested, head))
        self.requested = requested
        self.head = head


class FrameCorrupt(FanoutError):
    """A retained frame failed its integrity check (torn or tampered read)."""

    code = FRAME_CORRUPT


class ProducerBusy(FanoutError):
    code = PRODUCER_BUSY


class Frame(object):
    """One verified, immutable snapshot."""

    __slots__ = ("seq", "meta", "data")

    def __init__(self, seq, meta, data):
        self.seq = seq
        self.meta = meta
        self.data = data

    @property
    def sha256(self):
        return self.meta["sha256"]


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def frame_paths(root, seq):
    stem = os.path.join(root, "frame_%d" % seq)
    return stem + ".png", stem + ".json"


def png_size(data):
    """Return ``(width, height)`` from a PNG IHDR, or ``(None, None)``."""
    if len(data) >= 24 and data[:8] == PNG_SIGNATURE and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None, None


def encode_png(width, height, pixels, channels=3):
    """Encode raw 8-bit pixels (1=gray, 3=RGB, 4=RGBA) as a PNG, filter 0."""
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    stride = width * channels
    if len(pixels) != stride * height:
        raise ValueError("pixel buffer does not match width*height*channels")
    raw = b"".join(
        b"\x00" + bytes(pixels[row * stride:(row + 1) * stride]) for row in range(height))

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (PNG_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def decode_png(data):
    """Decode an 8-bit non-interlaced gray/RGB/RGBA PNG.

    Returns ``(width, height, channels, pixels)`` with ``pixels`` a ``bytes``
    object of ``width*height*channels``.  Integer arithmetic only.
    """
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG")
    pos, idat, header = 8, [], None
    while pos + 8 <= len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
        pos += 12 + length
    if header is None:
        raise ValueError("PNG without IHDR")
    width, height, depth, color_type, _, _, interlace = header
    if depth != 8 or interlace != 0 or color_type not in (0, 2, 6):
        raise ValueError("unsupported PNG variant")
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    stride = width * channels
    raw = zlib.decompress(b"".join(idat))
    if len(raw) != (stride + 1) * height:
        raise ValueError("PNG data length mismatch")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for row in range(height):
        base = row * (stride + 1)
        ftype = raw[base]
        line = bytearray(raw[base + 1:base + 1 + stride])
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        elif ftype != 0:
            raise ValueError("bad PNG filter type %d" % ftype)
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


def _atomic_write(root, name, data, fsync):
    """Write ``data`` to ``root/name`` so readers see all of it or none of it."""
    tmp = os.path.join(root, "%s%s.%d" % (_TMP_PREFIX, name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(root, name))


def _read_latest(root):
    """Return the head sidecar, or ``None`` when nothing has been published."""
    try:
        with open(os.path.join(root, _LATEST), "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    try:
        meta = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise FrameCorrupt("latest.json is not valid JSON: %s" % exc)
    if (not isinstance(meta, dict) or not _is_int(meta.get("frame"))
            or not _is_int(meta.get("retention")) or meta["retention"] < 1):
        raise FrameCorrupt("latest.json lacks frame/retention")
    return meta


class FrameBroadcaster(object):
    """The single producer of a frame ring.

    ``retention`` is the ring size ``K``.  A second producer on the same
    directory is refused with ``ProducerBusy`` (non-blocking ``flock``).
    """

    def __init__(self, root, retention=8, fsync=False, clock_ms=None):
        if not _is_int(retention) or retention < 1:
            raise ValueError("retention must be an integer >= 1")
        self.root = os.fspath(root)
        self.retention = retention
        self._fsync = bool(fsync)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1000000)
        os.makedirs(self.root, exist_ok=True)
        self._lock_fd = os.open(os.path.join(self.root, _LOCK), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise ProducerBusy("another producer owns %s" % self.root)
        latest = _read_latest(self.root)
        self.head = latest["frame"] if latest else 0
        self._sweep()

    def publish(self, data, view_epoch=0, scene_epoch=0, changed_regions=(),
                width=None, height=None, extra=None):
        """Publish one immutable frame and return its sidecar dict."""
        if self._lock_fd is None:
            raise ProducerBusy("broadcaster is closed")
        data = bytes(data)
        seq = self.head + 1
        png_w, png_h = png_size(data)
        meta = dict(extra or {})
        meta.update({
            "v": SCHEMA_VERSION,
            "frame": seq,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "width": png_w if width is None else width,
            "height": png_h if height is None else height,
            "view_epoch": view_epoch,
            "scene_epoch": scene_epoch,
            "changed_regions": [list(r) for r in changed_regions],
            "ts_ms": self._clock_ms(),
            "retention": self.retention,
            "oldest_frame": max(1, seq - self.retention + 1),
        })
        blob = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
        _atomic_write(self.root, "frame_%d.png" % seq, data, self._fsync)
        _atomic_write(self.root, "frame_%d.json" % seq, blob, self._fsync)
        _atomic_write(self.root, _LATEST, blob, self._fsync)  # commit point
        self.head = seq
        self._evict(seq - self.retention)
        return meta

    def _evict(self, seq):
        # Sidecar first: a reader that still finds the sidecar may lose the
        # image, and resolves that through latest.json as FRAME_EVICTED.
        if seq < 1:
            return
        png, sidecar = frame_paths(self.root, seq)
        for path in (sidecar, png):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _sweep(self):
        """Drop leftovers of a previous producer: temp files, stale frames."""
        low = self.head - self.retention
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            if name.startswith(_TMP_PREFIX):
                os.unlink(path)
                continue
            if not name.startswith("frame_"):
                continue
            stem = name[6:].split(".", 1)[0]
            if stem.isdigit() and (int(stem) <= low or int(stem) > self.head):
                os.unlink(path)

    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FrameReader(object):
    """A lock-free reader with its own cursor.  Any number may run at once."""

    def __init__(self, root, name="reader", start=0):
        self.root = os.fspath(root)
        self.name = name
        self.cursor = start  # last frame consumed through next()

    def latest(self):
        """Head sidecar, or ``None`` before the first publication."""
        return _read_latest(self.root)

    def _bounds(self):
        latest = _read_latest(self.root)
        if latest is None:
            return 0, 1
        head = latest["frame"]
        return head, max(1, head - latest["retention"] + 1)

    def read(self, seq):
        """Return the verified ``Frame`` ``seq`` or raise a typed error."""
        if not _is_int(seq) or seq < 1:
            raise ValueError("frame sequence must be an integer >= 1")
        head, oldest = self._bounds()
        if seq > head:
            raise FrameNotPublished(seq, head)
        if seq < oldest:
            raise FrameEvicted(seq, oldest, head)
        png_path, sidecar_path = frame_paths(self.root, seq)
        try:
            with open(sidecar_path, "rb") as handle:
                raw_meta = handle.read()
            with open(png_path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            head, oldest = self._bounds()
            if seq < oldest:
                raise FrameEvicted(seq, oldest, head)
            raise FrameCorrupt("frame %d is inside the ring but missing on disk" % seq)
        try:
            meta = json.loads(raw_meta.decode("utf-8"))
        except ValueError as exc:
            raise FrameCorrupt("frame %d sidecar is not valid JSON: %s" % (seq, exc))
        if not isinstance(meta, dict) or meta.get("frame") != seq:
            raise FrameCorrupt("frame %d sidecar names another frame" % seq)
        if meta.get("size_bytes") != len(data):
            raise FrameCorrupt("frame %d size mismatch (torn read)" % seq)
        if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
            raise FrameCorrupt("frame %d sha256 mismatch (torn read)" % seq)
        return Frame(seq, meta, data)

    def read_latest(self):
        """Return the newest verified frame, or ``None`` if the ring is empty."""
        while True:
            head, _ = self._bounds()
            if head == 0:
                return None
            try:
                return self.read(head)
            except FrameEvicted:
                continue  # the head moved K frames while we were reading

    def next(self):
        """Return frame ``cursor+1`` or ``None`` if it is not published yet.

        Raises ``FrameEvicted`` when this reader fell behind the ring; the
        cursor is left untouched so the caller must ``resync()`` explicitly.
        """
        try:
            frame = self.read(self.cursor + 1)
        except FrameNotPublished:
            return None
        self.cursor = frame.seq
        return frame

    def resync(self):
        """Full re-observe: jump the cursor to the head and return that frame."""
        frame = self.read_latest()
        if frame is not None:
            self.cursor = frame.seq
        return frame
