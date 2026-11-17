# @module {"id":"wa.state.blender","layer":0,"owner":"STATE","deps":["wa.state.schema"],"book":"HB-WA-a0-arch"}
"""Closed integer Blender scene sidecar; no bpy or vision-engine dependency.

ORIGIN: additive source-kind and sidecar pattern from wender/perceive/godot.py.
The base STATE retains prune_ax_v1 hashes. Geometry lives in this separate block.
Units, version and budgets are caller declarations, never inferred defaults.
"""
import re

from .schema import StateError, dumps

SCENE_SCHEMA = "wender.blender-scene/v1"
SCENE_KEYS = frozenset(("schema", "scene_name", "blender_version", "micrometers_per_unit",
                        "frame", "active_object", "objects"))
OBJECT_KEYS = frozenset(("name", "type", "selected", "hidden", "basis_ppm", "translation_um",
                         "transform_sha256", "mesh"))
MESH_KEYS = frozenset(("vertices", "edges", "faces", "boundary_edges", "nonmanifold_edges",
                       "bounds_um", "geometry_sha256"))
_SHA = re.compile(r"[0-9a-f]{64}\Z")


# @anchor:wa.state.blender.BlenderStateError
class BlenderStateError(StateError):
    """Refusal with an asset-factory-compatible stage code."""

    def __init__(self, message, *, rc=2):
        super().__init__(message)
        self.rc = rc


def _closed(obj, keys, path):
    if not isinstance(obj, dict) or set(obj) != keys:
        raise BlenderStateError("invalid keys at " + path)


def _int(value, path, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise BlenderStateError("invalid integer at " + path)


def _text(value, path):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BlenderStateError("invalid text at " + path)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise BlenderStateError("invalid UTF-8 at " + path) from exc


def _sha(value, path):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise BlenderStateError("invalid sha256 at " + path)


def _vector(value, length, path):
    if not isinstance(value, list) or len(value) != length:
        raise BlenderStateError("invalid vector at " + path)
    for item in value:
        _int(item, path)


# @anchor:wa.state.blender.bounded_bytes
def bounded_bytes(value, max_bytes):
    """Serialize canonical UTF-8, refusing floats and an explicit byte ceiling."""
    _int(max_bytes, "max_bytes", 1)
    try:
        data = dumps(value).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise BlenderStateError(str(exc)) from exc
    if len(data) > max_bytes:
        raise BlenderStateError("state byte budget exceeded", rc=6)
    return data


# @anchor:wa.state.blender.validate_scene
def validate_scene(scene):
    """Raise on malformed, unknown, float-bearing or inconsistent scene fields."""
    _closed(scene, SCENE_KEYS, "scene")
    # Recursive float refusal precedes any semantic comparison or coercion.
    try:
        dumps(scene)
    except (ValueError, TypeError) as exc:
        raise BlenderStateError(str(exc)) from exc
    if scene["schema"] != SCENE_SCHEMA:
        raise BlenderStateError("unsupported Blender scene schema")
    for field in ("scene_name", "blender_version"):
        _text(scene[field], field)
    _int(scene["micrometers_per_unit"], "micrometers_per_unit", 1)
    _int(scene["frame"], "frame")
    if not isinstance(scene["objects"], list):
        raise BlenderStateError("objects must be an array")
    names = set()
    for obj in scene["objects"]:
        _closed(obj, OBJECT_KEYS, "object")
        _text(obj["name"], "object.name")
        _text(obj["type"], "object.type")
        if obj["name"] in names:
            raise BlenderStateError("duplicate object identity")
        names.add(obj["name"])
        if type(obj["selected"]) is not bool or type(obj["hidden"]) is not bool:
            raise BlenderStateError("selected/hidden must be booleans")
        _vector(obj["basis_ppm"], 9, "basis_ppm")
        _vector(obj["translation_um"], 3, "translation_um")
        _sha(obj["transform_sha256"], "transform_sha256")
        mesh = obj["mesh"]
        if obj["type"] != "MESH":
            if mesh is not None:
                raise BlenderStateError("non-mesh must have null mesh evidence")
            continue
        _closed(mesh, MESH_KEYS, "mesh")
        for field in ("vertices", "edges", "faces", "boundary_edges", "nonmanifold_edges"):
            _int(mesh[field], "mesh." + field, 0)
        if mesh["boundary_edges"] > mesh["edges"] or mesh["nonmanifold_edges"] > mesh["edges"]:
            raise BlenderStateError("edge counts inconsistent")
        _sha(mesh["geometry_sha256"], "geometry_sha256")
        if mesh["vertices"] == 0:
            if mesh["bounds_um"] is not None:
                raise BlenderStateError("empty mesh bounds must be null")
        else:
            _vector(mesh["bounds_um"], 6, "bounds_um")
            if any(mesh["bounds_um"][i] > mesh["bounds_um"][i + 3] for i in range(3)):
                raise BlenderStateError("inverted mesh bounds")
    active = scene["active_object"]
    if active is not None and (not isinstance(active, str) or active not in names):
        raise BlenderStateError("active object absent from scene")
