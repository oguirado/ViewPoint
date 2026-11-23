"""BiRefNet CPU candidate entry point for QUAL's isolated file protocol."""

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time

from asset_factory import core
from tools.input import matte
from tools.input.evidence import InputIssue


def frozen_input(pin):
    path = Path(pin["path"])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError("frozen_input_digest_mismatch")
    return data


def frozen_record(pin):
    raw = frozen_input(pin)
    record = json.loads(raw)
    if core.canonical_json(record).encode() != raw:
        raise ValueError("noncanonical_frozen_record")
    return record


def encode_png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def evaluate(original, master, mask, regions, support_min, boundary_min, *, submitted=None):
    """Measure with INPUT's pixel checks. A cropped still has no capture receipt."""
    from PIL import Image

    result = {"source_rc": 0, "reason_codes": [], "metrics": {}, "boundary_band": None, "generator": None}
    if original.mode != "RGB" or master.mode != "RGBA" or mask.mode != "L":
        raise ValueError("invalid_matte_modes")
    if original.size != master.size or original.size != mask.size:
        raise ValueError("matte_registration_mismatch")
    if original.tobytes() != master.convert("RGB").tobytes():
        raise ValueError("master_rgb_not_straight")

    alpha = master.getchannel("A")
    bound = matte.rational(boundary_min)
    try:
        matte._metrics(alpha, mask, support_min, regions, bound, result)
    except InputIssue as issue:
        result["source_rc"] = issue.rc
        result["reason_codes"].append(issue.code)
    # Preserve observations when INPUT's protected-region check refuses early.
    if not result["metrics"]:
        try:
            matte._metrics(alpha, mask, support_min, {}, bound, result)
        except InputIssue as issue:
            if issue.code not in result["reason_codes"]:
                result["reason_codes"].append(issue.code)

    try:
        derived, size, crop, _ = matte._derivative(master)
    except InputIssue as issue:
        if result["source_rc"] == 0:
            result["source_rc"] = issue.rc
        result["reason_codes"].append(issue.code)
        return None, result

    result["generator"] = {"resize": list(size), "crop": list(crop), "premultiplications": 1}
    if submitted is not None:
        try:
            with Image.open(io.BytesIO(submitted), formats=("PNG",)) as image:
                identical = (image.mode == "RGB" and image.size == derived.size
                             and image.tobytes() == derived.tobytes())
        except (OSError, ValueError):
            identical = False
        if not identical:
            if result["source_rc"] == 0:
                result["source_rc"] = 5
            result["reason_codes"].append("submitted_derivative_mismatch")
    return encode_png(derived), result


def run(candidate_path, output):
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "":
        raise ValueError("cuda_visibility_refused")
    from providers.birefnet import offline, worker

    candidate = json.loads(candidate_path.read_bytes())
    request = frozen_record(candidate["request"])
    freeze = frozen_record(request["freeze"])
    recipe = json.loads(frozen_input(request["recipe"]))
    offline.validate_recipe(recipe, "birefnet")
    model_root = offline.verify_local(Path(request["model_root"]), recipe)
    for package in offline.package_observation(recipe):
        if not package["available"] or package["installed_version"] != package["version"]:
            raise ValueError("birefnet_runtime_dependency_mismatch")
    sys.addaudithook(worker.deny_network)

    if freeze["schema"] != "af.birefnet-cpu-pilot-mask-freeze/v1":
        raise ValueError("invalid_frozen_mask_schema")
    cases = {}
    for case in freeze["cases"]:
        name = case["id"]
        if name not in ("ref2", "ref3") or name in cases:
            raise ValueError("invalid_reference_id")
        original_bytes = frozen_input(case["original"])
        mask_bytes = frozen_input(case["mask"])
        original = matte._png(original_bytes, "RGB", recipe["max_input_bytes"], recipe["max_pixels"])
        mask = matte._png(mask_bytes, "L", recipe["max_input_bytes"], recipe["max_pixels"])
        if list(original.size) != case["size"] or original.size != mask.size:
            raise ValueError("matte_registration_mismatch")
        if mask.getbbox() is None or any(mask.histogram()[1:255]):
            raise ValueError("independent_binary_mask_missing")
        cases[name] = (case, original_bytes, original, mask)
    name = request["reference"]
    case, original_bytes, original, mask = cases[name]
    start = time.monotonic_ns()
    master = worker.infer("birefnet", original, model_root, recipe)
    inference_ns = time.monotonic_ns() - start

    start = time.monotonic_ns()
    policy = freeze["policy"]
    derived, assessment = evaluate(original, master, mask, case["regions"], policy["support_min"],
                                   policy["boundary_iou_min"])
    evaluation_ns = time.monotonic_ns() - start
    artifacts = {"original.png": original_bytes, "straight.png": encode_png(master)}
    if derived is not None:
        artifacts["diagnostic-rgb.png"] = derived
    for filename, data in artifacts.items():
        with (output / filename).open("xb") as stream:
            stream.write(data)
    result = {
        "schema": "af.birefnet-cpu-pilot-result/v1", "scope": "diagnostic-retained-still",
        "reference": name, "freeze_sha256": request["freeze"]["sha256"],
        "inference_ns": inference_ns, "evaluation_ns": evaluation_ns,
        "assessment": assessment, "derivative_available": derived is not None,
        "digests": {filename: hashlib.sha256(data).hexdigest() for filename, data in artifacts.items()},
    }
    with (output / "result.json").open("x", encoding="utf-8") as stream:
        stream.write(core.canonical_json(result))


if __name__ == "__main__":
    run(Path(sys.argv[1]), Path(sys.argv[2]))
