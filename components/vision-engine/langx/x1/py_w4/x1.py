"""LANG-X Experiment X1 fluency seed py_w4 implementation.

Kernels K1 (repack_bgrx_to_rgb) and K2 (damage_tiles).
"""

import math
import numpy as np


class X1Error(Exception):
    """Custom exception class for X1 errors with a `code` attribute."""

    def __init__(self, code: str):
        super().__init__(f"X1Error: {code}")
        self.code = code


def repack_bgrx_to_rgb(src, w: int, h: int, src_stride: int, dst, dst_stride: int) -> None:
    """Kernel K1: BGRX32 to RGB24 repack with stride.

    src: bytes-like object
    dst: writable bytearray or numpy uint8 array
    """
    if not (0 <= w <= 16384 and 0 <= h <= 16384):
        raise X1Error("E_DIM")

    if src_stride < 4 * w or dst_stride < 3 * w:
        raise X1Error("E_STRIDE")

    if w == 0 or h == 0:
        min_src_len = 0
        min_dst_len = 0
    else:
        min_src_len = (h - 1) * src_stride + 4 * w
        min_dst_len = (h - 1) * dst_stride + 3 * w

    # Get byte length of src and dst
    if isinstance(src, (bytes, bytearray, memoryview)):
        src_len = len(src)
    else:
        src_len = np.asarray(src).nbytes

    if isinstance(dst, np.ndarray):
        dst_len = dst.nbytes
    elif isinstance(dst, (bytearray, memoryview)):
        dst_len = len(dst)
    else:
        dst_len = len(dst)

    if src_len < min_src_len or dst_len < min_dst_len:
        raise X1Error("E_SHORT")

    if w == 0 or h == 0:
        return

    # Convert src and dst to 1D uint8 numpy arrays without copying if possible
    if isinstance(src, np.ndarray):
        src_np = src.view(np.uint8).reshape(-1)
    else:
        src_np = np.frombuffer(src, dtype=np.uint8)

    if isinstance(dst, np.ndarray):
        dst_np = dst.view(np.uint8).reshape(-1)
    else:
        dst_np = np.frombuffer(dst, dtype=np.uint8)

    # Use strided views to perform the byte permutation
    src_view = np.lib.stride_tricks.as_strided(
        src_np, shape=(h, w, 4), strides=(src_stride, 4, 1)
    )
    dst_view = np.lib.stride_tricks.as_strided(
        dst_np, shape=(h, w, 3), strides=(dst_stride, 3, 1)
    )

    # BGRX [0, 1, 2, 3] -> RGB [2, 1, 0]
    dst_view[...] = src_view[:, :, [2, 1, 0]]


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def damage_tiles_plain(w: int, h: int, s: int, rects):
    """Plain Python loop implementation for damage_tiles."""
    gw = (w + s - 1) // s
    gh = (h + s - 1) // s
    num_tiles = gw * gh
    num_words = (num_tiles + 63) // 64
    words = [0] * num_words

    for r in rects:
        rx, ry, rw, rh = r
        if rw < 0 or rh < 0:
            raise X1Error("E_RECT")

        x0 = max(int(rx), 0)
        y0 = max(int(ry), 0)
        x1 = min(int(rx) + int(rw), w)
        y1 = min(int(ry) + int(rh), h)

        if x1 <= x0 or y1 <= y0:
            continue

        tx0 = x0 // s
        tx1 = (x1 + s - 1) // s
        ty0 = y0 // s
        ty1 = (y1 + s - 1) // s

        for ty in range(ty0, ty1):
            for tx in range(tx0, tx1):
                t = ty * gw + tx
                words[t // 64] |= 1 << (t % 64)

    popcount = sum(bin(word).count("1") for word in words)
    return words, popcount


def damage_tiles_numpy(w: int, h: int, s: int, rects):
    """Vectorized numpy implementation for damage_tiles."""
    gw = (w + s - 1) // s
    gh = (h + s - 1) // s
    num_tiles = gw * gh
    num_words = (num_tiles + 63) // 64

    if not rects:
        return [0] * num_words, 0

    r_arr = np.array(rects, dtype=np.int64)
    if np.any(r_arr[:, 2] < 0) or np.any(r_arr[:, 3] < 0):
        raise X1Error("E_RECT")

    rx, ry, rw, rh = r_arr[:, 0], r_arr[:, 1], r_arr[:, 2], r_arr[:, 3]
    x0 = np.maximum(rx, 0)
    y0 = np.maximum(ry, 0)
    x1 = np.minimum(rx + rw, w)
    y1 = np.minimum(ry + rh, h)

    valid = (x1 > x0) & (y1 > y0)
    if not np.any(valid):
        return [0] * num_words, 0

    x0_v = x0[valid]
    y0_v = y0[valid]
    x1_v = x1[valid]
    y1_v = y1[valid]

    tx0 = x0_v // s
    tx1 = (x1_v + s - 1) // s
    ty0 = y0_v // s
    ty1 = (y1_v + s - 1) // s

    grid = np.zeros((gh, gw), dtype=bool)
    for t_x0, t_x1, t_y0, t_y1 in zip(tx0, tx1, ty0, ty1):
        grid[t_y0:t_y1, t_x0:t_x1] = True

    flat_grid = grid.ravel()[:num_tiles]
    padded_len = num_words * 64
    if len(flat_grid) < padded_len:
        flat_grid = np.pad(flat_grid, (0, padded_len - len(flat_grid)))

    bit_matrix = flat_grid.reshape(num_words, 64)
    powers = 1 << np.arange(64, dtype=np.uint64)
    words_u64 = np.dot(bit_matrix.astype(np.uint64), powers)

    words_list = [int(val) for val in words_u64]
    popcount = int(np.sum(flat_grid[:num_tiles]))
    return words_list, popcount


def damage_tiles(w: int, h: int, s: int, rects):
    """Kernel K2: Damage rectangles to tile bitset.

    Returns (words, popcount) where words is a list of uint64 ints.
    """
    if not (1 <= w <= 16384 and 1 <= h <= 16384):
        raise X1Error("E_DIM")

    if not (1 <= s <= 256 and _is_power_of_two(s)):
        raise X1Error("E_GRID")

    # For larger rectangle lists or general calls, use numpy path.
    # Note: numpy path and plain loop path produce identical output.
    return damage_tiles_numpy(w, h, s, rects)
