"""LOD ladder: Blender DECIMATE COLLAPSE <P>_LOD0..2 + independent host re-parse verification (router §3.5).

  bvqa asset lod-ladder --in <norm.glb> --out-dir <dir> --class <c> --inspected <inspect_receipt.json>
                        [--prev-receipt R] [--blender B] [--timeout S]
  bvqa asset lod-ladder --verify-only --lods <ladder.glb> --class <c> [--source <norm.glb>] [--inspected R]

Budget per prefix P with f0 source triangles: cap_k = B(f0, p_k) = min(f0, max((f0*p_k + 500)//1000, FLOOR)).
Transition k (n_{k-1} -> n_k) is required when f0 > FLOOR and n_{k-1} > cap_k; then n_k < n_{k-1}, n_k <= cap_k,
n_k >= min(FLOOR, cap_k)*FILL/1000 and the welded geometry sha256 differs from LOD_{k-1}. Otherwise n_k <= n_{k-1}
(equality legal, flagged floor_preserved / lod_saturated). Fidelity: bidirectional TriGrid Hausdorff (sampled
vertices + centroids, point-to-triangle) max(h_f, h_b)/diag <= bound_k (ppm). No helper (*_collision, -col,
-convcol) LOD nodes; hull nodes are verified by hull_gen, not here. The worker's own report is never trusted.
Exit: 0 OK | 2 usage/IO/schema | 3 incomplete (receipt not PASS, skinned, no mesh) | 4 stale | 5 gate fail | 6 budget.
"""
# @module {"id":"bv.assets.lod_ladder","layer":4,"owner":"BASSETS"}
import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from blender_visual_qa.assets import glb_core, inspect_gate, limits

SCHEMA = "bvqa.asset-lod-ladder/v1"
ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "bpy_side/assets/export_runtime.py"
BLENDER = "/usr/bin/blender"
RC_OK, RC_USAGE, RC_INCOMPLETE, RC_STALE, RC_FAIL, RC_BUDGET = (limits.RC_OK, limits.RC_USAGE, limits.RC_INCOMPLETE,
                                                                limits.RC_STALE, limits.RC_FAIL, limits.RC_BUDGET)
PPM = 1000000
PERMILLE = 1000

# Named policy (REG-REQ on bus_M2; INT registers them in limits.py, the fallbacks are the requested values).
FLOOR = getattr(limits, "ASSET_LOD_FLOOR_TRIS", 120)
RATIO_PERMILLE = (PERMILLE, getattr(limits, "ASSET_LOD1_PERMILLE", 500), getattr(limits, "ASSET_LOD2_PERMILLE", 250))
HAUSDORFF_PPM = (0, getattr(limits, "ASSET_LOD1_HAUSDORFF_PPM", 20000), getattr(limits, "ASSET_LOD2_HAUSDORFF_PPM", 50000))
FILL_PERMILLE = getattr(limits, "ASSET_LOD_FILL_PERMILLE", 900)
SAMPLES = getattr(limits, "ASSET_LOD_SAMPLES", 4000)
TIMEOUT_S = 600
GRID_CELLS = 24

LOD_RE = re.compile(r"^(?P<prefix>.+)_LOD(?P<k>[0-9]+)(\.\d+)?$")
HELPER_RE = re.compile(r"(_collision|-col|-convcol)$")
PART_RE = re.compile(r"\.\d+$")
LODS = (0, 1, 2)


