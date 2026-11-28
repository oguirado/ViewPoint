# SPDX-License-Identifier: AGPL-3.0-only
"""Replace the coarse antenna tips with locally remeshed source surfaces."""
import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.geometry import delaunay_2d_cdt

from blockfix import native_cells
from mesh import activate, apply, points, triangles


def components(bm):
    unseen = set(bm.verts)
    for first in bm.verts:
        if first not in unseen:
            continue
        unseen.remove(first)
        stack = [first]
        part = [first]
        while stack:
            vertex = stack.pop()
            for edge in vertex.link_edges:
                other = edge.other_vert(vertex)
                if other in unseen:
                    unseen.remove(other)
                    part.append(other)
                    stack.append(other)
        yield part


def contour_cells(mask):
    """Keep resolved fragments and close corner-only contacts."""
    pending = set(map(tuple, np.argwhere(mask)))
    parts = []
    while pending:
        seed = min(pending)
        pending.remove(seed)
        stack = [seed]
        part = [seed]
        while stack:
            x, z = stack.pop()
            for neighbor in ((x - 1, z), (x + 1, z), (x, z - 1), (x, z + 1)):
                if neighbor in pending:
                    pending.remove(neighbor)
                    part.append(neighbor)
                    stack.append(neighbor)
        parts.append(part)
    retained = np.zeros_like(mask)
    for part in parts:
        if len(part) >= 5:
            retained[tuple(np.array(part).T)] = True
    a, b, c, d = retained[:-1, :-1], retained[1:, :-1], retained[:-1, 1:], retained[1:, 1:]
    while True:
        main = (a & d) & ~(b | c)
        off = (b & c) & ~(a | d)
        if not (main.any() or off.any()):
            return retained
        c[main] = True
        a[off] = True


def outline(mask):
    edges = {}
    for x, z in map(tuple, np.argwhere(mask)):
        if not mask[x, z - 1]:
            edges[x, z] = (x + 1, z)
        if not mask[x + 1, z]:
            edges[x + 1, z] = (x + 1, z + 1)
        if not mask[x, z + 1]:
            edges[x + 1, z + 1] = (x, z + 1)
        if not mask[x - 1, z]:
            edges[x, z + 1] = (x, z)
    loops = []
    while edges:
        first = min(edges)
        loop = [first]
        current = edges.pop(first)
        while current != first:
            loop.append(current)
            current = edges.pop(current)
        turns = [point for i, point in enumerate(loop)
                 if loop[i - 1][0] != loop[(i + 1) % len(loop)][0]
                 and loop[i - 1][1] != loop[(i + 1) % len(loop)][1]]
        loops.append(turns)
    return loops


def source_depth(coords, faces, step):
    """Trace the outer depth envelope through the cropped source triangles."""
    origin = coords.min(axis=0) - 3 * step
    grid = (coords - origin) / step
    shape = np.ceil(grid[:, (0, 2)].max(axis=0)).astype(int) + 4
    nearest = np.full(shape, np.inf)
    farthest = np.full(shape, -np.inf)
    for tri in grid[faces]:
        a, b, c = tri[:, (0, 2)]
        u, v = b - a, c - a
        det = u[0] * v[1] - u[1] * v[0]
        if abs(det) < 1.0e-9:
            continue
        x0, z0 = np.floor(tri[:, (0, 2)].min(axis=0)).astype(int)
        x1, z1 = np.ceil(tri[:, (0, 2)].max(axis=0)).astype(int)
        x, z = np.ogrid[x0:x1, z0:z1]
        qx, qz = x + 0.5 - a[0], z + 0.5 - a[1]
        s = (qx * v[1] - qz * v[0]) / det
        t = (u[0] * qz - u[1] * qx) / det
        inside = (s >= 0) & (t >= 0) & (s + t <= 1)
        depth = tri[0, 1] + s * (tri[1, 1] - tri[0, 1]) + t * (tri[2, 1] - tri[0, 1])
        region = np.s_[x0:x1, z0:z1]
        np.minimum(nearest[region], depth, out=nearest[region], where=inside)
        np.maximum(farthest[region], depth, out=farthest[region], where=inside)
    return origin, nearest, farthest


