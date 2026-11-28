# SPDX-License-Identifier: AGPL-3.0-only
"""Measure registered silhouette overlap and rendered sRGB CIE76 color deltas."""
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


VIEWS = ("front", "flank", "back", "three-quarter")
LIGHTS = ("neutral", "grazing", "environment")
IOU_MIN = 0.97
FEATURE_MIN = 0.84
ANTENNA_MIN = 0.95
STREAK_SOURCE_MULTIPLIER = 2.0
STREAK_NOISE_ALLOWANCE = 0.25
TORSO_WINDOWS = {"front": (129, 255, 199, 310), "flank": (132, 225, 188, 304),
                 "back": (128, 243, 201, 310)}
EMBLEM_WINDOW = (198, 221, 235, 255)
COLOR_MEAN_MAX = 17.0
COLOR_P95_MAX = 43.0
REGIONS = {
    "gem_ring": ("front", (181, 207, 247, 273), ("neutral", "grazing")),
    "lower_arm": ("flank", (197, 265, 239, 279), LIGHTS),
    "dome_back": ("back", (203, 115, 235, 130), ("grazing",)),
}
# Limits are in CIE76 for color and sRGB levels for band-pass RMS.
REGION_LIMITS = {
    "gem_ring": {"neutral": (42, 19), "grazing": (34, 15)},
    "lower_arm": {"neutral": (35, 19), "grazing": (24, 12.5), "environment": (13.5, 7.25)},
    "dome_back": {"grazing": (12, 3)},
}


def rgba(root, subject, projection, view, lighting):
    path = root / f"{subject}_{projection}_{view}_{lighting}.png"
    with Image.open(path) as source:
        return np.asarray(source.convert("RGBA"))


def lab(srgb):
    c = srgb.astype(np.float64) / 255.0
    linear = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = linear @ np.array([[0.4124564, 0.2126729, 0.0193339],
                             [0.3575761, 0.7151522, 0.1191920],
                             [0.1804375, 0.0721750, 0.9503041]])
    xyz /= np.array([0.95047, 1.00000, 1.08883])
    t = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz * (29 / 6) ** 2 / 3 + 4 / 29)
    return np.column_stack((116 * t[:, 1] - 16, 500 * (t[:, 0] - t[:, 1]), 200 * (t[:, 1] - t[:, 2])))


def overlap(a, b):
    union = int((a | b).sum())
    if union == 0:
        raise RuntimeError("empty silhouette union")
    shared = int((a & b).sum())
    return shared / union


def compare(a, b):
    if a.shape != b.shape:
        raise RuntimeError("render dimensions differ")
    a_mask, b_mask = a[:, :, 3] >= 128, b[:, :, 3] >= 128
    solid = (a[:, :, 3] >= 230) & (b[:, :, 3] >= 230)
    if not solid.any():
        raise RuntimeError("empty opaque intersection for color measurement")
    delta = np.linalg.norm(lab(a[solid, :3]) - lab(b[solid, :3]), axis=1)
    return {"iou": overlap(a_mask, b_mask), "color_pixels": len(delta),
            "delta_e76_mean": float(delta.mean()), "delta_e76_p95": float(np.percentile(delta, 95))}, delta


def features(a, b):
    reference, candidate = a[:, :, 3] >= 128, b[:, :, 3] >= 128
    ys, xs = np.where(reference)
    x, y = int(xs.min()), int(ys.min())
    w, h = int(xs.max()) + 1 - x, int(ys.max()) + 1 - y
    # Fixed windows relative to the original orthographic front silhouette bounds.
    boxes = {"image_left_hand": (0, 0.365, 0.16, 0.75),
             "image_right_hand": (0.84, 0.365, 1, 0.75),
             "image_left_foot": (0.21, 0.795, 0.50, 1),
             "image_right_foot": (0.50, 0.795, 0.79, 1),
             "antenna_tips": (0.20, 0, 0.80, 0.12)}
    result = {}
    for name, (x1, y1, x2, y2) in boxes.items():
        bounds = [round(x + w * x1), round(y + h * y1), round(x + w * x2), round(y + h * y2)]
        l, t, r, bottom = bounds
        result[name] = {"bbox_px_ltrb": bounds, "iou": overlap(reference[t:bottom, l:r], candidate[t:bottom, l:r])}
    return result


