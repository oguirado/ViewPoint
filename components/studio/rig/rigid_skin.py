# SPDX-License-Identifier: AGPL-3.0-only
"""Partition the welded shell and preserve its bind surface at the cuts."""
from dataclasses import dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from rig_common import need

RIGIDITY_M = .0005
ALGORITHM = "connected-components/joint-plane-clipping/v1"


@dataclass(frozen=True)
class Cut:
    normal: tuple
    offset: float
    below: object
    above: object


def components(faces, count):
    edges = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    graph = coo_matrix((np.ones(len(edges), dtype=np.uint8), edges.T), shape=(count, count))
    return connected_components(graph.tocsr(), directed=False)[1]


def shell_cuts(heads, parents):
    need(all(parents[j] == parent for chain in ((6, 7, 8, 9), (10, 11, 12, 13),
             (0, 14, 15, 16, 17), (0, 18, 19, 20, 21))
             for parent, j in zip(chain, chain[1:])), "rigid_joint_hierarchy")

    def height(y, below, above):
        return Cut((0., 1., 0.), y, below, above)

    def arm(upper, fore, hand):
        # The visible wrist collar is above the center of the glove pivot.
        wrist_collar = heads[hand, 1] + .36 * (heads[fore, 1] - heads[hand, 1])
        return height(heads[fore, 1], height(wrist_collar, hand, fore), upper)

    def leg(thigh, shin, boot):
        # Keep the entire rigid boot casing, including its shaft, on the ankle.
        boot_collar = heads[boot, 1] + .42 * (heads[shin, 1] - heads[boot, 1])
        return height(heads[shin, 1], height(boot_collar, boot, shin), thigh)

    core = 1
    legs = Cut((1., 0., 0.), 0., leg(14, 15, 16), leg(18, 19, 20))
    legs = Cut((0., 0., 1.), -.145, core, Cut((0., 0., 1.), .145, legs, core))
    hips = height((heads[14, 1] + heads[15, 1]) / 2, legs, core)
    neck_seam = heads[4, 1] - .28 * (heads[4, 1] - heads[3, 1])
    core_and_dome = height(neck_seam, hips, 5)
    shoulder = .83 * min(abs(heads[7, 0]), abs(heads[11, 0]))
    return Cut((1., 0., 0.), -shoulder, arm(7, 8, 9),
               Cut((1., 0., 0.), shoulder, core_and_dome, arm(11, 12, 13)))


def clipped_polygon(poly, distances, keep_positive):
    result = []
    for a, b, da, db in zip(poly, np.roll(poly, -1, axis=0), distances,
                             np.roll(distances, -1), strict=True):
        if da == 0 or (da > 0) == keep_positive:
            result.append(a)
        if da * db < 0:
            result.append(a + da / (da - db) * (b - a))
    return np.asarray(result)


