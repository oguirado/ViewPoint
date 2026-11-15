"""PX oracles on decoded frames and TRUTH arrays (numpy allowed here only, g_numpy_scope). Integers out."""
import numpy as np

from .harness import CANNOT_SEE, FAIL, PASS

# Pinned pink criteria of the v1.0 magenta detector.
R_HI, G_LO, B_HI = 215, 45, 215
R_HI2, G_LO2, B_MID = 240, 30, 140
MAGENTA_RGB = (255, 0, 255)
STANDARD_VT = {"view_transform": "Standard", "look": "None", "exposure": 0, "gamma": 1}
PPM = 1000000


def _res(verdict, reason, metrics):
    return verdict, reason, {k: int(v) for k, v in metrics.items()}


def view_transform_ok(settings):
    """Magenta checks are only valid under the identity colour pipeline (T7)."""
    return isinstance(settings, dict) and all(k in settings and settings[k] == v for k, v in STANDARD_VT.items())


def _check_rgb(rgb):
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("rgb must be uint8 HxWx3")


def _pink_mask(rgb):
    r, g, b = (rgb[..., i].astype(np.int32) for i in range(3))
    return ((r > R_HI) & (g < G_LO) & (b > B_HI)) | ((r > R_HI2) & (g < G_LO2) & (b > B_MID))


# @anchor:bv.oracles.pixel.frame_magenta
def frame_magenta(rgb, num, den, settings):
    """FAIL iff pink*den > total*num (equality PASS)."""
    _check_rgb(rgb)
    if not view_transform_ok(settings):
        return _res(CANNOT_SEE, "view_transform", {"pink": 0, "total": 0})
    total = rgb.shape[0] * rgb.shape[1]
    if total == 0:
        return _res(CANNOT_SEE, "eligible_pixel_count_zero", {"pink": 0, "total": 0})
    pink = int(_pink_mask(rgb).sum())
    bad = pink * den > total * num
    return _res(FAIL if bad else PASS, "fallback_magenta_shader_detected" if bad else "no_magenta_fallback",
                {"pink": pink, "total": total, "num": num, "den": den})


def frame_black(rgb, dark_max, num, den):
    """FAIL iff dark*den >= total*num."""
    _check_rgb(rgb)
    total = rgb.shape[0] * rgb.shape[1]
    if total == 0:
        return _res(CANNOT_SEE, "eligible_pixel_count_zero", {"dark": 0, "total": 0})
    dark = int((rgb.max(axis=2) <= dark_max).sum())
    bad = dark * den >= total * num
    return _res(FAIL if bad else PASS, "black_frame_threshold_exceeded" if bad else "frame_not_black",
                {"dark": dark, "total": total})


def frame_uniform(rgb, spread_max):
    """FAIL iff every channel spans <= spread_max over the frame (a flat colour)."""
    _check_rgb(rgb)
    if rgb.size == 0:
        return _res(CANNOT_SEE, "eligible_pixel_count_zero", {"spread": 0})
    spread = int((rgb.reshape(-1, 3).max(axis=0).astype(np.int32) - rgb.reshape(-1, 3).min(axis=0)).max())
    bad = spread <= spread_max
    return _res(FAIL if bad else PASS, "frame_uniform" if bad else "frame_not_uniform", {"spread": spread})


def frame_frozen(frames):
    """FAIL iff >= 2 frames and all are byte-identical."""
    if len(frames) < 2:
        return _res(CANNOT_SEE, "need_two_frames", {"frames": len(frames)})
    for f in frames:
        _check_rgb(f)
    same = all(np.array_equal(frames[0], f) for f in frames[1:])
    return _res(FAIL if same else PASS, "frames_identical" if same else "frames_differ", {"frames": len(frames)})


# @anchor:bv.oracles.pixel.magenta_attrib
def magenta_attrib(rgb, ma, materials, settings):
    """Attribute exact (255,0,255) pixels to material index k -> materials[k-1]. Returns (verdict, reason, metrics, names)."""
    _check_rgb(rgb)
    if not view_transform_ok(settings):
        return CANNOT_SEE, "view_transform", {}, []
    if ma.shape != rgb.shape[:2]:
        raise ValueError("ma shape mismatch")
    mag = (rgb[..., 0] == MAGENTA_RGB[0]) & (rgb[..., 1] == MAGENTA_RGB[1]) & (rgb[..., 2] == MAGENTA_RGB[2])
    counts = {}
    for k in np.unique(ma[mag]).tolist():
        if k >= 1:
            if k > len(materials):
                return CANNOT_SEE, "material_index_out_of_range", {"index": k}, []
            counts[materials[k - 1]] = int((mag & (ma == k)).sum())
    names = sorted(n for n, c in counts.items() if c > 0)
    metrics = {"magenta_total": int(mag.sum()), "attributed": sum(counts.values())}
    return (FAIL if names else PASS), ("magenta_attributed" if names else "no_magenta"), metrics, names


# @anchor:bv.oracles.pixel.subject_visible
def subject_visible(ob_frames, pass_index, min_ppm):
    """FAIL iff in some frame count(ob==pass_index)*1e6 // (W*H) < min_ppm."""
    if not ob_frames:
        return _res(CANNOT_SEE, "no_frames", {"frames": 0})
    worst = None
    for ob in ob_frames:
        total = ob.shape[0] * ob.shape[1]
        ppm = int((ob == pass_index).sum()) * PPM // total
        worst = ppm if worst is None else min(worst, ppm)
    bad = worst < min_ppm
    return _res(FAIL if bad else PASS, "subject_not_visible" if bad else "subject_visible",
                {"min_ppm_seen": worst, "threshold_ppm": min_ppm, "frames": len(ob_frames)})


def zfight_pixel(ob_t, ob_t1, bboxes, deterministic_renderer):
    """ppm of ob_t != ob_t1 inside the pair bboxes (x0,y0,x1,y1). FAIL on workbench-det, ADVISORY otherwise."""
    if ob_t.shape != ob_t1.shape:
        raise ValueError("shape mismatch")
    area = changed = 0
    for x0, y0, x1, y1 in bboxes:
        a, b = ob_t[y0:y1, x0:x1], ob_t1[y0:y1, x0:x1]
        area += a.size
        changed += int((a != b).sum())
    if area == 0:
        return _res(CANNOT_SEE, "no_pair_bbox", {"area": 0})
    ppm = changed * PPM // area
    if ppm == 0:
        return _res(PASS, "no_pixel_zfight", {"changed_ppm": 0, "area": area})
    return _res(FAIL if deterministic_renderer else "ADVISORY", "pixel_zfight", {"changed_ppm": ppm, "area": area})
