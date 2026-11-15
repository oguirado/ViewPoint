"""bv.assets.hull_gen: bounded convex collision hull + independent integer re-parse verification (stdlib host).

SPLIT port of scr-blender-hull-gen (router §3.6). The host selects the visual mesh (the `<prefix>_LOD<k>` nodes of a
ladder for --lod, else every non-helper mesh), hands its distinct world points (um) to the Blender worker
`bpy_side/assets/hull_bmesh.py` (bmesh.ops.convex_hull, greedy inner approximation above the budget), and writes the
returned triangles (input-point indices, so the hull vertices are exact source points) as node `<prefix>-convcol`.
The emitted GLB is then re-parsed and verified without trusting the worker:
  single_hull_node | convcol_name | vertices_range 4 <= V <= max | faces_range 4 <= F <= max |
  volume_nonzero (V > 1e-6 diag^3) | watertight (each edge in 2 faces, chi = 2) | outward (signed volume > 0) |
  convex (no vertex in front of a face plane beyond 1 um + diag ppm) | support (every source point within tau_ppm*diag).
Helpers (`_collision`, `-col`, `-convcol`, `-colonly`, `-convcolonly`) never receive a hull: such an input is refused
with rc 5 and nothing is emitted. Metric `containment_ppm` = source ortho4 silhouette cells outside the hull's.
Exit codes: 0 pass | 2 usage/IO/worker error | 3 no visual mesh / hull GLB without mesh | 5 gate fail | 6 timeout.
Receipt schema bvqa.asset-hull-gen/v1 (sorted canonical JSON; input_sha256, prev_sha256, artefact_sha256).
"""
# @module {"id":"bv.assets.hull_gen","layer":4,"owner":"BASSETS"}
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from blender_visual_qa.assets import glb_core, limits, ortho4

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "bvqa.asset-hull-gen/v1"
WORKER = ROOT / "bpy_side/assets/hull_bmesh.py"
BLENDER = "/usr/bin/blender"
PPM = 1000000
MIN_VERTS = 4
MIN_FACES = 4
MAX_VERTS = getattr(limits, "ASSET_HULL_MAX_VERTS", 32)
MAX_FACES = getattr(limits, "ASSET_HULL_MAX_FACES", 60)
TAU_PPM = getattr(limits, "ASSET_HULL_TAU_PPM", 20000)
SUPPORT_SAMPLES = 3000
SUFFIX = "-convcol"
HELPER_RE = re.compile(r"(_collision|-col|-convcol|-colonly|-convcolonly)(\.\d+)?$", re.IGNORECASE)
LOD_RE = re.compile(r"^(?P<prefix>.+)_LOD(?P<k>[0-2])(\.\d+)?$")


class HullError(Exception):
    def __init__(self, rc, code, detail=""):
        super().__init__("%s: %s" % (code, detail))
        self.rc, self.code, self.detail = rc, code, detail


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def is_helper(name):
    return bool(HELPER_RE.search(name or ""))


def select_parts(scene, lod):
    vis = [p for p in scene.prims if not is_helper(p.name)]
    if lod != "ALL" and any(LOD_RE.match(p.name) for p in vis):
        vis = [p for p in vis if (lambda m: bool(m) and "LOD" + m.group("k") == lod)(LOD_RE.match(p.name))]
    return vis


def source_points(parts):
    return sorted({q for p in parts for q in glb_core.world_positions_um(p)})


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _diag(pts):
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return math.isqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3)))


def _pt_tri(p, a, b, c):
    """Exact-geometry distance (float, um) from point p to triangle abc (Ericson 5.1.5)."""
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return math.sqrt(_dot(ap, ap))
    bp = _sub(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return math.sqrt(_dot(bp, bp))
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3)
        q = tuple(a[i] + v * ab[i] for i in range(3))
        return math.dist(p, q)
    cp = _sub(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return math.sqrt(_dot(cp, cp))
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6)
        q = tuple(a[i] + w * ac[i] for i in range(3))
        return math.dist(p, q)
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        q = tuple(b[i] + w * (c[i] - b[i]) for i in range(3))
        return math.dist(p, q)
    den = va + vb + vc
    q = tuple(a[i] + (ab[i] * vb + ac[i] * vc) / den for i in range(3))
    return math.dist(p, q)


