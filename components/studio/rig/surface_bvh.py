# SPDX-License-Identifier: AGPL-3.0-only
"""Return the closest source triangle and point for every studio vertex."""

from pathlib import Path
import sys

import bpy

from mathutils.bvhtree import BVHTree
import numpy as np


if not bpy.app.background:
    raise RuntimeError("blender_background_required")

work = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
with np.load(work / "surface.npz") as source:
    xyz, faces, queries = (source[key] for key in ("xyz", "faces", "queries"))
tree = BVHTree.FromPolygons(xyz.tolist(), faces.tolist(), all_triangles=True)
triangles = np.empty(len(queries), dtype=np.int32)
hits = np.empty_like(queries, dtype=np.float64)
for i, query in enumerate(queries):
    point, _, triangle, distance = tree.find_nearest(query)
    if triangle is None or not np.isfinite(distance):
        raise RuntimeError(f"no_closest_source_triangle:{i}")
    hits[i] = point
    triangles[i] = triangle
np.savez(work / "closest.npz", triangles=triangles, hits=hits)
