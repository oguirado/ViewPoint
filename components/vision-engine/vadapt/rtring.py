"""RT1-L1 RAM frame ring: SPSC ring of rgb24 frames in ONE file inside a RamWorkspace (STORAGE LAW).

File layout (little endian):
  0   global header (64 B): magic b"VQARING1", version u32, slots u32, width u32, height u32,
      slot_bytes u64, data_off u64, head u64 (= newest committed render_index + 1; 0 = empty)
  64 + 64*k   slot header k (64 B aligned): seq u64 (even = stable, odd = writing), render_index u64,
              crc32 u32 (zlib.crc32 of the slot payload, written BEFORE the even seq)
  data_off + k*slot_bytes   frame k (slot = render_index % slots), data_off is 4096 aligned

Writer protocol per frame: seq odd -> slot render_index -> memcpy -> seq even -> head. Reader: seq (even) ->
render_index check -> view -> seq again (equal, else torn -> bounded retry -> deterministic drop, gap logged)
-> payload crc32 check (mismatch = torn). A zero-copy view is only trusted while Frame.valid() is True; use
RingReader.read(fn) to get that check for free.
Counters: .overrun = indices the producer lapped before the reader got them (clean drop, NOT torn);
.torn = reads that saw an in-flight/inconsistent slot (the "0 torn" gate counts only this); .gaps = skipped ranges.
Limit [H]: Python cannot emit ARM acquire/release fences (stlr/ldar, dmb ish); mmap stores/loads are program-ordered
only. The per-slot crc32 is the Python-level torn detector for a reordered store/load. Cost: zlib.crc32 over the
slot, once in the writer and once per delivered frame in the reader (4K rgb24 = 24.9 MB, measured ~5 ms per
pass on this host; 1000x600 ~1 ms); pass verify=False to RingReader to trade it away. RT2 item: native fenced
seqlock (AtomicU64 Acquire/Release) in visioncore_native replaces the crc as the primary guarantee.
close() with live numpy views (F5): the unmap is impossible, so the file AND its quota reservation are KEPT
(tmpfs pages stay resident) until a later close() succeeds after the views are dropped (or ws.close()).
"""
import logging
import mmap
import os
import struct
import zlib
from pathlib import Path

import numpy as np

from vadapt import ramws

LOG = logging.getLogger("vadapt.rtring")
MAGIC = b"VQARING1"
VERSION = 2
HDR = 64
SLOT_HDR = 64
ALIGN = 4096
MASK = (1 << 64) - 1
REL = "ring/frames.rgb24"
_G = struct.Struct("<8sIIIIQQQ")
_Q = struct.Struct("<Q")
_QQ = struct.Struct("<QQ")
_I = struct.Struct("<I")
CRC_OFF = 16
HEAD_OFF = 40


def data_off(slots):
    return (HDR + slots * SLOT_HDR + ALIGN - 1) // ALIGN * ALIGN


def ring_bytes(slots, w, h):
    return data_off(slots) + slots * w * h * 3


def _map(fd, size, write):
    prot = mmap.PROT_READ | (mmap.PROT_WRITE if write else 0)
    flags = mmap.MAP_SHARED | getattr(mmap, "MAP_POPULATE", 0)
    mm = mmap.mmap(fd, size, flags=flags, prot=prot)
    advice = "unavailable"
    if hasattr(mmap, "MADV_HUGEPAGE"):
        try:
            mm.madvise(mmap.MADV_HUGEPAGE)
            advice = "ok"
        except OSError as e:
            advice = "failed: %s" % e
            LOG.warning("madvise(MADV_HUGEPAGE) failed: %s", e)
    return mm, advice


def _unmap(mm):
    try:
        mm.close()
    except BufferError:
        LOG.warning("ring closed with live numpy views; unmap deferred to GC")


