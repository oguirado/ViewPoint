"""bv.assets.ortho4: four canonical orthographic silhouettes + rig/asset triage (VG-0249, stdlib, integer math).

Front/Back/Right/Left silhouettes are conservative rasterisations (a cell is set when a projected triangle touches it,
closed intervals) of every primitive of a GLB, on one shared res x res grid centred on the scene AABB. Checks:
  C1 connectivity: 8-connected components per view <= --max-components (a floating accessory is an ASSET fault);
  C2 rig invariants: Back == mirror(Front), Left == mirror(Right) (XOR ppm <= budget) and equal occupied row sets
     across the 4 views; a violation is a RIG fault (camera basis/roll/scale), never blamed on the asset.
Views (glTF Y up): front looks along -Z (u=+x), back along +Z (u=-x), right along -X (u=-z), left along +X (u=+z).
Exit codes: 0 pass | 2 usage/schema/IO | 3 empty or flat scene (zero checks) | 5 rig or asset fault.
Receipt schema bvqa.asset-ortho4/v1, sorted canonical JSON, bound to input_sha256 and prev_sha256.
"""
# @module {"id":"bv.assets.ortho4","layer":4,"owner":"BASSETS"}
import argparse
import hashlib
import json
import sys

from blender_visual_qa.assets import limits

SCHEMA = "bvqa.asset-ortho4/v1"
PPM = 1000000
RES = getattr(limits, "ASSET_ORTHO4_RES", 64)
MIRROR_XOR_MAX_PPM = getattr(limits, "ASSET_ORTHO4_MIRROR_XOR_MAX_PPM", 20000)
VIEWS = ("front", "back", "right", "left")
# view -> (u axis, u sign, v axis, v sign); v is screen-up.
VIEW_AXES = {"front": (0, 1, 1, 1), "back": (0, -1, 1, 1), "right": (2, -1, 1, 1), "left": (2, 1, 1, 1)}
MIRROR_PAIRS = (("front", "back"), ("right", "left"))


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def scene_triangles(scene):
    """World-space triangles (um) of every primitive."""
    from blender_visual_qa.assets import glb_core
    tris = []
    for prim in scene.prims:
        w = glb_core.world_positions_um(prim)
        idx = prim.indices
        for i in range(0, len(idx) - len(idx) % 3, 3):
            tris.append((w[idx[i]], w[idx[i + 1]], w[idx[i + 2]]))
    return tris


def frame(tris):
    """Shared grid frame: (doubled AABB centre per axis, E = largest AABB extent in um). E == 0 means flat/empty."""
    if not tris:
        return None, 0
    lo = [min(p[a] for t in tris for p in t) for a in range(3)]
    hi = [max(p[a] for t in tris for p in t) for a in range(3)]
    return tuple(lo[a] + hi[a] for a in range(3)), max(hi[a] - lo[a] for a in range(3))


def _proj(p, axes, c2, res):
    ua, us, va, vs = axes
    return (us * (2 * p[ua] - c2[ua]) * res, vs * (2 * p[va] - c2[va]) * res)


def _overlap(tri2, x0, x1, y0, y1):
    """Closed triangle (or degenerate segment/point) vs closed box, separating-axis test in exact integers."""
    if max(p[0] for p in tri2) < x0 or min(p[0] for p in tri2) > x1:
        return False
    if max(p[1] for p in tri2) < y0 or min(p[1] for p in tri2) > y1:
        return False
    box = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    for i in range(3):
        a, b = tri2[i], tri2[(i + 1) % 3]
        n = (a[1] - b[1], b[0] - a[0])
        if n == (0, 0):
            continue
        tp = [n[0] * p[0] + n[1] * p[1] for p in tri2]
        bp = [n[0] * p[0] + n[1] * p[1] for p in box]
        if max(tp) < min(bp) or min(tp) > max(bp):
            return False
    return True


