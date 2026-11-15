"""INSPECT gate for static GLB assets (AG1 L1, port of domain `scr-blender-inspect`, router 3.1).

Walks every node and primitive of the default scene in world space (AUDIT F1) through `glb_core`, welds world
positions on the 1 um integer lattice per node, and runs 24 integer checks. The ONLY policy source is the signed
class matrix `config/class_gate_matrix_v1.json` (every class key is required and integer; unknown key = rc 2).
Matrix binding: body_sha256 = sha256(canonical JSON, sort_keys, separators (',',':')) of the matrix WITHOUT `signed`.

CLI: python3 -m blender_visual_qa.assets.inspect_gate --in <asset.glb> --class prop|kit_tile|environment_piece|character
       [--matrix <m.json>] [--matrix-sha <hex>] [--prev-receipt <receipt.json>] [--out <receipt.json>]
     python3 -m blender_visual_qa.assets.inspect_gate --print-body-sha [--matrix <m.json>]
Exit (router 0.3): 0 OK | 2 usage/schema/IO | 3 zero checks (no triangles) or matrix unsigned | 4 stale sha (matrix
body sha != signed.body_sha256 or != --matrix-sha) | 5 gate fail. Stdout: the canonical receipt (bvqa.asset-inspect/v1).
"""
# @module {"id":"bv.assets.inspect_gate","layer":2,"owner":"BASSETS"}
import argparse
import hashlib
import json
from pathlib import Path
import sys

from blender_visual_qa.assets import glb_core

SCHEMA = "bvqa.asset-inspect/v1"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "config" / "class_gate_matrix_v1.json"
RC_OK, RC_USAGE, RC_INCOMPLETE, RC_STALE, RC_FAIL = 0, 2, 3, 4, 5
PPM = 1000000
POLICY_KEYS = ("closed", "tris_min", "tris_max", "degenerate_max", "duplicate_max", "nonmanifold_max",
               "twin_winding_max", "pinched_max", "reflection_max", "require_frozen", "scale_min_um", "scale_max_um",
               "pivot_y_tol_um", "pivot_xz_tol_um", "normals_required", "normal_len_tol_q16", "uv_required",
               "uv_degenerate_ppm_max", "uv_range_slack_q16", "uv_overlap_max", "materials_max",
               "unassigned_material_max", "skinned_allowed")
CHECK_IDS = ("geometry_present", "tris", "degenerate_faces", "duplicate_faces", "edge_multiplicity", "twin_winding",
             "pinched_vertices", "closure", "orientation_outward", "volume_nonzero", "reflection", "transforms_frozen",
             "scale_units", "pivot_y", "pivot_xz", "normals_present", "normals_unit", "uv_present", "uv_degenerate",
             "uv_range", "uv_overlap", "materials", "materials_assigned", "skinned")


