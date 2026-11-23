"""Bounded OpenEXR v2 scanline NONE reader; channel values are returned top-down.

Based on the measured room_CBLD reader. Unsupported layouts fail closed, including
subsampled channels, tiled/deep/multipart images and compressed scanlines.
"""
import struct
from pathlib import Path

import numpy as np

EXR_MAGIC = 20000630
MAX_NAME = 255
_DTYPES = {0: np.dtype('<u4'), 1: np.dtype('<f2'), 2: np.dtype('<f4')}


def read(path):
    """Return (channel names, HxWxC float32 array), without applying colour transforms."""
    b = Path(path).read_bytes()
    try:
        return _read(b)
    except (IndexError, KeyError, struct.error, UnicodeError, OverflowError) as exc:
        raise ValueError('E_EXR_NONE: malformed header or scanlines') from exc


def _read(b):
    def need(ok, reason):
        if not ok:
            raise ValueError('E_EXR_NONE: ' + reason)

    need(len(b) >= 8, 'truncated file')
    magic, version = struct.unpack_from('<II', b)
    need(magic == EXR_MAGIC and (version & ~0x400) == 2, 'only v2 single-part scanline supported')
    p = 8

    def string():
        nonlocal p
        end = b.find(b'\0', p, min(len(b), p + 256))
        need(end >= p, 'unterminated name')
        value = b[p:end].decode('ascii')
        p = end + 1
        return value

    hdr = {}
    while True:
        name = string()
        if not name:
            break
        kind = string()
        n, = struct.unpack_from('<i', b, p)
        p += 4
        need(n >= 0 and p + n <= len(b) and name not in hdr, 'attribute size or duplicate')
        hdr[name] = (kind, b[p:p+n])
        p += n
    for name, kind in (('channels', 'chlist'), ('compression', 'compression'),
                       ('dataWindow', 'box2i'), ('lineOrder', 'lineOrder')):
        need(name in hdr and hdr[name][0] == kind, 'missing or mistyped ' + name)
    need(hdr['compression'][1] == b'\0', 'compression must be NONE')
    need(hdr['lineOrder'][1] in (b'\0', b'\1', b'\2'), 'invalid lineOrder')
    x0, y0, x1, y1 = struct.unpack('<4i', hdr['dataWindow'][1])
    w, h = x1 - x0 + 1, y1 - y0 + 1
    need(w > 0 and h > 0 and h <= (len(b)-p)//8, 'invalid dimensions')
    cb = hdr['channels'][1]
    q, channels = 0, []
    while q < len(cb) and cb[q] != 0:
        end = cb.find(b'\0', q)
        need(end > q and end - q <= MAX_NAME, 'channel name')
        name = cb[q:end].decode('ascii')
        q = end + 1
        typ, linear, xs, ys = struct.unpack_from('<iB3xii', cb, q)
        q += 16
        need(typ in _DTYPES and xs == ys == 1 and linear in (0, 1), 'channel type or sampling')
        need(name not in [n for n, _ in channels], 'duplicate channel')
        channels.append((name, typ))
    need(channels and cb[q:] == b'\0', 'invalid channel list')
    rowbytes = sum(w * _DTYPES[t].itemsize for _, t in channels)
    data_start = p + h * 8
    need(h * (rowbytes + 8) <= len(b)-data_start, 'truncated pixels')
    offsets = np.frombuffer(b, dtype='<u8', count=h, offset=p)
    need(bool(np.all(offsets >= data_start)) and bool(np.all(offsets <= len(b)-rowbytes-8)), 'scanline offset')
    off = offsets.astype(np.int64)
    ordered = np.sort(off)
    need(bool(np.all(np.diff(ordered) >= rowbytes+8)), 'overlapping scanlines')
    buf = np.frombuffer(b, np.uint8)
    headers = buf[off[:, None] + np.arange(8)].copy().view('<i4').reshape(h, 2)
    y = headers[:, 0].astype(np.int64) - y0
    need(bool(np.all(headers[:, 1] == rowbytes)), 'scanline size')
    need(np.array_equal(np.sort(y), np.arange(h)), 'missing or duplicate row')
    out = np.empty((h, w, len(channels)), np.float32)
    # Vectorised offset reads also handle decreasing or non-contiguous storage.
    cursor = 0
    for i, (_, typ) in enumerate(channels):
        dtype = _DTYPES[typ]
        size = w * dtype.itemsize
        raw = buf[off[:, None] + 8 + cursor + np.arange(size)].copy()
        out[y, :, i] = raw.view(dtype).reshape(h, w)
        cursor += size
    return [name for name, _ in channels], out


def load_rgba(path):
    """Match bpy image pixels: bottom-up RGBA, absent colour/alpha channels = 1."""
    names, a = read(path)
    rgba = np.ones(a.shape[:2] + (4,), np.float32)
    for i, name in enumerate('RGBA'):
        if name in names:
            rgba[..., i] = a[..., names.index(name)]
    return np.ascontiguousarray(rgba[::-1])
