"""Registered projection from protected views to a static UV atlas."""
from fractions import Fraction
from typing import NamedTuple

from asset_factory import core, domains
from tools.input.evidence import InputIssue, bytes_digest, checked_record, closed, is_digest, point, require
from tools.mesh.contracts import MeshError
from .uv_quality import (area2, check_nonoverlap, checked_bytes, cross, dot, failure, load_triangles,
                         native, raster_size, sqrt_bounds, sub, texel_faces, weights, work_budget)


class _RegisteredView(NamedTuple):
    row: dict
    source: bytes
    parts: bytes
    confidence: bytes
    width: int
    crop: tuple
    rotation: tuple
    translation: tuple
    intrinsics: tuple


def _registered_view(selection, view, registration, pose_digest, read_checked):
    row = selection["selected"].get(view)
    require(row is not None, 3, "uv_registered_view_missing")
    timestamp = domains.rational(row["pts"])
    require(row["view"] == view and row["pose_digest"] == pose_digest
            and type(row["id"]) is str and row["id"] and timestamp >= 0, 4, "uv_registered_view_drift")
    camera = row["camera"]
    domains.pinhole(camera)
    rotation = tuple(tuple(map(domains.rational, r)) for r in camera["rotation"])
    translate = tuple(map(domains.rational, camera["translation_um"]))
    intrinsics = tuple(map(domains.rational, camera["intrinsics"]))
    width, height = raster_size(selection["raster"])
    crop = domains.rectangle(row["crop"]["rect"], (width, height))
    require(row["crop_transform"] == domains.crop_transform(row["crop"], (width, height)),
            4, "uv_registered_crop_drift")
    require(type(registration) is dict and registration.keys() == {"parts_digest", "confidence_digest"},
            2, "uv_source_registration_invalid")
    frame_digest = row["frame_digest"]
    require(is_digest(frame_digest), 2, "uv_frame_pin_invalid")
    source, parts, confidence = (
        checked_bytes(read_checked, role, pin, width * height * channels)
        for role, pin, channels in (
            ("uv-source-rgb", frame_digest, 3), ("uv-source-parts", registration["parts_digest"], 1),
            ("uv-source-confidence", registration["confidence_digest"], 1)))
    return _RegisteredView(row, source, parts, confidence, width, crop, rotation, translate, intrinsics)


class _CameraFace(NamedTuple):
    vertices: tuple
    normal: tuple
    plane: Fraction
    axes: tuple
    triangle: tuple
    footprint_squared: Fraction


def _footprint_squared(vertices, uv, intrinsics):
    """Lower-bound squared minimum source-pixel stretch across the UV face."""
    depths = [p[2] for p in vertices]
    if min(depths) <= 0:
        return Fraction(0)
    a, b, c = vertices
    ab, ac = sub(b, a), sub(c, a)
    (du1, dv1), (du2, dv2) = sub(uv[1], uv[0]), sub(uv[2], uv[0])
    area = area2(uv)
    u = tuple((dv2 * j - dv1 * k) / area for j, k in zip(ab, ac))
    v = tuple((du1 * k - du2 * j) / area for j, k in zip(ab, ac))
    fx, fy, _, _ = intrinsics
    # Each Jacobian numerator is affine in the camera-space position.
    # Enclose its Frobenius norm and the absolute determinant independently.
    frobenius_squared = sum(
        max(abs(f * (axis[i] * p[2] - p[i] * axis[2])) for p in vertices) ** 2
        for i, f in enumerate((fx, fy)) for axis in (u, v)
    ) / min(depths) ** 4
    determinant = fx * fy * dot(a, cross(u, v)) / max(depths) ** 3
    return determinant ** 2 / frobenius_squared if frobenius_squared else Fraction(0)