def grazing_streaks(root, subject, subject_root=None):
    """Measure dark, vertically coherent fine detail in the registered cyan panels."""
    result = {}
    for view, window in TORSO_WINDOWS.items():
        l, t, r, b = window
        neutral = rgba(root, "original", "ortho", view, "neutral").astype(float) / 255
        if neutral.shape != (432, 432, 4):
            raise RuntimeError("grazing masks require the registered orthographic image dimensions")
        red, green, blue, alpha = neutral.transpose(2, 0, 1)
        cyan = (alpha > 0.95) & (red < 0.48) & (green > 0.32) & (blue > 0.42) & (blue > red + 0.18)
        mask = cv2.erode(cyan.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)[t:b, l:r]
        if mask.sum() < 500:
            raise RuntimeError(f"{view}: insufficient reference cyan-panel pixels")

        def dark_streak_rms(folder, name):
            image = rgba(folder, name, "ortho", view, "grazing")
            if image.shape != neutral.shape:
                raise RuntimeError(f"{view}: grazing registration differs")
            luminance = image[:, :, :3] @ np.array([0.2126, 0.7152, 0.0722])
            columns = cv2.blur(luminance, (1, 13))
            dark = cv2.blur(columns, (11, 1)) - cv2.blur(columns, (3, 1))
            return float(np.sqrt(np.mean(np.maximum(dark[t:b, l:r][mask], 0) ** 2)))

        original = dark_streak_rms(root, "original")
        candidate = dark_streak_rms(subject_root or root, subject)
        limit = STREAK_SOURCE_MULTIPLIER * original + STREAK_NOISE_ALLOWANCE
        result[view] = {"bbox_px_ltrb": window, "mask_pixels": int(mask.sum()),
                        "original_dark_streak_rms_srgb_levels": original,
                        "candidate_dark_streak_rms_srgb_levels": candidate, "limit": limit,
                        "passes": candidate <= limit}
    return result


def quality_failures(landmarks, streaks):
    failures = []
    if landmarks["antenna_tips"]["iou"] < ANTENNA_MIN:
        failures.append("antenna_tip_iou")
    if not all(view["passes"] for view in streaks.values()):
        failures.append("grazing_torso_streaks")
    return failures


def emblem_colors(root):
    l, t, r, b = EMBLEM_WINDOW
    result = {"bbox_px_ltrb": list(EMBLEM_WINDOW)}
    for lighting in LIGHTS:
        a, c = (rgba(root, subject, "ortho", "front", lighting)[t:b, l:r] for subject in ("original", "studio"))
        result[lighting], _ = compare(a, c)
    return result