def rebuild_tip(tip, source_coords, source_faces, triangle_cap):
    solid, _, step = native_cells(tip.data)
    origin, y_first, y_last = source_depth(source_coords, source_faces, step * 0.6)
    step *= 0.6
    footprint = np.isfinite(y_first)
    mask = contour_cells(footprint)
    coords, edges = [], []
    for loop in outline(mask):
        start = len(coords)
        coords.extend(loop)
        edges.extend((start + i, start + (i + 1) % len(loop)) for i in range(len(loop)))
    # Extra points sample source thickness over the interior. The shared
    # triangulation keeps the depth fields separated everywhere in each face.
    support_stride = 7
    coords.extend((x, z) for x in range(support_stride, mask.shape[0] - 1, support_stride)
                  for z in range(support_stride, mask.shape[1] - 1, support_stride)
                  if mask[x - 1:x + 1, z - 1:z + 1].all())
    vertices, _, triangles_2d, *_ = delaunay_2d_cdt([Vector(v) for v in coords], edges, [], 0, 1.0e-6)
    positions = []
    for v in vertices:
        x, z = map(round, v)
        local = np.s_[max(0, x - 2):x + 2, max(0, z - 2):z + 2]
        active = footprint[local]
        low = float(np.median(y_first[local][active]))
        high = float(np.median(y_last[local][active]))
        if not np.isfinite(low) or high < low:
            raise RuntimeError("missing local source thickness")
        if high - low < 1.5:
            center = (low + high) / 2
            low, high = center - 0.75, center + 0.75
        positions.extend((origin + step * np.array([x, it, z])).tolist() for it in (low, high))
    faces = []
    boundary = set()
    for a, b, c in triangles_2d:
        p, q, r = (vertices[i] for i in (a, b, c))
        x, z = map(int, (p + q + r) / 3)
        if not mask[x, z]:
            continue
        if (q - p).cross(r - p) < 0:
            b, c = c, b
        faces.extend(((2 * a, 2 * b, 2 * c), (2 * c + 1, 2 * b + 1, 2 * a + 1)))
        for left, right in ((a, b), (b, c), (c, a)):
            if (right, left) in boundary:
                boundary.remove((right, left))
            else:
                boundary.add((left, right))
    for a, b in sorted(boundary):
        faces.extend(((2 * b, 2 * a, 2 * a + 1), (2 * b, 2 * a + 1, 2 * b + 1)))
    if not 0 < len(faces) <= triangle_cap:
        raise RuntimeError(f"local antenna triangle budget: {len(faces)} > {triangle_cap}")
    mesh = bpy.data.meshes.new("Source-depth antenna retopology")
    mesh.from_pydata(positions, [], faces)
    mesh.update()
    tip.data = mesh
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_edges], context="VERTS")
    bm.to_mesh(mesh)
    bm.free()
    return {"projection_step_source_units": step, "voxel_cells": int(solid.sum()),
            "front_projection_cells": int(footprint.sum()), "retained_projection_cells": int(mask.sum()),
            "interior_support_stride_cells": support_stride, "triangles": len(faces)}


def refine_tips(body, source, depth, tip_cap):
    xyz = points(source)
    faces = triangles(source)
    # Both tips are separate in the panel-actor profile. This cut sits below their
    # forked ends and above the coarse attachment geometry.
    z_min = 0.4585
    removed = 0
    if body is not None:
        bm = bmesh.new()
        bm.from_mesh(body.data)
        old_tips = [v for part in components(bm) if min(v.co.z for v in part) > 0.46 for v in part]
        removed = len(old_tips)
        bmesh.ops.delete(bm, geom=old_tips, context="VERTS")
        bm.to_mesh(body.data)
        bm.free()
    records = []
    for side in (-1, 1):
        within = (xyz[:, 2] >= z_min) & (side * xyz[:, 0] >= 0.125) & (side * xyz[:, 0] <= 0.207)
        chosen = faces[within[faces].all(axis=1)]
        if not len(chosen):
            raise RuntimeError("panel actor antenna crop is empty")
        indices, inverse = np.unique(chosen.ravel(), return_inverse=True)
        grid = bpy.data.meshes.new("Original antenna tip crop")
        grid.from_pydata(xyz[indices].tolist(), [], inverse.reshape(-1, 3).tolist())
        grid.update()
        tip = bpy.data.objects.new("Refined antenna tip", grid)
        bpy.context.collection.objects.link(tip)
        activate(tip)
        apply(tip, "Local octree remesh", "REMESH", mode="BLOCKS", octree_depth=depth,
              scale=0.9, use_remove_disconnected=False, use_smooth_shade=True)
        records.append({"side": side, "source_faces": len(chosen),
                        **rebuild_tip(tip, xyz[indices], inverse.reshape(-1, 3), tip_cap)})
        print(f"Local tip: {records[-1]}", flush=True)
        if body is not None:
            body.select_set(True)
            bpy.context.view_layer.objects.active = body
            bpy.ops.object.join()
    return {"cut_z_source_units": z_min, "octree_depth": depth, "triangle_cap_per_tip": tip_cap,
            "coarse_tip_vertices_removed": removed, "tips": records}