def rasterise(tris, view, c2, ext, res, axes=None):
    """Silhouette of `view` as a list of `res` row bitmasks (row 0 = top, bit c = column c)."""
    axes = axes or VIEW_AXES[view]
    half, w = ext * res, 2 * ext
    rows = [0] * res
    for t in tris:
        q = [_proj(p, axes, c2, res) for p in t]
        us, vs = [p[0] for p in q], [p[1] for p in q]
        c_lo = max(0, -(-(min(us) + half) // w) - 1)
        c_hi = min(res - 1, (max(us) + half) // w)
        r_lo = max(0, -(-(half - max(vs)) // w) - 1)
        r_hi = min(res - 1, (half - min(vs)) // w)
        for r in range(r_lo, r_hi + 1):
            y1 = half - w * r
            y0 = y1 - w
            for c in range(c_lo, c_hi + 1):
                if not (rows[r] >> c) & 1:
                    x0 = -half + w * c
                    if _overlap(q, x0, x0 + w, y0, y1):
                        rows[r] |= 1 << c
    return rows


def mirror(rows, res):
    return [int(format(r, "0%db" % res)[::-1], 2) for r in rows]


def popcount(rows):
    return sum(bin(r).count("1") for r in rows)


def components(rows, res):
    """8-connected components of a bitboard."""
    seen, n = set(), 0
    for r0 in range(res):
        for c0 in range(res):
            if (rows[r0] >> c0) & 1 and (r0, c0) not in seen:
                n += 1
                stack = [(r0, c0)]
                seen.add((r0, c0))
                while stack:
                    r, c = stack.pop()
                    for dr in (-1, 0, 1):
                        for dc in (-1, 0, 1):
                            rr, cc = r + dr, c + dc
                            if 0 <= rr < res and 0 <= cc < res and (rows[rr] >> cc) & 1 and (rr, cc) not in seen:
                                seen.add((rr, cc))
                                stack.append((rr, cc))
    return n


def silhouettes(tris, res=RES):
    c2, ext = frame(tris)
    if not ext:
        return None
    return {v: rasterise(tris, v, c2, ext, res) for v in VIEWS}


def triage(sil, res=RES, max_components=1, xor_max_ppm=MIRROR_XOR_MAX_PPM):
    """(rc, body) from the 4 silhouettes."""
    checks, metrics = [], {"res": res}

    def chk(cid, ok, value, limit, kind):
        checks.append({"id": cid, "ok": bool(ok), "value": value, "limit": limit, "fault": None if ok else kind})

    for a, b in MIRROR_PAIRS:
        mb = mirror(sil[b], res)
        xor = popcount([x ^ y for x, y in zip(sil[a], mb)])
        union = popcount([x | y for x, y in zip(sil[a], mb)])
        ppm = xor * PPM // max(1, union)
        chk("mirror_%s_%s" % (a, b), ppm <= xor_max_ppm, ppm, xor_max_ppm, "rig")
    row_sets = {v: [r for r in range(res) if sil[v][r]] for v in VIEWS}
    chk("equal_row_sets", all(row_sets[v] == row_sets["front"] for v in VIEWS),
        {v: [rs[0], rs[-1], len(rs)] if rs else [] for v, rs in row_sets.items()}, "equal", "rig")
    for v in VIEWS:
        n = components(sil[v], res)
        metrics["occupied_%s" % v] = popcount(sil[v])
        chk("connected_%s" % v, n <= max_components, n, max_components, "asset")
    failed = [c["id"] for c in checks if not c["ok"]]
    rig = any(c["fault"] == "rig" for c in checks)
    body = {"status": "FAIL" if failed else "PASS", "checks": checks, "failed": failed, "metrics": metrics,
            "rig_fault": rig, "asset_fault": any(c["fault"] == "asset" for c in checks),
            "silhouette_sha256": hashlib.sha256(canon({v: sil[v] for v in VIEWS}).encode()).hexdigest()}
    return (limits.RC_FAIL if failed else limits.RC_OK), body


def gate_scene(scene, res=RES, max_components=1, xor_max_ppm=MIRROR_XOR_MAX_PPM):
    sil = silhouettes(scene_triangles(scene), res)
    if sil is None:
        return limits.RC_INCOMPLETE, {"status": "INCOMPLETE_ZERO_CHECKS", "reason": "empty or flat scene",
                                      "checks": [], "failed": []}
    return triage(sil, res, max_components, xor_max_ppm)


# @anchor:bv.assets.ortho4.run
def run(argv):
    """CLI core. Returns (rc, receipt_or_None). main() prints the canonical receipt."""
    ap = argparse.ArgumentParser(prog="ortho4", description="4-ortho silhouettes, rig mirror + connectivity triage")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--res", type=int, default=RES)
    ap.add_argument("--max-components", type=int, default=1)
    ap.add_argument("--xor-max-ppm", type=int, default=MIRROR_XOR_MAX_PPM)
    ap.add_argument("--prev-receipt", default=None)
    ap.add_argument("--out", default=None, help="receipt path")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return limits.RC_USAGE, None
    if a.res < 1 or a.max_components < 1:
        return limits.RC_USAGE, None
    from blender_visual_qa.assets import glb_core
    try:
        with open(a.inp, "rb") as fh:
            raw = fh.read()
        prev = ""
        if a.prev_receipt:
            with open(a.prev_receipt, "rb") as fh:
                prev = hashlib.sha256(fh.read()).hexdigest()
        scene = glb_core.parse_glb(raw)
    except (OSError, glb_core.GlbError) as exc:
        sys.stderr.write("ortho4: %s\n" % (exc,))
        return limits.RC_USAGE, None
    rc, body = gate_scene(scene, a.res, a.max_components, a.xor_max_ppm)
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": hashlib.sha256(raw).hexdigest(), "prev_sha256": prev})
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
