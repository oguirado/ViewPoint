"""Bounded bmesh healing worker (VG-0238, P <= 4). Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/assets/heal_worker.py -- \
      --mesh M.json --weld-um 10 --max-hole-edges 64 --max-passes 4
M.json holds {"positions_um": [[x, y, z], ...], "tris": [[i, j, k], ...], "reflect": 0|1} (local micrometres).
Each pass: remove_doubles(dist=weld) -> dissolve_degenerate(dist=weld) -> delete sliver/duplicate faces -> holes_fill
(sides <= max-hole-edges) -> recalc_face_normals -> triangulate; stops when the face set no longer changes.
bmesh works in float metres; positions are re-quantised to integer um on output (the host re-verifies everything).
Output: `BVQA_HEAL {"positions_um": [...], "tris": [...], "stats": {...}}` rc 0; rc 2 + `BVQA_HEAL_ERROR {json}`
on bad arguments.
"""
import argparse
import json
import sys

UM_PER_M = 1000000.0
SQRT3 = 3.0 ** 0.5


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="heal_worker.py")
    p.add_argument("--mesh", required=True)
    p.add_argument("--weld-um", type=int, required=True)
    p.add_argument("--max-hole-edges", type=int, required=True)
    p.add_argument("--max-passes", type=int, required=True)
    return p.parse_args(argv)


def _signature(bm):
    return sorted(tuple(sorted(v.index for v in f.verts)) for f in bm.faces)


def _signed_volume6(bm):
    total = 0.0
    for f in bm.faces:
        a, b, c = (v.co for v in f.verts[:3])
        total += a.dot(b.cross(c))
    return total


def _cull(bm, bmesh, dist):
    """Delete sliver faces (altitude over the longest edge <= dist) and duplicate faces (same vertex set)."""
    seen, dead = set(), []
    for f in bm.faces:
        key = frozenset(v.index for v in f.verts)
        co = [v.co for v in f.verts]
        longest = max((co[i] - co[i - 1]).length for i in range(len(co)))
        area2 = 2.0 * f.calc_area()
        if key in seen or len(key) != len(co) or longest == 0.0 or area2 / longest <= dist:
            dead.append(f)
        else:
            seen.add(key)
    if dead:
        bmesh.ops.delete(bm, geom=dead, context="FACES_ONLY")
    return len(dead)


def heal(doc, weld_um, max_hole_edges, max_passes):
    import bmesh
    bm = bmesh.new()
    verts = [bm.verts.new([c / UM_PER_M for c in p]) for p in doc["positions_um"]]
    culled_new = 0
    for t in doc["tris"]:
        if len(set(t)) != 3:
            culled_new += 1
            continue
        try:
            bm.faces.new([verts[i] for i in t])
        except ValueError:  # duplicate face: bmesh refuses it, which is the culling
            culled_new += 1
    # remove_doubles is Euclidean; sqrt(3) * tol covers the host's Chebyshev weld box.
    dist = weld_um * SQRT3 / UM_PER_M
    culled = culled_new
    stats = {"passes": 0, "converged": 0, "merged_vertices": 0, "patches": 0}
    for _ in range(max_passes):
        stats["passes"] += 1
        bm.verts.index_update()
        before = _signature(bm)
        nv = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=dist)
        stats["merged_vertices"] += nv - len(bm.verts)
        bmesh.ops.dissolve_degenerate(bm, dist=dist, edges=list(bm.edges))
        bm.verts.index_update()
        culled += _cull(bm, bmesh, dist)
        loose = [v for v in bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        res = bmesh.ops.holes_fill(bm, edges=[e for e in bm.edges if e.is_boundary], sides=max_hole_edges)
        stats["patches"] += len(res.get("faces", []))
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bmesh.ops.triangulate(bm, faces=list(bm.faces))
        bm.verts.index_update()
        if _signature(bm) == before:
            stats["converged"] = 1
            break
    vol = _signed_volume6(bm)
    if (vol < 0) != bool(doc.get("reflect")):
        bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
    bm.verts.index_update()
    pos = [[int(round(c * UM_PER_M)) for c in v.co] for v in bm.verts]
    tris = [[v.index for v in f.verts] for f in bm.faces]
    stats["culled_faces"] = culled
    bm.free()
    return {"positions_um": pos, "tris": tris, "stats": stats}


def main(argv):
    try:
        a = parse_args(argv)
        with open(a.mesh) as fh:
            doc = json.load(fh)
    except (SystemExit, OSError, ValueError) as exc:
        print("BVQA_HEAL_ERROR " + json.dumps({"error": str(exc)}))
        return 2
    print("BVQA_HEAL " + json.dumps(heal(doc, a.weld_um, a.max_hole_edges, a.max_passes), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
