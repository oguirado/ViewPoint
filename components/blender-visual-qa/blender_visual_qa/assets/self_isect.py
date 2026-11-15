"""bv.assets.self_isect: self-intersection + sliver gate over EVERY primitive of a GLB (VG-0245; stdlib host).

The host welds world positions (um) per primitive, computes the integer sliver ratio of each triangle
(altitude over its longest edge, ppm: 2*area*PPM // longest_edge^2) and asks the Blender worker
`bpy_side/assets/self_isect.py` (mathutils.bvhtree.overlap) for non-adjacent intersecting triangle pairs.
Fail when any pair intersects or any triangle's ratio is below ASSET_SLIVER_RATIO_MIN_PPM (a zero-area triangle
has ratio 0). The worker output is validated (indices in range, i < j, no shared vertex) before it is trusted.
Exit codes: 0 pass | 2 usage/IO/worker error | 3 no triangles (zero checks) | 5 gate fail | 6 worker timeout.
Receipt schema bvqa.asset-self-isect/v1 (sorted canonical JSON; input_sha256, prev_sha256).
"""
# @module {"id":"bv.assets.self_isect","layer":4,"owner":"BASSETS"}
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blender_visual_qa.assets import glb_core, limits

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "bvqa.asset-self-isect/v1"
WORKER = ROOT / "bpy_side/assets/self_isect.py"
BLENDER = "/usr/bin/blender"
PPM = 1000000
SLIVER_MIN_PPM = getattr(limits, "ASSET_SLIVER_RATIO_MIN_PPM", 50000)
MAX_PAIRS = getattr(limits, "ASSET_ISECT_MAX_PAIRS", 256)


class IsectError(Exception):
    def __init__(self, rc, code, detail=""):
        super().__init__("%s: %s" % (code, detail))
        self.rc, self.code, self.detail = rc, code, detail


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def sliver_ratio_ppm(p, q, r):
    """altitude(on longest edge) / longest edge in ppm; equilateral ~ 866025, degenerate 0."""
    edges = (_sub(q, p), _sub(r, q), _sub(p, r))
    longest2 = max(_dot(e, e) for e in edges)
    if not longest2:
        return 0
    c = _cross(edges[0], _sub(r, p))
    return math.isqrt(_dot(c, c)) * PPM // longest2


def weld(prim):
    """-> (verts_um, tris) with coincident world positions merged; zero-index-collapsed tris keep their (repeated) ids."""
    pts = glb_core.world_positions_um(prim)
    ids, verts, remap = {}, [], []
    for pt in pts:
        if pt not in ids:
            ids[pt] = len(verts)
            verts.append(pt)
        remap.append(ids[pt])
    idx = prim.indices
    tris = [tuple(remap[idx[k + n]] for n in range(3)) for k in range(0, len(idx) - len(idx) % 3, 3)]
    return verts, tris


def slivers(verts, tris, min_ppm):
    out = []
    for t, (a, b, c) in enumerate(tris):
        ratio = sliver_ratio_ppm(verts[a], verts[b], verts[c])
        if ratio < min_ppm:
            out.append((t, ratio))
    return out


def default_runner(argv, timeout):
    import signal
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise IsectError(limits.RC_BUDGET, "E_TIMEOUT", "blender worker")
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return proc.returncode, out, err


def worker_argv(blender, mesh_json, max_pairs):
    return [blender, "-b", "--factory-startup", "-noaudio", "--python", str(WORKER), "--",
            "--mesh", str(mesh_json), "--max-pairs", str(max_pairs)]


