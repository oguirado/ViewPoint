"""Asset normalize: bake world TRS, metric scale and bottom-centre pivot (integer um/q16 math, stdlib only)."""
# @module {"id":"bv.assets.normalize","layer":4,"owner":"BASSETS"}
from __future__ import annotations
import argparse
import copy
import dataclasses
import json
from math import isqrt
import sys

from blender_visual_qa.assets import glb_core
from blender_visual_qa.assets.glb_core import GlbError

try:  # limits.py is INT-owned (lane L5); the fallback mirrors the seeded row ASSET_PIVOT_TOL_UM.
    from blender_visual_qa.assets.limits import ASSET_PIVOT_TOL_UM
except ImportError:  # pragma: no cover
    ASSET_PIVOT_TOL_UM = 10

SCHEMA = "bvqa.asset-normalize/v1"
Q16 = 65536
PPM = 1000000
UM_PER_M = 1000000
HELPER_SUFFIXES = ("_collision", "-col", "-convcol")

RC_OK, RC_USAGE, RC_INCOMPLETE, RC_STALE, RC_FAIL = 0, 2, 3, 4, 5


class NormalizeError(Exception):
    def __init__(self, rc, code, reason):
        super().__init__(reason)
        self.rc, self.code, self.reason = rc, code, reason


