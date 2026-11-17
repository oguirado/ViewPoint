"""Offline GEO evaluator for registered static GLBs. All geometry is rational µm.

A checked reader authorizes a frozen domain and separate, subject-bound face
registrations. A successful measurement is diagnostic until BASE qualifies it.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
import json
from math import ceil, floor, isqrt
from pathlib import Path
import struct
import sys

from asset_factory import core, domains, math_acceptance
from tools.input.evidence import (MAX_RECORD_BYTES, InputIssue, bytes_digest, canonical_bytes,
                                  checked_record as input_checked_record,
                                  closed, is_digest, require)
from tools.mesh import contracts, raw_admission
from tools.mesh.worker import _document


def record_limit(role):
    from tools.blender.topology_glb import MAX_GLB_BYTES

    if role in {"geo-registration", "geo-quad-cage", "geo-quad-normals"}:
        return MAX_GLB_BYTES
    return MAX_RECORD_BYTES


def checked_record(reader, role, pin):
    if record_limit(role) == MAX_RECORD_BYTES:
        return input_checked_record(reader, role, pin)
    require(is_digest(pin), 2, "invalid_" + role + "_pin")
    raw = reader(role, pin)
    require(raw is not None, 3, "missing_" + role)
    require(type(raw) is bytes, 2, "invalid_" + role + "_bytes")
    require(len(raw) <= record_limit(role), 6, role + "_byte_budget")
    require(bytes_digest(raw) == pin, 4, "stale_" + role)
    document = json.loads(raw)
    require(type(document) is dict and canonical_bytes(document) == raw, 2, "noncanonical_" + role)
    return document


class Work:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def charge(self, n=1):
        require(n <= self.limit - self.used, 6, "geometry_work_budget")
        self.used += n


def sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def cross(a, b):
    return tuple(a[i-2]*b[i-1] - a[i-1]*b[i-2] for i in range(3))


def lerp(a, b, t):
    return tuple(x + t*(y-x) for x, y in zip(a, b))


def cycle(points):
    return zip(points, points[1:] + points[:1])


def normal(t):
    return cross(sub(t[1], t[0]), sub(t[2], t[0]))


def norm2(v):
    return dot(v, v)


def halfspaces(p, t, n):
    return [dot(cross(sub(b, a), sub(p, a)), n) for a, b in cycle(t)]


def segment_d2(p, a, b):
    v = sub(b, a)
    along = dot(sub(p, a), v)
    length2 = norm2(v)
    if along <= 0:
        return norm2(sub(p, a))
    if along >= length2:
        return norm2(sub(p, b))
    return norm2(sub(p, a)) - Fraction(along**2, length2)


def triangle_d2(p, t):
    n = normal(t)
    if any(n) and min(halfspaces(p, t, n)) >= 0:
        return Fraction(dot(sub(p, t[0]), n)**2, norm2(n))
    return min(segment_d2(p, a, b) for a, b in cycle(t))


@dataclass(slots=True)
class _Node:
    box: tuple
    children: tuple = ()
    triangles: tuple = ()


class _TriangleBVH:
    """Balanced target tree. Only exact coordinate boxes participate in pruning."""

    def __init__(self, triangles, work):
        work.charge(len(triangles))
        # Approximation is confined to split order. Bounds and queries are exact.
        def sort_coordinate(axis):
            total = sum(axis)
            try:
                return float(total)
            except OverflowError:
                return total

        centers = [tuple(sort_coordinate(axis) for axis in zip(*t)) for t in triangles]

        def build(indices, depth):
            work.charge(len(indices))
            if len(indices) <= 8:
                faces = tuple(triangles[i] for i in indices)
                return _Node(_box(p for t in faces for p in t), triangles=faces)
            axis = depth % 3
            indices.sort(key=lambda i: centers[i][axis])
            mid = len(indices) // 2
            left = build(indices[:mid], depth+1)
            right = build(indices[mid:], depth+1)
            box = tuple((min(a, c), max(b, d)) for (a, b), (c, d) in zip(left.box, right.box))
            return _Node(box, children=(left, right))

        self.root = build(list(range(len(triangles))), 0)

    def cell_bounds(self, cell, work, compatible=None):
        nearest = [None] * len(cell)
        best_upper = None

        def bounds(node):
            work.charge(len(cell))
            return [sum(max(lo-x, x-hi, 0)**2 for x, (lo, hi) in zip(point, node.box))
                    for point in cell]

        pending = [(self.root, bounds(self.root))]
        while pending:
            node, lower = pending.pop()
            # Both objectives must be dominated: even a box too far from one
            # corner to improve the upper bound may contain another's nearest.
            if (best_upper is not None and max(lower) >= best_upper
                    and all(d >= best for d, best in zip(lower, nearest))):
                continue
            if node.children:
                children = [(child, bounds(child)) for child in node.children]
                children.sort(key=lambda pair: max(pair[1]), reverse=True)
                pending.extend(children)
                continue
            for triangle in node.triangles:
                work.charge(len(cell))
                if compatible is not None and not compatible(triangle):
                    continue
                ds = [triangle_d2(p, triangle) for p in cell]
                nearest = [d if old is None else min(d, old) for d, old in zip(ds, nearest)]
                proposal = max(ds)
                best_upper = proposal if best_upper is None else min(best_upper, proposal)
        require(best_upper is not None, 5, "geometric_normal_without_match")
        return max(nearest), best_upper


def directed_bounds(source, target, limit, refinements, work, *, integral=None):
    """Squared bounds on continuous directed H. Convexity certifies each cell.

    A fixed target triangle is convex. The maximum of squared distance to
    it over a source triangle is attained at a corner. Minimizing these
    whole-cell upper bounds is safe even if nearest faces differ by corner.
    """
    require(source and target, 3, "empty_distance_support")
    work.charge(len(source) + len(target))
    identical = {frozenset(t) for t in target}
    pending = [(t, 0) for t in source if frozenset(t) not in identical]
    index = _TriangleBVH(target, work) if pending else None
    return _refined_bounds(source, pending, index, limit, refinements, work, integral)


def _refined_bounds(source, pending, index, limit, refinements, work, integral=None, compatible=None):
    if integral is not None:
        # Twice the area is enough: the common factor cancels in the mean.
        area_lower = sum(sqrt_bounds(norm2(normal(t)))[0] for t in source)
        weighted_upper = 0
    lower = upper = 0
    while pending:
        cell, depth = pending.pop()
        cell_lower, cell_upper = index.cell_bounds(cell, work, compatible)
        lower = max(lower, cell_lower)
        if cell_lower <= limit**2 < cell_upper and depth < refinements:
            a, b, c = cell
            ab, bc, ca = [lerp(p, q, Fraction(1, 2)) for p, q in cycle(cell)]
            pending.extend((t, depth+1) for t in ((a, ab, ca), (ab, b, bc),
                                                   (ca, bc, c), (ab, bc, ca)))
        else:
            upper = max(upper, cell_upper)
            if integral is not None:
                weighted_upper += sqrt_bounds(norm2(normal(cell)))[1] * cell_upper
    if integral is not None:
        if area_lower:
            integral.append(min(upper, weighted_upper / area_lower))
        else:
            # A very small nonzero area can round down to zero in the reporting
            # enclosure. The supremum still gives a valid integral mean bound.
            integral.append(upper)
    return lower, upper


def sqrt_bounds(sq):
    """Tight enclosure on a one-millionth unit reporting lattice."""
    scaled = Fraction(sq) * 10**12
    lo = isqrt(scaled.numerator // scaled.denominator)
    return Fraction(lo, 10**6), Fraction(lo + (lo**2 != scaled), 10**6)


def decide(low_squared, high_squared, limit, name):
    require(low_squared <= limit**2, 5, name + ":witness")
    require(high_squared <= limit**2, 3, name + ":inconclusive")


def _distance(a, b, limit, domain, work, metrics, key, *, mean=None):
    comparisons = []
    for direction, first, second in (("forward", a, b), ("reverse", b, a)):
        lo, hi = directed_bounds(first, second, limit, domain["refinements"], work, integral=mean)
        label = key + ":" + direction
        metrics[label] = [domains.pair(sqrt_bounds(lo)[0]), domains.pair(sqrt_bounds(hi)[1])]
        comparisons.append((lo, hi, label))
    for lo, hi, label in sorted(comparisons, key=lambda row: row[0], reverse=True):
        decide(lo, hi, limit, label)


def normal_cover(source, target, distance, cosine_squared_min_ppm, refinements, work):
    """Every point has an in-range correspondent with an admissible oriented normal."""
    require(source and target, 3, "empty_distance_support")
    work.charge(len(target))
    worst = 0
    unchanged = {_oriented(t) for t in target}
    index = None
    for cell in source:
        work.charge()
        if _oriented(cell) in unchanged:
            continue
        if index is None:
            index = _TriangleBVH(target, work)
        n = normal(cell)
        threshold = cosine_squared_min_ppm * norm2(n)

        def compatible(triangle):
            m = normal(triangle)
            nm = dot(n, m)
            return nm > 0 and nm**2 * domains.PPM >= threshold * norm2(m)

        lo, hi = _refined_bounds([cell], [(cell, 0)], index, distance, refinements,
                                 work, compatible=compatible)
        decide(lo, hi, distance, "normal_distance")
        worst = max(worst, hi)
    return worst


def _edge_contacts(p, q, t):
    """Exact endpoints of the intersection of a closed segment and triangle."""
    n = normal(t)
    u, v = (dot(sub(x, t[0]), n) for x in (p, q))
    if u*v > 0:
        return ()
    if u != v:
        x = lerp(p, q, Fraction(u, u-v))
        return (x,) if min(halfspaces(x, t, n)) >= 0 else ()
    low, high = 0, 1
    for start, end in zip(halfspaces(p, t, n), halfspaces(q, t, n)):
        if max(start, end) < 0:
            return ()
        if start < 0:
            low = max(low, Fraction(start, start-end))
        if end < 0:
            high = min(high, Fraction(start, start-end))
    return (lerp(p, q, low), lerp(p, q, high)) if low <= high else ()


def _box(t):
    return tuple((min(axis), max(axis)) for axis in zip(*t))


def _disjoint(a, b):
    return any(right < low or high < left for (left, right), (low, high) in zip(a, b))


def _embedded(rows, work):
    # Sweep on X; Y/Z and exact edge/triangle tests settle surviving pairs.
    active = []
    for box, shell, triangle in sorted(rows):
        active = [r for r in active if r[0][0][1] >= box[0][0]]
        work.charge(len(active) + 1)
        for other_box, other_shell, other in active:
            if _disjoint(box, other_box):
                continue
            common = set(triangle).intersection(other) if shell == other_shell else set()
            require(len(common) < 3, 5, "duplicate_face")
            for source, target in ((triangle, other), (other, triangle)):
                for a, b in cycle(source):
                    for p in _edge_contacts(a, b, target):
                        if p in common:
                            continue
                        if len(common) == 2 and segment_d2(p, *common) == 0:
                            continue
                        raise InputIssue(5, "forbidden_triangle_intersection")
        active.append((box, shell, triangle))


def _component(graph):
    seen = set()
    stack = [next(iter(graph))]
    while stack:
        v = stack.pop()
        if v not in seen:
            seen.add(v)
            stack.extend(graph[v] - seen)
    return seen


def _shell(tris, expected, work):
    require(tris, 3, "empty_shell")
    edges = defaultdict(list)
    links = defaultdict(lambda: defaultdict(set))
    graph = defaultdict(set)
    signed = 0
    min_q2 = 1
    max_cos2 = 0
    max_aspect2 = 0
    for t in tris:
        work.charge()
        area4 = norm2(normal(t))
        require(area4 > 0, 5, "degenerate_triangle")
        lengths = [norm2(sub(b, a)) for a, b in cycle(t)]
        for i, vertex in enumerate(t):
            a, b = sub(t[i-1], vertex), sub(t[(i+1) % 3], vertex)
            positive_dot = max(0, dot(a, b))
            max_cos2 = max(max_cos2, Fraction(positive_dot**2, norm2(a)*norm2(b)))
        min_q2 = min(min_q2, Fraction(12*area4, sum(lengths)**2))
        max_aspect2 = max(max_aspect2, Fraction(max(lengths)**2, area4))
        signed += dot(t[0], cross(t[1], t[2]))
        for i, (a, b) in enumerate(cycle(t)):
            edges[tuple(sorted((a, b)))].append(a < b)
            graph[a].add(b)
            graph[b].add(a)
            v = t[i-1]
            links[v][a].add(b)
            links[v][b].add(a)
    boundary = defaultdict(set)
    for (a, b), signs in edges.items():
        if len(signs) == 1:
            boundary[a].add(b)
            boundary[b].add(a)
        else:
            require(sorted(signs) == [False, True], 5, "edge_incidence_or_orientation")
    require(len(_component(graph)) == len(graph), 5, "shell_disconnected")
    for v, link in links.items():
        degrees = [len(row) for row in link.values()]
        require(max(degrees) <= 2 and degrees.count(1) == (2 if v in boundary else 0)
                and len(_component(link)) == len(link), 5, "vertex_link")
    loops = 0
    while boundary:
        group = _component(boundary)
        for v in group:
            require(len(boundary.pop(v)) == 2, 5, "invalid_boundary")
        loops += 1
    chi = len(graph) - len(edges) + len(tris)
    twice_genus = 2 - loops - chi
    require(twice_genus >= 0 and twice_genus % 2 == 0, 5, "invalid_manifold_euler_characteristic")
    genus = twice_genus // 2
    if expected is not None:
        require(loops == expected["loops"] and genus == expected["genus"], 5, "shell_topology")
        if loops == 0:
            require(signed * expected["orientation"] > 0, 5, "shell_orientation")
    valences = [len(edges_at_vertex) for edges_at_vertex in graph.values()]
    histogram = {str(n): count for n, count in Counter(valences).items()}
    if not loops:
        require(sum(6-n for n in valences) == 6*chi, 5, "valence_identity")
    return dict(chi=chi, genus=genus, boundary_loops=loops, vertex_valence=histogram,
                signed_volume6_um3=domains.pair(signed) if loops == 0 else None,
                min_quality_squared=domains.pair(min_q2), max_aspect_squared=domains.pair(max_aspect2),
                minimum_angle_cos_squared=domains.pair(max_cos2))


def _int(n, minimum):
    return type(n) is int and n >= minimum


def _validate_domain(d):
    require("raw_registration_digest" in d, 3, "raw_geometry_registration_missing")
    require(is_digest(d["raw_registration_digest"]), 2, "invalid_raw_geometry_registration_digest")
    optional = tuple(k for k in ("quad_levels", "features") if k in d)
    closed(d, "af.geometry-domain/v1", "raw_registration_digest", *optional, "source", "frame", "intent", "reference_length_um",
           "shells", "regions_um", "normal_cos2_min_ppm", "min_q2_ppm", "max_aspect2", "views", "selection", "pose", "distance_max_ppm", "switch_max_um", "iou_min_ppm",
           "projected_max_millipx", "raster_support_max_millipx", "max_work", "refinements", "face_budgets")
    require(d["frame"] == domains.FRAME and is_digest(d["source"]) and is_digest(d["intent"]),
            2, "invalid_geometry_frame_or_source")
    for name in ("reference_length_um", "max_work"):
        require(_int(d[name], 1), 2, "invalid_" + name)
    for name in ("distance_max_ppm", "switch_max_um", "projected_max_millipx", "raster_support_max_millipx", "refinements"):
        require(_int(d[name], 0), 2, "invalid_" + name)
    require(d["distance_max_ppm"] <= contracts.DISTANCE_MAX_PPM and _int(d["iou_min_ppm"],
            contracts.IOU_MIN_PPM) and d["iou_min_ppm"] <= domains.PPM, 2, "geometry_policy_weakened")
    for key in ("normal_cos2_min_ppm", "min_q2_ppm"):
        require(_int(d[key], 1) and d[key] <= domains.PPM, 2, "invalid_shape_criterion")
    require(_int(d["max_aspect2"], 1), 2, "invalid_shape_criterion")
    budgets = d["face_budgets"]
    require(type(budgets) is list and 1 <= len(budgets) <= 9 and all(_int(n, 1) for n in budgets)
            and all(a > b for a, b in zip(budgets, budgets[1:])), 2, "invalid_lod_budgets")
    if "features" in d:
        features = d["features"]
        require(type(features) is dict and features.keys() == {"reference", "limits_um"}
                and is_digest(features["reference"]) and type(features["limits_um"]) is dict
                and features["limits_um"] and all(_int(x, 0) for x in features["limits_um"].values()),
                2, "invalid_curve_domain")
    quad_levels = d.get("quad_levels", {})
    require(type(quad_levels) is dict, 2, "invalid_quad_policy")
    for level, row in quad_levels.items():
        require(level in {str(i) for i in range(1, len(budgets)+1)} and type(row) is dict
                and row.keys() == {"jacobian2_min_ppm", "split_cos2_min_ppm"}
                and all(_int(v, 1) and v <= domains.PPM for v in row.values()), 2, "invalid_quad_policy")
    require(type(d["shells"]) is dict and d["shells"], 3, "shell_expectation_missing")
    for row in d["shells"].values():
        require(type(row) is dict and row.keys() - {"parent"} == {"genus", "loops", "role", "orientation"}
                and type(row["role"]) is str and row["role"] and _int(row["genus"], 0)
                and _int(row["loops"], 0) and type(row["orientation"]) is int, 2, "invalid_shell")
        require(row["orientation"] in ((0,) if row["loops"] else (-1, 1)), 2, "invalid_shell_orientation")
        require(row.get("parent") is None or (not row["loops"] and row["parent"] in d["shells"]),
                2, "invalid_containment_parent")
    for key, row in d["shells"].items():
        parents = _ancestors(key, d["shells"])
        if not row["loops"]:
            require(row["orientation"] == (-1)**len(parents), 2, "invalid_material_orientation")
    regions = d["regions_um"]
    require(type(regions) is dict and {"face", "left_hand", "right_hand"} <= regions.keys()
            and all(_int(n, 0) for n in regions.values()), 3, "protected_region_domain_missing")
    for name in ("face", "left_hand", "right_hand"):
        require(regions[name] * domains.PPM < d["reference_length_um"] * d["distance_max_ppm"],
                2, "protected_region_limit_not_tighter:" + name)
    require(type(d["views"]) is dict and d["views"], 3, "registered_views_missing")
    for view in d["views"].values():
        require(type(view) is dict and view.keys() == {"camera", "size", "near_um", "far_um"},
                2, "invalid_geometry_view")
        domains.pinhole(view["camera"])
        require(min(domains.integers(view["size"], 2)) > 0 and _int(view["near_um"], 1)
                and _int(view["far_um"], view["near_um"]+1), 2, "invalid_view_frustum")


def _registered_views(d, reader):
    """The authority supplies the verified INPUT selection in its own role."""
    selection = checked_record(reader, "geo-input-selection", d["selection"])
    require(selection["schema"] == "af.input-frame-selection/v1" and is_digest(d["pose"]),
            2, "invalid_input_selection")
    rows = selection["selected"]
    require(type(rows) is dict and rows.keys() == set(domains.REQUIRED_VIEWS)
            and rows.keys() == d["views"].keys(), 4, "registered_view_coverage_drift")
    require(selection["pose_digest"] == d["pose"], 4, "selection_pose_drift")
    transforms = {}
    for name, view in d["views"].items():
        row = rows[name]
        require(row["view"] == name and row["pose_digest"] == d["pose"]
                and canonical_bytes(row["camera"]) == canonical_bytes(view["camera"])
                and row["crop"]["size"] == view["size"], 4, "registered_camera_or_pose_drift")
        affine = domains.crop_transform(row["crop"], selection["raster"])
        require(affine == row["crop_transform"], 4, "registered_crop_drift")
        transforms[name] = tuple(map(domains.rational, affine))
    return transforms


def _ancestors(key, shells):
    ancestors = set()
    parent = shells[key].get("parent")
    while parent is not None:
        require(parent in shells and parent != key and parent not in ancestors
                and not shells[parent]["loops"], 2, "invalid_containment_tree")
        ancestors.add(parent)
        parent = shells[parent].get("parent")
    return ancestors


def _inside(point, closed_shell, work):
    # Exact parity. A ray contained in a face plane or meeting an edge is
    # inconclusive; try another direction, without assigning it an inside vote.
    for k in range(1, 33):
        direction = (1, k, k*k)
        hits = 0
        for t in closed_shell:
            work.charge()
            n = normal(t)
            num, den = dot(n, sub(t[0], point)), dot(n, direction)
            if den == num == 0:
                break
            if den == 0 or num * den <= 0:
                continue
            crossing = tuple(x + v*Fraction(num, den) for x, v in zip(point, direction))
            signs = halfspaces(crossing, t, n)
            if min(signs) == 0:
                break
            hits += min(signs) > 0
        else:
            return bool(hits % 2)
    raise InputIssue(3, "shell_containment_unresolved")


def _containment(parts, shells, work):
    boxes = {key: _box(p for t in part for p in t) for key, part in parts.items()}
    for key, part in parts.items():
        expected = _ancestors(key, shells)
        for other, enclosing in parts.items():
            if key == other or shells[other]["loops"]:
                continue
            enclosed = all(a <= c <= d <= b for (c,d),(a,b) in zip(boxes[key], boxes[other]))
            inside = enclosed and _inside(part[0][0], enclosing, work)
            require(inside == (other in expected), 5, "shell_containment:" + key)


def _partition(primitives, shells, selectors, components, work):
    require(type(selectors) is dict and selectors.keys() == shells.keys(), 3, "shell_registration_missing")
    occupied = {key: set() for key in primitives}
    counts = {key: 0 for key in primitives}
    parts = {}
    for key, selector in selectors.items():
        require(type(selector) is list and len(selector) == 2, 2, "invalid_shell_selector")
        primitive, ordinals = selector
        require(primitive in primitives and shells[key]["role"] == components[primitive]["role"],
                4, "shell_role_drift")
        source = primitives[primitive]
        if ordinals == "all":
            ordinals = range(len(source))
        else:
            require(type(ordinals) is list and ordinals, 2, "invalid_shell_faces")
        work.charge(len(ordinals))
        for i in ordinals:
            require(_int(i, 0) and i < len(source) and i not in occupied[primitive],
                    2, "invalid_or_repeated_shell_face")
            occupied[primitive].add(i)
        counts[primitive] += 1
        parts[key] = [source[i] for i in ordinals]
    require(all(len(occupied[k]) == len(tris) and counts[k] == components[k]["count"]
                for k, tris in primitives.items()), 3, "shell_face_coverage")
    return parts


def _oriented(t):
    return min(t[i:] + t[:i] for i in range(3))


def quad_metrics(cage, reference_normals, exported, work):
    """Bind retained cage to exported oriented triangles and authored patch normals."""
    closed(cage, "af.quad-cage/v1", "frame", "vertices_um", "quads", "triangles", "triangle_quads")
    require(cage["frame"] == domains.FRAME, 4, "cage_frame_drift")
    points, quads, triangles, owners = (cage[k] for k in ("vertices_um", "quads", "triangles", "triangle_quads"))
    require(type(quads) is list and quads and type(reference_normals) is list and
            len(quads) == len(reference_normals), 3, "quad_normal_coverage")
    require(len(owners) == len(triangles) == 2*len(quads) == len(exported), 4, "cage_export_coverage")
    work.charge(len(points) + len(triangles))
    xyz = [tuple(map(domains.rational, p)) for p in points]
    require(all(len(p) == 3 for p in xyz), 2, "invalid_cage_vertex")
    splits = defaultdict(list)
    soup = []
    for face, owner in zip(triangles, owners):
        ids = tuple(domains.integers(face, 3))
        require(_int(owner, 0) and owner < len(quads) and all(0 <= k < len(xyz) for k in ids),
                2, "invalid_cage_index")
        splits[owner].append(_oriented(ids))
        soup.append(_oriented(tuple(xyz[k] for k in ids)))
    require(Counter(soup) == Counter(map(_oriented, exported)), 4, "cage_export_drift")
    min_jacobian, min_cosine = 1, 1
    for q, row in enumerate(quads):
        a, b, c, d = domains.integers(row, 4)
        require(len(set(row)) == 4 and all(0 <= k < len(xyz) for k in row), 2, "invalid_quad")
        splits_ac = {_oriented((a,b,c)), _oriented((a,c,d))}
        splits_bd = {_oriented((a,b,d)), _oriented((b,c,d))}
        require(set(splits[q]) in (splits_ac, splits_bd), 5, "invalid_quad_split")
        ref = domains.integers(reference_normals[q], 3)
        require(any(ref), 3, "quad_reference_normal_missing")
        for i, k in enumerate(row):
            u, v = sub(xyz[row[(i+1)%4]], xyz[k]), sub(xyz[row[i-1]], xyz[k])
            sign = dot(cross(u, v), ref)
            require(sign > 0, 5, "quad_corner_jacobian")
            min_jacobian = min(min_jacobian, Fraction(sign**2, norm2(ref)*norm2(u)*norm2(v)))
        n, m = [normal([xyz[k] for k in face]) for face in splits[q]]
        nm = dot(n, m)
        require(nm > 0, 5, "quad_split_fold")
        min_cosine = min(min_cosine, Fraction(nm**2, norm2(n)*norm2(m)))
    return dict(quads=len(quads), minimum_jacobian2=domains.pair(min_jacobian),
                minimum_split_cosine2=domains.pair(min_cosine))


def _feature_bounds(parts, domain, registration, reader, work):
    """Bound a registered mesh edge chain against an independently enclosed curve.

    Each arc uses its own normalized arc-length parameter. Its deviation from
    the chord is bounded by kappa * length² / 8. Linear interpolation adds at
    most the largest endpoint error. The common parameter covers both curves.
    """
    policy = domain.get("features")
    if policy is None:
        return {}
    ref = checked_record(reader, "geo-feature-reference", policy["reference"])
    closed(ref, "af.feature-curves/v1", "source", "frame", "curves")
    require((ref["source"], ref["frame"]) == (domain["source"], domain["frame"]), 4, "feature_reference_drift")
    chains = registration["feature_edges"]
    require(type(chains) is dict and chains.keys() == ref["curves"].keys() == policy["limits_um"].keys(),
            3, "feature_curve_coverage")

    def points(row):
        require(type(row) is list and len(row) >= 2, 3, "feature_endpoints_missing")
        require(all(type(p) is list and len(p) == 3 for p in row), 2, "invalid_feature_point")
        return [tuple(map(domains.rational, p)) for p in row]

    edge_sets = {}
    bounds = {}
    for name, curve in ref["curves"].items():
        require(type(curve) is dict and curve.keys() == {"shell", "points_um", "segments"},
                2, "invalid_feature_curve")
        shell = curve["shell"]
        require(shell in parts, 4, "feature_shell_drift")
        if shell not in edge_sets:
            work.charge(3 * len(parts[shell]))
            edge_sets[shell] = {frozenset(edge) for t in parts[shell] for edge in cycle(t)}
        original, candidate = points(curve["points_um"]), points(chains[name])
        segments = curve["segments"]
        require(type(segments) is list and len(candidate) == len(original) == len(segments)+1,
                3, "feature_chain_coverage")
        work.charge(len(segments))
        squared_errors = [norm2(sub(a, b)) for a, b in zip(original, candidate)]
        errors = [sqrt_bounds(error)[1] for error in squared_errors]
        bound = 0
        for i, segment in enumerate(segments):
            require(frozenset(candidate[i:i+2]) in edge_sets[shell], 5, "registered_feature_edge_missing:" + name)
            require(type(segment) is dict and segment.keys() == {"arc_upper_um", "curvature_upper_per_um"},
                    2, "invalid_curve_certificate")
            length = segment["arc_upper_um"]
            curvature = domains.rational(segment["curvature_upper_per_um"])
            require(_int(length, 1) and curvature >= 0
                    and length**2 >= norm2(sub(original[i], original[i+1])), 4, "invalid_curve_enclosure")
            bound = max(bound, max(errors[i:i+2]) + curvature * length**2 / 8)
        require(max(squared_errors) <= policy["limits_um"][name]**2,
                5, "feature_endpoint_witness:" + name)
        require(bound <= policy["limits_um"][name], 3, "feature_curve_inconclusive:" + name)
        bounds[name] = domains.pair(bound)
    return bounds


def _check_seams(doc, data, primitive, welds, key, unique_positions, work):
    position = primitive["attributes"]["POSITION"]
    vertex_count = doc["accessors"][position]["count"]
    if "indices" in primitive:
        index = primitive["indices"]
        layout = {5121: "B", 5123: "H", 5125: "I"}[doc["accessors"][index]["componentType"]]
        work.charge(doc["accessors"][index]["count"])
        used = {i for i, in raw_admission._accessor(doc, data, index, layout)}
    else:
        used = range(vertex_count)
    if key not in welds:
        require(len(used) == unique_positions, 3, "coincident_index_seams_unqualified")
        return

    # Each POSITION accessor index names a canonical representative in the same
    # accessor. Exact binary32 equality is invariant under the GEO frame change.
    mapping = welds[key]
    require(type(mapping) is list and len(mapping) == vertex_count, 2, "invalid_welded_index_map")
    work.charge(vertex_count)
    points = list(raw_admission._accessor(doc, data, position, "fff"))
    for i, rep in enumerate(mapping):
        require(_int(rep, 0) and rep < vertex_count and mapping[rep] == rep,
                2, "invalid_welded_index_map")
        require(points[i] == points[rep], 4, "welded_vertex_position_drift")
    # Downstream topology uses position keys. Prove their equivalence relation
    # equals the authority's over used indices before using that representation.
    require(len({mapping[i] for i in used}) == unique_positions,
            3, "coincident_index_seams_unqualified")


def _geometry(blob, reg_pin, d, domain_pin, reader, work, level):
    subject = bytes_digest(blob)
    reg = checked_record(reader, "geo-registration", reg_pin)
    quad_policy = d.get("quad_levels", {}).get(str(level))
    extra = ("quad_cage", "quad_normals") if quad_policy else ()
    require(not quad_policy or set(extra) <= reg.keys(), 3, "required_quad_cage_missing")
    if "features" in d:
        require("feature_edges" in reg, 3, "feature_chain_missing")
        extra += ("feature_edges",)
    welds = reg.get("welded_index_maps", {})
    if "welded_index_maps" in reg:
        require(type(welds) is dict, 2, "invalid_welded_index_maps")
        extra += ("welded_index_maps",)
    closed(reg, "af.geometry-registration/v1", *extra, "subject", "domain", "raw_registration", "shells", "regions")
    # The domain pins the entire raw registration. A null back-reference on
    # that exact record avoids a hash cycle. All other records bind the domain.
    expected_domain = None if reg_pin == d["raw_registration_digest"] else domain_pin
    require(reg["subject"] == subject and reg["domain"] == expected_domain, 4, "geometry_registration_drift")
    admission = raw_admission.inspect_raw(blob, intent_digest=d["intent"],
                                          registration_digest=reg["raw_registration"], read_checked=reader)
    if admission["source_rc"]:
        raise InputIssue(admission["source_rc"], admission["reason_codes"][0])
    doc = _document(blob)
    data = memoryview(blob)[28 + struct.unpack_from("<I", blob, 12)[0]:]
    intent = checked_record(reader, "raw-intent", d["intent"])
    shells = d["shells"]
    require(d["reference_length_um"]**2 <= sum(lo**2 for lo, _ in intent["extent_ranges_um"]),
            4, "reference_length_outside_authorized_extent")
    primitives = {}
    for key, triangles in raw_admission._primitives(doc, data, intent["max_faces"]):
        vertices = {}
        part = []
        for triangle in triangles:
            work.charge()
            for point in triangle:
                if point not in vertices:
                    vertices[point] = tuple(Fraction(x, raw_admission._LATTICE) for x in point)
            part.append(tuple(vertices[point] for point in triangle))
        mi, pi = map(int, key.split(":"))
        prim = doc["meshes"][mi]["primitives"][pi]
        _check_seams(doc, data, prim, welds, key, len(vertices), work)
        primitives[key] = part
    require(welds.keys() <= primitives.keys(), 4, "welded_primitive_drift")
    parts = _partition(primitives, shells, reg["shells"], intent["component_counts"], work)
    summaries = {key: _shell(part, shells[key], work) for key, part in parts.items()}
    _embedded([(_box(t), key, t) for key, part in parts.items() for t in part], work)
    _containment(parts, shells, work)
    regs = reg["regions"]
    require(type(regs) is dict and regs.keys() == d["regions_um"].keys(), 3, "protected_region_missing")
    patches, landmarks = {}, set()
    for name, selector in regs.items():
        require(type(selector) is list and len(selector) == 2, 2, "invalid_region_selector")
        key, ids = selector
        require(key in parts and type(ids) is list and ids and all(_int(i, 0) and i < len(parts[key])
                for i in ids) and len(set(ids)) == len(ids), 2, "invalid_region_faces")
        if name in {"face", "left_hand", "right_hand"}:
            selected = {(key, i) for i in ids}
            require(landmarks.isdisjoint(selected), 5, "overlapping_anatomical_regions")
            landmarks.update(selected)
        patches[name] = [parts[key][i] for i in ids]
    quad = None
    if quad_policy:
        cage = checked_record(reader, "geo-quad-cage", reg["quad_cage"])
        refs = checked_record(reader, "geo-quad-normals", reg["quad_normals"])
        closed(refs, "af.quad-normals/v1", "domain", "subject", "cage", "normals")
        require((refs["domain"], refs["subject"], refs["cage"]) == (domain_pin, subject, reg["quad_cage"]),
                4, "cage_reference_drift")
        quad = quad_metrics(cage, refs["normals"], [t for part in parts.values() for t in part], work)
        quad["cage_digest"] = reg["quad_cage"]
        require(domains.rational(quad["minimum_jacobian2"])*domains.PPM >= quad_policy["jacobian2_min_ppm"]
                and domains.rational(quad["minimum_split_cosine2"])*domains.PPM >= quad_policy["split_cos2_min_ppm"],
                5, "quad_conditioning")
    return parts, patches, summaries, quad, _feature_bounds(parts, d, reg, reader, work)


def _projection(tris, view, affine, work):
    camera = view["camera"]
    rotation, translation, (fx, fy, cx, cy) = domains._camera(camera)
    width, height = view["size"]
    cache = {}
    projected, mask = [], set()
    sx, sy, tx, ty = affine
    min_depth, radial2 = None, 0
    for t in tris:
        work.charge()
        for p in t:
            if p in cache:
                continue
            x, y, z = (dot(r, p)+v for r, v in zip(rotation, translation))
            require(view["near_um"] < z < view["far_um"], 3, "clipped_depth_domain")
            min_depth = z if min_depth is None else min(z, min_depth)
            radial2 = max(radial2, (x*x + y*y)/(z*z))
            u, v = sx*(fx*x/z + cx) + tx, sy*(fy*y/z + cy) + ty
            require(0 <= u <= width and 0 <= v <= height, 3, "clipped_raster_domain")
            cache[p] = (u, v, 0)
        tri = tuple(cache[p] for p in t)
        projected.append(tri)
        n = normal(tri)
        if not any(n):
            continue
        (xmin, xmax), (ymin, ymax), _ = _box(tri)
        xs = range(max(0, floor(xmin)), min(width, ceil(xmax)))
        ys = range(max(0, floor(ymin)), min(height, ceil(ymax)))
        work.charge((xs.stop-xs.start)*(ys.stop-ys.start))
        for y in ys:
            for x in xs:
                if min(halfspaces((Fraction(2*x+1, 2), Fraction(2*y+1, 2), 0), tri, n)) >= 0:
                    mask.add((x, y))
    return projected, mask, min_depth, radial2, max(sx*fx, sy*fy)


def _patch_boundary(triangles, work):
    # A registered anatomical patch is one connected manifold. Disconnected
    # landmarks need separate region IDs to preserve their correspondence.
    inventory = _shell(triangles, None, work)
    incidence = Counter(tuple(sorted((a, b))) for t in triangles for a, b in cycle(t))
    segments = [(a, b, b) for (a, b), count in incidence.items() if count == 1]
    return (inventory["genus"], inventory["boundary_loops"]), segments


def _raster_bounds(first, second, work):
    """Outward L-infinity Hausdorff bounds for unions of closed unit pixel cells."""
    require(first and second, 3, "empty_raster_support")
    maximum = 0
    for source, target in ((first, second), (second, first)):
        for x, y in source - target:
            work.charge(len(target))
            maximum = max(maximum, min(max(abs(x-u), abs(y-v)) for u, v in target))
    return max(0, 1000*maximum - 500), 1000*maximum


def _compare(a, b, domain, transforms, limit, report, work):
    parts_a, patches_a = a[:2]
    parts_b, patches_b = b[:2]
    distances = report["bounds_um"]
    report["chamfer2_upper_ppm_by_shell"] = {}
    for key in parts_a:
        means = []
        _distance(parts_a[key], parts_b[key], limit, domain, work, distances, "shell:" + key, mean=means)
        squared_ratio = Fraction(sum(means), 2*domain["reference_length_um"]**2)
        report["chamfer2_upper_ppm_by_shell"][key] = ceil(domains.PPM*squared_ratio)
    for region, tolerance in domain["regions_um"].items():
        _distance(patches_a[region], patches_b[region], tolerance, domain, work, distances, "region:" + region)
        shape_a, edges_a = _patch_boundary(patches_a[region], work)
        shape_b, edges_b = _patch_boundary(patches_b[region], work)
        require(shape_a == shape_b, 5, "protected_patch_topology:" + region)
        if edges_a:
            _distance(edges_a, edges_b, tolerance, domain, work, distances, "boundary:" + region)
    report["normal_cover_upper_um"] = {}
    for kind, source, target in (("shell", parts_a, parts_b), ("region", patches_a, patches_b)):
        for name in source:
            tolerance = limit if kind == "shell" else domain["regions_um"][name]
            for direction, aa, bb in (("forward", source, target), ("reverse", target, source)):
                bound = normal_cover(aa[name], bb[name], tolerance, domain["normal_cos2_min_ppm"],
                                     domain["refinements"], work)
                report["normal_cover_upper_um"][f"{kind}:{name}:{direction}"] = domains.pair(sqrt_bounds(bound)[1])
    first = [t for part in parts_a.values() for t in part]
    second = [t for part in parts_b.values() for t in part]
    for key, view in domain["views"].items():
        if key == "face":
            view_a, view_b = patches_a["face"], patches_b["face"]
            keys = ["region:face"]
        elif key == "hands":
            view_a = patches_a["left_hand"] + patches_a["right_hand"]
            view_b = patches_b["left_hand"] + patches_b["right_hand"]
            keys = ["region:left_hand", "region:right_hand"]
        else:
            view_a, view_b = first, second
            keys = ["shell:" + shell for shell in parts_a]
        pa, ma, za, ra, focal = _projection(view_a, view, transforms[key], work)
        pb, mb, zb, rb, _ = _projection(view_b, view, transforms[key], work)
        union, intersection = len(ma | mb), len(ma & mb)
        row = {"union": union, "intersection": intersection, "bounds_px": {}}
        report["views"][key] = row
        if union:
            iou = Fraction(intersection, union)
            row["iou_ppm"] = floor(domains.PPM*iou)
            require(iou*domains.PPM >= domain["iou_min_ppm"], 5, "silhouette:" + key)
            lo, hi = _raster_bounds(ma, mb, work)
            row["raster_support_millipx"] = [lo, hi]
            require(lo <= domain["raster_support_max_millipx"], 5, "raster_displacement:" + key)
            require(hi <= domain["raster_support_max_millipx"], 3, "raster_displacement_inconclusive:" + key)
        else:
            work.charge(len(pa) + len(pb))
            require(all(normal(t)[2] == 0 for tris in (pa, pb) for t in tris),
                    3, "empty_silhouette:" + key)
            row.update(iou_ppm=None, raster_support_millipx=None, raster_exemption="zero_projected_area")
        _distance(pa, pb, Fraction(domain["projected_max_millipx"], 1000),
                  domain, work, row["bounds_px"], "projection")
        # The frustum cone and depth slab are convex. The projection
        # Jacobian bound covers every segment joining matched surface points.
        epsilon = max(domains.rational(distances[name + ":" + direction][1])
                      for direction in ("forward", "reverse") for name in keys)
        screen2 = (1000 * focal * epsilon / min(za, zb))**2 * (1 + max(ra, rb))
        row["correspondence_upper_millipx"] = domains.pair(sqrt_bounds(screen2)[1])
        require(screen2 <= domain["projected_max_millipx"]**2, 3,
                "projection_displacement_inconclusive:" + key)


def _base_checks(report, d):
    checks = {}

    def add(name, interval, unit, scale, operator, limit, basis="CALIBRATED"):
        lower, upper = interval
        checks[name] = dict(lower=domains.pair(lower), upper=domains.pair(upper),
                            unit=unit, scale=scale, operator=operator, limit=limit, basis=basis)

    add("shells_and_registrations", (1, 1), "predicate", 1, "eq", 1, "DERIVED")
    for i, level in enumerate(report["levels"][1:]):
        add(f"level{i}.triangles", (level["faces"],)*2, "count", 1, "le", d["face_budgets"][i], "DERIVED")
        for j, feature in enumerate(sorted(level["features_um"])):
            bound = domains.rational(level["features_um"][feature])
            add(f"level{i}.feature{j}", (0, bound), "um", 1, "le", d["features"]["limits_um"][feature])
        if level["quad_cage"] is not None:
            policy = d["quad_levels"][str(i+1)]
            for key, limit in (("minimum_jacobian2", "jacobian2_min_ppm"),
                               ("minimum_split_cosine2", "split_cos2_min_ppm")):
                value = domains.rational(level["quad_cage"][key])
                add(f"level{i}.quad.{key}", (value, value), "ppm", domains.PPM, "ge", policy[limit])
        for j, key in enumerate(sorted(level["shells"])):
            shell = level["shells"][key]
            prefix = f"level{i}.shell{j}."
            add(prefix+"quality", (domains.rational(shell["min_quality_squared"]),)*2,
                "ppm", domains.PPM, "ge", d["min_q2_ppm"])
            add(prefix+"aspect2", (domains.rational(shell["max_aspect_squared"]),)*2,
                "ratio_squared", 1, "le", d["max_aspect2"])
    for p in report["pairs"]:
        prefix = f"pair{p['from']}.{p['to']}."
        def distance_metric(name, bounds, region=None):
            low, high = map(domains.rational, bounds)
            if region is not None:
                add(name, (low, high), "um", 1, "le", d["regions_um"][region])
            elif p["from"]:
                add(name, (low, high), "um", 1, "le", d["switch_max_um"])
            else:
                length = d["reference_length_um"]
                add(name, (low/length, high/length), "ppm", domains.PPM, "le", d["distance_max_ppm"])

        for i, key in enumerate(sorted(d["shells"])):
            for direction in ("forward", "reverse"):
                name = prefix + f"shell{i}." + direction
                distance_metric(name, p["bounds_um"][f"shell:{key}:{direction}"])
                normal_bound = p["normal_cover_upper_um"]["shell:" + key + ":" + direction]
                distance_metric(name + ".oriented", [[0, 1], normal_bound])
        for i, key in enumerate(sorted(d["regions_um"])):
            for direction in ("forward", "reverse"):
                distance_metric(prefix + f"region{i}." + direction,
                                p["bounds_um"][f"region:{key}:{direction}"], key)
                boundary = p["bounds_um"].get(f"boundary:{key}:{direction}")
                if boundary is not None:
                    distance_metric(prefix + f"region{i}.boundary." + direction, boundary, key)
                normal_bound = p["normal_cover_upper_um"][f"region:{key}:{direction}"]
                distance_metric(prefix + f"region{i}.oriented." + direction, [[0, 1], normal_bound], key)
        for key, view in p["views"].items():
            name = prefix + key
            if view.get("raster_exemption") == "zero_projected_area":
                add(name+".zero_projected_area", (1, 1), "predicate", 1, "eq", 1, "DERIVED")
            else:
                iou = Fraction(view["intersection"], view["union"])
                add(name+".raster_support", view["raster_support_millipx"], "millipx", 1, "le",
                    d["raster_support_max_millipx"])
                add(name+".iou", (iou, iou), "ppm", domains.PPM, "ge", d["iou_min_ppm"])
            for direction in ("forward", "reverse"):
                bounds = map(domains.rational, view["bounds_px"]["projection:"+direction])
                add(name+"."+direction, bounds, "millipx", 1000, "le", d["projected_max_millipx"])
            add(name+".correspondence", (0, domains.rational(view["correspondence_upper_millipx"])),
                "millipx", 1, "le", d["projected_max_millipx"])
    return checks


def _base_scope(report, domain):
    from tools.input import evidence
    from tools.mesh import worker

    modules = [domains, raw_admission, evidence, worker, contracts, math_acceptance, core]
    dependencies = {m.__name__: core.sha256_file(Path(m.__file__)) for m in modules}
    evaluator = "sha256:" + core.sha256_file(Path(__file__))
    return dict(gate_id="geo.preservation", operation="mesh_preservation", role="character",
                policy_digest=report["domain_digest"], domain_digest=report["domain_digest"],
                input_refs=list(dict.fromkeys(report["subjects"][:-1])),
                reference_refs=[domain["intent"], domain["selection"], domain["pose"]],
                evaluator_digest=evaluator, toolchain_digest=math_acceptance.canonical_digest({
                    "evaluator": evaluator, "modules": dependencies, "python": sys.version}))


def _qualify(report, profile_digest, stage_digest, reader):
    profile = checked_record(reader, "profile", profile_digest)
    require(all(profile.get(k) == v for k, v in report["base_scope"].items()), 4, "geo_profile_scope_drift")
    stage = checked_record(reader, "stage", stage_digest)
    checks = report["checks"]
    for rows in (profile["criteria"], stage["observations"]):
        require(type(rows) is list and len(rows) == len(checks)
                and {r["id"] for r in rows} == checks.keys(), 3, "geo_check_coverage")
    require(stage["work_counts"].get("geometry") == report["work"]
            and type(stage["work_counts"]["geometry"]) is int, 4, "geometry_work_count_drift")
    for rule in profile["criteria"]:
        expected = checks[rule["id"]]
        require(all(canonical_bytes(rule[k]) == canonical_bytes(expected[k])
                    for k in ("unit", "scale", "operator", "limit"))
                and rule["basis"] in (expected["basis"], "TARGET")
                and rule["uncertainty_kind"] in ("exact", "bounded")
                and rule["method_digest"] == report["base_scope"]["evaluator_digest"], 4, "geo_criterion_drift")
    for obs in stage["observations"]:
        expected = checks[obs["id"]]
        require(all(canonical_bytes(obs[k]) == canonical_bytes(expected[k]) for k in ("lower", "upper"))
                and obs["confidence_ppm"] is None, 4, "geo_observation_drift")
        cert = checked_record(reader, "certificate", obs["certificate_ref"])
        require(cert["witness_digest"] == report["native_digest"], 4, "geo_certificate_witness_drift")
    return math_acceptance.evaluate_gate(profile_digest=profile_digest, subject_digest=report["subjects"][-1],
                                         receipt_digest=stage_digest, read_checked=reader)


def certify(blobs, *, domain_digest, registration_digests, read_checked,
            profile_digest=None, stage_digest=None):
    """Measure raw, master, then consecutive LODs under independently pinned intent.

    Return an integer-only native certificate. INT/CALIB own the production BASE
    envelope, checked reader, threshold qualification and registry publication.
    """
    report = {"schema": "af.mesh-regression/v2", "domain_digest": domain_digest,
              "subjects": [], "registration_digests": registration_digests,
              "levels": [], "pairs": [], "reason_codes": [], "work": 0}
    rc, work = 0, None
    try:
        require(callable(read_checked), 2, "invalid_reader")
        source_reader = read_checked
        cache = {}

        def read_checked(role, pin):
            if (role, pin) not in cache:
                try:
                    cache[role, pin] = source_reader(role, pin)
                except InputIssue:
                    raise
                except Exception as exc:
                    raise InputIssue(3, "geometry_reader_unavailable:" + role) from exc
            return cache[role, pin]

        domain = checked_record(read_checked, "geo-domain", domain_digest)
        _validate_domain(domain)
        transforms = _registered_views(domain, read_checked)
        require(type(registration_digests) is list and all(is_digest(pin) for pin in registration_digests),
                2, "invalid_geometry_registrations")
        require(isinstance(blobs, (list, tuple)) and all(type(b) is bytes for b in blobs),
                2, "invalid_glb_bytes")
        require(len(blobs) == len(domain["face_budgets"]) + 1
                and len(registration_digests) == len(blobs), 3, "geometry_level_coverage")
        subjects = report["subjects"] = [bytes_digest(b) for b in blobs]
        require(subjects[0] == domain["source"], 4, "raw_geometry_drift")
        require(registration_digests[0] == domain["raw_registration_digest"], 4,
                "raw_geometry_registration_drift")
        work = Work(domain["max_work"])
        raw = previous = None
        for i, (blob, reg) in enumerate(zip(blobs, registration_digests)):
            mesh = _geometry(blob, reg, domain, domain_digest, read_checked, work, i)
            count = sum(len(part) for part in mesh[0].values())
            report["levels"].append({"faces": count, "bytes": len(blob), "shells": mesh[2], "quad_cage": mesh[3],
                                     "features_um": mesh[4]})
            if i == 0:
                raw = mesh
            else:
                for shell in mesh[2].values():
                    require(domains.rational(shell["min_quality_squared"])*domains.PPM >= domain["min_q2_ppm"]
                            and domains.rational(shell["max_aspect_squared"]) <= domain["max_aspect2"],
                            5, "triangle_conditioning")
                require(count <= domain["face_budgets"][i-1], 5, "lod_face_budget")
                if i > 1:
                    require(count < report["levels"][i-1]["faces"], 5, "lod_reduction")
                limit = Fraction(domain["reference_length_um"] * domain["distance_max_ppm"], domains.PPM)
                pairs = [(0, raw, limit)]
                if i > 1:
                    pairs.append((i-1, previous, domain["switch_max_um"]))
                for j, source, bound in pairs:
                    entry = {"from": j, "to": i, "bounds_um": {}, "views": {}}
                    report["pairs"].append(entry)
                    _compare(source, mesh, domain, transforms, bound, entry, work)
            previous = mesh
        report["checks"] = _base_checks(report, domain)
        report["base_scope"] = _base_scope(report, domain)
        report["work"] = work.used
        report["measurement_rc"] = 0
        report["native_digest"] = math_acceptance.canonical_digest(report)
        if profile_digest is None or stage_digest is None:
            raise InputIssue(3, "base_qualification_missing")
        qualified = _qualify(report, profile_digest, stage_digest, read_checked)
        rc = qualified["rc"]
        report.update(base=qualified, reason_codes=qualified["reasons"])
    except (InputIssue, contracts.MeshError) as exc:
        rc = exc.rc
        report["reason_codes"].append(exc.code)
    except MemoryError:
        rc = 6
        report["reason_codes"].append("geometry_memory_budget")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, struct.error,
            core.ContractError, RecursionError) as exc:
        rc = 2
        report["reason_codes"].append(str(exc) or "invalid_geometry_input")
    report.setdefault("measurement_rc", rc)
    report.update(work=work.used if work else 0, rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc])
    return report


def main(argv=None):
    """Read-only boundary. The caller controls the authority snapshot and domain pin.

    Files beneath the authority root use ROLE/FULL_HEX and contain exactly the
    same bytes the programmatic role-aware BASE/INPUT reader would return.
    """
    import argparse
    from tools.blender.topology import certify_chain
    from tools.mesh.api import _regular_stream

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--level", type=Path, action="append", required=True, help="master first, then each LOD")
    parser.add_argument("--authority", type=Path, required=True, help="independently authorized read-only snapshot")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--registration", action="append", required=True, help="raw first, then each level")
    parser.add_argument("--profile")
    parser.add_argument("--stage")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    def reader(role, pin):
        if not is_digest(pin) or not role.replace("-", "").isalpha():
            return None
        try:
            with _regular_stream(args.authority / role / pin[7:]) as stream:
                data = stream.read(record_limit(role) + 1)
        except FileNotFoundError:
            return None
        require(len(data) <= record_limit(role), 6, "authority_record_budget")
        return data

    result = certify_chain(args.raw, args.level, domain_digest=args.domain,
                           registration_digests=args.registration, read_checked=reader,
                           profile_digest=args.profile, stage_digest=args.stage)
    print(core.canonical_json(result), end="")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
