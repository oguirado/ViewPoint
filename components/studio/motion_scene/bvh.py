# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded BVH reader and forward kinematics; no model or Blender imports.

BVH decimal channels are asset bytes. Only integer measurements enter receipts.
Supported convention: right handed Y up, root XYZ translations then three
distinct Euler rotation channels (any order), rotation-only child joints by default. Derived clips may opt into
XYZ translations on child joints, applied before local rotation.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
import time


class Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason
        super().__init__(reason)


def require(condition, rc, reason):
    if not condition:
        raise Refusal(rc, reason)


def check_time(deadline_ns):
    require(type(deadline_ns) is int, 2, "deadline_invalid")
    require(time.monotonic_ns() < deadline_ns, 6, "deadline_exceeded")


@dataclass(frozen=True)
class Joint:
    name: str
    parent: int | None
    offset: tuple
    channels: tuple
    start: int
    end_site: tuple | None = None


@dataclass(frozen=True)
class Clip:
    joints: tuple
    rows: tuple
    frame_time: Decimal
    frame_hashes: tuple


# [TARGET, DESIGN_CHOICE] parser/work limits, independent of model peak RAM.
MAX_BYTES = 16 * 2**20
MAX_JOINTS = 150
MAX_FRAMES = 10000
MAX_SAMPLES = 1500000
IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def number(token):
    require(len(token) <= 64 and _NUMBER.fullmatch(token) is not None, 2, "bvh_number_invalid")
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise Refusal(2, "bvh_number_invalid") from exc
    require(value.is_finite() and abs(value) <= 10**9, 2, "bvh_number_invalid")
    return value


def parse(data, *, deadline_ns=None, allow_joint_translation=False):
    if deadline_ns is None:
        deadline_ns = time.monotonic_ns() + 10**10
    check_time(deadline_ns)
    require(type(data) is bytes and 0 < len(data) <= MAX_BYTES, 6, "bvh_byte_budget")
    try:
        text = data.decode("ascii")
    except UnicodeError as exc:
        raise Refusal(2, "bvh_encoding") from exc
    require("\x00" not in text, 2, "bvh_encoding")
    pieces = re.split(r"(?m)^\s*MOTION\s*$", text)
    require(len(pieces) == 2, 2, "bvh_motion_section")
    tokens = re.findall(r"\{|\}|[^\s{}]+", pieces[0])
    pos, joints, channel_count = 0, [], 0

    def take(expected=None):
        nonlocal pos
        require(pos < len(tokens), 2, "bvh_truncated")
        value = tokens[pos]
        pos += 1
        require(expected is None or value == expected, 2, "bvh_structure")
        return value

    def vec():
        return tuple(number(take()) for _ in range(3))

    def joint(parent):
        nonlocal channel_count
        check_time(deadline_ns)
        require(len(joints) < MAX_JOINTS, 6, "bvh_joint_budget")
        name = take()
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", name) is not None
                and all(j.name != name for j in joints), 2, "bvh_joint_name")
        take("{")
        take("OFFSET")
        offset = vec()
        take("CHANNELS")
        count = take()
        counts = {"6"} if parent is None else {"3", "6"} if allow_joint_translation else {"3"}
        require(count in counts, 2, "bvh_channels")
        channels = tuple(take() for _ in range(int(count)))
        rotation = channels[-3:]
        require(set(rotation) == {"Xrotation", "Yrotation", "Zrotation"}, 2, "bvh_channels")
        if count == "6":
            require(channels[:3] == ("Xposition", "Yposition", "Zposition"), 2, "bvh_channels")
        index = len(joints)
        joints.append(Joint(name, parent, offset, channels, channel_count))
        channel_count += len(channels)
        end_seen = False
        while True:
            token = take()
            if token == "}":
                return
            if token == "JOINT":
                require(not end_seen, 2, "bvh_structure")
                joint(index)
            elif token == "End":
                require(not end_seen, 2, "bvh_structure")
                take("Site")
                take("{")
                take("OFFSET")
                end = vec()
                joints[index] = Joint(name, parent, offset, channels, joints[index].start, end)
                take("}")
                end_seen = True
            else:
                raise Refusal(2, "bvh_structure")

    take("HIERARCHY")
    take("ROOT")
    joint(None)
    require(pos == len(tokens), 2, "bvh_structure")
    lines = [line.strip() for line in pieces[1].splitlines() if line.strip()]
    require(len(lines) >= 2 and re.fullmatch(r"Frames:\s*\d+", lines[0]) is not None,
            2, "bvh_frame_header")
    frames = int(lines[0].split(":")[1])
    require(0 < frames <= MAX_FRAMES and frames * len(joints) <= MAX_SAMPLES, 6, "bvh_frame_budget")
    match = re.fullmatch(r"Frame\s+Time:\s*(\S+)", lines[1])
    require(match is not None, 2, "bvh_frame_header")
    frame_time = number(match[1])
    require(0 < frame_time <= 1, 2, "bvh_frame_time")
    require(len(lines) - 2 == frames, 5, "frame_count_mismatch")
    rows, hashes = [], []
    for line in lines[2:]:
        check_time(deadline_ns)
        parts = line.split()
        require(len(parts) == channel_count, 2, "bvh_channel_count")
        row = tuple(number(token) for token in parts)
        rows.append(row)
        # Hash the parsed channel row, not presentation whitespace.
        normalized = " ".join(str(v.normalize()) for v in row).encode("ascii")
        hashes.append(hashlib.sha256(normalized).hexdigest())
    return Clip(tuple(joints), tuple(rows), frame_time, tuple(hashes))


def multiply(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def rotate(matrix, vector):
    return tuple(sum(matrix[i][j] * vector[j] for j in range(3)) for i in range(3))


def rotation(axis, degrees):
    theta = math.radians(float(degrees % 360))
    c, s = math.cos(theta), math.sin(theta)
    if axis == "X":
        return ((1, 0, 0), (0, c, -s), (0, s, c))
    if axis == "Y":
        return ((c, 0, s), (0, 1, 0), (-s, 0, c))
    return ((c, -s, 0), (s, c, 0), (0, 0, 1))


def forward(clip, row, units_um):
    positions, world_rotations, local_rotations = [], [], []
    for joint in clip.joints:
        values = dict(zip(joint.channels, row[joint.start:joint.start + len(joint.channels)]))
        position = tuple(float(v * units_um) for v in joint.offset)
        local = IDENTITY
        for channel in joint.channels:
            if channel.endswith("rotation"):
                local = multiply(local, rotation(channel[0], values[channel]))
        if "Xposition" in values:
            position = tuple(position[i] + float(values[axis + "position"] * units_um)
                             for i, axis in enumerate("XYZ"))
        if joint.parent is None:
            world = local
        else:
            delta = rotate(world_rotations[joint.parent], position)
            position = tuple(positions[joint.parent][i] + delta[i] for i in range(3))
            world = multiply(world_rotations[joint.parent], local)
        positions.append(position)
        world_rotations.append(world)
        local_rotations.append(local)
    return positions, world_rotations, local_rotations


def rotation_delta_mdeg(a, b):
    # trace(A^T B) is stable across Euler wrap and gimbal representations.
    cosine = (sum(a[i][j] * b[i][j] for i in range(3) for j in range(3)) - 1) / 2
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine)))) * 1000
    return math.ceil(max(0.0, angle - 1e-6))
