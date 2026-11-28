# SPDX-License-Identifier: AGPL-3.0-only
"""Conservative numerical bound for all within-part distances."""
import numpy as np

def distance_bounds(rest, pose, parts):
    """Bound every vertex-pair distance change by twice the max rigid-fit residual."""
    bounds = {}
    for joint, index in parts.items():
        a, b = rest[index], pose[index]
        a, b = a - a.mean(axis=0), b - b.mean(axis=0)
        u, _, vh = np.linalg.svd(a.T @ b)
        flip = np.diag([1., 1., np.linalg.det(u @ vh)])
        bounds[joint] = float(2 * np.linalg.norm(a @ u @ flip @ vh - b, axis=1).max())
    return bounds
