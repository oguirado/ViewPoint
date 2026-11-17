"""Verify the exact TRELLIS.2 RGB derivative against independent matte intent."""
from fractions import Fraction
import io
import math
import warnings

from asset_factory import core
from asset_factory.domains import (PPM, REQUIRED_VIEWS, compose_crop, crop_transform, integers,
                                   invert_crop, pair, rational, rectangle)
from tools.input.evidence import (InputIssue, bytes_digest, checked_record, closed, invented_details,
                                  is_digest, native_receipt, point, refuse, require)

# Pinned upstream preprocessing for non-opaque RGBA input.
RESIZE_LONG_SIDE = 1024
HARD_ALPHA_THRESHOLD = 204
_PROTECTED_FEATURES = {"extremities": "extremity", "fine_structure": "fine_structure"}


def _png(raw, mode, encoded_limit, pixel_limit):
    from PIL import Image
    require(type(raw) is bytes and len(raw) <= encoded_limit, 6, "matte_encoded_byte_budget")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=("PNG",)) as image:
                require(image.width * image.height <= pixel_limit, 6, "matte_pixel_budget")
                require(image.mode == mode and raw[24] == 8 and image.n_frames == 1
                        and "transparency" not in image.info
                        and "icc_profile" not in image.info and "exif" not in image.info,
                        2, "invalid_matte_png_mode_or_metadata")
                image.verify()
            with Image.open(io.BytesIO(raw), formats=("PNG",)) as image:
                image.load()
                return image
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InputIssue(6, "matte_pixel_budget") from exc
    except (OSError, IndexError, SyntaxError) as exc:
        raise InputIssue(2, "invalid_matte_png") from exc


def _derivative(master):
    import numpy as np
    from PIL import Image

    width, height = master.size
    scale = min(1, RESIZE_LONG_SIDE / max(width, height))
    size = (int(width * scale), int(height * scale))
    require(min(size) > 0, 3, "generator_resize_empty")
    image = master.resize(size, Image.Resampling.LANCZOS) if size != master.size else master
    hard = image.getchannel("A").point(lambda value: 255 if value > HARD_ALPHA_THRESHOLD else 0)
    hard_box = hard.getbbox()
    require(hard_box is not None, 5, "generator_hard_alpha_empty")
    left, top, right, bottom = hard_box
    x, y = (left + right - 1) / 2, (top + bottom - 1) / 2
    radius = max(right - left - 1, bottom - top - 1) // 2
    crop = tuple(round(edge) for edge in (x - radius, y - radius, x + radius, y + radius))
    require(crop[0] < crop[2] and crop[1] < crop[3], 5, "generator_crop_empty")
    array = np.asarray(image.crop(crop)).astype(np.float32) / 255
    rgb = (array[:, :, :3] * array[:, :, 3:4] * 255).astype(np.uint8)
    return Image.fromarray(rgb), size, crop, image.getchannel("A")


