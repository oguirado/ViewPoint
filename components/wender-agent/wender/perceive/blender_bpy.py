# @module {"id":"wa.perceive.blender_bpy","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.blender","wa.state.blender"],"book":"HB-WA-s1-perceive"}
"""Read-only Blender-side exporter using injected scene and dependency graph.

No bpy import occurs in the host. Call inside Blender with ``bpy.context.scene``
and ``bpy.context.evaluated_depsgraph_get()``. Native evaluated coordinates are
hashed as finite big-endian doubles; summaries round to nearest integer with
ties to even using exact binary ratios. Basis uses ppm, world bounds/translation
use caller-declared micrometres per Blender unit. These are quantized summaries,
not exact geometry. Materials, pixels and appearance evidence are AUSENTE.

Only OBJECT mode and non-instanced evaluated meshes are supported. Edit/sculpt
mode, fractional frames and generated instances refuse instead of silently
exporting stale or incomplete geometry. No writes, frame capture or model calls.
Budgets are explicit positive integers supplied by the caller [TARGET policy].
"""
from fractions import Fraction
import hashlib
import math
import struct
import time

from .blender import build_blender_state
from wender.state.blender import BlenderStateError

LIMIT_KEYS = frozenset(("max_objects", "max_vertices", "max_edges", "max_faces", "max_bytes", "timeout_ms"))


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BlenderStateError("nonfinite or invalid native Blender number")
    return value


def _quantize(value, scale):
    return round(Fraction(_finite(value)) * scale)


def _check(deadline):
    if time.monotonic_ns() >= deadline:
        raise BlenderStateError("Blender observation deadline exceeded", rc=6)


def _mesh_summary(mesh, matrix, scale, remaining, deadline):
    counts = {"vertices": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons)}
    for field, value in counts.items():
        remaining[field] -= value
        if remaining[field] < 0:
            raise BlenderStateError("Blender " + field + " budget exceeded", rc=6)
    digest = hashlib.sha256(b"wender.blender.mesh/v1\x00")
    low, high = None, None
    for i, vertex in enumerate(mesh.vertices):
        if i % 1024 == 0:
            _check(deadline)
        local = [_finite(v) for v in vertex.co]
        digest.update(b"v" + struct.pack("!3d", *local))
        world = matrix @ vertex.co
        point = [_quantize(v, scale) for v in world]
        low = point if low is None else [min(a, b) for a, b in zip(low, point)]
        high = point if high is None else [max(a, b) for a, b in zip(high, point)]
    edge_uses = {}
    for i, edge in enumerate(mesh.edges):
        if i % 1024 == 0:
            _check(deadline)
        key = tuple(sorted(edge.vertices))
        edge_uses[key] = 0
        digest.update(b"e" + struct.pack("!2Q", *edge.vertices))
    for i, face in enumerate(mesh.polygons):
        if i % 1024 == 0:
            _check(deadline)
        indices = list(face.vertices)
        digest.update(b"f" + struct.pack("!Q", len(indices)))
        for index, a in enumerate(indices):
            b = indices[(index + 1) % len(indices)]
            key = tuple(sorted((a, b)))
            if key not in edge_uses:
                raise BlenderStateError("evaluated polygon references absent edge")
            edge_uses[key] += 1
            digest.update(struct.pack("!Q", a))
    return {**counts, "boundary_edges": sum(v == 1 for v in edge_uses.values()),
            "nonmanifold_edges": sum(v != 2 for v in edge_uses.values()),
            "bounds_um": None if low is None else low + high, "geometry_sha256": digest.hexdigest()}


# @anchor:wa.perceive.blender_bpy.capture_scene
def capture_scene(scene, depsgraph, *, blender_version, micrometers_per_unit, limits):
    """Export a bounded evaluated scene without changing it; publish nothing.

    Caller must update its view layer before obtaining the graph. Geometry and
    transforms come from that graph. Limits cap total evaluated counts, bytes,
    and wall time; Blender evaluation itself also needs an outer timeout/scope.
    Scene units are required to be declared by the caller, including unitless
    scenes. No conversion or default units are inferred from scene settings.
    """
    if not isinstance(limits, dict) or set(limits) != LIMIT_KEYS or any(
            type(value) is not int or value <= 0 for value in limits.values()):
        raise BlenderStateError("explicit positive Blender limits required")
    if type(micrometers_per_unit) is not int or micrometers_per_unit <= 0:
        raise BlenderStateError("explicit positive micrometers_per_unit required")
    if not isinstance(blender_version, str) or not blender_version:
        raise BlenderStateError("explicit Blender version required")
    deadline = time.monotonic_ns() + limits["timeout_ms"] * 1000000
    if scene.frame_subframe != 0:
        raise BlenderStateError("fractional Blender frame unsupported", rc=3)
    if len(scene.objects) > limits["max_objects"]:
        raise BlenderStateError("Blender object budget exceeded", rc=6)
    for instance in depsgraph.object_instances:
        _check(deadline)
        if instance.is_instance:
            raise BlenderStateError("generated instances need an explicit instance adapter", rc=3)
    if depsgraph.scene.original != scene:
        raise BlenderStateError("dependency graph belongs to a different scene", rc=4)
    evaluated_names = {obj.name_full for obj in depsgraph.objects}
    if any(obj.name_full not in evaluated_names for obj in scene.objects):
        raise BlenderStateError("scene contains objects excluded from the dependency graph", rc=3)
    remaining = {key: limits["max_" + key] for key in ("vertices", "edges", "faces")}
    objects = []
    for obj in sorted(scene.objects, key=lambda item: item.name_full):
        _check(deadline)
        if obj.mode != "OBJECT":
            raise BlenderStateError("only reconciled OBJECT mode is supported", rc=3)
        evaluated = obj.evaluated_get(depsgraph)
        matrix = evaluated.matrix_world
        matrix_values = [_finite(v) for row in matrix for v in row]
        entry = {
            "name": obj.name_full, "type": obj.type, "selected": obj.select_get(),
            "hidden": bool(obj.hide_viewport or obj.hide_get()),
            "basis_ppm": [_quantize(matrix[i][j], 1000000) for i in range(3) for j in range(3)],
            "translation_um": [_quantize(matrix[i][3], micrometers_per_unit) for i in range(3)],
            "transform_sha256": hashlib.sha256(struct.pack("!16d", *matrix_values)).hexdigest(),
            "mesh": None,
        }
        if obj.type == "MESH":
            mesh = evaluated.to_mesh()
            try:
                if mesh is None:
                    raise BlenderStateError("evaluated mesh unavailable", rc=3)
                entry["mesh"] = _mesh_summary(mesh, matrix, micrometers_per_unit, remaining, deadline)
            finally:
                evaluated.to_mesh_clear()
        objects.append(entry)
    active = depsgraph.view_layer.objects.active
    snapshot = {
        "schema": "wender.blender-scene/v1", "scene_name": scene.name,
        "blender_version": blender_version, "micrometers_per_unit": micrometers_per_unit,
        "frame": scene.frame_current, "active_object": None if active is None else active.name_full,
        "objects": objects,
    }
    result = build_blender_state(snapshot, max_bytes=limits["max_bytes"])
    _check(deadline)
    return result
