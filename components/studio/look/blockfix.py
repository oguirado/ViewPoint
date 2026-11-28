# SPDX-License-Identifier: AGPL-3.0-only
"""Close ambiguous edge and corner contacts in Blender's BLOCKS remesh lattice."""
from itertools import product

import bpy
import numpy as np


CORNERS = tuple(product((0, 1), repeat=3))
ADJACENT = tuple(sum(1 << j for j, b in enumerate(CORNERS) if sum(x != y for x, y in zip(a, b)) == 1)
                 for a in CORNERS)
SQUARES = tuple(sum(1 << j for j, corner in enumerate(CORNERS) if corner[axis] == side)
                for axis in range(3) for side in (0, 1))
PAD = 2


def connected(bits):
    if bits == 0:
        return True
    frontier = bits & -bits
    remaining = bits ^ frontier
    while frontier:
        index = (frontier & -frontier).bit_length() - 1
        frontier ^= 1 << index
        extra = ADJACENT[index] & remaining
        frontier |= extra
        remaining ^= extra
    return remaining == 0


def native_cells(mesh):
    verts = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    intervals = np.concatenate([np.diff(np.unique(verts[:, axis])) for axis in range(3)])
    step = float(np.median(intervals))
    origin = verts.min(axis=0)
    native_points = (verts.astype(np.float64) - origin) / step
    n = np.rint(native_points).astype(np.int32)
    if np.max(np.abs(native_points - n)) > 0.004:
        raise RuntimeError("Remesh BLOCKS does not form a regular lattice")
    n += PAD
    loops = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", loops)
    faces = n[loops.reshape(-1, 4)]
    low, high = faces.min(axis=1), faces.max(axis=1)
    spans = high - low
    if ((spans > 1).any() or not (spans.sum(axis=1) == 2).all()):
        raise RuntimeError("Remesh BLOCKS contains non-unit or non-axial faces")
    size = n.max(axis=0) + PAD
    crossing = np.zeros(size, dtype=np.bool_)
    crossing[tuple(low[spans[:, 0] == 0].T)] = True
    solid = np.logical_xor.accumulate(crossing, axis=0)
    verify_count = sum(np.count_nonzero(np.diff(solid, axis=axis)) for axis in range(3))
    if verify_count != len(faces) or solid[[0, -1], :, :].any():
        raise RuntimeError("Remesh BLOCKS parity reconstruction changed native boundary faces")
    return solid, origin.astype(np.float64) - PAD * step, step


def cap_contacts(solid):
    # A valid corner has connected solid and connected void across its eight cells.
    good = [pattern for pattern in range(256)
            if connected(pattern) and connected(255 ^ pattern)
            and all(connected(pattern & square) for square in SQUARES)]
    add = np.array([min((option for option in good if option & state == state),
                        key=lambda option: ((option ^ state).bit_count(), option)) ^ state
                    for state in range(256)], dtype=np.uint8)
    added = []
    initial = int(solid.sum())
    slices = [tuple(slice(bit, bit + solid.shape[i] - 1) for i, bit in enumerate(corner))
              for corner in CORNERS]
    for _ in range(32):
        state = np.zeros(np.array(solid.shape) - 1, dtype=np.uint8)
        for k, corner in enumerate(slices):
            state |= solid[corner].astype(np.uint8) << k
        update = add[state]
        if not update.any():
            return added
        for k, corner in enumerate(slices):
            solid[corner] |= (update & (1 << k)) != 0
        change = int(solid.sum()) - initial - sum(added)
        added.append(change)
        if not change or sum(added) > initial * 0.02:
            raise RuntimeError(f"nonlocal octree contact change: {added} of {initial}")
    raise RuntimeError("octree contact iterations did not converge")


def surface(solid, origin, step):
    all_faces = []
    for axis in range(3):
        forward_axis = (axis + 1) % 3
        across_axis = (axis + 2) % 3
        before = np.take(solid, np.arange(solid.shape[axis] - 1), axis=axis)
        after = np.take(solid, np.arange(1, solid.shape[axis]), axis=axis)
        gap = np.nonzero(before != after)
        towards_positive = before[gap]
        face_start = np.column_stack(gap)
        face_start[:, axis] += 1
        fwd = np.eye(3, dtype=np.int32)[forward_axis]
        across = np.eye(3, dtype=np.int32)[across_axis]
        faces = np.stack((face_start, face_start + fwd, face_start + fwd + across, face_start + across), axis=1)
        faces[~towards_positive] = faces[~towards_positive, ::-1]
        all_faces.append(faces)
    faces = np.concatenate(all_faces)
    verts, inverse = np.unique(faces.reshape(-1, 3), axis=0, return_inverse=True)
    coordinates = origin + verts * step
    new_mesh = bpy.data.meshes.new("Closed octree blocks")
    new_mesh.from_pydata(coordinates.tolist(), [], inverse.reshape(-1, 4).tolist())
    new_mesh.update()
    return new_mesh


def close_block_contacts(obj):
    solid, origin, step = native_cells(obj.data)
    initial = int(solid.sum())
    added = cap_contacts(solid)
    obj.data = surface(solid, origin, step)
    return {"step_source_units": step, "solid_cells_before_repair": initial,
            "ambiguous_corner_voxels_added_by_pass": added}