def find_pairs(meshes, runner=None, blender=BLENDER, timeout=300):
    """meshes: [(name, verts, tris)] -> [(count, [(i, j)...])] per mesh, validated. Raises IsectError."""
    runner = runner or default_runner
    with tempfile.TemporaryDirectory(prefix="bvqa_isect_") as tmp:
        mj = os.path.join(tmp, "mesh.json")
        with open(mj, "w") as fh:
            json.dump({"prims": [{"name": n, "verts_um": [list(v) for v in vs], "tris": [list(t) for t in ts]}
                                 for n, vs, ts in meshes]}, fh)
        try:
            rc, out, err = runner(worker_argv(blender, mj, MAX_PAIRS), timeout)
        except FileNotFoundError:
            raise IsectError(limits.RC_USAGE, "E_BLENDER", "blender not found: %s" % blender)
    if rc:
        raise IsectError(limits.RC_USAGE, "E_BLENDER", "worker rc %s: %s" % (rc, (out[-300:] + err[-300:]).replace("\n", " ")))
    for line in out.splitlines():
        if line.startswith("BVQA_SELF_ISECT "):
            rec = json.loads(line[len("BVQA_SELF_ISECT "):])
            res = rec.get("prims", [])
            if len(res) != len(meshes):
                raise IsectError(limits.RC_USAGE, "E_BLENDER", "worker prim count mismatch")
            fixed = []
            for (n, vs, ts), r in zip(meshes, res):
                pairs = [tuple(p) for p in r["pairs"]]
                for i, j in pairs:
                    if not (0 <= i < j < len(ts)) or set(ts[i]) & set(ts[j]):
                        raise IsectError(limits.RC_USAGE, "E_BLENDER", "worker returned an invalid pair")
                fixed.append((int(r["count"]), pairs))
            return fixed
    raise IsectError(limits.RC_USAGE, "E_BLENDER", "no BVQA_SELF_ISECT line")


def gate_scene(scene, runner=None, blender=BLENDER, timeout=300, sliver_min=None):
    """-> (rc, body). Raises IsectError for worker problems."""
    sliver_min = SLIVER_MIN_PPM if sliver_min is None else sliver_min
    meshes = []
    for p in scene.prims:
        verts, tris = weld(p)
        if tris:
            meshes.append((str(p.name), verts, tris))
    if not meshes:
        return limits.RC_INCOMPLETE, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "no triangles: zero checks executed",
                                      "prims": [], "failed": []}
    found = find_pairs(meshes, runner, blender, timeout)
    prims, failed = [], set()
    for (name, verts, tris), (count, pairs) in zip(meshes, found):
        sl = slivers(verts, tris, sliver_min)
        fails = []
        if count:
            fails.append("self_intersection")
        if sl:
            fails.append("sliver")
        failed.update(fails)
        prims.append({"name": name, "triangles": len(tris), "intersecting_pairs": count,
                      "pairs": [list(p) for p in pairs], "sliver_count": len(sl),
                      "sliver_min_ratio_ppm": min((r for _, r in sl), default=None),
                      "slivers": [t for t, _ in sl[:MAX_PAIRS]], "failed": fails})
    body = {"status": "FAIL" if failed else "PASS", "prims": prims, "failed": sorted(failed),
            "primitives_checked": len(prims), "options": {"sliver_min_ppm": sliver_min}}
    return (limits.RC_FAIL if failed else limits.RC_OK), body


def make_receipt(rc, body, input_sha256, prev_sha256):
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": input_sha256, "prev_sha256": prev_sha256})
    return rec


def run(argv, runner=None):
    ap = argparse.ArgumentParser(prog="self-isect", description="self-intersection + sliver gate over all primitives")
    ap.add_argument("--mesh", required=True)
    ap.add_argument("--sliver-min-ppm", type=int, default=SLIVER_MIN_PPM)
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--prev-receipt", default=None)
    ap.add_argument("--out", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return limits.RC_USAGE, None
    try:
        with open(a.mesh, "rb") as fh:
            raw = fh.read()
        prev = ""
        if a.prev_receipt:
            with open(a.prev_receipt, "rb") as fh:
                prev = hashlib.sha256(fh.read()).hexdigest()
        scene = glb_core.parse_glb(raw)
        rc, body = gate_scene(scene, runner, a.blender, a.timeout, a.sliver_min_ppm)
    except (OSError, glb_core.GlbError, IsectError) as exc:
        sys.stderr.write("self-isect: %s\n" % (exc,))
        return getattr(exc, "rc", limits.RC_USAGE), None
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
