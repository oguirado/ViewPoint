# SPDX-License-Identifier: AGPL-3.0-only
"""Transfer UniRig weights from the original surface onto the textured mesh."""
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from bindfix import recompute_inverse_binds
from rig_common import Hold, invoke_blender, need, write
from glb_utils import add, pack, primitive, unpack
from pose_metrics import measure
from template_rig import accessor, transforms

HERE = Path(__file__).resolve().parent
TOLERANCE_M = 1e-6
# Columns correspond to the locked JOINT_MAP order. These source roles are specific
# to the observed source branching pattern. None entries are generated from its anatomy.
SOURCE = (0, 5, 6, 7, 9, 12, None, 16, 17, 18, None, 13, 14, 15,
          3, None, 4, None, 1, None, 2, None)
SOURCE_PARENTS = (None, 0, 1, 0, 3, 0, 5, 6, 7, 8, 9, 9, 9, 7, 13, 14, 7, 16, 17)
CONSOLIDATE = {8: 4, 10: 5, 11: 5}
LEGS = ((-1, 14, 15, 16, 17, 3, 4), (1, 18, 19, 20, 21, 1, 2))


def fields(doc):
    return primitive(doc)["attributes"]


def joints_in(doc):
    nodes = doc["nodes"]
    skin, = doc["skins"]
    links = {}
    for i, node in enumerate(nodes):
        for child in node.get("children", []):
            need(0 <= child < len(nodes) and child not in links, "unirig_joint_parent")
            links[child] = i
    index = {nodes[i]["name"]: i for i in skin["joints"]}
    need(len(index) == len(skin["joints"]) == len(set(skin["joints"])), "unirig_joint_names")
    return skin, index, links


def numbered(index, count):
    names = [f"bone_{i}" for i in range(count)]
    need(set(names) == index.keys(), "unirig_joint_roster")
    return [index[name] for name in names]


def parent_numbers(ids, parent):
    inverse = {node: number for number, node in enumerate(ids)}
    return tuple(inverse.get(parent.get(node)) for node in ids)


def skin_arrays(doc, data, ids):
    skin, = doc["skins"]
    attr = fields(doc)
    need({key for key in attr if key.startswith(("JOINTS_", "WEIGHTS_"))}
         == {"JOINTS_0", "WEIGHTS_0"}, "unirig_weight_set_roster")
    xyz = accessor(doc, data, attr["POSITION"])
    slot = accessor(doc, data, attr["JOINTS_0"])
    weights = accessor(doc, data, attr["WEIGHTS_0"]).astype(np.float64)
    need(slot.dtype.kind == "u" and slot.shape == weights.shape == (len(xyz), 4)
         and np.isfinite(xyz).all() and np.isfinite(weights).all()
         and np.all(weights >= 0) and slot.max() < len(ids), "unirig_invalid_weights")
    need(np.all(weights.sum(axis=1) > 0), "unirig_unweighted_region")
    error = float(np.max(np.abs(weights.sum(axis=1) - 1)))
    need(error < TOLERANCE_M, "unirig_unnormalized_weights")
    inverse = {node: i for i, node in enumerate(ids)}
    reverse_slot = np.array([inverse[node] for node in skin["joints"]], dtype=np.int32)
    return xyz, reverse_slot[slot], weights, error


def bind_report(doc, data, xyz, ids, joints, weights, parents):
    skin, = doc["skins"]
    mesh_id, = [i for i, node in enumerate(doc["nodes"]) if node.get("skin") == 0]
    world = np.asarray(transforms(doc, parents), dtype=np.float64)
    need(np.array_equal(world[mesh_id], np.eye(4)), "unirig_mesh_world_not_identity")
    lookup = [skin["joints"].index(i) for i in ids]
    binds = accessor(doc, data, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)[lookup]
    error = world[ids] @ binds - np.eye(4)
    spans = np.array(np.meshgrid(*zip(xyz.min(axis=0), xyz.max(axis=0)))).reshape(3, -1).T
    bound = float(np.linalg.norm(np.einsum("aij,pj->api", error[:, :3],
                                         np.column_stack((spans, np.ones(len(spans))))), axis=2).max())
    need(np.isfinite(bound) and bound <= TOLERANCE_M, "unirig_bind_pose_mismatch")
    max_skin = 0.0
    for lo in range(0, len(xyz), 32_768):
        hi = lo + 32_768
        hom = np.column_stack((xyz[lo:hi], np.ones(len(xyz[lo:hi]))))
        delta = np.einsum("vkij,vj,vk->vi", error[joints[lo:hi], :3], hom, weights[lo:hi])
        max_skin = max(max_skin, float(np.linalg.norm(delta, axis=1).max()))
    return {"max_bbox_joint_bind_error_m": bound, "max_blended_bind_error_m": max_skin,
            "bind_tolerance_m": TOLERANCE_M}


