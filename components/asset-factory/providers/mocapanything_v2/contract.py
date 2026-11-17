"""Private motion evidence contracts and bounded, read-only asset inspection."""

import hashlib
import json
import math
from pathlib import Path
import struct

from asset_factory import core
from .bvh import Refusal, require

ROOT = Path(__file__).resolve().parent
MAX_METADATA = 16 * 2**20  # [TARGET, DESIGN_CHOICE]


def read_bytes(path, maximum):
    path = Path(path)
    try:
        require(not path.is_symlink(), 2, "symlink_refused")
        with path.open("rb") as stream:
            require(path.stat().st_size <= maximum, 6, "input_byte_budget")
            data = stream.read(maximum + 1)
        require(len(data) <= maximum, 6, "input_byte_budget")
        return data
    except FileNotFoundError as exc:
        raise Refusal(3, "input_missing") from exc
    except OSError as exc:
        raise Refusal(2, "input_io") from exc


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate(record, name):
    schema = core.load_json(ROOT / "schemas" / (name + ".schema.json"), max_bytes=MAX_METADATA)
    core.validate(record, schema)


def load(path, name=None):
    # core.load_json rejects duplicates, floats, NaN and boolean integers.
    read_bytes(path, MAX_METADATA)
    obj = core.load_json(Path(path), max_bytes=MAX_METADATA)
    if name:
        validate(obj, name)
    return obj


def strict_asset_json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 2, "glb_duplicate_key")
            out[key] = value
        return out

    def constant(_):
        raise Refusal(2, "glb_nonfinite")

    try:
        obj = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refusal(2, "glb_invalid") from exc
    return obj


def inspect_rig(data):
    """Inspect hierarchy, not skin quality. Refuse static geometry as a rig."""
    require(len(data) >= 20, 2, "glb_invalid")
    magic, version, total = struct.unpack_from("<4sII", data)
    require(magic == b"glTF" and version == 2 and total == len(data), 2, "glb_invalid")
    length, kind = struct.unpack_from("<I4s", data, 12)
    require(kind == b"JSON" and 0 < length <= MAX_METADATA and length % 4 == 0
            and 20 + length <= len(data), 2, "glb_invalid")
    # Validate every chunk boundary; a truncated BIN cannot authenticate a rig.
    cursor = 20 + length
    while cursor < len(data):
        require(cursor + 8 <= len(data), 2, "glb_invalid")
        size, tag = struct.unpack_from("<I4s", data, cursor)
        require(tag == b"BIN\x00" and size % 4 == 0 and cursor + 8 + size == len(data), 2, "glb_invalid")
        cursor += 8 + size
    doc = strict_asset_json(data[20:20 + length])
    require(type(doc) is dict and type(doc.get("asset")) is dict
            and doc["asset"].get("version") == "2.0", 2, "glb_invalid")
    skins, nodes = doc.get("skins", []), doc.get("nodes", [])
    require(type(skins) is list and type(nodes) is list, 2, "glb_invalid")
    require(bool(skins), 3, "rig_skin_missing")
    require(len(skins) == 1 and type(skins[0]) is dict, 3, "rig_skin_unsupported")
    ids = skins[0].get("joints")
    require(type(ids) is list and 0 < len(ids) <= 150
            and all(type(i) is int and 0 <= i < len(nodes) for i in ids)
            and len(set(ids)) == len(ids), 2, "rig_joints_invalid")
    parents = {}
    for i, node in enumerate(nodes):
        require(type(node) is dict, 2, "glb_invalid")
        children = node.get("children", [])
        require(type(children) is list, 2, "rig_hierarchy_invalid")
        for child in children:
            require(type(child) is int and 0 <= child < len(nodes)
                    and child not in parents, 2, "rig_hierarchy_invalid")
            parents[child] = i
    joints, seen = {}, set()
    for index in ids:
        chain, current = set(), index
        while current in parents:
            require(current not in chain, 2, "rig_hierarchy_invalid")
            chain.add(current)
            current = parents[current]
        node = nodes[index]
        name = node.get("name")
        require(type(name) is str and name and name not in seen, 2, "rig_joint_name")
        seen.add(name)
        require("matrix" not in node and node.get("rotation", [0, 0, 0, 1]) == [0, 0, 0, 1]
                and node.get("scale", [1, 1, 1]) == [1, 1, 1], 3, "rig_rest_transform_unsupported")
        translation = node.get("translation", [0, 0, 0])
        require(type(translation) is list and len(translation) == 3
                and all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1000000
                        for v in translation), 2, "rig_translation_invalid")
        parent = parents.get(index)
        require(parent is None or parent in ids, 3, "rig_ancestor_transform_unsupported")
        joints[name] = {"parent": None if parent is None else nodes[parent].get("name"),
                        "offset_um": [round(v * 1000000) for v in translation]}
    require(sum(j["parent"] is None for j in joints.values()) == 1, 2, "rig_hierarchy_invalid")
    return joints
