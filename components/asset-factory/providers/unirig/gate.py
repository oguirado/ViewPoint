"""G06: inspect GLB skin bytes, rest binds and input-bound LBS pose probes.

The v1 entry point checks declared probes. The scoped skin entry point checks
whole exported meshes against independent fixture bytes. Both use integer
receipts and run without Blender or the model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import struct
import time

from asset_factory import core, math_acceptance
from asset_factory.domains import PPM, UM_PER_METER
from .common import MAX_JSON_BYTES, Refusal, digest, integer, load, outcome, require, validate

MAX_GLB_BYTES = 256 * 2**20  # [TARGET: bounded work, DESIGN_CHOICE]
IDENTITY = tuple(float(i % 5 == 0) for i in range(16))
_COMPONENTS = {5121: ("B", 1), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_WIDTH = {"SCALAR": 1, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def mul(a, b):
    """Column-major glTF matrix multiplication."""
    return tuple(sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
                 for c in range(4) for r in range(4))


def point(m, p):
    return tuple(sum(m[c * 4 + r] * p[c] for c in range(3)) + m[12 + r]
                 for r in range(3))


def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def affine(m):
    require(len(m) == 16 and all(type(v) in (int, float) and math.isfinite(v) for v in m),
            2, "matrix_invalid")
    require(all(abs(m[i] - v) <= 1e-7 for i, v in zip((3, 7, 11, 15), (0, 0, 0, 1))),
            5, "matrix_not_affine")  # [TARGET: float32 affine representation]
    a, b, c, d, e, f, g, h, i = (m[k] for k in (0, 4, 8, 1, 5, 9, 2, 6, 10))
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    require(math.isfinite(determinant) and abs(determinant) > 1e-12, 5, "singular_bind_matrix")
    return tuple(m)


def node_matrix(node, *, exact=False):
    if "matrix" in node:
        require(not any(k in node for k in ("translation", "rotation", "scale")),
                2, "matrix_trs_conflict")
        matrix = affine(node["matrix"])
        if not exact:
            return matrix
        require(tuple(matrix[i] for i in (3, 7, 11, 15)) == (0, 0, 0, 1),
                5, "matrix_not_affine")
        return tuple(map(Fraction, matrix))
    t, q, s = (node.get(k, default) for k, default in
               (("translation", [0, 0, 0]), ("rotation", [0, 0, 0, 1]), ("scale", [1, 1, 1])))
    require(len(t) == 3 and len(q) == 4 and len(s) == 3, 2, "trs_invalid")
    require(all(type(v) in (int, float) and math.isfinite(v) for v in t + q + s), 2, "trs_invalid")
    require(abs(sum(v * v for v in q) - 1) <= 1e-5, 5, "rotation_not_unit")
    factor = 2
    if exact:
        t, q, s = (tuple(map(Fraction, values)) for values in (t, q, s))
        factor = 2 / sum(v*v for v in q)
    x, y, z, w = q
    r = (1-factor*(y*y+z*z), factor*(x*y+z*w), factor*(x*z-y*w), 0,
         factor*(x*y-z*w), 1-factor*(x*x+z*z), factor*(y*z+x*w), 0,
         factor*(x*z+y*w), factor*(y*z-x*w), 1-factor*(x*x+y*y), 0, *t, 1)
    matrix = tuple(r[k] * (s[k // 4] if k < 12 else 1) for k in range(16))
    if exact:
        require(all(s), 5, "singular_bind_matrix")
        return matrix
    return affine(matrix)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 2, "duplicate_key")
        result[key] = value
    return result


class GLB:
    def __init__(self, data, deadline, vertex_budget, *, exact=False):
        require(len(data) <= MAX_GLB_BYTES, 6, "glb_budget")
        require(len(data) >= 20 and struct.unpack_from("<4sII", data) == (b"glTF", 2, len(data)),
                2, "glb_header")
        chunks, offset = [], 12
        while offset < len(data):
            require(offset + 8 <= len(data), 2, "glb_chunk")
            length, kind = struct.unpack_from("<II", data, offset)
            offset += 8
            require(length % 4 == 0 and offset + length <= len(data), 2, "glb_chunk")
            chunks.append((kind, data[offset:offset + length]))
            offset += length
        require([k for k, _ in chunks] == [0x4E4F534A, 0x004E4942], 2, "glb_chunks_unsupported")
        self.doc = json.loads(chunks[0][1].decode("utf-8"), object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(Refusal(2, "nonfinite_json")))
        self.binary, self.deadline, self.vertex_budget = chunks[1][1], deadline, vertex_budget
        require(self.doc.get("asset", {}).get("version") == "2.0", 2, "gltf_version")
        require(not self.doc.get("extensionsRequired"), 3, "required_extension_unsupported")
        buffers = self.doc.get("buffers", [])
        require(len(buffers) == 1 and "uri" not in buffers[0] and
                integer(buffers[0].get("byteLength"), 0, len(self.binary)) and
                len(self.binary) - buffers[0]["byteLength"] < 4, 2, "buffer_invalid")
        self.buffer_length = buffers[0]["byteLength"]
        self.exact = exact

    def check_time(self):
        require(time.monotonic_ns() <= self.deadline, 6, "deadline_exceeded")

    def accessor(self, index, typ, components):
        self.check_time()
        accessors, views = self.doc.get("accessors", []), self.doc.get("bufferViews", [])
        require(integer(index, 0, len(accessors)-1), 2, "accessor_index")
        a = accessors[index]
        require(not any(k in a for k in ("sparse", "extensions")), 3, "accessor_unsupported")
        require(a.get("type") == typ and a.get("componentType") in components,
                2, "accessor_type")
        count, vi = a.get("count"), a.get("bufferView")
        require(integer(count, 1, self.vertex_budget), 6, "accessor_budget")
        require(integer(vi, 0, len(views)-1), 2, "buffer_view_index")
        view = views[vi]
        require(integer(view.get("buffer"), 0, 0), 2, "buffer_index")
        fmt, size = _COMPONENTS[a["componentType"]]
        width = _WIDTH[typ]
        stride, start, relative, length = (view.get("byteStride", size * width),
                                         view.get("byteOffset", 0), a.get("byteOffset", 0),
                                         view.get("byteLength"))
        require(integer(stride, size*width, 252) and stride % size == 0 and
                integer(start, 0, self.buffer_length) and integer(relative, 0, self.buffer_length) and
                integer(length, 0, self.buffer_length) and (start + relative) % size == 0 and
                start + length <= self.buffer_length and
                relative + stride*(count-1) + size*width <= length, 2, "accessor_bounds")
        normalized = a.get("normalized", False)
        require(type(normalized) is bool, 2, "normalized_invalid")
        require(not normalized or a["componentType"] in (5121, 5123), 2, "normalized_invalid")
        rows = []
        for n in range(count):
            if n % 1024 == 0:
                self.check_time()
            row = struct.unpack_from("<" + fmt*width, self.binary, start + relative + n*stride)
            require(all(math.isfinite(x) for x in row), 2, "nonfinite_accessor")
            if normalized:
                denominator = (1 << (8 * size)) - 1
                row = tuple(Fraction(x, denominator) if self.exact else x / denominator for x in row)
            elif self.exact and fmt == "f":
                row = tuple(map(Fraction, row))
            rows.append(row)
        return rows, normalized

    def hierarchy(self, overrides=None, *, local_deltas=None):
        nodes = self.doc.get("nodes", [])
        require(isinstance(nodes, list) and 0 < len(nodes) <= 4096, 6, "node_budget")
        parents = [None] * len(nodes)
        for p, node in enumerate(nodes):
            for child in node.get("children", []):
                require(integer(child, 0, len(nodes)-1) and parents[child] is None,
                        5, "hierarchy_invalid")
                parents[child] = p
        matrices = [None] * len(nodes)
        for n in range(len(nodes)):
            trail, current = [], n
            while current is not None and matrices[current] is None:
                require(current not in trail, 5, "hierarchy_cycle")
                trail.append(current)
                current = parents[current]
            for current in reversed(trail):
                if self.exact:
                    self.check_time()
                local = node_matrix(nodes[current], exact=self.exact)
                if overrides is not None and str(current) in overrides:
                    local = affine(tuple(x / 1000000 for x in overrides[str(current)]))
                if local_deltas is not None and current in local_deltas:
                    local = mul(local, local_deltas[current])
                parent = parents[current]
                matrices[current] = local if parent is None else mul(matrices[parent], local)
        self.check_time()
        return parents, matrices

    def scene_nodes(self, parents):
        """Validate references before deriving coverage from the selected scene."""
        nodes = self.doc["nodes"]
        meshes, skins = (self.doc.get(k, []) for k in ("meshes", "skins"))
        require(type(meshes) is list and type(skins) is list, 2, "scene_references_invalid")
        for node in nodes:
            if "skin" in node:
                require(integer(node["skin"], 0, len(skins)-1), 2, "skin_index")
            if "mesh" in node:
                require(integer(node["mesh"], 0, len(meshes)-1), 2, "mesh_index")
                require("skin" in node, 5, "unskinned_mesh")
        scenes = self.doc.get("scenes", [])
        require(type(scenes) is list, 2, "scenes_invalid")
        for scene in scenes:
            require(type(scene) is dict, 2, "scene_invalid")
            roots = scene.get("nodes", [])
            require(type(roots) is list and
                    all(integer(n, 0, len(nodes)-1) for n in roots), 2, "scene_root_invalid")
            require(len(set(roots)) == len(roots) and
                    all(parents[n] is None for n in roots), 2, "scene_root_invalid")
        require("scene" in self.doc, 3, "active_scene_missing")
        selected = self.doc["scene"]
        require(integer(selected, 0, len(scenes)-1), 2, "scene_index")
        active, pending = set(), list(scenes[selected].get("nodes", []))
        while pending:
            self.check_time()
            node = pending.pop()
            if node not in active:
                active.add(node)
                pending.extend(nodes[node].get("children", []))
        if self.exact:
            require(any("mesh" in nodes[n] for n in active), 5, "skinned_mesh_missing")
        else:
            require(any("mesh" in nodes[n] for n in active), 3, "active_mesh_missing")
        return active


def _policy(policy):
    validate(policy, "policy")
    require(policy["threshold_status"] == "DESIGN_CHOICE_UNCALIBRATED", 2, "threshold_provenance")
    for key, lo, hi in (("weight_sum_error_ppm", 0, 1), ("bind_error_um", 0, 1000),
                        ("deformation_error_um", 0, 1000), ("max_vertices", 1, 1000000),
                        ("timeout_s", 1, 300)):
        require(integer(policy[key], lo, hi), 2, "policy_invalid")


def _read_influences(glb, attrs, count, joint_count, pack_slots, work):
    prefixes = ("JOINTS_", "WEIGHTS_")
    skin_keys = [k for k in attrs if k.startswith(prefixes)]
    set_count = len(skin_keys) // 2
    sets = [(f"JOINTS_{i}", f"WEIGHTS_{i}") for i in range(set_count)]
    require(len(skin_keys) == 2 * set_count
            and all(j in attrs and w in attrs for j, w in sets), 2, "skin_influence_sets_invalid")
    influences = [[] for _ in range(count)]
    for joint_key, weight_key in sets:
        work(count)
        indices, weights = _influence_set(glb, attrs, joint_key, weight_key, count)
        for row, js, ws in zip(influences, indices, weights):
            require(all(integer(j, 0, joint_count-1) for j in js), 5, "joint_index")
            require(all(0 <= w <= 1 for w in ws), 5, "weight_invalid")
            row.extend((j, w) for j, w in zip(js, ws) if w)
            require(len(row) <= pack_slots, 5, "skin_influence_cap")
    return ([tuple(j for j, _ in row) for row in influences],
            [tuple(w for _, w in row) for row in influences])


def _influence_set(glb, attrs, joint_key, weight_key, count):
    indices, normj = glb.accessor(attrs[joint_key], "VEC4", (5121, 5123))
    weights, normw = glb.accessor(attrs[weight_key], "VEC4", (5121, 5123, 5126))
    wa = glb.doc["accessors"][attrs[weight_key]]
    require(not normj and (wa["componentType"] == 5126 or normw), 2, "skin_normalization")
    require(count == len(indices) == len(weights), 5, "attribute_count")
    return indices, weights


def _read_rig(glb, policy, metrics, *, pack_slots=None, work=None):
    parents, world = glb.hierarchy()
    nodes, skins, meshes = (glb.doc.get(k, []) for k in ("nodes", "skins", "meshes"))
    require(len(skins) == 1, 5 if not skins else 3, "skin_missing" if not skins else "multiple_skins_unsupported")
    skin = skins[0]
    joints = skin.get("joints", [])
    require(joints and all(integer(n, 0, len(nodes)-1) for n in joints) and
            len(set(joints)) == len(joints), 5, "joints_invalid")
    if "skeleton" in skin:
        root = skin["skeleton"]
        require(integer(root, 0, len(nodes)-1), 2, "skeleton_root_invalid")
        for joint in joints:
            current = joint
            while current is not None and current != root:
                current = parents[current]
            require(current == root, 5, "skeleton_root_not_ancestor")
    active = glb.scene_nodes(parents)
    require(all(joint in active for joint in joints), 3, "active_joints_missing")
    require("inverseBindMatrices" in skin, 3, "inverse_binds_missing")
    ibm, normalized = glb.accessor(skin["inverseBindMatrices"], "MAT4", (5126,))
    require(not normalized and len(ibm) == len(joints), 5, "inverse_bind_count")
    if glb.exact:
        from tools import rig_math

        require(all(rig_math.is_rigid_rotation(world[j]) for j in joints),
                5, "skin_joint_world_not_rigid")
        for matrix in ibm:
            require(tuple(matrix[i] for i in (3, 7, 11, 15)) == (0, 0, 0, 1),
                    5, "matrix_not_affine")
            require(rig_math.det(tuple(matrix[c:c+3] for c in (0, 4, 8))) != 0,
                    5, "singular_bind_matrix")
    else:
        ibm = [affine(m) for m in ibm]
    palettes = [mul(world[j], m) for j, m in zip(joints, ibm)]
    metrics["joints"], metrics["vertices"] = len(joints), 0
    records, faces, bind_error, weight_error = {}, {}, 0, 0

    def bind_distance(a, b):
        if glb.exact:
            return rig_math.norm2(rig_math.sub(a, b)) * UM_PER_METER**2
        return distance(a, b) * UM_PER_METER
    for ni, node in enumerate(nodes):
        if ni not in active or "mesh" not in node:
            continue
        require(node.get("skin") == 0, 5, "unskinned_mesh")
        require(integer(node["mesh"], 0, len(meshes)-1), 2, "mesh_index")
        primitives = meshes[node["mesh"]].get("primitives", [])
        require(primitives, 5, "empty_mesh")
        for pi, primitive in enumerate(primitives):
            glb.check_time()
            require(primitive.get("mode", 4) == 4 and not primitive.get("targets") and
                    not primitive.get("extensions"), 3, "primitive_unsupported")
            attrs = primitive.get("attributes", {})
            require(all(k in attrs for k in ("POSITION", "JOINTS_0", "WEIGHTS_0")),
                    5, "skinning_attributes_missing")
            if not glb.exact:
                require(not any(k.startswith(("JOINTS_", "WEIGHTS_")) and
                                k not in ("JOINTS_0", "WEIGHTS_0") for k in attrs),
                        3, "extra_influences_unsupported")
            pos, _ = glb.accessor(attrs["POSITION"], "VEC3", (5126,))
            if "indices" in primitive:
                triangles, normalized_indices = glb.accessor(primitive["indices"], "SCALAR", (5121, 5123, 5125))
                require(not normalized_indices and len(triangles) >= 3 and len(triangles) % 3 == 0 and
                        all(integer(row[0], 0, len(pos)-1) for row in triangles), 2, "triangle_indices_invalid")
            else:
                require(len(pos) >= 3 and len(pos) % 3 == 0, 5, "triangles_missing")
                triangles = [(i,) for i in range(len(pos))]
            if glb.exact:
                require(len({row[0] for row in triangles}) == len(pos), 5, "skin_unused_vertex")
                faces[ni, pi] = tuple(tuple(row[0] for row in triangles[i:i+3])
                                      for i in range(0, len(triangles), 3))
            if glb.exact:
                indices, weights = _read_influences(glb, attrs, len(pos), len(joints), pack_slots, work)
            else:
                indices, weights = _influence_set(glb, attrs, "JOINTS_0", "WEIGHTS_0", len(pos))
            metrics["vertices"] += len(pos)
            require(metrics["vertices"] <= policy["max_vertices"], 6, "vertex_budget")
            # An off-axis or unused joint's broken bind must not be hidden
            # by a degenerate point set or cancellation between weights.
            for palette in palettes:
                if glb.exact:
                    glb.check_time()
                for basis in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)):
                    bind_error = max(bind_error, bind_distance(point(palette, basis), point(world[ni], basis)))
            for v, (p, js, ws) in enumerate(zip(pos, indices, weights)):
                if v % 1024 == 0:
                    glb.check_time()
                require(all(integer(j, 0, len(joints)-1) for j in js), 5, "joint_index")
                require(all(0 <= w <= 1 for w in ws) and sum(ws) > 0, 5, "weight_invalid")
                error = abs(sum(ws) - 1) * 1000000
                weight_error = max(weight_error, error)
                require(error <= (0 if glb.exact else policy["weight_sum_error_ppm"]), 5, "weight_sum")
                expected = point(world[ni], p)
                for j, weight in zip(js, ws):
                    if weight:
                        # Check each contributing bind, not only their possibly cancelling average.
                        bind_error = max(bind_error, bind_distance(point(palettes[j], p), expected))
            records[(ni, pi)] = (pos, indices, weights)
    require(records and metrics["vertices"] > 0, 5, "skinned_mesh_missing")
    if glb.exact:
        bind_error = rig_math.ceil_sqrt(bind_error)
    metrics.update(weight_sum_error_ppm=math.ceil(weight_error), bind_error_um=math.ceil(bind_error))
    require(bind_error <= policy["bind_error_um"], 5, "bind_pose_mismatch")
    return parents, world, joints, ibm, records, faces


def inspect_bytes(data, *, policy, probes=None):
    """Return (gate receipt, skeleton). Never repairs or normalizes bad weights."""
    start = time.monotonic_ns()
    source = hashlib.sha256(data).hexdigest()
    metrics = {"vertices": None, "joints": None, "weight_sum_error_ppm": None,
               "bind_error_um": None, "deformation_error_um": None, "deformation_samples": 0}
    skeleton, rc, reasons = None, 0, []
    try:
        _policy(policy)
        glb = GLB(data, start + policy["timeout_s"]*10**9, policy["max_vertices"])
        parents, world, joints, ibm, records, _ = _read_rig(glb, policy, metrics)
        nodes = glb.doc["nodes"]
        skeleton = {"schema": "af.rig-skeleton/v1", "rigged_sha256": source,
                    "skinning": "gltf_linear_blend", "joints": [
                        {"node": n, "name": nodes[n].get("name", ""), "parent_node": parents[n],
                         "inverse_bind_ppm": [round(x*1000000) for x in m]}
                        for n, m in zip(joints, ibm)]}
        validate(skeleton, "skeleton")
        require(probes is not None, 3, "deformation_reference_missing")
        validate(probes, "probes")
        require(probes["rigged_sha256"] == source and probes["policy_sha256"] == core.sha256_json(policy),
                4, "deformation_reference_stale")
        require(probes["reference_kind"] == "independent_expected_positions", 3, "independent_reference_missing")
        require(digest(probes["reference_sha256"]), 2, "reference_identity_invalid")
        require(probes["reference_sha256"] == core.sha256_json({"poses": probes["poses"]}),
                4, "reference_bytes_stale")
        require(probes["poses"], 3, "deformation_poses_missing")
        require(len(probes["poses"]) <= 32, 6, "pose_budget")
        maximum, movement = 0.0, False
        seen_names = set()
        for pose in probes["poses"]:
            require(pose["name"] and pose["name"] not in seen_names, 2, "pose_name_invalid")
            seen_names.add(pose["name"])
            overrides = pose["local_matrices_ppm"]
            require(overrides, 3, "pose_missing")
            for key, values in overrides.items():
                require(key.isdigit() and str(int(key)) == key and
                        integer(int(key), 0, len(nodes)-1) and len(values) == 16,
                        2, "pose_matrix_invalid")
            _, posed_world = glb.hierarchy(overrides)
            palette = [mul(posed_world[j], m) for j, m in zip(joints, ibm)]
            require(pose["samples"], 3, "deformation_samples_missing")
            seen = set()
            for sample in pose["samples"]:
                glb.check_time()
                key = (sample["mesh_node"], sample["primitive"])
                vertex = sample["vertex"]
                require(key in records and integer(vertex, 0, len(records[key][0])-1) and
                        (*key, vertex) not in seen and len(sample["expected_um"]) == 3,
                        2, "deformation_sample_invalid")
                seen.add((*key, vertex))
                pos, js, ws = (rows[vertex] for rows in records[key])
                predicted = tuple(sum(w * point(palette[j], pos)[axis] for j, w in zip(js, ws))
                                  for axis in range(3))
                expected = tuple(v/1000000 for v in sample["expected_um"])
                movement |= distance(point(world[key[0]], pos), expected) > 1e-6
                maximum = max(maximum, distance(predicted, expected)*1000000)
                metrics["deformation_samples"] += 1
                require(metrics["deformation_samples"] <= policy["max_vertices"], 6, "probe_budget")
        require(movement, 3, "deformation_unobserved")
        metrics["deformation_error_um"] = math.ceil(maximum)
        require(maximum <= policy["deformation_error_um"], 5, "deformation_mismatch")
        glb.check_time()
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.code]
    except core.ContractError as exc:
        rc, reasons = 2, [exc.code]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError, struct.error):
        rc, reasons = 2, ["glb_or_probe_invalid"]
    try:
        policy_hash, probe_hash = core.sha256_json(policy), core.sha256_json(probes) if probes is not None else None
    except core.ContractError:
        rc, reasons, policy_hash, probe_hash = 2, ["metadata_invalid"], None, None
    receipt = outcome("af.rig-gate/v1", rc, reasons, rigged_sha256=source,
                      policy_sha256=policy_hash, probes_sha256=probe_hash,
                      metrics=metrics, wall_ms=(time.monotonic_ns()-start)//1000000,
                      absence_reasons={k: "not_measured_before_refusal" for k, v in metrics.items() if v is None},
                      scope="G06-structural-binds-and-declared-LBS-probes",
                      threshold_status="DESIGN_CHOICE_UNCALIBRATED")
    return receipt, skeleton if rc == 0 else None


_SKIN_METRICS = (
    "vertices", "joints", "weight_sum_error_ppm", "bind_error_um", "bone_segment_count",
    "bone_clearance_lower_um", "bone_symmetry_error_upper_ppm", "locality_leak_upper_ppm",
    "skin_edge_gradient_upper", "posed_edge_length_upper_um", "discarded_mass_upper_ppm",
    "weight_l1_upper_ppm", "skin_position_error_upper_um", "packed_pose_error_upper_um",
    "pose_coverage_lower_ppm",
    "posed_vertex_count", "pose_error_upper_um", "minimum_jacobian_lower_ppm",
    "deformation_volume_loss_upper_ppm", "deformation_volume_gain_upper_ppm",
    "deformation_intersections", "tetrahedra", "pack_influences", "quota_denominator",
    "joint_origin_error_upper_um",
)


def _skin_fixture(read_checked, pin):
    require(callable(read_checked) and isinstance(pin, str) and pin.startswith("sha256:")
            and digest(pin[7:]), 2, "skin_fixture_pin_invalid")
    try:
        raw = read_checked("skin-fixture", pin)
    except OSError:
        raw = None
    require(raw is not None, 3, "skin_fixture_unavailable")
    require(type(raw) is bytes, 2, "skin_fixture_reader_invalid")
    require(len(raw) <= MAX_JSON_BYTES, 6, "skin_fixture_budget")
    require(hashlib.sha256(raw).hexdigest() == pin[7:], 4, "skin_fixture_stale")
    fixture = json.loads(raw, object_pairs_hook=_pairs)
    require(raw == core.canonical_json(fixture).encode(), 2, "skin_fixture_not_canonical")
    if isinstance(fixture, dict) and fixture.get("schema") == "af.skin-anatomy-fixture/v1":
        raise Refusal(3, "skin_fixture_local_pose_required")
    validate(fixture, "skin_fixture")
    return fixture


def _skin_policy(policy):
    _policy(policy)
    limits = policy.get("skin_fixture")
    require(limits is not None, 3, "skin_fixture_policy_missing")
    require(all(v >= 0 for k, v in limits.items()
                if k not in ("required_pose_joints", "volume_change_status"))
            and 1 <= limits["pack_slots"] <= 4 and 1 <= limits["pack_denominator"] <= 65535
            and 0 <= limits["symmetry_max_ppm"] < 2*PPM
            and 0 < limits["stretch_min_ppm"] <= PPM <= limits["stretch_max_ppm"]
            and limits["work_budget"] > 0, 2, "skin_fixture_policy_invalid")
    poses = limits["required_pose_joints"]
    require(poses.keys() >= {"elbows", "hands", "knees", "face_attachment"}
            and all(len(set(joints)) == len(joints) for joints in poses.values()),
            2, "skin_fixture_pose_policy_invalid")
    return limits


def _skin_mesh(glb, parents, world, joints, records, faces, fixture, limits, metrics, work):
    from tools import rig_math

    vertices = tuple(tuple(p) for p in fixture["vertices_um"])
    lookup = {p: i for i, p in enumerate(vertices)}
    require(len(lookup) == len(vertices) and all(len(p) == 3 for p in vertices),
            2, "skin_fixture_vertices_invalid")
    names = [glb.doc["nodes"][j].get("name") for j in joints]
    origins = fixture["joint_origins"]
    require(len(set(names)) == len(names) and set(names) == origins.keys(),
            5, "skin_anatomy_joint_identity")
    names_by_node = dict(zip(joints, names))
    joint_parents = {}
    for joint, name in names_by_node.items():
        parent = parents[joint]
        while parent is not None and parent not in names_by_node:
            work()
            parent = parents[parent]
        joint_parents[name] = names_by_node.get(parent)
    require(joint_parents == fixture["joint_parents"], 5, "skin_anatomy_joint_hierarchy")
    supports = fixture["allowed_joints"]
    require(len(supports) == len(vertices)
            and all(len(set(s)) == len(s) and set(s) <= origins.keys() for s in supports),
            3, "skin_anatomy_coverage_missing")
    bound_faces, variants = Counter(), [[] for _ in vertices]
    maximum_leak = 0
    for key, (positions, indices, weights) in records.items():
        mesh_world = world[key[0]]
        position_ids = []
        for pos, row, magnitudes in zip(positions, indices, weights):
            work()
            rest = tuple(v*UM_PER_METER for v in point(mesh_world, pos))
            require(rest in lookup, 4, "skin_rest_registration_stale")
            i = lookup[rest]
            position_ids.append(i)
            weighted = Counter()
            for j, w in zip(row, magnitudes):
                if w:
                    weighted[names[j]] += w
            maximum_leak = max(maximum_leak, sum(w for n, w in weighted.items() if n not in supports[i]))
            variants[i].append((pos, weighted))
        for triangle in faces[key]:
            ids = tuple(position_ids[i] for i in triangle)
            require(len(set(ids)) == 3, 5, "skin_degenerate_surface")
            bound_faces[rig_math.cycle_face(ids)] += 1
    require(all(variants), 3, "skin_rest_vertices_missing")
    metrics["locality_leak_upper_ppm"] = math.ceil(maximum_leak*PPM)
    require(maximum_leak == 0, 5, "skin_locality_leak")

    tetrahedra = [(r["part"], tuple(r["vertices"])) for r in fixture["tetrahedra"]]
    for part, tet in tetrahedra:
        work()
        require(part and len(tet) == 4 and len(set(tet)) == 4
                and all(integer(i, 0, len(vertices)-1) for i in tet), 2, "skin_rest_tet_invalid")
        require(rig_math.det(rig_math.edges(tuple(vertices[i] for i in tet))) > 0,
                5, "skin_rest_tet_invalid")
    ids = [tet for _, tet in tetrahedra]
    boundary = rig_math.boundary_faces(ids)
    require(Counter(boundary) == bound_faces, 4, "skin_rest_surface_stale")
    rig_math.check_intersections(ids, boundary, vertices, work)
    metrics["tetrahedra"] = len(ids)
    weights = [variants_at_vertex[0][1] for variants_at_vertex in variants]
    require(all(all(w == row[0][1] for _, w in row) for row in variants),
            5, "skin_seam_weight_mismatch")
    edges = {tuple(sorted((face[i-1], face[i]))) for face in boundary for i in range(3)}
    gradient, edge_data = 0, []
    for a, b in sorted(edges):
        work()
        separation = rig_math.norm2(rig_math.sub(vertices[a], vertices[b]))
        support = weights[a].keys() | weights[b].keys()
        delta = sum(abs(weights[a][j] - weights[b][j]) for j in support)
        gradient = max(gradient, rig_math.ceil_sqrt(Fraction((delta*PPM)**2, separation)))
        edge_data.append((a, b, sorted(support), delta))
    metrics["skin_edge_gradient_upper"] = gradient
    require(gradient <= limits["gradient_max_ppm_per_um"], 5, "skin_edge_gradient")
    return names, vertices, tetrahedra, boundary, variants, edge_data


def _skin_bones(names, joints, world, vertices, tetrahedra, boundary, fixture, limits,
                metrics, work):
    from tools import rig_math

    head = {n: tuple(v*UM_PER_METER for v in point(world[j], (0, 0, 0))) for n, j in zip(names, joints)}
    # The fixture parent map has already been matched to the candidate hierarchy.
    # Every nonleaf edge uses its candidate child head, including at branches.
    ends = {name: [] for name in names}
    for child, parent in fixture["joint_parents"].items():
        if parent is not None:
            ends[parent].append(head[child])
    tails = fixture["leaf_tails_um"]
    require(tails.keys() == {n for n, children in ends.items() if not children},
            3, "skin_bone_coverage_missing")
    for name, tail in tails.items():
        require(len(tail) == 3, 2, "skin_bone_endpoint_invalid")
        ends[name].append(tuple(tail))
    source = [tuple(vertices[i] for i in tet) for _, tet in tetrahedra]
    surface = [tuple(vertices[i] for i in face) for face in boundary]
    clearances, lengths = [], {}
    for name, endpoints in ends.items():
        for end in endpoints:
            clearances.append(rig_math.bone_clearance(head[name], end, source, surface,
                                                       limits["clearance_um"], work))
        if len(endpoints) == 1:
            lengths[name] = rig_math.norm2(rig_math.sub(head[name], endpoints[0]))
    origin_errors_squared = {}
    for name, reference in fixture["joint_origins"].items():
        require(len(reference) == 3, 2, "skin_joint_reference_invalid")
        origin_errors_squared[name] = rig_math.norm2(rig_math.sub(head[name], reference))
    origin_error = max(origin_errors_squared.values())
    metrics["joint_origin_error_upper_um"] = rig_math.ceil_sqrt(origin_error)
    require(origin_error <= limits["joint_origin_max_um"]**2, 5, "skin_joint_origin_mismatch")
    symmetry = 0
    paired = set()
    for pair in fixture["symmetric_pairs"]:
        require(len(pair) == 2 and pair[0] != pair[1] and set(pair) <= lengths.keys()
                and not paired.intersection(pair), 2, "skin_symmetry_pair_invalid")
        paired.update(pair)
        short, long = sorted(lengths[n] for n in pair)
        limit = limits["symmetry_max_ppm"]
        require(long*(2*PPM-limit)**2 <= short*(2*PPM+limit)**2, 5, "skin_bone_asymmetry")
        if long != short:
            hi, lo = rig_math.ceil_sqrt(long), rig_math.floor_sqrt(short)
            symmetry = max(symmetry, math.ceil(Fraction(2*PPM*(hi-lo), hi+lo)))
    metrics.update(bone_segment_count=len(clearances), bone_clearance_lower_um=min(clearances),
                   bone_symmetry_error_upper_ppm=symmetry)
    return origin_errors_squared


def _pose_deltas(pose, expected_joints, names):
    from tools import rig_math

    deltas = {r["joint"]: r["matrix_ppm"] for r in pose["joint_deltas"]}
    require(set(deltas) == set(expected_joints) and len(deltas) == len(pose["joint_deltas"])
            and set(deltas) <= set(names), 3, "skin_pose_joint_coverage_missing")
    result = {}
    for name, values in deltas.items():
        require(len(values) == 16, 2, "skin_pose_matrix_invalid")
        matrix = tuple(Fraction(v, PPM) for v in values)
        require(tuple(matrix[i] for i in (3, 7, 11, 12, 13, 14, 15)) == (0, 0, 0, 0, 0, 0, 1)
                and rig_math.is_rigid_rotation(matrix), 5, "skin_pose_not_rigid")
        result[name] = matrix
    return result


def _pose_placement_budgets(deltas, joint_parents, origin_errors_squared, work):
    from tools import rig_math

    pivot_budgets = {
        name: rig_math.pivot_displacement_upper_um(delta, origin_errors_squared[name])
        for name, delta in deltas.items()
    }
    # The ancestry is already matched to the decoded hierarchy. In world
    # coordinates each posed pivot contributes a translation to its chain.
    # Earlier rotations preserve that translation's norm.
    budgets = {None: 0}
    for joint in joint_parents:
        trail = []
        current = joint
        while current not in budgets:
            work()
            trail.append(current)
            current = joint_parents[current]
        for name in reversed(trail):
            budgets[name] = budgets[joint_parents[name]] + pivot_budgets.get(name, 0)
    return budgets


def _skin_poses(glb, rig, joints, ibm, fixture, origin_errors_squared, limits, metrics, checked, work):
    from tools import rig_math

    names, vertices, tetrahedra, boundary, variants, edge_data = rig
    poses = {p["id"]: p for p in fixture["poses"]}
    required = limits["required_pose_joints"]
    require(poses.keys() == required.keys() and len(poses) == len(fixture["poses"]),
            3, "skin_pose_coverage_missing")
    metrics["pack_influences"] = limits["pack_slots"]
    metrics["quota_denominator"] = limits["pack_denominator"]
    packing = [rig_math.pack_weights(row[0][1], limits["pack_slots"], limits["pack_denominator"])
               for row in variants]
    metrics["discarded_mass_upper_ppm"] = math.ceil(max(row[1] for row in packing)*PPM)
    metrics["weight_l1_upper_ppm"] = math.ceil(max(row[2] for row in packing)*PPM)
    upper_error = packing_bound = packed_error = edge_bound = loss = gain = 0
    pose_within_budget = True
    jacobian = None
    count = 0
    for name, expected_joints in sorted(required.items()):
        pose = poses[name]
        expected = pose["expected_um"]
        require(len(expected) == len(vertices) and all(len(v) == 3 for v in expected),
                3, "skin_pose_vertices_missing")
        deltas = _pose_deltas(pose, expected_joints, names)
        require(all(any(list(vertices[i]) != expected[i] and any(w.get(joint, 0) for _, w in row)
                        for i, row in enumerate(variants)) for joint in expected_joints),
                3, "skin_pose_movement_missing")
        local_deltas = {j: deltas[n] for n, j in zip(names, joints) if n in deltas}
        _, posed_world = glb.hierarchy(local_deltas=local_deltas)
        palette = {n: mul(posed_world[j], m) for n, j, m in zip(names, joints, ibm)}
        chain_budgets = _pose_placement_budgets(
            deltas, fixture["joint_parents"], origin_errors_squared, work)
        posed = []
        for i, row in enumerate(variants):
            work(len(row))
            value = None
            quotas, removed, quantization = packing[i]
            for position, weights in row:
                points = {j: tuple(v*UM_PER_METER for v in point(palette[j], position)) for j in weights}
                p = tuple(sum(w*points[j][axis] for j, w in weights.items()) for axis in range(3))
                require(value is None or p == value, 5, "skin_seam_deformation")
                value = p
                bound = rig_math.displacement_squared(points.values(), removed, quantization)
                packed = tuple(sum(quotas[j]*points[j][axis] for j in quotas) / limits["pack_denominator"]
                               for axis in range(3))
                packed_delta = rig_math.norm2(rig_math.sub(p, packed))
                require(packed_delta <= bound, 2, "skin_pack_certificate_invalid")
                packing_bound = max(packing_bound, bound)
                packed_error = max(packed_error, packed_delta)
                error = rig_math.norm2(rig_math.sub(p, expected[i]))
                upper_error = max(upper_error, error)
                placement_budget = sum(w * chain_budgets[j] for j, w in weights.items())
                if error > (limits["pose_error_um"] + placement_budget)**2:
                    pose_within_budget = False
                count += 1
            posed.append(value)
        # Interior and boundary checks precede reference deltas so a collapsed
        # or intersecting mesh gets its own witnessed failure.
        work(len(tetrahedra))
        lower, part_loss, part_gain = rig_math.pose_volume(
            tetrahedra, vertices, posed, Fraction(limits["stretch_min_ppm"], PPM),
            Fraction(limits["stretch_max_ppm"], PPM))
        jacobian = lower if jacobian is None else min(jacobian, lower)
        loss, gain = max(loss, part_loss), max(gain, part_gain)
        rig_math.check_intersections([tet for _, tet in tetrahedra], boundary, posed, work)
        for a, b, support, delta in edge_data:
            work()
            pos_a, weights_a = variants[a][0]
            pos_b = variants[b][0][0]
            at_a = [point(palette[j], pos_a) for j in support]
            at_b = [point(palette[j], pos_b) for j in support]
            shared = tuple(UM_PER_METER * sum(weights_a[j] * (u[axis] - v[axis])
                                               for j, u, v in zip(support, at_a, at_b))
                           for axis in range(3))
            extra = rig_math.displacement_squared(at_b, 0, delta) * UM_PER_METER**2
            edge_bound = max(edge_bound, rig_math.ceil_sqrt(rig_math.norm2(shared))
                             + rig_math.ceil_sqrt(extra))
        checked.append(name)
    metrics.update(pose_coverage_lower_ppm=PPM, posed_vertex_count=count,
                   pose_error_upper_um=rig_math.ceil_sqrt(upper_error),
                   skin_position_error_upper_um=rig_math.ceil_sqrt(packing_bound),
                   packed_pose_error_upper_um=rig_math.ceil_sqrt(packed_error),
                   posed_edge_length_upper_um=edge_bound,
                   minimum_jacobian_lower_ppm=math.floor(jacobian*PPM),
                   deformation_volume_loss_upper_ppm=math.ceil(loss*PPM),
                   deformation_volume_gain_upper_ppm=math.ceil(gain*PPM),
                   deformation_intersections=0)
    require(max(loss, gain)*PPM <= limits["volume_change_max_ppm"], 5, "skin_deformation_volume")
    require(pose_within_budget, 5, "skin_pose_displacement")
    require(packed_error <= limits["pack_error_um"]**2, 5, "skin_pack_displacement")
    require(packing_bound <= limits["pack_error_um"]**2, 3, "skin_pack_bound_inconclusive")


def inspect_skin_bytes(data, *, policy, fixture_digest, read_checked):
    """Evaluate a fixed independent fixture under exact mathematical LBS.

    The caller supplies a policy-owner digest and a trusted, role-filtered
    reader. The v2 fixture provides joint-local rotations applied after each
    GLB bind-local matrix. The receipt covers the listed poses on the subject
    GLB and its registered tetrahedral fixture.
    """
    start = time.monotonic_ns()
    metrics = dict.fromkeys(_SKIN_METRICS)
    rc, reasons, policy_digest, work_count, checked = 0, [], None, 0, []
    try:
        limits = _skin_policy(policy)
        policy_digest = math_acceptance.canonical_digest(policy)
        fixture = _skin_fixture(read_checked, fixture_digest)
        glb = GLB(data, start + policy["timeout_s"]*10**9, policy["max_vertices"], exact=True)

        def work(n=1):
            nonlocal work_count
            work_count += n
            require(work_count <= limits["work_budget"], 6, "skin_fixture_work_budget")
            glb.check_time()

        parents, world, joints, ibm, records, faces = _read_rig(
            glb, policy, metrics, pack_slots=limits["pack_slots"], work=work)
        rig = _skin_mesh(glb, parents, world, joints, records, faces, fixture, limits, metrics, work)
        names, vertices, tetrahedra, boundary, _, _ = rig
        origin_errors_squared = _skin_bones(
            names, joints, world, vertices, tetrahedra, boundary, fixture, limits, metrics, work)
        _skin_poses(glb, rig, joints, ibm, fixture, origin_errors_squared, limits, metrics, checked, work)
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.code]
    except core.ContractError as exc:
        rc, reasons = 2, [exc.code]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError,
            RecursionError, struct.error):
        rc, reasons = 2, ["skin_input_invalid"]
    valid_pin = isinstance(fixture_digest, str) and fixture_digest.startswith("sha256:") and digest(fixture_digest[7:])
    return outcome("af.skin-gate/v1", rc, reasons, rigged_sha256=hashlib.sha256(data).hexdigest(),
                   policy_digest=policy_digest, fixture_digest=fixture_digest if valid_pin else None,
                   scope="independent-fixture-discrete-exact-LBS",
                   threshold_status="DESIGN_CHOICE_UNCALIBRATED", metrics=metrics, checked_pose_ids=checked,
                   wall_ns=time.monotonic_ns()-start, work_count=work_count,
                   absence_reasons={k: "not_measured_before_refusal" for k, v in metrics.items() if v is None})

def evaluate(path, *, policy, probes=None):
    path = Path(path)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_GLB_BYTES + 1)
        return inspect_bytes(data, policy=policy, probes=probes)
    except OSError:
        return outcome("af.rig-gate/v1", 3, ["input_unavailable"]), None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--probes")
    args = parser.parse_args(argv)
    try:
        result, _ = evaluate(args.input, policy=load(args.policy),
                             probes=load(args.probes) if args.probes else None)
    except (core.ContractError, OSError, ValueError):
        result = outcome("af.rig-gate/v1", 2, ["metadata_invalid"])
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
