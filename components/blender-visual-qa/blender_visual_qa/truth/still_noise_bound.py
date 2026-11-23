"""Compute the Hoeffding still-noise bounds on truth-bench samples."""
import math

import numpy as np

from . import calibrate


ALPHAS = (0.01, 0.001, 0.0001)
B_TILES = (64, 256)


def compute_bounds(samples_q4, fit_rows, rows):
    t0_still = calibrate.t0_still(samples_q4)
    t0_fp = calibrate.t0_fp(fit_rows)
    fit_info = calibrate.fit_t0(fit_rows, samples_q4)
    contaminated = fit_info["contaminated"]

    # Frozen t0_q4 of rows
    t0_q4_vals = sorted({r["t0_q4"] for r in rows if "t0_q4" in r})
    if len(t0_q4_vals) == 1:
        frozen_t0_q4 = t0_q4_vals[0]
    elif len(t0_q4_vals) > 1:
        frozen_t0_q4 = t0_q4_vals
    else:
        frozen_t0_q4 = fit_info["T0_Q4"]

    xs = sorted(int(x) for x in samples_q4)
    q = xs[999 * (len(xs) - 1) // 1000]
    R = float(q)
    mu = float(np.mean(xs))

    bounds = []
    for alpha in ALPHAS:
        for b in B_TILES:
            eps = 2.0 * R * math.sqrt(math.log(2.0 / alpha) / float(b))
            cand = mu + eps
            bounds.append({
                "alpha": alpha,
                "b": b,
                "eps": eps,
                "candidate_threshold_q4": cand,
            })

    report = {
        "schema": "bvqa.still-noise-bound/v1",
        "T0_still": t0_still,
        "T0_fp": t0_fp,
        "frozen_t0_q4": frozen_t0_q4,
        "contaminated": contaminated,
        "mu": mu,
        "R": R,
        "bounds": bounds,
    }
    return report
