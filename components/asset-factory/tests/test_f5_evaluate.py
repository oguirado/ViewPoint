from pathlib import Path
import tempfile
import unittest

from asset_factory import core
from tools.render import evaluate, metrics, identity
from tools.render.pipeline import HERE, Refusal, descriptor


def fixture(root):
    source = root / "scene.blend"
    source.write_bytes(b"synthetic scene")
    inputs = [descriptor(source, "blend_scene")]
    for role in sorted(identity.ROLES - {"blend_scene"}):
        path = root / role
        path.write_bytes(("synthetic " + role).encode())
        inputs.append(descriptor(path, role))
    params = {"start": 1, "end": 2, "samples": 16, "reference_samples": 1024, "references": [],
              "seed": 0, "device": "CPU", "resolution_percentage": 50}
    request_hash = identity.request_sha256(params, inputs)
    job = {"mode": "render", "device": "CPU", "seed": 0, "resolution_percentage": 50,
           "request_sha256": request_hash,
           "tasks": [{"frame": f, "samples": 16, "dir": "low/" + str(f)} for f in (1, 2)]}
    core.atomic_write_json(root / "render_job.json", job)
    job_hash = core.sha256_file(root / "render_job.json")
    rows = []
    for frame in (1, 2):
        directory = root / "low" / str(frame)
        directory.mkdir(parents=True)
        outputs = []
        for name in ("raw.png", "denoised.png", "colour.pfm", "albedo.pfm", "normal.pfm", "denoised.pfm"):
            p = directory / name
            p.write_bytes(b"synthetic bytes")
            outputs.append(descriptor(p, name, root))
        record = {"schema": "af.render-frame/v1", "rc": 0, "status": "OK", "frame": frame,
                  "samples": 16, "resolution_percentage": 50, "width": 32, "height": 32, "device": "CPU",
                  "seed": 0, "request_sha256": request_hash, "job_sha256": job_hash,
                  "denoiser": {"name": "OIDN", "version": "2.5.1", "device": "CPU"},
                  "view": {"transform": "AgX", "look": "None", "exposure_milli": 0, "gamma_milli": 1000, "display": "sRGB"},
                  "inputs": inputs, "outputs": outputs,
                  "timings_ms": {"render": 1, "oidn": 1, "tone_map": 1}, "absence_reasons": {}}
        core.atomic_write_json(directory / "receipt.json", record)
        rows.append(descriptor(directory / "receipt.json", "render_frame", root))
    run = {"schema": "af.render-run/v1", "rc": 0, "status": "OK", "frames": rows,
           "parameters": params, "toolchain": inputs, "request_sha256": request_hash,
           "job": descriptor(root / "render_job.json", "render_job", root)}
    core.atomic_write_json(root / "receipt.json", run)
    return run


class EvidenceTests(unittest.TestCase):
    def test_missing_manifest_cannot_see(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Refusal) as caught:
                evaluate.read_run(Path(d))
            self.assertEqual(caught.exception.rc, 3)

    def test_malformed_run_and_parameters_never_escape_as_rc_one(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            for payload in ([], {**run, "parameters": []}):
                core.atomic_write_json(root / "receipt.json", payload)
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.main(["--run", str(root), "--vqa-root", str(root)])
                self.assertEqual(rc, 2)

    def test_passing_receipts_then_tampered_frame_goes_red(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            self.assertEqual(len(evaluate.read_run(root)[1]), 2)
            (root / "low" / "1" / "denoised.png").write_bytes(b"tampered pixel")
            with self.assertRaises(Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 4)

    def test_source_tamper_goes_red(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            (root / "scene.blend").write_bytes(b"changed scene")
            with self.assertRaises(Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 4)

    def test_oversized_run_range_refused_before_allocation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            run["parameters"]["end"] = 10**30
            core.atomic_write_json(root / "receipt.json", run)
            with self.assertRaises(Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 2)

    def test_swapped_frames_and_missing_coverage_go_red(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            run["frames"].reverse()
            core.atomic_write_json(root / "receipt.json", run)
            with self.assertRaises(Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 4)
            run["frames"].pop()
            core.atomic_write_json(root / "receipt.json", run)
            with self.assertRaises(Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 3)

    def test_escaping_symlink_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "escape").symlink_to(root.parent, target_is_directory=True)
            with self.assertRaises(Refusal) as caught:
                evaluate.local(root, "escape/outside")
            self.assertEqual(caught.exception.rc, 2)

    def test_sibling_noise_passes_flat_flicker_but_temporal_gate_fails(self):
        np, _ = metrics.modules()
        sibling = HERE.parents[2] / "blender-visual-qa"
        noise = evaluate.NoiseMetric(sibling)
        a = np.full((32, 32, 3), 80, dtype=np.int32)
        b = np.full((32, 32, 3), 160, dtype=np.int32)
        self.assertEqual((noise.measure(a), noise.measure(b)), (0, 0))
        residual = metrics.reference_delta(metrics.luma(a), metrics.luma(b), metrics.luma(a), metrics.luma(a))
        policy = evaluate.load_policy(HERE / "policy.json")
        verdict = metrics.gate(noise=0, reference_residual=residual, detail_metrics=None, motion=None, policy=policy)
        self.assertEqual(verdict["rc"], 5)
        self.assertIn("reference_flicker_above_ceiling", verdict["failed"])

    def test_nonfinite_guide_refused(self):
        import struct
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "albedo.pfm").write_bytes(b"PF\n1 1\n-1.0\n" + struct.pack("<3f", float("nan"), 0, 0))
            with self.assertRaises(Refusal) as caught:
                metrics.guide(root)
            self.assertEqual(caught.exception.rc, 3)


if __name__ == "__main__":
    unittest.main()
