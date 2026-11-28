# SPDX-License-Identifier: AGPL-3.0-only
"""Extract search fields and conservative GLTF skeleton fingerprints."""

import base64
import json
import math
import struct
from pathlib import Path

import asset_db as upstream

ABSENT = upstream.ABSENT
GLTF = {".gltf", ".glb", ".vrm"}


def action_records(item):
    clips = item["animation_clips"]
    if not isinstance(clips, list):
        return []
    return [
        {"clip": name, "action": " ".join(name.replace("_", " ").split()),
         "basis": "clip name"}
        for name in clips if isinstance(name, str) and name
    ]


def read_values(path, document, view, offset, size, dependency):
    if offset < 0 or size < 0 or offset + size > view["byteLength"]:
        raise ValueError("buffer view outside declared range")
    buffer = document["buffers"][view["buffer"]]
    offset += view.get("byteOffset", 0)
    if offset + size > buffer["byteLength"]:
        raise ValueError("buffer outside declared range")
    uri = buffer.get("uri")
    if uri is not None:
        if uri.startswith("data:"):
            header, data = uri.split(",", 1)
            if not header.endswith(";base64"):
                raise ValueError("unsupported GLTF data encoding")
            return base64.b64decode(data, validate=True)[offset:offset + size]
        with dependency(uri).open("rb") as stream:
            stream.seek(offset)
            return stream.read(size)
    with path.open("rb") as stream:
        stream.seek(12)
        json_size, _ = struct.unpack("<II", stream.read(8))
        stream.seek(json_size, 1)
        length, kind = struct.unpack("<II", stream.read(8))
        if kind != 0x004E4942 or offset + size > length:
            raise ValueError("invalid GLB binary chunk")
        stream.seek(offset, 1)
        return stream.read(size)


def transform(node):
    if "matrix" in node:
        fields = {"matrix": (node["matrix"], 16)}
    else:
        fields = {
            "translation": (node.get("translation", [0, 0, 0]), 3),
            "rotation": (node.get("rotation", [0, 0, 0, 1]), 4),
            "scale": (node.get("scale", [1, 1, 1]), 3),
        }
    result = {}
    for field, (values, width) in fields.items():
        if len(values) != width or not all(type(x) in (int, float) and math.isfinite(x) for x in values):
            raise ValueError("invalid joint transform")
        result[field] = [float(x) if x else 0.0 for x in values]
    rotation = result.get("rotation")
    if rotation and next((x for x in reversed(rotation) if x), 0) < 0:
        result["rotation"] = [-x if x else 0.0 for x in rotation]
    return result


def skeletons(path, document, dependency):
    nodes = document.get("nodes", [])
    parents = {}
    for index, node in enumerate(nodes):
        for child in node.get("children", []):
            if type(child) is not int or not 0 <= child < len(nodes) or child in parents:
                raise ValueError("ambiguous node parent")
            parents[child] = index
    records = []
    for skin in document.get("skins", []):
        indexes = skin["joints"]
        if not indexes or any(type(i) is not int or not 0 <= i < len(nodes) for i in indexes):
            raise ValueError("invalid joint index")
        joint_names = {i: nodes[i].get("name") for i in indexes}
        if any(not isinstance(name, str) or not name for name in joint_names.values()):
            raise ValueError("unnamed joint")
        if len(set(joint_names.values())) != len(indexes):
            raise ValueError("duplicate joint name")

        accessor_index = skin.get("inverseBindMatrices")
        identity = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]
        matrices = [identity] * len(indexes)
        if accessor_index is not None:
            accessor = document["accessors"][accessor_index]
            view = document["bufferViews"][accessor["bufferView"]]
            stride = view.get("byteStride", 64)
            if (accessor.get("type") != "MAT4" or accessor.get("componentType") != 5126
                    or "sparse" in accessor or accessor.get("count") != len(indexes) or stride < 64):
                raise ValueError("unsupported inverse bind accessor")
            raw = read_values(path, document, view, accessor.get("byteOffset", 0),
                              stride * (len(indexes) - 1) + 64, dependency)
            matrices = [
                [float(x) if x else 0.0 for x in struct.unpack_from("<16f", raw, i * stride)]
                for i in range(len(indexes))
            ]
            if any(not math.isfinite(x) for matrix in matrices for x in matrix):
                raise ValueError("non-finite inverse bind matrix")

        topology, rest, bind = {}, {}, {}
        for i, matrix in zip(indexes, matrices):
            parent = parents.get(i)
            chain = [transform(nodes[i])]
            visited = {i}
            while parent is not None and parent not in joint_names:
                if parent in visited:
                    raise ValueError("cyclic joint ancestry")
                visited.add(parent)
                chain.append(transform(nodes[parent]))
                parent = parents.get(parent)
            name = joint_names[i]
            topology[name] = joint_names.get(parent)
            rest[name] = chain
            bind[name] = matrix
        for name in topology:
            ancestry = set()
            while name is not None:
                if name in ancestry:
                    raise ValueError("cyclic skeleton")
                ancestry.add(name)
                name = topology[name]
        clips = {}
        for clip in document.get("animations", []):
            name = clip.get("name")
            if not isinstance(name, str) or not name:
                continue
            channels = [channel["target"] for channel in clip["channels"]]
            supported = bool(channels) and all(
                target.get("node") in joint_names and target.get("path") in {"translation", "rotation", "scale"}
                for target in channels
            )
            if name in clips or not supported:
                clips[name] = ABSENT
            else:
                clips[name] = sorted({joint_names[channel["node"]] for channel in channels})
        records.append({
            "skeleton_id": upstream.digest(upstream.canonical(topology)),
            "rest_id": upstream.digest(upstream.canonical(rest)),
            "bind_id": upstream.digest(upstream.canonical(bind)),
            "joint_count": len(indexes),
            "clips": clips,
        })
    return records


