"""Research prototype for problem P5: Hoeffding's inequality noise gate.

Renders noise false alarms with theoretical bounds using Hoeffding's inequality.
Not wired into production.

Method from screened paper:
For b independent samples in [0, 1] with mean mu, P of the sample mean deviating
from mu by more than eps is at most 2 * exp(-b * eps^2 / 4), so the false alarm
rate is at most alpha when eps >= 2 * sqrt(ln(2 / alpha) / b).
"""

import numpy as np


def hoeffding_eps(alpha, b):
    """Compute Hoeffding deviation threshold epsilon for false alarm rate alpha and sample count b.

    eps = 2 * sqrt(ln(2 / alpha) / b)
    """
    if alpha <= 0 or alpha >= 1:
        raise ValueError("alpha must be in (0, 1)")
    b_arr = np.asarray(b, dtype=float)
    if np.any(b_arr <= 0):
        raise ValueError("sample count b must be positive")
    eps = 2.0 * np.sqrt(np.log(2.0 / float(alpha)) / b_arr)
    if np.isscalar(b) or getattr(b, "ndim", 0) == 0:
        return float(eps)
    return eps


def noise_floor_q4(samples):
    """Estimate noise floor parameters (mu, R) from static zero-vector SAD samples.

    mu: sample mean of static SAD samples.
    R: p99.9 ceiling as the rank-sorted index (999 * (n - 1)) // 1000.
    """
    s = np.sort(np.asarray(samples).ravel())
    n = len(s)
    if n == 0:
        return 0.0, 0.0
    idx = (999 * (n - 1)) // 1000
    R = float(s[idx])
    mu = float(np.mean(s))
    return mu, R


def tile_is_noise(residual_sums, pixel_counts, mu, R, alpha, use_integer=False):
    """Determine whether tiles are noise using Hoeffding's inequality threshold.

    Returns a boolean mask (or scalar bool) where True indicates the tile is classed as noise.

    Condition:
    (per_pixel_mean / R) <= (mu / R) + eps
    where b = pixel_counts and eps = hoeffding_eps(alpha, b).
    """
    res = np.asarray(residual_sums)
    b = np.asarray(pixel_counts)
    is_scalar_input = np.isscalar(residual_sums) and np.isscalar(pixel_counts)

    eps = hoeffding_eps(alpha, b)

    if use_integer:
        # Integer path: residual_sums <= floor(pixel_counts * mu + pixel_counts * R * eps)
        thresh = np.floor(b * mu + b * R * eps + 1e-12)
        mask = res <= thresh
    else:
        # Float path: (residual_sums / pixel_counts) / R <= (mu / R) + eps
        per_px = res / b
        if R == 0:
            mask = res <= 0
        else:
            mask = (per_px / R) <= ((mu / R) + eps)

    if is_scalar_input:
        return bool(mask)
    return mask
