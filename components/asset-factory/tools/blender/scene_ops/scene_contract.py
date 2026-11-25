# SPDX-License-Identifier: AGPL-3.0-only
"""Closed request shapes and Blender-independent scene comparison."""

import json
from math import atan2, degrees, isfinite, sqrt


class Refusal(Exception):
    def __init__(self, code, detail="", **evidence):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.evidence = evidence


def require(condition, code, detail="", **evidence):
    if not condition:
        raise Refusal(code, detail, **evidence)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def fields(value, required, optional=()):
    required = set(required)
    require(isinstance(value, dict) and required <= value.keys()
            and value.keys() <= required.union(optional), "INVALID_FIELDS")


def name(value):
    require(isinstance(value, str) and value and "\x00" not in value
            and len(value.encode("utf-8")) <= 63, "INVALID_NAME")


OPERATIONS = {
    "object.rename": ("name",),
    "object.set_location": ("location",),
    "parent.set": ("parent",),
    "material.assign": ("slot", "material"),
    "constraint.add": ("name", "type", "target", "influence"),
    "constraint.remove": ("name",),
}
CONSTRAINTS = {"COPY_LOCATION", "COPY_ROTATION"}


def number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and isfinite(value)


def request(value):
    require(isinstance(value, dict), "INVALID_REQUEST")
    operation = value.get("operation")
    if operation == "af.scene.inspect":
        fields(value, ("operation", "blend", "base_revision_sha256"))
    elif operation == "af.scene.diff":
        fields(value, ("operation", "before", "before_sha256", "after", "after_sha256"))
    elif operation == "af.scene.edit":
        fields(value, ("operation", "blend", "base_revision_sha256", "scene", "edits",
                       "output_blend"), ("reviewed_retry_revision_sha256",))
        name(value["scene"])
        edits = value["edits"]
        require(isinstance(edits, list) and 1 <= len(edits) <= 64, "INVALID_EDIT_COUNT")
        for edit in edits:
            require(isinstance(edit, dict) and isinstance(edit.get("op"), str)
                    and edit["op"] in OPERATIONS, "OPERATION_NOT_ALLOWED")
            op = edit["op"]
            fields(edit, ("op", "object", *OPERATIONS[op]))
            for key in ("object", "name", "target", "material"):
                if key in edit:
                    name(edit[key])
            if op == "parent.set" and edit["parent"] is not None:
                name(edit["parent"])
            if op == "material.assign":
                require(type(edit["slot"]) is int and edit["slot"] >= 0, "INVALID_SLOT")
            if op == "object.set_location":
                xyz = edit["location"]
                require(isinstance(xyz, list) and len(xyz) == 3
                        and all(number(x, -1e6, 1e6) for x in xyz), "INVALID_LOCATION")
            if op == "constraint.add":
                require(isinstance(edit["type"], str) and edit["type"] in CONSTRAINTS,
                        "CONSTRAINT_NOT_ALLOWED")
                require(number(edit["influence"], 0, 1), "INVALID_INFLUENCE")
    else:
        raise Refusal("OPERATION_NOT_ALLOWED")
    return operation


def vector_close(before, after, tolerance):
    return len(before) == len(after) and all(abs(a - b) <= tolerance for a, b in zip(before, after))


def rotation_close(before, after):
    # Angular distance in degrees, with q and -q treated as the same orientation.
    def normalized(q):
        scale = sqrt(sum(x * x for x in q))
        return tuple(x / scale for x in q)

    require(len(before) == len(after) == 4 and any(before) and any(after), "INVALID_SNAPSHOT")
    a, b = normalized(before), normalized(after)
    difference = sqrt(sum((x - y)**2 for x, y in zip(a, b)))
    opposite = sqrt(sum((x + y)**2 for x, y in zip(a, b)))
    return degrees(4 * atan2(min(difference, opposite), max(difference, opposite))) <= 0.001


def transforms(before, after):
    try:
        require(before.keys() == after.keys() == {"location", "rotation_wxyz", "scale"},
                "INVALID_SNAPSHOT")
        change = {}
        for field in before:
            a, b = before[field], after[field]
            size = 4 if field == "rotation_wxyz" else 3
            require(isinstance(a, list) and isinstance(b, list) and len(a) == len(b) == size
                    and all(number(x, -1e40, 1e40) for x in a + b), "INVALID_SNAPSHOT")
            equal = rotation_close(a, b) if field == "rotation_wxyz" else vector_close(a, b, 1e-6)
            if not equal:
                change[field] = {"before": a, "after": b}
        return change
    except AttributeError as exc:
        raise Refusal("INVALID_SNAPSHOT") from exc


def compare(before, after):
    old, new = before["objects"], after["objects"]
    changes = {}
    for key in sorted(old.keys() & new.keys()):
        a, b = old[key], new[key]
        require(isinstance(a, dict) and isinstance(b, dict), "INVALID_SNAPSHOT")
        object_changes = {}
        for field in sorted(a.keys() | b.keys()):
            av, bv = a.get(field), b.get(field)
            if field in {"local_transform", "world_transform"} and av is not None and bv is not None:
                delta = transforms(av, bv)
                if delta:
                    object_changes[field] = delta
            elif av != bv:
                object_changes[field] = {"before": av, "after": bv}
        if object_changes:
            changes[key] = object_changes
    return {"schema": "vp3d.scene_diff/v1",
            "before_revision_sha256": before.get("revision_sha256"),
            "after_revision_sha256": after.get("revision_sha256"),
            "added": {key: new[key] for key in sorted(new.keys() - old.keys())},
            "removed": {key: old[key] for key in sorted(old.keys() - new.keys())},
            "changed": changes,
            "scene_changes": {key: {"before": before.get(key), "after": after.get(key)}
                              for key in ("scenes", "active_scene", "materials")
                              if before.get(key) != after.get(key)}}