def closest_on_triangles(triangle, queries):
    triangle = triangle.astype(np.float64)
    a, b, c = np.moveaxis(triangle, 1, 0)
    normal = np.cross(b - a, c - a)
    norm2 = np.einsum("ij,ij->i", normal, normal)
    need(np.all(norm2 > 0), "unirig_degenerate_nearest_triangle")
    plane = queries - (np.einsum("ij,ij->i", queries - a, normal) / norm2)[:, None] * normal
    ab, ac = b - a, c - a
    v = np.einsum("ij,ij->i", np.cross(plane - a, ac), normal) / norm2
    w = np.einsum("ij,ij->i", np.cross(ab, plane - a), normal) / norm2
    plane_weights = np.column_stack((1 - v - w, v, w))
    plane_valid = np.all(plane_weights >= 0, axis=1)
    candidates, weights = [plane], [plane_weights]
    for i, j in ((0, 1), (1, 2), (2, 0)):
        start, edge = triangle[:, i], triangle[:, j] - triangle[:, i]
        t = np.clip(np.einsum("ij,ij->i", queries - start, edge)
                    / np.einsum("ij,ij->i", edge, edge), 0, 1)
        candidates.append(start + t[:, None] * edge)
        fractions = np.zeros_like(plane_weights)
        fractions[:, i], fractions[:, j] = 1 - t, t
        weights.append(fractions)
    candidates = np.stack(candidates, axis=1)
    distance = np.sum((queries[:, None] - candidates)**2, axis=2)
    distance[~plane_valid, 0] = np.inf
    order = np.arange(len(queries)), distance.argmin(axis=1)
    return candidates[order], np.stack(weights, axis=1)[order]


def top_four(dense):
    need(np.isfinite(dense).all() and dense.min() >= 0, "unirig_invalid_weight_transfer")
    order = np.argsort(-dense, axis=1, kind="stable")[:, :4]
    kept = np.take_along_axis(dense, order, axis=1)
    total = kept.sum(axis=1, keepdims=True)
    need(np.all(total > 0), "unirig_unweighted_region")
    dropped = dense.sum(axis=1) - total[:, 0]
    return order.astype("u1"), kept / total, dropped


