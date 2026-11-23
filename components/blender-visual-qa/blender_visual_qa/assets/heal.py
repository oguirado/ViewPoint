"""bv.assets.heal: bounded mesh healing driver (VG-0238, P <= 4 passes), stdlib host, integer math.

Five operators per pass, over every primitive (local um positions, as mesh_gate sees them):
  weld   duplicate vertices within --weld-um (Chebyshev, grid buckets, smallest index is the representative);
  cull   zero-area, sliver (altitude <= --weld-um over the longest edge) and duplicate faces;
  orient parity propagation across 2-face edges, then per closed component signed volume > 0 (sign flips with a
         reflecting world matrix, exactly like mesh_gate);
  fill   simple boundary loops of <= --max-hole-edges edges: fan from the first apex whose fan has no zero-area
         triangle, else a rounded-centroid fan (Euler delta chi = +1 per patch);
  stop   when a pass changes nothing, or after --max-passes (<= ASSET_HEAL_MAX_PASSES) passes.
Repair-or-regenerate: culled faces > ASSET_HEAL_CULL_MAX_PERMILLE of the input faces -> REGENERATE rc 5, nothing
emitted. The output is re-verified without trusting the driver: mesh_gate.gate_scene (rc 0 required) and an ortho4
silhouette IoU pre/post >= ASSET_HEAL_IOU_MIN_PPM over the 4 views.
UVs: --uv auto keeps the original corner UVs of every surviving face and places patch faces in a synthesized atlas
column to the right of max(u) (no overlap by construction); input without UVs (or --uv atlas) gets a full
per-triangle atlas. UV synthesis is NOT conformal (VG-0238 declares it AUSENTE). Normals are dropped.
--engine blender runs bpy_side/assets/heal_worker.py (bmesh remove_doubles/dissolve_degenerate/holes_fill/
recalc_face_normals) instead of the host operators; the same re-verification applies.
Exit codes: 0 healed | 2 usage/IO/worker error | 3 empty mesh | 5 REGENERATE or verify fail | 6 worker timeout.
Receipt schema bvqa.asset-heal/v1 (sorted canonical JSON; input_sha256, prev_sha256, artefact_sha256).
"""
# @module {"id":"bv.assets.heal","layer":4,"owner":"BASSETS"}
import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from blender_visual_qa.assets import glb_core, limits, mesh_gate, ortho4
from blender_visual_qa.drive import worker

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "bvqa.asset-heal/v1"
WORKER = ROOT / "bpy_side/assets/heal_worker.py"
BLENDER = "/usr/bin/blender"
PPM = 1000000
PERMILLE = 1000
MAX_PASSES = getattr(limits, "ASSET_HEAL_MAX_PASSES", 4)
CULL_MAX_PERMILLE = getattr(limits, "ASSET_HEAL_CULL_MAX_PERMILLE", 300)
WELD_UM = getattr(limits, "ASSET_HEAL_WELD_UM", 10)
IOU_MIN_PPM = getattr(limits, "ASSET_HEAL_IOU_MIN_PPM", 990000)
MAX_HOLE_EDGES = getattr(limits, "ASSET_HEAL_MAX_HOLE_EDGES", 64)
MIN_PASSES = 1
LOOP_MIN = 3
Q16 = glb_core.Q16
ATLAS_MARGIN_Q16 = Q16 // 256


