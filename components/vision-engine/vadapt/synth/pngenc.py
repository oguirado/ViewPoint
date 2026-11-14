"""PNG encoder (zlib, filters 0-4) for decode tests: colour type 2/6, 8 bit, non-interlaced."""
import struct
import zlib

import numpy as np


def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _filter_row(ft, row, prev, bpp):
    n = len(row)
    out = np.zeros(n, np.uint8)
    for i in range(n):
        a = int(row[i - bpp]) if i >= bpp else 0
        b = int(prev[i])
        c = int(prev[i - bpp]) if i >= bpp else 0
        pred = (0, a, b, (a + b) // 2, _paeth(a, b, c))[ft]
        out[i] = (int(row[i]) - pred) & 0xFF
    return out


def encode_png(rgb, filt=0, alpha=None):
    """rgb uint8 (H,W,3); filt in 0..4, or -1 = cycle filters per row; alpha uint8 (H,W) -> colour type 6."""
    H, W, _ = rgb.shape
    img = rgb if alpha is None else np.concatenate([rgb, alpha[:, :, None]], axis=2)
    bpp = img.shape[2]
    rows = img.reshape(H, W * bpp)
    prev = np.zeros(W * bpp, np.uint8)
    raw = bytearray()
    for y in range(H):
        ft = (y % 5) if filt < 0 else filt
        raw.append(ft)
        raw += (rows[y] if ft == 0 else _filter_row(ft, rows[y], prev, bpp)).tobytes()
        prev = rows[y]
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2 if alpha is None else 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + _chunk(b"IEND", b"")
