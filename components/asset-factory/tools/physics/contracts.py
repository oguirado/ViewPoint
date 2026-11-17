"""Integer rigid-body input contracts. No geometry, mass or unit inference."""
from __future__ import annotations

import math
from pathlib import Path
import re

from asset_factory import core

MAX_INPUT_BYTES = 8 * 2**20  # [TARGET] bounded protocol and scene documents
MAX_STEPS = 10000  # [TARGET] includes the doubled-step run
MAX_BODIES = 64  # [TARGET] bounded qualification scenes
MAX_HULL_VERTICES = 512  # [TARGET] use decomposed hulls, never visual mesh soups
MAX_HULL_FACES = 1024  # [TARGET] caps quadratic convexity work
# Frozen SI reference for this analytic qualification scope, independent of
# caller geometry, candidate traces or a rehashed protocol. Other scales need
# a separately qualified policy, not a larger caller-controlled denominator.
NORMALIZATION_LENGTH_UM = 1000000  # [TARGET] one SI metre
MAX_TRAJECTORY_DISPLACEMENT_UM = 10000  # [TARGET] existing 10000 ppm at that reference
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


class Refusal(Exception):
    def __init__(self, rc, code):
        self.rc, self.code = rc, code
        super().__init__(code)


def require(test, rc=2, code="physics_input_invalid"):
    if not test:
        raise Refusal(rc, code)


def keys(obj, fields):
    require(type(obj) is dict and set(obj) == set(fields))


def integer(value, lower=None, upper=None):
    require(type(value) is int and (lower is None or value >= lower)
            and (upper is None or value <= upper))
    return value


def vector(value, n, lower=None):
    require(type(value) is list and len(value) == n)
    for entry in value:
        integer(entry, lower)


def digest(value):
    require(type(value) is str and DIGEST.fullmatch(value) is not None)


def name(value):
    require(type(value) is str and NAME.fullmatch(value) is not None)


def quaternion(value):
    vector(value, 4)
    require(abs(sum(x*x for x in value) - 10**12) <= 3 * 10**6,
            code="quaternion_not_unit")
    require(next((x for x in value if x), 0) > 0, code="quaternion_not_canonical")


def resolve_file(root, reference):
    keys(reference, ("path", "sha256", "media_type"))
    digest(reference["sha256"])
    require(type(reference["media_type"]) is str and reference["media_type"])
    require(type(reference["path"]) is str and reference["path"])
    rel = Path(reference["path"])
    require(not rel.is_absolute() and ".." not in rel.parts, code="path_escape")
    root = Path(root).resolve(strict=True)
    path = (root / rel).resolve()
    require(path.is_relative_to(root), code="path_escape")
    require(path.is_file(), 3, "source_unavailable")
    require(core.sha256_file(path) == reference["sha256"], 4, "source_stale")
    return path


def _det(a, b, c, d, e, f):
    return a*b*c + 2*d*e*f - a*f*f - b*e*e - c*d*d


def inertia(record, source_sha256):
    keys(record, ("mass_ug", "com_um", "tensor_ug_um2", "provenance"))
    integer(record["mass_ug"], 1)
    vector(record["com_um"], 3)
    vector(record["tensor_ug_um2"], 6)
    p = record["provenance"]
    keys(p, ("method", "source_sha256"))
    require(p["method"] in ("cad", "analytic_fixture", "measured"), 3, "inertia_unproven")
    require(p["source_sha256"] == source_sha256, 4, "inertia_source_stale")
    a, b, c, d, e, f = record["tensor_ug_um2"]
    require(a > 0 and a*b-d*d > 0 and _det(a, b, c, d, e, f) > 0,
            code="inertia_not_positive_definite")
    # The second moment matrix tr(I)/2 * Id - I must be positive semidefinite.
    x, y, z = b+c-a, a+c-b, a+b-c
    require(min(x, y, z) >= 0 and x*y >= 4*d*d and x*z >= 4*e*e
            and y*z >= 4*f*f and _det(x, y, z, -2*d, -2*e, -2*f) >= 0,
            code="inertia_triangle_inequality")


def convex_hull(vertices, faces):
    require(type(vertices) is list and 4 <= len(vertices) <= MAX_HULL_VERTICES)
    require(type(faces) is list and 4 <= len(faces) <= MAX_HULL_FACES)
    for v in vertices:
        vector(v, 3)
    require(len({tuple(v) for v in vertices}) == len(vertices), code="duplicate_hull_vertex")
    edges, volume6, seen = {}, 0, set()
    for face in faces:
        vector(face, 3, 0)
        require(len(set(face)) == 3 and max(face) < len(vertices))
        require(tuple(sorted(face)) not in seen, code="duplicate_hull_face")
        seen.add(tuple(sorted(face)))
        a, b, c = (vertices[i] for i in face)
        u, v = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
        n = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]]
        require(any(n), code="degenerate_hull_face")
        distances = [sum(n[i]*(p[i]-a[i]) for i in range(3)) for p in vertices]
        require(max(distances) <= 0 and min(distances) < 0, code="hull_not_convex_or_oriented")
        volume6 += sum(a[i]*n[i] for i in range(3))
        for i, j in zip(face, face[1:] + face[:1]):
            edges[(i, j)] = edges.get((i, j), 0) + 1
    require(all(n == 1 and edges.get((j, i)) == 1 for (i, j), n in edges.items()),
            code="hull_not_closed")
    require(set(i for face in faces for i in face) == set(range(len(vertices))) and volume6 > 0,
            code="hull_invalid_volume")


