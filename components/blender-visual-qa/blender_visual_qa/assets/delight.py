"""bv.assets.delight: de-light a baked diffuse map by dividing out a Cycles AO / irradiance bake (stdlib only).

VG-0252 fixed-point kernel in Q16: a' = max(a, min(FLOOR, 1.0)); r = min(1.0, floor((d*65536 + a'//2) / a')), so the gain is capped
at 65536/FLOOR (16x) and a zero-irradiance texel never divides by zero. Baked-occlusion fraction F = sum(max(r-d, 0)) / sum(d) in Q16.
The Cycles bake itself runs in Blender; this host module consumes the 8-bit diffuse and irradiance PNGs it produced.
Status is PREVIEW (rc 3) until ASSET_DELIGHT_CALIBRATED is set from a measured receipt; calibrated, PASS rc 0.
Exit codes: 0 PASS | 2 usage/bad PNG/dims mismatch | 3 PREVIEW (uncalibrated threshold) | 5 gate fail (kernel darkened a texel, or
--fail-baked and F >= threshold). Receipt schema bvqa.asset-delight/v1, sorted canonical JSON, bound to input_sha256 and prev_sha256.
"""
# @module {"id":"bv.assets.delight","layer":4,"owner":"BASSETS"}
import argparse
import sys

from blender_visual_qa.assets.orm_pack import OrmError, canon, decode_png, encode_png, _sha

try:  # limits.py is INT-owned; the fallbacks mirror the requested REG-REQ rows.
    from blender_visual_qa.assets.limits import ASSET_DELIGHT_BAKED_FRAC_Q16, ASSET_DELIGHT_FLOOR_Q16
except ImportError:  # pragma: no cover
    ASSET_DELIGHT_FLOOR_Q16 = 4096
    ASSET_DELIGHT_BAKED_FRAC_Q16 = 6554

SCHEMA = "bvqa.asset-delight/v1"
RC_OK, RC_USAGE, RC_PREVIEW, RC_FAIL = 0, 2, 3, 5
# No measured receipt backs the two thresholds yet (X3:uncalibrated): every run is PREVIEW until this flips.
ASSET_DELIGHT_CALIBRATED = False
Q16_ONE = 65536
BYTE_MAX = 255
AZIMUTHS = 8
# STORAGE LAW L6: the de-lit map is the one deliverable (an asset, not a frame; no scratch image is ever written); capped.
DELIVERABLE_CAP = 64 << 20