def _camera_faces(faces, view):

    records = []
    for xyz, uv in faces:
        vertices = tuple(tuple(dot(axis, p) + t for axis, t in zip(view.rotation, view.translation)) for p in xyz)
        a, b, c = vertices
        normal = cross(sub(b, a), sub(c, a))
        omit = max(range(3), key=lambda i: abs(normal[i]))
        axes = tuple(i for i in range(3) if i != omit)
        plane = dot(normal, a)
        triangle = tuple(tuple(p[i] for i in axes) for p in vertices)
        records.append(_CameraFace(vertices, normal, plane, axes, triangle,
                                   _footprint_squared(vertices, uv, view.intrinsics)))
    return records


class _RayNode(NamedTuple):
    low: tuple
    high: tuple
    faces: tuple
    children: tuple


def _ray_tree(camera_faces, charge):
    boxes = [tuple((min(axis), max(axis)) for axis in zip(*face.vertices)) for face in camera_faces]

    def build(ids):
        charge(len(ids))
        low = tuple(min(boxes[i][axis][0] for i in ids) for axis in range(3))
        high = tuple(max(boxes[i][axis][1] for i in ids) for axis in range(3))
        if len(ids) <= 8:
            return _RayNode(low, high, tuple(ids), ())
        axis = max(range(3), key=lambda k: high[k] - low[k])
        ids.sort(key=lambda i: sum(boxes[i][axis]))
        middle = len(ids) // 2
        return _RayNode(low, high, (), (build(ids[:middle]), build(ids[middle:])))

    return build(list(range(len(camera_faces))))


def _box_intersects(node, direction, maximum):
    # Ray origin is the camera. Slab comparisons use signed exact depths.
    lower, upper = Fraction(0), maximum
    for lo, hi, speed in zip(node.low, node.high, direction):
        if speed == 0:
            if lo > 0 or hi < 0:
                return False
            continue
        a, b = (lo / speed, hi / speed) if speed > 0 else (hi / speed, lo / speed)
        lower = max(lower, a)
        upper = b if upper is None else min(upper, b)
        if lower > upper or upper <= 0:
            return False
    return True


def _nearest(camera_faces, tree, direction, charge):
    nearest = None
    hits = []
    remaining = [tree]
    while remaining:
        node = remaining.pop()
        charge(1)
        if not _box_intersects(node, direction, nearest):
            continue
        remaining.extend(node.children)
        charge(len(node.faces))
        for face in node.faces:
            entry = camera_faces[face]
            denominator = dot(entry.normal, direction)
            if denominator == 0:
                if entry.plane == 0:
                    # Conservatively withhold an unresolved coplanar intersection.
                    return None, None
                continue
            depth = entry.plane / denominator
            if depth <= 0 or (nearest is not None and depth > nearest):
                continue
            if min(weights(entry.triangle, tuple(depth * direction[i] for i in entry.axes))) < 0:
                continue
            if depth != nearest:
                nearest, hits = depth, []
            hits.append(face)
    return nearest, hits


def _ray_status(camera_faces, depth, hits, direction, target, parts):
    if hits is None:
        return "ambiguous"
    if not hits:
        return "miss"
    if len(hits) > 1:
        # A shared edge or vertex is one observation only when the incident
        # faces agree on the part and share that exact geometric support.
        supports, triangles = set(), set()
        for face in hits:
            entry = camera_faces[face]
            bary = weights(entry.triangle, tuple(depth * direction[i] for i in entry.axes))
            support = frozenset(p for p, weight in zip(entry.vertices, bary) if weight > 0)
            supports.add(support)
            triangles.add(frozenset(entry.vertices))
        if (len({parts[i] for i in hits}) != 1 or len(supports) != 1
                or len(next(iter(supports))) > 2 or len(triangles) != len(hits)):
            return "ambiguous"
    return "hit" if target in hits else "occluded"


class _ProjectionPolicy(NamedTuple):
    min_confidence_ppm: int
    min_incidence_squared: Fraction
    min_footprint_squared: Fraction
    max_disagreement_ppm: int


class _Sample(NamedTuple):
    entry: dict
    rgb: bytes
    rank: tuple


