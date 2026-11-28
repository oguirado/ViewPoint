# SPDX-License-Identifier: AGPL-3.0-only
"""Held-out camera rings for the visibility census.

Each ring samples six azimuths (seeds decide the phase offset, elevation
band assignment and framing fill).  Distances are resolved at measurement
time from the posed geometry, so the same ring frames any asset.
"""
import hashlib

import numpy as np

from .common import digest_obj

SCHEMA = "rp4.pose-oracle.cameras.v1"
BODY_BANDS = [-8.0, 8.0, 22.0, 38.0, 58.0, 76.0]
CHAIN_BANDS = [-24.0, -6.0, 10.0, 26.0, 46.0, 68.0]
CHAIN_NAMES = ["torso", "left_arm", "right_arm", "left_leg", "right_leg"]


def _rng(seed):
    return np.random.default_rng(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16))


def _ring(rng, bands, fill_range):
    offset = float(rng.uniform(0.0, 60.0))
    order = rng.permutation(len(bands))
    views = []
    for k in range(len(bands)):
        views.append({"az_deg": round((offset + 60.0 * k + float(rng.uniform(-7.0, 7.0))) % 360.0, 3),
                      "el_deg": round(float(np.clip(bands[order[k]] + rng.uniform(-6.0, 6.0), -80.0, 80.0)), 3),
                      "fill": round(float(rng.uniform(*fill_range)), 4)})
    return views


def generate(seed, resolution=(960, 540), lens_mm=50.0, sensor_width_mm=36.0, chains=CHAIN_NAMES):
    rng = _rng(seed)
    groups = [{"name": "body", "target": "body", "views": _ring(rng, BODY_BANDS, (0.90, 0.96))}]
    for chain in chains:
        groups.append({"name": chain, "target": f"chain:{chain}", "views": _ring(rng, CHAIN_BANDS, (0.82, 0.92))})
    doc = {"schema": SCHEMA, "seed_sha256": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
           "seed_note": "digest of the discarded seed; the seed itself is not retained",
           "lens_mm": lens_mm, "sensor_width_mm": sensor_width_mm,
           "resolution": list(resolution), "groups": groups}
    return doc


def digest(camera_doc):
    return digest_obj({k: camera_doc[k] for k in ("schema", "lens_mm", "sensor_width_mm", "resolution", "groups")})


def distance_for(camera_doc, radius, fill):
    """Camera distance so a sphere of ``radius`` fills ``fill`` of the short frame axis."""
    width, height = camera_doc["resolution"]
    half = camera_doc["sensor_width_mm"] * min(1.0, height / width) / 2.0
    return max(0.25, radius / (fill * half / camera_doc["lens_mm"]))
