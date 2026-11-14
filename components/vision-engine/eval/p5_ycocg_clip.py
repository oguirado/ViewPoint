"""eval/p5_ycocg_clip.py - Research prototype for problem P5: integer statistical history clipping.

Method:
  Lossless lifting YCoCg transformation:
    Co = R - B
    t = B + (Co >> 1)
    Cg = G - t
    Y = t + (Cg >> 1)
  Exact inverse:
    t = Y - (Cg >> 1)
    G = Cg + t
    B = t - (Co >> 1)
    R = Co + B

  3x3 Neighbourhood statistics:
    m1 = sum of 9 samples
    m2 = sum of squares of 9 samples
    81 * s^2 = 9 * m2 - m1^2
    9s = isqrt(max(0, 9 * m2 - m1^2))
    c = round(m1 / 9)
    e = floor(9s * gamma_q8 / 2304)

  History Clipping:
    d = h - c
    abs_d = |d|
    t = min_{k: abs_d_k > 0} (e_k / abs_d_k) clamped to <= 1.0
    return c + trunc(d * t)
"""

import numpy as np


def rgb_to_ycocg(rgb):
    """Lossless YCoCg lifting transform from RGB (uint8 or int) to int32 (Y, Co, Cg)."""
    arr = np.asarray(rgb, dtype=np.int32)
    r = arr[..., 0]
    g = arr[..., 1]
    b = arr[..., 2]
    co = r - b
    t = b + (co >> 1)
    cg = g - t
    y = t + (cg >> 1)
    return np.stack([y, co, cg], axis=-1)


