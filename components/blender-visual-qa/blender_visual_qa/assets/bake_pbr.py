"""PBR bake high -> low: Blender re-packs LOD0 UVs and bakes Cycles maps; the host re-verifies every output (router §3.7).

  bvqa asset bake-pbr --high <raw_norm.glb> --low <lod0.glb> --out-dir <dir> [--res 1024] [--slow]
                      [--margin-px 8] [--cage-extrusion-um 5000] [--prev-receipt R] [--blender B] [--timeout S]
  bvqa asset bake-pbr --verify-only --high <raw_norm.glb> --low <lod0.glb> --out-dir <dir> [--res 1024]

Worker (bpy_side/assets/bake_worker.py): uv.smart_project + uv.pack_islands on the joined LOD0, then Cycles CPU
selected-to-active bakes NORMAL (tangent), AO, ROUGHNESS, and EMIT with the high material's Base Color (albedo) and
Metallic (metal) re-wired into an emission shader. Images start magenta, so any texel the bake did not write stays
magenta. Outputs: {albedo,normal,ao,roughness,metal}.png + low_uv.glb (LOD0 with the new UVs).
Host checks (the worker's own report is never trusted): high/low world AABB aligned <= AABB_TOL_PPM of the high
diagonal (before Blender runs); low_uv.glb keeps the LOD0 AABB and triangle count, UVs inside [0,1] with zero pairwise
overlap; each map is res x res; inside the UV coverage mask (texel centres rasterized from low_uv.glb) unwritten
magenta texels <= HOLE_MAX_PPM, albedo not all black, normal blue mean > NORMAL_BLUE_MIN.
--res above ASSET_BAKE_RES (CPU default) needs --slow. Exit: 0 OK | 2 usage/IO/schema | 3 incomplete (no triangles,
skinned, missing output) | 5 gate fail | 6 timeout.
"""
# @module {"id":"bv.assets.bake_pbr","layer":4,"owner":"BASSETS"}
import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

from blender_visual_qa.assets import glb_core, limits, mesh_gate, orm_pack
from blender_visual_qa.drive import worker

SCHEMA = "bvqa.asset-bake-pbr/v1"

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "bpy_side/assets/bake_worker.py"
BLENDER = "/usr/bin/blender"
RC_OK, RC_USAGE, RC_INCOMPLETE, RC_STALE, RC_FAIL, RC_BUDGET = (limits.RC_OK, limits.RC_USAGE, limits.RC_INCOMPLETE,
                                                                limits.RC_STALE, limits.RC_FAIL, limits.RC_BUDGET)
RES = limits.ASSET_BAKE_RES
RES_SLOW = limits.ASSET_BAKE_RES_SLOW
RES_MIN = limits.ASSET_BAKE_RES_MIN
MARGIN_PX = limits.ASSET_BAKE_MARGIN_PX
CAGE_UM = limits.ASSET_BAKE_CAGE_UM
AABB_TOL_PPM = limits.ASSET_BAKE_AABB_TOL_PPM
NORMAL_BLUE_MIN = limits.ASSET_BAKE_NORMAL_BLUE_MIN
HOLE_MAX_PPM = limits.ASSET_BAKE_HOLE_MAX_PPM
SPP = limits.ASSET_BAKE_SPP
MAPS = ("albedo", "normal", "ao", "roughness", "metal")
LOW_UV = "low_uv.glb"
TIMEOUT_S = 1800
PPM = 1000000
Q16 = 65536
BYTE_MAX = 255
MAGENTA = (BYTE_MAX, 0, BYTE_MAX)


class BakeError(Exception):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc = rc
        self.reason = reason


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- geometry
def world_tris(scene):
    """(positions_um world, uv_q16 or None) per triangle corner of every static prim; skinned flag."""
    pts, skinned, ntri = [], False, 0
    for p in scene.prims:
        skinned = skinned or p.skinned
        pts.extend(glb_core.world_positions_um(p))
        ntri += len(p.indices) // 3
    return pts, skinned, ntri


def aabb(pts):
    return [min(p[k] for p in pts) for k in range(3)], [max(p[k] for p in pts) for k in range(3)]


