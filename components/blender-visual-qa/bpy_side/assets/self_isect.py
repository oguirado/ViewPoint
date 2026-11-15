"""Self-intersection worker (mathutils.bvhtree.overlap). Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/assets/self_isect.py -- --mesh M.json
M.json holds {"prims": [{"name": str, "verts_um": [[x, y, z], ...], "tris": [[a, b, c], ...]}]}; vertex ids are
already welded by the host, so two triangles that share any vertex id are adjacent and never reported.
Prints one line `BVQA_SELF_ISECT {"prims": [{"name": str, "count": n, "pairs": [[i, j], ...]}]}` (i < j, pairs
capped at --max-pairs, count is the full number) rc 0; rc 2 + `BVQA_SELF_ISECT_ERROR {json}` on bad arguments/input.
Limit: a pair that shares a vertex is skipped even if it overlaps beyond that vertex (declared, host sliver check
and mesh-gate cover coplanar fans).
"""
import argparse
import json
import sys

UM_PER_M = 1000000.0


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="self_isect.py")
    p.add_argument("--mesh", required=True)
    p.add_argument("--max-pairs", type=int, default=256)
    return p.parse_args(argv)


# @anchor:bpy.assets.self_isect.pairs
def intersecting_pairs(verts_um, tris):
    """Non-adjacent intersecting triangle pairs (i < j) and their full count."""
    from mathutils.bvhtree import BVHTree
    verts = [(v[0] / UM_PER_M, v[1] / UM_PER_M, v[2] / UM_PER_M) for v in verts_um]
    tree = BVHTree.FromPolygons(verts, [tuple(t) for t in tris], epsilon=0.0)
    seen = set()
    for i, j in tree.overlap(tree):
        if i == j:
            continue
        a, b = (i, j) if i < j else (j, i)
        if set(tris[a]) & set(tris[b]):
            continue
        seen.add((a, b))
    return sorted(seen)


def main(argv):
    try:
        a = parse_args(argv)
        with open(a.mesh) as fh:
            doc = json.load(fh)
        out = []
        for prim in doc["prims"]:
            pairs = intersecting_pairs(prim["verts_um"], prim["tris"])
            out.append({"name": prim["name"], "count": len(pairs), "pairs": [list(p) for p in pairs[:a.max_pairs]]})
    except (SystemExit, OSError, ValueError, KeyError, ImportError) as exc:
        sys.stdout.write("BVQA_SELF_ISECT_ERROR " + json.dumps({"error": str(exc)}) + "\n")
        sys.stdout.flush()
        sys.exit(2)
    sys.stdout.write("BVQA_SELF_ISECT " + json.dumps({"prims": out}, sort_keys=True) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv)
