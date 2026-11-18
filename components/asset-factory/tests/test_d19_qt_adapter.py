import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from asset_factory import core
from providers.trellis2.qualification.adapter import stage_outputs, validated_image
from tools.qualification.manifest import Refusal


class TrellisQualificationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write_image(self, mode):
        stream = io.BytesIO()
        image = Image.new(mode, (4, 4))
        if mode == "RGBA":
            image.putpixel((1, 1), (30, 20, 10, 255))
        image.save(stream, format="PNG")
        path = self.root / "submitted.png"
        path.write_bytes(stream.getvalue())
        receipt = {
            "schema": "af.input-evidence/v1", "gate_id": "input.matte",
            "source_rc": 0, "reason_codes": [],
            "subject_digest": "sha256:" + core.sha256_file(path),
            "generator": {"expected_premultiplications": 1, "required_preprocess_image": False,
                          "submitted_rgb_digest": "sha256:" + hashlib.sha256(image.tobytes()).hexdigest()},
        }
        return path, receipt

    def test_submits_the_exact_once_prepared_rgb(self):
        path, receipt = self.write_image("RGB")
        self.assertEqual(validated_image(path, receipt), core.sha256_file(path))

    def test_refuses_rgba_that_runtime_would_convert_without_applying_alpha(self):
        path, receipt = self.write_image("RGBA")
        with self.assertRaisesRegex(Refusal, "qt_submitted_rgb_required"):
            validated_image(path, receipt)

    def test_refuses_changed_input_bytes(self):
        path, receipt = self.write_image("RGB")
        path.write_bytes(path.read_bytes() + b"drift")
        with self.assertRaisesRegex(Refusal, "qt_submitted_image_stale"):
            validated_image(path, receipt)

    def test_refuses_changed_pixel_digest(self):
        path, receipt = self.write_image("RGB")
        receipt["generator"]["submitted_rgb_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(Refusal, "qt_submitted_pixels_stale"):
            validated_image(path, receipt)

    def test_refuses_unproven_preparation(self):
        path, receipt = self.write_image("RGB")
        receipt["generator"]["expected_premultiplications"] = 2
        with self.assertRaisesRegex(Refusal, "qt_matte_preparation_unproven"):
            validated_image(path, receipt)

    def outputs(self):
        source = self.root / "generated"
        source.mkdir()
        (source / "raw.glb").write_bytes(b"fixture GLB")
        (source / "model-native.npz").write_bytes(b"fixture NPZ")
        result = {
            "rc": 0, "seed": 42, "pipeline_type": "512", "geometry_only": True,
            "preprocess_image": False, "raw_preserved": True, "input_sha256": "a" * 64,
            "native_permit_sha256": "b" * 64,
            "output_sha256": core.sha256_file(source / "raw.glb"),
            "model_native": {"path": "model-native.npz", "sha256": core.sha256_file(source / "model-native.npz")},
            "cuda_max_memory_allocated_bytes": 100, "cuda_max_memory_reserved_bytes": 120,
        }
        core.atomic_write_json(source / "runtime-measurement.json", result)
        return source

    def test_preserves_outputs_and_passes_cuda_peaks_to_qual(self):
        source = self.outputs()
        output = self.root / "output"
        output.mkdir()
        stage_outputs(source, output, seed=42, input_sha256="a" * 64, permit_sha256="b" * 64)
        self.assertEqual({path.name for path in output.iterdir()}, {
            "raw.glb", "model-native.npz", "result.json", "cuda-peaks.json",
        })
        self.assertEqual(core.load_json(output / "cuda-peaks.json", max_bytes=1024),
                         {"allocated_bytes": 100, "reserved_bytes": 120})

    def test_refuses_missing_permit_before_staging(self):
        source = self.outputs()
        record = source / "runtime-measurement.json"
        result = core.load_json(record, max_bytes=4096)
        del result["native_permit_sha256"]
        core.atomic_write_json(record, result)
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(Refusal, "qt_native_permit_unproven"):
            stage_outputs(source, output, seed=42, input_sha256="a" * 64, permit_sha256="b" * 64)
        self.assertEqual(list(output.iterdir()), [])

    def test_refuses_tampered_npz_before_staging(self):
        source = self.outputs()
        (source / "model-native.npz").write_bytes(b"changed NPZ")
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(Refusal, "qt_native_output_stale"):
            stage_outputs(source, output, seed=42, input_sha256="a" * 64, permit_sha256="b" * 64)
        self.assertEqual(list(output.iterdir()), [])

    def test_refuses_missing_cuda_peaks_before_staging(self):
        source = self.outputs()
        record = source / "runtime-measurement.json"
        result = core.load_json(record, max_bytes=4096)
        del result["cuda_max_memory_reserved_bytes"]
        core.atomic_write_json(record, result)
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(Refusal, "qt_cuda_peaks_unproven"):
            stage_outputs(source, output, seed=42, input_sha256="a" * 64, permit_sha256="b" * 64)
        self.assertEqual(list(output.iterdir()), [])
