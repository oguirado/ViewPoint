"""Screen fingerprint variant using dual difference hashing (dHash) on 32x18 luma grid.

Algorithm:
1. Convert RGB uint8 frame (H, W, 3) to integer Luma (BT.601 formula).
2. Downscale luma to a fixed 32 by 18 grid using integer block means with boundary overlap smoothing.
3. Compute horizontal difference hash: compare adjacent horizontal block means (32 - 1 = 31 diffs per row x 18 rows = 558 bits).
4. Compute vertical difference hash: compare adjacent vertical block means (18 - 1 = 17 diffs per column x 32 columns = 544 bits).
5. Quantize global mean luma into a 64-bit thermometer code appended to the horizontal hash to differentiate uniform or brightness-shifted frames (e.g., solid black vs. solid white).
6. The fingerprint is the tuple (h_hash, v_hash) of Python ints.
7. Distance between two fingerprints is the sum of popcounts of XOR:
   popcount(h_hash1 ^ h_hash2) + popcount(v_hash1 ^ v_hash2).
8. SF_THRESHOLD is set to 30.

Justification for SF_THRESHOLD = 30:
- Minor UI changes (caret blinking, single-character typing, 1-code-value brightness shifts, small scrolls, 16x16 text tile changes) alter only a small local neighborhood of block means, resulting in Hamming distances typically between 0 and 20 bits (well below 30).
- Major UI transitions, layout changes, or solid black vs. solid white frames flip many dHash bits or thermometer bits, resulting in Hamming distances typically exceeding 50 bits (well above 30).
- Thus, SF_THRESHOLD = 30 effectively separates same-screen frames from different-screen frames.
"""

import numpy as np

SF_THRESHOLD = 30


def screen_fingerprint(frame: np.ndarray) -> tuple[int, int]:
    """Compute 32x18 dHash fingerprint for an RGB frame.

    Args:
        frame: Read-only RAM uint8 array of shape (H, W, 3).

    Returns:
        tuple of (h_hash, v_hash) as Python ints.
    """
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError("frame must be a uint8 numpy array of shape (H, W, 3)")

    H, W, _ = frame.shape

    # 1. Convert RGB to luma (integer BT.601)
    r = frame[:, :, 0].astype(np.uint32)
    g = frame[:, :, 1].astype(np.uint32)
    b = frame[:, :, 2].astype(np.uint32)
    luma = (19595 * r + 38470 * g + 7471 * b) >> 16

    # 2. Downscale luma to 32x18 grid via integer block means with boundary overlap
    GH, GW = 18, 32
    y_starts = (np.arange(GH) * H) // GH
    y_ends = np.maximum(y_starts + 1, ((np.arange(1, GH + 1) * H) // GH))
    x_starts = (np.arange(GW) * W) // GW
    x_ends = np.maximum(x_starts + 1, ((np.arange(1, GW + 1) * W) // GW))

    pad_h = max(1, H // GH)
    pad_w = max(1, W // GW)

    grid = np.zeros((GH, GW), dtype=np.int32)
    for r_idx in range(GH):
        y0 = max(0, y_starts[r_idx] - pad_h // 2)
        y1 = min(H, y_ends[r_idx] + pad_h // 2)
        for c_idx in range(GW):
            x0 = max(0, x_starts[c_idx] - pad_w // 2)
            x1 = min(W, x_ends[c_idx] + pad_w // 2)
            grid[r_idx, c_idx] = int(luma[y0:y1, x0:x1].mean())

    # 3. Horizontal difference hash (18 rows, 31 diffs per row = 558 bits)
    h_diff = (grid[:, 1:] >= grid[:, :-1]).reshape(-1)
    v_diff = (grid[1:, :] >= grid[:-1, :]).reshape(-1)

    h_int = int.from_bytes(np.packbits(h_diff).tobytes(), "big")
    v_int = int.from_bytes(np.packbits(v_diff).tobytes(), "big")

    # Global mean luma 64-bit thermometer code
    mean_luma = int(grid.mean())
    ones_count = (mean_luma * 64) // 256
    luma_code = (1 << ones_count) - 1 if ones_count > 0 else 0
    h_int = (h_int << 64) | luma_code

    return (h_int, v_int)


def fingerprint_distance(fp1: tuple[int, int], fp2: tuple[int, int]) -> int:
    """Compute Hamming distance between two fingerprints as sum of XOR popcounts."""
    h1, v1 = fp1
    h2, v2 = fp2
    return (h1 ^ h2).bit_count() + (v1 ^ v2).bit_count()


def same_screen(fp1: tuple[int, int], fp2: tuple[int, int]) -> bool:
    """Return True if fingerprint distance is at most SF_THRESHOLD."""
    return bool(fingerprint_distance(fp1, fp2) <= SF_THRESHOLD)
