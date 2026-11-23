"""Reopen fixture GLBs whose animation is appended to an authenticated SKIN."""
from fractions import Fraction
from hashlib import sha256
import math
import struct
import time

from asset_factory import core
from providers.unirig.common import Refusal, integer, require
from providers.unirig.gate import GLB
from tools.blender.bake_support import Refusal as ReadRefusal, read_bytes
from tools.rig_math import det

MAX_ROWS = 1_000_000  # [TARGET] Per-accessor work ceiling.
WALL_NS = 5_000_000_000  # [TARGET] CPU work ceiling.
UNIT_TOLERANCE = 1e-5  # [TARGET] Float fixture representation tolerance.
WIDTH = {"translation": "VEC3", "rotation": "VEC4", "scale": "VEC3"}
ANIMATED_KEYS = {"animations", "accessors", "bufferViews", "buffers"}


def _binds(glb, nodes, active, parents):
    skins = glb.doc.get("skins", [])
    require(type(skins) is list and len(skins) == 1, 3, "one_skin_required")
    skin = skins[0]
    joints = skin.get("joints")
    require(type(joints) is list and joints and
            all(integer(joint, 0, len(nodes) - 1) and joint in active for joint in joints)
            and len(set(joints)) == len(joints), 5, "joints_invalid")
    if "skeleton" in skin:
        root = skin["skeleton"]
        require(integer(root, 0, len(nodes) - 1) and root in active, 5, "skeleton_root_invalid")
        for ancestor in joints:
            while ancestor is not None and ancestor != root:
                ancestor = parents[ancestor]
            require(ancestor == root, 5, "skeleton_root_not_ancestor")
    require("inverseBindMatrices" in skin, 3, "inverse_binds_missing")
    matrices, _ = glb.accessor(skin["inverseBindMatrices"], "MAT4", (5126,))
    require(len(matrices) == len(joints), 5, "inverse_bind_count")
    for matrix in matrices:
        require(tuple(matrix[i] for i in (3, 7, 11, 15)) == (0, 0, 0, 1),
                5, "inverse_bind_not_affine")
        columns = tuple(tuple(map(Fraction, matrix[i:i + 3])) for i in (0, 4, 8))
        require(det(columns) != 0, 5, "inverse_bind_singular")
    return set(joints)