class RingWriter:
    """Producer side. Creates the file via ws.path(), reserves quota BEFORE ftruncate."""

    def __init__(self, ws, slots, width, height, rel=REL, _seq0=0):
        if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (slots, width, height)):
            raise ValueError("slots/width/height must be positive ints")
        if _seq0 & 1:
            raise ValueError("seq0 must be even")
        self.ws, self.slots, self.width, self.height = ws, slots, width, height
        self.slot_bytes = width * height * 3
        self.data_off = data_off(slots)
        total = self.data_off + slots * self.slot_bytes
        self.path = ws.path(rel)  # E_RAM_ESCAPE outside the workspace root
        self.size, self._rel, self.index, self._open_slot = total, rel, -1, None
        self._closed = self._released = False
        # O_EXCL first: a collision raises before anything is reserved (F4); the empty file holds no byte yet
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                ws.reserve(total)  # E_RAM_QUOTA before any byte exists
            except BaseException:
                self._drop_file()
                raise
            try:
                os.ftruncate(fd, total)
                self._mm, self.advice = _map(fd, total, True)
            except BaseException:
                self._drop_file()
                self._resync()  # release() returns st_blocks only; give back the rest of the reservation
                raise
        finally:
            os.close(fd)
        _G.pack_into(self._mm, 0, MAGIC, VERSION, slots, width, height, self.slot_bytes, self.data_off, 0)
        for k in range(slots):
            _QQ.pack_into(self._mm, HDR + k * SLOT_HDR, _seq0, 0)

    def _drop_file(self):
        try:
            self.ws.release(self.path)
        except (ramws.RamError, OSError):
            pass

    def _resync(self):
        try:
            self.ws.account()
        except (ramws.RamError, OSError):
            pass

    def _seq(self, k):
        return _Q.unpack_from(self._mm, HDR + k * SLOT_HDR)[0]

    def _slot_view(self, k):
        return np.frombuffer(self._mm, dtype=np.uint8, count=self.slot_bytes,
                             offset=self.data_off + k * self.slot_bytes).reshape(self.height, self.width, 3)

    def _begin(self, r):
        if self._closed:
            raise ValueError("ring closed")
        if self._open_slot is not None:
            raise ValueError("previous frame not committed")
        if r <= self.index:
            raise ValueError("render_index must increase (%d <= %d)" % (r, self.index))
        k = r % self.slots
        s = self._seq(k)
        _Q.pack_into(self._mm, HDR + k * SLOT_HDR, (s + 1) & MASK)  # odd: writing
        _Q.pack_into(self._mm, HDR + k * SLOT_HDR + 8, r)
        self._open_slot = (k, s, r)
        return k

    def _commit(self):
        k, s, r = self._open_slot
        _I.pack_into(self._mm, HDR + k * SLOT_HDR + CRC_OFF, zlib.crc32(self._slot_view(k)))  # before the even seq
        _Q.pack_into(self._mm, HDR + k * SLOT_HDR, (s + 2) & MASK)  # even: stable
        _Q.pack_into(self._mm, HEAD_OFF, r + 1)
        self.index, self._open_slot = r, None

    def _copy(self, k, frame):
        self._slot_view(k)[...] = frame

    def begin(self, render_index=None):
        """Zero-copy producer path: writable (h, w, 3) view of the slot; call commit() when filled."""
        k = self._begin(self.index + 1 if render_index is None else render_index)
        return self._slot_view(k)

    def commit(self):
        self._commit()
        return self.index

    def publish(self, frame, render_index=None):
        frame = np.asarray(frame)
        if frame.dtype != np.uint8 or frame.shape != (self.height, self.width, 3):
            raise ValueError("frame must be uint8 (%d, %d, 3)" % (self.height, self.width))
        k = self._begin(self.index + 1 if render_index is None else render_index)
        self._copy(k, frame)
        self._commit()
        return self.index

    def close(self):
        """Unmap, then ws.release() the ring file (quota returned). With live views the unmap is deferred and the
        file + reservation are kept; call close() again after dropping them (F5)."""
        self._closed = True
        if self._released:
            return
        try:
            self._mm.close()
        except BufferError:
            LOG.warning("ring closed with live numpy views; file and quota kept until they drop")
            return
        self._released = True
        if self.path.exists():
            self.ws.release(self.path)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Frame:
    __slots__ = ("view", "render_index", "_reader", "_k", "_seq")

    def __init__(self, view, render_index, reader, k, seq):
        self.view, self.render_index, self._reader, self._k, self._seq = view, render_index, reader, k, seq

    def valid(self):
        """True while the producer has not started overwriting this slot."""
        return self._reader._seq(self._k) == self._seq