def _registered_original(intent, origin, read_checked):
    from PIL import Image

    selection_digest = intent["selection_digest"]
    selection = checked_record(read_checked, "input-selection", selection_digest)
    closed(selection, "af.input-frame-selection/v1", "source_digest", "stream", "raster", "plan_digest",
           "selected", "pose_digest", "side_landmark_signs", "invented_details")
    view = intent["view"]
    require(view in REQUIRED_VIEWS, 2, "invalid_matte_view")
    selected = selection["selected"]
    require(type(selected) is dict and selected.keys() == set(REQUIRED_VIEWS),
            3, "matte_selection_views_incomplete")
    row = selected[view]
    require(is_digest(selection["pose_digest"]), 2, "invalid_matte_selection_pose")
    require(row["view"] == view and row["pose_digest"] == selection["pose_digest"], 4,
            "matte_selection_view_drift")
    raster = integers(selection["raster"], 2)
    require(min(raster) > 0, 2, "invalid_matte_frame_raster")
    require(math.prod(raster) <= intent["max_pixels"], 6, "matte_frame_pixel_budget")
    crop = row["crop"]
    transform = crop_transform(crop, raster)
    require(transform[0] == transform[1], 5, "anisotropic_registered_crop")
    require(transform == row["crop_transform"] == intent["frame_to_original_transform"]
            and tuple(crop["size"]) == origin.size, 4, "matte_crop_transform_drift")
    frame_digest = row["frame_digest"]
    require(is_digest(frame_digest), 2, "invalid_matte_frame_pin")
    pixels = read_checked("selected-frame-rgb", frame_digest)
    require(pixels is not None, 3, "missing_selected_frame_rgb")
    require(type(pixels) is bytes and len(pixels) == 3 * math.prod(raster), 2, "invalid_selected_frame_rgb")
    require(bytes_digest(pixels) == frame_digest, 4, "stale_selected_frame_rgb")
    registered = Image.frombytes("RGB", tuple(raster), pixels).crop(crop["rect"])
    if registered.size != origin.size:
        registered = registered.resize(origin.size, Image.Resampling.LANCZOS)
    require(registered.tobytes() == origin.tobytes(), 4, "matte_original_pixels_drift")
    return {"selection_digest": selection_digest, "frame_digest": frame_digest, "view": view,
            "frame_to_original": transform, "frame_crop": crop, "frame_raster": raster,
            "crop_resize_filter": "Pillow.LANCZOS"}


def _metrics(alpha, expected, support_min, regions, boundary_min, receipt):
    from PIL import Image, ImageChops, ImageFilter

    selection = alpha.point(lambda value: 255 if value >= support_min else 0)
    width, height = selection.size
    bbox = selection.getbbox()
    require(bbox is not None and alpha.getextrema() != (255, 255), 5, "alpha_opaque_or_empty")
    require(expected.getbbox() is not None and not any(expected.histogram()[1:255]),
            3, "independent_binary_mask_missing")
    for name, boxes in regions.items():
        for rect in boxes:
            candidate = selection.crop(rect)
            reference = expected.crop(rect)
            if name in _PROTECTED_FEATURES:
                feature = _PROTECTED_FEATURES[name]
                require(reference.getbbox() is not None, 3, f"protected_{feature}_unobserved")
                require(ImageChops.subtract(reference, candidate).getbbox() is None, 5,
                        f"protected_{feature}_missing")
            else:
                require(reference.getbbox() is None, 3, "protected_background_not_observed")
                require(candidate.getbbox() is None, 5,
                        "protected_hole_filled" if name == "holes" else "protected_border_filled")
    perimeter = Image.new("L", selection.size, 255)
    if width > 2 and height > 2:
        perimeter.paste(0, (1, 1, width - 1, height - 1))
    border_pixels = ImageChops.multiply(selection, perimeter).histogram()[255]
    require(border_pixels == 0, 5, "protected_border_filled")
    intersect = ImageChops.darker(selection, expected).histogram()[255]
    union = ImageChops.lighter(selection, expected).histogram()[255]
    margin = min(bbox[0], bbox[1], width - bbox[2], height - bbox[3])
    pixels = width * height
    band = ImageChops.subtract(expected.filter(ImageFilter.MaxFilter(3)),
                               expected.filter(ImageFilter.MinFilter(3)))
    band_intersection = ImageChops.multiply(band, ImageChops.darker(selection, expected)).histogram()[255]
    band_union = ImageChops.multiply(band, ImageChops.lighter(selection, expected)).histogram()[255]
    require(band_union > 0, 3, "boundary_band_support_missing")
    boundary_iou = Fraction(band_intersection, band_union)
    nonzero = alpha.point(lambda value: 255 if value else 0)
    outside = ImageChops.subtract(nonzero, expected).histogram()[255]
    outside_at_threshold = ImageChops.subtract(selection, expected).histogram()[255]
    receipt["metrics"] = dict(alpha_iou=point(Fraction(intersect, union)), boundary_iou=point(boundary_iou),
                              margin=point(Fraction(margin, min(width, height))),
                              foreground=point(Fraction(selection.histogram()[255], pixels)),
                              border_pixels=point(border_pixels),
                              alpha_outside_pixels=point(outside))
    receipt["boundary_band"] = {"reference_radius_px": 1, "intersection_pixels": band_intersection,
                                "union_pixels": band_union, "intent_min": pair(boundary_min),
                                "low_alpha_outside_pixels": outside - outside_at_threshold}
    require(outside_at_threshold == 0, 5, "alpha_outside_reference_mask")
    require(outside == 0, 5, "low_alpha_outside_reference_mask")
    require(boundary_iou >= boundary_min, 5, "boundary_iou_below_intent")


