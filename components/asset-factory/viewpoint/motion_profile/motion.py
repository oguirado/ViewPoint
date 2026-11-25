import hashlib
import json
import math
import re


class Rejected(ValueError):
    pass


ROOT_CHANNELS = ("Xposition", "Yposition", "Zposition", "Zrotation", "Xrotation", "Yrotation")
JOINT_CHANNELS = ROOT_CHANNELS[3:]
IDENTITY = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
HASH = re.compile(r"[a-f0-9]{64}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
TOLERANCE_M = 0.001
POSE_TOLERANCE = 0.0000001


def demand(condition, reason):
    if not condition:
        raise Rejected(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def checked_json(data):
    def object_pairs(pairs):
        result = dict(pairs)
        demand(len(result) == len(pairs), "MALFORMED_INPUT")
        return result

    def reject_constant(_value):
        raise Rejected("MALFORMED_INPUT")

    return json.loads(data.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=reject_constant)


def shape(value, keys, reason):
    demand(type(value) is dict and value.keys() == set(keys.split()), reason)


def bounded_number(value):
    return type(value) in (int, float) and abs(value) <= 10000


def vector(value):
    return type(value) is list and len(value) == 3 and all(map(bounded_number, value))


def add(left, right):
    return tuple(a_value + b_value for a_value, b_value in zip(left, right, strict=True))


def transform(matrix, point):
    return tuple(sum(coef * component for coef, component in zip(row, point, strict=True)) for row in matrix)


def multiply(left, right):
    return tuple(zip(*(transform(left, column) for column in zip(*right, strict=True)), strict=True))


def rotation(values):
    z_angle, x_angle, y_angle = map(math.radians, values)
    z_cos, x_cos, y_cos = map(math.cos, (z_angle, x_angle, y_angle))
    z_sin, x_sin, y_sin = map(math.sin, (z_angle, x_angle, y_angle))
    z_rot = ((z_cos, -z_sin, 0), (z_sin, z_cos, 0), (0, 0, 1))
    x_rot = ((1, 0, 0), (0, x_cos, -x_sin), (0, x_sin, x_cos))
    y_rot = ((y_cos, 0, y_sin), (0, 1, 0), (-y_sin, 0, y_cos))
    return multiply(multiply(z_rot, x_rot), y_rot)


def checked_rig(raw):
    rig = checked_json(raw)
    shape(rig, "schema axes joints soles support_z_m", "RIG_FORMAT")
    demand(rig["schema"] == "vp3d.motion_fixture_rig/v1" and rig["axes"] == "RH_Z_UP_METRE"
           and bounded_number(rig["support_z_m"]), "RIG_FORMAT")
    joints = rig["joints"]
    demand(type(joints) is list and 1 <= len(joints) <= 64, "RIG_FORMAT")
    names, leaves = set(), set()
    for index, joint in enumerate(joints):
        shape(joint, "name parent offset channels end_site", "RIG_FORMAT")
        name, parent = joint["name"], joint["parent"]
        demand(type(name) is str and NAME.fullmatch(name) and name not in names, "RIG_FORMAT")
        demand((parent is None if index == 0 else type(parent) is str and parent in names)
               and vector(joint["offset"]) and (joint["end_site"] is None or vector(joint["end_site"]))
               and joint["channels"] == list(JOINT_CHANNELS if index else ROOT_CHANNELS), "RIG_FORMAT")
        leaves.discard(parent)
        leaves.add(name)
        names.add(name)
    demand(leaves == {joint["name"] for joint in joints if joint["end_site"] is not None}, "RIG_FORMAT")
    shape(rig["soles"], "left right", "SOLE_REFERENCE_INVALID")
    sole_joints = set()
    for sole in rig["soles"].values():
        shape(sole, "joint triangle", "SOLE_REFERENCE_INVALID")
        triangle, joint = sole["triangle"], sole["joint"]
        demand(type(joint) is str and joint in leaves and joint not in sole_joints and
               type(triangle) is list and len(triangle) == 3 and all(map(vector, triangle)),
               "SOLE_REFERENCE_INVALID")
        first, second, third = triangle
        edge = [value - origin for value, origin in zip(second, first, strict=True)]
        other = [value - origin for value, origin in zip(third, first, strict=True)]
        cross = [edge[left] * other[right] - edge[right] * other[left] for left, right in ((1, 2), (2, 0), (0, 1))]
        demand(math.hypot(*cross) > 1e-10, "SOLE_REFERENCE_INVALID")
        sole_joints.add(joint)
    return rig


def numeric(token):
    demand(NUMBER.fullmatch(token) is not None, "BVH_FORMAT")
    value = float(token)
    demand(bounded_number(value), "BVH_FORMAT")
    return value


def read_bvh(raw, rig):
    content = raw.decode("ascii")
    section = re.split(r"(?m)^MOTION[ \t]*\r?$", content)
    demand(len(section) == 2, "BVH_FORMAT")
    hierarchy, motion = section
    tokens = iter(re.findall(r"[^\s{}]+|[{}]", hierarchy))

    def take():
        value = next(tokens, None)
        demand(value is not None, "BVH_FORMAT")
        return value

    def expect(*values):
        demand(all(take() == value for value in values), "BVH_FORMAT")

    joints = []

    def node(parent):
        demand(len(joints) < len(rig["joints"]), "RIG_BVH_MISMATCH")
        name = take()
        expect("{", "OFFSET")
        offset = [numeric(take()) for _ in range(3)]
        expect("CHANNELS")
        channels = ROOT_CHANNELS if parent is None else JOINT_CHANNELS
        demand(take() == str(len(channels)) and [take() for _ in channels] == list(channels), "RIG_BVH_MISMATCH")
        joint = {"name": name, "parent": parent, "offset": offset, "channels": list(channels), "end_site": None}
        joints.append(joint)
        token = take()
        while token == "JOINT":
            node(name)
            token = take()
        if token == "End":
            expect("Site", "{", "OFFSET")
            joint["end_site"] = [numeric(take()) for _ in range(3)]
            expect("}")
            token = take()
        demand(token == "}", "BVH_FORMAT")

    expect("HIERARCHY", "ROOT")
    node(None)
    demand(next(tokens, None) is None, "BVH_FORMAT")
    demand(joints == rig["joints"], "RIG_BVH_MISMATCH")
    headers = motion.strip().splitlines()
    demand(len(headers) >= 3, "BVH_FORMAT")
    count = re.fullmatch(r"Frames:\s*([0-9]{1,3})\s*", headers[0])
    interval = re.fullmatch(r"Frame Time:\s*(\S+)\s*", headers[1])
    demand(count is not None and interval is not None, "BVH_FORMAT")
    frame_count, frame_time = int(count[1]), numeric(interval[1])
    demand(1 <= frame_count <= 256 and frame_count == len(headers) - 2 and 0 < frame_time <= 1, "BVH_FORMAT")
    width = sum(len(joint["channels"]) for joint in joints)
    rows = [row.split() for row in headers[2:]]
    demand(all(len(row) == width for row in rows), "BVH_FORMAT")
    return hierarchy, headers[:2], [[numeric(token) for token in row] for row in rows], frame_time


def measure(rig, rows, frame_time):
    timeline = []
    onsets = {foot: [] for foot in rig["soles"]}
    previous = {foot: False for foot in onsets}
    penetrations = []
    for frame, row in enumerate(rows):
        joints, offset = {}, 0
        for joint in rig["joints"]:
            width = len(joint["channels"])
            values = row[offset:offset+width]
            offset += width
            parent = joints.get(joint["parent"])
            if parent is None:
                position = add(joint["offset"], values[:3])
                basis = rotation(values[3:])
            else:
                position = add(parent["position_m"], transform(parent["basis"], joint["offset"]))
                basis = multiply(parent["basis"], rotation(values))
            end = joint["end_site"]
            joints[joint["name"]] = {"position_m": position, "basis": basis,
                                     "end_site_m": None if end is None else add(position, transform(basis, end))}
        soles = {}
        for foot, definition in rig["soles"].items():
            joint = joints[definition["joint"]]
            triangle = [add(joint["position_m"], transform(joint["basis"], point))
                        for point in definition["triangle"]]
            heights = [point[2] - rig["support_z_m"] for point in triangle]
            touching = all(abs(height) <= TOLERANCE_M for height in heights)
            if touching and not previous[foot]:
                onsets[foot].append(frame)
            if min(heights) < -TOLERANCE_M:
                penetrations.append({"frame": frame, "foot": foot})
            previous[foot] = touching
            soles[foot] = {"triangle_m": triangle, "contact": touching}
        timeline.append({"time_s": frame * frame_time, "joints": joints, "soles": soles})
    return {"timeline": timeline, "contact_onsets_frame": onsets, "penetrations": penetrations,
            "frame_time_s": frame_time, "support_z_m": rig["support_z_m"], "contact_tolerance_m": TOLERANCE_M}


def same(actual, expected):
    if isinstance(actual, dict):
        return (type(expected) is dict and actual.keys() == expected.keys()
                and all(same(actual[key], expected[key]) for key in actual))
    if isinstance(actual, (tuple, list)):
        return type(expected) is list and len(actual) == len(expected) and all(
            same(left, right) for left, right in zip(actual, expected, strict=True))
    if type(actual) is float:
        return bounded_number(expected) and math.isclose(actual, expected, rel_tol=0, abs_tol=POSE_TOLERANCE)
    return type(actual) is type(expected) and actual == expected


def compare(actual, expected):
    demand(not actual["penetrations"], "SOLE_PENETRATION")
    demand(actual["contact_onsets_frame"] == expected.get("contact_onsets_frame"), "CONTACT_ONSET_MISMATCH")
    demand(same(actual, expected), "STRUCTURAL_MOTION_MISMATCH")
