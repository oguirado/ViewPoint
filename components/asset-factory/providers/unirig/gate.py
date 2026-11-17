"""G06: inspect GLB skin bytes, rest binds and input-bound LBS pose probes.

No model or Blender imports. Numeric GLB data stays internal; receipts contain
integer measurements. PASS covers the declared probes, never anatomical or
appearance approval. Missing independent probes is CANNOT_SEE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import time

from asset_factory import core
from .common import Refusal, digest, integer, load, outcome, require, validate

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


def node_matrix(node):
    if "matrix" in node:
        require(not any(k in node for k in ("translation", "rotation", "scale")),
                2, "matrix_trs_conflict")
        return affine(node["matrix"])
    t, q, s = (node.get(k, default) for k, default in
               (("translation", [0, 0, 0]), ("rotation", [0, 0, 0, 1]), ("scale", [1, 1, 1])))
    require(len(t) == 3 and len(q) == 4 and len(s) == 3, 2, "trs_invalid")
    require(all(type(v) in (int, float) and math.isfinite(v) for v in t + q + s), 2, "trs_invalid")
    require(abs(sum(v * v for v in q) - 1) <= 1e-5, 5, "rotation_not_unit")
    x, y, z, w = q
    r = (1-2*(y*y+z*z), 2*(x*y+z*w), 2*(x*z-y*w), 0,
         2*(x*y-z*w), 1-2*(x*x+z*z), 2*(y*z+x*w), 0,
         2*(x*z+y*w), 2*(y*z-x*w), 1-2*(x*x+y*y), 0, *t, 1)
    return affine(tuple(r[k] * (s[k // 4] if k < 12 else 1) for k in range(16)))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 2, "duplicate_key")
        result[key] = value
    return result


class GLB:
    def __init__(self, data, deadline, vertex_budget):
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
                row = tuple(x / ((1 << (8 * size))-1) for x in row)
            rows.append(row)
        return rows, normalized

    def hierarchy(self, overrides=None):
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
                local = node_matrix(nodes[current])
                if overrides is not None and str(current) in overrides:
                    local = affine(tuple(x / 1000000 for x in overrides[str(current)]))
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
        require(any("mesh" in nodes[n] for n in active), 3, "active_mesh_missing")
        return active


def _policy(policy):
    validate(policy, "policy")
    require(policy["threshold_status"] == "DESIGN_CHOICE_UNCALIBRATED", 2, "threshold_provenance")
    for key, lo, hi in (("weight_sum_error_ppm", 0, 1), ("bind_error_um", 0, 1000),
                        ("deformation_error_um", 0, 1000), ("max_vertices", 1, 1000000),
                        ("timeout_s", 1, 300)):
        require(integer(policy[key], lo, hi), 2, "policy_invalid")


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
        ibm = [affine(m) for m in ibm]
        palettes = [mul(world[j], m) for j, m in zip(joints, ibm)]
        metrics["joints"], metrics["vertices"] = len(joints), 0
        records, bind_error, weight_error = {}, 0.0, 0.0
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
                indices, normj = glb.accessor(attrs["JOINTS_0"], "VEC4", (5121, 5123))
                weights, normw = glb.accessor(attrs["WEIGHTS_0"], "VEC4", (5121, 5123, 5126))
                wa = glb.doc["accessors"][attrs["WEIGHTS_0"]]
                require(not normj and (wa["componentType"] == 5126 or normw), 2, "skin_normalization")
                require(len(pos) == len(indices) == len(weights), 5, "attribute_count")
                metrics["vertices"] += len(pos)
                require(metrics["vertices"] <= policy["max_vertices"], 6, "vertex_budget")
                # An off-axis or unused joint's broken bind must not be hidden
                # by a degenerate point set or cancellation between weights.
                for palette in palettes:
                    for basis in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)):
                        bind_error = max(bind_error, distance(point(palette, basis), point(world[ni], basis))*1000000)
                for v, (p, js, ws) in enumerate(zip(pos, indices, weights)):
                    if v % 1024 == 0:
                        glb.check_time()
                    require(all(integer(j, 0, len(joints)-1) for j in js), 5, "joint_index")
                    require(all(0 <= w <= 1 for w in ws) and sum(ws) > 0, 5, "weight_invalid")
                    error = abs(sum(ws) - 1) * 1000000
                    weight_error = max(weight_error, error)
                    require(error <= policy["weight_sum_error_ppm"], 5, "weight_sum")
                    expected = point(world[ni], p)
                    for j, weight in zip(js, ws):
                        if weight:
                            # Check each contributing bind, not only their possibly cancelling average.
                            bind_error = max(bind_error, distance(point(palettes[j], p), expected)*1000000)
                records[(ni, pi)] = (pos, indices, weights)
        require(records and metrics["vertices"] > 0, 5, "skinned_mesh_missing")
        metrics.update(weight_sum_error_ppm=math.ceil(weight_error), bind_error_um=math.ceil(bind_error))
        require(bind_error <= policy["bind_error_um"], 5, "bind_pose_mismatch")
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