def inspect_matte(original, straight_alpha, submitted, approved_mask, *, intent_digest, read_checked):
    """Recompute the matte receipt against the exact submitted RGB PNG."""
    if type(submitted) is not bytes:
        return refuse(native_receipt("input.matte", None, intent_digest),
                      InputIssue(2, "submitted_rgb_bytes_missing"))
    _, receipt = _assess(original, straight_alpha, submitted, approved_mask, intent_digest, read_checked)
    return receipt


def prepare_matte(original, straight_alpha, approved_mask, *, intent_digest, read_checked):
    """Return the once-prepared RGB PNG and its native evidence, or a typed refusal."""
    return _assess(original, straight_alpha, None, approved_mask, intent_digest, read_checked)


def _assess(original, straight_alpha, submitted, approved_mask, intent_digest, read_checked):
    receipt = native_receipt("input.matte", None, intent_digest)
    try:
        inputs = [bytes_digest(raw) for raw in (original, straight_alpha)]
        if submitted is not None:
            receipt["subject_digest"] = bytes_digest(submitted)
    except (ValueError, TypeError) as exc:
        return None, refuse(receipt, exc)
    try:
        from PIL import Image, ImageChops

        intent = checked_record(read_checked, "matte-intent", intent_digest)
        closed(intent, "af.input-matte-intent/v1", "original_digest", "mask_digest", "support_min",
               "regions", "max_encoded_bytes", "max_pixels", "invented_details",
               "selection_digest", "view", "frame_to_original_transform", "boundary_iou_min")
        mask_digest = bytes_digest(approved_mask)
        require(inputs[0] == intent["original_digest"] and mask_digest == intent["mask_digest"],
                4, "matte_reference_drift")
        invented_details(intent["invented_details"])
        encoded, pixels, threshold = (intent[key] for key in
                                      ("max_encoded_bytes", "max_pixels", "support_min"))
        require(all(type(n) is int and n > 0 for n in (encoded, pixels, threshold)) and threshold <= 255,
                2, "invalid_matte_policy")
        origin = _png(original, "RGB", encoded, pixels)
        master = _png(straight_alpha, "RGBA", encoded, pixels)
        mask = _png(approved_mask, "L", encoded, pixels)
        require(origin.size == master.size == mask.size, 4, "matte_registration_mismatch")
        registration = _registered_original(intent, origin, read_checked)
        require(origin.tobytes() == master.convert("RGB").tobytes(), 5, "master_rgb_not_straight")
        boundary_min = rational(intent["boundary_iou_min"])
        require(0 < boundary_min <= 1, 2, "invalid_boundary_iou_min")
        regions = intent["regions"]
        require(type(regions) is dict and regions.keys() == {"border", "holes", *_PROTECTED_FEATURES},
                2, "invalid_matte_regions")
        require(all(type(boxes) is list for boxes in regions.values()), 2, "invalid_matte_regions")
        require(regions["border"] and regions["extremities"], 3, "required_matte_regions_missing")
        regions = {name: [rectangle(rect, mask.size) for rect in boxes] for name, boxes in regions.items()}
        alpha = master.getchannel("A")
        receipt.update(reference_refs=[intent_digest, mask_digest, registration["selection_digest"],
                                       registration["frame_digest"]],
                       invented_details=intent["invented_details"], registration=registration,
                       original_rgb_digest=bytes_digest(origin.tobytes()))
        _metrics(alpha, mask, threshold, regions, boundary_min, receipt)

        derived, size, crop, resized_alpha = _derivative(master)
        frame_to_original = registration["frame_to_original"]
        original_to_resize = crop_transform({"rect": [0, 0, *master.size], "size": list(size)}, master.size)
        resize_to_generator = [pair(v) for v in (1, 1, -crop[0], -crop[1])]
        frame_to_generator = compose_crop(compose_crop(frame_to_original, original_to_resize),
                                          resize_to_generator)
        receipt["generator"] = {
            "resize": list(size), "crop": list(crop), "expected_premultiplications": 1,
            "required_preprocess_image": False, "submitted_rgb_digest": bytes_digest(derived.tobytes()),
            "resize_filter": "Pillow.LANCZOS",
            "crop_pads_raster": crop[0] < 0 or crop[1] < 0 or crop[2] > size[0] or crop[3] > size[1],
            "transforms": {"frame_to_original": frame_to_original, "original_to_resize": original_to_resize,
                           "resize_to_generator": resize_to_generator, "frame_to_generator": frame_to_generator,
                           "generator_to_frame": invert_crop(frame_to_generator)},
        }
        actual_support = resized_alpha.point(lambda v: 255 if v >= threshold else 0)
        for name, feature in _PROTECTED_FEATURES.items():
            protected = Image.new("L", mask.size)
            for rect in regions[name]:
                support = mask.crop(rect)
                protected.paste(support, rect)
                extent = support.getbbox()
                coords = [rect[i % 2] + edge for i, edge in enumerate(extent)]
                mapped = [Fraction(edge * size[i % 2], master.size[i % 2]) for i, edge in enumerate(coords)]
                require(crop[0] <= mapped[0] < mapped[2] <= crop[2]
                        and crop[1] <= mapped[1] < mapped[3] <= crop[3], 5,
                        f"protected_{feature}_lost_in_generator_crop")
                x0, y0, x1, y1 = mapped
                scaled = (math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1))
                require(actual_support.crop(scaled).getbbox() is not None, 5,
                        f"protected_{feature}_lost_in_generator_resize")
            if regions[name]:
                resized_support = protected.resize(size, Image.Resampling.BOX).point(lambda v: 255 if v else 0)
                require(resized_support.getbbox() is not None
                        and ImageChops.subtract(resized_support, actual_support).getbbox() is None, 5,
                        f"protected_{feature}_lost_in_generator_resize")
        if submitted is None:
            buffer = io.BytesIO()
            derived.save(buffer, format="PNG")
            submitted = buffer.getvalue()
            require(len(submitted) <= encoded, 6, "matte_encoded_byte_budget")
        else:
            candidate = _png(submitted, "RGB", encoded, pixels)
            require(candidate.size == derived.size and candidate.tobytes() == derived.tobytes(), 5,
                    "submitted_derivative_mismatch")
        digest = bytes_digest(submitted)
        receipt.update(subject_digest=digest, input_refs=[*inputs, digest])
        receipt["observation_bounds_ppm"] = {
            name: list(outward(bounds)) for name, bounds in receipt["metrics"].items()
            if name not in ("border_pixels", "alpha_outside_pixels")}
        return submitted, receipt
    except ImportError:
        return None, refuse(receipt, InputIssue(3, "matte_pixel_toolchain_unavailable"))
    except (ValueError, TypeError, KeyError, core.ContractError, MemoryError, RecursionError) as exc:
        return None, refuse(receipt, exc)


def outward(bounds):
    from asset_factory.math_acceptance import outward_bounds
    return outward_bounds(*bounds, scale=PPM)