class LodError(Exception):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc, self.reason = rc, reason


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def budget(f0, permille, floor=None):
    """B(f0, p) = min(f0, max(floor((f0*p + 500)/1000), FLOOR))."""
    floor = FLOOR if floor is None else floor
    return min(f0, max((f0 * permille + PERMILLE // 2) // PERMILLE, floor))


def caps_for(f0):
    return [budget(f0, p) for p in RATIO_PERMILLE]


# --------------------------------------------------------------------------- independent geometry
def node_base(name):
    return PART_RE.sub("", name)


def tri_soup(prims):
    """World points (um ints) and triangle index triples over a list of prims."""
    pts, tris = [], []
    for p in prims:
        base = len(pts)
        pts.extend(glb_core.world_positions_um(p))
        ix = p.indices
        tris.extend((base + ix[i], base + ix[i + 1], base + ix[i + 2]) for i in range(0, len(ix) - len(ix) % 3, 3))
    return pts, tris


def geom_sha(prims):
    """sha256 over the sorted welded world triangles (container, vertex order and metadata independent)."""
    pts, tris = tri_soup(prims)
    rows = sorted(sorted(list(pts[i]) for i in t) for t in tris)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _len(a):
    return math.sqrt(_dot(a, a))


def pt_tri_dist(p, a, b, c):
    """Euclidean distance point-triangle (Ericson, closest point by Voronoi region)."""
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return _len(ap)
    bp = _sub(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return _len(bp)
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3)
        return _len(_sub(p, (a[0] + v * ab[0], a[1] + v * ab[1], a[2] + v * ab[2])))
    cp = _sub(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return _len(cp)
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6)
        return _len(_sub(p, (a[0] + w * ac[0], a[1] + w * ac[1], a[2] + w * ac[2])))
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return _len(_sub(p, (b[0] + w * (c[0] - b[0]), b[1] + w * (c[1] - b[1]), b[2] + w * (c[2] - b[2]))))
    den = va + vb + vc
    if den == 0:  # degenerate triangle: fall back to the nearest vertex
        return min(_len(ap), _len(bp), _len(cp))
    v, w = vb / den, vc / den
    return _len(_sub(p, (a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w, a[2] + ab[2] * v + ac[2] * w)))


class TriGrid:
    """Uniform grid over triangle AABBs; dist() grows a cube shell until the best hit is inside the searched ball."""

    def __init__(self, pts, tris, cell):
        self.pts, self.tris, self.cell, self.g = pts, tris, cell, {}
        for ti, t in enumerate(tris):
            P = [pts[i] for i in t]
            lo = [math.floor(min(p[k] for p in P) / cell) for k in range(3)]
            hi = [math.floor(max(p[k] for p in P) / cell) for k in range(3)]
            for x in range(lo[0], hi[0] + 1):
                for y in range(lo[1], hi[1] + 1):
                    for z in range(lo[2], hi[2] + 1):
                        self.g.setdefault((x, y, z), []).append(ti)

    def dist(self, p, rmax):
        c = self.cell
        cx, cy, cz = (math.floor(p[k] / c) for k in range(3))
        best, r, rlim = math.inf, 0, max(1, int(rmax / c) + 1)
        while r <= rlim:
            for x in range(cx - r, cx + r + 1):
                for y in range(cy - r, cy + r + 1):
                    for z in range(cz - r, cz + r + 1):
                        if max(abs(x - cx), abs(y - cy), abs(z - cz)) != r:
                            continue
                        for ti in self.g.get((x, y, z), ()):
                            a, b, cc = (self.pts[i] for i in self.tris[ti])
                            best = min(best, pt_tri_dist(p, a, b, cc))
            if best <= r * c:
                break
            r += 1
        return best if best < math.inf else rmax


def _samples(pts, tris, samples):
    cen = [tuple((pts[a][k] + pts[b][k] + pts[c][k]) / 3.0 for k in range(3)) for a, b, c in tris]
    allp = list(pts) + cen
    step = max(1, len(allp) // max(1, samples))
    return allp[::step]


def one_way(src_pts, src_tris, dst_pts, dst_tris, diag, samples):
    if not dst_tris:
        return diag
    grid = TriGrid(dst_pts, dst_tris, max(diag / GRID_CELLS, 1e-9))
    return max((grid.dist(p, diag) for p in _samples(src_pts, src_tris, samples)), default=0.0)


def hausdorff_um(prims_a, prims_b, diag, samples=None):
    """(h_forward a->b, h_backward b->a) in whole um."""
    samples = SAMPLES if samples is None else samples
    pa, ta = tri_soup(prims_a)
    pb, tb = tri_soup(prims_b)
    return (int(round(one_way(pa, ta, pb, tb, diag, samples))), int(round(one_way(pb, tb, pa, ta, diag, samples))))


def bbox_diag_um(pts):
    if not pts:
        return 0
    lo = [min(p[k] for p in pts) for k in range(3)]
    hi = [max(p[k] for p in pts) for k in range(3)]
    return math.isqrt(sum((hi[k] - lo[k]) ** 2 for k in range(3)))


def source_groups(scene):
    """{prefix: [prims]} of the visual (non-helper) meshes of a source scene; skinned flag."""
    groups, skinned = {}, False
    for p in scene.prims:
        base = node_base(p.name)
        if HELPER_RE.search(base):
            continue
        skinned = skinned or p.skinned
        groups.setdefault(base, []).append(p)
    return groups, skinned


def ntris(prims):
    return sum(len(p.indices) // 3 for p in prims)


# --------------------------------------------------------------------------- predicate
def verify_ladder(scene, source=None, tris_max=None):
    """(checks, per_prefix). `source` = {prefix: [prims]} of the source scene or None (f0 = LOD0 count)."""
    checks, per = [], {}

    def chk(cid, ok, value, limit=None):
        checks.append({"id": cid, "ok": bool(ok), "value": value, "limit": limit})

    groups, helper_lods, stray, ignored = {}, [], [], []
    for p in scene.prims:
        base = node_base(p.name)
        m = LOD_RE.match(base)
        if m and HELPER_RE.search(m.group("prefix")):
            helper_lods.append(p.name)
        elif m:
            groups.setdefault(m.group("prefix"), {}).setdefault(int(m.group("k")), []).append(p)
        elif HELPER_RE.search(base):
            ignored.append(p.name)
        else:
            stray.append(p.name)
    chk("no_helper_lods", not helper_lods, sorted(helper_lods), [])
    chk("no_stray_visual_nodes", not stray, sorted(stray), [])
    if source is not None:
        chk("prefixes_match_source", sorted(groups) == sorted(source), sorted(groups), sorted(source))
    for prefix, lods in sorted(groups.items()):
        complete = sorted(lods) == list(LODS)
        chk(prefix + ":complete", complete, sorted(lods), list(LODS))
        if not complete:
            continue
        n = [ntris(lods[k]) for k in LODS]
        shas = [geom_sha(lods[k]) for k in LODS]
        src = (source or {}).get(prefix)
        f0 = ntris(src) if src else n[0]
        caps = caps_for(f0)
        entry = {"f0": f0, "tris": n, "caps": caps, "floor": FLOOR, "geom_sha256": shas, "transitions": []}
        chk(prefix + ":lod0_is_source", n[0] == f0 and (src is None or geom_sha(src) == shas[0]),
            {"n0": n[0], "f0": f0}, "LOD0 == source geometry")
        if tris_max is not None:
            chk(prefix + ":lod0_tris_max", n[0] <= tris_max, n[0], tris_max)
        for k in (1, 2):
            required = f0 > FLOOR and n[k - 1] > caps[k]
            lo = min(FLOOR, caps[k]) * FILL_PERMILLE // PERMILLE
            same = shas[k] == shas[k - 1]
            if required:
                ok = n[k] < n[k - 1] and n[k] <= caps[k] and n[k] >= lo and not same
                flag = None
            else:
                ok = n[k] <= n[k - 1] and n[k] >= min(lo, n[k - 1])
                flag = "floor_preserved" if f0 <= FLOOR else "lod_saturated"
            tr = {"k": k, "n_prev": n[k - 1], "n": n[k], "cap": caps[k], "min": lo, "required": required,
                  "geometry_identical": same, "flag": flag, "ok": ok}
            entry["transitions"].append(tr)
            chk("%s:lod%d_rule" % (prefix, k), ok, tr, "required=%s" % required)
        diag = bbox_diag_um([q for k in LODS for q in tri_soup(lods[k])[0]])
        entry["diag_um"] = diag
        if diag <= 0:
            chk(prefix + ":diag_positive", False, diag, ">0")
            per[prefix] = entry
            continue
        entry["hausdorff"] = []
        for k in (1, 2):
            hf, hb = hausdorff_um(lods[k], lods[0], diag)
            ratio = (max(hf, hb) * PPM + diag // 2) // diag
            h = {"k": k, "hf_um": hf, "hb_um": hb, "ratio_ppm": ratio, "bound_ppm": HAUSDORFF_PPM[k]}
            entry["hausdorff"].append(h)
            chk("%s:hausdorff_lod%d" % (prefix, k), ratio <= HAUSDORFF_PPM[k], h, HAUSDORFF_PPM[k])
        per[prefix] = entry
    if ignored:
        per["_ignored_helpers"] = sorted(ignored)
    return checks, per


# --------------------------------------------------------------------------- inspected receipt binding
def check_inspected(path, input_sha, cls):
    """PASS inspect receipt bound to this input (rc 4 stale) and class; accepts the cli envelope or the raw receipt."""
    try:
        raw = Path(path).read_bytes()
        rec = json.loads(raw.decode("utf-8").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError) as exc:
        raise LodError(RC_USAGE, "inspected receipt unreadable: %s" % exc)
    if isinstance(rec, dict) and isinstance(rec.get("stage_receipt"), dict):
        rec = rec["stage_receipt"]
    if not isinstance(rec, dict) or rec.get("schema") != inspect_gate.SCHEMA:
        raise LodError(RC_USAGE, "inspected receipt schema is not %s" % inspect_gate.SCHEMA)
    if rec.get("input_sha256") != input_sha:
        raise LodError(RC_STALE, "inspected receipt bound to %s, input is %s" % (rec.get("input_sha256"), input_sha))
    if rec.get("verdict") != "PASS" or rec.get("rc") != RC_OK:
        raise LodError(RC_INCOMPLETE, "inspected receipt is not PASS (%s)" % rec.get("verdict"))
    if rec.get("class") != cls:
        raise LodError(RC_STALE, "inspected receipt class %s != %s" % (rec.get("class"), cls))
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- Blender worker
def default_runner(argv, timeout):
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise LodError(RC_BUDGET, "blender worker timeout %ss" % timeout)
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return proc.returncode, out, err


def worker_argv(blender, src, out, plan):
    return [blender, "-b", "--factory-startup", "-noaudio", "--python", str(WORKER), "--",
            "--in", str(src), "--out", str(out), "--plan", str(plan)]


def build(src, out_glb, plan, *, blender=BLENDER, timeout=TIMEOUT_S, runner=default_runner):
    """Run the worker; returns its BVQA_LOD report (informational only)."""
    rc, out, err = runner(worker_argv(blender, src, out_glb, plan), timeout)
    if rc:
        raise LodError(RC_USAGE, "blender worker rc %s: %s" % (rc, (out[-300:] + err[-300:]).replace("\n", " ")))
    for line in out.splitlines():
        if line.startswith("BVQA_LOD "):
            return json.loads(line[len("BVQA_LOD "):])
    raise LodError(RC_USAGE, "blender worker printed no BVQA_LOD line")


# --------------------------------------------------------------------------- CLI
def _parse(argv):
    ap = argparse.ArgumentParser(prog="bvqa asset lod-ladder")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out-dir")
    ap.add_argument("--class", dest="cls")
    ap.add_argument("--inspected")
    ap.add_argument("--matrix", default=str(inspect_gate.DEFAULT_MATRIX))
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--lods")
    ap.add_argument("--source")
    ap.add_argument("--prev-receipt")
    ap.add_argument("--out")
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S)
    return ap.parse_args(argv)


def run(argv, runner=default_runner):
    """(rc, receipt or None)."""
    try:
        a = _parse(argv)
    except SystemExit:
        return RC_USAGE, None
    rec = {"schema": SCHEMA, "stage": "lod-ladder", "class": a.cls, "input_sha256": None, "prev_receipt_sha256": None,
           "inspected_sha256": None, "ladder_sha256": None, "mode": "verify" if a.verify_only else "build",
           "policy": {"floor": FLOOR, "ratio_permille": list(RATIO_PERMILLE), "hausdorff_ppm": list(HAUSDORFF_PPM),
                      "fill_permille": FILL_PERMILLE, "samples": SAMPLES},
           "checks": [], "failed": [], "per_prefix": {}, "worker": None, "reason": None}

    def done(rc, verdict, reason=None):
        rec.update(rc=rc, verdict=verdict, reason=reason)
        return rc, rec
    try:
        if not a.cls or (a.verify_only and not a.lods) or (not a.verify_only and not (a.inp and a.out_dir and a.inspected)):
            raise LodError(RC_USAGE, "build needs --in --out-dir --class --inspected; verify needs --lods --class")
        try:
            pol, _ = inspect_gate.load_policy(a.matrix, a.cls)
        except inspect_gate.MatrixError as exc:
            raise LodError(exc.rc, exc.reason)
        rec["class"] = pol["_class"]
        if a.prev_receipt:
            rec["prev_receipt_sha256"] = hashlib.sha256(Path(a.prev_receipt).read_bytes()).hexdigest()
        src_path = a.source if a.verify_only else a.inp
        source, src_bytes = None, None
        if src_path:
            src_bytes = Path(src_path).read_bytes()
            rec["input_sha256"] = glb_core.sha256(src_bytes)
            source, skinned = source_groups(glb_core.parse_glb(src_bytes))
            if skinned:
                raise LodError(RC_INCOMPLETE, "skinned source: static LOD ladder refuses it")
            if not source or not any(ntris(v) for v in source.values()):
                raise LodError(RC_INCOMPLETE, "source has no visual triangles")
        if a.inspected:
            rec["inspected_sha256"] = check_inspected(a.inspected, rec["input_sha256"] or "", rec["class"])
            if rec["prev_receipt_sha256"] is None:
                rec["prev_receipt_sha256"] = rec["inspected_sha256"]
        if a.verify_only:
            ladder_path = Path(a.lods)
        else:
            out_dir = Path(a.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            ladder_path = out_dir / "ladder.glb"
            plan = {p: {"f0": ntris(v), "caps": caps_for(ntris(v)), "floor": FLOOR} for p, v in sorted(source.items())}
            with tempfile.TemporaryDirectory(prefix="bvqa_lod_") as td:
                plan_path = Path(td) / "plan.json"
                plan_path.write_text(canon(plan))
                rec["worker"] = build(Path(a.inp).resolve(), ladder_path.resolve(), plan_path,
                                      blender=a.blender, timeout=a.timeout, runner=runner)
        ladder = ladder_path.read_bytes()
        rec["ladder_sha256"] = glb_core.sha256(ladder)
        checks, per = verify_ladder(glb_core.parse_glb(ladder), source, pol["tris_max"])
    except LodError as exc:
        return done(exc.rc, "STALE" if exc.rc == RC_STALE else "BUDGET" if exc.rc == RC_BUDGET else
                    "INCOMPLETE" if exc.rc == RC_INCOMPLETE else "ERROR", exc.reason)
    except (OSError, glb_core.GlbError) as exc:
        return done(RC_USAGE, "ERROR", str(exc))
    rec.update(checks=checks, per_prefix=per, failed=[c["id"] for c in checks if not c["ok"]])
    if not any(k.endswith(":complete") for k in (c["id"] for c in checks)):
        rec["failed"].append("no_lod_groups")
    if rec["failed"]:
        return done(RC_FAIL, "FAIL", "failed: " + ",".join(rec["failed"]))
    return done(RC_OK, "PASS")


# @anchor:bv.assets.lod_ladder.main
def main(argv=None):
    rc, rec = run(sys.argv[1:] if argv is None else argv)
    if rec is None:
        return rc
    text = canon(rec)
    out = None
    try:
        a = _parse(sys.argv[1:] if argv is None else argv)
        out = a.out or (str(Path(a.out_dir) / "lod_receipt.json") if a.out_dir else None)
    except SystemExit:
        pass
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text)
    sys.stdout.write(text)
    if rc:
        print(rec["reason"], file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