def split_surface(xyz, faces, remap, attrs, heads, parents):
    welded = xyz[remap]
    ids = components(remap[faces], len(xyz))
    sizes = np.bincount(ids)
    main = int(sizes.argmax())
    source_ids = ids[remap[faces[:, 0]]]
    need(np.all(ids[remap[faces]] == source_ids[:, None]), "rigid_weld_component_mismatch")
    # The tiny floating components are pieces of the two antenna tips. Their
    # sides are separate assembly parts, with the shared head joint as driver.
    antenna_base = heads[5, 1] + .5 * (xyz[:, 1].max() - heads[5, 1])
    antenna_side = {}
    for i in np.flatnonzero(sizes):
        if i == main:
            continue
        cloud = xyz[ids == i]
        need(cloud[:, 1].min() >= antenna_base and np.all(cloud[:, 0] * cloud[0, 0] > 0),
             "rigid_unclassified_component")
        antenna_side[int(i)] = "left" if cloud[0, 0] > 0 else "right"

    cuts = shell_cuts(heads, parents)
    values = {"POSITION": welded, **attrs}
    keys, indices, bone_ids = {}, [], []
    out_attrs = {key: [] for key in values}
    basis = np.eye(3)
    plane_splits = 0
    max_area_error = 0.

    def add_vertex(face, bary, joint):
        active = np.flatnonzero(bary > 1e-10)
        # Include the part to duplicate a shared cut border, and original IDs
        # to preserve the UV and shading seam already present in the bake.
        key = (joint, tuple(sorted((int(face[k]), round(float(bary[k]), 11)) for k in active)))
        if key not in keys:
            keys[key] = len(bone_ids)
            bone_ids.append(joint)
            for name, array in values.items():
                value = bary @ array[face]
                if name in ("NORMAL", "TANGENT") and len(active) > 1:
                    value[:3] /= np.linalg.norm(value[:3])
                    if name == "TANGENT":
                        value[3] = array[face[int(np.argmax(bary))], 3]
                out_attrs[name].append(value)
        return keys[key]

    def emit(poly, cut, face, corners):
        nonlocal plane_splits
        if isinstance(cut, Cut):
            distances = poly @ (corners @ cut.normal - cut.offset)
            distances[abs(distances) < 1e-10] = 0.
            if np.max(distances) <= 0:
                emit(poly, cut.below, face, corners)
            elif np.min(distances) >= 0:
                emit(poly, cut.above, face, corners)
            else:
                plane_splits += 1
                emit(clipped_polygon(poly, distances, False), cut.below, face, corners)
                emit(clipped_polygon(poly, distances, True), cut.above, face, corners)
            return
        points = poly @ corners
        for k in range(1, len(poly) - 1):
            a, b = points[k:k + 2] - points[0]
            if np.linalg.norm(np.cross(a, b)) > 1e-13:
                indices.append([add_vertex(face, poly[i], cut) for i in (0, k, k + 1)])

    for face, component in zip(faces, source_ids, strict=True):
        corners = welded[face].astype(np.float64)
        first = len(indices)
        emit(basis, cuts if component == main else 5, face, corners)
        output = np.asarray([[out_attrs["POSITION"][i] for i in triangle] for triangle in indices[first:]],
                            dtype="<f4").astype(np.float64)
        source_area = np.linalg.norm(np.cross(corners[1] - corners[0], corners[2] - corners[0])) * .5
        need(len(output) > 0, "rigid_bind_triangle_missing")
        split_area = np.linalg.norm(np.cross(output[:, 1] - output[:, 0],
                                             output[:, 2] - output[:, 0]), axis=1).sum() * .5
        max_area_error = max(max_area_error, float(abs(split_area - source_area)))
    need(max_area_error <= 1e-7, f"rigid_bind_surface_area_changed:{max_area_error:.3e}")
    attrs = {name: np.asarray(value, dtype="<f4") for name, value in out_attrs.items()}
    faces_out = np.asarray(indices, dtype="<u4")
    primary = np.asarray(bone_ids, dtype="u1")
    need(np.all(primary[faces_out] == primary[faces_out[:, :1]]), "rigid_triangle_crosses_bones")
    need(set(primary) == {1, 5, 7, 8, 9, 11, 12, 13, 14, 15, 16, 18, 19, 20},
         "rigid_shell_part_roster")
    joints = np.zeros((len(primary), 4), dtype="u1")
    weights = np.zeros(joints.shape, dtype="<f4")
    joints[:, 0], weights[:, 0] = primary, 1.
    return attrs, faces_out, joints, weights, {
        "algorithm": ALGORITHM, "connected_components": len(sizes),
        "antenna_components_by_side": {side: list(antenna_side.values()).count(side)
                                       for side in ("left", "right")},
        "welded_vertices": len(xyz), "plane_splits": plane_splits,
        "max_source_triangle_area_error_m2": max_area_error,
        "max_influences": 1,
        "vertices_by_joint": {f"bone_{j}": int(np.count_nonzero(primary == j)) for j in sorted(set(primary))},
    }
