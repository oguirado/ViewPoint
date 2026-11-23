"""bv.assets.pbr_validate: PBR BRDF parameter validator for GLB materials + optional albedo/ORM PNGs (stdlib only).

AG3 N4 (VG-0037). Material factors are quantised to integer permille / sRGB bytes; every check compares integers.
Checks: param_range, rough_floor (GGX floor, same byte as orm_pack), albedo_range (dielectrics), metal_mid
(metallic should be ~0 or ~1), f0_low (metals need a bright base colour), albedo_texture, orm_floor.
Exit codes: 0 pass | 2 usage/schema/IO | 3 nothing to check | 5 gate fail.
Receipt schema bvqa.asset-pbr-validate/v1, sorted canonical JSON, bound to input_sha256 and prev_sha256.
"""
import argparse
import hashlib
import json
import sys

from blender_visual_qa.assets import limits as _limits
from blender_visual_qa.assets import orm_pack

SCHEMA = "bvqa.asset-pbr-validate/v1"
RC_OK, RC_USAGE, RC_EMPTY, RC_FAIL = 0, 2, 3, 5
PPM = 1000000
PERMILLE = 1000
BYTE_MAX = orm_pack.BYTE_MAX
# Registered in limits.py by INT (REG-REQ on bus_N4); the fallbacks keep the lane testable before registration.
ROUGH_MIN_BYTE = _limits.ASSET_ORM_ROUGH_MIN_BYTE
ALBEDO_MIN_BYTE = getattr(_limits, "ASSET_PBR_ALBEDO_MIN_BYTE", 30)
ALBEDO_MAX_BYTE = getattr(_limits, "ASSET_PBR_ALBEDO_MAX_BYTE", 240)
ALBEDO_OUT_MAX_PPM = getattr(_limits, "ASSET_PBR_ALBEDO_OUT_MAX_PPM", 10000)
METAL_MID_LO = getattr(_limits, "ASSET_PBR_METAL_MID_LO_PERMILLE", 100)
METAL_MID_HI = getattr(_limits, "ASSET_PBR_METAL_MID_HI_PERMILLE", 900)
F0_MIN_BYTE = getattr(_limits, "ASSET_PBR_F0_MIN_BYTE", 170)
LUMA_W = (2126, 7152, 722)  # Rec.709 weights, sum 10000


def _permille(x):
    return int(round(float(x) * PERMILLE))


def _srgb_byte(linear):
    """Linear 0..1 -> sRGB byte (clamped, rounded); the only float step, quantised immediately."""
    c = min(max(float(linear), 0.0), 1.0)
    v = c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return int(round(v * BYTE_MAX))


def _luma_byte(rgb):
    return sum(w * c for w, c in zip(LUMA_W, rgb)) // sum(LUMA_W)


def material_params(mat):
    """glTF material dict -> (base_rgba_permille list[4], metal_permille, rough_permille); spec defaults applied."""
    pbr = mat.get("pbrMetallicRoughness") or {}
    base = pbr.get("baseColorFactor", [1, 1, 1, 1])
    if not isinstance(base, list) or len(base) != 4:
        raise ValueError("baseColorFactor must have 4 components")
    return ([_permille(c) for c in base], _permille(pbr.get("metallicFactor", 1)),
            _permille(pbr.get("roughnessFactor", 1)))


