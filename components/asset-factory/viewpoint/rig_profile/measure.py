import json
import math
import struct


class Rejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


def exact(record, *keys):
    require(isinstance(record, dict) and record.keys() == set(keys), "GLB_OUT_OF_PROFILE")


def indices_equal(actual, expected):
    return actual == expected and all(type(index) is int for index in actual)


def unique(pairs):
    record = dict(pairs)
    require(len(record) == len(pairs), "GLB_DUPLICATE_MEMBER")
    return record


def pack(document, binary):
    head = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    head += b" " * (-len(head) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    return (struct.pack("<4sIII4s", b"glTF", 2, 28 + len(head) + len(binary), len(head), b"JSON")
            + head + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


def unpack(raw):
    require(28 <= len(raw) <= 1024 * 1024, "GLB_SIZE")
    magic, version, size, json_size, json_kind = struct.unpack_from("<4sIII4s", raw)
    boundary = 20 + json_size
    require((magic, version, size, json_kind) == (b"glTF", 2, len(raw), b"JSON")
            and json_size % 4 == 0 and boundary + 8 <= len(raw), "GLB_HEADER")
    bin_size, kind = struct.unpack_from("<I4s", raw, boundary)
    base = boundary + 8
    require(kind == b"BIN\0" and bin_size % 4 == 0 and base + bin_size == len(raw), "GLB_BINARY")
    return json.loads(raw[20:boundary], object_pairs_hook=unique), base


def vector(values, size):
    require(isinstance(values, (tuple, list)) and len(values) == size
            and all(type(value) in (int, float) and math.isfinite(value) and abs(value) <= 10000
                    for value in values), "RIG_VECTOR")
    return values


def translation_matrix(offset):
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, *offset, 1]


def transform(rotation, translation, point):
    axis, scalar = rotation[:3], rotation[3]

    def cross(left, right):
        return [left[1] * right[2] - left[2] * right[1],
                left[2] * right[0] - left[0] * right[2],
                left[0] * right[1] - left[1] * right[0]]

    twice = [2 * value for value in cross(axis, point)]
    return [coord + scalar * first + second + shift
            for coord, first, second, shift in zip(point, twice, cross(axis, twice), translation)]


def measure(raw, *, diagnose=False):
    try:
        return _measure(raw, diagnose)
    except (IndexError, KeyError, TypeError, OverflowError, UnicodeError, struct.error,
            json.JSONDecodeError) as error:
        raise Rejected("GLB_MALFORMED") from error


def _measure(raw, diagnose):
    document, base = unpack(raw)
    require(isinstance(document, dict), "GLB_OUT_OF_PROFILE")
    require(bool(document.get("meshes")), "RIG_SKELETON_ONLY")
    exact(document, "asset", "scene", "scenes", "nodes", "meshes", "skins", "animations",
          "buffers", "bufferViews", "accessors")
    require(document["asset"] == {"version": "2.0"} and type(document["scene"]) is int and document["scene"] == 0
            and document["scenes"] == [{"nodes": [0, 2]}]
            and indices_equal(document["scenes"][0]["nodes"], [0, 2]), "GLB_SCENE")
    require(len(document["nodes"]) == 3 and len(document["meshes"]) == len(document["skins"])
            == len(document["buffers"]) == len(document["animations"]) == 1, "GLB_RIG_CARDINALITY")
    root, tip, mesh = document["nodes"]
    exact(root, "name", "translation", "children")
    exact(tip, "name", "translation")
    exact(mesh, "name", "mesh", "skin")
    names = [root["name"], tip["name"], mesh["name"]]
    require(all(isinstance(name, str) and 1 <= len(name) <= 48 and name.isascii() and name.isprintable()
                for name in names) and len(set(names)) == 3 and indices_equal(root["children"], [1])
            and indices_equal([mesh["mesh"], mesh["skin"]], [0, 0]), "RIG_HIERARCHY")
    offsets = [vector(node["translation"], 3) for node in (root, tip)]
    require(any(offsets[1]), "RIG_DEGENERATE_BIND")
    skin = document["skins"][0]
    exact(skin, "skeleton", "joints", "inverseBindMatrices")
    require(indices_equal([skin["skeleton"]], [0]) and indices_equal(skin["joints"], [0, 1]), "RIG_JOINT_ORDER")
    buffer = document["buffers"][0]
    exact(buffer, "byteLength")
    length = buffer["byteLength"]
    require(type(length) is int and 0 <= len(raw) - base - length <= 3, "GLB_BUFFER")
    used = {}
    ranges = {}

    def accessor(index, shape, component, width):
        require(type(index) is int and 0 <= index < len(document["accessors"]), "GLB_ACCESSOR_INDEX")
        item = document["accessors"][index]
        required = ("bufferView", "componentType", "count", "type")
        if (shape, component) in (("VEC3", 5126), ("SCALAR", 5126)) and "min" in item:
            required += ("min", "max")
        exact(item, *required)
        count = item["count"]
        fmt = "<" + {5126: "f", 5123: "H", 5121: "B"}[component] * width
        stride = struct.calcsize(fmt)
        require(item["type"] == shape and item["componentType"] == component and type(count) is int
                and 1 <= count <= 1024, "GLB_ACCESSOR_FORMAT")
        view_index = item["bufferView"]
        require(type(view_index) is int and 0 <= view_index < len(document["bufferViews"]),
                "GLB_BUFFER_VIEW_INDEX")
        view = document["bufferViews"][view_index]
        exact(view, "buffer", "byteOffset", "byteLength")
        offset = view["byteOffset"]
        require(indices_equal([view["buffer"], view["byteLength"]], [0, count * stride])
                and type(offset) is int and offset % 4 == 0 and 0 <= offset <= length - count * stride,
                "GLB_ACCESSOR_BOUNDS")
        require(index not in used or used[index] == (shape, component, width), "GLB_ACCESSOR_ALIAS")
        require(view_index not in ranges or ranges[view_index][2] == index, "GLB_VIEW_ALIAS")
        used[index] = (shape, component, width)
        ranges[view_index] = (offset, offset + count * stride, index)
        start = base + offset
        rows = [struct.unpack_from(fmt, raw, start + row * stride) for row in range(count)]
        for row in rows:
            vector(row, width)
        if "min" in item:
            require(item["min"] == [min(column) for column in zip(*rows)]
                    and item["max"] == [max(column) for column in zip(*rows)], "GLB_ACCESSOR_EXTREMA")
        return rows, start

    primitive_list = document["meshes"][0]["primitives"]
    exact(document["meshes"][0], "primitives")
    require(len(primitive_list) == 1, "GLB_SINGLE_PRIMITIVE")
    primitive = primitive_list[0]
    exact(primitive, "attributes", "indices", "mode")
    attrs = primitive["attributes"]
    require({"WEIGHTS_0", "JOINTS_0"} <= attrs.keys(), "RIG_UNWEIGHTED_MESH")
    exact(attrs, "POSITION", "JOINTS_0", "WEIGHTS_0")
    require(primitive["mode"] == 4, "GLB_TRIANGLES")
    positions, _ = accessor(attrs["POSITION"], "VEC3", 5126, 3)
    require("min" in document["accessors"][attrs["POSITION"]], "GLB_POSITION_EXTREMA")
    faces, _ = accessor(primitive["indices"], "SCALAR", 5123, 1)
    joints, _ = accessor(attrs["JOINTS_0"], "VEC4", 5121, 4)
    weights, weight_start = accessor(attrs["WEIGHTS_0"], "VEC4", 5126, 4)
    indices = [row[0] for row in faces]
    triangles = [indices[start:start + 3] for start in range(0, len(indices), 3)]
    require(len(positions) == len(joints) == len(weights) and len(weights) <= 128
            and set(indices) == set(range(len(weights)))
            and all(len(set(triangle)) == 3 for triangle in triangles), "RIG_VERTEX_COVERAGE")
    repairs, skin_weights = [], []
    for index, (joint_row, weight_row) in enumerate(zip(joints, weights)):
        require(all(joint in (0, 1) for joint in joint_row), "RIG_UNKNOWN_JOINT")
        total = math.fsum(weight_row)
        require(total > 0, "RIG_UNWEIGHTED_MESH")
        active = [(joint, weight) for joint, weight in zip(joint_row, weight_row) if weight > 0]
        require(all(0 <= weight <= 1 for weight in weight_row)
                and len({joint for joint, _ in active}) == len(active), "RIG_INVALID_WEIGHTS")
        if abs(total - 1) > 1e-6:
            require(diagnose, "RIG_SKIN_NOT_NORMALIZED")
            repairs.append((index, weight_start + index * 16, tuple(weight / total for weight in weight_row)))
        skin_weights.append(sorted(active))

    world = [offsets[0], [first + second for first, second in zip(*offsets)]]
    inverse, _ = accessor(skin["inverseBindMatrices"], "MAT4", 5126, 16)
    require([list(row) for row in inverse] == [translation_matrix([-item for item in shift]) for shift in world],
            "RIG_INVERSE_BIND_MISMATCH")
    animation = document["animations"][0]
    exact(animation, "name", "samplers", "channels")
    require(isinstance(animation["name"], str) and 1 <= len(animation["name"]) <= 48
            and len(animation["samplers"]) == len(animation["channels"]) == 4, "RIG_MULTIJOINT_TRACKS")
    timeline, tracks, samplers = None, {}, set()
    for channel in animation["channels"]:
        exact(channel, "sampler", "target")
        target = channel["target"]
        exact(target, "node", "path")
        coordinate = (target["node"], target["path"])
        index = channel["sampler"]
        require(type(target["node"]) is int and coordinate in {(joint, path) for joint in (0, 1)
                for path in ("translation", "rotation")}
                and coordinate not in tracks and type(index) is int and 0 <= index < 4
                and index not in samplers, "RIG_MULTIJOINT_TRACKS")
        samplers.add(index)
        sampler = animation["samplers"][index]
        exact(sampler, "input", "output", "interpolation")
        require(sampler["interpolation"] == "LINEAR", "RIG_INTERPOLATION")
        rows, _ = accessor(sampler["input"], "SCALAR", 5126, 1)
        require("min" in document["accessors"][sampler["input"]], "RIG_TIME_EXTREMA")
        times = [row[0] for row in rows]
        require(3 <= len(times) <= 16 and times[0] == 0 and all(left < right for left, right
                in zip(times, times[1:])) and (timeline is None or timeline == times), "RIG_SAMPLE_CLOCK")
        timeline = times
        rotation = target["path"] == "rotation"
        values, _ = accessor(sampler["output"], "VEC4" if rotation else "VEC3", 5126, 4 if rotation else 3)
        require(len(values) == len(times) and (not rotation or all(abs(math.fsum(part * part for part
                in row) - 1) < 1e-6 for row in values)), "RIG_POSE_VALUES")
        tracks[coordinate] = values
    require(all(any(len(set(tracks[(joint, path)])) > 1 for path in ("translation", "rotation"))
                for joint in (0, 1)), "RIG_BOTH_JOINTS_MUST_MOVE")
    sorted_ranges = sorted(ranges.values())
    require(len(used) == len(document["accessors"]) and len(ranges) == len(document["bufferViews"])
            and all(left[1] <= right[0] for left, right in zip(sorted_ranges, sorted_ranges[1:])),
            "GLB_ACCESSOR_OVERLAP_OR_UNUSED")

    poses = []
    for sample, seconds in enumerate(timeline):
        locals_ = [{path: list(tracks[(joint, path)][sample]) for path in ("translation", "rotation")}
                   for joint in (0, 1)]
        root_pose, tip_pose = locals_
        deformed = []
        for position, influences in zip(positions, skin_weights):
            weighted = [0., 0., 0.]
            for joint, value in influences:
                point = [coord - rest for coord, rest in zip(position, world[joint])]
                if joint == 1:
                    point = transform(tip_pose["rotation"], tip_pose["translation"], point)
                point = transform(root_pose["rotation"], root_pose["translation"], point)
                weighted = [prior + value * coord for prior, coord in zip(weighted, point)]
            deformed.append([round(coord, 8) or 0 for coord in weighted])
        poses.append({"seconds": seconds, "local": locals_, "vertices": deformed})
    report = {"clip": animation["name"], "joints": [{"name": names[joint], "parent": None if joint == 0 else
              names[0], "rest_translation": offsets[joint], "inverse_bind": list(inverse[joint])}
              for joint in (0, 1)], "positions": [list(point) for point in positions], "triangles": triangles,
              "skin": [[[joint, weight] for joint, weight in row] for row in skin_weights], "poses": poses}
    return report, repairs