def summarize(item, roots, cache):
    tech = item["technical"] if isinstance(item["technical"], dict) else {}
    licence = item["licence_rights"]
    provenance = item["provenance"]
    source = Path(provenance[0]["path"])
    units = axes = basis = ABSENT
    rig = []
    diagnostic = ABSENT
    if source.suffix.lower() in GLTF:
        units, axes, basis = "metre", "right-handed +Y up +Z front", "glTF 2.0 file coordinates"
    elif tech.get("units") == "metres, Z up":
        units, axes, basis = "metre", "+Z up", "source SET units declaration"
    elif item["kind"] in {"set", "camera"} and source.suffix.lower() == ".json":
        try:
            path = upstream.actual_source(roots, provenance[0])
            upstream.check_digest(path, item["sha256"], cache)
            if path.stat().st_size <= 1_000_000 and json.loads(path.read_bytes()).get("units") == "metres, Z up":
                units, axes, basis = "metre", "+Z up", "source SET units declaration"
        except (upstream.Refusal, OSError, ValueError, AttributeError):
            pass
    if item["kind"] in {"motion", "rig"} and source.suffix.lower() in GLTF:
        first = provenance[0]
        deps = {entry["uri"]: entry for entry in first.get("dependencies", []) if "sha256" in entry}

        def dependency(uri):
            entry = deps[uri]
            path = upstream.actual_source(roots, {"root": first["root"], **entry})
            upstream.check_digest(path, entry["sha256"], cache)
            return path

        try:
            path = upstream.actual_source(roots, first)
            upstream.check_digest(path, item["sha256"], cache)
            document = upstream.gltf_document(path)
            rig = skeletons(path, document, dependency)
            if len(rig) != 1:
                diagnostic = "one unambiguous skin required for direct reuse"
        except (upstream.Refusal, OSError, ValueError, TypeError, IndexError, KeyError, struct.error, OverflowError) as exc:
            diagnostic = exc.reason if isinstance(exc, upstream.Refusal) else type(exc).__name__

    terms = []
    for evidence in licence.get("evidence", []):
        scope = evidence.get("terms", {}).get("grant_scope")
        if isinstance(scope, str) and scope not in terms:
            terms.append(scope)
    observations = []
    for origin in provenance:
        for receipt in origin.get("receipts", []):
            if any(key in receipt for key in ("status", "verdict", "production_admission")):
                observations.append({**receipt, "origin": origin["path"]})
    return {
        "actions": action_records(item),
        "rights": {"source_status": licence.get("status", ABSENT),
                   "upstream_drive_policy": licence.get("drive_policy", ABSENT), "recorded_scopes": terms},
        "observations": observations,
        "units": units, "axes": axes, "units_axes_basis": basis,
        "skeletons": rig, "skeleton_diagnostic": diagnostic,
        "origin_count": len(provenance),
    }
