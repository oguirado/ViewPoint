"""bv.assets.uv_gate: UV flip + stretch gate over EVERY primitive of a GLB (stdlib only, integer math).

AG3 N4 (VG-0234). Flip: a triangle whose UV winding is opposite to the dominant UV winding of its primitive.
Stretch: anisotropy sigma_max/sigma_min of the UV->3D Jacobian, in permille (1000 = isometric), computed with
bigint Gram-matrix eigenvalues (no floats). UV scale and mesh scale cancel in the ratio.
Exit codes: 0 pass | 2 usage/schema/IO | 3 nothing to check | 5 gate fail.
Receipt schema bvqa.asset-uv-gate/v1, sorted canonical JSON, bound to input_sha256 and prev_sha256.
"""
import argparse
import hashlib
import json
import sys
from math import isqrt

from blender_visual_qa.assets import limits as _limits

SCHEMA = "bvqa.asset-uv-gate/v1"
RC_OK, RC_USAGE, RC_EMPTY, RC_FAIL = 0, 2, 3, 5
PPM = 1000000
PERMILLE = 1000
# Registered in limits.py by INT (REG-REQ on bus_N4); the fallbacks keep the lane testable before registration.
FLIP_MAX_PPM = getattr(_limits, "ASSET_UV_FLIP_MAX_PPM", 0)
STRETCH_MAX_PERMILLE = getattr(_limits, "ASSET_UV_STRETCH_MAX_PERMILLE", 4000)
STRETCH_TRIS_MAX_PPM = getattr(_limits, "ASSET_UV_STRETCH_TRIS_MAX_PPM", 50000)
STRETCH_INF = 1 << 62  # reported for a triangle with a collapsed UV axis


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def tri_uv_metrics(p0, p1, p2, q0, q1, q2):
    """-> (uv_area2, stretch_permille or None). stretch None = 3D-degenerate triangle (not this gate's business).
    uv_area2 is the signed doubled UV area (0 = collapsed UV)."""
    u1, v1 = q1[0] - q0[0], q1[1] - q0[1]
    u2, v2 = q2[0] - q0[0], q2[1] - q0[1]
    area2 = u1 * v2 - u2 * v1
    e1, e2 = _sub(p1, p0), _sub(p2, p0)
    cross = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
    if cross == (0, 0, 0):
        return area2, None
    if not area2:
        return area2, STRETCH_INF
    ss = tuple(e1[i] * v2 - e2[i] * v1 for i in range(3))
    st = tuple(e2[i] * u1 - e1[i] * u2 for i in range(3))
    a, b, c = _dot(ss, ss), _dot(ss, st), _dot(st, st)
    tr = a + c
    det = a * c - b * b
    s = isqrt((a - c) * (a - c) + 4 * b * b)
    lmax2 = tr + s  # 2 * lambda_max (floor-safe: s underestimates by < 1)
    lmin2 = (4 * det) // lmax2 if lmax2 else 0
    if not lmin2:
        return area2, STRETCH_INF
    return area2, isqrt(lmax2 * PPM // lmin2)  # sqrt(lmax/lmin) * 1000


def gate_prim(prim, max_stretch=None, allow_mirror=False, require_uv=True):
    """-> (metrics, failed) for one primitive."""
    max_stretch = STRETCH_MAX_PERMILLE if max_stretch is None else max_stretch
    pos, idx = prim.positions_um, prim.indices
    ntri = len(idx) // 3
    uv_present = prim.uvs_q16 is not None and len(prim.uvs_q16) == len(pos)
    metrics = {"tris": ntri, "uv_tris": 0, "flipped_tris": 0, "flipped_ppm": 0, "uv_zero_area": 0,
               "stretch_max_permille": 0, "stretch_over_tris": 0, "stretch_over_ppm": 0}
    failed = []
    if not uv_present:
        if require_uv:
            failed.append("uv_missing")
        return metrics, failed
    areas, stretches = [], []
    for f in range(ntri):
        i0, i1, i2 = idx[3 * f:3 * f + 3]
        a2, st = tri_uv_metrics(pos[i0], pos[i1], pos[i2], prim.uvs_q16[i0], prim.uvs_q16[i1], prim.uvs_q16[i2])
        if st is None:
            continue
        areas.append(a2)
        stretches.append(st)
    pos_n = sum(1 for a in areas if a > 0)
    neg_n = sum(1 for a in areas if a < 0)
    dominant = -1 if neg_n > pos_n else 1  # tie -> positive
    flipped = sum(1 for a in areas if a * dominant < 0)
    zero = sum(1 for a in areas if not a)
    over = sum(1 for s in stretches if s > max_stretch)
    n = len(areas)
    metrics.update({"uv_tris": n, "flipped_tris": flipped, "flipped_ppm": flipped * PPM // n if n else 0,
                    "uv_zero_area": zero, "stretch_max_permille": max(stretches) if stretches else 0,
                    "stretch_over_tris": over, "stretch_over_ppm": over * PPM // n if n else 0})
    if zero:
        failed.append("uv_degenerate")
    if not allow_mirror and metrics["flipped_ppm"] > FLIP_MAX_PPM:
        failed.append("uv_flipped")
    if metrics["stretch_over_ppm"] > STRETCH_TRIS_MAX_PPM:
        failed.append("uv_stretch")
    return metrics, failed


def gate_scene(scene, max_stretch=None, allow_mirror=False, require_uv=True):
    """Returns (rc, body) where body is the receipt payload without hashes."""
    max_stretch = STRETCH_MAX_PERMILLE if max_stretch is None else max_stretch
    prims, failed = [], set()
    for p in scene.prims:
        if not len(p.indices):
            continue
        m, f = gate_prim(p, max_stretch, allow_mirror, require_uv)
        m["name"] = str(p.name)
        m["failed"] = sorted(f)
        prims.append(m)
        failed.update(f)
    if not prims:
        return RC_EMPTY, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "empty mesh: zero checks executed",
                          "prims": [], "failed": []}
    body = {"status": "FAIL" if failed else "PASS", "prims": prims, "failed": sorted(failed),
            "primitives_checked": len(prims),
            "options": {"max_stretch_permille": max_stretch, "allow_mirror": int(allow_mirror),
                        "require_uv": int(require_uv)}}
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
    ap = argparse.ArgumentParser(prog="uv-gate", description="UV flip + stretch gate over all primitives of a GLB")
    ap.add_argument("--mesh", required=True)
    ap.add_argument("--max-stretch", type=int, default=STRETCH_MAX_PERMILLE, help="permille, 1000 = isometric")
    ap.add_argument("--allow-mirror", default="0", help="1 = mirrored UV islands are legitimate (skip flip check)")
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
        sys.stderr.write("uv-gate: %s\n" % (exc,))
        return RC_USAGE, None
    rc, body = gate_scene(scene, a.max_stretch, _bool01(a.allow_mirror), _bool01(a.require_uv))
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
