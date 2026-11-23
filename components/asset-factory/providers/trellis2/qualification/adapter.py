"""QUAL entry point for the permit-bound TRELLIS geometry runtime."""

import hashlib
import io
from pathlib import Path
import sys

from PIL import Image

from asset_factory import core
from providers.trellis2 import runtime
from tools.qualification.manifest import JSON_LIMIT, Refusal, SHA256
from tools.qualification.sandbox import GPU_AUTHORITY_PATH


def require(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


def validated_image(path, receipt):
    require(isinstance(receipt, dict) and receipt.get("schema") == "af.input-evidence/v1"
            and receipt.get("gate_id") == "input.matte" and type(receipt.get("source_rc")) is int
            and receipt["source_rc"] == 0 and receipt.get("reason_codes") == [],
            3, "qt_matte_evidence_unavailable")
    generator = receipt.get("generator")
    require(isinstance(generator, dict) and type(generator.get("expected_premultiplications")) is int
            and generator["expected_premultiplications"] == 1
            and generator.get("required_preprocess_image") is False,
            3, "qt_matte_preparation_unproven")
    with path.open("rb") as stream:
        data = stream.read(32 * 2**20 + 1)
    require(0 < len(data) <= 32 * 2**20, 6, "qt_image_budget_exceeded")
    digest = hashlib.sha256(data).hexdigest()
    require(receipt.get("subject_digest") == "sha256:" + digest, 4, "qt_submitted_image_stale")
    try:
        with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
            require(image.mode == "RGB" and image.n_frames == 1 and "transparency" not in image.info,
                    5, "qt_submitted_rgb_required")
            require(image.width * image.height <= 4096**2, 6, "qt_image_budget_exceeded")
            pixels_digest = hashlib.sha256(image.tobytes()).hexdigest()
    except Image.DecompressionBombError as exc:
        raise Refusal(6, "qt_image_budget_exceeded") from exc
    except OSError as exc:
        raise Refusal(2, "qt_submitted_png_invalid") from exc
    require(generator.get("submitted_rgb_digest") == "sha256:" + pixels_digest,
            4, "qt_submitted_pixels_stale")
    return digest


def stage_outputs(source, output, *, seed, input_sha256, permit_sha256):
    measurement = core.load_json(source / "runtime-measurement.json", max_bytes=JSON_LIMIT)
    require(isinstance(measurement, dict) and measurement.get("rc") == 0
            and measurement.get("pipeline_type") == "512" and measurement.get("seed") == seed
            and measurement.get("geometry_only") is True and measurement.get("raw_preserved") is True
            and measurement.get("preprocess_image") is False
            and measurement.get("input_sha256") == input_sha256, 4, "qt_native_measurement_stale")
    permit = measurement.get("native_permit_sha256")
    require(isinstance(permit, str) and SHA256.fullmatch(permit) and permit == permit_sha256,
            3, "qt_native_permit_unproven")
    allocated = measurement.get("cuda_max_memory_allocated_bytes")
    reserved = measurement.get("cuda_max_memory_reserved_bytes")
    require(type(allocated) is int and type(reserved) is int and 0 < allocated <= reserved,
            3, "qt_cuda_peaks_unproven")
    artifacts = {name: core.sha256_file(source / name) for name in ("raw.glb", "model-native.npz")}
    require(measurement.get("output_sha256") == artifacts["raw.glb"]
            and measurement.get("model_native") == {"path": "model-native.npz", "sha256": artifacts["model-native.npz"]},
            4, "qt_native_output_stale")
    require(output.is_dir() and not output.is_symlink() and not any(output.iterdir()),
            5, "qt_output_not_empty")
    for name in artifacts:
        (source / name).replace(output / name)
    (source / "runtime-measurement.json").replace(output / "result.json")
    core.atomic_write_json(output / "cuda-peaks.json", {"allocated_bytes": allocated, "reserved_bytes": reserved})


def main(manifest_path, output):
    manifest = core.load_json(manifest_path, max_bytes=JSON_LIMIT)
    request = core.load_json(Path(manifest["request"]["path"]), max_bytes=JSON_LIMIT)
    schema = core.load_json(Path(manifest["input_schema"]["path"]), max_bytes=JSON_LIMIT)
    core.validate(request, schema)
    require(manifest["provider"] == "trellis2" and manifest["runtime"]["cuda"] is True,
            2, "qt_candidate_identity_invalid")
    image = Path(request["image"])
    matte = core.load_json(Path(request["matte_receipt"]), max_bytes=JSON_LIMIT)
    input_sha256 = validated_image(image, matte)
    permit = Path(request["permit"])
    source = output.parent / "generated"
    if GPU_AUTHORITY_PATH.exists():
        from providers.trellis2.qualification import sandbox_runtime
        permit_sha256 = sandbox_runtime.execute(request, manifest, source, input_sha256=input_sha256)
    else:
        permit_sha256 = core.sha256_json(core.load_json(permit, max_bytes=JSON_LIMIT))
        runtime.execute(image_path=image, output_dir=source, model_root=Path(request["model_root"]),
                        dino_root=Path(request["dino_root"]), decoder_root=Path(request["decoder_root"]),
                        seed=request["seed"], pipeline_type=request["pipeline_type"], permit_path=permit)
    stage_outputs(source, output, seed=request["seed"], input_sha256=input_sha256, permit_sha256=permit_sha256)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
    except (Refusal, runtime.Refusal, core.ContractError, OSError) as exc:
        print(getattr(exc, "code", type(exc).__name__), file=sys.stderr)
        raise SystemExit(getattr(exc, "rc", 2 if isinstance(exc, core.ContractError) else 3)) from None
