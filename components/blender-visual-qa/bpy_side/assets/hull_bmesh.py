"""Bounded convex hull worker (bmesh.ops.convex_hull). Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/assets/hull_bmesh.py -- \
      --points P.json --max-verts 32 --max-faces 60
P.json holds {"points_um": [[x, y, z], ...]} (distinct world-space points of the selected visual mesh, micrometres).
The exact bmesh hull is taken; when it exceeds the budget it is rebuilt from the points that farthest extend the
current hull, starting from an extreme-point tetrahedron (greedy inner approximation, never an AABB fallback).
Hull vertices are always input points, so the result is printed as triangles of INPUT POINT INDICES, outward wound:
  `BVQA_HULL {"tris": [[i, j, k], ...], "exact": bool}` rc 0, or `BVQA_HULL {"error": "..."}` rc 0 on a degenerate
  (flat / < 4 distinct points) set. rc 2 + `BVQA_HULL_ERROR {json}` on bad arguments.
"""
import argparse
import json
import math
import sys

UM_PER_M = 1000000.0
EPS_VOL = 1e-12
EPS_OUT = 1e-9


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="hull_bmesh.py")
    p.add_argument("--points", required=True)
    p.add_argument("--max-verts", type=int, required=True)
    p.add_argument("--max-faces", type=int, required=True)
    return p.parse_args(argv)


def _sub(u, v):
    return (u[0] - v[0], u[1] - v[1], u[2] - v[2])


def _cross(u, v):
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def _dot(u, v):
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


def signed_volume(pts, tris):
    return sum(_dot(pts[a], _cross(pts[b], pts[c])) for a, b, c in tris) / 6.0


def hull_of(pts, ids):
    """Exact bmesh convex hull of pts[ids] -> triangles of indices into pts (hull vertices are input vertices)."""
    import bmesh
    bm = bmesh.new()
    back = {}
    for i in ids:
        back[bm.verts.new(pts[i])] = i
    res = bmesh.ops.convex_hull(bm, input=list(bm.verts))
    faces = [g for g in res["geom"] if isinstance(g, bmesh.types.BMFace)]
    if faces:
        bmesh.ops.triangulate(bm, faces=faces)
    tris = [tuple(back[v] for v in f.verts) for f in bm.faces]
    bm.free()
    return tris


def _verts(tris):
    return sorted({i for t in tris for i in t})


# @anchor:bpy.assets.hull_bmesh.build
def build(pts, max_verts, max_faces):
    """pts in metres. Returns (tris, exact). Raises ValueError on a degenerate set."""
    if len(pts) < 4:
        raise ValueError("degenerate input: %d distinct points" % len(pts))
    tris = hull_of(pts, range(len(pts)))
    if len(_verts(tris)) < 4 or len(tris) < 4 or abs(signed_volume(pts, tris)) <= EPS_VOL:
        raise ValueError("degenerate input: zero-volume point set")
    exact = len(_verts(tris)) <= max_verts and len(tris) <= max_faces
    if not exact:
        cand = _verts(tris)
        a = min(cand, key=lambda i: pts[i])
        b = max(cand, key=lambda i: _dot(_sub(pts[i], pts[a]), _sub(pts[i], pts[a])))
        ab = _sub(pts[b], pts[a])
        c = max(cand, key=lambda i: _dot(_cross(ab, _sub(pts[i], pts[a])), _cross(ab, _sub(pts[i], pts[a]))))
        nrm = _cross(ab, _sub(pts[c], pts[a]))
        d = max(cand, key=lambda i: abs(_dot(nrm, _sub(pts[i], pts[a]))))
        cur = sorted({a, b, c, d})
        tris = hull_of(pts, cur)
        if len(tris) < 4 or abs(signed_volume(pts, tris)) <= EPS_VOL:
            raise ValueError("degenerate seed tetrahedron")
        for _ in range(4 * max_verts):
            hv = _verts(tris)
            cen = tuple(sum(pts[i][k] for i in hv) / len(hv) for k in range(3))
            planes = []
            for i, j, k in tris:
                n = _cross(_sub(pts[j], pts[i]), _sub(pts[k], pts[i]))
                ln = math.sqrt(_dot(n, n)) or 1.0
                n = (n[0] / ln, n[1] / ln, n[2] / ln)
                off = _dot(n, pts[i])
                if _dot(n, cen) - off > 0:
                    n, off = (-n[0], -n[1], -n[2]), -off
                planes.append((n, off))
            have = set(hv)
            best, best_out = None, EPS_OUT
            for i in cand:
                if i in have:
                    continue
                out = max(_dot(n, pts[i]) - off for n, off in planes)
                if out > best_out:
                    best, best_out = i, out
            if best is None:
                break
            nt = hull_of(pts, hv + [best])
            if len(_verts(nt)) > max_verts or len(nt) > max_faces:
                break
            tris = nt
    if signed_volume(pts, tris) < 0:
        tris = [(a, c, b) for a, b, c in tris]
    return tris, exact


def main():
    try:
        a = parse_args(sys.argv)
        with open(a.points) as fh:
            raw = json.load(fh)["points_um"]
        pts = [tuple(c / UM_PER_M for c in p) for p in raw]
    except (SystemExit, OSError, ValueError, KeyError, TypeError) as exc:
        print("BVQA_HULL_ERROR " + json.dumps({"reason": str(exc)}, sort_keys=True))
        sys.stdout.flush()
        sys.exit(2)
    try:
        tris, exact = build(pts, a.max_verts, a.max_faces)
        rec = {"tris": [list(t) for t in tris], "exact": exact}
    except ValueError as exc:
        rec = {"error": str(exc)}
    print("BVQA_HULL " + json.dumps(rec, sort_keys=True, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