def _div_round(num, den):
    """Round-half-away integer division (den > 0)."""
    if num < 0:
        return -((-num * 2 + den) // (den * 2))
    return (num * 2 + den) // (den * 2)


def det3_q16(m):
    return glb_core.det3_q48(m)


def _cofactor(m):
    """Cofactor matrix of the linear part (row-major 4r+c): inverse-transpose up to a positive scalar when det>0."""
    a = [[m[4 * r + c] for c in range(3)] for r in range(3)]
    cof = [[0] * 3 for _ in range(3)]
    for i in range(3):
        rows = [x for x in range(3) if x != i]
        for j in range(3):
            cols = [x for x in range(3) if x != j]
            minor = a[rows[0]][cols[0]] * a[rows[1]][cols[1]] - a[rows[0]][cols[1]] * a[rows[1]][cols[0]]
            cof[i][j] = minor if (i + j) % 2 == 0 else -minor
    return cof


def bake_normal(cof, n):
    v = [cof[r][0] * n[0] + cof[r][1] * n[1] + cof[r][2] * n[2] for r in range(3)]
    ln = isqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if ln == 0:
        return (0, 0, 0)
    return tuple(_div_round(c * Q16, ln) for c in v)


def is_helper(prim):
    return prim.name.endswith(HELPER_SUFFIXES)


def bake_prim(prim):
    """Return a fresh world-space copy of prim (own lists: shared mesh instances never alias)."""
    out = copy.copy(prim)
    m = prim.world_q16
    out.positions_um = glb_core.world_positions_um(prim)
    if prim.normals_q16 is not None:
        cof = _cofactor(m)
        out.normals_q16 = [bake_normal(cof, n) for n in prim.normals_q16]
    else:
        out.normals_q16 = None
    out.indices = list(prim.indices)
    out.uvs_q16 = None if prim.uvs_q16 is None else list(prim.uvs_q16)
    return out


def visual_bounds(prims):
    pts = [p for pr in prims if not is_helper(pr) for p in pr.positions_um]
    if not pts:
        pts = [p for pr in prims for p in pr.positions_um]
    if not pts:
        return None
    return tuple((min(c[k] for c in pts), max(c[k] for c in pts)) for k in range(3))


def scale_ppm(bounds, target_height_um, unit_scale_ppm):
    if target_height_um is None:
        return unit_scale_ppm
    height = bounds[1][1] - bounds[1][0]
    if height <= 0:
        raise NormalizeError(RC_INCOMPLETE, "E_FLAT", "zero-height asset cannot be fitted to --target-height-um")
    return _div_round(target_height_um * PPM, height)


def apply_scale(prims, k_ppm):
    for pr in prims:
        pr.positions_um = [tuple(_div_round(c * k_ppm, PPM) for c in p) for p in pr.positions_um]


def ground(prims):
    """Translate so X/Z centre = 0 and min Y = 0 (measured on visual meshes)."""
    b = visual_bounds(prims)
    dx = -((b[0][0] + b[0][1]) // 2)
    dy = -b[1][0]
    dz = -((b[2][0] + b[2][1]) // 2)
    for pr in prims:
        pr.positions_um = [(p[0] + dx, p[1] + dy, p[2] + dz) for p in pr.positions_um]


IDENTITY_Q16 = (Q16, 0, 0, 0, 0, Q16, 0, 0, 0, 0, Q16, 0, 0, 0, 0, Q16)


def normalize_scene(scene, target_height_um=None, unit_scale_ppm=PPM, drop_animations=False):
    """Pure transform: returns (new_scene, info). Raises NormalizeError (rc 3 / rc 5)."""
    if not scene.prims:
        raise NormalizeError(RC_INCOMPLETE, "E_EMPTY", "scene has no primitives")
    for pr in scene.prims:
        if det3_q16(pr.world_q16) < 0:
            raise NormalizeError(RC_FAIL, "E_REFLECTION", "negative determinant on " + pr.name)
    if any(pr.skinned for pr in scene.prims) and not drop_animations:
        raise NormalizeError(RC_INCOMPLETE, "E_SKINNED", "skinned input needs --drop-animations")
    prims = [bake_prim(pr) for pr in scene.prims]
    bounds = visual_bounds(prims)
    if bounds is None:
        raise NormalizeError(RC_INCOMPLETE, "E_EMPTY", "no vertices")
    k_ppm = scale_ppm(bounds, target_height_um, unit_scale_ppm)
    apply_scale(prims, k_ppm)
    ground(prims)
    for pr in prims:
        pr.world_q16 = IDENTITY_Q16
        pr.node_path = pr.name
        pr.skinned = False
    out = copy.copy(scene)
    out.prims = prims
    return out, {"k_ppm": k_ppm, "dropped_skin": any(pr.skinned for pr in scene.prims)}


def measure(scene):
    b = visual_bounds(scene.prims)
    return {
        "dims_um": [b[k][1] - b[k][0] for k in range(3)],
        "pivot_y_um": b[1][0],
        "centre_x_um": (b[0][0] + b[0][1]) // 2,
        "centre_z_um": (b[2][0] + b[2][1]) // 2,
        "tris": sum(len(p.indices) // 3 for p in scene.prims),
    }


def _frozen(scene):
    return all(tuple(p.world_q16) == IDENTITY_Q16 for p in scene.prims)


def check_post(src_scene, out_scene, k_ppm):
    """Re-parsed output postconditions; returns list of failed check names."""
    bad = []
    m = measure(out_scene)
    if abs(m["pivot_y_um"]) > ASSET_PIVOT_TOL_UM:
        bad.append("pivot_y")
    if abs(m["centre_x_um"]) > ASSET_PIVOT_TOL_UM or abs(m["centre_z_um"]) > ASSET_PIVOT_TOL_UM:
        bad.append("centre_xz")
    if not _frozen(out_scene):
        bad.append("transforms_not_frozen")
    if m["tris"] != measure(src_scene)["tris"]:
        bad.append("tri_count")
    return bad


def run_bytes(data, target_height_um=None, unit_scale_ppm=PPM, drop_animations=False):
    """bytes -> (out_bytes, receipt dict). Raises NormalizeError / GlbError."""
    src = glb_core.parse_glb(data)
    out_scene, info = normalize_scene(src, target_height_um, unit_scale_ppm, drop_animations)
    out_bytes = glb_core.write_glb(out_scene)
    reparsed = glb_core.parse_glb(out_bytes)
    bad = check_post(src, reparsed, info["k_ppm"])
    receipt = {"schema": SCHEMA, "input_sha256": glb_core.sha256(data), "output_sha256": glb_core.sha256(out_bytes),
               "k_ppm": info["k_ppm"], "prims": len(reparsed.prims), "failed": bad}
    receipt.update(measure(reparsed))
    if bad:
        receipt["status"], receipt["rc"] = "FAIL", RC_FAIL
        raise NormalizeError(RC_FAIL, "E_POSTCONDITION", ",".join(bad))
    receipt["status"], receipt["rc"] = "PASS", RC_OK
    return out_bytes, receipt


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="normalize")
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst")
    ap.add_argument("--target-height-um", type=int)
    ap.add_argument("--unit-scale-ppm", type=int, default=PPM)
    ap.add_argument("--drop-animations", action="store_true")
    ap.add_argument("--prev-receipt-sha256")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        print(canon({"schema": SCHEMA, "status": "USAGE", "rc": RC_USAGE}))
        return RC_USAGE
    try:
        with open(a.src, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print(canon({"schema": SCHEMA, "status": "E_IO", "rc": RC_USAGE, "reason": str(exc)}))
        return RC_USAGE
    try:
        out_bytes, rec = run_bytes(data, a.target_height_um, a.unit_scale_ppm, a.drop_animations)
    except GlbError as exc:
        print(canon({"schema": SCHEMA, "status": getattr(exc, "code", "E_SCHEMA"), "rc": RC_USAGE}))
        return RC_USAGE
    except NormalizeError as exc:
        print(canon({"schema": SCHEMA, "status": exc.code, "rc": exc.rc, "reason": exc.reason,
                     "input_sha256": glb_core.sha256(data)}))
        return exc.rc
    rec["prev_receipt_sha256"] = a.prev_receipt_sha256
    if a.dst:
        with open(a.dst, "wb") as fh:
            fh.write(out_bytes)
    print(canon(rec))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
