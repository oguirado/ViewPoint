"""Consume a read-only QUAL handoff and profile the pinned geometry workload."""

from contextlib import contextmanager
from importlib import import_module
import hashlib
import io
import os
from pathlib import Path
import resource
import sys
import tempfile
import time

from PIL import Image

from asset_factory import core
from providers.trellis2 import admission, runtime
from tools.qualification.gpu import AUTHORITY_FIELDS, AUTHORITY_SCHEMA
from tools.qualification.manifest import JSON_LIMIT, Refusal, SHA256, fields
from tools.qualification.sandbox import GPU_AUTHORITY_PATH


def trusted_handoff(manifest, request, input_sha256):
    try:
        if (GPU_AUTHORITY_PATH.is_symlink() or not
                os.statvfs(GPU_AUTHORITY_PATH).f_flag & os.ST_RDONLY):
            raise Refusal(5, "gpu_authority_mount_writable")
        handoff = core.load_json(GPU_AUTHORITY_PATH, max_bytes=JSON_LIMIT)
    except (OSError, core.ContractError) as exc:
        raise Refusal(3, "gpu_authority_unavailable") from exc
    fields(handoff, AUTHORITY_FIELDS, "gpu_authority_invalid")
    if (handoff["schema"] != AUTHORITY_SCHEMA or
            handoff["candidate_sha256"] != core.sha256_json(manifest) or
            handoff["input_sha256"] != input_sha256 or type(handoff["permit_sha256"]) is not str or
            SHA256.fullmatch(handoff["permit_sha256"]) is None or
            type(handoff["seed"]) is not int or handoff["seed"] != request["seed"] or
            request["pipeline_type"] != "512"):
        raise Refusal(4, "gpu_authority_binding_changed")
    try:
        admission._fresh(handoff)
    except admission.Refusal as exc:
        raise Refusal(exc.rc, exc.code) from exc
    return handoff


@contextmanager
def stage(timings, key, sync=None):
    start = time.monotonic_ns()
    try:
        yield
        if sync is not None:
            sync()
    finally:
        timings[key] += time.monotonic_ns() - start


def instrument_pipeline(pipeline, timings, synchronize):
    def measured(method, name):
        def run(*args, **kwargs):
            with stage(timings, name, synchronize):
                return method(*args, **kwargs)
        return run
    for method, key in (("get_cond", "conditioning"), ("sample_sparse_structure", "sampling"),
                        ("sample_shape_slat", "sampling"), ("decode_shape_slat", "decode")):
        setattr(pipeline, method, measured(getattr(pipeline, method), key))


def execute(request, manifest, source, *, input_sha256):
    """Keep CUDA and model initialization after the scope and authority gates."""
    handoff = trusted_handoff(manifest, request, input_sha256)
    image_data = Path(request["image"]).read_bytes()
    if hashlib.sha256(image_data).hexdigest() != handoff["input_sha256"]:
        raise Refusal(4, "runtime_input_stale")
    with Image.open(io.BytesIO(image_data), formats=("PNG",)) as decoded:
        image = decoded.copy()

    started = time.monotonic_ns()
    timings = dict.fromkeys(("import", "weight_load", "conditioning", "sampling", "decode", "write"), 0)
    with stage(timings, "import"):
        import numpy as np
        import torch
        for module in ("trellis2.models", "trellis2.pipelines.samplers",
                       "trellis2.modules.image_feature_extractor",
                       "trellis2.pipelines.trellis2_image_to_3d"):
            import_module(module)
    runtime._need(torch.cuda.is_available(), 3, "gpu_unavailable")
    torch.cuda.reset_peak_memory_stats()
    with stage(timings, "weight_load", torch.cuda.synchronize):
        pipeline = runtime.load_pipeline(Path(request["model_root"]), Path(request["dino_root"]),
                                         Path(request["decoder_root"]), "512")
        pipeline.to("cuda")
    instrument_pipeline(pipeline, timings, torch.cuda.synchronize)
    meshes = pipeline.run(image, seed=handoff["seed"], pipeline_type="512", preprocess_image=False)
    torch.cuda.synchronize()
    runtime._need(len(meshes) == 1, 2, "geometry_count_invalid")
    with stage(timings, "decode"):
        vertices = meshes[0].vertices.detach().cpu().tolist()
        faces = meshes[0].faces.detach().cpu().tolist()
    for name in ("nvdiffrast", "nvdiffrec"):
        runtime._need(not any(key == name or key.startswith(name + ".") for key in sys.modules),
                      5, "restricted_import")

    with tempfile.TemporaryDirectory(prefix=".qt-native-", dir=source.parent) as temporary:
        prepared = Path(temporary) / "published"
        prepared.mkdir()
        with stage(timings, "write"):
            raw = runtime.encode_glb(vertices, faces)
            array_path = prepared / "model-native.npz"
            np.savez(array_path, vertices=np.asarray(vertices, dtype=np.float32),
                     faces=np.asarray(faces, dtype=np.int32))
            with (prepared / "raw.glb").open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            array_sha256 = core.sha256_file(array_path)
            raw_sha256 = hashlib.sha256(raw).hexdigest()
        result = {
            "rc": 0, "status": "OK", "seed": handoff["seed"], "pipeline_type": "512",
            "preprocess_image": False, "geometry_only": True, "raw_preserved": True,
            "input_sha256": handoff["input_sha256"], "output_sha256": raw_sha256,
            "model_native": {"path": array_path.name, "sha256": array_sha256},
            "native_permit_sha256": handoff["permit_sha256"],
            "cuda_max_memory_allocated_bytes": torch.cuda.max_memory_allocated(),
            "cuda_max_memory_reserved_bytes": torch.cuda.max_memory_reserved(),
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "wall_ms": (time.monotonic_ns() - started) // 1_000_000,
            "stage_wall_ns": timings,
            "vertices": len(vertices), "triangles": len(faces),
            "coordinate_convention": "native xyz -> glTF (x,z,-y); no scale normalization",
            "appearance_verdict": "CANNOT_SEE", "input_gate_verdict": "CANNOT_SEE",
            **runtime.runtime_versions(torch),
        }
        core.atomic_write_json(prepared / "runtime-measurement.json", result)
        runtime._commit_generation(prepared, source)
    return handoff["permit_sha256"]
