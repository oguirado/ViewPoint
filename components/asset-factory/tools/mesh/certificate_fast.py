"""Batched conservative filters for the exact GEO triangle-distance oracle.

The floating kernel proposes a traversal and a shortlist. Python evaluates
every surviving extremum exactly. A cell whose legacy traversal cannot be
proved (including its work charge) is replayed by the original evaluator.
"""
import ctypes
from fractions import Fraction
from math import isfinite, isqrt
import os

import numpy as np
from numba import get_num_threads, njit, prange, set_num_threads

from tools.input.evidence import InputIssue
from tools.mesh.certificate import triangle_d2


# Conservative normal-range envelope for all products used by the witness
# search. Exceptional floating candidates can always use the original path.
_COORD_LIMIT = 2.0**100
_EPS = 2.0**-52
_UNIT_PAD = 128 * _EPS
_TINY = 2.0**-450
_MAX_CANDIDATES = 48
CHUNK_UNWRITTEN = -1


@njit
def _dot(x, y):
    return x[0]*y[0] + x[1]*y[1] + x[2]*y[2]


@njit
def _cross(x, y):
    return (x[1]*y[2] - x[2]*y[1],
            x[2]*y[0] - x[0]*y[2],
            x[0]*y[1] - x[1]*y[0])


@njit
def _difference(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


@njit
def _clamp(value):
    return max(0.0, min(1.0, value))


@njit
def _segment_witness(a, b):
    """Endpoint vectors relative to the query. Return a convex witness."""
    edge = _difference(b, a)
    length2 = _dot(edge, edge)
    t = _clamp(-_dot(a, edge) / length2) if length2 > _TINY else 0.0
    return (a[0]+t*edge[0], a[1]+t*edge[1], a[2]+t*edge[2])


@njit
def _triangle_interval(point, triangle, pad, exact_coordinates):
    # The closest-point arithmetic below only chooses a feasible witness.
    # Even a mistaken inside/outside sign cannot shrink the enclosure.
    a = _difference(triangle[0], point)
    b = _difference(triangle[1], point)
    c = _difference(triangle[2], point)
    if exact_coordinates and ((0., 0., 0.) in (a, b, c)):
        return 0.0, 0.0
    ab, ac = _difference(b, a), _difference(c, a)
    n = _cross(ab, ac)
    n2 = _dot(n, n)
    s, v = -1.0, -1.0
    if n2 > _TINY:
        s = _dot(_cross(c, a), n) / n2
        v = _dot(_cross(a, b), n) / n2
    if 0 <= s <= 1 and 0 <= v and s+v <= 1:
        t = _clamp(v / (1-s)) if s < 1 else 0.0
        weight = (1-s)*t
        w = (a[0]+s*ab[0]+weight*ac[0], a[1]+s*ab[1]+weight*ac[1],
             a[2]+s*ab[2]+weight*ac[2])
    else:
        w = _segment_witness(a, b)
        other = _segment_witness(a, c)
        if _dot(other, other) < _dot(w, w):
            w = other
        other = _segment_witness(b, c)
        if _dot(other, other) < _dot(w, w):
            w = other
    length = np.sqrt(_dot(w, w))
    upper = length + pad
    if length <= pad:
        return 0.0, np.nextafter(upper*upper, np.inf)
    # The support plane of the target triangle in direction w gives an
    # independent lower bound. Use w / |w|, including its rounding error.
    unit = (w[0]/length, w[1]/length, w[2]/length)
    lower = max(0.0, min(_dot(unit, a), _dot(unit, b), _dot(unit, c)) - pad)
    return (max(0.0, np.nextafter(lower*lower, -np.inf)),
            np.nextafter(upper*upper, np.inf))


@njit
def _gap(point, box, axis):
    lo, hi = box[axis, 0], box[axis, 1]
    coordinate = point[axis]
    if coordinate < lo:
        return lo, coordinate
    if coordinate > hi:
        return coordinate, hi
    return 0.0, 0.0


@njit
def _box_interval(cell, box, pad, exact_coordinates):
    lower = np.empty(3)
    upper = np.empty(3)
    for corner in range(3):
        g0 = _gap(cell[corner], box, 0)
        g1 = _gap(cell[corner], box, 1)
        g2 = _gap(cell[corner], box, 2)
        v = (g0[0]-g0[1], g1[0]-g1[1], g2[0]-g2[1])
        distance = _dot(v, v)
        if exact_coordinates:
            # No input conversion error. The squared norm has one difference,
            # one square and at most two additions along each term's path.
            lower[corner] = max(0.0, distance * (1 - 16*_EPS))
            upper[corner] = distance * (1 + 16*_EPS)
            # Underflow is impossible for nonzero inputs in normal GEO ranges;
            # explicitly widen at the subnormal boundary nevertheless.
            if 0.0 < distance < _TINY or (distance == 0 and v != (0., 0., 0.)):
                lower[corner], upper[corner] = 0.0, _TINY
        else:
            radius = np.sqrt(distance)
            lo, hi = max(0.0, radius-pad), radius+pad
            lower[corner] = max(0.0, np.nextafter(lo*lo, -np.inf))
            upper[corner] = np.nextafter(hi*hi, np.inf)
    return lower, upper


@njit
def _gap_pair(a, b):
    # Error-free difference for finite normal range (Knuth TwoDiff).
    d = a-b
    bv = a-d
    av = d+bv
    return d, (a-av)+(bv-b)


@njit
def _same_gap(a, point_a, b, point_b):
    for axis in range(3):
        pa = _gap(point_a, a, axis)
        pb = _gap(point_b, b, axis)
        if pa != pb and _gap_pair(*pa) != _gap_pair(*pb):
            return False
    return True


@njit
def _box_dominates(cell, a, b, alo, bhi):
    # Prove that max(a) >= max(b), including exact ties on a shared span.
    for corner_b in range(3):
        dominated = False
        for corner_a in range(3):
            if alo[corner_a] >= bhi[corner_b] or _same_gap(a, cell[corner_a], b, cell[corner_b]):
                dominated = True
                break
        if not dominated:
            return False
    return True


@njit
def _box_witness(point, box, triangle):
    # If the exact projection onto the box is a triangle vertex, that
    # vertex witnesses an upper bound equal to the box's lower bound.
    x = min(max(point[0], box[0, 0]), box[0, 1])
    y = min(max(point[1], box[1, 0]), box[1, 1])
    z = min(max(point[2], box[2, 0]), box[2, 1])
    for vertex in triangle:
        if vertex[0] == x and vertex[1] == y and vertex[2] == z:
            return True
    return False


@njit
def _ties_allow_prune(cell, box, triangles, candidates, size, lo, nearest_hi, best_hi, pad):
    has_nearest = np.empty(3, np.bool_)
    for corner in range(3):
        has_nearest[corner] = lo[corner] >= nearest_hi[corner]
    upper_threshold = max(lo)
    has_upper = upper_threshold >= best_hi
    for i in range(size):
        triangle = triangles[int(candidates[i, 0])]
        whole = True
        for corner in range(3):
            vertex = _box_witness(cell[corner], box, triangle)
            has_nearest[corner] |= vertex
            if not has_upper and not vertex:
                _, upper = _triangle_interval(cell[corner], triangle, pad, True)
                whole &= upper <= upper_threshold
        has_upper |= whole
        if has_upper and has_nearest[0] and has_nearest[1] and has_nearest[2]:
            return True
    return False


@njit
def _shortlist(cell, boxes, branches, triangles, pad, exact_coordinates, winners):
    nearest_lo = np.full(3, np.inf)
    nearest_hi = np.full(3, np.inf)
    best_lo, best_hi = np.inf, np.inf
    node_stack = np.empty(64, np.int64)
    low_stack = np.empty((64, 3))
    high_stack = np.empty((64, 3))
    node_stack[0] = 0
    low_stack[0], high_stack[0] = _box_interval(cell, boxes[0], pad, exact_coordinates)
    # Every tree node fits the balanced int64 index depth.
    count = 1
    work = 3
    candidates = np.empty((_MAX_CANDIDATES, 5))
    size = 0
    while count:
        count -= 1
        node = node_stack[count]
        lo, hi = low_stack[count], high_stack[count]
        if max(lo) >= best_hi and (lo[0] >= nearest_hi[0] and lo[1] >= nearest_hi[1]
                                   and lo[2] >= nearest_hi[2]):
            continue
        if max(hi) >= best_lo and (hi[0] >= nearest_lo[0] and hi[1] >= nearest_lo[1]
                                  and hi[2] >= nearest_lo[2]):
            if exact_coordinates and _ties_allow_prune(
                    cell, boxes[node], triangles, candidates, size, lo, nearest_hi, best_hi, pad):
                continue
            return 0, -1  # The exact legacy work depends on this comparison.

        left, right = branches[node]
        if left >= 0:
            alo, ahi = _box_interval(cell, boxes[left], pad, exact_coordinates)
            blo, bhi = _box_interval(cell, boxes[right], pad, exact_coordinates)
            if max(alo) > max(bhi):
                first, second = left, right
                firstlo, firsthi, secondlo, secondhi = alo, ahi, blo, bhi
            elif max(ahi) < max(blo):
                first, second = right, left
                firstlo, firsthi, secondlo, secondhi = blo, bhi, alo, ahi
            elif max(alo) >= max(bhi) or (
                    exact_coordinates and _box_dominates(cell, boxes[left], boxes[right], alo, bhi)):
                # Python's stable descending sort appends right last on ties.
                first, second = left, right
                firstlo, firsthi, secondlo, secondhi = alo, ahi, blo, bhi
            else:
                return 0, -2
            node_stack[count], node_stack[count+1] = first, second
            low_stack[count], low_stack[count+1] = firstlo, secondlo
            high_stack[count], high_stack[count+1] = firsthi, secondhi
            count += 2
            work += 6
            continue

        start, stop = -left-1, right
        for face in range(start, stop):
            work += 3
            lower = np.empty(3)
            upper = np.empty(3)
            for corner in range(3):
                lower[corner], upper[corner] = _triangle_interval(
                    cell[corner], triangles[face], pad, exact_coordinates)
                nearest_lo[corner] = min(nearest_lo[corner], lower[corner])
                nearest_hi[corner] = min(nearest_hi[corner], upper[corner])
            high_lower = max(lower)
            best_lo = min(best_lo, high_lower)
            best_hi = min(best_hi, max(upper))
            if (high_lower <= best_hi or lower[0] <= nearest_hi[0]
                    or lower[1] <= nearest_hi[1] or lower[2] <= nearest_hi[2]):
                if size == len(candidates):
                    # Compact with the tighter bounds discovered since the last leaf.
                    kept = 0
                    for i in range(size):
                        row = candidates[i]
                        if (row[4] <= best_hi or row[1] <= nearest_hi[0]
                                or row[2] <= nearest_hi[1] or row[3] <= nearest_hi[2]):
                            candidates[kept] = row
                            kept += 1
                    size = kept
                    if size == len(candidates):
                        return 0, -3
                candidates[size, 0] = face
                candidates[size, 1:4] = lower
                candidates[size, 4] = high_lower
                size += 1
    used = 0
    for i in range(size):
        face, a, b, c, whole = candidates[i]
        mask = (int(a <= nearest_hi[0]) + 2*int(b <= nearest_hi[1])
                + 4*int(c <= nearest_hi[2]) + 8*int(whole <= best_hi))
        if mask:
            winners[used] = int(face)*16 + mask
            used += 1
    return used, work


# Call Linux on the worker itself. Python's affinity functions target the
# calling thread, so calling them only from evaluate cannot pin a warm pool.
_libc = ctypes.CDLL(None)
_sched_getaffinity = _libc.sched_getaffinity
_sched_setaffinity = _libc.sched_setaffinity
for _affinity_call in (_sched_getaffinity, _sched_setaffinity):
    _affinity_call.argtypes = (ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p)
    _affinity_call.restype = ctypes.c_int


@njit
def _pinned_chunk(start, stop, cells, boxes, branches, triangles, pad, exact,
                  affinity, counts, work, choices):
    previous = np.zeros_like(affinity)
    if _sched_getaffinity(0, previous.nbytes, previous.ctypes) != 0:
        return 1
    changed = not np.array_equal(previous, affinity)
    if changed and _sched_setaffinity(0, affinity.nbytes, affinity.ctypes) != 0:
        return 1
    restored = True
    try:
        for i in range(start, stop):
            counts[i], work[i] = _shortlist(
                cells[i], boxes, branches, triangles, pad, exact, choices[i])
    finally:
        if changed:
            restored = _sched_setaffinity(0, previous.nbytes, previous.ctypes) == 0
    return 0 if restored else 2


@njit(parallel=True)
def _batch(cells, boxes, branches, triangles, scale, exact, affinity, threads):
    counts = np.full(len(cells), CHUNK_UNWRITTEN, np.int64)
    work = np.zeros(len(cells), np.int64)
    choices = np.empty((len(cells), _MAX_CANDIDATES), np.int64)
    placement = np.full(threads, CHUNK_UNWRITTEN, np.int8)
    pad = np.nextafter(scale * _UNIT_PAD, np.inf)
    for chunk in prange(threads):
        start = len(cells) * chunk // threads
        stop = len(cells) * (chunk + 1) // threads
        placement[chunk] = _pinned_chunk(start, stop, cells, boxes, branches, triangles,
                                         pad, exact, affinity, counts, work, choices)
    return counts, work, choices, placement


# Float32 GLB positions decoded in physical micrometres normally fit this
# dyadic lattice. The exact Fraction oracle handles other coordinate domains.
_GRID_BITS = 40
_GRID_SQUARED = 1 << (2 * _GRID_BITS)


def _grid(point):
    values = []
    for coordinate in point:
        numerator, denominator = coordinate.as_integer_ratio()
        if denominator & (denominator-1) or denominator.bit_length() > _GRID_BITS+1:
            return None
        values.append(numerator << (_GRID_BITS+1 - denominator.bit_length()))
    return tuple(values)


def _integer_dot(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]


def _integer_sub(a, b):
    return a[0]-b[0], a[1]-b[1], a[2]-b[2]


def _integer_cross(a, b):
    return (a[1]*b[2] - a[2]*b[1],
            a[2]*b[0] - a[0]*b[2],
            a[0]*b[1] - a[1]*b[0])


def _face_terms(positions):
    a, b, c = positions
    edges = _integer_sub(b, a), _integer_sub(c, b), _integer_sub(a, c)
    normal = _integer_cross(edges[0], edges[1])
    normal2 = _integer_dot(normal, normal)
    inside_axes = tuple(_integer_cross(normal, e) for e in edges)
    lengths = tuple(_integer_dot(e, e) for e in edges)
    return positions, edges, normal, normal2, inside_axes, lengths


def _grid_triangle_d2(point, face_terms):
    vertices, edges, normal, normal2, axes, lengths = face_terms
    offsets = tuple(_integer_sub(point, v) for v in vertices)
    if normal2 and all(_integer_dot(p, axis) >= 0 for p, axis in zip(offsets, axes)):
        return Fraction(_integer_dot(offsets[0], normal)**2, normal2 * _GRID_SQUARED)
    best_numerator, best_denominator = None, 1
    for i, (p, edge, length) in enumerate(zip(offsets, edges, lengths)):
        along = _integer_dot(p, edge)
        if along <= 0:
            numerator, denominator = _integer_dot(p, p), 1
        elif along >= length:
            q = offsets[(i+1) % 3]
            numerator, denominator = _integer_dot(q, q), 1
        else:
            numerator, denominator = _integer_dot(p, p)*length - along**2, length
        if best_numerator is None or numerator*best_denominator < best_numerator*denominator:
            best_numerator, best_denominator = numerator, denominator
    return Fraction(best_numerator, best_denominator * _GRID_SQUARED)


class Batch:
    """Flatten the original BVH without changing its partitions or leaf order."""

    def __init__(self, root):
        self.faces = []
        self._float_points = {}
        self._exact = True
        boxes, branches = [], []

        def flatten(node):
            ordinal = len(boxes)
            boxes.append(node.box)
            branches.append(None)
            if node.children:
                branches[ordinal] = tuple(flatten(child) for child in node.children)
            else:
                first = len(self.faces)
                self.faces.extend(node.triangles)
                branches[ordinal] = (-first-1, len(self.faces))
            return ordinal

        flatten(root)
        self._boxes = boxes
        self._branches = branches
        self.max_cell_work = 3*(len(boxes) + len(self.faces))
        self.branches = np.asarray(branches, dtype=np.int64)
        self.triangles = np.empty((len(self.faces), 3, 3))
        for i, triangle in enumerate(self.faces):
            if len(triangle) != 3:
                raise ValueError("compiled distance requires triangle targets")
            for j, point in enumerate(triangle):
                self.triangles[i, j] = self._float_point(point)
        self._float_points.clear()
        self.boxes = np.asarray(boxes, dtype=np.float64)
        self.scale = max(1., np.max(np.abs(self.boxes)))
        if not isfinite(self.scale) or self.scale > _COORD_LIMIT:
            raise OverflowError("coordinate range outside compiled filter")
        self._cache = {}
        self._grid_points = {}
        self._grid_faces = {}
        self._grid_boxes = None

    def _float_point(self, point):
        key = id(point)
        cached = self._float_points.get(key)
        if cached is not None and cached[0] is point:
            return cached[1]
        values = tuple(map(float, point))
        self._exact &= all(Fraction(v) == x for v, x in zip(values, point))
        self._float_points[key] = (point, values)
        return values

    def evaluate(self, cells, *, workers=None):
        if not cells or any(len(cell) != 3 for cell in cells):
            return [None] * len(cells)
        old_exact = self._exact
        coordinates = np.empty((len(cells), 3, 3))
        try:
            for i, cell in enumerate(cells):
                for j, point in enumerate(cell):
                    coordinates[i, j] = self._float_point(point)
            scale = max(self.scale, np.max(np.abs(coordinates)))
            exact = self._exact
        finally:
            self._float_points.clear()
            self._exact = old_exact
        if not isfinite(scale) or scale > _COORD_LIMIT:
            return [None] * len(cells)
        try:
            allowed = os.sched_getaffinity(0)
        except OSError:
            return [None] * len(cells)
        n = get_num_threads()
        if workers is not None:
            allowed &= set(range(16))
            if type(workers) is not int or not 1 <= workers <= min(n, len(allowed), 16):
                raise InputIssue(3, "geometry_chunk_placement")
        threads = min(n if workers is None else workers, len(allowed), len(cells), 16)
        # Linux returns the caller's effective mask after cpuset restrictions.
        # Use native unsigned-long words and room for the usual cpu_set_t.
        word_bits = ctypes.sizeof(ctypes.c_ulong) * 8
        affinity = np.zeros(max(1024 // word_bits, max(allowed) // word_bits + 1),
                            dtype=np.dtype(ctypes.c_ulong))
        for cpu in allowed:
            word, bit = divmod(cpu, word_bits)
            affinity[word] |= ctypes.c_ulong(1 << bit).value
        try:
            set_num_threads(threads)
            counts, work, choices, placement = _batch(
                coordinates, self.boxes, self.branches, self.triangles, scale, exact,
                affinity, threads)
        finally:
            set_num_threads(n)
        if placement.shape != (threads,) or np.any(placement == CHUNK_UNWRITTEN):
            raise InputIssue(3, "geometry_chunk_coverage")
        if np.any(placement == 2):
            raise OSError("could not restore Numba worker affinity")
        if np.any(placement):
            if workers is not None:
                raise InputIssue(3, "geometry_chunk_placement")
            return [None] * len(cells)
        if (counts.shape != (len(cells),) or work.shape != counts.shape
                or choices.shape != (len(cells), _MAX_CANDIDATES)
                or np.any(counts < 0) or np.any(counts > _MAX_CANDIDATES) or np.any(work == 0)):
            raise InputIssue(3, "geometry_chunk_coverage")
        return [(self, int(charge), choices[i, :counts[i]])
                for i, charge in enumerate(work)]

    def _grid_point(self, point):
        key = id(point)
        entry = self._grid_points.get(key)
        if entry is None:
            value = _grid(point)
            if len(self._grid_points) >= 131072:
                self._grid_points.clear()
            self._grid_points[key] = (point, value)
            return value
        return entry[1]

    def _grid_face(self, face):
        if face not in self._grid_faces:
            vertices = tuple(self._grid_point(point) for point in self.faces[face])
            terms = None if None in vertices else _face_terms(vertices)
            if len(self._grid_faces) >= 65536:
                self._grid_faces.clear()
            self._grid_faces[face] = terms
        return self._grid_faces[face]

    def exact(self, point, face):
        # Adjacent source cells share vertex objects as well as target faces.
        # Keep a bounded cache and retain the key object to preclude id reuse.
        key = (id(point), face)
        entry = self._cache.get(key)
        if entry is None:
            grid_point = self._grid_point(point)
            grid_face = self._grid_face(face)
            if grid_point is None or grid_face is None:
                distance = triangle_d2(point, self.faces[face])
            else:
                distance = _grid_triangle_d2(grid_point, grid_face)
            if len(self._cache) >= 65536:
                self._cache.clear()
            self._cache[key] = (point, distance)
            return distance
        return entry[1]


    def area_units(self, cell):
        points = tuple(self._grid_point(point) for point in cell)
        if len(points) != 3 or None in points:
            return None
        a, b, c = points
        normal = _integer_cross(_integer_sub(b, a), _integer_sub(c, a))
        squared = _integer_dot(normal, normal) * 10**12
        lower = isqrt(squared >> (4*_GRID_BITS))
        return lower, lower + ((lower**2 << (4*_GRID_BITS)) != squared)

    def replay(self, tree, cell, work):
        # When an interval overlaps an exact tie, keep the original traversal
        # and work accounting. Integer box predicates avoid Fraction traffic.
        points = tuple(self._grid_point(point) for point in cell)
        if len(points) != 3 or None in points:
            return tree.cell_bounds(cell, work)
        if self._grid_boxes is None:
            self._grid_boxes = [None] * len(self._boxes)
        before = work.used

        def box_bounds(ordinal):
            work.charge(3)
            bounds = self._grid_boxes[ordinal]
            if bounds is None:
                box = self._boxes[ordinal]
                low, high = map(_grid, zip(*box))
                if low is None or high is None:
                    raise _OutsideGrid
                bounds = self._grid_boxes[ordinal] = tuple(zip(low, high))
            return tuple(sum(max(lo-x, x-hi, 0)**2 for x, (lo, hi) in zip(point, bounds))
                         for point in points)

        nearest = [None] * 3
        upper = None
        try:
            pending = [(0, box_bounds(0))]
            while pending:
                node, lower = pending.pop()
                if upper is not None and max(lower)*upper.denominator >= upper.numerator*_GRID_SQUARED:
                    if all(lo*d.denominator >= d.numerator*_GRID_SQUARED for lo, d in zip(lower, nearest)):
                        continue
                left, right = self._branches[node]
                if left >= 0:
                    a, b = box_bounds(left), box_bounds(right)
                    if max(a) < max(b):
                        pending.extend(((right, b), (left, a)))
                    else:
                        pending.extend(((left, a), (right, b)))
                    continue
                for face in range(-left-1, right):
                    work.charge(3)
                    distances = [self.exact(point, face) for point in cell]
                    for i, distance in enumerate(distances):
                        if nearest[i] is None or distance < nearest[i]:
                            nearest[i] = distance
                    worst = max(distances)
                    if upper is None or worst < upper:
                        upper = worst
        except _OutsideGrid:
            # Replaying uses the same prefix of charges. Rewind that prefix
            # when switching arithmetic, so a later refusal has the old work.
            work.used = before
            return tree.cell_bounds(cell, work)
        return max(nearest), upper


class _OutsideGrid(Exception):
    pass


def resolve(tree, cell, work, result):
    if result is None:
        return tree.cell_bounds(cell, work)
    batch, charge, choices = result
    if charge <= 0 or not len(choices) or charge > work.limit-work.used:
        return batch.replay(tree, cell, work)
    nearest = [None, None, None]
    best_upper = None
    for row in choices:
        face, mask = divmod(int(row), 16)
        values = [batch.exact(point, face) if mask & (8 | 1 << i) else None
                  for i, point in enumerate(cell)]
        for i, value in enumerate(values):
            if mask & (1 << i) and (nearest[i] is None or value < nearest[i]):
                nearest[i] = value
        if mask & 8:
            whole = max(values)
            if best_upper is None or whole < best_upper:
                best_upper = whole
    if best_upper is None or any(value is None for value in nearest):
        return tree.cell_bounds(cell, work)
    work.charge(charge)
    return max(nearest), best_upper
