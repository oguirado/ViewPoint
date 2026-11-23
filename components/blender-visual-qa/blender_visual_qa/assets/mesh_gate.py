"""bv.assets.mesh_gate: topology + UV oracle over EVERY primitive of a GLB (stdlib only, integer math).

Port of scr-blender-mesh-gate v2 (AUDIT F1: all primitives, welded positions, full pairwise UV overlap).
Input comes from glb_core.parse_glb (positions in um, UVs in q16); the gate itself is duck-typed on the Scene.
Exit codes: 0 pass | 2 usage/schema/IO | 3 empty mesh (zero checks) | 5 gate fail.
Receipt schema bvqa.asset-mesh-gate/v1, sorted canonical JSON, bound to input_sha256 and prev_sha256.
"""
import argparse
import hashlib
import json
import sys
from collections import defaultdict

SCHEMA = "bvqa.asset-mesh-gate/v1"
RC_OK, RC_USAGE, RC_EMPTY, RC_FAIL = 0, 2, 3, 5
PPM = 1000000
UV_GRID_SHIFT = 12  # q16 UV space 0..65536 -> 16 broad-phase cells per axis


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _det3(m):
    # upper 3x3 of a 4x4 (row- or column-major: the determinant is the same)
    return _dot((m[0], m[1], m[2]), _cross((m[4], m[5], m[6]), (m[8], m[9], m[10])))


