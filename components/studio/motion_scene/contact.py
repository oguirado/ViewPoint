# SPDX-License-Identifier: AGPL-3.0-only
"""Measure glove contact inside the finite, planar front of the switch cap."""

from collections import Counter

import numpy as np


def cap_face(points, triangles, axis):
    depths = points @ axis
    plane = float(depths.min())
    front = np.abs(depths - plane) <= 1e-6
    faces = triangles[np.all(front[triangles], axis=1)]
    edges = Counter()
    for a, b, c in faces:
        edges.update(tuple(sorted(edge)) for edge in ((a, b), (b, c), (c, a)))
    boundary = np.asarray([edge for edge, count in edges.items()
                           if count == 1 and np.linalg.norm(points[edge[0]] - points[edge[1]]) > 1e-8])
    if len(boundary) < 3:
        raise ValueError("evaluated_cap_face_boundary_missing")
    starts, ends = points[boundary[:, 0]], points[boundary[:, 1]]
    normals = np.cross(ends - starts, axis)
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    center = points[front].mean(axis=0)
    normals[np.einsum("ij,ij->i", center - starts, normals) < 0] *= -1
    offsets = np.einsum("ij,ij->i", starts, normals)
    if (points[front] @ normals.T - offsets < -1e-6).any():
        raise ValueError("evaluated_cap_face_not_convex")
    return plane, normals, offsets


def clip(polygon, normal, offset):
    distances = polygon @ normal - offset
    result = []
    for i, end in enumerate(polygon):
        a, b = distances[i - 1], distances[i]
        if (a < 0) != (b < 0):
            start = polygon[i - 1]
            result.append(start + (end - start) * (a / (a - b)))
        if b >= 0:
            result.append(end)
    return np.asarray(result)


def contact_clearance(face, glove_triangles, axis):
    """Return signed axial separation and the minimum distance inside the cap boundary."""
    plane, normals, offsets = face
    clearance = plane - float((glove_triangles @ axis).max())
    sides = glove_triangles @ normals.T - offsets
    outside = (sides.max(axis=1) < 0).any(axis=1)
    inside = (sides.min(axis=1) >= 0).all(axis=1)
    depth_ranges = []
    if inside.any():
        depths = glove_triangles[inside] @ axis - plane
        depth_ranges.extend(zip(depths.min(axis=1), depths.max(axis=1)))
    for tri, sides_of_tri in zip(glove_triangles[~outside & ~inside], sides[~outside & ~inside]):
        polygon = tri
        for normal, offset in zip(normals[(sides_of_tri < 0).any(axis=0)],
                                  offsets[(sides_of_tri < 0).any(axis=0)]):
            polygon = clip(polygon, normal, offset)
            if not len(polygon):
                break
        if len(polygon):
            depths = polygon @ axis - plane
            depth_ranges.append((depths.min(), depths.max()))
    gap = min((max(float(lo), -float(hi), 0.0) for lo, hi in depth_ranges), default=None)
    return {"clearance_mm": round(clearance * 1000, 3),
            "cap_face_gap_mm": None if gap is None else round(gap * 1000, 6)}
