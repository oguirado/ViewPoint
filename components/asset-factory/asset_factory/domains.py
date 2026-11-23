"""Shared camera, pose, time and texture domains for INPUT and downstream gates.

The factory world frame is right-handed, Z-up and front-negative-Y, with the
subject's left at +X. Pinhole extrinsics map world micrometres into camera axes
+X right, +Y down and +Z forward. Intrinsics are in pixels and projection returns
milli-pixels. Frame coordinates count from the top-left pixel edge. Pixel
(column, row) has centre (column + 1/2, row + 1/2).

The exact-matrix camera remains supported. A camera may instead carry a nonzero
rational (x, y, z, w) quaternion, whose squared norm is divided out exactly.
Quantizing these parameters gives a rigid rotation, but calibration must bound
its error against independent observations.

A half-open crop maps source edges to destination edges as
(x, y) -> (sx*x + tx, sy*y + ty). Wire order is [sx, sy, tx, ty] in rational
pairs. The shared helpers allow different axis scales. compose_crop(a, b)
means apply a, then b. Crops and inverses describe coordinates, not the
resampling filter or which source pixels survive it.
"""
from fractions import Fraction

FRAME = "right-handed-z-up-front-negative-y"
REQUIRED_VIEWS = ("front", "left", "right", "back", "face", "hands")
UM_PER_METER = 1_000_000
MILLIPX_PER_PIXEL = 1_000
PPM = 1_000_000
TEXTURE_ORIGIN = "top-left"
COLOR_SPACES = ("srgb", "linear")
TIME_SCHEMA = {"type": "array", "minItems": 2, "maxItems": 2,
               "prefixItems": [{"type": "integer"}, {"type": "integer", "minimum": 1}]}
DIGEST_SCHEMA = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}


def _array(item, size):
    return {"type": "array", "items": item, "minItems": size, "maxItems": size}


def _record(**fields):
    return {"type": "object", "properties": fields, "required": list(fields),
            "additionalProperties": False}


CAMERA_SCHEMA = _record(rotation=_array(_array(TIME_SCHEMA, 3), 3),
                        translation_um=_array(TIME_SCHEMA, 3), intrinsics=_array(TIME_SCHEMA, 4))
QUATERNION_CAMERA_SCHEMA = _record(rotation_quaternion_xyzw=_array(TIME_SCHEMA, 4),
                                   translation_um=_array(TIME_SCHEMA, 3),
                                   intrinsics=_array(TIME_SCHEMA, 4))
RATIONAL_CAMERA_SCHEMA = {"oneOf": [CAMERA_SCHEMA, QUATERNION_CAMERA_SCHEMA]}
CROP_SCHEMA = _record(rect=_array({"type": "integer", "minimum": 0}, 4),
                      size=_array({"type": "integer", "minimum": 1}, 2))
POSE_SCHEMA = _record(frame={"const": FRAME}, pose_digest=DIGEST_SCHEMA)
TEXTURE_SCHEMA = _record(size=CROP_SCHEMA["properties"]["size"], origin={"const": TEXTURE_ORIGIN},
                         color_space={"enum": list(COLOR_SPACES)}, camera_digest=DIGEST_SCHEMA,
                         pose_digest=DIGEST_SCHEMA, timestamp=TIME_SCHEMA)


def rational(value):
    if (type(value) not in (list, tuple) or len(value) != 2
            or any(type(v) is not int for v in value) or value[1] <= 0):
        raise ValueError("invalid_rational")
    return Fraction(*value)


def pair(value):
    value = Fraction(value)
    return [value.numerator, value.denominator]


def integers(value, length):
    if type(value) is not list or len(value) != length or any(type(v) is not int for v in value):
        raise ValueError("invalid_integer_vector")
    return value


def rectangle(value, raster):
    x0, y0, x1, y1 = integers(value, 4)
    if not (0 <= x0 < x1 <= raster[0] and 0 <= y0 < y1 <= raster[1]):
        raise ValueError("invalid_rectangle")
    return x0, y0, x1, y1


