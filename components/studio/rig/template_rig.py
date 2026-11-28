# SPDX-License-Identifier: AGPL-3.0-only
"""Fit and skin a mesh against an explicit humanoid template and joint map."""
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from rig_common import Hold, document, invoke_blender, need, pin
from glb_utils import add, pack, primitive, unpack
from rigid_skin import RIGIDITY_M, split_surface
from rigid_distance import distance_bounds

HERE = Path(__file__).resolve().parent

CHAINS = ((0, 1, 2, 3, 4, 5), (6, 7, 8, 9), (10, 11, 12, 13),
          (14, 15, 16, 17), (18, 19, 20, 21))
DTYPES = {5121: "u1", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

def accessor(doc, data, index):
    need(type(index) is int and 0 <= index < len(doc["accessors"]), "studio_accessor_index")
    a = doc["accessors"][index]
    view_id = a["bufferView"]
    need(type(view_id) is int and 0 <= view_id < len(doc["bufferViews"]), "studio_accessor_view")
    view = doc["bufferViews"][view_id]
    need(view["buffer"] == 0 and "sparse" not in a and not a.get("normalized"),
         "studio_unsupported_accessor")
    dtype = np.dtype(DTYPES[a["componentType"]])
    width = WIDTHS[a["type"]]
    row_bytes = dtype.itemsize * width
    stride = view.get("byteStride", row_bytes)
    start, offset = view.get("byteOffset", 0), a.get("byteOffset", 0)
    count = a["count"]
    need(count > 0 and row_bytes <= stride and start >= 0 and offset >= 0
         and offset + (count - 1) * stride + row_bytes <= view["byteLength"]
         and start + view["byteLength"] <= len(data), "studio_accessor_bounds")
    return np.ndarray((count, width), dtype=dtype, buffer=data, offset=start + offset,
                      strides=(stride, dtype.itemsize))


def topology(doc, mapping):
    nodes = doc["nodes"]
    skin, = doc["skins"]
    index = mapping["joint_glb_nodes"]
    declared = mapping["adapter_map"]["joints"]
    names = [item["target"] for item in declared]
    need(len(names) == len(index) == len(skin["joints"]) == 22
         and set(names) == index.keys() and set(index.values()) == set(skin["joints"])
         and all(type(i) is int and 0 <= i < len(nodes) and nodes[i].get("name") == name for name, i in index.items()),
         "template_joint_roster")
    parent = {}
    for i, node in enumerate(nodes):
        for child in node.get("children", []):
            need(type(child) is int and 0 <= child < len(nodes) and child not in parent, "template_joint_hierarchy")
            parent[child] = i
    for row in declared:
        joint = index[row["target"]]
        above = parent.get(joint)
        expected = row["parent"]
        need(above not in skin["joints"] if expected is None else above == index[names[expected]],
             "template_joint_hierarchy")
    return skin, index, parent


def transforms(doc, parent):
    world = {}
    active = set()

    def visit(i):
        if i not in world:
            need(i not in active, "studio_joint_hierarchy_cycle")
            active.add(i)
            node = doc["nodes"][i]
            need("matrix" not in node, "template_expected_trs")
            matrix = np.eye(4)
            matrix[:3, :3] = (Rotation.from_quat(node.get("rotation", [0, 0, 0, 1])).as_matrix()
                              * node.get("scale", [1, 1, 1]))
            matrix[:3, 3] = node.get("translation", [0, 0, 0])
            world[i] = visit(parent[i]) @ matrix if i in parent else matrix
            active.remove(i)
        return world[i]
    return [visit(i) for i in range(len(doc["nodes"]))]


def load_template(template_raw, mapping):
    doc, data = unpack(template_raw)
    _, _, parent = topology(doc, mapping)
    part = primitive(doc)
    positions = accessor(doc, data, part["attributes"]["POSITION"])
    height = float(np.ptp(positions[:, 1]))
    need(np.isfinite(height) and height > 0, "template_invalid_height")
    return doc, transforms(doc, parent), height


def source_mesh(raw):
    doc, data = unpack(raw)
    node, = doc["nodes"]
    part = primitive(doc)
    need(set(node) <= {"name", "mesh"} and node["mesh"] == 0 and "skins" not in doc
         and not doc.get("animations") and part.get("mode", 4) == 4,
         "template_selected_mesh_layout")
    attrs = part["attributes"]
    need("NORMAL" in attrs and "TEXCOORD_0" in attrs and "material" in part,
         "template_selected_pbr_layout")
    xyz = accessor(doc, data, attrs["POSITION"]).astype(np.float64)
    faces = accessor(doc, data, part["indices"]).astype(np.int32).reshape(-1, 3)
    need(np.isfinite(xyz).all() and len(xyz) > 0 and len(faces) > 0 and faces.min() >= 0 and faces.max() < len(xyz),
         "template_selected_mesh_invalid")
    return doc, data, xyz, faces


def fit(xyz, reference, height):
    low, high = xyz.min(axis=0), xyz.max(axis=0)
    need(high[1] > low[1], "template_invalid_height")
    scale = height / (high[1] - low[1])
    shift = np.array([(low[0] + high[0]) / 2, low[1], 0.0])
    vertices = (xyz - shift) * scale
    # Weld exact single-precision seams before measuring, so UV and normal splits do not
    # bias a landmark. Sampling windows are fractions of the admitted mesh extents.
    unique = np.unique(vertices, axis=0)
    x, y, z = unique.T
    width = np.max(abs(x))

    def section(side, ylo, yhi, xlo, xhi, description):
        cloud = unique[(y >= ylo * height) & (y <= yhi * height)
                       & (side * x >= xlo * width) & (side * x <= xhi * width)]
        need(len(cloud) >= 12, "template_landmark_missing:" + description)
        return cloud

    pelvis = section(1, .20, .28, -.27, .27, "pelvis")
    dome = section(1, .74, .86, -.32, .32, "dome")
    receiver = section(1, .86, .94, .30, .48, "antenna_base")
    core_z = float(np.median(pelvis[:, 2]))
    hips = np.array([0., float(np.quantile(pelvis[:, 1], .72)), core_z])
    head = np.array([0., float(np.median(dome[:, 1])), float(np.median(dome[:, 2]))])
    heads = reference.copy()
    tails = {}
    ratios = {}
    landmarks = {"hips": hips.tolist(), "dome": head.tolist(),
                 "antenna_base": [0., float(np.median(receiver[:, 1])), float(np.median(receiver[:, 2]))]}

    def remap(chain, start, end):
        a, b = reference[chain[0]], reference[chain[-1]]
        direction = b - a
        ratio = np.linalg.norm(end - start) / np.linalg.norm(direction)
        turn, _ = Rotation.align_vectors([(end-start)/np.linalg.norm(end-start)],
                                         [direction/np.linalg.norm(direction)])
        heads[list(chain)] = start + turn.apply(reference[list(chain)] - a) * ratio
        ratios[str(chain[0])] = float(ratio)

    remap(CHAINS[0], hips, head)
    tails[5] = np.array(landmarks["antenna_base"])
    for side, arm, leg in ((-1, CHAINS[1], CHAINS[3]), (1, CHAINS[2], CHAINS[4])):
        label = "right" if side == -1 else "left"
        shoulder_cloud = section(side, .56, .63, .67, .94, label + "_shoulder")
        hand_cloud = section(side, .24, .34, .80, 1.01, label + "_hand")
        ankle_cloud = section(side, .035, .11, .12, .46, label + "_ankle")
        sole_cloud = section(side, 0, .035, .12, .46, label + "_sole")
        boot_top = section(side, .165, .19, .16, .43, label + "_boot_top")
        shoulder = np.median(shoulder_cloud, axis=0)
        wrist = np.median(hand_cloud, axis=0)
        wrist[1] = np.quantile(hand_cloud[:, 1], .64)
        hip = np.array([np.median(sole_cloud[:, 0]), hips[1] - .03 * height, core_z])
        ankle = np.median(ankle_cloud, axis=0)
        ankle[1] = np.quantile(ankle_cloud[:, 1], .26)
        toe = np.array([np.median(sole_cloud[:, 0]), ankle[1], np.quantile(sole_cloud[:, 2], .77)])
        remap(arm[1:], shoulder, wrist)
        heads[arm[0]] = heads[3] + .42 * (shoulder - heads[3])
        remap(leg[:3], hip, ankle)
        # The top shaft of the boot provides a second anchor for the shorter
        # thigh on the remeshed character. Keep the template-derived knee's
        # depth offset while fitting the hip-knee and knee-ankle segments.
        heads[leg[1], 1] = np.quantile(boot_top[:, 1], .50)
        need(ankle[1] < heads[leg[1], 1] < hip[1], "template_boot_knee_outside_leg:" + label)
        heads[leg[-1]] = toe
        tails[arm[-1]] = np.array([wrist[0], np.quantile(hand_cloud[:, 1], .12), wrist[2]])
        tails[leg[-1]] = toe + np.array([0., 0., .025 * height])
        landmarks.update({label + "_shoulder": shoulder.tolist(), label + "_hand": wrist.tolist(),
                          label + "_hip": hip.tolist(), label + "_ankle": ankle.tolist(),
                          label + "_knee": heads[leg[1]].tolist(),
                          label + "_toe": toe.tolist()})
    for chain in CHAINS:
        for first, second in zip(chain, chain[1:]):
            tails[first] = heads[second]
    need(all(np.linalg.norm(tails[i] - h) > .005 for i, h in enumerate(heads)),
         "template_degenerate_fitted_bone")
    return vertices.astype("<f4"), heads, np.array([tails[i] for i in range(len(heads))]), {
        "landmarks_gltf_m": landmarks, "uniform_mesh_scale": float(scale),
        "source_origin_removed": shift.tolist(), "chain_scales": ratios,
    }


def build_skin(selected, template, mapping, geometry, faces, heads, joint_ids, weights):
    doc, data, _, _ = source_mesh(selected)
    part = primitive(doc)
    attrs = part["attributes"]
    for name, vertices in geometry.items():
        kind = "VEC" + str(vertices.shape[1])
        attrs[name] = add(doc, data, vertices, kind, "<f4", 5126, bounds=name == "POSITION")
        doc["bufferViews"][doc["accessors"][attrs[name]]["bufferView"]]["target"] = 34962
    if faces is not None:
        part["indices"] = add(doc, data, faces.ravel(), "SCALAR", "<u4", 5125)
        doc["bufferViews"][doc["accessors"][part["indices"]]["bufferView"]]["target"] = 34963
    nodes = deepcopy(template["nodes"])
    skin = deepcopy(template["skins"][0])
    mesh_node, = [n for n in nodes if "mesh" in n]
    mesh_node["name"] = doc["nodes"][0].get("name", "Studio mesh")
    doc["nodes"] = nodes
    doc["scenes"], doc["scene"] = deepcopy(template["scenes"]), template["scene"]
    doc["skins"] = [skin]
    _, index, parent = topology(doc, mapping)
    template_world = transforms(doc, parent)
    fitted = {index[f"bone_{i}"]: head for i, head in enumerate(heads)}
    for i, head in fitted.items():
        above = parent[i]
        above_world = template_world[above]
        local = np.linalg.solve(above_world[:3, :3], head - fitted.get(above, above_world[:3, 3]))
        nodes[i]["translation"] = local.tolist()
    world = transforms(doc, parent)
    bindings = np.array([np.linalg.inv(world[i]) for i in skin["joints"]])
    skin["inverseBindMatrices"] = add(doc, data, bindings.transpose(0, 2, 1).reshape(-1, 16),
                                      "MAT4", "<f4", 5126)
    slots = np.empty(22, dtype="u1")
    for joint, slot in zip(index.values(), [skin["joints"].index(i) for i in index.values()], strict=True):
        slots[int(nodes[joint]["name"].split("_")[-1])] = slot
    attrs["JOINTS_0"] = add(doc, data, slots[joint_ids], "VEC4", "u1", 5121)
    attrs["WEIGHTS_0"] = add(doc, data, weights, "VEC4", "<f4", 5126)
    for key in ("JOINTS_0", "WEIGHTS_0"):
        doc["bufferViews"][doc["accessors"][attrs[key]]["bufferView"]]["target"] = 34962
    return pack(doc, data)


def validate(candidate, template, mapping, expected_positions=None, *, rigid=False, tolerance=1e-6):
    doc, data = unpack(candidate)
    skin, index, parent = topology(doc, mapping)
    need(skin["joints"] == template["skins"][0]["joints"], "template_joint_order")
    need(all(node.get("children") == old.get("children")
             for node, old in zip(doc["nodes"], template["nodes"], strict=True)),
         "template_joint_hierarchy")
    p = primitive(doc)
    attrs = p["attributes"]
    need(not any(k.startswith(("WEIGHTS_", "JOINTS_")) and not k.endswith("_0") for k in attrs),
         "template_too_many_weight_sets")
    xyz = accessor(doc, data, attrs["POSITION"]).astype(float)
    if expected_positions is not None:
        need(np.array_equal(xyz, expected_positions), "template_positions_changed")
    weights = accessor(doc, data, attrs["WEIGHTS_0"]).astype(float)
    joints = accessor(doc, data, attrs["JOINTS_0"]).astype(int)
    need(weights.shape == joints.shape == (len(xyz), 4)
         and np.isfinite(weights).all() and weights.min() >= 0
         and joints.min() >= 0 and joints.max() < len(index), "template_invalid_weights")
    counts = np.count_nonzero(weights, axis=1)
    unweighted = int(np.count_nonzero(counts == 0))
    need(unweighted == 0, "template_unweighted_region")
    sum_error = float(np.max(abs(weights.sum(axis=1) - 1)))
    need(sum_error < 1e-6, "template_weights_not_normalized")
    if rigid:
        need(np.all(weights[:, 0] == 1) and np.all(weights[:, 1:] == 0), "template_nonrigid_weights")
        faces = accessor(doc, data, p["indices"]).reshape(-1, 3)
        primary = joints[:, 0]
        need(np.all(primary[faces] == primary[faces[:, :1]]), "rigid_triangle_crosses_bones")
    matrices = transforms(doc, parent)
    binds = accessor(doc, data, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
    world_error = np.asarray(matrices)[skin["joints"]] @ binds - np.eye(4)
    homogeneous = np.column_stack((xyz, np.ones(len(xyz))))
    actions = np.einsum("vkij,vj->vki", world_error[joints], homogeneous)[:, :, :3]
    max_influence = float(np.linalg.norm(actions[weights > 0], axis=1).max())
    max_blended = float(np.linalg.norm(np.einsum("vki,vk->vi", actions, weights), axis=1).max())
    # Bound the maximum rest displacement for any joint on the mesh's AABB.
    spans = np.array(np.meshgrid(*zip(xyz.min(axis=0), xyz.max(axis=0)))).reshape(3, -1).T
    bound = float(np.linalg.norm(np.einsum("aij,pj->api", world_error[:, :3],
                                         np.column_stack((spans, np.ones(len(spans))))), axis=2).max())
    need(np.isfinite(bound) and bound <= tolerance, "template_bind_pose_mismatch")
    return {"joints": len(index), "skin_order": [doc["nodes"][i]["name"] for i in skin["joints"]],
            "joint_glb_nodes": index, "adapter_semantics": mapping["adapter_semantics"],
            "joint_order_and_hierarchy_identical": True,
            "vertices": len(xyz), "triangles": doc["accessors"][p["indices"]]["count"] // 3,
            "weight_histogram": {str(i): int(np.count_nonzero(counts == i)) for i in range(1, 5)},
            "max_weight_sum_error": sum_error, "unweighted_vertices": unweighted,
            "max_active_joint_bind_error_m": max_influence, "max_blended_bind_error_m": max_blended,
            "max_bbox_joint_bind_error_m": bound, "bind_tolerance_m": tolerance}


def prepare(selected, template_raw, mapping, scratch):
    template, worlds, height = load_template(template_raw, mapping)
    _, _, xyz, faces = source_mesh(selected)
    reference = np.array([worlds[mapping["joint_glb_nodes"][f"bone_{i}"]][:3, 3] for i in range(22)])
    xyz, heads, tails, fitting = fit(xyz, reference, height)
    unique, remap = np.unique(xyz, axis=0, return_inverse=True)
    proxy_faces = remap[faces]
    need(np.all(np.diff(np.sort(proxy_faces, axis=1), axis=1) != 0), "template_degenerate_proxy_face")
    parents = [r["parent"] for r in mapping["adapter_map"]["joints"]]
    np.savez(scratch / "fit.npz", xyz=unique, faces=proxy_faces, remap=remap, heads=heads,
             tails=tails, parents=np.asarray([-1 if p is None else p for p in parents]))
    return template, mapping, xyz, heads, fitting


def rigid_geometry(selected, template_raw, mapping, work, tolerance):
    template, _, _, heads, fitting = prepare(selected, template_raw, mapping, work)
    doc, data, _, faces = source_mesh(selected)
    part = primitive(doc)
    need(set(part["attributes"]) == {"POSITION", "NORMAL", "TEXCOORD_0", "TANGENT"},
         "rigid_look_attribute_roster")
    attrs = {k: accessor(doc, data, v) for k, v in part["attributes"].items() if k != "POSITION"}
    with np.load(work / "fit.npz") as fit_data:
        geometry, triangles, joints, weights, partition = split_surface(
            fit_data["xyz"], faces, fit_data["remap"], attrs, heads, fit_data["parents"])
    raw = build_skin(selected, template, mapping, geometry, triangles, heads, joints, weights)
    report = validate(raw, template, mapping, geometry["POSITION"], rigid=True, tolerance=tolerance)
    _, original = unpack(selected)
    _, baked = unpack(raw)
    need(baked[:len(original)] == original, "template_look_payload_changed")
    return raw, geometry["POSITION"], joints[:, 0], fitting, partition, report


MECHANICAL_WEIGHT_POWER = 4


def mechanical_top_four(dense):
    """Sharpen Blender's strongest influences for the segmented shell."""
    order = np.argsort(-dense, axis=1, kind="stable")[:, :4]
    weights = np.take_along_axis(dense, order, axis=1) ** MECHANICAL_WEIGHT_POWER
    total = weights.sum(axis=1, keepdims=True)
    need(np.all(total > 0), "template_unweighted_region")
    return order, weights / total


def constrain_chassis_weights(dense, vertices):
    """Keep arm and leg weights off the closed central shell."""
    x, y, _ = vertices.T
    height, width = float(y.max()), float(abs(x).max())
    need(height > 0 and width > 0, "template_shell_extent")
    outside = np.maximum.reduce(((.24 * height - y) / (.03 * height),
                                 (y - .49 * height) / (.03 * height),
                                 (abs(x) - .48 * width) / (.06 * width)))
    keep = np.clip(outside, 0, 1)
    limbs = (7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21)
    corrected = np.count_nonzero((dense[:, limbs].sum(axis=1) > 0) & (keep < 1))
    dense[:, limbs] *= keep[:, None]
    stranded = dense.sum(axis=1) == 0
    dense[stranded, 1] = 1
    return int(corrected)


def auto_geometry(selected, template_raw, mapping, work, tolerance, blender):
    template, _, xyz, heads, fitting = prepare(selected, template_raw, mapping, work)
    invoke_blender(work, HERE / "auto_weights.py", [work], blender=blender)
    dense = np.load(work / "heat.npy", allow_pickle=False)
    with np.load(work / "fit.npz") as fit_data:
        proxy, remap = fit_data["xyz"], fit_data["remap"]
    need(dense.shape == (len(proxy), len(heads)) and np.isfinite(dense).all()
         and np.all(dense >= 0), "template_invalid_heat_weights")
    covered = dense.sum(axis=1) > 0
    need(np.any(covered), "template_unweighted_region")
    missing = ~covered
    if missing.any():
        # Disconnected antenna fragments may miss heat. Bound local transfer and
        # refuse unweighted vertices elsewhere, including the soles.
        need(np.all(proxy[missing, 1] >= heads[5, 1]), "template_unweighted_region")
        distance, nearest = cKDTree(proxy[covered]).query(proxy[missing])
        need(np.all(distance <= .015), "template_antenna_heat_distance")
        dense[missing] = dense[covered][nearest]
    corrected = constrain_chassis_weights(dense, proxy)
    order, weights = mechanical_top_four(dense)
    joints = order[remap]
    weights = weights[remap]
    raw = build_skin(selected, template, mapping, {"POSITION": xyz}, None, heads, joints, weights)
    report = validate(raw, template, mapping, xyz, tolerance=tolerance)
    _, original = unpack(selected)
    _, baked = unpack(raw)
    need(baked[:len(original)] == original, "template_look_payload_changed")
    return raw, xyz, joints, weights, fitting, {
        "algorithm": "blender-bone-heat", "heated_vertices": int(covered.sum()),
        "local_transfer_vertices": int(missing.sum()), "mechanical_weight_power": MECHANICAL_WEIGHT_POWER,
        "chassis_constrained_vertices": corrected,
    }, report


def qualify(selected_path, template_path, mapping_path, work, *, method, blender, tolerance):
    from pose_metrics import measure

    selected, template = selected_path.read_bytes(), template_path.read_bytes()
    mapping = document(mapping_path)
    if method == "rigid":
        raw, xyz, primary, fitting, weighting, validation = rigid_geometry(
            selected, template, mapping, work, tolerance)
    else:
        raw, xyz, joints, weights, fitting, weighting, validation = auto_geometry(
            selected, template, mapping, work, tolerance, blender)
    artifact = work / "template-rig.glb"
    artifact.write_bytes(raw)
    invoke_blender(work, HERE / "blender_template.py", ["poses", work], blender=blender)
    status, reason = "PASS", None
    pose_report = None
    try:
        if method == "rigid":
            labels = {f"bone_{j}": primary == j for j in np.unique(primary)}
            with np.load(work / "poses.npz") as poses:
                # The per-vertex part mask requires the same rest vertex order.
                neutral = poses["rest"]
                need(neutral.shape == xyz.shape, "template_rigid_vertex_count")
                need(float(np.linalg.norm(xyz - neutral, axis=1).max()) <= tolerance,
                     "template_blender_neutral_replay")
                rows = {name: distance_bounds(neutral, poses[name], labels)
                        for name in ("arms-up", "step", "jab")}
            pose_report = {"poses": rows, "rigidity_tolerance_m": RIGIDITY_M}
            need(all(value <= RIGIDITY_M for row in rows.values() for value in row.values()),
                 "template_pose_collapse")
        else:
            with np.load(work / "fit.npz") as fitted:
                pose_report = measure(work, xyz, joints, weights, fitted["heads"], fitted["tails"])
            need(not pose_report["flagged_sections"], "template_pose_collapse")
    except Hold as error:
        status, reason = "HOLD", str(error)
    return artifact, {"status": status, "pose_refusal": reason, "method": method, "rig": pin(artifact),
                      "selected": pin(selected_path), "template": pin(template_path),
                      "joint_map": pin(mapping_path), "fit": fitting, "weights": weighting,
                      "measurements": validation, "pose_report": pose_report}
