"""LANG-X X1 fluency seed py_w3 baseline kernels implementation."""

import math
import struct
import numpy as np

# Error Codes
E_DIM = "E_DIM"
E_STRIDE = "E_STRIDE"
E_SHORT = "E_SHORT"
E_RECT = "E_RECT"
E_GRID = "E_GRID"


class X1Error(Exception):
    """Exception raised for errors in LANG-X X1 kernels."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def repack_bgrx_to_rgb(src, w: int, h: int, src_stride: int, dst, dst_stride: int):
    """
    Kernel K1: BGRX32 to RGB24 repack with stride.

    Inputs:
        src: bytes-like object containing source BGRX32 pixels.
        w: image width (0 to 16384).
        h: image height (0 to 16384).
        src_stride: source bytes per row (>= 4*w).
        dst: writable bytearray or numpy uint8 array for output RGB24.
        dst_stride: destination bytes per row (>= 3*w).

    Errors raised (X1Error):
        E_DIM: w or h outside 0..16384.
        E_STRIDE: src_stride < 4*w or dst_stride < 3*w.
        E_SHORT: src or dst buffer too short.
    """
    # Check E_DIM before any write
    if not (isinstance(w, int) and isinstance(h, int) and 0 <= w <= 16384 and 0 <= h <= 16384):
        raise X1Error(E_DIM, f"Width ({w}) or height ({h}) out of range [0, 16384]")

    # Check E_STRIDE before any write
    if src_stride < 4 * w or dst_stride < 3 * w:
        raise X1Error(E_STRIDE, f"Invalid stride: src_stride={src_stride} (< {4*w}) or dst_stride={dst_stride} (< {3*w})")

    # Required min sizes
    if h == 0 or w == 0:
        min_src_len = 0
        min_dst_len = 0
    else:
        min_src_len = (h - 1) * src_stride + 4 * w
        min_dst_len = (h - 1) * dst_stride + 3 * w

    src_len = len(src)
    dst_len = len(dst)

    if src_len < min_src_len or dst_len < min_dst_len:
        raise X1Error(E_SHORT, f"Buffer too short: src len={src_len} (min {min_src_len}), dst len={dst_len} (min {min_dst_len})")

    if w == 0 or h == 0:
        return

    # Convert src and dst to numpy array views
    src_arr = np.frombuffer(src, dtype=np.uint8)
    if isinstance(dst, np.ndarray):
        dst_arr = dst
    else:
        dst_arr = np.frombuffer(dst, dtype=np.uint8)

    src_view = np.lib.stride_tricks.as_strided(
        src_arr,
        shape=(h, w, 4),
        strides=(src_stride, 4, 1)
    )

    dst_view = np.lib.stride_tricks.as_strided(
        dst_arr,
        shape=(h, w, 3),
        strides=(dst_stride, 3, 1)
    )

    # Copy R, G, B channels:
    # src holds B, G, R, X at indices 0, 1, 2, 3
    # dst receives R, G, B at indices 0, 1, 2
    # So dst channel 0, 1, 2 comes from src channel 2, 1, 0
    dst_view[:, :, :] = src_view[:, :, [2, 1, 0]]


def _damage_tiles_plain(w: int, h: int, s: int, rects):
    gw = math.ceil(w / s)
    gh = math.ceil(h / s)
    total_tiles = gw * gh
    num_words = math.ceil(total_tiles / 64)
    words = [0] * num_words

    for rx, ry, rw, rh in rects:
        x0 = max(rx, 0)
        y0 = max(ry, 0)
        x1 = min(rx + rw, w)
        y1 = min(ry + rh, h)

        if x1 <= x0 or y1 <= y0:
            continue

        tx0 = x0 // s
        tx1 = math.ceil(x1 / s)
        ty0 = y0 // s
        ty1 = math.ceil(y1 / s)

        for ty in range(ty0, ty1):
            row_offset = ty * gw
            for tx in range(tx0, tx1):
                t = row_offset + tx
                word_idx = t // 64
                bit_idx = t % 64
                words[word_idx] |= (1 << bit_idx)

    popcount = sum(bin(word).count("1") for word in words)
    return words, popcount


def _damage_tiles_numpy(w: int, h: int, s: int, rects):
    gw = math.ceil(w / s)
    gh = math.ceil(h / s)
    total_tiles = gw * gh
    num_words = math.ceil(total_tiles / 64)

    if not rects:
        return [0] * num_words, 0

    arr = np.array(rects, dtype=np.int64)
    rx, ry, rw, rh = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]

    x0 = np.maximum(rx, 0)
    y0 = np.maximum(ry, 0)
    x1 = np.minimum(rx + rw, w)
    y1 = np.minimum(ry + rh, h)

    valid = (x1 > x0) & (y1 > y0)
    if not np.any(valid):
        return [0] * num_words, 0

    x0 = x0[valid]
    y0 = y0[valid]
    x1 = x1[valid]
    y1 = y1[valid]

    tx0 = x0 // s
    tx1 = (x1 + s - 1) // s
    ty0 = y0 // s
    ty1 = (y1 + s - 1) // s

    tiles_list = []
    for r_tx0, r_tx1, r_ty0, r_ty1 in zip(tx0, tx1, ty0, ty1):
        tx_range = np.arange(r_tx0, r_tx1, dtype=np.int64)
        ty_range = np.arange(r_ty0, r_ty1, dtype=np.int64)
        t_grid = (ty_range[:, None] * gw + tx_range[None, :]).ravel()
        tiles_list.append(t_grid)

    unique_tiles = np.unique(np.concatenate(tiles_list))
    word_indices = unique_tiles // 64
    bit_indices = (unique_tiles % 64).astype(np.uint64)

    words = np.zeros(num_words, dtype=np.uint64)
    np.bitwise_or.at(words, word_indices, np.uint64(1) << bit_indices)

    word_list = [int(w_val) for w_val in words]
    popcount = sum(bin(w_val).count("1") for w_val in word_list)
    return word_list, popcount


def damage_tiles(w: int, h: int, s: int, rects, use_numpy: bool = True):
    """
    Kernel K2: Damage rectangles to a tile bitset for grid s.

    Inputs:
        w: screen width (1 to 16384).
        h: screen height (1 to 16384).
        s: tile size (power of two from 1 to 256).
        rects: list of tuples (x, y, w, h) as signed 32-bit integers.
        use_numpy: if True, uses vectorized numpy path when beneficial.

    Errors raised (X1Error):
        E_DIM: w or h outside 1..16384.
        E_GRID: s not a power of two in range 1..256.
        E_RECT: a rectangle with w < 0 or h < 0.
    """
    if not (isinstance(w, int) and isinstance(h, int) and 1 <= w <= 16384 and 1 <= h <= 16384):
        raise X1Error(E_DIM, f"Width ({w}) or height ({h}) out of range [1, 16384]")

    if not (isinstance(s, int) and 1 <= s <= 256 and (s & (s - 1)) == 0):
        raise X1Error(E_GRID, f"Tile size s ({s}) not a power of two in range [1, 256]")

    for r in rects:
        rx, ry, rw, rh = r
        if rw < 0 or rh < 0:
            raise X1Error(E_RECT, f"Rectangle width or height negative: w={rw}, h={rh}")

    if use_numpy and len(rects) > 10:
        return _damage_tiles_numpy(w, h, s, rects)
    else:
        return _damage_tiles_plain(w, h, s, rects)


def splitmix64(seed: int, length: int) -> bytes:
    """
    SplitMix64 generator.
    Returns `length` bytes generated by little-endian serialization of 64-bit outputs.
    """
    state = seed & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while len(out) < length:
        state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        z = (z ^ (z >> 31)) & 0xFFFFFFFFFFFFFFFF
        out.extend(struct.pack("<Q", z))
    return bytes(out[:length])


def fnv1a64(data: bytes) -> int:
    """
    FNV-1a 64 hash algorithm.
    """
    h = 0xCBF29CE484222325
    prime = 0x100000001B3
    for b in data:
        h = (h ^ b) & 0xFFFFFFFFFFFFFFFF
        h = (h * prime) & 0xFFFFFFFFFFFFFFFF
    return h
