"""LANG-X experiment X1 fluency seed py_w5 implementation."""

import math
import numpy as np

E_DIM = "E_DIM"
E_STRIDE = "E_STRIDE"
E_SHORT = "E_SHORT"
E_RECT = "E_RECT"
E_GRID = "E_GRID"


class X1Error(Exception):
    """Exception raised for errors in X1 kernels."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride):
    """Kernel K1: BGRX32 to RGB24 repack with stride using numpy strided views."""
    if not (0 <= w <= 16384 and 0 <= h <= 16384):
        raise X1Error(E_DIM, "W or H outside range 0 to 16384")

    if src_stride < 4 * w or dst_stride < 3 * w:
        raise X1Error(E_STRIDE, "src_stride < 4W or dst_stride < 3W")

    if w == 0 or h == 0:
        return

    min_src_len = (h - 1) * src_stride + 4 * w
    min_dst_len = (h - 1) * dst_stride + 3 * w

    if len(src) < min_src_len or len(dst) < min_dst_len:
        raise X1Error(E_SHORT, "Source or destination buffer too short")

    src_arr = np.frombuffer(src, dtype=np.uint8)
    if isinstance(dst, (bytearray, memoryview)):
        dst_arr = np.frombuffer(dst, dtype=np.uint8)
    elif isinstance(dst, np.ndarray):
        dst_arr = dst
    else:
        dst_arr = np.frombuffer(dst, dtype=np.uint8)

    src_view = np.lib.stride_tricks.as_strided(
        src_arr, shape=(h, w, 4), strides=(src_stride, 4, 1)
    )
    dst_view = np.lib.stride_tricks.as_strided(
        dst_arr, shape=(h, w, 3), strides=(dst_stride, 3, 1)
    )

    dst_view[:, :, 0] = src_view[:, :, 2]  # R
    dst_view[:, :, 1] = src_view[:, :, 1]  # G
    dst_view[:, :, 2] = src_view[:, :, 0]  # B


def _validate_damage_inputs(w, h, s, rects):
    if not (1 <= w <= 16384 and 1 <= h <= 16384):
        raise X1Error(E_DIM, "W or H outside range 1 to 16384")

    if s not in (1, 2, 4, 8, 16, 32, 64, 128, 256):
        raise X1Error(E_GRID, "s not a power of two in range 1 to 256")

    for rect in rects:
        rx, ry, rw, rh = rect
        if rw < 0 or rh < 0:
            raise X1Error(E_RECT, "Rectangle width or height negative")


def damage_tiles_plain_loop(w, h, s, rects):
    """Kernel K2 plain-loop implementation."""
    _validate_damage_inputs(w, h, s, rects)

    gw = (w + s - 1) // s
    gh = (h + s - 1) // s
    total_tiles = gw * gh

    grid = np.zeros((gh, gw), dtype=bool)

    for x, y, rw, rh in rects:
        x0 = max(x, 0)
        y0 = max(y, 0)
        x1 = min(x + rw, w)
        y1 = min(y + rh, h)

        if x1 <= x0 or y1 <= y0:
            continue

        tx0 = x0 // s
        ty0 = y0 // s
        tx1 = (x1 + s - 1) // s
        ty1 = (y1 + s - 1) // s

        for ty in range(ty0, ty1):
            for tx in range(tx0, tx1):
                grid[ty, tx] = True

    tile_bits = grid.ravel()
    num_words = (total_tiles + 63) // 64
    pad_len = num_words * 64 - len(tile_bits)
    padded = np.pad(tile_bits, (0, pad_len))
    packed_bytes = np.packbits(padded, bitorder="little")
    words = packed_bytes.view(np.uint64).tolist()
    popcount = int(np.count_nonzero(tile_bits))

    return words, popcount


def damage_tiles(w, h, s, rects, use_plain_loop=False):
    """Kernel K2: damage rectangles to tile bitset with vectorised numpy path."""
    if use_plain_loop:
        return damage_tiles_plain_loop(w, h, s, rects)

    _validate_damage_inputs(w, h, s, rects)

    gw = (w + s - 1) // s
    gh = (h + s - 1) // s
    total_tiles = gw * gh

    grid = np.zeros((gh, gw), dtype=bool)

    if len(rects) > 0:
        r_arr = np.array(rects, dtype=np.int64)
        rx = r_arr[:, 0]
        ry = r_arr[:, 1]
        rw = r_arr[:, 2]
        rh = r_arr[:, 3]

        x0 = np.maximum(rx, 0)
        y0 = np.maximum(ry, 0)
        x1 = np.minimum(rx + rw, w)
        y1 = np.minimum(ry + rh, h)

        valid = (x1 > x0) & (y1 > y0)

        x0 = x0[valid]
        y0 = y0[valid]
        x1 = x1[valid]
        y1 = y1[valid]

        tx0 = x0 // s
        ty0 = y0 // s
        tx1 = (x1 + s - 1) // s
        ty1 = (y1 + s - 1) // s

        for i in range(len(tx0)):
            grid[ty0[i] : ty1[i], tx0[i] : tx1[i]] = True

    tile_bits = grid.ravel()
    num_words = (total_tiles + 63) // 64
    pad_len = num_words * 64 - len(tile_bits)
    padded = np.pad(tile_bits, (0, pad_len))
    packed_bytes = np.packbits(padded, bitorder="little")
    words = packed_bytes.view(np.uint64).tolist()
    popcount = int(np.count_nonzero(tile_bits))

    return words, popcount


class SplitMix64:
    """SplitMix64 pseudo-random number generator."""

    def __init__(self, seed):
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def next_u64(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        z = (z ^ (z >> 31)) & 0xFFFFFFFFFFFFFFFF
        return z

    def fill_bytes(self, length):
        buf = bytearray()
        while len(buf) < length:
            u = self.next_u64()
            buf.extend(u.to_bytes(8, byteorder="little"))
        return bytes(buf[:length])


def fnv1a_64(data: bytes) -> int:
    """FNV-1a 64-bit hash over data bytes."""
    h = 0xCBF29CE484222325
    for b in data:
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h