def check_material(mat, index=0):
    """-> (metrics, failed ids) for one glTF material."""
    base, metal, rough = material_params(mat)
    rgb = [_srgb_byte(c / PERMILLE) for c in base[:3]]
    rough_byte = (min(max(rough, 0), PERMILLE) * BYTE_MAX + PERMILLE // 2) // PERMILLE
    luma = _luma_byte(rgb)
    failed = []
    if any(v < 0 or v > PERMILLE for v in base + [metal, rough]):
        failed.append("param_range")
    if rough_byte < ROUGH_MIN_BYTE:
        failed.append("rough_floor")
    dielectric = metal <= METAL_MID_LO
    if dielectric and (min(rgb) < ALBEDO_MIN_BYTE or max(rgb) > ALBEDO_MAX_BYTE):
        failed.append("albedo_range")
    if METAL_MID_LO < metal < METAL_MID_HI:
        failed.append("metal_mid")
    if metal >= METAL_MID_HI and luma < F0_MIN_BYTE:
        failed.append("f0_low")
    metrics = {"name": str(mat.get("name", "material%d" % index)), "base_srgb_bytes": rgb,
               "metal_permille": metal, "rough_permille": rough, "rough_byte": rough_byte, "luma_byte": luma,
               "failed": sorted(failed)}
    return metrics, failed


def check_albedo_png(data):
    """-> (metrics, failed) for an albedo PNG: share of pixels outside the dielectric albedo window."""
    w, h, ch, px = orm_pack.decode_png(data)
    step = ch if ch >= 3 else 1
    chans = range(3) if ch >= 3 else range(1)
    out = n = 0
    for i in range(0, len(px), ch):
        n += 1
        if any(px[i + c] < ALBEDO_MIN_BYTE or px[i + c] > ALBEDO_MAX_BYTE for c in chans):
            out += 1
    ppm = out * PPM // n if n else 0
    failed = ["albedo_texture"] if ppm > ALBEDO_OUT_MAX_PPM else []
    return {"width": w, "height": h, "pixels": n, "out_of_range_px": out, "out_of_range_ppm": ppm,
            "channels": step}, failed


def validate_glb(raw):
    from blender_visual_qa.assets import glb_core
    doc, _ = glb_core._split(raw)
    mats = doc.get("materials", [])
    if not isinstance(mats, list):
        raise glb_core.GlbError("E_SCHEMA", "materials is not a list")
    out, failed = [], set()
    for i, m in enumerate(mats):
        try:
            met, f = check_material(m, i)
        except (ValueError, TypeError, AttributeError) as exc:
            raise glb_core.GlbError("E_SCHEMA", "material %d: %s" % (i, exc))
        out.append(met)
        failed.update(f)
    return out, failed


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def make_receipt(rc, body, input_sha256, prev_sha256):
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": input_sha256, "prev_sha256": prev_sha256})
    return rec


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def run(argv):
    """CLI core. Returns (rc, receipt_or_None). Prints nothing; main() prints the canonical receipt."""
    ap = argparse.ArgumentParser(prog="pbr-validate", description="PBR parameter validator (GLB materials, PNG maps)")
    ap.add_argument("--glb", default=None)
    ap.add_argument("--albedo", default=None, help="albedo PNG")
    ap.add_argument("--orm", default=None, help="ORM PNG (green = roughness)")
    ap.add_argument("--prev-receipt", default=None, help="receipt file whose sha256 is chained as prev_sha256")
    ap.add_argument("--out", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return RC_USAGE, None
    if not (a.glb or a.albedo or a.orm):
        sys.stderr.write("pbr-validate: need --glb, --albedo or --orm\n")
        return RC_USAGE, None
    from blender_visual_qa.assets import glb_core
    body = {"materials": [], "albedo": None, "orm": None}
    failed, srcs, checks = set(), [], 0
    try:
        prev = hashlib.sha256(_read(a.prev_receipt)).hexdigest() if a.prev_receipt else ""
        if a.glb:
            raw = _read(a.glb)
            srcs.append(raw)
            body["materials"], f = validate_glb(raw)
            checks += len(body["materials"])
            failed.update(f)
        if a.albedo:
            raw = _read(a.albedo)
            srcs.append(raw)
            body["albedo"], f = check_albedo_png(raw)
            checks += 1
            failed.update(f)
        if a.orm:
            raw = _read(a.orm)
            srcs.append(raw)
            bad, first, w, h = orm_pack.validate_orm(raw)
            body["orm"] = {"width": w, "height": h, "floor_byte": ROUGH_MIN_BYTE, "violations": bad,
                           "first_bad_pixel": first}
            checks += 1
            if bad:
                failed.add("orm_floor")
    except (OSError, glb_core.GlbError, orm_pack.OrmError) as exc:
        sys.stderr.write("pbr-validate: %s\n" % (exc,))
        return RC_USAGE, None
    body["checks"] = checks
    body["failed"] = sorted(failed)
    if not checks:
        rc, body["status"] = RC_EMPTY, "INCOMPLETE_ZERO_CHECKS"
    else:
        rc, body["status"] = (RC_FAIL, "FAIL") if failed else (RC_OK, "PASS")
    rec = make_receipt(rc, body, hashlib.sha256(b"\0".join(srcs)).hexdigest(), prev)
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