def to_q16(b):
    return (b * Q16_ONE + BYTE_MAX // 2) // BYTE_MAX


def from_q16(q):
    return (min(q, Q16_ONE) * BYTE_MAX + Q16_ONE // 2) // Q16_ONE


def delight_q16(d, a, floor=None):
    """d, a in Q16 -> de-lit albedo in Q16, clamped to 1.0."""
    floor = ASSET_DELIGHT_FLOOR_Q16 if floor is None else floor
    ap = max(a, min(floor, Q16_ONE))
    return min(Q16_ONE, (d * Q16_ONE + ap // 2) // ap)


def delight_plane(diffuse, irradiance, floor=None):
    """Per-texel bytes -> (albedo bytes, baked fraction Q16, darkened texel count)."""
    out = bytearray(len(diffuse))
    gain = total = dark = 0
    for i, (db, ab) in enumerate(zip(diffuse, irradiance)):
        d, r = to_q16(db), delight_q16(to_q16(db), to_q16(ab), floor)
        if r < d:
            dark += 1
        gain += max(r - d, 0)
        total += d
        out[i] = from_q16(r)
    return bytes(out), (gain * Q16_ONE // total if total else 0), dark


def azimuth_variance(frames):
    """frames: AZIMUTHS equal-length byte planes (same asset lit from 8 azimuths) -> sum of per-texel population variance (x n^2)."""
    n = len(frames)
    if n != AZIMUTHS or len({len(f) for f in frames}) != 1:
        raise OrmError("need %d equal-length azimuth planes" % AZIMUTHS)
    return sum(n * sum(v * v for v in col) - sum(col) ** 2 for col in zip(*frames))


def make_receipt(rc, body, input_sha256, prev_sha256):
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": input_sha256, "prev_sha256": prev_sha256})
    return rec


def _load(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise OrmError("cannot read %s: %s" % (path, exc))
    w, h, ch, px = decode_png(data)
    return w, h, ch, px, data


def run(argv):
    """CLI core. Returns (rc, receipt_or_None)."""
    ap = argparse.ArgumentParser(prog="delight", description="divide a Cycles AO/irradiance bake out of a baked diffuse map")
    ap.add_argument("--diffuse", required=True, help="baked diffuse PNG (gray/RGB/RGBA)")
    ap.add_argument("--irradiance", required=True, help="AO / irradiance PNG, same size (first channel used)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fail-baked", action="store_true", help="rc 5 when the baked-occlusion fraction reaches the threshold")
    ap.add_argument("--prev-receipt", default=None)
    ap.add_argument("--receipt", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return RC_USAGE, None
    try:
        prev = ""
        if a.prev_receipt:
            with open(a.prev_receipt, "rb") as fh:
                prev = _sha(fh.read())
        w, h, ch, dpx, ddata = _load(a.diffuse)
        iw, ih, ich, ipx, idata = _load(a.irradiance)
        if (w, h) != (iw, ih):
            raise OrmError("map dimensions differ: %dx%d vs %dx%d" % (w, h, iw, ih))
        irr = ipx[0::ich]
        n = w * h
        frac, dark = 0, 0
        out = bytearray(n * ch)
        for c in range(min(ch, 3)):  # colour channels are divided, alpha is carried through
            plane, f, d = delight_plane(dpx[c::ch], irr)
            out[c::ch] = plane
            frac, dark = max(frac, f), dark + d
        if ch == 4:
            out[3::4] = dpx[3::4]
        png = encode_png(w, h, bytes(out)) if ch == 4 else _repack(w, h, ch, bytes(out))
        if len(png) > DELIVERABLE_CAP:
            raise OrmError("de-lit map %d bytes > deliverable cap %d" % (len(png), DELIVERABLE_CAP))
        with open(a.out, "wb") as fh:
            fh.write(png)
    except (OSError, OrmError) as exc:
        sys.stderr.write("delight: %s\n" % (exc,))
        return RC_USAGE, None
    baked = frac >= ASSET_DELIGHT_BAKED_FRAC_Q16
    if dark or (a.fail_baked and baked):
        rc, status = RC_FAIL, "FAIL"
    elif not ASSET_DELIGHT_CALIBRATED:
        rc, status = RC_PREVIEW, "PREVIEW"
    else:
        rc, status = RC_OK, "PASS"
    body = {"width": w, "height": h, "floor_q16": ASSET_DELIGHT_FLOOR_Q16, "baked_frac_q16": frac,
            "baked_frac_threshold_q16": ASSET_DELIGHT_BAKED_FRAC_Q16, "baked": baked, "darkened_texels": dark,
            "calibrated": ASSET_DELIGHT_CALIBRATED, "out_sha256": _sha(png), "status": status}
    rec = make_receipt(rc, body, _sha(ddata + b"\0" + idata), prev)
    if a.receipt:
        with open(a.receipt, "w") as fh:
            fh.write(canon(rec))
    return rc, rec


def _repack(w, h, ch, px):
    """Gray / RGB output: widen to RGBA with opaque alpha (encode_png writes RGBA only)."""
    rgba = bytearray(w * h * 4)
    for i in range(w * h):
        r = px[i * ch]
        g = px[i * ch + 1] if ch >= 3 else r
        b = px[i * ch + 2] if ch >= 3 else r
        rgba[i * 4:i * 4 + 4] = bytes((r, g, b, BYTE_MAX))
    return encode_png(w, h, bytes(rgba))


def main(argv=None):
    rc, rec = run(sys.argv[1:] if argv is None else argv)
    if rec is not None:
        sys.stdout.write(canon(rec))
    return rc


if __name__ == "__main__":
    sys.exit(main())
