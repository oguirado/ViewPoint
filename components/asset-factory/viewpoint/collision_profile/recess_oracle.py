# SPDX-License-Identifier: AGPL-3.0-only
"""Independent fine geometry for the offline orthogonal recess fixture."""

from dataclasses import dataclass


def box_clearance(peg, obstacle):
    """Signed L-infinity separation, or negative minimum axis escape, in µm."""
    escape = (
        min(peg[1][axis] - obstacle[0][axis], obstacle[1][axis] - peg[0][axis])
        for axis in range(3)
    )
    return -min(escape)


def strips(outline, z):
    """Reconstruct an x-monotone orthogonal extrusion from its authored boundary."""
    if not (4 <= len(outline) <= 64 and len({tuple(p) for p in outline}) == len(outline)):
        raise ValueError("REFERENCE_OUTLINE")
    edges = list(zip(outline, outline[1:] + outline[:1]))
    if any((a[0] == b[0]) == (a[1] == b[1]) for a, b in edges):
        raise ValueError("REFERENCE_ORTHOGONAL_EDGES")
    for i, (a, b) in enumerate(edges):
        for j in range(i + 2, len(edges)):
            if i == 0 and j == len(edges) - 1:
                continue
            c, d = edges[j]
            if all(max(min(a[k], b[k]), min(c[k], d[k]))
                   <= min(max(a[k], b[k]), max(c[k], d[k])) for k in range(2)):
                raise ValueError("REFERENCE_SELF_INTERSECTION")
    xs = sorted({x for x, _ in outline})
    result = []
    for left, right in zip(xs, xs[1:]):
        cross = sorted(a[1] for a, b in edges
                       if a[1] == b[1] and min(a[0], b[0]) <= left
                       and right <= max(a[0], b[0]))
        if len(cross) != 2 or cross[0] == cross[1]:
            raise ValueError("REFERENCE_X_MONOTONE_REQUIRED")
        result.append(((left, cross[0], z[0]), (right, cross[1], z[1])))
    return tuple(result)


def envelope(boxes):
    return (
        tuple(min(box[0][i] for box in boxes) for i in range(3)),
        tuple(max(box[1][i] for box in boxes) for i in range(3)),
    )


@dataclass(frozen=True)
class Region:
    bounds: tuple
    children: tuple = ()


def index(boxes):
    # The strips are already ordered by x.
    if len(boxes) == 1:
        return Region(boxes[0])
    mid = len(boxes) // 2
    return Region(envelope(boxes), (index(boxes[:mid]), index(boxes[mid:])))


def selective_clearance(peg, region):
    """Exact minimum across strips, with a conservative lower bound per subtree."""
    probes = 0

    def visit(node, lower, best):
        nonlocal probes
        if best is not None and lower > best:
            return best
        if not node.children:
            probes += 1
            return lower if best is None else min(lower, best)
        ranked = sorted(
            ((box_clearance(peg, child.bounds), child) for child in node.children),
            key=lambda item: item[0],
        )
        for bound, child in ranked:
            best = visit(child, bound, best)
        return best

    clearance = visit(region, box_clearance(peg, region.bounds), None)
    return clearance, probes
