"""Bounded GLB inventory for topology custody; no geometry evaluator lives here.

Only embedded, static, triangle GLBs are supported. Preservation checks are
structural; they do not replace F2-U3 appearance/UV or F2-U1 geometry gates.
"""
import hashlib
import json
import math
import struct
from pathlib import Path

MAX_GLB_BYTES = 128 * 2**20  # [TARGET] bounded input, not a quality threshold.

from asset_factory import core


class TopologyError(core.ContractError):
    def __init__(self, rc, reason):
        super().__init__(reason, "$")
        self.rc, self.reason = rc, reason

    def as_dict(self):
        return {"rc": self.rc, "status": core.STATUS[self.rc], "reason_codes": [self.code], "path": self.path}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TopologyError(2, "duplicate_key")
        result[key] = value
    return result


def canonical(value):
    try:
        return core.canonical_json(value)
    except core.ContractError as exc:
        raise TopologyError(2, exc.code) from exc


def json_digest(value):
    return core.sha256_json(value)


def read_bytes(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_GLB_BYTES + 1)
    if len(data) > MAX_GLB_BYTES:
        raise TopologyError(6, "input_budget_exceeded")
    return data


def _int(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise TopologyError(2, "glb_invalid_integer")
    return value


def _object(value):
    if not isinstance(value, dict):
        raise TopologyError(2, "glb_invalid_document")
    return value


def inspect_glb(path):
    data = read_bytes(path)
    if len(data) < 20 or struct.unpack_from("<4sII", data) != (b"glTF", 2, len(data)):
        raise TopologyError(2, "glb_invalid_header")
    offset, chunks = 12, []
    while offset < len(data):
        if offset + 8 > len(data):
            raise TopologyError(2, "glb_truncated_chunk")
        size, kind = struct.unpack_from("<I4s", data, offset)
        offset += 8
        if size % 4 or offset + size > len(data):
            raise TopologyError(2, "glb_invalid_chunk")
        chunks.append((kind, data[offset:offset + size]))
        offset += size
    if not chunks or chunks[0][0] != b"JSON" or len(chunks) != 2 or chunks[1][0] != b"BIN\0":
        raise TopologyError(2, "glb_embedded_chunks_required")
    try:
        doc = json.loads(chunks[0][1], object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _object(doc)
        binary = chunks[1][1]
        if doc.get("asset", {}).get("version") != "2.0":
            raise TopologyError(2, "glb_asset_version")
        if doc.get("skins") or doc.get("animations") or doc.get("extensionsRequired"):
            raise TopologyError(3, "unsupported_glb_feature")
        buffers = doc["buffers"]
        if len(buffers) != 1 or "uri" in buffers[0]:
            raise TopologyError(2, "external_buffer_refused")
        length = _int(buffers[0]["byteLength"])
        if length > len(binary) or len(binary) - length > 3:
            raise TopologyError(2, "glb_buffer_length")
        views = doc["bufferViews"]
        for view in views:
            _object(view)
            if view.get("buffer", 0) != 0:
                raise TopologyError(2, "glb_buffer_index")
            start = _int(view.get("byteOffset", 0))
            if start + _int(view["byteLength"]) > length:
                raise TopologyError(2, "glb_view_bounds")
        accessors = doc["accessors"]
        def accessor(index):
            acc = _object(accessors[_int(index)])
            if "sparse" in acc:
                raise TopologyError(3, "unsupported_sparse_accessor")
            view = views[_int(acc["bufferView"])]
            count = _int(acc["count"], 1)
            widths = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
            sizes = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
            size = widths[acc["componentType"]] * sizes[acc["type"]]
            stride = _int(view.get("byteStride", size), size)
            end = _int(acc.get("byteOffset", 0)) + (count - 1) * stride + size
            if end > view["byteLength"]:
                raise TopologyError(2, "glb_accessor_bounds")
            return acc
        # Only active scene mesh instances contribute face counts. Unreachable
        # meshes/nodes are refused rather than used to manufacture a budget.
        nodes = doc["nodes"]
        roots = doc["scenes"][_int(doc.get("scene", 0))]["nodes"]
        seen, active = set(), []
        def visit(index):
            index = _int(index)
            if index in seen:
                raise TopologyError(2, "glb_node_reuse_or_cycle")
            seen.add(index)
            node = _object(nodes[index])
            if not isinstance(node.get("name", ""), str):
                raise TopologyError(2, "glb_invalid_document")
            if node.get("name", "").lower().endswith(("_col", "-col", "_occ", "-occ", "_collision", "-convcol")):
                raise TopologyError(3, "helper_separation_required")
            if "mesh" in node:
                active.append(_int(node["mesh"]))
            for child in node.get("children", []):
                visit(child)
        for root in roots:
            visit(root)
        if len(seen) != len(nodes) or set(active) != set(range(len(doc["meshes"]))):
            raise TopologyError(3, "unreachable_geometry")
        faces, uv_primitives, primitives, sample_population = 0, 0, 0, 0
        used_materials, uv_fingerprints, uv_spans = [], [], []
        for index in active:
            for prim in doc["meshes"][index]["primitives"]:
                _object(prim)
                if prim.get("mode", 4) != 4 or prim.get("targets") or prim.get("extensions"):
                    raise TopologyError(3, "unsupported_primitive")
                attrs = _object(prim["attributes"])
                pos = accessor(attrs["POSITION"])
                if pos["type"] != "VEC3" or pos["componentType"] != 5126:
                    raise TopologyError(2, "glb_position_type")
                if "indices" in prim:
                    indices = accessor(prim["indices"])
                    if indices["type"] != "SCALAR" or indices["componentType"] not in (5121, 5123, 5125):
                        raise TopologyError(2, "glb_index_type")
                    count = indices["count"]
                else:
                    count = pos["count"]
                if count % 3:
                    raise TopologyError(2, "glb_nontriangle_count")
                for attr, index in attrs.items():
                    if accessor(index)["count"] != pos["count"]:
                        raise TopologyError(2, "glb_attribute_count")
                faces += count // 3
                sample_population += pos["count"] + count // 3
                primitives += 1
                uv_primitives += int("TEXCOORD_0" in attrs)
                if "TEXCOORD_0" in attrs:
                    uv = accessor(attrs["TEXCOORD_0"])
                    if uv["type"] != "VEC2" or uv["componentType"] != 5126:
                        raise TopologyError(3, "unsupported_uv_encoding")
                    view = views[uv["bufferView"]]
                    start = view.get("byteOffset", 0) + uv.get("byteOffset", 0)
                    stride = view.get("byteStride", 8)
                    values = [struct.unpack_from("<ff", binary, start + i * stride) for i in range(uv["count"])]
                    if any(not math.isfinite(v) for pair in values for v in pair):
                        raise TopologyError(2, "uv_nonfinite")
                    uv_fingerprints.append(digest(b"".join(struct.pack("<ff", *pair) for pair in values)))
                    uv_spans.append([int(max(pair[k] for pair in values) > min(pair[k] for pair in values))
                                     for k in (0, 1)])
                if "material" in prim:
                    used_materials.append(doc["materials"][_int(prim["material"])])
        if not faces:
            raise TopologyError(3, "empty_geometry")
        images = []
        for image in doc.get("images", []):
            if "uri" in image:
                raise TopologyError(2, "external_image_refused")
            view = views[_int(image["bufferView"])]
            start = view.get("byteOffset", 0)
            images.append(digest(binary[start:start + view["byteLength"]]))
        # Material JSON can contain glTF floats; this hash is not a stage JSON
        # identity. It is a conservative structural preservation fingerprint.
        materials = json.dumps(used_materials, sort_keys=True, separators=(",", ":"), allow_nan=False)
        texture_state = json.dumps({key: doc.get(key, []) for key in ("textures", "samplers")},
                                   sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"sha256": digest(data), "faces": faces, "primitives": primitives,
                "uv_primitives": uv_primitives, "materials_sha256": digest(materials.encode()),
                "sample_population": sample_population,
                "image_sha256": images, "texture_state_sha256": digest(texture_state.encode()),
                "uv_sha256": uv_fingerprints, "uv_spans": uv_spans, "bytes": len(data)}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise TopologyError(2, "glb_invalid_document") from exc