def contains(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def crop_transform(value, raster):
    if type(value) is not dict or value.keys() != CROP_SCHEMA["properties"].keys():
        raise ValueError("invalid_crop")
    x0, y0, x1, y1 = rectangle(value["rect"], raster)
    w, h = integers(value["size"], 2)
    if min(w, h) <= 0:
        raise ValueError("invalid_crop_size")
    sx, sy = Fraction(w, x1 - x0), Fraction(h, y1 - y0)
    return [pair(sx), pair(sy), pair(-x0 * sx), pair(-y0 * sy)]


def _rational_vector(values, length):
    if type(values) is not list or len(values) != length:
        raise ValueError("invalid_rational_vector")
    return tuple(map(rational, values))


def compose_crop(first, second):
    """Apply first and then second, returning a canonical rational affine map."""
    ax, ay, atx, aty = _rational_vector(first, 4)
    bx, by, btx, bty = _rational_vector(second, 4)
    if min(ax, ay, bx, by) <= 0:
        raise ValueError("invalid_crop_scale")
    return [pair(v) for v in (bx * ax, by * ay, bx * atx + btx, by * aty + bty)]


def invert_crop(transform):
    """Invert a crop's coordinate map on the retained destination domain."""
    sx, sy, tx, ty = _rational_vector(transform, 4)
    if min(sx, sy) <= 0:
        raise ValueError("invalid_crop_scale")
    return [pair(v) for v in (1 / sx, 1 / sy, -tx / sx, -ty / sy)]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _camera(value):
    if type(value) is not dict:
        raise ValueError("invalid_camera")
    if value.keys() == QUATERNION_CAMERA_SCHEMA["properties"].keys():
        x, y, z, w = quaternion = _rational_vector(value["rotation_quaternion_xyzw"], 4)
        norm = _dot(quaternion, quaternion)
        if not norm:
            raise ValueError("invalid_quaternion")
        k = 2 / norm
        rotation = (
            (1 - k*(y*y + z*z), k*(x*y - z*w), k*(x*z + y*w)),
            (k*(x*y + z*w), 1 - k*(x*x + z*z), k*(y*z - x*w)),
            (k*(x*z - y*w), k*(y*z + x*w), 1 - k*(x*x + y*y)),
        )
    elif value.keys() == CAMERA_SCHEMA["properties"].keys():
        rows = value["rotation"]
        if type(rows) is not list or len(rows) != 3:
            raise ValueError("invalid_rotation")
        u, v, w = rotation = tuple(_rational_vector(row, 3) for row in rows)
        cross = tuple(u[i - 2] * v[i - 1] - u[i - 1] * v[i - 2] for i in range(3))
        if any(_dot(a, b) != int(i == j) for i, a in enumerate(rotation)
               for j, b in enumerate(rotation)) or cross != w:
            raise ValueError("non_rigid_camera")
    else:
        raise ValueError("invalid_camera")
    translation = _rational_vector(value["translation_um"], 3)
    intrinsics = _rational_vector(value["intrinsics"], 4)
    if min(intrinsics[:2]) <= 0:
        raise ValueError("invalid_focal_length")
    return rotation, translation, intrinsics


def camera_center(value):
    """Return the exact world-space camera centre in micrometres."""
    rotation, translation, _ = _camera(value)
    return tuple(-_dot(column, translation) for column in zip(*rotation))


def pinhole(value):
    rotation, translation, (fx, fy, cx, cy) = _camera(value)

    def project(world_um):
        x, y, z = (_dot(row, integers(world_um, 3)) + t for row, t in zip(rotation, translation))
        if z <= 0:
            raise ValueError("landmark_behind_camera")
        return (MILLIPX_PER_PIXEL * (fx * x / z + cx),
                MILLIPX_PER_PIXEL * (fy * y / z + cy))

    return project