def _weights(glb, attrs, count, joint_count):
    keys = {key for key in attrs if key.startswith(("JOINTS_", "WEIGHTS_"))}
    sets = [(f"JOINTS_{i}", f"WEIGHTS_{i}") for i in range(len(keys) // 2)]
    require(sets and keys == {key for pair in sets for key in pair}, 5, "skin_influence_sets_invalid")
    sums = [0.0] * count
    for joint_key, weight_key in sets:
        joints, normalized_joints = glb.accessor(attrs[joint_key], "VEC4", (5121, 5123))
        weights, normalized_weights = glb.accessor(attrs[weight_key], "VEC4", (5121, 5123, 5126))
        component = glb.doc["accessors"][attrs[weight_key]]["componentType"]
        require(not normalized_joints and (normalized_weights or component == 5126),
                2, "skin_attribute_encoding")
        require(len(joints) == len(weights) == count, 5, "skin_attribute_count")
        for index, (indices, row) in enumerate(zip(joints, weights, strict=True)):
            require(all(joint < joint_count for joint in indices), 5, "skin_joint_index")
            require(all(weight >= 0.0 for weight in row), 5, "skin_weight_negative")
            sums[index] += math.fsum(row)
    require(all(abs(total - 1.0) <= UNIT_TOLERANCE for total in sums), 5, "skin_weight_sum")


def _mesh(glb, reference, nodes, active, joint_count):
    meshes = glb.doc.get("meshes", [])
    source_meshes = reference.doc.get("meshes", [])
    skinned = [nodes[index] for index in active if "mesh" in nodes[index]]
    require(skinned, 3, "skinned_mesh_missing")
    vertices = 0
    for node in skinned:
        require(type(node.get("skin")) is int and node["skin"] == 0, 5, "skin_binding_missing")
        index = node["mesh"]
        require(integer(index, 0, len(meshes) - 1), 5, "mesh_index")
        require(index < len(source_meshes), 4, "reference_mesh_changed")
        primitives = meshes[index]["primitives"]
        source_primitives = source_meshes[index]["primitives"]
        require(primitives and len(primitives) == len(source_primitives), 4, "reference_mesh_changed")
        for primitive, source in zip(primitives, source_primitives, strict=True):
            require(primitive.get("mode", 4) == 4 and not primitive.get("extensions")
                    and not primitive.get("targets"), 3, "animated_primitive_unsupported")
            attrs = primitive["attributes"]
            positions, _ = glb.accessor(attrs["POSITION"], "VEC3", (5126,))
            require("NORMAL" in attrs, 3, "normal_missing")
            normals, _ = glb.accessor(attrs["NORMAL"], "VEC3", (5126,))
            require(len(normals) == len(positions), 5, "normal_count")
            require(all(abs(math.hypot(*normal) - 1.0) <= UNIT_TOLERANCE for normal in normals),
                    5, "normal_not_unit")
            require("NORMAL" in source["attributes"], 3, "reference_normal_missing")
            originals, _ = reference.accessor(source["attributes"]["NORMAL"], "VEC3", (5126,))
            require(len(originals) == len(normals), 4, "reference_normal_count")
            require(all(math.fsum(a * b for a, b in zip(normal, original, strict=True)) > 0.0
                        for normal, original in zip(normals, originals, strict=True)),
                    5, "normal_orientation_changed")
            _weights(glb, attrs, len(positions), joint_count)
            vertices += len(positions)
            require(vertices <= MAX_ROWS, 6, "animated_vertex_budget")
    return vertices


def _animation(glb, reference, nodes, active, name, required_channels):
    clips = glb.doc.get("animations")
    require(type(clips) is list and len(clips) == 1, 3, "one_animation_required")
    animation = clips[0]
    require(type(name) is str and bool(name) and animation.get("name") == name, 4, "animation_name_stale")
    require(type(required_channels) is dict and bool(required_channels) and
            all(type(node) is str and type(path) is str and path in WIDTH
                for node, path in required_channels.items()), 3, "required_channels_unavailable")
    channels, samplers = animation["channels"], animation["samplers"]
    require(type(channels) is list and channels and type(samplers) is list and samplers,
            3, "animation_channels_missing")
    expect = set()
    for node_name, path in required_channels.items():
        matches = [i for i in active if nodes[i].get("name") == node_name]
        require(len(matches) == 1, 5, "required_animation_node_invalid")
        expect.add((matches[0], path))
    seen, used = set(), set()
    samples = 0
    for channel in channels:
        glb.check_time()
        target = channel["target"]
        node, path = target["node"], target["path"]
        require(integer(node, 0, len(nodes) - 1) and node in active, 5, "animation_target_missing")
        require(type(path) is str and path in WIDTH, 3, "animation_path_unsupported")
        require((node, path) not in seen, 5, "animation_channel_duplicate")
        ordinal = channel["sampler"]
        require(integer(ordinal, 0, len(samplers) - 1), 5, "animation_sampler_missing")
        sampler = samplers[ordinal]
        mode = sampler.get("interpolation", "LINEAR")
        require(mode in ("LINEAR", "STEP", "CUBICSPLINE"), 3, "animation_interpolation_unsupported")
        for index in (sampler["input"], sampler["output"]):
            require(integer(index, len(reference.doc["accessors"]), len(glb.doc["accessors"]) - 1),
                    5, "animation_accessor_index")
            view = glb.doc["accessors"][index].get("bufferView")
            require(integer(view, len(reference.doc["bufferViews"]), len(glb.doc["bufferViews"]) - 1)
                    and glb.doc["bufferViews"][view].get("byteOffset", 0) >= reference.buffer_length,
                    5, "animation_buffer_view")
        times, _ = glb.accessor(sampler["input"], "SCALAR", (5126,))
        require(times[0][0] >= 0 and all(a < b for (a,), (b,) in zip(times, times[1:])),
                5, "animation_time_order")
        values, _ = glb.accessor(sampler["output"], WIDTH[path], (5126,))
        multiplier = 3 if mode == "CUBICSPLINE" else 1
        require(len(values) == len(times) * multiplier, 5, "animation_sample_count")
        if path == "rotation":
            rotations = values[1::3] if multiplier == 3 else values
            require(all(abs(math.hypot(*row) - 1.0) <= UNIT_TOLERANCE for row in rotations),
                    5, "animation_rotation_not_unit")
        seen.add((node, path))
        used.add(ordinal)
        samples += len(times)
    require(expect <= seen, 3, "required_channel_missing")
    require(seen == expect, 5, "unregistered_animation_channel")
    require(len(used) == len(samplers), 5, "animation_sampler_unused")
    return len(seen), samples


def _skin_unchanged(glb, reference):
    source = reference.doc
    candidate = glb.doc
    require({k: v for k, v in source.items() if k not in ANIMATED_KEYS}
            == {k: v for k, v in candidate.items() if k not in ANIMATED_KEYS}, 4, "animated_skin_changed")
    for key in ("accessors", "bufferViews"):
        require(candidate[key][:len(source[key])] == source[key], 4, "animated_skin_changed")
    require(glb.buffer_length >= reference.buffer_length
            and glb.binary[:reference.buffer_length] == reference.binary[:reference.buffer_length]
            and candidate["buffers"][0] == source["buffers"][0] | {"byteLength": glb.buffer_length},
            4, "animated_skin_changed")


def check_animated(path, *, expected_sha256, skin_path, skin_sha256, animation_name, required_channels):
    """Check decoded bytes against an independently registered static SKIN and channel set."""
    started = time.monotonic_ns()
    rc, reason, metrics, actual = 0, None, {}, None
    try:
        data = read_bytes(path)
        actual = sha256(data).hexdigest()
        require(actual == expected_sha256, 4, "animated_glb_stale")
        source = read_bytes(skin_path)
        require(sha256(source).hexdigest() == skin_sha256, 4, "animated_reference_stale")
        deadline = started + WALL_NS
        glb = GLB(data, deadline, MAX_ROWS)
        reference = GLB(source, deadline, MAX_ROWS)
        parents, _ = glb.hierarchy()
        nodes = glb.doc["nodes"]
        active = glb.scene_nodes(parents)
        joints = _binds(glb, nodes, active, parents)
        vertices = _mesh(glb, reference, nodes, active, len(joints))
        channels, samples = _animation(glb, reference, nodes, active, animation_name, required_channels)
        _skin_unchanged(glb, reference)
        glb.check_time()
        metrics = {"channels": channels, "time_samples": samples, "joints": len(joints), "vertices": vertices}
    except Refusal as exc:
        rc, reason = exc.rc, exc.code
    except ReadRefusal as exc:
        rc, reason = exc.rc, exc.reason
    except MemoryError:
        rc, reason = 6, "animated_glb_memory_budget"
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError, RecursionError, struct.error):
        rc, reason = 2, "animated_glb_invalid"
    return {"schema": "af.animated-glb-check/v1", "scope": "registered-skin-append-fixture",
            "rc": rc, "status": core.STATUS[rc], "verdict": core.VERDICT[rc],
            "production_verdict": "CANNOT_SEE", "calibration_state": "TARGET_UNCALIBRATED",
            "reason_codes": [reason] if reason else [], "glb_sha256": actual,
            "skin_sha256": skin_sha256, "metrics": metrics, "wall_ns": time.monotonic_ns() - started}