def semantic_skeleton(xyz, source_world, selected, mapping, height, mesh_name):
    parents = tuple(row["parent"] for row in mapping["adapter_map"]["joints"])
    names = [row["target"] for row in mapping["adapter_map"]["joints"]]
    need(names == [f"bone_{i}" for i in range(len(SOURCE))], "unirig_clips_semantic_order")
    need(mapping["adapter_map"]["basis"] == "RH_Y_UP_METRE", "unirig_clips_basis")
    # Use the previously accepted metre scale for equal pose metrics. All
    # joint positions and orientations come from source or measured source geometry.
    lo, hi = selected.min(axis=0), selected.max(axis=0)
    need(height > 0 and np.isfinite(height), "unirig_invalid_comparison_scale")
    need(hi[1] > lo[1], "unirig_invalid_selected_height")
    scale = height / (hi[1] - lo[1])
    origin = np.array([(hi[0] + lo[0]) * .5, lo[1], 0])
    heads = np.zeros((len(names), 3))
    orientations = np.repeat(np.eye(3)[None, :, :], len(names), axis=0)
    for target, raw in enumerate(SOURCE):
        if raw is not None:
            heads[target] = source_world[raw, :3, 3]
            orientations[target] = Rotation.from_matrix(source_world[raw, :3, :3]).as_matrix()
    # Collars are inferred along the generated chest-to-shoulder edges.
    for collar, shoulder in ((6, 7), (10, 11)):
        heads[collar] = .52 * heads[3] + .48 * heads[shoulder]
        orientations[collar] = orientations[shoulder]
    floor = float(xyz[:, 1].min())
    width = float(np.max(abs(xyz[:, 0])))
    for side, hip, knee, ankle, toe, _, _ in LEGS:
        need(side * heads[hip, 0] > .12 * width and side * heads[ankle, 0] > .12 * width
             and heads[0, 1] > heads[hip, 1] > heads[ankle, 1] >= floor, "unirig_leg_anatomy")
        points = xyz[(side * xyz[:, 0] > .10 * width) & (side * xyz[:, 0] < .50 * width)
                     & (xyz[:, 1] < floor + .035 * (hi[1] - lo[1]))]
        need(len(points) >= 12, "unirig_sole_landmark_missing")
        heads[knee] = .36 * heads[hip] + .64 * heads[ankle]
        heads[toe] = (np.median(points[:, 0]), heads[ankle, 1], np.quantile(points[:, 2], .77))
        need(heads[toe, 2] - heads[ankle, 2] > .009, "unirig_toe_landmark_missing")
        orientations[knee] = orientations[hip]
        orientations[toe] = orientations[ankle]
    need(heads[8, 0] <= heads[7, 0] < -.5 * width
         and .5 * width < heads[11, 0] <= heads[12, 0]
         and heads[7, 1] > heads[8, 1] > heads[9, 1]
         and heads[11, 1] > heads[12, 1] > heads[13, 1]
         and np.all(np.diff(heads[:6, 1]) > 0), "unirig_torso_arm_anatomy")
    metric = (heads - origin) * scale
    nodes = [{"name": mesh_name, "mesh": 0, "skin": 0}]
    for i, parent in enumerate(parents):
        upstream = np.eye(3) if parent is None else orientations[parent]
        offset = metric[i] if parent is None else metric[i] - metric[parent]
        nodes.append({"name": names[i], "translation": (upstream.T @ offset).tolist(),
                      "rotation": Rotation.from_matrix(upstream.T @ orientations[i]).as_quat().tolist()})
        if parent is not None:
            nodes[parent + 1].setdefault("children", []).append(i + 1)
    tails = metric.copy()
    for i, parent in reversed(list(enumerate(parents))):
        if parent is not None:
            tails[parent] = metric[i]
    for target in (5, 9, 13, 17, 21):
        delta = metric[target] - metric[parents[target]]
        tails[target] = metric[target] + .25 * delta
    need(np.all(np.linalg.norm(tails - metric, axis=1) > .005), "unirig_degenerate_semantic_segment")
    return nodes, metric, tails, heads, origin, float(scale)


def joint_map(nodes, heads, mapping, receipt):
    return {"schema": "rp4.studio-unirig-transfer.joint-map/v1", "source_receipt": receipt,
            "adapter_semantics": mapping["adapter_semantics"],
            "joint_glb_nodes": {node["name"]: i for i, node in enumerate(nodes) if i},
            "roles": [{"role": row["source"], "target": row["target"], "parent": row["parent"],
                       "raw_joint": f"bone_{src}" if src is not None else None,
                       "origin_source": heads[i].tolist()}
                      for i, (src, row) in enumerate(zip(SOURCE, mapping["adapter_map"]["joints"], strict=True))],
            "inferred_roles": [nodes[i + 1]["name"] for i, raw_id in enumerate(SOURCE) if raw_id is None],
            "consolidated_source_weights": CONSOLIDATE}