def _find(parent, x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union(parent, a, b):
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[ra] = rb


def _tri_overlap_2d(a, b):
    """True when two 2D triangles share interior area (touching edges/points do not count). Integer SAT."""
    for tri in (a, b):
        for i in range(3):
            p, q = tri[i], tri[(i + 1) % 3]
            nx, ny = q[1] - p[1], p[0] - q[0]
            pa = [nx * v[0] + ny * v[1] for v in a]
            pb = [nx * v[0] + ny * v[1] for v in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def _uv_area2(t):
    return (t[1][0] - t[0][0]) * (t[2][1] - t[0][1]) - (t[1][1] - t[0][1]) * (t[2][0] - t[0][0])


def _uv_overlap(tris):
    """tris: list of (tri_id, ((u,v),..3)). Returns the set of overlapping tri_ids (full pairwise, grid broad phase)."""
    cells = defaultdict(list)
    for k, (_, t) in enumerate(tris):
        xs, ys = [v[0] >> UV_GRID_SHIFT for v in t], [v[1] >> UV_GRID_SHIFT for v in t]
        for cx in range(min(xs), max(xs) + 1):
            for cy in range(min(ys), max(ys) + 1):
                cells[(cx, cy)].append(k)
    seen, hit = set(), set()
    for members in cells.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                if _tri_overlap_2d(tris[a][1], tris[b][1]):
                    hit.add(tris[a][0])
                    hit.add(tris[b][0])
    return hit


def gate_prim(prim, max_genus=0, allow_open=False, require_uv=True, allow_uv_overlap=False):
    """Topology + UV metrics and failed check ids for one primitive. Returns (metrics, failed)."""
    pos, idx = list(prim.positions_um), list(prim.indices)
    ntri = len(idx) // 3
    weld, wid = {}, []
    for p in pos:
        wid.append(weld.setdefault(tuple(p), len(weld)))
    wpos = [None] * len(weld)
    for p, w in zip(pos, wid):
        wpos[w] = tuple(p)
    tris = [tuple(wid[i] for i in idx[3 * f:3 * f + 3]) for f in range(ntri)]

    degenerate = duplicate = 0
    seen_faces, good = set(), []
    for t in tris:
        if len(set(t)) != 3 or _cross(_sub(wpos[t[1]], wpos[t[0]]), _sub(wpos[t[2]], wpos[t[0]])) == (0, 0, 0):
            degenerate += 1
            continue
        key = tuple(sorted(t))
        if key in seen_faces:
            duplicate += 1
            continue
        seen_faces.add(key)
        good.append(t)

    directed = defaultdict(int)
    undirected = defaultdict(int)
    for t in good:
        for i in range(3):
            a, b = t[i], t[(i + 1) % 3]
            directed[(a, b)] += 1
            undirected[(min(a, b), max(a, b))] += 1
    over = sum(1 for d in undirected.values() if d > 2)
    boundary = sum(1 for d in undirected.values() if d == 1)
    twins = sum(1 for d in directed.values() if d > 1)

    # pinched vertices: incident faces of a vertex must form one fan connected through edges at that vertex
    inc = defaultdict(list)
    for f, t in enumerate(good):
        for v in t:
            inc[v].append(f)
    pinched = 0
    for v, faces in inc.items():
        parent = {f: f for f in faces}
        by_edge = {}
        for f in faces:
            for w in good[f]:
                if w != v:
                    if w in by_edge:
                        _union(parent, f, by_edge[w])
                    else:
                        by_edge[w] = f
        if len({_find(parent, f) for f in faces}) > 1:
            pinched += 1

    verts_used = {v for t in good for v in t}
    comp = {v: v for v in verts_used}
    for t in good:
        _union(comp, t[0], t[1])
        _union(comp, t[1], t[2])
    components = len({_find(comp, v) for v in verts_used})
    chi = len(verts_used) - len(undirected) + len(good)
    closed = bool(good) and boundary == 0
    manifold = not over and not twins and not pinched
    genus = None
    if closed and manifold and (2 * components - chi) % 2 == 0:
        genus = (2 * components - chi) // 2

    volume6 = None
    if closed and manifold:
        volume6 = sum(_dot(wpos[t[0]], _cross(wpos[t[1]], wpos[t[2]])) for t in good)
        m = getattr(prim, "world_q16", None)
        if m is not None and len(m) > 10 and _det3(m) < 0:
            volume6 = -volume6

    uv_present = prim.uvs_q16 is not None and len(prim.uvs_q16) == len(pos)
    uv_zero = 0
    uv_hit = set()
    uv_tris = 0
    if uv_present:
        uvt = []
        for f in range(ntri):
            t = tuple(tuple(prim.uvs_q16[i]) for i in idx[3 * f:3 * f + 3])
            uv_tris += 1
            if _uv_area2(t) == 0:
                uv_zero += 1
            else:
                uvt.append((f, t))
        uv_hit = _uv_overlap(uvt)
    overlap_ppm = (len(uv_hit) * PPM // uv_tris) if uv_tris else 0

    metrics = {
        "tris": ntri, "vertices": len(verts_used), "edges": len(undirected), "components": components,
        "euler_characteristic": chi, "genus": genus, "boundary_edges": boundary,
        "uv_overlap_tris": len(uv_hit), "overlap_ppm": overlap_ppm, "uv_zero_area": uv_zero,
    }
    failed = []
    if degenerate:
        failed.append("degenerate_faces")
    if duplicate:
        failed.append("duplicate_faces")
    if over:
        failed.append("edge_multiplicity")
    if twins:
        failed.append("twin_winding")
    if pinched:
        failed.append("pinched_vertices")
    if boundary and not allow_open:
        failed.append("open_boundary")
    if genus is not None and genus > max_genus:
        failed.append("genus")
    if volume6 is not None and volume6 < 0:
        failed.append("inward_winding")
    if require_uv and not uv_present:
        failed.append("uv_missing")
    if uv_zero:
        failed.append("uv_degenerate")
    if overlap_ppm and not allow_uv_overlap:
        failed.append("uv_overlap")
    return metrics, failed


def gate_scene(scene, max_genus=0, allow_open=False, require_uv=True, allow_uv_overlap=False):
    """Run the gate over every primitive. Returns (rc, body) where body is the receipt payload without hashes."""
    prims = []
    failed = set()
    for p in scene.prims:
        if not len(p.indices):
            continue
        m, f = gate_prim(p, max_genus, allow_open, require_uv, allow_uv_overlap)
        m["name"] = str(p.name)
        m["failed"] = sorted(f)
        prims.append(m)
        failed.update(f)
    if not prims:
        return RC_EMPTY, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "empty mesh: zero checks executed",
                          "prims": [], "failed": []}
    body = {"status": "FAIL" if failed else "PASS", "prims": prims, "failed": sorted(failed),
            "primitives_checked": len(prims),
            "options": {"max_genus": max_genus, "allow_open": int(allow_open), "require_uv": int(require_uv),
                        "allow_uv_overlap": int(allow_uv_overlap)}}
    return (RC_FAIL if failed else RC_OK), body


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def make_receipt(rc, body, input_sha256, prev_sha256):
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": input_sha256, "prev_sha256": prev_sha256})
    return rec


def _bool01(s):
    return s not in ("0", "false", "False")


def run(argv):
    """CLI core. Returns (rc, receipt_or_None). Prints nothing; main() prints the canonical receipt."""
    ap = argparse.ArgumentParser(prog="mesh-gate", description="topology + UV gate over all primitives of a GLB")
    ap.add_argument("--mesh", required=True)
    ap.add_argument("--max-genus", type=int, default=0)
    ap.add_argument("--allow-uv-overlap", default="0")
    ap.add_argument("--allow-open", default="0")
    ap.add_argument("--require-uv", default="1")
    ap.add_argument("--prev-receipt", default=None, help="receipt file whose sha256 is chained as prev_sha256")
    ap.add_argument("--out", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return RC_USAGE, None
    from blender_visual_qa.assets import glb_core
    try:
        with open(a.mesh, "rb") as fh:
            raw = fh.read()
        prev = ""
        if a.prev_receipt:
            with open(a.prev_receipt, "rb") as fh:
                prev = hashlib.sha256(fh.read()).hexdigest()
        scene = glb_core.parse_glb(raw)
    except (OSError, glb_core.GlbError) as exc:
        sys.stderr.write("mesh-gate: %s\n" % (exc,))
        return RC_USAGE, None
    rc, body = gate_scene(scene, a.max_genus, _bool01(a.allow_open), _bool01(a.require_uv),
                          _bool01(a.allow_uv_overlap))
    rec = make_receipt(rc, body, hashlib.sha256(raw).hexdigest(), prev)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(canon(rec))
    return rc, rec


def main(argv=None):
    rc, rec = run(sys.argv[1:] if argv is None else argv)
    if rec is not None:
        sys.stdout.write(canon(rec))
    return rc


if __name__ == "__main__":
    sys.exit(main())