class MatrixError(Exception):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc, self.reason = rc, reason


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def body_sha256(matrix):
    body = {k: v for k, v in matrix.items() if k != "signed"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_policy(matrix_path, cls, expect_sha=None):
    """(policy dict, body sha). Raises MatrixError with rc 2 (malformed), 3 (unsigned) or 4 (stale sha)."""
    try:
        matrix = json.loads(Path(matrix_path).read_text())
    except (OSError, ValueError) as exc:
        raise MatrixError(RC_USAGE, "matrix unreadable: %s" % exc)
    if not isinstance(matrix, dict) or matrix.get("schema") != "bvqa.class-gate-matrix/v1":
        raise MatrixError(RC_USAGE, "matrix schema is not bvqa.class-gate-matrix/v1")
    sha = body_sha256(matrix)
    signed = matrix.get("signed")
    if signed is None:
        raise MatrixError(RC_INCOMPLETE, "matrix unsigned (signed.body_sha256 ABSENT)")
    if not isinstance(signed, dict) or not isinstance(signed.get("body_sha256"), str):
        raise MatrixError(RC_USAGE, "matrix signed block malformed")
    if signed["body_sha256"] != sha:
        raise MatrixError(RC_STALE, "matrix body sha %s != signed %s" % (sha, signed["body_sha256"]))
    if expect_sha is not None and expect_sha != sha:
        raise MatrixError(RC_STALE, "matrix body sha %s != expected %s" % (sha, expect_sha))
    cls = matrix.get("aliases", {}).get(cls, cls)
    pol = matrix.get("classes", {}).get(cls)
    if not isinstance(pol, dict):
        raise MatrixError(RC_USAGE, "class %r not in matrix" % cls)
    unknown = sorted(set(pol) - set(POLICY_KEYS))
    missing = sorted(set(POLICY_KEYS) - set(pol))
    if unknown or missing:
        raise MatrixError(RC_USAGE, "class %s keys unknown=%s missing=%s" % (cls, unknown, missing))
    bad = sorted(k for k, v in pol.items() if type(v) is not int)
    if bad:
        raise MatrixError(RC_USAGE, "class %s non-integer values %s" % (cls, bad))
    return dict(pol, _class=cls), sha


# --------------------------------------------------------------------------- integer topology
def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def analyse_group(prims):
    """Topology of one node (all its primitives welded on the 1 um lattice). Exact integer arithmetic."""
    vid, pts, tris, degenerate = {}, [], [], 0
    for prim in prims:
        loc = []
        for p in glb_core.world_positions_um(prim):
            if p not in vid:
                vid[p] = len(pts)
                pts.append(p)
            loc.append(vid[p])
        idx = prim.indices
        for i in range(0, len(idx), 3):
            a, b, c = loc[idx[i]], loc[idx[i + 1]], loc[idx[i + 2]]
            if a == b or b == c or a == c or _cross(_sub(pts[b], pts[a]), _sub(pts[c], pts[a])) == (0, 0, 0):
                degenerate += 1
                continue
            tris.append((a, b, c))
    directed, undirected = {}, {}
    for t in tris:
        for i in range(3):
            a, b = t[i], t[(i + 1) % 3]
            directed[(a, b)] = directed.get((a, b), 0) + 1
            k = (min(a, b), max(a, b))
            undirected[k] = undirected.get(k, 0) + 1
    seen, dup = set(), 0
    for t in tris:
        k = tuple(sorted(t))
        dup += k in seen
        seen.add(k)
    # pinched vertex (VG-0232 D2-D3): the faces around a vertex must form ONE edge-connected fan
    vfaces = {}
    for fi, t in enumerate(tris):
        for v in t:
            vfaces.setdefault(v, []).append(fi)
    pinched = 0
    for v, fl in vfaces.items():
        parent = {f: f for f in fl}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        around = {}
        for f in fl:
            for u in tris[f]:
                if u != v:
                    around.setdefault(u, []).append(f)
        for fs in around.values():
            for f2 in fs[1:]:
                parent[find(f2)] = find(fs[0])
        pinched += len({find(f) for f in fl}) > 1
    vol6 = 0
    for t in tris:
        a, b, c = pts[t[0]], pts[t[1]], pts[t[2]]
        cr = _cross(b, c)
        vol6 += a[0] * cr[0] + a[1] * cr[1] + a[2] * cr[2]
    return {"tris": len(tris), "degenerate": degenerate, "duplicate": dup,
            "nonmanifold_edges": sum(1 for n in undirected.values() if n > 2),
            "twin_winding_edges": sum(1 for n in directed.values() if n > 1),
            "boundary_edges": sum(1 for n in undirected.values() if n == 1), "pinched": pinched,
            "volume6_um3": vol6, "vertices": len(pts), "edges": len(undirected)}


def _uv_tri_overlap(a, b):
    """Strict 2D SAT on integer q16 triangles: touching edges/vertices do NOT overlap."""
    for tri in (a, b):
        for i in range(3):
            p, q = tri[i], tri[(i + 1) % 3]
            nx, ny = p[1] - q[1], q[0] - p[0]
            if nx == 0 and ny == 0:
                continue
            pa = [nx * t[0] + ny * t[1] for t in a]
            pb = [nx * t[0] + ny * t[1] for t in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def uv_stats(prims, slack):
    tris_by_mat, degenerate, total, out_of_range, layouts = {}, 0, 0, 0, set()
    for prim in prims:
        if prim.uvs_q16 is None:
            continue
        # Instances (shared mesh, or baked clones of one) reuse one UV layout: pool each layout once per material.
        layout = (prim.material, tuple(prim.uvs_q16), tuple(prim.indices))
        if layout in layouts:
            continue
        layouts.add(layout)
        out_of_range += sum(1 for t in prim.uvs_q16 for c in t if c < -slack or c > glb_core.Q16 + slack)
        idx = prim.indices
        for i in range(0, len(idx), 3):
            a, b, c = (prim.uvs_q16[idx[i + k]] for k in range(3))
            total += 1
            if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) == 0:
                degenerate += 1
                continue
            tris_by_mat.setdefault(prim.material, []).append((a, b, c))
    overlaps = 0
    for tris in tris_by_mat.values():
        boxes = sorted((min(p[0] for p in t), max(p[0] for p in t), min(p[1] for p in t), max(p[1] for p in t), t)
                       for t in tris)
        for i, (u0, u1, v0, v1, t) in enumerate(boxes):
            for w0, w1, z0, z1, s in boxes[i + 1:]:
                if w0 >= u1:
                    break
                if z0 < v1 and v0 < z1 and _uv_tri_overlap(t, s):
                    overlaps += 1
    return {"uv_tris": total, "uv_degenerate": degenerate, "uv_out_of_range": out_of_range, "uv_overlap_pairs": overlaps}


# --------------------------------------------------------------------------- the gate
def inspect_scene(scene, pol):
    """(checks, metrics). checks == [] means zero checks executed (no triangles) -> rc 3."""
    prims = scene.prims
    n_raw = sum(len(p.indices) // 3 for p in prims)
    if not prims or n_raw == 0:
        return [], {"tris_raw": n_raw}
    groups = {}
    for p in prims:
        groups.setdefault(p.node_path, []).append(p)
    ga = {k: analyse_group(g) for k, g in sorted(groups.items())}
    tot = lambda k: sum(a[k] for a in ga.values())
    world = [w for p in prims for w in glb_core.world_positions_um(p)]
    lo = tuple(min(w[i] for w in world) for i in range(3))
    hi = tuple(max(w[i] for w in world) for i in range(3))
    dims = tuple(hi[i] - lo[i] for i in range(3))
    closed_groups = [a for a in ga.values() if a["boundary_edges"] == 0 and a["tris"]]
    uv = uv_stats(prims, pol["uv_range_slack_q16"])
    q16 = glb_core.Q16
    lo_len2, hi_len2 = (q16 - pol["normal_len_tol_q16"]) ** 2, (q16 + pol["normal_len_tol_q16"]) ** 2
    bad_normals = sum(1 for p in prims for n in (p.normals_q16 or ())
                      if not lo_len2 <= n[0] * n[0] + n[1] * n[1] + n[2] * n[2] <= hi_len2)
    mats = sorted({p.material for p in prims if p.material is not None})
    n_tris = tot("tris")
    metrics = {"tris": n_tris, "tris_raw": n_raw, "vertices_welded": tot("vertices"), "edges": tot("edges"),
               "boundary_edges": tot("boundary_edges"), "nodes_with_mesh": len(ga), "prims": len(prims),
               "bbox_min_um": list(lo), "bbox_max_um": list(hi), "dims_um": list(dims),
               "volume6_um3": tot("volume6_um3"), "materials": mats, **uv}
    checks = []

    def chk(cid, ok, value, limit):
        checks.append({"id": cid, "ok": bool(ok), "value": value, "limit": limit})

    chk("geometry_present", n_tris >= pol["tris_min"], n_tris, ">=%d" % pol["tris_min"])
    chk("tris", n_tris <= pol["tris_max"], n_tris, "<=%d" % pol["tris_max"])
    chk("degenerate_faces", tot("degenerate") <= pol["degenerate_max"], tot("degenerate"), "<=%d" % pol["degenerate_max"])
    chk("duplicate_faces", tot("duplicate") <= pol["duplicate_max"], tot("duplicate"), "<=%d" % pol["duplicate_max"])
    chk("edge_multiplicity", tot("nonmanifold_edges") <= pol["nonmanifold_max"], tot("nonmanifold_edges"),
        "<=%d" % pol["nonmanifold_max"])
    chk("twin_winding", tot("twin_winding_edges") <= pol["twin_winding_max"], tot("twin_winding_edges"),
        "<=%d" % pol["twin_winding_max"])
    chk("pinched_vertices", tot("pinched") <= pol["pinched_max"], tot("pinched"), "<=%d" % pol["pinched_max"])
    chk("closure", not pol["closed"] or tot("boundary_edges") == 0, tot("boundary_edges"),
        "==0" if pol["closed"] else "open class")
    inward = sum(1 for a in closed_groups if a["volume6_um3"] < 0)
    chk("orientation_outward", inward == 0, inward, "==0 closed nodes with volume<0")
    flat = sum(1 for a in closed_groups if a["volume6_um3"] == 0)
    chk("volume_nonzero", flat == 0 and (not pol["closed"] or bool(closed_groups)), flat, "==0 closed nodes with volume 0")
    reflected = sum(1 for p in prims if glb_core.det3_q48(p.world_q16) < 0)
    chk("reflection", reflected <= pol["reflection_max"], reflected, "<=%d" % pol["reflection_max"])
    unfrozen = sum(1 for p in prims if tuple(p.world_q16) != glb_core.IDENTITY_Q16)
    chk("transforms_frozen", not pol["require_frozen"] or unfrozen == 0, unfrozen, "==0" if pol["require_frozen"] else "off")
    chk("scale_units", pol["scale_min_um"] <= max(dims) <= pol["scale_max_um"], max(dims),
        "[%d,%d]" % (pol["scale_min_um"], pol["scale_max_um"]))
    chk("pivot_y", abs(lo[1]) <= pol["pivot_y_tol_um"], lo[1], "|v|<=%d" % pol["pivot_y_tol_um"])
    off2 = max(abs(lo[0] + hi[0]), abs(lo[2] + hi[2]))  # 2 * |bbox centre| on X and Z
    chk("pivot_xz", off2 <= 2 * pol["pivot_xz_tol_um"], off2, "2|c|<=%d" % (2 * pol["pivot_xz_tol_um"]))
    no_n = sum(1 for p in prims if p.normals_q16 is None)
    chk("normals_present", not pol["normals_required"] or no_n == 0, no_n, "==0" if pol["normals_required"] else "off")
    chk("normals_unit", bad_normals == 0, bad_normals, "==0 outside 1+-%d/65536" % pol["normal_len_tol_q16"])
    no_uv = sum(1 for p in prims if p.uvs_q16 is None)
    chk("uv_present", not pol["uv_required"] or no_uv == 0, no_uv, "==0" if pol["uv_required"] else "off")
    deg_ppm = uv["uv_degenerate"] * PPM // uv["uv_tris"] if uv["uv_tris"] else 0
    chk("uv_degenerate", deg_ppm <= pol["uv_degenerate_ppm_max"], deg_ppm, "<=%d ppm" % pol["uv_degenerate_ppm_max"])
    chk("uv_range", uv["uv_out_of_range"] == 0, uv["uv_out_of_range"], "==0 (slack %d q16)" % pol["uv_range_slack_q16"])
    chk("uv_overlap", uv["uv_overlap_pairs"] <= pol["uv_overlap_max"], uv["uv_overlap_pairs"], "<=%d" % pol["uv_overlap_max"])
    chk("materials", len(mats) <= pol["materials_max"], len(mats), "<=%d" % pol["materials_max"])
    unassigned = sum(1 for p in prims if p.material is None)
    chk("materials_assigned", unassigned <= pol["unassigned_material_max"], unassigned,
        "<=%d" % pol["unassigned_material_max"])
    skinned = sum(1 for p in prims if p.skinned)
    chk("skinned", pol["skinned_allowed"] or skinned == 0, skinned, "allowed" if pol["skinned_allowed"] else "==0")
    return checks, metrics


def run(data, cls, matrix_path=DEFAULT_MATRIX, expect_matrix_sha=None, prev_receipt=None):
    """(rc, receipt dict). `prev_receipt` is the raw bytes of the previous chain receipt (or None)."""
    receipt = {"schema": SCHEMA, "stage": "inspect", "class": cls, "input_sha256": glb_core.sha256(bytes(data)),
               "prev_receipt_sha256": glb_core.sha256(prev_receipt) if prev_receipt is not None else None,
               "matrix_path": Path(matrix_path).name, "matrix_sha256": None, "checks": [], "failed": [],
               "metrics": {}, "reason": None}

    def done(rc, verdict, reason=None):
        receipt.update(rc=rc, verdict=verdict, reason=reason)
        return rc, receipt
    try:
        pol, msha = load_policy(matrix_path, cls, expect_matrix_sha)
    except MatrixError as exc:
        return done(exc.rc, "STALE" if exc.rc == RC_STALE else "ERROR" if exc.rc == RC_USAGE else "INCOMPLETE",
                    exc.reason)
    receipt.update(matrix_sha256=msha, **{"class": pol["_class"]})
    try:
        scene = glb_core.parse_glb(bytes(data))
    except glb_core.GlbError as exc:
        return done(RC_USAGE, "ERROR", str(exc))
    checks, metrics = inspect_scene(scene, pol)
    receipt.update(checks=checks, metrics=metrics, failed=[c["id"] for c in checks if not c["ok"]])
    if not checks:
        return done(RC_INCOMPLETE, "INCOMPLETE", "zero checks executed (no triangles)")
    if receipt["failed"]:
        return done(RC_FAIL, "FAIL", "failed: " + ",".join(receipt["failed"]))
    return done(RC_OK, "PASS")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bvqa asset inspect")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--class", dest="cls")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--matrix-sha")
    ap.add_argument("--prev-receipt")
    ap.add_argument("--out")
    ap.add_argument("--print-body-sha", action="store_true")
    args = ap.parse_args(argv)
    if args.print_body_sha:
        try:
            print(body_sha256(json.loads(Path(args.matrix).read_text())))
        except (OSError, ValueError) as exc:
            print("matrix unreadable: %s" % exc, file=sys.stderr)
            return RC_USAGE
        return RC_OK
    if not args.inp or not args.cls:
        print("--in and --class are required", file=sys.stderr)
        return RC_USAGE
    try:
        data = Path(args.inp).read_bytes()
        prev = Path(args.prev_receipt).read_bytes() if args.prev_receipt else None
    except OSError as exc:
        print("E_IO: %s" % exc, file=sys.stderr)
        return RC_USAGE
    rc, receipt = run(data, args.cls, args.matrix, args.matrix_sha, prev)
    text = canon(receipt)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    sys.stdout.write(text)
    if rc:
        print(receipt["reason"], file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