def _source_pixel(camera_faces, tree, face, bary, part_ids, view, policy, charge):

    row, source, parts, confidence, width, crop, _, _, intrinsics = view
    fx, fy, cx, cy = intrinsics
    geometry = camera_faces[face]
    x, y, z = (dot(bary, axis) for axis in zip(*geometry.vertices))
    if z <= 0:
        return "behind_camera", None
    ray = (x / z, y / z, 1)
    if geometry.plane >= 0:
        return "back_facing", None
    i, j = (fx * ray[0] + cx).__floor__(), (fy * ray[1] + cy).__floor__()
    left, top, right, bottom = crop
    if not (left <= i < right and top <= j < bottom):
        return "outside_registered_crop", None
    near, observed = _nearest(camera_faces, tree, ray, charge)
    status = _ray_status(camera_faces, near, observed, ray, face, part_ids)
    if status != "hit":
        return {"miss": "ray_miss", "ambiguous": "ambiguous_ray", "occluded": "occluded"}[status], None
    if near != z:
        return "occluded", None
    part = part_ids[face]
    # Nearest-pixel sampling also needs visibility at the sampled pixel center.
    center_ray = ((i + Fraction(1, 2) - cx) / fx, (j + Fraction(1, 2) - cy) / fy, 1)
    depth, center_hits = _nearest(camera_faces, tree, center_ray, charge)
    status = _ray_status(camera_faces, depth, center_hits, center_ray, face, part_ids)
    if status in ("miss", "ambiguous"):
        return "sample_ray_miss" if status == "miss" else "ambiguous_sample_ray", None
    if status != "hit" or any(dot(camera_faces[k].normal, center_ray) >= 0 for k in center_hits):
        return "sample_occluded_or_wrong_part", None
    normal = geometry.normal
    incidence_squared = min(dot(normal, direction) ** 2 / (dot(normal, normal) * dot(direction, direction))
                            for direction in (ray, center_ray))
    if incidence_squared < policy.min_incidence_squared:
        return "grazing_incidence", None
    if geometry.footprint_squared < policy.min_footprint_squared:
        return "source_footprint_too_small", None
    index = j * width + i
    if parts[index] != part:
        return "wrong_part" if parts[index] else "source_support_missing", None
    raw_confidence = Fraction(confidence[index], 255)
    confidence_lower, _ = sqrt_bounds(incidence_squared * raw_confidence ** 2)
    confidence_ppm = int(confidence_lower * domains.PPM)
    if confidence_ppm < policy.min_confidence_ppm:
        return "source_confidence_missing", None
    displacement = sub(tuple(depth * k for k in center_ray), (x, y, z))
    distance_squared = dot(displacement, displacement)
    _, distance_upper = sqrt_bounds(distance_squared, scale=1)
    rgb = source[3 * index:3 * index + 3]
    entry = dict(view=row["view"], frame_id=row["id"], frame_digest=row["frame_digest"],
                 timestamp=row["pts"], pixel=[i, j], rgb8=list(rgb),
                 confidence_ppm=confidence_ppm, cos_incidence_squared=domains.pair(incidence_squared),
                 source_footprint_lower_squared=domains.pair(geometry.footprint_squared),
                 hit_distance_upper_um=int(distance_upper))
    rank = (incidence_squared, geometry.footprint_squared, -distance_squared,
            confidence_ppm, -domains.rational(row["pts"]))
    return "visible", _Sample(entry, rgb, rank)


