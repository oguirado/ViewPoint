import math
import numpy as np


class X1Error(Exception):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def splitmix64(seed, count):
    """SplitMix64 pseudo-random generator producing unsigned 64-bit integers."""
    state = seed & 0xFFFFFFFFFFFFFFFF
    outputs = []
    for _ in range(count):
        state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        z = (z ^ (z >> 31)) & 0xFFFFFFFFFFFFFFFF
        outputs.append(z)
    return outputs


def fnv1a_64(data):
    """FNV-1a 64-bit hash over byte sequence data."""
    h = 0xCBF29CE484222325
    prime = 0x100000001B3
    for b in data:
        h = ((h ^ b) * prime) & 0xFFFFFFFFFFFFFFFF
    return h


def repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride):
    """
    Kernel K1: BGRX32 to RGB24 repack with stride using numpy strided views.
    """
    if not (0 <= w <= 16384 and 0 <= h <= 16384):
        raise X1Error("E_DIM")

    if src_stride < 4 * w or dst_stride < 3 * w:
        raise X1Error("E_STRIDE")

    if w == 0 or h == 0:
        src_min_len = 0
        dst_min_len = 0
    else:
        src_min_len = (h - 1) * src_stride + 4 * w
        dst_min_len = (h - 1) * dst_stride + 3 * w

    if len(src) < src_min_len or len(dst) < dst_min_len:
        raise X1Error("E_SHORT")

    if w == 0 or h == 0:
        return

    # Prepare numpy array views
    src_arr = np.frombuffer(src, dtype=np.uint8)
    if isinstance(dst, np.ndarray):
        if dst.dtype != np.uint8:
            raise X1Error("E_SHORT")
        dst_arr = dst
    else:
        dst_arr = np.frombuffer(dst, dtype=np.uint8)

    src_view = np.lib.stride_tricks.as_strided(
        src_arr, shape=(h, w, 4), strides=(src_stride, 4, 1)
    )
    dst_view = np.lib.stride_tricks.as_strided(
        dst_arr, shape=(h, w, 3), strides=(dst_stride, 3, 1)
    )

    # Permute BGRX [0, 1, 2] -> RGB [2, 1, 0]
    dst_view[...] = src_view[:, :, [2, 1, 0]]


def _damage_tiles_plain(w, h, s, rects, gw, gh):
    n_tiles = gw * gh
    n_words = (n_tiles + 63) // 64
    words = [0] * n_words

    for rect in rects:
        rx, ry, rw, rh = rect
        if rw < 0 or rh < 0:
            raise X1Error("E_RECT")

        x0 = max(rx, 0)
        y0 = max(ry, 0)
        x1 = min(rx + rw, w)
        y1 = min(ry + rh, h)

        if x1 <= x0 or y1 <= y0:
            continue

        tx0 = x0 // s
        ty0 = y0 // s
        tx1 = (x1 + s - 1) // s
        ty1 = (y1 + s - 1) // s

        for ty in range(ty0, ty1):
            for tx in range(tx0, tx1):
                t = ty * gw + tx
                w_idx = t // 64
                b_idx = t % 64
                words[w_idx] |= (1 << b_idx)

    popcount = sum(bin(word).count('1') for word in words)
    return words, popcount


def _damage_tiles_numpy(w, h, s, rects, gw, gh):
    n_tiles = gw * gh
    n_words = (n_tiles + 63) // 64

    rects_arr = np.array(rects, dtype=np.int64)
    if rects_arr.shape[0] == 0:
        return [0] * n_words, 0

    x = rects_arr[:, 0]
    y = rects_arr[:, 1]
    rw = rects_arr[:, 2]
    rh = rects_arr[:, 3]

    if np.any(rw < 0) or np.any(rh < 0):
        raise X1Error("E_RECT")

    x0 = np.clip(x, 0, w)
    y0 = np.clip(y, 0, h)
    x1 = np.clip(x + rw, 0, w)
    y1 = np.clip(y + rh, 0, h)

    valid = (x1 > x0) & (y1 > y0)
    if not np.any(valid):
        return [0] * n_words, 0

    x0, y0, x1, y1 = x0[valid], y0[valid], x1[valid], y1[valid]

    tx0 = x0 // s
    ty0 = y0 // s
    tx1 = (x1 + s - 1) // s
    ty1 = (y1 + s - 1) // s

    grid = np.zeros((gh, gw), dtype=bool)

    for i in range(len(tx0)):
        grid[ty0[i]:ty1[i], tx0[i]:tx1[i]] = True

    flat = np.pad(grid.reshape(-1), (0, n_words * 64 - n_tiles))
    bits_64 = flat.reshape(-1, 64)
    powers = (1 << np.arange(64, dtype=np.uint64))
    words = bits_64.dot(powers).tolist()

    popcount = int(np.sum(flat))
    return words, popcount


def damage_tiles(w, h, s, rects, use_numpy=True):
    """
    Kernel K2: damage rectangles to a tile bitset for grid s.
    """
    if not (1 <= w <= 16384 and 1 <= h <= 16384):
        raise X1Error("E_DIM")

    if not (isinstance(s, int) and 1 <= s <= 256 and (s & (s - 1)) == 0):
        raise X1Error("E_GRID")

    gw = (w + s - 1) // s
    gh = (h + s - 1) // s

    if use_numpy and len(rects) > 10:
        return _damage_tiles_numpy(w, h, s, rects, gw, gh)
    else:
        return _damage_tiles_plain(w, h, s, rects, gw, gh)
