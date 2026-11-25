# SPDX-License-Identifier: AGPL-3.0-only
"""Measure the self-contained triangle-mesh glTF subset used by the offline exchange."""

from base64 import b64decode
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import stat
from struct import iter_unpack


MAX_BYTES = 64 * 1024 * 1024


class SceneRefused(ValueError):
    pass


def _unique(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("duplicate glTF keys")
    return result


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _index(items, index):
    if type(items) is not list or type(index) is not int or not 0 <= index < len(items):
        raise ValueError("invalid glTF array index")
    return items[index]


def _accessor(document, buffer, index, kind, formats):
    accessor = _index(document["accessors"], index)
    view = _index(document["bufferViews"], accessor["bufferView"])
    component = accessor["componentType"]
    if (accessor["type"] != kind or type(component) is not int or component not in formats or "sparse" in accessor
            or accessor.get("normalized", False) or "byteStride" in view or view["buffer"] != 0):
        raise ValueError("unsupported glTF accessor")
    fmt, width = formats[component]
    base = view.get("byteOffset", 0)
    offset = accessor.get("byteOffset", 0)
    count = accessor["count"]
    length = view["byteLength"]
    if (any(type(value) is not int for value in (base, offset, length, count))
            or min(base, offset, length) < 0 or count <= 0 or base + length > len(buffer)
            or offset + count * width > length):
        raise ValueError("out-of-range glTF accessor")
    return iter_unpack(fmt, memoryview(buffer)[base + offset:base + offset + count * width])


def _measure(document):
    if document["asset"]["version"] != "2.0":
        raise ValueError("unsupported glTF version")
    entry, = document["buffers"]
    prefix = "data:application/octet-stream;base64,"
    if not entry["uri"].startswith(prefix):
        raise ValueError("external glTF buffer")
    buffer = b64decode(entry["uri"][len(prefix):], validate=True)
    if len(buffer) != entry["byteLength"]:
        raise ValueError("buffer length mismatch")

    names = []
    lower = [float("inf")] * 3
    upper = [float("-inf")] * 3
    vertices = triangles = 0
    scene = _index(document["scenes"], document["scene"])
    for index in scene["nodes"]:
        node = _index(document["nodes"], index)
        if "children" in node or "mesh" not in node:
            raise ValueError("fixture needs root mesh nodes")
        names.append(node["name"])
        for primitive in _index(document["meshes"], node["mesh"])["primitives"]:
            if primitive.get("mode", 4) != 4:
                raise ValueError("fixture needs triangle primitives")
            points = _accessor(document, buffer, primitive["attributes"]["POSITION"],
                               "VEC3", {5126: ("<fff", 12)})
            point_count = 0
            for point in points:
                if not all(isfinite(value) and abs(value) <= 1_000_000 for value in point):
                    raise ValueError("unrepresentable mesh coordinate")
                for axis, value in enumerate(point):
                    lower[axis] = min(lower[axis], value)
                    upper[axis] = max(upper[axis], value)
                point_count += 1

            indices = _accessor(document, buffer, primitive["indices"], "SCALAR",
                                {5121: ("<B", 1), 5123: ("<H", 2), 5125: ("<I", 4)})
            index_count = 0
            for (vertex,) in indices:
                if vertex >= point_count:
                    raise ValueError("index outside position accessor")
                index_count += 1
            if index_count % 3:
                raise ValueError("incomplete triangle")
            vertices += point_count
            triangles += index_count // 3

    if not triangles or not all(type(name) is str and name for name in names):
        raise ValueError("empty or unnamed mesh")
    return {"nodes": sorted(names), "vertices": vertices, "triangles": triangles,
            "local_bounds_um": [[round(v * 1_000_000) for v in side] for side in (lower, upper)]}


def inspect(directory, name):
    try:
        fd = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SceneRefused("ARTIFACT_UNAVAILABLE") from exc
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SceneRefused("ARTIFACT_UNSAFE")
        if not 0 < before.st_size <= MAX_BYTES:
            raise SceneRefused("ARTIFACT_SIZE")
        data = stream.read(before.st_size + 1)
        if len(data) != before.st_size or _fingerprint(before) != _fingerprint(os.fstat(fd)):
            raise SceneRefused("ARTIFACT_CHANGED")
    try:
        doc = json.loads(data, object_pairs_hook=_unique)
        structure = _measure(doc)
        provenance = doc["extras"]["external_control"]
        if type(provenance) is not dict:
            raise ValueError("missing provenance")
    except (ValueError, TypeError, IndexError, KeyError, AttributeError, OverflowError) as exc:
        raise SceneRefused("ARTIFACT_STRUCTURE_INVALID") from exc
    return {
        "artifact": {"name": name, "sha256": sha256(data).hexdigest(), "bytes": len(data)},
        "structure": structure,
        "provenance": provenance,
    }, _fingerprint(before)
