# SPDX-License-Identifier: AGPL-3.0-only
"""Read and write embedded GLB geometry and skin accessors."""
import json
import struct

import numpy as np
from rig_common import need


def unpack(data):
    need(len(data) >= 28, "glb_header")
    head = struct.unpack_from("<4sIII4s", data)
    need(head[:3] == (b"glTF", 2, len(data)) and head[4] == b"JSON", "glb_header")
    offset = 20 + head[3]
    need(head[3] % 4 == 0 and offset + 8 <= len(data), "glb_json_chunk")
    length, kind = struct.unpack_from("<I4s", data, offset)
    need(kind == b"BIN\x00" and offset + 8 + length == len(data), "glb_binary_chunk")
    doc = json.loads(data[20:offset])
    buffers = doc.get("buffers", [])
    need(doc.get("asset", {}).get("version") == "2.0" and len(buffers) == 1
         and "uri" not in buffers[0] and type(buffers[0].get("byteLength")) is int
         and 0 <= length - buffers[0]["byteLength"] <= 3, "glb_embedded_buffer")
    return doc, bytearray(data[offset + 8:])


def pack(doc, data):
    need(len(doc["buffers"]) == 1, "glb_buffer_count")
    doc["buffers"][0]["byteLength"] = len(data)
    meta = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    meta += b" " * (-len(meta) % 4)
    data += b"\0" * (-len(data) % 4)
    return (struct.pack("<4sIII4s", b"glTF", 2, 28 + len(meta) + len(data), len(meta), b"JSON")
            + meta + struct.pack("<I4s", len(data), b"BIN\x00") + data)


def append(doc, data, raw):
    data.extend(b"\0" * (-len(data) % 4))
    offset = len(data)
    data.extend(raw)
    doc["bufferViews"].append({"buffer": 0, "byteOffset": offset, "byteLength": len(raw)})
    return len(doc["bufferViews"]) - 1


def add(doc, data, rows, shape, dtype, component, *, bounds=False):
    rows = np.ascontiguousarray(rows, dtype=dtype)
    item = {"bufferView": append(doc, data, rows.tobytes()), "componentType": component,
            "count": len(rows), "type": shape}
    if bounds:
        item.update(min=rows.min(axis=0).tolist(), max=rows.max(axis=0).tolist())
    doc["accessors"].append(item)
    return len(doc["accessors"]) - 1


def primitive(doc):
    need(len(doc["meshes"]) == 1 and len(doc["meshes"][0]["primitives"]) == 1,
         "multiple_mesh_primitives_unsupported")
    return doc["meshes"][0]["primitives"][0]