class RingReader:
    """Consumer side (any process). Read-only mapping; views are read-only numpy arrays."""

    def __init__(self, path, root=ramws.ROOT, verify=True):
        real = os.path.realpath(path)
        base = os.path.realpath(root)
        if not real.startswith(base + os.sep):
            raise ramws.RamError("E_RAM_ESCAPE", "ring outside ramws root: %s" % path)
        fd = os.open(real, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            if size < HDR:
                raise ValueError("ring file too small")
            self._mm, self.advice = _map(fd, size, False)
        finally:
            os.close(fd)
        magic, ver, self.slots, self.width, self.height, self.slot_bytes, self.data_off, _ = _G.unpack_from(self._mm, 0)
        if magic != MAGIC or ver != VERSION:
            _unmap(self._mm)
            raise ValueError("not a VQARING1 v%d file" % VERSION)
        self.verify = verify
        self.last, self.gaps, self.torn, self.overrun = -1, [], [], []

    def _seq(self, k):
        return _Q.unpack_from(self._mm, HDR + k * SLOT_HDR)[0]

    def head(self):
        """Newest committed render_index, or -1."""
        return _Q.unpack_from(self._mm, HEAD_OFF)[0] - 1

    def _try(self, r, retries):
        """(Frame, None) or (None, status): 'lapped' (producer overwrote r), 'absent' (r never published),
        'torn' (in-flight / inconsistent after bounded retries)."""
        k = r % self.slots
        off = HDR + k * SLOT_HDR
        for _ in range(retries):
            s1 = _Q.unpack_from(self._mm, off)[0]
            if s1 & 1:
                continue
            ri = _Q.unpack_from(self._mm, off + 8)[0]
            crc = _I.unpack_from(self._mm, off + CRC_OFF)[0]
            if ri != r:
                if _Q.unpack_from(self._mm, off)[0] != s1:
                    continue
                # Reconfirm both fields: two stale scalar seq loads alone may
                # disagree with the observed ri. This Python-model check is
                # neither an atomic snapshot nor a hardware acquire fence.
                if _QQ.unpack_from(self._mm, off) != (s1, ri):
                    continue
                return None, ("lapped" if ri > r else "absent")
            v = np.frombuffer(self._mm, dtype=np.uint8, count=self.slot_bytes,
                              offset=self.data_off + k * self.slot_bytes).reshape(self.height, self.width, 3)
            if _Q.unpack_from(self._mm, off)[0] != s1:
                continue
            if self.verify and zlib.crc32(v) != crc:
                continue  # payload does not match what the writer sealed: reordered/partial store
            return Frame(v, r, self, k, s1), None
        return None, "torn"

    def _drop(self, r, status):
        if status == "torn":
            self.torn.append(r)
            LOG.warning("drop render_index %d (torn)", r)
        elif status == "lapped":
            self.overrun.append(r)
            LOG.info("overrun render_index %d (lapped by producer)", r)

    def acquire(self, r, retries=8):
        """Zero-copy Frame for render_index r, or None (lapped -> .overrun, torn -> .torn)."""
        f, status = self._try(r, retries)
        if f is None:
            self._drop(r, status)
            return None
        if r > self.last + 1:
            self.gaps.append((self.last + 1, r - 1))
            LOG.info("render_index gap %d..%d", self.last + 1, r - 1)
        self.last = max(self.last, r)
        return f

    def poll(self, retries=8, latest=False):
        """Next unread frame IN ORDER (every intact slot is delivered; only lapped ones are dropped, counted in
        .overrun; torn ones in .torn), or None. latest=True jumps to the newest committed frame (skipped = gap)."""
        h = self.head()
        if h < 0 or h <= self.last:
            return None
        if latest:
            return self.acquire(h, retries)
        lo = max(self.last + 1, h - self.slots + 1)
        if self.last >= 0 and lo > self.last + 1:
            for r in range(self.last + 1, lo):
                self._drop(r, "lapped")
            self.gaps.append((self.last + 1, lo - 1))
        for r in range(lo, h + 1):
            f, status = self._try(r, retries)
            if f is not None:
                self.last = r
                return f
            self._drop(r, status)
            if status == "absent":
                self.gaps.append((r, r))
            self.last = r
        return None

    def read(self, fn, retries=8):
        """Run fn(view) on the newest frame with the post-use seq check; returns (result, render_index) or None."""
        for _ in range(retries):
            f = self.poll(retries, latest=True)
            if f is None:
                return None
            res = fn(f.view)
            if f.valid():
                return res, f.render_index
            self.torn.append(f.render_index)
        return None

    def close(self):
        _unmap(self._mm)
