"""Integer, deterministic display-pixel metrics; no raw-difference flicker verdict.

Reference deltas cancel real motion and illumination in the matched reference.
Motion compensation uses independent albedo/normal guide blocks. Its luminance
residual is only a proxy: unmatched/occluded blocks are excluded and counted;
changing illumination and motion beyond the search radius remain limitations.
All sums use int64 with explicit bounds; null is never zero. VE-0014, VE-0015.
"""
from pathlib import Path

from tools.render.pipeline import Refusal

PPM = 1000000


def modules():
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        raise Refusal(3, "pixel_decoder_unavailable") from None
    return np, Image


def rgb(path):
    np, Image = modules()
    with Image.open(path) as im:
        if im.format != "PNG" or not (0 < im.width * im.height <= 40000000):
            raise Refusal(2, "unsupported_image")
        return np.asarray(im.convert("RGB"), dtype=np.int32)


def luma(a):
    np, _ = modules()
    if a.ndim != 3 or a.shape[2] != 3 or a.dtype.kind not in "iu" or not a.size or a.max() > 255 or a.min() < 0:
        raise Refusal(2, "rgb_required")
    a = a.astype(np.int64)
    return ((77 * a[:, :, 0] + 150 * a[:, :, 1] + 29 * a[:, :, 2] + 128) // 256).astype(np.int32)


def matched(*arrays):
    if not arrays or any(a.shape != arrays[0].shape for a in arrays) or arrays[0].ndim != 2 or not arrays[0].size:
        raise Refusal(2, "dimension_mismatch")
    if any(a.dtype.kind not in "iu" or a.min() < 0 or a.max() > 255 for a in arrays):
        raise Refusal(2, "integer_luma_required")


def ratio(numerator, denominator):
    return None if denominator == 0 else int(numerator) * PPM // int(denominator)


def ceiling_ppm(numerator, denominator):
    return (int(numerator) * PPM + int(denominator) - 1) // int(denominator)


def rgb_mse_ppm(candidate, reference):
    """Ceiling of RGB MSE / 255**2 in ppm; <=1000 implies PSNR >=30 dB.

    This is display-referred fidelity, not a perceptual or calibration claim.
    Widen before subtraction/squaring; the decoder bounds the pixel count.
    """
    np, _ = modules()
    luma(candidate)
    luma(reference)
    if candidate.shape != reference.shape or candidate.size > 120000000:
        raise Refusal(2, "dimension_mismatch")
    delta = candidate.astype(np.int64) - reference.astype(np.int64)
    return ceiling_ppm((delta * delta).sum(dtype=np.int64), 255**2 * candidate.size)


def reference_delta(c0, c1, r0, r1):
    np, _ = modules()
    matched(c0, c1, r0, r1)
    residual = np.abs((c1.astype(np.int64) - c0) - (r1.astype(np.int64) - r0))
    return ceiling_ppm(residual.sum(dtype=np.int64), 255 * c0.size)


def gradient(a):
    np, _ = modules()
    a = a.astype(np.int64)
    return int(np.abs(a[1:] - a[:-1]).sum(dtype=np.int64) + np.abs(a[:, 1:] - a[:, :-1]).sum(dtype=np.int64))


def detail(candidate, reference, *, tile=32):
    np, _ = modules()
    matched(candidate, reference)
    if type(tile) is not int or tile < 2:
        raise Refusal(2, "invalid_tile")
    g0, g1 = gradient(reference), gradient(candidate)
    local = []
    worst = 0
    for y in range(0, candidate.shape[0], tile):
        for x in range(0, candidate.shape[1], tile):
            c, r = candidate[y:y+tile, x:x+tile], reference[y:y+tile, x:x+tile]
            g = gradient(r)
            # TARGET: ignore featureless reference tiles; never synthesize a ratio.
            if g >= 255:
                local.append(ratio(gradient(c), g))
            worst = max(worst, ceiling_ppm(np.abs(c.astype(np.int64) - r).sum(), 255 * c.size))
    return {"detail_ratio_ppm": ratio(g1, g0), "detail_tile_min_ppm": min(local) if local else None,
            "detail_reference_gradient": g0, "detail_candidate_gradient": g1,
            "detail_feature_tiles": len(local), "worst_tile_error_ppm": worst}


def guide(directory):
    """Quantize linear albedo and signed normals once; subsequent metrics are integer.

    PFM conversion is not a floating-point metric reduction. Finite IEEE float32
    samples are clipped and rounded half-up to an explicitly recorded 8-bit grid.
    """
    np, _ = modules()
    arrays = []
    dimensions = None
    for name in ("albedo", "normal"):
        with (Path(directory) / (name + ".pfm")).open("rb") as stream:
            if stream.readline() != b"PF\n":
                raise Refusal(2, "pfm_header")
            w, h = map(int, stream.readline().split())
            if stream.readline().strip() != b"-1.0" or not (0 < w * h <= 40000000):
                raise Refusal(2, "pfm_dimensions_or_scale")
            data = stream.read(w * h * 12 + 1)
        if len(data) != w * h * 12:
            raise Refusal(2, "pfm_size")
        if dimensions is not None and dimensions != (w, h):
            raise Refusal(2, "guide_dimensions")
        dimensions = w, h
        a = np.frombuffer(data, dtype="<f4").reshape(h, w, 3)[::-1]
        if not np.isfinite(a).all():
            raise Refusal(3, "nonfinite_guide")
        if name == "normal":
            a = (a + 1) / 2
        arrays.append(np.floor(np.clip(a, 0, 1).astype(np.float64) * 255 + 0.5).astype(np.int32))
    return np.concatenate(arrays, axis=2)


def downsample(a, factor):
    h, w = a.shape[:2]
    h, w = h // factor * factor, w // factor * factor
    if h == 0 or w == 0:
        raise Refusal(3, "motion_image_too_small")
    if a.ndim == 2:
        return a[:h, :w].reshape(h // factor, factor, w // factor, factor).sum(axis=(1, 3), dtype="int64") // (factor * factor)
    return a[:h, :w].reshape(h // factor, factor, w // factor, factor, a.shape[2]).sum(axis=(1, 3), dtype="int64") // (factor * factor)


def motion_residual(previous, current, previous_guide, current_guide, *, factor=4, tile=8, radius=4, max_error=8):
    """Independent deterministic block search, tie-break: least motion then dy/dx.

    Reports a worst accepted tile, so a local flicker cannot be averaged away.
    Guide matching never consults candidate luminance. This cannot certify
    illumination changes as physical or artificial without reference evidence.
    """
    np, _ = modules()
    matched(previous, current)
    if any(type(v) is not int for v in (factor, tile, radius, max_error)) or not (1 <= factor <= 16 and 2 <= tile <= 64 and 0 <= radius <= 16 and 0 <= max_error <= 255):
        raise Refusal(2, "invalid_motion_policy")
    if previous_guide.shape != current_guide.shape or previous_guide.shape[:2] != current.shape:
        raise Refusal(2, "guide_dimensions")
    for g in (previous_guide, current_guide):
        if g.ndim != 3 or g.dtype.kind not in "iu" or g.min() < 0 or g.max() > 255:
            raise Refusal(2, "integer_guide_required")
    p, c = downsample(previous, factor), downsample(current, factor)
    pg, cg = downsample(previous_guide, factor), downsample(current_guide, factor)
    offsets = sorted(((dy, dx) for dy in range(-radius, radius+1) for dx in range(-radius, radius+1)),
                     key=lambda d: (d[0]*d[0]+d[1]*d[1], d[0], d[1]))
    total_error = accepted = 0
    worst = 0
    offsets_used = {}
    full_tile = tile * factor

    def refine(source_guide, target_patch, base_y, base_x, origin_y, origin_x):
        fine = sorted(((dy, dx) for dy in range(1-factor, factor) for dx in range(1-factor, factor)),
                      key=lambda d: ((base_y+d[0]-origin_y)**2+(base_x+d[1]-origin_x)**2, d[0], d[1]))
        best_fine = None
        for dy, dx in fine:
            sy, sx = base_y + dy, base_x + dx
            if sy < 0 or sx < 0 or sy+full_tile > source_guide.shape[0] or sx+full_tile > source_guide.shape[1]:
                continue
            error = int(np.abs(target_patch.astype(np.int64) - source_guide[sy:sy+full_tile, sx:sx+full_tile]).sum(dtype=np.int64))
            if best_fine is None or error < best_fine[0]:
                best_fine = error, sy, sx
        return best_fine

    def choose_refinement(source_guide, target_patch, candidates, oy, ox):
        refined = []
        # Keep competing coarse hypotheses: averaging can alias a half-cell move.
        for _, by, bx in sorted(candidates, key=lambda r: (r[0], (r[1]-oy)**2+(r[2]-ox)**2, r[1], r[2]))[:4]:
            candidate = refine(source_guide, target_patch, by, bx, oy, ox)
            if candidate is not None:
                refined.append(candidate)
                if candidate == (0, oy, ox):
                    break
        return min(refined, key=lambda r: (r[0], (r[1]-oy)**2+(r[2]-ox)**2, r[1], r[2])) if refined else None

    for y in range(0, c.shape[0] - tile + 1, tile):
        for x in range(0, c.shape[1] - tile + 1, tile):
            target = cg[y:y+tile, x:x+tile]
            candidates = []
            for dy, dx in offsets:
                sy, sx = y + dy, x + dx
                if sy < 0 or sx < 0 or sy + tile > p.shape[0] or sx + tile > p.shape[1]:
                    continue
                error = int(np.abs(target - pg[sy:sy+tile, sx:sx+tile]).sum(dtype=np.int64))
                candidates.append((error, sy*factor, sx*factor))
            if not candidates:
                continue
            fy, fx = y * factor, x * factor
            target_full = current_guide[fy:fy+full_tile, fx:fx+full_tile]
            forward = choose_refinement(previous_guide, target_full, candidates, fy, fx)
            if forward is None or forward[0] > max_error * target_full.size:
                continue
            _, fsy, fsx = forward
            # Backwards consistency rejects occluded/ambiguous guide matches.
            source_full = previous_guide[fsy:fsy+full_tile, fsx:fsx+full_tile]
            source = downsample(source_full, factor)
            reverse_candidates = []
            for ry, rx in offsets:
                sy, sx = fsy + ry * factor, fsx + rx * factor
                if sy < 0 or sx < 0 or sy + full_tile > current.shape[0] or sx + full_tile > current.shape[1]:
                    continue
                candidate = downsample(current_guide[sy:sy+full_tile, sx:sx+full_tile], factor)
                error = int(np.abs(source - candidate).sum(dtype=np.int64))
                reverse_candidates.append((error, sy, sx))
            if not reverse_candidates:
                continue
            backwards = choose_refinement(current_guide, source_full, reverse_candidates, fsy, fsx)
            if backwards is None or backwards[1:] != (fy, fx):
                continue
            error = int(np.abs(current[fy:fy+full_tile, fx:fx+full_tile].astype(np.int64)
                               - previous[fsy:fsy+full_tile, fsx:fsx+full_tile]).sum(dtype=np.int64))
            total_error += error
            accepted += full_tile * full_tile
            worst = max(worst, ceiling_ppm(error, 255 * full_tile * full_tile))
            key = str(fsy-fy) + "," + str(fsx-fx)
            offsets_used[key] = offsets_used.get(key, 0) + 1
    return {"motion_residual_ppm": None if not accepted else ceiling_ppm(total_error, 255 * accepted),
            "motion_worst_tile_ppm": None if not accepted else worst,
            "motion_coverage_ppm": ratio(accepted, current.size), "motion_pixels": accepted,
            "motion_total_pixels": current.size, "motion_offsets": offsets_used}


def gate(*, noise, reference_residual, detail_metrics, motion, policy):
    """Any observed FAIL overrides missing coverage; no evidence never passes."""
    failures, unavailable = [], []
    rgb_error = None if detail_metrics is None else detail_metrics.get("rgb_mse_ppm")
    if rgb_error is None:
        unavailable.append("rgb_reference_unavailable")
    elif type(rgb_error) is not int or not 0 <= rgb_error <= PPM:
        raise Refusal(2, "rgb_reference_invalid")
    elif rgb_error > policy["rgb_mse_max_ppm"]:
        failures.append("rgb_reference_error_above_ceiling")
    if noise is None:
        unavailable.append("noise_unavailable")
    elif noise > policy["noise_max_ppm"]:
        failures.append("noise_above_ceiling")
    if reference_residual is not None:
        if reference_residual > policy["flicker_max_ppm"]:
            failures.append("reference_flicker_above_ceiling")
    elif motion is None or motion["motion_residual_ppm"] is None or motion["motion_coverage_ppm"] < policy["motion_min_coverage_ppm"]:
        unavailable.append("temporal_coverage_unavailable")
    # A qualified reference delta explains physical lighting/motion directly.
    # A guide-only proxy must not overrule that independent stronger witness.
    if reference_residual is None and motion and motion["motion_residual_ppm"] is not None:
        if max(motion["motion_residual_ppm"], motion["motion_worst_tile_ppm"]) > policy["flicker_max_ppm"]:
            failures.append("motion_flicker_above_ceiling")
    if detail_metrics is None or detail_metrics["detail_ratio_ppm"] is None or detail_metrics["detail_tile_min_ppm"] is None:
        unavailable.append("detail_reference_unavailable")
    else:
        if detail_metrics["detail_ratio_ppm"] < policy["detail_min_ppm"]:
            failures.append("detail_below_floor")
        if detail_metrics["detail_tile_min_ppm"] < policy["detail_tile_min_ppm"]:
            failures.append("local_detail_below_floor")
    rc = 5 if failures else 3 if unavailable else 0
    return {"rc": rc, "verdict": {0: "PASS", 3: "CANNOT_SEE", 5: "FAIL"}[rc],
            "failed": failures, "unavailable": unavailable}