def verify_hull(hull_scene, src_pts, max_verts=MAX_VERTS, max_faces=MAX_FACES, tau_ppm=TAU_PPM, hull_node=None):
    """Independent checks over a parsed hull GLB. Returns (checks, metrics) or (None, metrics) when it holds no mesh."""
    prims = hull_scene.prims
    present = sorted({p.name for p in prims})
    if hull_node:
        sel = [p for p in prims if p.name == hull_node]
    else:
        sel = [p for p in prims if p.name.lower().endswith(SUFFIX)] or prims
    metrics = {"nodes_present": present, "hull_node_selected": sorted({p.name for p in sel})}
    if not sel:
        return None, metrics
    checks = []

    def chk(cid, ok, value, limit):
        checks.append({"id": cid, "ok": bool(ok), "value": value, "limit": limit})

    names = sorted({p.node_path for p in sel})
    chk("single_hull_node", len(names) == 1, names, 1)
    chk("convcol_name", all(p.name.lower().endswith(SUFFIX) for p in sel), metrics["hull_node_selected"], "*" + SUFFIX)
    vid, pos, tris = {}, [], []
    for p in sel:
        loc = []
        for q in glb_core.world_positions_um(p):
            if q not in vid:
                vid[q] = len(pos)
                pos.append(q)
            loc.append(vid[q])
        for i in range(0, len(p.indices) - len(p.indices) % 3, 3):
            t = (loc[p.indices[i]], loc[p.indices[i + 1]], loc[p.indices[i + 2]])
            if len(set(t)) == 3:
                tris.append(t)
    nv, nf = len(pos), len(tris)
    chk("vertices_range", MIN_VERTS <= nv <= max_verts, nv, [MIN_VERTS, max_verts])
    chk("faces_range", MIN_FACES <= nf <= max_faces, nf, [MIN_FACES, max_faces])
    if not pos:
        return checks, metrics
    diag = _diag(src_pts) if src_pts else _diag(pos)
    vol6 = sum(_dot(pos[a], _cross(pos[b], pos[c])) for a, b, c in tris)
    # |V| > 1e-6 * diag^3  <=>  |6V| * PPM > 6 * diag^3
    chk("volume_nonzero", abs(vol6) * PPM > 6 * diag ** 3, vol6 // 6, "> diag^3/%d" % PPM)
    edges = {}
    for a, b, c in tris:
        for i, j in ((a, b), (b, c), (c, a)):
            k = (i, j) if i < j else (j, i)
            edges[k] = edges.get(k, 0) + 1
    ne = len(edges)
    chi = nv - ne + nf
    chk("watertight", bool(edges) and all(n == 2 for n in edges.values()) and chi == 2,
        {"V": nv, "E": ne, "F": nf, "chi": chi}, "chi=2, every edge in 2 faces")
    chk("outward", vol6 > 0, vol6 // 6, "> 0")
    planes = []
    for a, b, c in tris:
        n = _cross(_sub(pos[b], pos[a]), _sub(pos[c], pos[a]))
        n2 = _dot(n, n)
        if n2:
            planes.append((n, _dot(n, pos[a]), math.isqrt(n2) or 1))
    tol_um = 1 + diag // PPM
    worst = 0
    for n, d, ln in planes:
        for p in pos:
            front = _dot(n, p) - d
            if front > 0:
                worst = max(worst, -(-front // ln))
    chk("convex", bool(planes) and worst <= tol_um, worst, tol_um)
    support = None
    if src_pts and planes:
        step = max(1, len(src_pts) // SUPPORT_SAMPLES)
        support = 0
        for p in src_pts[::step]:
            if all(_dot(n, p) - d <= 0 for n, d, _ in planes):
                continue
            dist = min(_pt_tri(p, pos[a], pos[b], pos[c]) for a, b, c in tris)
            support = max(support, int(math.ceil(dist)))
        limit = tau_ppm * diag // PPM
        chk("support", support <= limit, support, limit)
    metrics.update({"V": nv, "F": nf, "volume_um3": vol6 // 6, "diag_um": diag, "support_um": support,
                    "convex_front_um": worst})
    return checks, metrics


def containment_ppm(src_parts, hull_scene, res=ortho4.RES):
    """Source silhouette cells not covered by the hull silhouette over the 4 ortho views (ppm of source cells)."""
    st = [t for p in src_parts for t in ortho4.scene_triangles(glb_core.Scene(prims=[p]))]
    ht = ortho4.scene_triangles(hull_scene)
    c2, ext = ortho4.frame(st + ht)
    if not ext:
        return None
    out = occ = 0
    for v in ortho4.VIEWS:
        s = ortho4.rasterise(st, v, c2, ext, res)
        h = ortho4.rasterise(ht, v, c2, ext, res)
        occ += ortho4.popcount(s)
        out += ortho4.popcount([a & ~b for a, b in zip(s, h)])
    return out * PPM // max(1, occ)


def default_runner(argv, timeout):
    """Run one Blender subprocess -> (rc, stdout, stderr). Own process group, killed on timeout."""
    import signal
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise HullError(limits.RC_BUDGET, "E_TIMEOUT", "blender worker")
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return proc.returncode, out, err


def worker_argv(blender, points_json, max_verts, max_faces):
    return [blender, "-b", "--factory-startup", "-noaudio", "--python", str(WORKER), "--",
            "--points", str(points_json), "--max-verts", str(max_verts), "--max-faces", str(max_faces)]


def generate(pts, max_verts, max_faces, runner=None, blender=BLENDER, timeout=300):
    """Worker call -> triangles of indices into pts. Raises HullError."""
    runner = runner or default_runner
    with tempfile.TemporaryDirectory(prefix="bvqa_hull_") as tmp:
        pj = os.path.join(tmp, "points.json")
        with open(pj, "w") as fh:
            json.dump({"points_um": [list(p) for p in pts]}, fh)
        try:
            rc, out, err = runner(worker_argv(blender, pj, max_verts, max_faces), timeout)
        except FileNotFoundError:
            raise HullError(limits.RC_USAGE, "E_BLENDER", "blender not found: %s" % blender)
    if rc:
        raise HullError(limits.RC_USAGE, "E_BLENDER", "worker rc %s: %s" % (rc, (out[-300:] + err[-300:]).replace("\n", " ")))
    for line in out.splitlines():
        if line.startswith("BVQA_HULL "):
            rec = json.loads(line[len("BVQA_HULL "):])
            if "error" in rec:
                raise HullError(limits.RC_FAIL, "E_DEGENERATE", rec["error"])
            tris = [tuple(t) for t in rec["tris"]]
            if any(len(t) != 3 or not all(0 <= i < len(pts) for i in t) for t in tris):
                raise HullError(limits.RC_USAGE, "E_BLENDER", "worker returned an index outside the point set")
            return tris
    raise HullError(limits.RC_USAGE, "E_BLENDER", "no BVQA_HULL line")


def hull_scene(pts, tris, name):
    used = sorted({i for t in tris for i in t})
    remap = {old: k for k, old in enumerate(used)}
    prim = glb_core.Prim(name=name, node_path="/" + name, world_q16=glb_core.IDENTITY_Q16,
                         positions_um=[pts[i] for i in used], indices=[remap[i] for t in tris for i in t])
    return glb_core.Scene(prims=[prim])


def _receipt(rc, body, input_sha, prev):
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": input_sha, "prev_sha256": prev})
    return rec


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise HullError(limits.RC_USAGE, "E_IO", str(exc))


def _parse(raw):
    try:
        return glb_core.parse_glb(raw)
    except glb_core.GlbError as exc:
        raise HullError(limits.RC_USAGE, exc.code, exc.detail)


def _run(a, runner):
    prev = hashlib.sha256(_read(a.prev_receipt)).hexdigest() if a.prev_receipt else ""
    src_parts, src_pts, input_sha = [], None, None
    if a.verify_only:
        if not a.hull:
            raise HullError(limits.RC_USAGE, "E_USAGE", "--verify-only needs --hull")
        if a.source:
            raw = _read(a.source)
            input_sha = hashlib.sha256(raw).hexdigest()
            src_parts = select_parts(_parse(raw), a.lod)
            src_pts = source_points(src_parts)
        hull_path = a.hull
        node = None
    else:
        if not a.inp or not a.out:
            raise HullError(limits.RC_USAGE, "E_USAGE", "--in and --out required")
        raw = _read(a.inp)
        input_sha = hashlib.sha256(raw).hexdigest()
        scene = _parse(raw)
        if not scene.prims:
            return limits.RC_INCOMPLETE, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "no mesh in input"}, input_sha, prev
        stem = os.path.splitext(os.path.basename(a.inp))[0]
        if is_helper(stem) or not any(not is_helper(p.name) for p in scene.prims):
            return limits.RC_FAIL, {"status": "FAIL", "failed": ["collision_helper_input"],
                                    "reason": "collision helpers never receive a hull; nothing emitted"}, input_sha, prev
        src_parts = select_parts(scene, a.lod)
        if not src_parts:
            return limits.RC_INCOMPLETE, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "no visual mesh for " + a.lod}, input_sha, prev
        src_pts = source_points(src_parts)
        m = LOD_RE.match(src_parts[0].name)
        prefix = m.group("prefix") if m else re.sub(r"_LOD[0-2]$", "", stem)
        node = prefix + SUFFIX
        try:
            tris = generate(src_pts, a.max_verts, a.max_faces, runner, a.blender, a.timeout)
        except HullError as exc:
            if exc.rc == limits.RC_FAIL:
                return exc.rc, {"status": "FAIL", "failed": ["hull_degenerate"], "reason": exc.detail}, input_sha, prev
            raise
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "wb") as fh:
            fh.write(glb_core.write_glb(hull_scene(src_pts, tris, node)))
        hull_path = a.out
    hraw = _read(hull_path)
    hscene = _parse(hraw)
    checks, metrics = verify_hull(hscene, src_pts, a.max_verts, a.max_faces, a.tau_ppm, a.hull_node or None)
    if checks is None:
        return limits.RC_INCOMPLETE, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "hull GLB holds no mesh",
                                      "metrics": metrics}, input_sha, prev
    if src_parts:
        metrics["containment_ppm"] = containment_ppm(src_parts, hscene)
    failed = [c["id"] for c in checks if not c["ok"]]
    body = {"status": "FAIL" if failed else "PASS", "checks": checks, "failed": failed, "metrics": metrics,
            "artefact": os.path.basename(hull_path), "artefact_sha256": hashlib.sha256(hraw).hexdigest(),
            "node": node, "lod": a.lod, "tau_ppm": a.tau_ppm, "max_verts": a.max_verts, "max_faces": a.max_faces}
    return (limits.RC_FAIL if failed else limits.RC_OK), body, input_sha, prev


# @anchor:bv.assets.hull_gen.run
def run(argv, runner=None):
    """CLI core. Returns (rc, receipt_or_None). main() prints the canonical receipt."""
    ap = argparse.ArgumentParser(prog="hull-gen", description="bounded convex collision hull + re-parse verification")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out")
    ap.add_argument("--lod", default="LOD2")
    ap.add_argument("--max-verts", type=int, default=MAX_VERTS)
    ap.add_argument("--max-faces", type=int, default=MAX_FACES)
    ap.add_argument("--tau-ppm", type=int, default=TAU_PPM)
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--hull")
    ap.add_argument("--hull-node", default="")
    ap.add_argument("--source")
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--prev-receipt", default=None)
    ap.add_argument("--receipt", default=None, help="receipt path (default: hull_receipt.json next to --out)")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return limits.RC_USAGE, None
    if a.max_verts < MIN_VERTS or a.max_faces < MIN_FACES or a.tau_ppm < 0:
        return limits.RC_USAGE, _receipt(limits.RC_USAGE, {"status": "E_USAGE", "reason": "budget below 4 or tau < 0"}, None, "")
    try:
        rc, body, input_sha, prev = _run(a, runner)
    except HullError as exc:
        rc, body, input_sha, prev = exc.rc, {"status": exc.code, "reason": exc.detail}, None, ""
    rec = _receipt(rc, body, input_sha, prev)
    dest = a.receipt or (os.path.join(os.path.dirname(os.path.abspath(a.out)), "hull_receipt.json")
                         if a.out and not a.verify_only else None)
    if dest:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with open(dest, "w") as fh:
            fh.write(canon(rec))
    return rc, rec


def main(argv=None):
    rc, rec = run(sys.argv[1:] if argv is None else argv)
    if rec is not None:
        sys.stdout.write(canon(rec))
    return rc


if __name__ == "__main__":
    sys.exit(main())
