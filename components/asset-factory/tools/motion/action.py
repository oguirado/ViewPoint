"""Encode and decode the fixture's glTF animation without replacing its skin."""
from bisect import bisect_left
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
import json
import math
import struct

from providers.unirig.common import require
from providers.unirig.gate import GLB

MAX_ACTION_BYTES = 8 * 2**20
MAX_ACCESSOR_ROWS = 16_384
WIDTH = {"translation": 3, "rotation": 4}
IDENTITY_ROTATION = (0.0, 0.0, 0.0, 1.0)


def unit(q):
    length = math.hypot(*q)
    return tuple(v / length for v in q)


def quaternion_product(a, b):
    x, y, z, w = a
    i, j, k, s = b
    return (w*i + x*s + y*k - z*j, w*j - x*k + y*s + z*i,
            w*k + x*j - y*i + z*s, w*s - x*i - y*j - z*k)


def relative_angle(a, b):
    inverse = (-a[0], -a[1], -a[2], a[3])
    x, y, z, w = quaternion_product(inverse, b)
    return 2 * math.atan2(math.hypot(x, y, z), abs(w))


def slerp(a, b, t):
    dot = math.fsum(x*y for x, y in zip(a, b))
    if dot < 0:
        b = tuple(-v for v in b)
        dot = -dot
    if dot >= 1 - 1e-12:
        return unit(tuple(x + t*(y - x) for x, y in zip(a, b)))
    angle = math.acos(min(dot, 1))
    return unit(tuple((x*math.sin((1 - t)*angle) + y*math.sin(t*angle))
                      / math.sin(angle) for x, y in zip(a, b)))


@dataclass(frozen=True)
class Track:
    node: int
    path: str
    times: tuple
    rows: tuple
    interpolation: str = "LINEAR"

    def sample(self, pts):
        end = bisect_left(self.times, pts)
        cubic = self.interpolation == "CUBICSPLINE"
        if end == 0 or (end < len(self.times) and self.times[end] == pts):
            return self.rows[3*end + 1 if cubic else end]
        if end == len(self.times):
            return self.rows[-2 if cubic else -1]
        start = end - 1
        if self.interpolation == "STEP":
            return self.rows[start]
        interval = self.times[end] - self.times[start]
        t = float((pts - self.times[start]) / interval)
        if not cubic:
            a, b = self.rows[start], self.rows[end]
            if self.path == "rotation":
                return slerp(a, b, t)
            return tuple(x + t*(y - x) for x, y in zip(a, b))
        dt = float(interval)
        t2, t3 = t*t, t*t*t
        a, out_tangent = self.rows[3*start + 1:3*start + 3]
        in_tangent, b = self.rows[3*end:3*end + 2]
        return tuple((2*t3 - 3*t2 + 1)*x + (t3 - 2*t2 + t)*dt*u
                     + (-2*t3 + 3*t2)*y + (t3 - t2)*dt*v
                     for x, u, v, y in zip(a, out_tangent, in_tangent, b))


def open_glb(data, deadline_ns):
    require(type(data) is bytes, 2, "action_bytes_invalid")
    require(len(data) <= MAX_ACTION_BYTES, 6, "action_byte_budget")
    return GLB(data, deadline_ns, MAX_ACCESSOR_ROWS)


def _covers_duration(key, duration):
    if key == duration:
        return True
    if key <= 0:
        return False
    (bits,) = struct.unpack("<I", struct.pack("<f", key))
    # Binary32 spacing is asymmetric at powers of two. Use the neighbor
    # toward the declared time and compare the midpoint in exact arithmetic.
    neighbor_bits = bits + (1 if duration > key else -1)
    (neighbor,) = struct.unpack("<f", struct.pack("<I", neighbor_bits))
    return math.isfinite(neighbor) and 2 * abs(key - duration) <= abs(Fraction(neighbor) - key)


