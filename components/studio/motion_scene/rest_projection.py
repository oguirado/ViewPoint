# SPDX-License-Identifier: AGPL-3.0-only
"""Project a metre-scale BVH through the rig's rest basis without changing its skin."""
from dataclasses import replace
from decimal import Decimal
import re

import bvh
from evidence import checked_json, demand, shape
from rest_adapter import bind_rest

MAP_SCHEMA = "vp3d.bvh_retarget_map/v1"
POSITION = ("Xposition", "Yposition", "Zposition")
MAX_DERIVED_BYTES = 400_000


def decimal(value, reason):
    demand(type(value) in (int, float) and abs(value) <= 10_000, reason)
    return Decimal(str(value))


def vector(value, reason):
    demand(type(value) is list and len(value) == 3, reason)
    return tuple(decimal(item, reason) for item in value)


def skeleton(raw, *, target):
    reason = "RETARGET_MAP_INVALID"
    mapping = checked_json(raw)
    shape(mapping, "schema basis metres_scale joints feet", reason)
    demand(mapping["schema"] == MAP_SCHEMA and mapping["basis"] == "RH_Y_UP_METRE", reason)
    scale = mapping["metres_scale"]
    demand(type(scale) is str and re.fullmatch(r"[1-9][0-9]{0,5}/[1-9][0-9]{0,5}", scale), reason)
    numerator, denominator = map(Decimal, scale.split("/"))
    scale = numerator / denominator
    demand(Decimal("0.001") <= scale <= 1000, reason)
    rows = mapping["joints"]
    demand(type(rows) is list and 1 < len(rows) <= bvh.MAX_JOINTS, reason)
    for row in rows:
        shape(row, "source target parent source_rest_m target_rest_m source_end_m target_end_m rotation_order", reason)
        for name in ("source", "target"):
            demand(type(row[name]) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", row[name]), reason)
    for name in ("source", "target"):
        demand(len({row[name] for row in rows}) == len(rows), reason)
    feet, markers = {}, mapping["feet"]
    shape(markers, "left right", reason)
    for foot, marker in markers.items():
        shape(marker, "joint local_m floor_y_m", reason)
        demand(type(marker["joint"]) is str and marker["joint"] in {row["target"] for row in rows[1:]}, reason)
        feet[foot] = (marker["joint"], vector(marker["local_m"], reason), decimal(marker["floor_y_m"], reason))
    foot_names = {foot[0] for foot in feet.values()}
    demand(len(foot_names) == len(feet), reason)

    joints = []
    cursor = 0
    parents = {row["parent"] for row in rows if type(row["parent"]) is int}
    side = "target" if target else "source"
    for index, row in enumerate(rows):
        parent, order = row["parent"], row["rotation_order"]
        demand((parent is None if index == 0 else type(parent) is int and 0 <= parent < index)
               and type(order) is str and len(order) == 3 and set(order) == set("XYZ"), reason)
        position = index == 0 or (target and row["target"] in foot_names)
        channels = (POSITION if position else ()) + tuple(axis + "rotation" for axis in order)
        end = row[side + "_end_m"]
        demand((end is None) == (index in parents) and (row["target"] not in foot_names or index not in parents),
               reason)
        joints.append(bvh.Joint(row[side], parent, vector(row[side + "_rest_m"], reason), channels, cursor,
                                None if end is None else vector(end, reason)))
        cursor += len(channels)
    return tuple(joints), scale, feet


def render(clip):
    children = {index: [] for index in range(len(clip.joints))}
    for index, joint in enumerate(clip.joints[1:], 1):
        children[joint.parent].append(index)
    lines = ["HIERARCHY"]

    def node(index, indent):
        joint = clip.joints[index]
        keyword = "ROOT" if index == 0 else "JOINT"
        lines.extend((f"{indent}{keyword} {joint.name}", indent + "{",
                      indent + "  OFFSET " + " ".join(map(str, joint.offset)),
                      indent + f"  CHANNELS {len(joint.channels)} " + " ".join(joint.channels)))
        for child in children[index]:
            node(child, indent + "  ")
        if joint.end_site is not None:
            lines.extend((indent + "  End Site", indent + "  {",
                          indent + "    OFFSET " + " ".join(map(str, joint.end_site)), indent + "  }"))
        lines.append(indent + "}")

    node(0, "")
    lines.extend(("MOTION", f"Frames: {len(clip.rows)}", f"Frame Time: {clip.frame_time}"))
    lines.extend(" ".join(map(str, row)) for row in clip.rows)
    raw = ("\n".join(lines) + "\n").encode("ascii")
    demand(len(raw) <= MAX_DERIVED_BYTES, "DERIVED_OUTPUT_BUDGET")
    return raw


def retarget(clip, source, target, scale):
    demand(clip.joints == source, "SOURCE_REST_MISMATCH")
    rows = []
    for row in clip.rows:
        values = [value * scale for value in row[:3]]
        for old, new in zip(source, target, strict=True):
            if old.parent is not None and len(new.channels) == 6:
                values.extend((Decimal(0),) * 3)
            width = len(old.channels)
            values.extend(row[old.start + width - 3:old.start + width])
        rows.append(tuple(values))
    return replace(clip, joints=target, rows=tuple(rows))


def retarget_unirig(clip, map_raw, rig_bytes, *, admitted_rig_sha256, semantics):
    """Bridge a caller-admitted UniRig asset to the supplied-BVH retarget path."""
    source, scale, _ = skeleton(map_raw, target=False)
    target, _, markers = skeleton(map_raw, target=True)
    binding = bind_rest(rig_bytes, admitted_rig_sha256, source, target, markers,
                        semantics, clip.frame_time, len(clip.rows))
    return retarget(clip, source, target, scale), binding
