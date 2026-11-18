"""Fit the three principal dimensions of a procedural, registered head in µm."""
from fractions import Fraction
import re

from asset_factory import domains
from tools.input.evidence import checked_record, closed, require
from viewpoint.handlers import local_capabilities
from .blender_fixture import decoded_rgb


def camera_projection(camera):
    """Exact world-to-camera rotation, world-space centre in µm and pixel intrinsics."""
    rotation, translation, intrinsics = domains._camera(camera)
    centre = tuple(-sum(a*b for a, b in zip(column, translation)) for column in zip(*rotation))
    return rotation, centre, intrinsics


def registered(views):
    require(type(views) is dict, 2, "invalid_face_views")
    for name in ("front", "profile"):
        require(name in views, 3, "missing_required_" + name)
    require(set(views) == {"front", "profile"}, 2, "unexpected_face_view")
    for name, view in views.items():
        x, y, z = domains.camera_center(view["camera"])
        # Fixture capture envelope in INPUT's front-negative-Y frame.
        depth, lateral = (-y, x) if name == "front" else (x, y)
        require(depth > 2 * max(abs(lateral), abs(z)), 5, "camera_view_mismatch")
        require(view["landmarks"], 3, "missing_landmarks")
        require(len(view["landmarks"]) <= 256, 6, "landmark_budget")


def samples(points, view):
    seen = set()
    for item in view["landmarks"]:
        idx = item["vertex"]
        require(type(idx) is int and 0 <= idx < len(points) and idx not in seen,
                2, "invalid_landmark_index")
        seen.add(idx)
        xy = item["xy_millipx"]
        require(type(xy) is list and len(xy) == 2, 2, "invalid_landmark_observation")
        yield idx, domains.integers(points[idx], 3), tuple(map(domains.rational, xy))


def fit_axes(points, views):
    """Cross-multiply the two pinhole equations and solve their normal equations."""
    registered(views)
    matrix = [[Fraction(0) for _ in range(4)] for _ in range(3)]
    for view in views.values():
        cam = view["camera"]
        rows, translation, (fx, fy, cx, cy) = domains._camera(cam)
        for _, point, xy in samples(points, view):
            for axis, (focal, centre) in enumerate(((fx, cx), (fy, cy))):
                ray = (xy[axis] / domains.MILLIPX_PER_PIXEL - centre) / focal
                coeff = [(a - ray*b)*v for a, b, v in zip(rows[axis], rows[2], point)]
                rhs = ray*translation[2] - translation[axis]
                for i in range(3):
                    for j, value in enumerate([*coeff, rhs]):
                        matrix[i][j] += coeff[i] * value

    for i in range(3):
        pivot = next((j for j in range(i, 3) if matrix[j][i]), None)
        require(pivot is not None, 3, "insufficient_fit_rank")
        matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
        matrix[i] = [v / matrix[i][i] for v in matrix[i]]
        for j in range(3):
            if i != j:
                matrix[j] = [a - matrix[j][i]*b for a, b in zip(matrix[j], matrix[i])]
    scales = tuple(row[-1] for row in matrix)
    require(all(Fraction(1, 2) <= s <= 2 for s in scales), 5, "outside_fixture_fit_domain")
    return scales


def project_errors(points, views, used_vertices):
    """Worst L-infinity error in milli-pixels per registered held-out view."""
    registered(views)
    errors = {}
    for name, view in views.items():
        project = domains.pinhole(view["camera"])
        largest = Fraction(0)
        for idx, point, observed in samples(points, view):
            require(idx not in used_vertices, 5, "landmark_not_held_out")
            predicted = project(point)
            largest = max(largest, *(abs(a-b) for a, b in zip(predicted, observed)))
        errors[name] = largest.__ceil__()
    return errors


def read_inputs(args, reader):
    cap = next(cap for cap in local_capabilities() if cap["name"] == "af.face_fit")
    spec = cap["args_schema"]
    require(type(args) is dict and args.keys() == set(spec["required"]), 2, "face_capability_args")
    for key, value in args.items():
        require(type(value) is str and re.fullmatch(spec["properties"][key]["pattern"], value),
                2, "face_capability_argument_" + key)
    mesh = checked_record(reader, "face-template", "sha256:" + args["mesh_sha256"])
    closed(mesh, "face.fixture-mesh/v1", "vertices_um", "faces", "parts", "materials")
    require(0 < len(mesh["vertices_um"]) <= 65536, 6, "mesh_vertex_budget")
    registration = checked_record(reader, "face-views", "sha256:" + args["face_views_sha256"])
    closed(registration, "face.fixture-views/v1", "mesh_sha256", "fit_pin", "heldout_pin",
           "source_sha256", "track_pin")
    require(registration["mesh_sha256"] == args["mesh_sha256"], 4, "face_mesh_drift")
    fit = checked_record(reader, "face-fit-observations", registration["fit_pin"])
    closed(fit, "face.fit-observations/v1", "source_sha256", "views")
    require(fit["source_sha256"] == registration["source_sha256"], 4, "fit_source_drift")
    registered(fit["views"])
    for view in fit["views"].values():
        size = view["size"]
        pin = view["observation_image"]
        rgb = decoded_rgb(reader("face-designated-render", pin), pin, size)
        for _, _, xy in samples(mesh["vertices_um"], view):
            x, y = (int(v // domains.MILLIPX_PER_PIXEL) for v in xy)
            require(0 <= x < size[0] and 0 <= y < size[1], 3, "landmark_outside_observation")
            offset = 3 * (y * size[0] + x)
            require(any(rgb[offset:offset+3]), 3, "landmark_without_render_support")
    return mesh, registration, fit["views"]


def fit_request(args, reader):
    mesh, _, views = read_inputs(args, reader)
    scales = fit_axes(mesh["vertices_um"], views)
    vertices = [[round(v*s) for v, s in zip(point, scales)] for point in mesh["vertices_um"]]
    errors = project_errors(vertices, views, set())
    require(max(errors.values()) <= 1, 5, "fit_observation_residual")
    return vertices, [domains.pair(s) for s in scales]