def ycocg_to_rgb(ycocg):
    """Exact inverse transform from YCoCg (Y, Co, Cg) back to uint8 RGB."""
    arr = np.asarray(ycocg, dtype=np.int32)
    y = arr[..., 0]
    co = arr[..., 1]
    cg = arr[..., 2]
    t = y - (cg >> 1)
    g = cg + t
    b = t - (co >> 1)
    r = co + b
    rgb = np.stack([r, g, b], axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def compute_box_3x3(frame, gamma_q8=320):
    """Compute 3x3 neighbourhood box center `c` and half-extent `e` for a 2D or 3D frame.

    Args:
        frame (np.ndarray): Shape (H, W) or (H, W, C) int or uint8.
        gamma_q8 (int): Q8 fixed-point scale factor for half-extent (default 320 for gamma=1.25).

    Returns:
        c (np.ndarray): Center array, int64, same shape as frame.
        e (np.ndarray): Half-extent array, int64, same shape as frame.
    """
    arr = np.asarray(frame, dtype=np.int64)
    if arr.ndim == 2:
        H, W = arr.shape
        padded = np.pad(arr, ((1, 1), (1, 1)), mode="edge")
        slices = [
            padded[dy : dy + H, dx : dx + W]
            for dy in range(3)
            for dx in range(3)
        ]
        m1 = sum(slices)
        m2 = sum(s * s for s in slices)
    elif arr.ndim == 3:
        H, W, C = arr.shape
        padded = np.pad(arr, ((1, 1), (1, 1), (0, 0)), mode="edge")
        slices = [
            padded[dy : dy + H, dx : dx + W, :]
            for dy in range(3)
            for dx in range(3)
        ]
        m1 = sum(slices)
        m2 = sum(s * s for s in slices)
    else:
        raise ValueError("frame must be a 2D or 3D array")

    arg = np.maximum(0, 9 * m2 - m1 * m1)
    s9 = np.floor(np.sqrt(arg)).astype(np.int64)
    c = np.round(m1 / 9.0).astype(np.int64)
    e = (s9 * gamma_q8) // 2304
    return c, e


def clip_history(h, c, e):
    """Vectorised clipping of history values h into box (c, e) along ray to c.

    Args:
        h (np.ndarray or scalar): History values array or scalar.
        c (np.ndarray or scalar): Center array or scalar.
        e (np.ndarray or scalar): Half-extent array or scalar.

    Returns:
        clipped (np.ndarray or scalar): Clipped history values, int64 dtype, same shape as h.
    """
    is_scalar = np.isscalar(h) and np.isscalar(c) and np.isscalar(e)
    h_arr = np.asarray(h, dtype=np.float64)
    c_arr = np.asarray(c, dtype=np.float64)
    e_arr = np.asarray(e, dtype=np.float64)

    d = h_arr - c_arr
    abs_d = np.abs(d)

    if h_arr.ndim == 3:
        ratios = np.where(abs_d > 0, e_arr / np.maximum(abs_d, 1e-12), 1e18)
        t = np.minimum(1.0, np.min(ratios, axis=-1, keepdims=True))
    elif h_arr.ndim == 2:
        ratios = np.where(abs_d > 0, e_arr / np.maximum(abs_d, 1e-12), 1e18)
        t = np.minimum(1.0, ratios)
    else:
        # 1D array or scalar
        nonzero = abs_d > 0
        if not np.any(nonzero):
            t = 1.0
        else:
            ratios = np.where(nonzero, e_arr / np.maximum(abs_d, 1e-12), 1e18)
            t = np.minimum(1.0, np.min(ratios))

    clipped = c_arr + np.trunc(d * t)
    res = np.round(clipped).astype(np.int64)
    if is_scalar:
        return int(res.item())
    return res


def clip_frame_ycocg(cur_rgb, prev_rgb, gamma_q8=320):
    """Clip prev_rgb into cur_rgb 3x3 neighbourhood box in YCoCg space, returning clipped RGB uint8 frame."""
    cur_ycocg = rgb_to_ycocg(cur_rgb)
    prev_ycocg = rgb_to_ycocg(prev_rgb)
    c, e = compute_box_3x3(cur_ycocg, gamma_q8=gamma_q8)
    clipped_ycocg = clip_history(prev_ycocg, c, e)
    return ycocg_to_rgb(clipped_ycocg)


def clip_frame_luma(cur_luma, prev_luma, gamma_q8=320):
    """Clip prev_luma into cur_luma 3x3 neighbourhood box in Luma space, returning clipped luma array."""
    c, e = compute_box_3x3(cur_luma, gamma_q8=gamma_q8)
    clipped_luma = clip_history(prev_luma, c, e)
    return np.clip(clipped_luma, 0, 255).astype(np.uint8)


def clipped_residual(cur, prev, tile_size=8, gamma_q8=320):
    """Compute per-tile SAD residual of cur against prev after clipping prev into cur neighbourhood box.

    Args:
        cur (np.ndarray): Current frame, shape (H, W, 3) RGB or (H, W) Luma.
        prev (np.ndarray): Previous frame, same shape as cur.
        tile_size (int): Tile size in pixels (default 8).
        gamma_q8 (int): Q8 fixed-point scale factor (default 320).

    Returns:
        np.ndarray: 2D array of shape (H // tile_size, W // tile_size) containing per-tile SAD residuals.
    """
    cur_arr = np.asarray(cur)
    prev_arr = np.asarray(prev)

    if cur_arr.ndim == 3 and cur_arr.shape[-1] == 3:
        clipped_prev = clip_frame_ycocg(cur_arr, prev_arr, gamma_q8=gamma_q8)
        diff = np.abs(cur_arr.astype(np.int64) - clipped_prev.astype(np.int64))
        diff = np.sum(diff, axis=-1)
    elif cur_arr.ndim == 2:
        clipped_prev = clip_frame_luma(cur_arr, prev_arr, gamma_q8=gamma_q8)
        diff = np.abs(cur_arr.astype(np.int64) - clipped_prev.astype(np.int64))
    else:
        raise ValueError("cur and prev must be (H, W, 3) RGB or (H, W) Luma")

    H, W = diff.shape
    gh = H // tile_size
    gw = W // tile_size
    res = np.zeros((gh, gw), dtype=np.int64)

    for ty in range(gh):
        for tx in range(gw):
            tile_diff = diff[ty * tile_size : (ty + 1) * tile_size, tx * tile_size : (tx + 1) * tile_size]
            res[ty, tx] = np.sum(tile_diff)

    return res


def raw_residual(cur, prev, tile_size=8):
    """Compute raw per-tile SAD residual of cur against unclipped prev."""
    cur_arr = np.asarray(cur, dtype=np.int64)
    prev_arr = np.asarray(prev, dtype=np.int64)

    diff = np.abs(cur_arr - prev_arr)
    if diff.ndim == 3:
        diff = np.sum(diff, axis=-1)

    H, W = diff.shape
    gh = H // tile_size
    gw = W // tile_size
    res = np.zeros((gh, gw), dtype=np.int64)

    for ty in range(gh):
        for tx in range(gw):
            tile_diff = diff[ty * tile_size : (ty + 1) * tile_size, tx * tile_size : (tx + 1) * tile_size]
            res[ty, tx] = np.sum(tile_diff)

    return res
