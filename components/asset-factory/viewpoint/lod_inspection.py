# SPDX-License-Identifier: AGPL-3.0-only
"""Inspect adjacent static mesh LODs through a pinned offline fixture."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from fractions import Fraction
from functools import lru_cache
import json
from pathlib import Path
import sys

from viewpoint.lod_wire import Rejected, require
from viewpoint.mini_topology import oracle
from tools.input.evidence import InputIssue
from tools.mesh import certificate as geometry
from tools.mesh.contracts import MeshError
from tools.mesh.worker import _document


V1 = "vp3d.lod_inspection_policy/v1"
V2 = "vp3d.lod_inspection_policy/v2"
LEGACY_COORDINATES = "glTF Y-up, micrometres; height along Z"
COORDINATES = "glTF Y-up, micrometres"


def is_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def load_policy(path, pin):
    raw = path.read_bytes()
    require(oracle.digest(raw) == pin, "POLICY_PIN_MISMATCH")
    policy = json.loads(raw)
    require(oracle.encoded(policy) == raw, "INSPECTION_POLICY")
    common = {"schema", "parent_sha256", "object", "coordinates", "max_distance_um",
              "triangle_budget", "patch_cuts_x_um", "patch_ids", "landmarks_um"}
    require(isinstance(policy, dict) and policy.get("schema") in (V1, V2), "INSPECTION_POLICY")
    if policy["schema"] == V1:
        require(set(policy) == common and policy["coordinates"] == LEGACY_COORDINATES,
                "INSPECTION_POLICY")
    else:
        require(set(policy) == common | {"topology", "source_sha256", "reference_sha256"}
                and policy["coordinates"] == COORDINATES
                and policy["topology"] in ("closed", "match_parent_boundary")
                and is_sha256(policy["source_sha256"]) and is_sha256(policy["reference_sha256"]),
                "INSPECTION_POLICY")
    require(is_sha256(policy["parent_sha256"]) and isinstance(policy["object"], str)
            and policy["object"], "INSPECTION_POLICY")
    limit, budget = policy["max_distance_um"], policy["triangle_budget"]
    cuts, names, landmarks = (policy[key] for key in (
        "patch_cuts_x_um", "patch_ids", "landmarks_um"))
    require(type(limit) is int and 0 <= limit <= 100_000_000
            and type(budget) is int and 1 <= budget <= oracle.MAX_ELEMENTS
            and isinstance(cuts, list) and 1 <= len(cuts) < oracle.MAX_ELEMENTS
            and all(type(cut) is int for cut in cuts) and sorted(set(cuts)) == cuts
            and isinstance(names, list) and len(names) == len(cuts) + 1
            and all(isinstance(name, str) and name for name in names)
            and len(set(names)) == len(names)
            and isinstance(landmarks, list) and 1 <= len(landmarks) <= oracle.MAX_ELEMENTS
            and all(isinstance(point, list) and len(point) == 3
                    and all(type(axis) is int for axis in point) for point in landmarks),
            "INSPECTION_POLICY")
    return policy


def edge_faces(triangles):
    edges = defaultdict(list)
    for index, face in enumerate(triangles):
        for edge in geometry.cycle(face):
            edges[tuple(sorted(edge))].append(index)
    return edges


def segment_coverage(segments, *, oriented=False):
    """Compare exact geometric segments while allowing collinear subdivision."""
    lines = defaultdict(list)
    for start, end in segments:
        axis = next(i for i in range(3) if start[i] != end[i])
        slope = tuple(Fraction(b - a, end[axis] - start[axis])
                      for a, b in zip(start, end))
        intercept = tuple(a - rate * start[axis] for a, rate in zip(start, slope))
        direction = (start[axis] < end[axis]) if oriented else None
        lines[axis, slope, intercept, direction].append(tuple(sorted((start[axis], end[axis]))))
    coverage = {}
    for line, spans in lines.items():
        merged = []
        for low, high in sorted(spans):
            if merged and low <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((low, high))
        coverage[line] = merged
    return coverage


def section(triangles, cut):
    segments, contacts, planar = set(), set(), Counter()
    for face in triangles:
        if all(point[0] == cut for point in face):
            planar.update(tuple(sorted(edge)) for edge in geometry.cycle(face))
            continue
        points = {point for point in face if point[0] == cut}
        for start, end in geometry.cycle(face):
            if min(start[0], end[0]) < cut < max(start[0], end[0]):
                points.add(geometry.lerp(start, end, Fraction(cut - start[0], end[0] - start[0])))
        if len(points) == 2:
            segments.add(tuple(sorted(points)))
        else:
            contacts.update(points)
    segments.update(edge for edge, uses in planar.items() if uses == 1)
    isolated = {point for point in contacts
                if all(geometry.segment_d2(point, *edge) != 0 for edge in segments)}
    return segments, isolated


def compare_seams(cuts, names, before, after):
    seams = []
    for index, cut in enumerate(cuts):
        first, first_points = section(before, cut)
        second, second_points = section(after, cut)
        require((first or first_points) and first_points == second_points
                and segment_coverage(first) == segment_coverage(second), "SEAM_CONTINUITY")
        seams.append({"cut_x_um": cut, "patch_ids": sorted(names[index:index + 2]),
                      "knots_checked": len(first_points | {point for edge in first | second for point in edge})})
    return seams


def check_topology(reports, triangles, edges, mode, evidence):
    closed = mode == "closed"
    summaries, boundaries = [], []
    for role, report, faces, incidence in zip(("parent", "child"), reports, triangles, edges):
        if report["degenerate_faces"]:
            evidence["defect"] = {"mesh": role, "faces": [
                index for index, face in enumerate(faces) if not any(geometry.normal(face))]}
            raise Rejected("DEGENERATE_FACE")
        if not oracle.clean(report):
            evidence["defect"] = {"mesh": role}
            raise Rejected("TOPOLOGY_NOT_CLEAN")
        boundary = [edge for edge, uses in incidence.items() if len(uses) == 1]
        if closed and boundary:
            evidence["defect"] = {"mesh": role, "boundary_edge_um": min(boundary)}
            raise Rejected("CLOSED_BOUNDARY_EDGE")
        try:
            shell = geometry._shell(faces, None, geometry.Work(oracle.MAX_ELEMENTS))
        except InputIssue as exc:
            evidence["defect"] = {"mesh": role, "detail": exc.code}
            raise Rejected("TOPOLOGY_NOT_CLEAN") from exc
        report.update({key: shell[key] for key in ("chi", "genus", "boundary_loops", "signed_volume6_um3")})
        summaries.append(shell)
        boundaries.append([edge for face in faces for edge in geometry.cycle(face)
                           if len(incidence[tuple(sorted(edge))]) == 1])
    parent, child = summaries
    if closed:
        parent_volume, child_volume = (Fraction(*item["signed_volume6_um3"]) for item in summaries)
        require(parent_volume * child_volume > 0, "SHELL_ORIENTATION_CHANGED")
    else:
        require(parent["boundary_loops"] > 0, "OPEN_BOUNDARY_PROFILE_REQUIRED")
        require(parent["boundary_loops"] == child["boundary_loops"]
                and segment_coverage(boundaries[0], oriented=True) == segment_coverage(boundaries[1], oriented=True),
                "OPEN_BOUNDARY_CHANGED")
    require(parent["genus"] == child["genus"], "SHELL_GENUS_CHANGED")
    evidence["topology"]["verdict"] = "clean"


def ratio(number):
    return [number.numerator, number.denominator]


def target_triangles(triangles):
    rows = []
    for face in triangles:
        scaled = tuple(tuple(6 * axis for axis in point) for point in face)
        bounds = tuple((min(axis), max(axis)) for axis in zip(*scaled))
        rows.append((scaled, bounds))
    return tuple(rows)


@lru_cache(maxsize=8)
def cached_parent(sha256, raw):
    # The digest identifies the content. Keeping the bounded source bytes in
    # the key also requires exact byte equality before reusing parsed data.
    model = oracle.decode_mesh(raw, _document)
    return model, target_triangles(oracle.triangles(model))


def load_parent(path):
    raw = oracle.read_mesh(path)
    return cached_parent(oracle.digest(raw), raw)


def directed(source, target):
    # Scale by six so vertices, edge midpoints and centroids all use integers.
    samples = {}
    for index, face in enumerate(source):
        vertices = tuple(tuple(6 * axis for axis in point) for point in face)
        centroid = tuple(2 * sum(axis) for axis in zip(*face))
        midpoints = (tuple(3 * (a + b) for a, b in zip(start, end))
                     for start, end in geometry.cycle(face))
        for point in (*vertices, *midpoints, centroid):
            samples.setdefault(point, index)
    worst = None
    for point, source_index in samples.items():
        nearest = None
        for index, (face, bounds) in enumerate(target):
            if nearest is not None:
                # Faces are visited in input order, so a tie retains the
                # earlier face. The exact box distance is a lower bound.
                lower = sum(max(lo - axis, axis - hi, 0)**2
                            for axis, (lo, hi) in zip(point, bounds))
                if lower >= nearest[0]:
                    continue
            distance = geometry.triangle_d2(point, face)
            if nearest is None or distance < nearest[0]:
                nearest = (distance, index)
                if distance == 0:
                    break
        squared, target_index = nearest
        if worst is None or squared > worst[0]:
            worst = (squared, source_index, target_index, point)
    squared, source_index, target_index, point = worst
    squared = Fraction(squared, 36)
    return {
        "squared_um2": ratio(squared),
        "interval_um": [ratio(bound) for bound in geometry.sqrt_bounds(squared)],
        "source_face": source_index, "target_face": target_index,
        "sample_um": [ratio(Fraction(axis, 6)) for axis in point], "samples": len(samples),
        # The report counts the logical sample/face pairs covered by the test.
        "triangle_evaluations": len(samples) * len(target),
    }


def face_ring(patch_index, cuts, triangles, edges):
    patch_faces = set()
    for index, face in enumerate(triangles):
        low, high = min(p[0] for p in face), max(p[0] for p in face)
        start = bisect_right(cuts, low)
        stop = bisect_right(cuts, high) - (low < high and high in cuts)
        if start <= patch_index <= stop:
            patch_faces.add(index)
    neighbors = {index for incident in edges.values() if patch_faces.intersection(incident)
                 for index in incident}
    return sorted(patch_faces), sorted(neighbors - patch_faces)


def inspect(parent_path, child_path, policy_path, policy_pin, child_pin, reference_path, evidence):
    policy = load_policy(policy_path, policy_pin)
    parent, parent_target = load_parent(parent_path)
    child = oracle.mesh(child_path, _document)
    require(parent["sha256"] == policy["parent_sha256"]
            and child["parent_sha256"] == parent["sha256"], "WRONG_ANCESTOR")
    require(child["sha256"] == child_pin, "CHILD_PIN_MISMATCH")
    if policy["schema"] == V2:
        require(child["sha256"] == policy["source_sha256"], "SOURCE_PIN_MISMATCH")
        require(reference_path is not None
                and oracle.digest(reference_path.read_bytes()) == policy["reference_sha256"],
                "REFERENCE_PIN_MISMATCH")
        reference_pin = policy["reference_sha256"]
        topology_mode = policy["topology"]
    else:
        require(reference_path is None, "INSPECTION_POLICY")
        # The v1 parent is the sole pinned geometric reference.
        reference_pin = parent["sha256"]
        topology_mode = "closed"

    parent_report, child_report = [oracle.topology(model, geometry) for model in (parent, child)]
    evidence.update({
        "policy_sha256": policy_pin, "parent_sha256": parent["sha256"], "child_sha256": child_pin,
        "source_sha256": child["sha256"], "reference_sha256": reference_pin,
        "object": policy["object"], "coordinates": policy["coordinates"],
        "topology": {"parent": parent_report, "child": child_report, "mode": topology_mode, "verdict": "rejected"},
    })
    before, after = triangles = tuple(oracle.triangles(model) for model in (parent, child))
    edges = tuple(edge_faces(faces) for faces in triangles)
    check_topology((parent_report, child_report), triangles, edges, topology_mode, evidence)

    budget, limit = policy["triangle_budget"], policy["max_distance_um"]
    require(child_report["triangles"] <= min(budget, parent_report["triangles"]), "TRIANGLE_BUDGET")
    bounds = parent_report["bounds_um"]
    require(bounds == child_report["bounds_um"], "BOUNDS_CHANGED")
    parent_points, child_points = (set(model["vertices_um"]) for model in (parent, child))
    require(all(tuple(point) in parent_points & child_points for point in policy["landmarks_um"]),
            "LANDMARK_CHANGED")

    cuts, names = policy["patch_cuts_x_um"], policy["patch_ids"]
    require(bounds[0][0] < cuts[0] and cuts[-1] < bounds[1][0], "PATCH_CUT_OUTSIDE_MESH")
    seams = compare_seams(cuts, names, before, after)
    forward = directed(before, target_triangles(after))
    reverse = directed(after, parent_target)
    directions = {"parent_to_child": forward, "child_to_parent": reverse}
    for witness in directions.values():
        # A source sample on a cut belongs to the slab on its right.
        patch_index = bisect_right(cuts, Fraction(*witness["sample_um"][0]))
        witness["patch_id"] = names[patch_index]
    direction = max(directions, key=lambda key: Fraction(*directions[key]["squared_um2"]))
    worst = directions[direction]
    patch, ring = face_ring(names.index(worst["patch_id"]), cuts, before, edges[0])
    removed, added = len(parent_points - child_points), len(child_points - parent_points)
    exceeds = Fraction(*worst["squared_um2"]) > limit**2
    evidence.update({
        "check": "sampled_bidirectional_point_to_triangle",
        "max_distance_um": limit, "triangle_budget": budget,
        "directions": directions, "worst": {"direction": direction, **worst},
        "parent_patch_faces": patch, "adjacent_parent_face_ring": ring,
        "seams": seams, "landmarks_checked": len(policy["landmarks_um"]),
        "triangles_inspected": len(before) + len(after),
        "triangle_evaluations": forward["triangle_evaluations"] + reverse["triangle_evaluations"],
        # Without persistent vertex IDs, this counts paired position replacements.
        "vertices_changed": min(removed, added),
        "vertices_change_basis": "unmatched_xyz_positions_paired_by_count",
        "vertices_added": added,
        "vertices_removed": removed,
        "decision": "retain_finer_lod" if exceeds else "accept_child_lod",
        "selected_sha256": parent["sha256"] if exceeds else child_pin,
    })
    require(not exceeds, "PATCH_DISTANCE_EXCEEDED")


def main(argv):
    parser = argparse.ArgumentParser(prog="lod_wire.py inspect", description=__doc__)
    parser.add_argument("parent", type=Path)
    parser.add_argument("child", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--child-sha256", required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args(argv)
    evidence = {"schema": "vp3d.lod_inspection/v1", "scope": "offline_fixture"}
    try:
        inspect(args.parent, args.child, args.policy, args.policy_sha256, args.child_sha256,
                args.reference, evidence)
    except (Rejected, oracle.Refusal, MeshError) as exc:
        evidence.update(status="REJECTED", reason=getattr(exc, "code", str(exc)))
        rc = 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Inspection harness error: {exc}", file=sys.stderr)
        return 2
    else:
        evidence["status"] = "OK"
        rc = 0
    sys.stdout.buffer.write(oracle.encoded(evidence))
    return rc