def project_registered(raw, *, layout_digest, read_checked, candidate_rgba=None):
    """Recompute per-texel visibility and source RGB with a protected layout.

    Returns a native receipt and straight RGBA8 samples in top-left order.
    Unobserved texels remain transparent and are marked inferred. The result
    supplies projection support to LOOK-BAKE and conveys no asset admission.
    """
    receipt = native("uv.projection", raw, layout_digest)
    output = None
    try:
        layout = checked_record(read_checked, "uv-projection-layout", layout_digest)
        closed(layout, "af.uv-projection-layout/v1", "mesh_digest", "frame", "size", "parts",
               "pose_digest", "selection_digest", "required_views", "views", "support_digest",
               "max_faces", "max_work", "max_mesh_bytes", "min_confidence_ppm", "min_cos_incidence",
               "min_source_pixels_per_texel", "max_view_disagreement_ppm")
        size, faces, _ = load_triangles(raw, layout)
        length = size[0] * size[1]
        parts = domains.integers(layout["parts"], len(faces))
        require(min(parts) >= 1 and max(parts) <= 255 and is_digest(layout["pose_digest"]),
                2, "uv_part_or_pose_invalid")
        charge = work_budget(layout["max_work"])
        charge(length)
        check_nonoverlap(faces, charge)
        expected = checked_bytes(read_checked, "uv-atlas-parts", layout["support_digest"], length)
        owned = texel_faces(size, faces, charge)
        require({face for hits in owned.values() for face, _ in hits} == set(range(len(faces))),
                3, "uv_face_texel_support_missing")
        require(sum(bool(part) for part in expected) == len(owned)
                and all({parts[f] for f, _ in hits} == {expected[i]} for i, hits in owned.items()),
                5, "uv_atlas_support_changed")
        selection = checked_record(read_checked, "uv-input-selection", layout["selection_digest"])
        closed(selection, "af.input-frame-selection/v1", "source_digest", "stream", "raster",
               "plan_digest", "selected", "invented_details")
        require(is_digest(selection["source_digest"]) and is_digest(selection["plan_digest"]),
                2, "uv_selection_pin_invalid")
        registrations, declared = layout["views"], layout["required_views"]
        require(type(registrations) is dict and registrations and set(registrations) <= set(domains.REQUIRED_VIEWS)
                and type(declared) is list and declared and len(set(declared)) == len(declared)
                and set(declared) <= set(domains.REQUIRED_VIEWS), 2, "uv_registered_views_invalid")
        require(set(declared) <= registrations.keys(), 3, "uv_required_view_missing")
        views = {}
        frame_ids, frame_digests, timestamps = set(), set(), set()
        min_confidence, max_disagreement = layout["min_confidence_ppm"], layout["max_view_disagreement_ppm"]
        min_incidence = domains.rational(layout["min_cos_incidence"])
        min_footprint = domains.rational(layout["min_source_pixels_per_texel"])
        require(type(min_confidence) is int and 0 < min_confidence <= domains.PPM
                and 0 < min_incidence <= 1 and min_footprint > 0
                and type(max_disagreement) is int and 0 <= max_disagreement <= domains.PPM,
                2, "uv_projection_policy_invalid")
        policy = _ProjectionPolicy(min_confidence, min_incidence ** 2, min_footprint ** 2, max_disagreement)
        charge(len(registrations) * (len(faces) + len(owned)))
        for view, registration in sorted(registrations.items()):
            data = _registered_view(selection, view, registration, layout["pose_digest"], read_checked)
            row = data.row
            stamp = domains.rational(row["pts"])
            require(row["id"] not in frame_ids and row["frame_digest"] not in frame_digests
                    and stamp not in timestamps, 5, "uv_replayed_source_frame")
            frame_ids.add(row["id"])
            frame_digests.add(row["frame_digest"])
            timestamps.add(stamp)
            camera = _camera_faces(faces, data)
            views[view] = (camera, _ray_tree(camera, charge), data)

        require(candidate_rgba is None or (type(candidate_rgba) is bytes and len(candidate_rgba) == length * 4),
                2, "uv_projection_rgba_invalid")
        if candidate_rgba is not None:
            receipt["input_refs"].append(bytes_digest(candidate_rgba))
        output = bytearray(length * 4)
        texels, inferred, wrong, ray_misses, ambiguities, disputed = [], 0, 0, 0, 0, 0
        max_spread, max_distance = None, None
        used_views = set()
        for index, hits in sorted(owned.items()):
            visible, view_checks = [], {}
            # Coincident atlas edge samples may be aggregated only when their
            # world point and independently declared part agree.
            positions = {tuple(dot(bary, axis) for axis in zip(*faces[face][0])) for face, bary in hits}
            for name, (camera, tree, data) in views.items():
                if len(positions) != 1:
                    view_checks[name] = "ambiguous_uv_edge"
                    continue
                samples = [_source_pixel(camera, tree, face, bary, parts, data, policy, charge)
                           for face, bary in hits]
                good = [value for _, value in samples if value is not None]
                for state, _ in samples:
                    ray_misses += state in ("ray_miss", "sample_ray_miss")
                    ambiguities += state in ("ambiguous_ray", "ambiguous_sample_ray")
                    wrong += state == "wrong_part"
                view_checks[name] = "visible" if good else next(
                    (state for state, _ in samples if state == "wrong_part"), samples[0][0])
                visible.extend(good)
            observing_views = {sample.entry["view"] for sample in visible}
            entry = dict(index=index, part=expected[index], frame_id=None, frame_digest=None, view=None,
                         timestamp=None, pixel=None, confidence_ppm=None, inferred=True,
                         occlusion="unobserved", view_checks=view_checks, color_spread_rgb8=None,
                         view_disagreement_upper_ppm=None, samples=[sample.entry for sample in visible])
            for sample in visible:
                distance = sample.entry["hit_distance_upper_um"]
                max_distance = distance if max_distance is None else max(max_distance, distance)
            if len(observing_views) > 1:
                spread = [max(channel) - min(channel) for channel in zip(*(sample.rgb for sample in visible))]
                disagreement = (max(spread) * domains.PPM + 254) // 255
                entry.update(color_spread_rgb8=spread, view_disagreement_upper_ppm=disagreement)
                max_spread = disagreement if max_spread is None else max(max_spread, disagreement)
            disagreement = entry["view_disagreement_upper_ppm"]
            if disagreement is not None and disagreement > policy.max_disagreement_ppm:
                disputed += 1
                entry["occlusion"] = "uncertain"
            elif visible:
                best = max(visible, key=lambda sample: sample.rank)
                output[4 * index:4 * index + 4] = best.rgb + b"\xff"
                entry.update(best.entry, inferred=False, occlusion="visible")
                used_views.update(observing_views)
            inferred += entry["inferred"]
            texels.append(entry)
        output = bytes(output)
        mask_refs = {pin for registration in registrations.values() for pin in registration.values()}
        uncovered_views = sorted(set(declared) - used_views)
        receipt.update(size=list(size), selection_digest=layout["selection_digest"], texels=texels,
                       projection_sha256=bytes_digest(output), inferred_texels=inferred,
                       missing_required_views=uncovered_views, disputed_texels=disputed,
                       ray_misses=ray_misses, ambiguous_hits=ambiguities, hit_distance_upper_um=max_distance,
                       view_disagreement_upper_ppm=max_spread, disagreement_encoding="source_rgb8",
                       reference_refs=sorted({layout_digest, layout["selection_digest"], layout["support_digest"],
                                              selection["source_digest"], selection["plan_digest"],
                                              *frame_digests, *mask_refs}))
        metrics = receipt["metrics"]
        metrics.update(bake_coverage_lower_ppm=point(Fraction(len(owned) - inferred, len(owned))),
                       wrong_part_hits=point(wrong), ray_misses=point(ray_misses),
                       ambiguous_hits=point(ambiguities))
        if max_distance is not None:
            metrics["hit_distance_upper_um"] = point(max_distance)
        if max_spread is not None:
            metrics["view_disagreement_upper_ppm"] = point(Fraction(max_spread, domains.PPM))
        reasons = []
        if disputed:
            reasons.append("uv_view_disagreement")
        if candidate_rgba is not None and candidate_rgba != output:
            reasons.append("uv_projection_pixels_changed")
            if wrong:
                reasons.append("uv_projection_wrong_part")
        if reasons:
            receipt.update(source_rc=5, reason_codes=reasons)
        elif inferred or uncovered_views:
            receipt.update(source_rc=3, reason_codes=["uv_required_projection_support_missing"])
        return receipt, output
    except (InputIssue, MeshError, core.ContractError, ValueError, TypeError, KeyError, IndexError,
            MemoryError, OSError, OverflowError, RecursionError) as error:
        return failure(receipt, error), None