def local_quality(root, subject, subject_root=None):
    """Compare independently fixed reference regions under the reported lighting."""
    result = {}
    for name, (view, bounds, lights) in REGIONS.items():
        l, t, r, b = bounds
        crop = np.s_[t:b, l:r]
        reference = rgba(root, "original", "ortho", view, "neutral")
        if reference.shape != (432, 432, 4):
            raise RuntimeError("local masks require the registered orthographic image size")
        mask = reference[crop][:, :, 3] >= 245
        if name == "gem_ring":
            y, x = np.ogrid[t:b, l:r]
            radius = np.hypot(x - 214, y - 239)
            mask &= (radius >= 15) & (radius <= 29) & (reference[crop][:, :, :3].max(axis=2) < 115)
        if mask.sum() < 100:
            raise RuntimeError(f"{name}: reference mask is too small")

        def signal(image):
            rgb = image[:, :, :3].astype(float)
            if name == "gem_ring":
                # Include the green excess that creates high-frequency flecks on the dark rim.
                levels = (rgb @ [0.2126, 0.7152, 0.0722] + rgb[:, :, 1] - rgb[:, :, 0]) * 0.5
                return levels - cv2.blur(levels, (5, 5))
            luminance = rgb @ [0.2126, 0.7152, 0.0722]
            horizontal = cv2.blur(luminance, (9, 1))
            return horizontal - cv2.blur(horizontal, (1, 7))

        result[name] = {"view": view, "bbox_px_ltrb": bounds, "reference_pixels": int(mask.sum()),
                        "lights": {}}
        for light in lights:
            raw = rgba(root, "original", "ortho", view, light)
            asset = rgba(subject_root or root, subject, "ortho", view, light)
            if raw.shape != reference.shape or asset.shape != reference.shape:
                raise RuntimeError(f"{name}: local render registration differs")
            shared = mask & (raw[crop][:, :, 3] >= 245) & (asset[crop][:, :, 3] >= 245)
            if not shared.any():
                raise RuntimeError(f"{name}: candidate has no matched opaque pixels")
            delta = np.linalg.norm(lab(raw[crop][shared, :3]) - lab(asset[crop][shared, :3]), axis=1)
            source_band, asset_band = signal(raw)[crop][shared], signal(asset)[crop][shared]
            mean, p95 = float(delta.mean()), float(np.percentile(delta, 95))
            source_rms = float(np.sqrt(np.mean(source_band ** 2)))
            studio_rms = float(np.sqrt(np.mean(asset_band ** 2)))
            residual = float(np.sqrt(np.mean((asset_band - source_band) ** 2)))
            coverage = float(shared.sum() / mask.sum())
            max_p95, max_rms = REGION_LIMITS[name][light]
            result[name]["lights"][light] = {
                "matched_coverage": coverage, "delta_e76_mean": mean, "delta_e76_p95": p95,
                "original_band_rms": source_rms, "candidate_band_rms": studio_rms, "residual_band_rms": residual,
                "max_delta_e76_p95": max_p95, "max_residual_band_rms": max_rms,
                "passes": coverage >= 0.98 and p95 <= max_p95 and residual <= max_rms,
            }
    return result


def local_failures(report):
    return [name for name, region in report.items() if not all(light["passes"] for light in region["lights"].values())]


def collect(root, subject, panel_actor=False):
    comparisons = {}
    deltas = []
    for projection in ("ortho", "shot"):
        for view in VIEWS:
            for lighting in LIGHTS:
                if (projection != "ortho" or view == "three-quarter") and lighting != "neutral":
                    continue
                a = rgba(root, "original", projection, view, lighting)
                b = rgba(root, subject, projection, view, lighting)
                comparison, delta = compare(a, b)
                comparisons[f"{projection}_{view}_{lighting}"] = comparison
                deltas.append(delta)
    front = [rgba(root, name, "ortho", "front", "neutral") for name in ("original", subject)]
    total = np.concatenate(deltas)
    return {"views": comparisons, "front_landmark_regions": features(*front) if panel_actor else {},
            "delta_e76_aggregate": {"pixels": len(total), "mean": float(total.mean()),
                                    "p95": float(np.percentile(total, 95))}}


def sheet(root, destination):
    tile = 218
    x_start, y_start, gap = 95, 68, 12
    sheet_image = Image.new("RGB", (x_start + 6 * tile + 4 * gap, y_start + 3 * tile + 4 * gap), "#121820")
    draw = ImageDraw.Draw(sheet_image)
    font = ImageFont.load_default(size=17)
    small = ImageFont.load_default(size=13)
    for column, light in enumerate(LIGHTS):
        left = x_start + column * (2 * tile + gap)
        draw.text((left + tile, 12), light.capitalize(), fill="#e0e8ee", font=font, anchor="mt")
        draw.text((left + tile // 2, 40), "Original", fill="#a9b9c6", font=small, anchor="mt")
        draw.text((left + 3 * tile // 2, 40), "Studio", fill="#a9b9c6", font=small, anchor="mt")
        for row, view in enumerate(VIEWS[:3]):
            top = y_start + row * (tile + gap)
            draw.text((14, top + tile // 2), view.capitalize(), fill="#e0e8ee", font=font, anchor="lm")
            for side, subject in enumerate(("original", "studio")):
                source = Image.fromarray(rgba(root, subject, "ortho", view, light))
                source.thumbnail((tile, tile), Image.Resampling.LANCZOS)
                surface = Image.new("RGBA", (tile, tile), "#262e37")
                surface.alpha_composite(source)
                sheet_image.paste(surface.convert("RGB"), (left + side * tile, top))
    sheet_image.save(destination)