def lift_samples(xyz, raw_joints, raw_weights, heads):
    dense = np.zeros((len(xyz), len(SOURCE)), dtype=np.float64)
    remap = {raw: i for i, raw in enumerate(SOURCE) if raw is not None} | CONSOLIDATE
    need(set(remap) == set(range(len(SOURCE_PARENTS))), "unirig_source_role_coverage")
    for influence in range(4):
        np.add.at(dense, (np.arange(len(xyz)), np.array([remap[i] for i in range(len(remap))])[
            raw_joints[:, influence]]), raw_weights[:, influence])
    # Redistribute each leg's total UniRig influence within its own chain,
    # using short linear transitions around the newly inferred flexion pivots.
    for _, hip, knee, ankle, toe, _, _ in LEGS:
        extent = heads[hip, 1] - heads[ankle, 1]
        leg = dense[:, hip] + dense[:, ankle]
        lower = np.clip(.5 + (heads[knee, 1] - xyz[:, 1]) / (.12 * extent), 0, 1)
        foot = np.clip(.5 + (heads[ankle, 1] + .025 * extent - xyz[:, 1]) / (.10 * extent), 0, 1)
        fore = np.clip(.5 + (xyz[:, 2] - heads[toe, 2]) / (.14 * extent), 0, 1)
        dense[:, hip] = leg * (1 - lower) * (1 - foot)
        dense[:, knee] = leg * lower * (1 - foot)
        dense[:, ankle] = leg * foot * (1 - fore)
        dense[:, toe] = leg * foot * fore
    need(np.allclose(dense.sum(axis=1), raw_weights.sum(axis=1), rtol=0, atol=1e-12),
         "unirig_augmentation_lost_weight")
    return dense


def geometry(doc, data):
    p = primitive(doc)
    pxyz = accessor(doc, data, p["attributes"]["POSITION"])
    faces = accessor(doc, data, p["indices"]).astype(np.int32).reshape(-1, 3)
    need(p.get("mode", 4) == 4 and len(faces) > 0 and faces.min() >= 0 and faces.max() < len(pxyz),
         "unirig_invalid_triangle_geometry")
    return pxyz, faces


def validate(candidate, mapping):
    doc, data = unpack(candidate)
    _, index, parents = joints_in(doc)
    ids = numbered(index, len(SOURCE))
    need(parent_numbers(ids, parents) == tuple(r["parent"] for r in mapping["adapter_map"]["joints"]),
         "unirig_clips_semantic_hierarchy")
    xyz, joints, weights, error = skin_arrays(doc, data, ids)
    measurements = bind_report(doc, data, xyz, ids, joints, weights, parents)
    histogram = np.bincount((weights > 0).sum(axis=1), minlength=5)
    measurements |= {"joint_glb_nodes": index, "adapter_semantics": mapping["adapter_semantics"],
                     "vertices": len(xyz), "weight_histogram": histogram.tolist(),
                     "max_weight_sum_error": error}
    return measurements, (xyz, joints, weights)