def validate_scene(scene):
    core.canonical_json(scene)
    keys(scene, ("schema", "asset_id", "units", "source", "bodies"))
    require(scene["schema"] == "af.physics-scene/v1")
    name(scene["asset_id"])
    require(scene["units"] == {"length": "um", "mass": "ug", "inertia": "ug_um2"},
            code="units_unsupported")
    core.validate(scene, core.load_json(Path(__file__).with_name("scene.schema.json"), max_bytes=65536))
    keys(scene["source"], ("path", "sha256", "media_type"))
    digest(scene["source"]["sha256"])
    bodies = scene["bodies"]
    require(type(bodies) is list and 1 <= len(bodies) <= MAX_BODIES)
    ids, geom_ids, dynamics = set(), set(), 0
    for body in bodies:
        keys(body, ("id", "dynamic", "position_um", "quaternion_ppm", "velocity_um_s",
                    "angular_velocity_urad_s", "inertial", "colliders"))
        name(body["id"])
        require(body["id"] not in ids, code="duplicate_body")
        ids.add(body["id"])
        require(type(body["dynamic"]) is bool)
        vector(body["position_um"], 3)
        quaternion(body["quaternion_ppm"])
        vector(body["velocity_um_s"], 3)
        vector(body["angular_velocity_urad_s"], 3)
        if body["dynamic"]:
            dynamics += 1
            inertia(body["inertial"], scene["source"]["sha256"])
        else:
            require(body["inertial"] is None and body["velocity_um_s"] == [0, 0, 0]
                    and body["angular_velocity_urad_s"] == [0, 0, 0])
        require(type(body["colliders"]) is list and 1 <= len(body["colliders"]) <= 32)
        for geom in body["colliders"]:
            keys(geom, ("id", "shape", "position_um", "quaternion_ppm", "source_sha256", "data"))
            name(geom["id"])
            require(geom["id"] not in geom_ids, code="duplicate_collider")
            geom_ids.add(geom["id"])
            vector(geom["position_um"], 3)
            quaternion(geom["quaternion_ppm"])
            require(geom["source_sha256"] == scene["source"]["sha256"], 4, "collider_source_stale")
            shape, data = geom["shape"], geom["data"]
            if shape == "box":
                keys(data, ("half_extents_um",))
                vector(data["half_extents_um"], 3, 1)
            elif shape == "sphere":
                keys(data, ("radius_um",))
                integer(data["radius_um"], 1)
            elif shape == "cylinder":
                keys(data, ("radius_um", "half_height_um"))
                integer(data["radius_um"], 1)
                integer(data["half_height_um"], 1)
            elif shape == "plane":
                require(not body["dynamic"], code="dynamic_plane")
                keys(data, ("half_extents_um",))
                vector(data["half_extents_um"], 2, 1)
            elif shape == "convex_hull":
                keys(data, ("vertices_um", "faces"))
                convex_hull(data["vertices_um"], data["faces"])
            else:
                raise Refusal(3, "collider_unsupported")
    require(dynamics > 0, 3, "no_dynamic_body")


def validate_protocol(p):
    core.canonical_json(p)
    core.validate(p, core.load_json(Path(__file__).with_name("protocol.schema.json"), max_bytes=65536))
    keys(p, ("schema", "gravity_um_s2", "expected_gravity_um_s2", "timestep_us", "steps",
             "timeout_s", "normalization_length_um", "max_trajectory_error_ppm",
             "inertia_tolerance_ppm", "solver_iterations", "solver_tolerance_ppb",
             "contact", "free_fall"))
    require(p["schema"] == "af.physics-protocol/v1")
    vector(p["gravity_um_s2"], 3)
    vector(p["expected_gravity_um_s2"], 3)
    integer(p["timestep_us"], 2, 100000)
    require(p["timestep_us"] % 2 == 0, code="timestep_not_halvable")
    integer(p["steps"], 1, MAX_STEPS // 2)
    integer(p["timeout_s"], 1, 300)
    integer(p["normalization_length_um"], 1)
    require(p["normalization_length_um"] == NORMALIZATION_LENGTH_UM,
            code="normalization_scale_invalid")
    integer(p["max_trajectory_error_ppm"], 0, 10000)  # [TARGET] G12 frozen upper bound
    integer(p["inertia_tolerance_ppm"], 0, 1000)  # [TARGET] strategy 3a; uncalibrated
    integer(p["solver_iterations"], 1, 1000)
    integer(p["solver_tolerance_ppb"], 1, 1000000)
    c = p["contact"]
    keys(c, ("max_penetration_um", "initial_penetration_um", "max_contacts", "require_contact",
             "friction_ppm_um", "timeconst_us", "dampratio_ppm"))
    integer(c["max_penetration_um"], 0)
    integer(c["initial_penetration_um"], 0, c["max_penetration_um"])
    integer(c["max_contacts"], 1, 4096)
    require(c["max_contacts"] * p["steps"] <= 100000, 6, "trace_budget_exceeded")
    require(type(c["require_contact"]) is bool)
    vector(c["friction_ppm_um"], 3, 0)
    integer(c["timeconst_us"], 1)
    integer(c["dampratio_ppm"], 1)
    f = p["free_fall"]
    require(f is not None, 3, "analytic_oracle_unavailable")
    keys(f, ("body_id", "steps", "tolerance_um"))
    name(f["body_id"])
    integer(f["steps"], 1, p["steps"])
    integer(f["tolerance_um"], 0)


def load_inputs(scene_path, protocol_path, root):
    scene = core.load_json(scene_path, max_bytes=MAX_INPUT_BYTES)
    protocol = core.load_json(protocol_path, max_bytes=MAX_INPUT_BYTES)
    validate_scene(scene)
    validate_protocol(protocol)
    resolve_file(root, scene["source"])
    require(protocol["gravity_um_s2"] == protocol["expected_gravity_um_s2"], 5, "wrong_gravity")
    return scene, protocol