def decode(data, skin, *, name, duration, deadline_ns):
    """Read a standalone animation GLB and authenticate the static source."""
    clip = open_glb(data, deadline_ns)
    doc, original = clip.doc, skin.doc
    mutable = {"animations", "accessors", "bufferViews", "buffers"}
    require({k: v for k, v in doc.items() if k not in mutable}
            == {k: v for k, v in original.items() if k not in mutable},
            5, "action_skin_mismatch")
    first_accessor = len(original["accessors"])
    first_view = len(original["bufferViews"])
    require(doc["accessors"][:first_accessor] == original["accessors"] and
            doc["bufferViews"][:first_view] == original["bufferViews"] and
            clip.buffer_length >= skin.buffer_length and
            clip.binary[:skin.buffer_length] == skin.binary[:skin.buffer_length],
            5, "action_skin_mismatch")
    require(doc["buffers"][0] == original["buffers"][0] | {"byteLength": clip.buffer_length},
            5, "action_skin_mismatch")
    animations = doc.get("animations")
    require(type(animations) is list and len(animations) == 1, 3, "one_action_required")
    animation = animations[0]
    require(type(animation) is dict and animation.keys() <= {"name", "channels", "samplers"},
            3, "animation_fields_unsupported")
    require(animation.get("name") == name, 4, "action_name_stale")
    channels, samplers = animation["channels"], animation["samplers"]
    joints = set(original["skins"][0]["joints"])
    require(type(channels) is list and type(samplers) is list, 2, "action_channels_invalid")
    require(0 < len(channels) <= len(joints) * len(WIDTH) and len(samplers) == len(channels),
            2, "action_channels_invalid")

    def read_accessor(index, kind):
        require(type(index) is int and first_accessor <= index < len(doc["accessors"]),
                2, "animation_accessor_index")
        view = doc["accessors"][index].get("bufferView")
        require(type(view) is int and first_view <= view < len(doc["bufferViews"]) and
                doc["bufferViews"][view].get("byteOffset", 0) >= skin.buffer_length,
                2, "animation_buffer_view")
        require(not doc["bufferViews"][view].get("extensions"), 3, "animation_buffer_extension")
        rows, _ = clip.accessor(index, kind, (5126,))
        return rows

    tracks = {}
    used_samplers = set()
    for channel in channels:
        require(type(channel) is dict and set(channel) == {"target", "sampler"},
                3, "animation_channel_fields_unsupported")
        target = channel["target"]
        require(type(target) is dict and set(target) == {"node", "path"},
                3, "animation_target_unsupported")
        node, path, index = target["node"], target["path"], channel["sampler"]
        require(type(node) is int and node in joints, 5, "animation_joint_mismatch")
        require(path in WIDTH, 3, "animation_path_unsupported")
        require((node, path) not in tracks, 2, "duplicate_animation_channel")
        require(type(index) is int and 0 <= index < len(samplers) and index not in used_samplers,
                2, "animation_sampler_index")
        used_samplers.add(index)
        sampler = samplers[index]
        require(type(sampler) is dict and sampler.keys() <= {"input", "output", "interpolation"},
                3, "animation_sampler_fields_unsupported")
        mode = sampler.get("interpolation", "LINEAR")
        require(mode in ("LINEAR", "STEP") or (mode == "CUBICSPLINE" and path == "translation"),
                3, "animation_interpolation_unsupported")
        times = tuple(Fraction(row[0]) for row in read_accessor(sampler["input"], "SCALAR"))
        require(len(times) >= 2 and times[0] == 0 and _covers_duration(times[-1], duration),
                5, "channel_time_coverage")
        require(all(a < b for a, b in zip(times, times[1:])), 2, "channel_pts_order")
        time_accessor = doc["accessors"][sampler["input"]]
        require(time_accessor.get("min") == [times[0]] and time_accessor.get("max") == [times[-1]],
                2, "channel_pts_metadata")
        rows = tuple(read_accessor(sampler["output"], "VEC" + str(WIDTH[path])))
        require(len(rows) == len(times) * (3 if mode == "CUBICSPLINE" else 1),
                2, "channel_sample_count")
        if path == "rotation":
            require(all(abs(math.hypot(*row) - 1) <= 1e-5 for row in rows), 5, "rotation_not_unit")
            rows = tuple(unit(row) for row in rows)
        tracks[node, path] = Track(node, path, times, rows, mode)
    return tracks


def encode(skin, name, tracks):
    """Append native glTF animation samplers to a verified static fixture GLB."""
    doc = deepcopy(skin.doc)
    binary = bytearray(skin.binary[:skin.buffer_length])

    def accessor(rows, width, *, time_input=False):
        binary.extend(b"\0" * (-len(binary) % 4))
        offset = len(binary)
        for row in rows:
            binary.extend(struct.pack("<" + "f"*width, *row))
        view_idx = len(doc["bufferViews"])
        doc["bufferViews"].append({"buffer": 0, "byteOffset": offset, "byteLength": len(binary) - offset})
        acc = {"bufferView": view_idx, "componentType": 5126, "count": len(rows),
               "type": "SCALAR" if width == 1 else "VEC" + str(width)}
        if time_input:
            acc.update(min=[struct.unpack_from("<f", binary, offset)[0]],
                       max=[struct.unpack_from("<f", binary, len(binary)-4)[0]])
        doc["accessors"].append(acc)
        return len(doc["accessors"]) - 1

    channels, samplers = [], []
    for track in tracks:
        channels.append({"target": {"node": track.node, "path": track.path}, "sampler": len(samplers)})
        samplers.append({"input": accessor(tuple((t,) for t in track.times), 1, time_input=True),
                         "output": accessor(track.rows, WIDTH[track.path]),
                         "interpolation": track.interpolation})
    doc["animations"] = [{"name": name, "channels": channels, "samplers": samplers}]
    return pack_glb(doc, binary)


def pack_glb(doc, binary):
    doc = doc | {"buffers": [doc["buffers"][0] | {"byteLength": len(binary)}]}
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    text += b" " * (-len(text) % 4)
    padded = bytes(binary) + b"\0" * (-len(binary) % 4)
    return (struct.pack("<4sIIII", b"glTF", 2, 28 + len(text) + len(padded),
                        len(text), 0x4E4F534A) + text
            + struct.pack("<II", len(padded), 0x004E4942) + padded)
