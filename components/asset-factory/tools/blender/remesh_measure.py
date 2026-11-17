"""Dense bidirectional BVH distance audit, independent of reconstruction.

All vertices and triangle centres are queried, not just an ordinal subsample.
The per-triangle centre-distance plus radius is also recorded as a numerical
cover estimate. Floating BVH arithmetic is not an exact-distance certificate.
"""
import hashlib
import json
import math
from pathlib import Path
import sys


def _geometry(path):
    import bpy
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.gltf(filepath=str(path), merge_vertices=True)
    objects = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if len(objects) != 1:
        raise ValueError("single_mesh_required")
    obj = objects[0]
    points = [obj.matrix_world @ v.co for v in obj.data.vertices]
    obj.data.calc_loop_triangles()
    triangles = [tuple(t.vertices) for t in obj.data.loop_triangles]
    if not points or not triangles:
        raise ValueError("empty_geometry")
    return points, triangles


def _direction(points, triangles, target):
    peak, cover = 0.0, 0.0
    for p in points:
        hit = target.find_nearest(p)
        if hit[3] is None:
            raise ValueError("distance_unavailable")
        peak = max(peak, hit[3])
    for a, b, c in triangles:
        p, q, r = points[a], points[b], points[c]
        centre = (p + q + r) / 3
        hit = target.find_nearest(centre)
        if hit[3] is None:
            raise ValueError("distance_unavailable")
        peak = max(peak, hit[3])
        cover = max(cover, hit[3] + max((v - centre).length for v in (p, q, r)))
    # Round outwards with a micrometre guard; still a floating-point estimate.
    return math.ceil(peak * 1000000) + 1, math.ceil(cover * 1000000) + 1


def main(plan):
    from mathutils.bvhtree import BVHTree
    raw = Path(plan["source"])
    candidate = Path(plan["candidate"])
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (raw, candidate)]
    if hashes != [plan["source_sha256"], plan["candidate_sha256"]]:
        return 4
    before, bt = _geometry(raw)
    after, at = _geometry(candidate)
    forward, forward_cover = _direction(before, bt, BVHTree.FromPolygons(after, at, all_triangles=True))
    backward, backward_cover = _direction(after, at, BVHTree.FromPolygons(before, bt, all_triangles=True))
    diag = math.floor(math.sqrt(sum((max(p[k] for p in before) - min(p[k] for p in before)) ** 2
                                    for k in range(3))) * 1000000)
    if diag <= 0:
        return 3
    rc = 0 if max(forward, backward, forward_cover, backward_cover) * 1000000 <= diag * 20000 else 5
    record = {"schema": "af.remesh-distance-audit/v1", "rc": rc,
              "input_sha256": hashes[0], "candidate_sha256": hashes[1],
              "raw_diag_um": diag, "forward_um": forward, "backward_um": backward,
              "forward_cover_estimate_um": forward_cover, "backward_cover_estimate_um": backward_cover,
              "forward_samples": len(before) + len(bt), "backward_samples": len(after) + len(at),
              "distance_is_exact": False, "estimator": "all_vertices_and_triangle_centres_BVH",
              "coverage_limit": "floating_BVH_and_centre_radius_cover_are_numerical_estimates"}
    Path(plan["audit"]).write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return rc


if __name__ == "__main__":
    try:
        rc = main(json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text()))
    except Exception as exc:
        print("AF_REMESH_ERROR", type(exc).__name__)
        rc = 2
    print("AF_REMESH_RC", rc, flush=True)
    if rc:
        raise RuntimeError("AF_REMESH_RC %d" % rc)
