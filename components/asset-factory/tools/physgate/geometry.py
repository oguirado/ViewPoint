"""Exact solid and uniform-density properties for an exported box fixture."""
from fractions import Fraction
from itertools import product
from math import ceil, isqrt, prod

from asset_factory.domains import PPM, UM_PER_METER, pair
from asset_factory.math_acceptance import outward_bounds
from tools.blender.assembly.geometry import AssemblyError, decode_mesh
from tools.physics.contracts import Refusal, convex_hull, keys, require, vector

MAX_GLTF_BYTES = 64 * 1024  # [TARGET] Fixture input ceiling.
MAX_COORDINATE_UM = 10 * UM_PER_METER  # [TARGET] Fixture coordinate ceiling.
MAX_OUTWARD_UM = 1000  # [TARGET] Uncalibrated outward-distance allowance.
MAX_EXTRA_VOLUME_PPM = 10_000  # [TARGET] Uncalibrated source-relative allowance.


def render_box(data):
    require(type(data) is bytes and data, 3, "render_mesh_unavailable")
    require(len(data) <= MAX_GLTF_BYTES, 6, "render_mesh_byte_budget")
    try:
        vertices, faces, _ = decode_mesh(data, tolerance_nm=0)
    except AssemblyError as exc:
        if str(exc) == "part_coordinate_residual":
            raise Refusal(3, "render_exact_lattice_unproven") from exc
        raise Refusal(exc.rc, str(exc)) from exc
    require(all(abs(x) <= MAX_COORDINATE_UM for point in vertices for x in point),
            6, "fixture_coordinate_budget")

    points = sorted(set(vertices))
    axes = tuple(tuple(sorted(set(values))) for values in zip(*points))
    require(len(axes) == 3 and all(len(axis) == 2 for axis in axes)
            and set(points) == set(product(*axes)), 3, "render_box_scope_unproven")
    require(len(faces) == 12, 5, "render_box_not_closed")
    for axis, bounds in enumerate(axes):
        for bound in bounds:
            face = {point for point in points if point[axis] == bound}
            triangles = [set(vertices[i] for i in triangle) for triangle in faces
                         if all(vertices[i] in face for i in triangle)]
            require(len(triangles) == 2 and len(triangles[0]) == len(triangles[1]) == 3
                    and triangles[0] | triangles[1] == face, 5, "render_box_not_closed")
            first, second = sorted(triangles[0] & triangles[1])
            require(all(first[i] != second[i] for i in range(3) if i != axis),
                    5, "render_box_face_overlap")
    index = {point: i for i, point in enumerate(points)}
    convex_hull([list(point) for point in points],
                [[index[vertices[i]] for i in face] for face in faces])
    return axes


def collider_box(collider):
    keys(collider, ("center_um", "half_extents_um"))
    center, half = collider["center_um"], collider["half_extents_um"]
    vector(center, 3)
    vector(half, 3)
    require(min(half) > 0, 5, "collider_inverted")
    require(all(abs(c) + h <= MAX_COORDINATE_UM for c, h in zip(center, half)),
            6, "fixture_coordinate_budget")
    return tuple((c - h, c + h) for c, h in zip(center, half))


def _volume(box):
    return prod(high - low for low, high in box)


def _distance_upper(a, b):
    # Distance to a box is separable by axis. Its maximum over another
    # box is attained by selecting the farther endpoint on each axis.
    squared = sum(max(0, c - lo, hi - d)**2 for (lo, hi), (c, d) in zip(a, b))
    root = isqrt(squared)
    return root + (root * root < squared)


def collision_metrics(render, proxy, height_um, plane_um):
    source_volume, proxy_volume = _volume(render), _volume(proxy)
    intersection = prod(max(0, min(hi, d) - max(lo, c))
                        for (lo, hi), (c, d) in zip(render, proxy))
    return {
        "source_volume_um3": source_volume,
        "lost_volume_upper_ppm": ceil(Fraction(PPM * (source_volume - intersection), source_volume)),
        "extra_volume_upper_ppm": ceil(Fraction(PPM * (proxy_volume - intersection), source_volume)),
        "collision_inward_upper_um": _distance_upper(render, proxy),
        "collision_outward_upper_um": _distance_upper(proxy, render),
        "initial_clearance_lower_um": height_um + min(render[2][0], proxy[2][0]) - plane_um,
        "hull_count": 1,
        "hull_vertices": 8,
        "hull_triangles": 12,
    }


def mass_properties(box, density, source_sha256):
    length_m = [Fraction(hi - lo, UM_PER_METER) for lo, hi in box]
    volume_m3 = prod(length_m)
    mass_kg = density * volume_m3
    com_um = [Fraction(lo + hi, 2) for lo, hi in box]
    diagonal = [mass_kg * sum(length**2 for j, length in enumerate(length_m) if j != i) / 12
                for i in range(3)]
    tensor = diagonal + [Fraction(0)] * 3
    # Both records describe the same COM-frame tensor. The kit uses its
    # legacy nearest integer wire representation, audited on compiled replay.
    metrics = {
        "density_kg_m3": density,
        "volume_um3": _volume(box),
        "mass_ng": list(outward_bounds(pair(mass_kg), pair(mass_kg), 10**12)),
        "com_um": [list(outward_bounds(pair(value), pair(value))) for value in com_um],
        "inertia_e18": [list(outward_bounds(pair(value), pair(value), 10**18))
                        for value in tensor],
    }
    inertial = {"mass_ug": round(mass_kg * 10**9), "com_um": [round(x) for x in com_um],
                "tensor_ug_um2": [round(value * 10**21) for value in tensor],
                "provenance": {"method": "analytic_fixture", "source_sha256": source_sha256}}
    return metrics, inertial