def transfer(source, selected_raw, mapping, height, source_receipt, work, *, blender):
    raw = source
    fixed = recompute_inverse_binds(raw, timeout_s=60)
    need(recompute_inverse_binds(fixed, timeout_s=60) == fixed, "unirig_raw_bind_not_idempotent")
    source_doc, source_data = unpack(fixed)
    _, index, source_parent = joints_in(source_doc)
    ids = numbered(index, len(SOURCE_PARENTS))
    need(source_receipt["joint_count"] == len(ids)
         and parent_numbers(ids, source_parent) == SOURCE_PARENTS, "unirig_source_anatomy_shape")
    xyz, joints, weights, _ = skin_arrays(source_doc, source_data, ids)
    source_bind = bind_report(source_doc, source_data, xyz, ids, joints, weights, source_parent)
    _, faces = geometry(source_doc, source_data)

    doc, data = unpack(selected_raw)
    need(len(doc["nodes"]) == 1 and set(doc["nodes"][0]) <= {"name", "mesh"}
         and doc["nodes"][0].get("mesh") == 0 and not doc.get("skins") and "TEXCOORD_0" in fields(doc)
         and bool(doc.get("materials")), "unirig_studio_source_profile")
    selected, studio_faces = geometry(doc, data)
    raw_min, raw_max = xyz.min(axis=0), xyz.max(axis=0)
    need(np.max(abs(selected.min(axis=0) - raw_min)) < .015
         and np.max(abs(selected.max(axis=0) - raw_max)) < .015, "unirig_surface_frame_mismatch")
    worlds = np.asarray(transforms(source_doc, source_parent))[ids]
    nodes, heads, tails, raw_heads, origin, scale = semantic_skeleton(
        xyz, worlds, selected, mapping, height, doc["nodes"][0].get("name", "Studio mesh"))
    write(work / "JOINT_MAP.json", joint_map(nodes, raw_heads, mapping, source_receipt))


    a, b, c = np.moveaxis(xyz[faces], 1, 0)
    nondegenerate = np.any(np.cross(b - a, c - a) != 0, axis=1)
    faces = faces[nondegenerate]
    np.savez(work / "surface.npz", xyz=xyz, faces=faces, queries=selected)
    need(len(faces) > 0, "unirig_no_source_triangles")
    invoke_blender(work, HERE / "surface_bvh.py", [work], blender=blender)
    with np.load(work / "closest.npz") as cache:
        chosen, hits = cache["triangles"], cache["hits"]
    need(chosen.shape == (len(selected),) and hits.shape == selected.shape
         and chosen.min() >= 0 and chosen.max() < len(faces), "unirig_invalid_nearest_witness")
    corners = faces[chosen]
    projected, bary = closest_on_triangles(xyz[corners], selected)
    need(np.max(np.linalg.norm(projected - hits, axis=1)) < 1e-5, "unirig_closest_point_readback")
    distances = np.linalg.norm(projected - selected, axis=1)
    unique, inverse = np.unique(corners, return_inverse=True)
    sampled = lift_samples(xyz[unique], joints[unique], weights[unique], raw_heads)
    dense = np.einsum("ni,nik->nk", bary, sampled[inverse.reshape(corners.shape)])
    slots, transferred, dropped = top_four(dense)
    prior = bytes(data)
    attrs = fields(doc)
    pos = doc["accessors"][attrs["POSITION"]]
    position_view = doc["bufferViews"][pos["bufferView"]]
    positions = ((selected - origin) * scale).astype("<f4")
    accessor(doc, data, attrs["POSITION"])[:] = positions
    pos.update(min=positions.min(axis=0).tolist(), max=positions.max(axis=0).tolist())
    doc["nodes"] = nodes
    doc["scenes"][doc.get("scene", 0)]["nodes"] = [0, 1]
    joints_out = list(range(1, len(nodes)))
    bind = add(doc, data, np.repeat(np.eye(4).reshape(1, 16), len(joints_out), axis=0), "MAT4", "<f4", 5126)
    doc["skins"] = [{"name": "UniRig Transfer", "joints": joints_out, "skeleton": 1, "inverseBindMatrices": bind}]
    attrs["JOINTS_0"] = add(doc, data, slots, "VEC4", "u1", 5121)
    attrs["WEIGHTS_0"] = add(doc, data, transferred, "VEC4", "<f4", 5126)
    for key in ("JOINTS_0", "WEIGHTS_0"):
        doc["bufferViews"][doc["accessors"][attrs[key]]["bufferView"]]["target"] = 34962
    lo = position_view.get("byteOffset", 0)
    hi = lo + position_view["byteLength"]
    need("byteStride" not in position_view and data[:lo] == prior[:lo]
         and data[hi:len(prior)] == prior[hi:], "unirig_studio_pbr_payload_changed")
    rig = recompute_inverse_binds(pack(doc, data), timeout_s=60)
    need(recompute_inverse_binds(rig, timeout_s=60) == rig, "unirig_studio_bind_not_idempotent")
    output = work / "unirig-transfer.glb"
    measurements, (xyz_out, joints_out, weights_out) = validate(rig, mapping)
    measurements |= {"triangles": len(studio_faces), "source_bind": source_bind,
                     "source_receipt": source_receipt, "source_rig": source_receipt["output"],
                     "source_triangles": int(np.count_nonzero(nondegenerate)),
                     "skipped_degenerate_source_triangles": int(np.count_nonzero(~nondegenerate)),
                     "pbr_payload_preserved_except_placement": True, "uniform_scale": scale,
                     "max_dropped_weight": float(dropped.max()),
                     "distance_quantiles_m_a3h": dict(zip(("p50", "p95", "p99", "max"),
                         np.quantile(distances, (.50, .95, .99, 1.)).tolist(), strict=True))}
    pose_input = work / "template-rig.glb"
    pose_input.write_bytes(rig)
    invoke_blender(work, HERE / "blender_template.py", ["poses", work], blender=blender)
    try:
        poses = measure(work, xyz_out, joints_out, weights_out, heads, tails)
        need(not poses["flagged_sections"], "unirig_pose_collapse")
    except Hold as error:
        measurements.update(status="HOLD", pose_refusal=str(error))
    else:
        measurements.update(status="PASS_LOCAL_GATES", pose_report=poses)
    output.write_bytes(rig)
    return output, measurements