def aabb_delta_ppm(ref, other):
    """Largest |min/max delta| over the 3 axes, in ppm of the ref AABB diagonal."""
    (lo_r, hi_r), (lo_o, hi_o) = ref, other
    diag = math.isqrt(sum((hi_r[k] - lo_r[k]) ** 2 for k in range(3)))
    if diag <= 0:
        raise BakeError(RC_INCOMPLETE, "degenerate high AABB")
    worst = max(max(abs(lo_r[k] - lo_o[k]), abs(hi_r[k] - hi_o[k])) for k in range(3))
    return (worst * PPM + diag // 2) // diag, diag


def uv_tris(scene):
    """[(tri_id, ((u,v),(u,v),(u,v)))] in q16 over all prims; None when a prim has no UVs."""
    out = []
    for pi, p in enumerate(scene.prims):
        if p.uvs_q16 is None:
            return None
        for t in range(len(p.indices) // 3):
            out.append(((pi, t), tuple(tuple(p.uvs_q16[p.indices[3 * t + c]]) for c in range(3))))
    return out


def coverage(tris, res):
    """Set of covered texels (x, y_png) whose centre lies inside (or on) a UV triangle. glTF UVs have their origin at
    the image top-left, so the PNG row is the v texel index unchanged."""
    cov = set()
    two_res = 2 * res
    for _, t in tris:
        # Scale q16 UVs so a texel centre (i + 0.5)/res maps to the integer (2i + 1) * Q16.
        pts = [(u * two_res, v * two_res) for u, v in t]
        area = (pts[1][0] - pts[0][0]) * (pts[2][1] - pts[0][1]) - (pts[1][1] - pts[0][1]) * (pts[2][0] - pts[0][0])
        if area == 0:
            continue
        sign = 1 if area > 0 else -1
        x0 = max(0, min(p[0] for p in pts) // (2 * Q16))
        x1 = min(res - 1, max(p[0] for p in pts) // (2 * Q16))
        y0 = max(0, min(p[1] for p in pts) // (2 * Q16))
        y1 = min(res - 1, max(p[1] for p in pts) // (2 * Q16))
        edges = [(pts[i], pts[(i + 1) % 3]) for i in range(3)]
        for ty in range(y0, y1 + 1):
            cy = (2 * ty + 1) * Q16
            for tx in range(x0, x1 + 1):
                cx = (2 * tx + 1) * Q16
                if all(sign * ((b[0] - a[0]) * (cy - a[1]) - (b[1] - a[1]) * (cx - a[0])) >= 0 for a, b in edges):
                    cov.add((tx, ty))
    return cov


# --------------------------------------------------------------------------- map checks
def map_stats(name, data, res, cov):
    """Integer stats of one baked map over the coverage mask and its failed check ids."""
    try:
        w, h, ch, px = orm_pack.decode_png(data)
    except orm_pack.OrmError as exc:
        return {"error": str(exc)}, [name + ":decode"]
    st = {"w": w, "h": h, "channels": ch, "covered": 0, "holes": 0, "holes_ppm": 0, "mean_rgb": [0, 0, 0],
          "nonzero": 0}
    failed = []
    if (w, h) != (res, res):
        return st, [name + ":size"]
    if ch < 3:
        rgb_of = lambda i: (px[i], px[i], px[i])  # noqa: E731
    else:
        rgb_of = lambda i: tuple(px[i:i + 3])  # noqa: E731
    sums, n, holes, nonzero = [0, 0, 0], 0, 0, 0
    for x, y in cov:
        rgb = rgb_of((y * w + x) * ch)
        if rgb == MAGENTA:
            holes += 1
            continue
        n += 1
        if any(rgb):
            nonzero += 1
        for k in range(3):
            sums[k] += rgb[k]
    total = len(cov)
    st.update(covered=total, holes=holes, nonzero=nonzero,
              holes_ppm=(holes * PPM + total // 2) // total if total else 0,
              mean_rgb=[(s + n // 2) // n if n else 0 for s in sums])
    if not total:
        failed.append(name + ":no_coverage")
    if st["holes_ppm"] > HOLE_MAX_PPM:
        failed.append(name + ":holes")
    if name == "albedo" and nonzero == 0:
        failed.append(name + ":black")
    if name == "normal" and not st["mean_rgb"][2] > NORMAL_BLUE_MIN:
        failed.append(name + ":blue_mean")
    return st, failed


def verify_outputs(out_dir, low_scene, res):
    """(checks dict, failed list). Raises BakeError(RC_INCOMPLETE) when an output is missing."""
    out_dir = Path(out_dir)
    missing = [m + ".png" for m in MAPS if not (out_dir / (m + ".png")).is_file()]
    if not (out_dir / LOW_UV).is_file():
        missing.append(LOW_UV)
    if missing:
        raise BakeError(RC_INCOMPLETE, "missing outputs: " + ",".join(missing))
    failed, checks = [], {}
    uv_bytes = (out_dir / LOW_UV).read_bytes()
    uv_scene = glb_core.parse_glb(uv_bytes)
    lp, _, ln = world_tris(low_scene)
    up, _, un = world_tris(uv_scene)
    checks["low_uv"] = {"sha256": glb_core.sha256(uv_bytes), "tris": un, "low_tris": ln}
    if un != ln:
        failed.append("low_uv:tri_count")
    if not up:
        failed.append("low_uv:empty")
    else:
        dppm, _ = aabb_delta_ppm(aabb(lp), aabb(up))
        checks["low_uv"]["aabb_delta_ppm"] = dppm
        if dppm > AABB_TOL_PPM:
            failed.append("low_uv:aabb")
    tris = uv_tris(uv_scene)
    if tris is None or not tris:
        failed.append("low_uv:no_uvs")
        tris = []
    outside = sum(1 for _, t in tris for u, v in t if u < 0 or v < 0 or u > Q16 or v > Q16)
    overlap = mesh_gate._uv_overlap(tris) if tris else set()
    checks["low_uv"].update(uv_outside=outside, uv_overlap_tris=len(overlap))
    if outside:
        failed.append("low_uv:uv_outside")
    if overlap:
        failed.append("low_uv:uv_overlap")
    cov = coverage([t for t in tris if t[0] not in overlap], res)
    checks["coverage_ppm"] = (len(cov) * PPM + res * res // 2) // (res * res)
    checks["maps"] = {}
    for m in MAPS:
        data = (out_dir / (m + ".png")).read_bytes()
        st, f = map_stats(m, data, res, cov)
        st["sha256"] = hashlib.sha256(data).hexdigest()
        checks["maps"][m] = st
        failed += f
    return checks, failed


# --------------------------------------------------------------------------- Blender worker
def default_runner(argv, timeout):
    return worker.asset(argv, timeout, input_flags=("--high", "--low"), output_flag="--out-dir")


def worker_argv(blender, a):
    return [blender, "-b", "--factory-startup", "-noaudio", "--python", str(WORKER), "--",
            "--high", str(Path(a.high).resolve()), "--low", str(Path(a.low).resolve()),
            "--out-dir", str(Path(getattr(a, "work_dir", None) or a.out_dir).resolve()), "--res", str(a.res), "--margin-px", str(a.margin_px),
            "--cage-extrusion-um", str(a.cage_extrusion_um), "--samples", str(SPP)]


def bake(a, runner):
    """Run the worker; returns its BVQA_BAKE report (informational only)."""
    rc, out, err = runner(worker_argv(a.blender, a), a.timeout)
    if rc:
        raise BakeError(RC_USAGE, "blender worker rc %s: %s" % (rc, (out[-300:] + err[-300:]).replace("\n", " ")))
    for line in out.splitlines():
        if line.startswith("BVQA_BAKE "):
            return json.loads(line[len("BVQA_BAKE "):])
    raise BakeError(RC_USAGE, "blender worker printed no BVQA_BAKE line")


# --------------------------------------------------------------------------- CLI
def _parse(argv):
    ap = argparse.ArgumentParser(prog="bvqa asset bake-pbr")
    ap.add_argument("--high")
    ap.add_argument("--low")
    ap.add_argument("--out-dir")
    ap.add_argument("--res", type=int, default=RES)
    ap.add_argument("--slow", action="store_true")
    ap.add_argument("--margin-px", type=int, default=MARGIN_PX)
    ap.add_argument("--cage-extrusion-um", type=int, default=CAGE_UM)
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--prev-receipt")
    ap.add_argument("--out")
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S)
    return ap.parse_args(argv)


def _read(path):
    data = Path(path).read_bytes()
    scene = glb_core.parse_glb(data)
    pts, skinned, ntri = world_tris(scene)
    return data, scene, pts, skinned, ntri


DELIVERABLE_CAP = 64 << 20  # L6: the 5 maps + low_uv.glb are assets (kept), capped per run


def deliver(work, out_dir):
    """Copy the final deliverables (maps + low_uv.glb) from the RAM workspace to out_dir; fail closed over the cap."""
    names = [m + ".png" for m in MAPS] + [LOW_UV]
    total = sum((Path(work) / n).stat().st_size for n in names)
    if total > DELIVERABLE_CAP:
        raise BakeError(RC_FAIL, "deliverables %d bytes > cap %d" % (total, DELIVERABLE_CAP))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for n in names:
        worker.publish(Path(out_dir) / n, worker.read_output(Path(work) / n))
    return total


# @anchor:bv.assets.bake_pbr.run
def run(argv, runner=default_runner, *, ws=None, ram_root=None, ram_config=None):
    """(rc, receipt or None). STORAGE LAW: the Cycles bake writes into a vadapt.ramws workspace (4 GiB); only a verified
    PASS delivers the maps + low_uv.glb to --out-dir (plus ram_receipt.json)."""
    from blender_visual_qa.assets import turntable as tt
    own = None
    try:
        a = _parse(argv)
    except SystemExit:
        return RC_USAGE, None
    if a.verify_only or not (a.high and a.low and a.out_dir):
        return _run(argv, runner, None, None, None)
    try:
        mod, own = (sys.modules[type(ws).__module__], None) if ws is not None else tt.open_workspace(
            Path(a.out_dir), root=ram_root, config=ram_config)
        ws = ws if ws is not None else own
        work = ws.path("bake/")
        return _run(argv, runner, work, ws, mod)
    except tt.TurntableError as exc:
        return exc.rc, {"schema": SCHEMA, "stage": "bake-pbr", "rc": exc.rc, "verdict": "ERROR", "reason": str(exc),
                        "failed": [], "checks": {}}
    finally:
        if own is not None:
            own.close()


def _run(argv, runner, work, ws, mod):
    from blender_visual_qa.assets import turntable as tt
    try:
        a = _parse(argv)
    except SystemExit:
        return RC_USAGE, None
    a.work_dir = str(work) if work is not None else None
    rec = {"schema": SCHEMA, "stage": "bake-pbr", "input_sha256": None, "high_sha256": None,
           "prev_receipt_sha256": None, "mode": "verify" if a.verify_only else "build",
           "params": {"res": a.res, "margin_px": a.margin_px, "cage_extrusion_um": a.cage_extrusion_um, "spp": SPP},
           "policy": {"aabb_tol_ppm": AABB_TOL_PPM, "hole_max_ppm": HOLE_MAX_PPM, "normal_blue_min": NORMAL_BLUE_MIN},
           "aabb_delta_ppm": None, "checks": {}, "failed": [], "worker": None, "reason": None}

    def done(rc, verdict, reason=None):
        rec.update(rc=rc, verdict=verdict, reason=reason)
        return rc, rec
    try:
        if not (a.high and a.low and a.out_dir):
            raise BakeError(RC_USAGE, "needs --high --low --out-dir")
        if a.res < RES_MIN or a.res > RES_SLOW or a.res & (a.res - 1):
            raise BakeError(RC_USAGE, "--res must be a power of two in [%d, %d]" % (RES_MIN, RES_SLOW))
        if a.res > RES and not a.slow:
            raise BakeError(RC_USAGE, "--res %d above the CPU default %d needs --slow" % (a.res, RES))
        if a.margin_px < 0 or a.cage_extrusion_um < 0 or a.timeout <= 0:
            raise BakeError(RC_USAGE, "--margin-px/--cage-extrusion-um must be >= 0, --timeout > 0")
        if a.prev_receipt:
            rec["prev_receipt_sha256"] = hashlib.sha256(Path(a.prev_receipt).read_bytes()).hexdigest()
        hb, _, hp, hskin, hn = _read(a.high)
        lb, low_scene, lp, lskin, ln = _read(a.low)
        rec["high_sha256"], rec["input_sha256"] = glb_core.sha256(hb), glb_core.sha256(lb)
        if hskin or lskin:
            raise BakeError(RC_INCOMPLETE, "skinned input: static bake refuses it")
        if not hn or not ln:
            raise BakeError(RC_INCOMPLETE, "high or low has no triangles")
        dppm, diag = aabb_delta_ppm(aabb(hp), aabb(lp))
        rec["aabb_delta_ppm"] = dppm
        if dppm > AABB_TOL_PPM:
            rec["failed"] = ["aabb_misaligned"]
            return done(RC_FAIL, "FAIL", "high/low AABB delta %d ppm > %d" % (dppm, AABB_TOL_PPM))
        if not a.verify_only:
            rec["worker"] = bake(a, runner)
            tt.ram_account(mod, ws)
        checks, failed = verify_outputs(work if work is not None else a.out_dir, low_scene, a.res)
        if work is not None and not failed:
            rec["deliverable_bytes"] = deliver(work, a.out_dir)
            tt.write_ram_receipt(a.out_dir, ws)
    except tt.TurntableError as exc:
        return done(exc.rc, "ERROR", str(exc))
    except BakeError as exc:
        return done(exc.rc, "BUDGET" if exc.rc == RC_BUDGET else "INCOMPLETE" if exc.rc == RC_INCOMPLETE else
                    "ERROR", exc.reason)
    except (OSError, ValueError, glb_core.GlbError) as exc:
        return done(RC_USAGE, "ERROR", str(exc))
    rec.update(checks=checks, failed=failed)
    if failed:
        return done(RC_FAIL, "FAIL", "failed: " + ",".join(failed))
    return done(RC_OK, "PASS")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    rc, rec = run(args)
    if rec is None:
        return rc
    text = canon(rec)
    try:
        a = _parse(args)
        out = a.out or (str(Path(a.out_dir) / "bake_receipt.json") if a.out_dir else None)
    except SystemExit:
        out = None
    if out and rc != RC_USAGE:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text)
    sys.stdout.write(text + "\n")
    if rc:
        print(rec["reason"], file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