class HealError(Exception):
    def __init__(self, rc, code, detail=""):
        super().__init__("%s: %s" % (code, detail))
        self.rc, self.code, self.detail = rc, code, detail


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _find(parent, x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


# --------------------------------------------------------------------------- operators
def weld(pos, tris, tol):
    """Merge vertices within tol (Chebyshev). Returns (tris remapped, merged_count, rep). Positions are never moved."""
    parent = list(range(len(pos)))
    cell = max(1, tol)
    grid = defaultdict(list)
    for i, p in enumerate(pos):
        grid[(p[0] // cell, p[1] // cell, p[2] // cell)].append(i)
    for (cx, cy, cz), members in grid.items():
        near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
                for j in grid.get((cx + dx, cy + dy, cz + dz), ())]
        for i in members:
            for j in near:
                if j > i and max(abs(pos[i][k] - pos[j][k]) for k in range(3)) <= tol:
                    ri, rj = _find(parent, i), _find(parent, j)
                    if ri != rj:
                        parent[max(ri, rj)] = min(ri, rj)
    rep = [_find(parent, i) for i in range(len(pos))]
    used = {v for t in tris for v in t}
    merged = sum(1 for v in used if rep[v] != v)
    return [tuple(rep[v] for v in t) for t in tris], merged, rep


def _is_sliver(pos, t, tol):
    a, b, c = (pos[v] for v in t)
    if len({a, b, c}) != len(t):
        return True
    cr = _cross(_sub(b, a), _sub(c, a))
    n2 = _dot(cr, cr)
    longest = max(_dot(e, e) for e in (_sub(b, a), _sub(c, b), _sub(a, c)))
    # altitude over the longest edge = |cross| / |longest|; sliver when altitude <= tol (squared, exact).
    return n2 <= tol * tol * longest


def cull(pos, tris, tol):
    """Drop zero-area/sliver faces and duplicate faces (same vertex set). Returns (kept, culled_count)."""
    kept, seen = [], set()
    for t in tris:
        if len(set(t)) != len(t) or _is_sliver(pos, t, tol):
            continue
        key = tuple(sorted(t))
        if key in seen:
            continue
        seen.add(key)
        kept.append(t)
    return kept, len(tris) - len(kept)


def _components(tris):
    parent = {v: v for t in tris for v in t}
    for t in tris:
        for v in t[1:]:
            ra, rb = _find(parent, t[0]), _find(parent, v)
            if ra != rb:
                parent[ra] = rb
    comp = defaultdict(list)
    for f, t in enumerate(tris):
        comp[_find(parent, t[0])].append(f)
    return [comp[k] for k in sorted(comp)]


def orient(pos, tris, reflect=False):
    """Consistent winding by parity propagation, then outward per closed component. Returns (tris, flipped)."""
    tris = list(tris)
    edge_faces = defaultdict(list)
    for f, t in enumerate(tris):
        for i in range(3):
            a, b = t[i], t[(i + 1) % 3]
            edge_faces[(min(a, b), max(a, b))].append(f)
    flip = [None] * len(tris)

    def directed(f, a, b):
        t = tris[f]
        fwd = any(t[i] == a and t[(i + 1) % 3] == b for i in range(3))
        return fwd != bool(flip[f])

    for seed in range(len(tris)):
        if flip[seed] is not None:
            continue
        flip[seed] = False
        stack = [seed]
        while stack:
            f = stack.pop()
            t = tris[f]
            for i in range(3):
                a, b = t[i], t[(i + 1) % 3]
                faces = edge_faces[(min(a, b), max(a, b))]
                if len(faces) != 2:
                    continue
                g = faces[0] if faces[1] == f else faces[1]
                if flip[g] is None:
                    flip[g] = False
                    # neighbour must traverse the shared edge the other way
                    flip[g] = directed(f, a, b) == directed(g, a, b)
                    stack.append(g)
    out = [(t[0], t[2], t[1]) if fl else t for t, fl in zip(tris, flip)]
    for faces in _components(out):
        und = defaultdict(int)
        for f in faces:
            t = out[f]
            for i in range(3):
                a, b = t[i], t[(i + 1) % 3]
                und[(min(a, b), max(a, b))] += 1
        if any(d != 2 for d in und.values()):
            continue
        vol6 = sum(_dot(pos[out[f][0]], _cross(pos[out[f][1]], pos[out[f][2]])) for f in faces)
        if (vol6 < 0) != bool(reflect):
            for f in faces:
                t = out[f]
                out[f] = (t[0], t[2], t[1])
                flip[f] = not flip[f]
    return out, sum(1 for fl in flip if fl)


def boundary_loops(tris):
    """Directed boundary edges (a->b with no b->a) traced into loops. Returns (simple_loops, non_simple_count)."""
    directed = set()
    for t in tris:
        for i in range(3):
            directed.add((t[i], t[(i + 1) % 3]))
    nxt = defaultdict(list)
    for a, b in sorted(directed):
        if (b, a) not in directed:
            nxt[a].append(b)
    used, loops, bad = set(), [], 0
    for start in sorted(nxt):
        for b0 in nxt[start]:
            if (start, b0) in used:
                continue
            loop, a, b, ok = [start], start, b0, True
            while True:
                used.add((a, b))
                if b == start:
                    break
                if b in loop or len(nxt.get(b, ())) != 1:
                    ok = False
                    break
                loop.append(b)
                a, b = b, nxt[b][0]
            if ok and len(loop) >= LOOP_MIN:
                loops.append(loop)
            else:
                bad += 1
    return loops, bad


def _nonzero(pos, t):
    return _cross(_sub(pos[t[1]], pos[t[0]]), _sub(pos[t[2]], pos[t[0]])) != (0, 0, 0)


def fill_loop(pos, loop):
    """Fan patch whose faces traverse each boundary edge reversed. Returns (new_tris, new_vertex_or_None)."""
    k = len(loop)
    for r in range(k):
        lp = loop[r:] + loop[:r]
        fan = [(lp[0], lp[i + 1], lp[i]) for i in range(1, k - 1)]
        if all(_nonzero(pos, t) for t in fan):
            return fan, None
    c = tuple((sum(pos[v][j] for v in loop) * 2 + k) // (2 * k) for j in range(3))
    ci = len(pos)
    pos.append(c)
    return [(ci, loop[(i + 1) % k], loop[i]) for i in range(k)], c


def fill(pos, tris, max_edges):
    loops, bad = boundary_loops(tris)
    new, patches, skipped, added = [], 0, bad, 0
    for loop in loops:
        if len(loop) > max_edges:
            skipped += 1
            continue
        fan, c = fill_loop(pos, loop)
        new += fan
        patches += 1
        added += c is not None
    return list(tris) + new, patches, skipped, added, len(new)


# --------------------------------------------------------------------------- driver
def heal_mesh(pos, tris, max_passes=MAX_PASSES, tol=WELD_UM, max_hole_edges=MAX_HOLE_EDGES, reflect=False):
    """Bounded driver over one mesh. pos is extended in place by centroid vertices.
    Returns (tris, stats, vmap) where vmap[i] is the surviving vertex of input vertex i."""
    tris = list(tris)
    vmap = list(range(len(pos)))
    stats = {"merged_vertices": 0, "culled_faces": 0, "flipped_faces": 0, "patches": 0, "patch_faces": 0,
             "centroid_vertices": 0, "loops_skipped": 0, "passes": 0, "converged": 0}
    for _ in range(max_passes):
        stats["passes"] += 1
        before = list(tris)
        tris, merged, rep = weld(pos, tris, tol)
        vmap = [rep[v] for v in vmap]
        tris, culled = cull(pos, tris, tol)
        tris, flipped = orient(pos, tris, reflect)
        tris, patches, skipped, added, nf = fill(pos, tris, max_hole_edges)
        stats["merged_vertices"] += merged
        stats["culled_faces"] += culled
        stats["flipped_faces"] += flipped
        stats["patches"] += patches
        stats["patch_faces"] += nf
        stats["centroid_vertices"] += added
        stats["loops_skipped"] = skipped
        if tris == before:
            stats["converged"] = 1
            break
    return tris, stats, vmap


def map_vertices(orig, new, used, tol):
    """vmap for an engine that does not report its weld: input vertex -> an output vertex within tol (or None)."""
    cell = max(1, tol)
    grid = defaultdict(list)
    for j in sorted(used):
        p = new[j]
        grid[(p[0] // cell, p[1] // cell, p[2] // cell)].append(j)
    vmap = []
    for p in orig:
        c = (p[0] // cell, p[1] // cell, p[2] // cell)
        hit = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in grid.get((c[0] + dx, c[1] + dy, c[2] + dz), ()):
                        if max(abs(p[k] - new[j][k]) for k in range(3)) <= tol and (hit is None or j < hit):
                            hit = j
        vmap.append(hit)
    return vmap


def uv_lookup(prim, vmap):
    """Original corner UVs keyed by the surviving vertex set of each original face: {frozenset: {vertex: uv}}."""
    uvs = prim.uvs_q16
    if uvs is None or len(uvs) != len(prim.positions_um):
        return {}
    out = {}
    idx = list(prim.indices)
    for f in range(len(idx) // 3):
        corners = idx[3 * f:3 * f + 3]
        vs = [vmap[i] for i in corners]
        if None in vs or len(set(vs)) != 3:
            continue
        out.setdefault(frozenset(vs), {v: tuple(uvs[i]) for v, i in zip(vs, corners)})
    return out


def _atlas_uv(slot, n, u0):
    """Right triangle in cell `slot` of an n x n grid of Q16 cells, shifted to start at u0."""
    cx, cy = slot % n, slot // n
    size = Q16 // n
    m = min(ATLAS_MARGIN_Q16, size // 8)
    x0, y0, s = u0 + cx * size + m, cy * size + m, size - 2 * m
    return ((x0, y0), (x0 + s, y0), (x0, y0 + s))


def _isqrt_ceil(n):
    r = 1
    while r * r < n:
        r += 1
    return r


def build_prim(prim, pos, tris, lookup):
    """Unshared corners (position + UV) so original seams survive; mesh_gate welds positions back.
    Faces found in `lookup` keep their original corner UVs; every other face gets an atlas cell."""
    P, I, U = [], [], []
    plan = [(t, lookup.get(frozenset(t))) for t in tris]
    fresh = [k for k, (_, src) in enumerate(plan) if src is None]
    n = _isqrt_ceil(max(1, len(fresh)))
    kept_u = [uv[0] for _, src in plan if src is not None for uv in src.values()]
    u0 = 0 if not kept_u else ((max(kept_u) + Q16) // Q16) * Q16
    slots = {k: s for s, k in enumerate(fresh)}
    for k, (t, src) in enumerate(plan):
        uv3 = [src[v] for v in t] if src is not None else _atlas_uv(slots[k], n, u0)
        for v, uv in zip(t, uv3):
            I.append(len(P))
            P.append(tuple(pos[v]))
            U.append(tuple(uv))
    return glb_core.Prim(name=prim.name, node_path=prim.node_path, world_q16=tuple(prim.world_q16), positions_um=P,
                         indices=I, uvs_q16=U, normals_q16=None, material=prim.material, skinned=False,
                         mesh_id=-1), len(fresh)


def silhouette_iou_ppm(before, after, res=ortho4.RES):
    """Intersection over union of the 4 ortho silhouettes on one shared frame (None when flat/empty)."""
    bt, at = ortho4.scene_triangles(before), ortho4.scene_triangles(after)
    c2, ext = ortho4.frame(bt + at)
    if not ext:
        return None
    inter = union = 0
    for v in ortho4.VIEWS:
        b = ortho4.rasterise(bt, v, c2, ext, res)
        a = ortho4.rasterise(at, v, c2, ext, res)
        inter += ortho4.popcount([x & y for x, y in zip(b, a)])
        union += ortho4.popcount([x | y for x, y in zip(b, a)])
    return inter * PPM // max(1, union)


# --------------------------------------------------------------------------- blender engine
def default_runner(argv, timeout):
    return worker.asset(argv, timeout, input_flags=("--mesh",))


def worker_argv(blender, mesh_json, tol, max_hole_edges, max_passes):
    return [blender, "-b", "--factory-startup", "-noaudio", "--python", str(WORKER), "--", "--mesh", str(mesh_json),
            "--weld-um", str(tol), "--max-hole-edges", str(max_hole_edges), "--max-passes", str(max_passes)]


def heal_blender(pos, tris, a, runner=None, reflect=False):
    runner = runner or default_runner
    with tempfile.TemporaryDirectory(prefix="bvqa_heal_") as tmp:
        mj = os.path.join(tmp, "mesh.json")
        with open(mj, "w") as fh:
            json.dump({"positions_um": [list(p) for p in pos], "tris": [list(t) for t in tris],
                       "reflect": int(bool(reflect))}, fh)
        try:
            rc, out, err = runner(worker_argv(a.blender, mj, a.weld_um, a.max_hole_edges, a.max_passes), a.timeout)
        except FileNotFoundError:
            raise HealError(limits.RC_USAGE, "E_BLENDER", "blender not found: %s" % a.blender)
    if rc:
        raise HealError(limits.RC_USAGE, "E_BLENDER", "worker rc %s: %s" % (rc, (out[-300:] + err[-300:]).replace("\n", " ")))
    for line in out.splitlines():
        if line.startswith("BVQA_HEAL "):
            rec = json.loads(line[len("BVQA_HEAL "):])
            npos = [tuple(int(c) for c in p) for p in rec["positions_um"]]
            ntris = [tuple(int(i) for i in t) for t in rec["tris"]]
            if any(len(t) != 3 or not all(0 <= i < len(npos) for i in t) for t in ntris):
                raise HealError(limits.RC_USAGE, "E_BLENDER", "worker returned an index outside the vertex set")
            return npos, ntris, dict(rec.get("stats", {}))
    raise HealError(limits.RC_USAGE, "E_BLENDER", "no BVQA_HEAL line")


# --------------------------------------------------------------------------- CLI
def _receipt(rc, body, input_sha, prev):
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": input_sha, "prev_sha256": prev})
    return rec


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise HealError(limits.RC_USAGE, "E_IO", str(exc))


def heal_scene(scene, a, runner=None):
    """Heal every primitive. Returns (rc, body, healed_scene_or_None)."""
    out_prims, per, total_in, total_cull = [], [], 0, 0
    for prim in scene.prims:
        idx = list(prim.indices)
        if not idx:
            continue
        pos = [tuple(p) for p in prim.positions_um]
        tris = [tuple(idx[3 * f:3 * f + 3]) for f in range(len(idx) // 3)]
        reflect = glb_core.det3_q48(prim.world_q16) < 0
        if a.engine == "blender":
            pos, htris, stats = heal_blender(pos, tris, a, runner, reflect)
            vmap = map_vertices([tuple(p) for p in prim.positions_um], pos, {v for t in htris for v in t}, a.weld_um)
        else:
            htris, stats, vmap = heal_mesh(pos, tris, a.max_passes, a.weld_um, a.max_hole_edges, reflect)
        lookup = uv_lookup(prim, vmap) if a.uv == "auto" else {}
        total_in += len(tris)
        total_cull += int(stats.get("culled_faces", 0))
        np_, synth = build_prim(prim, pos, htris, lookup)
        stats.update({"name": str(prim.name), "tris_in": len(tris), "tris_out": len(htris), "uv_synth_faces": synth})
        per.append(stats)
        out_prims.append(np_)
    if not per:
        return limits.RC_INCOMPLETE, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "empty mesh: nothing to heal"}, None
    cull_pm = total_cull * PERMILLE // max(1, total_in)
    body = {"engine": a.engine, "prims": per, "cull_permille": cull_pm, "cull_max_permille": a.cull_max_permille,
            "max_passes": a.max_passes, "weld_um": a.weld_um, "max_hole_edges": a.max_hole_edges, "uv": a.uv}
    if cull_pm > a.cull_max_permille:
        body.update({"status": "REGENERATE", "failed": ["cull_budget"],
                     "reason": "culled %d permille of input faces > %d" % (cull_pm, a.cull_max_permille)})
        return limits.RC_FAIL, body, None
    return limits.RC_OK, body, glb_core.Scene(prims=out_prims)


def _run(a, runner):
    prev = hashlib.sha256(_read(a.prev_receipt)).hexdigest() if a.prev_receipt else ""
    raw = _read(a.inp)
    input_sha = hashlib.sha256(raw).hexdigest()
    try:
        scene = glb_core.parse_glb(raw)
    except glb_core.GlbError as exc:
        raise HealError(limits.RC_USAGE, exc.code, exc.detail)
    rc, body, healed = heal_scene(scene, a, runner)
    if healed is None:
        return rc, body, input_sha, prev
    data = glb_core.write_glb(healed)
    reparsed = glb_core.parse_glb(data)
    grc, gbody = mesh_gate.gate_scene(reparsed, a.max_genus, False, True, False)
    iou = silhouette_iou_ppm(scene, reparsed)
    failed = ["mesh_gate:" + f for f in gbody.get("failed", [])]
    if grc != limits.RC_OK and not failed:
        failed.append("mesh_gate:rc")
    if iou is None or iou < a.iou_min_ppm:
        failed.append("silhouette_iou")
    body.update({"mesh_gate_rc": grc, "mesh_gate_failed": gbody.get("failed", []), "silhouette_iou_ppm": iou,
                 "iou_min_ppm": a.iou_min_ppm, "failed": failed})
    if failed:
        body.update({"status": "REGENERATE", "reason": "healed mesh does not verify; nothing emitted"})
        return limits.RC_FAIL, body, input_sha, prev
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "wb") as fh:
        fh.write(data)
    body.update({"status": "PASS", "artefact": os.path.basename(a.out), "artefact_sha256": hashlib.sha256(data).hexdigest()})
    return limits.RC_OK, body, input_sha, prev


# @anchor:bv.assets.heal.run
def run(argv, runner=None):
    """CLI core. Returns (rc, receipt_or_None). main() prints the canonical receipt."""
    ap = argparse.ArgumentParser(prog="heal", description="bounded mesh healing (VG-0238) + mesh-gate re-verification")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", choices=("host", "blender"), default="host")
    ap.add_argument("--max-passes", type=int, default=MAX_PASSES)
    ap.add_argument("--weld-um", type=int, default=WELD_UM)
    ap.add_argument("--max-hole-edges", type=int, default=MAX_HOLE_EDGES)
    ap.add_argument("--cull-max-permille", type=int, default=CULL_MAX_PERMILLE)
    ap.add_argument("--iou-min-ppm", type=int, default=IOU_MIN_PPM)
    ap.add_argument("--max-genus", type=int, default=0)
    ap.add_argument("--uv", choices=("auto", "atlas"), default="auto")
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--prev-receipt", default=None)
    ap.add_argument("--receipt", default=None, help="receipt path (default: heal_receipt.json next to --out)")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return limits.RC_USAGE, None
    if not MIN_PASSES <= a.max_passes <= MAX_PASSES or a.weld_um < 0 or a.max_hole_edges < LOOP_MIN \
            or not 0 <= a.cull_max_permille <= PERMILLE or not 0 <= a.iou_min_ppm <= PPM:
        return limits.RC_USAGE, _receipt(limits.RC_USAGE, {"status": "E_USAGE",
                                                           "reason": "pass budget outside 1..P, or bad tolerance"}, None, "")
    try:
        rc, body, input_sha, prev = _run(a, runner)
    except HealError as exc:
        rc, body, input_sha, prev = exc.rc, {"status": exc.code, "reason": exc.detail}, None, ""
    rec = _receipt(rc, body, input_sha, prev)
    dest = a.receipt or os.path.join(os.path.dirname(os.path.abspath(a.out)), "heal_receipt.json")
    if "containment:" not in (body.get("reason") or ""):
        worker.publish(dest, canon(rec).encode())
    return rc, rec


def main(argv=None):
    rc, rec = run(sys.argv[1:] if argv is None else argv)
    if rec is not None:
        sys.stdout.write(canon(rec))
    return rc


if __name__ == "__main__":
    sys.exit(main())
