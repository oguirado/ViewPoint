"""LANG-X Experiment X1 fluency seed py_w1.

Provides Python + numpy reference-style kernels:
- repack_bgrx_to_rgb: BGRX32 to RGB24 repack with stride.
- damage_tiles: damage rectangles to tile bitset for grid s.
"""

import math
import numpy as np
from numpy.lib.stride_tricks import as_strided


E_DIM = "E_DIM"
E_STRIDE = "E_STRIDE"
E_SHORT = "E_SHORT"
E_RECT = "E_RECT"
E_GRID = "E_GRID"


class X1Error(Exception):
    """Exception raised for errors in LANG-X X1 kernels."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def repack_bgrx_to_rgb(src, w: int, h: int, src_stride: int, dst, dst_stride: int) -> None:
    """Repack BGRX32 buffer to RGB24 buffer with stride.

    Inputs:
    - src: bytes-like object containing source pixel data.
    - w: width in pixels (0..16384).
    - h: height in pixels (0..16384).
    - src_stride: source row stride in bytes (>= 4 * w).
    - dst: writable bytearray or numpy uint8 array destination buffer.
    - dst_stride: destination row stride in bytes (>= 3 * w).

    Errors checked before write:
    - E_DIM for w or h outside 0..16384.
    - E_STRIDE for src_stride < 4*w or dst_stride < 3*w.
    - E_SHORT for source or destination buffer too short.
    """
    if not (0 <= w <= 16384 and 0 <= h <= 16384):
        raise X1Error(E_DIM)

    if src_stride < 4 * w or dst_stride < 3 * w:
        raise X1Error(E_STRIDE)

    if w == 0 or h == 0:
        return

    req_src_len = (h - 1) * src_stride + 4 * w
    req_dst_len = (h - 1) * dst_stride + 3 * w

    src_mv = memoryview(src)
    dst_mv = memoryview(dst)

    if src_mv.nbytes < req_src_len or dst_mv.nbytes < req_dst_len:
        raise X1Error(E_SHORT)

    # Use numpy strided views without per-pixel Python loop
    src_arr = np.frombuffer(src, dtype=np.uint8)
    if isinstance(dst, np.ndarray):
        dst_arr = dst.reshape(-1)
    else:
        dst_arr = np.frombuffer(dst, dtype=np.uint8)

    src_view = as_strided(src_arr, shape=(h, w, 4), strides=(src_stride, 4, 1))
    dst_view = as_strided(dst_arr, shape=(h, w, 3), strides=(dst_stride, 3, 1))

    # BGRX -> B=0, G=1, R=2, X=3
    # Destination receives R=2, G=1, B=0
    dst_view[:, :, :] = src_view[:, :, [2, 1, 0]]


def damage_tiles(w: int, h: int, s: int, rects, use_numpy_path: bool = True):
    """Damage rectangles to tile bitset for grid s.

    Inputs:
    - w: screen width (1..16384).
    - h: screen height (1..16384).
    - s: tile size (power of two from 1..256).
    - rects: list of (x, y, w, h) rectangles as signed 32-bit integers.

    Returns:
    - (words, popcount): list of unsigned 64-bit int words and total set bit count.
    """
    if not (1 <= w <= 16384 and 1 <= h <= 16384):
        raise X1Error(E_DIM)

    if not (1 <= s <= 256 and (s & (s - 1)) == 0):
        raise X1Error(E_GRID)

    gw = (w + s - 1) // s
    gh = (h + s - 1) // s
    total_tiles = gw * gh
    n_words = (total_tiles + 63) // 64

    # Validate rectangles first
    for r in rects:
        rx, ry, rw, rh = r[0], r[1], r[2], r[3]
        if rw < 0 or rh < 0:
            raise X1Error(E_RECT)

    if use_numpy_path and len(rects) > 0:
        rects_arr = np.asanyarray(rects, dtype=np.int64)
        rx, ry, rw, rh = rects_arr[:, 0], rects_arr[:, 1], rects_arr[:, 2], rects_arr[:, 3]

        x0 = np.maximum(rx, 0)
        y0 = np.maximum(ry, 0)
        x1 = np.minimum(rx + rw, w)
        y1 = np.minimum(ry + rh, h)

        valid = (x1 > x0) & (y1 > y0)
        x0, y0, x1, y1 = x0[valid], y0[valid], x1[valid], y1[valid]

        tile_grid = np.zeros((gh, gw), dtype=bool)
        if len(x0) > 0:
            tx0 = x0 // s
            tx1 = (x1 + s - 1) // s
            ty0 = y0 // s
            ty1 = (y1 + s - 1) // s

            for t0_y, t1_y, t0_x, t1_x in zip(ty0, ty1, tx0, tx1):
                tile_grid[t0_y:t1_y, t0_x:t1_x] = True

        flat = tile_grid.reshape(-1)
        popcount = int(np.count_nonzero(flat))

        pad_len = n_words * 64 - len(flat)
        if pad_len > 0:
            flat_padded = np.pad(flat, (0, pad_len), mode='constant', constant_values=False)
        else:
            flat_padded = flat

        matrix = flat_padded.reshape(n_words, 64)
        powers = (np.uint64(1) << np.arange(64, dtype=np.uint64))
        words_arr = (matrix.astype(np.uint64) * powers).sum(axis=1, dtype=np.uint64)
        words = [int(x) for x in words_arr]
        return words, popcount

    # Plain loop path
    words = [0] * n_words
    for rx, ry, rw, rh in rects:
        x0 = max(rx, 0)
        y0 = max(ry, 0)
        x1 = min(rx + rw, w)
        y1 = min(ry + rh, h)

        if x1 <= x0 or y1 <= y0:
            continue

        tx0 = x0 // s
        tx1 = (x1 + s - 1) // s
        ty0 = y0 // s
        ty1 = (y1 + s - 1) // s

        for ty in range(ty0, ty1):
            row_offset = ty * gw
            for tx in range(tx0, tx1):
                t = row_offset + tx
                w_idx = t // 64
                b_idx = t % 64
                words[w_idx] |= (1 << b_idx)

    popcount = sum(bin(w).count('1') for w in words)
    return words, popcount
