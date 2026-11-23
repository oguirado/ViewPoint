"""Exact scalar and polyhedral predicates for the protected skin fixture."""
from collections import Counter, defaultdict
from fractions import Fraction
from itertools import combinations, product
from math import isqrt

from providers.unirig.common import require
from tools.mesh.certificate import (
    _edge_contacts, cross, dot, norm2, segment_d2, sub, triangle_d2,
)


def floor_sqrt(value):
    value = Fraction(value)
    return isqrt(value.numerator // value.denominator)


def ceil_sqrt(value):
    lower = floor_sqrt(value)
    return lower + (lower * lower < value)


def pack_weights(weights, slots, denominator):
    """Select by weight then joint ID and assign exact largest-remainder quotas."""
    kept = sorted(weights, key=lambda joint: (-weights[joint], joint))[:slots]
    retained = sum(weights[joint] for joint in kept)
    require(retained > 0, 5, "weight_invalid")
    exact = {j: Fraction(weights[j] * denominator, retained) for j in kept}
    quotas = {j: int(exact[j]) for j in kept}
    order = sorted(kept, key=lambda j: (quotas[j] - exact[j], j))
    for joint in order[:denominator - sum(quotas.values())]:
        quotas[joint] += 1
    return quotas, 1 - retained, sum(abs(Fraction(quotas[j], denominator)
                                          - exact[j] / denominator) for j in kept)


def displacement_squared(palette_points, removed, quantization):
    """Squared LBS displacement bound from the exact transformed-point diameter."""
    diameter = max((norm2(sub(a, b)) for a, b in combinations(palette_points, 2)), default=0)
    return diameter * (removed + Fraction(quantization, 2)) ** 2


def det(columns):
    a, b, c = columns
    return dot(a, cross(b, c))


def is_rigid_rotation(matrix):
    """Test the exact world-space linear block for a proper rotation."""
    columns = tuple(matrix[i:i+3] for i in (0, 4, 8))
    return (all(dot(a, b) == int(i == j)
                for i, a in enumerate(columns) for j, b in enumerate(columns))
            and det(columns) == 1)


def pivot_displacement_upper_um(rotation, offset_squared_um):
    """Bound the translation induced by moving a proper rotation's pivot."""
    # The squared operator norm of R - I is 3 - trace(R).
    chord_squared = 3 - sum(rotation[i] for i in (0, 5, 10))
    return ceil_sqrt(chord_squared * offset_squared_um)


def edges(points):
    return tuple(sub(p, points[0]) for p in points[1:])


def faces(tet):
    a, b, c, d = tet
    return ((b, c, d), (a, d, c), (a, b, d), (a, c, b))


def cycle_face(face):
    return min(face[i:] + face[:i] for i in range(3))


def boundary_faces(tetrahedra):
    incidence = defaultdict(list)
    for tet in tetrahedra:
        for face in faces(tet):
            incidence[tuple(sorted(face))].append(cycle_face(face))
    boundary = []
    for rows in incidence.values():
        if len(rows) == 1:
            boundary.append(rows[0])
        else:
            require(len(rows) == 2 and rows[0] == cycle_face(rows[1][::-1]),
                    5, "skin_invalid_tetrahedral_incidence")
    return boundary


def _box(points):
    return tuple((min(axis), max(axis)) for axis in zip(*points))


def _pairs(rows, work, *, open_interval):
    active = []
    for box, ids, coords in sorted((_box(coords), ids, coords) for ids, coords in rows):
        work(len(active) + 1)
        active = [entry for entry in active if entry[0][0][1] >= box[0][0]]
        for other_box, other_ids, other in active:
            if all(max(a, c) < min(b, d) if open_interval else max(a, c) <= min(b, d)
                   for (a, b), (c, d) in zip(box, other_box)):
                yield ids, coords, other_ids, other
        active.append((box, ids, coords))


def _tet_overlaps(a, b):
    # For full-dimensional tetrahedra, face normals and edge-cross-edge
    # normals suffice. Equality of projected endpoints is boundary contact.
    axes = [cross(*edges(face)) for tet in (a, b) for face in faces(tet)]
    a_edges, b_edges = (tuple(sub(q, p) for p, q in combinations(tet, 2)) for tet in (a, b))
    axes.extend(cross(u, v) for u, v in product(a_edges, b_edges))
    for axis in axes:
        if any(axis):
            ap, bp = ([dot(axis, p) for p in tet] for tet in (a, b))
            if min(max(ap), max(bp)) <= max(min(ap), min(bp)):
                return False
    return True


def check_intersections(tetrahedra, boundary, positions, work):
    """Reject overlapping tet interiors and unauthorized boundary contacts."""
    tets = [(ids, tuple(positions[i] for i in ids)) for ids in tetrahedra]
    for _, a, _, b in _pairs(tets, work, open_interval=True):
        require(not _tet_overlaps(a, b), 5, "skin_tet_intersection")
    triangles = [(ids, tuple(positions[i] for i in ids)) for ids in boundary]
    for ia, a, ib, b in _pairs(triangles, work, open_interval=False):
        shared = tuple(positions[i] for i in sorted(set(ia).intersection(ib)))
        for src, dst in ((a, b), (b, a)):
            for i in range(3):
                for p in _edge_contacts(src[i], src[i-1], dst):
                    require(p in shared or (len(shared) == 2 and segment_d2(p, *shared) == 0),
                            5, "skin_boundary_intersection")


def _segment_interval(start, end, tet):
    lo, hi = Fraction(0), Fraction(1)
    for face in faces(tet):
        normal = cross(*edges(face))
        x, y = (dot(normal, sub(p, face[0])) for p in (start, end))
        if min(x, y) > 0:
            return None
        if x > 0:
            lo = max(lo, Fraction(x, x-y))
        if y > 0:
            hi = min(hi, Fraction(x, x-y))
    return (lo, hi) if lo <= hi else None


def bone_clearance(start, end, tetrahedra, surface, required, work):
    """Cover the whole segment, then bound its distance to the surface."""
    require(start != end, 5, "skin_zero_length_bone")
    work(len(tetrahedra))
    intervals = sorted(interval for tet in tetrahedra
                       if (interval := _segment_interval(start, end, tet)) is not None)
    covered = 0
    for lo, hi in intervals:
        if lo > covered:
            break
        covered = max(covered, hi)
    require(covered == 1, 5, "skin_bone_outside")
    if required == 0:
        return 0
    pending, minimum = [(start, end)], None
    while pending:
        a, b = pending.pop()
        work(len(surface))
        middle = tuple(Fraction(x+y, 2) for x, y in zip(a, b))
        distance2 = min(triangle_d2(middle, face) for face in surface)
        require(distance2 >= required**2, 5, "skin_bone_clearance")
        lower = floor_sqrt(distance2) - ceil_sqrt(Fraction(norm2(sub(b, a)), 4))
        if lower >= required:
            minimum = lower if minimum is None else min(minimum, lower)
        else:
            pending.extend(((a, middle), (middle, b)))
    return minimum


def _psd(matrix):
    return (all(matrix[i][i] >= 0 for i in range(3))
            and all(matrix[i][i]*matrix[j][j] >= matrix[i][j]**2
                    for i, j in combinations(range(3), 2))
            and det(matrix) >= 0)


def pose_volume(tetrahedra, rest, posed, stretch_min, stretch_max):
    """Compare exact Jacobians, singular limits and per-part volume changes."""
    rest_volume, pose_volumes = Counter(), Counter()
    minimum = None
    for part, ids in tetrahedra:
        r, p = (edges(tuple(points[i] for i in ids)) for points in (rest, posed))
        old, new = det(r), det(p)
        require(old > 0, 5, "skin_rest_tet_invalid")
        require(new > 0, 5, "skin_tet_inverted_or_collapsed")
        ratio = Fraction(new, old)
        minimum = ratio if minimum is None else min(minimum, ratio)
        lower = tuple(tuple(dot(u, v) - stretch_min**2*dot(x, y)
                            for v, y in zip(p, r)) for u, x in zip(p, r))
        upper = tuple(tuple(stretch_max**2*dot(x, y) - dot(u, v)
                            for v, y in zip(p, r)) for u, x in zip(p, r))
        require(_psd(lower) and _psd(upper), 5, "skin_tet_stretch")
        rest_volume[part] += old
        pose_volumes[part] += new
    changes = [Fraction(pose_volumes[part] - v, v) for part, v in rest_volume.items()]
    return minimum, max(0, -min(changes)), max(0, max(changes))
