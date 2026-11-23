"""Production publication boundary and full decoded-pixel fidelity witnesses."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.render import evaluate, identity, metrics, pipeline
from test_f5_evaluate import fixture


class ExecutionIdentityTests(unittest.TestCase):
    def fake_runner(self, *, corrupt=False):
        def run(argv, log, timeout_s, *, env):
            self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "")
            Path(log).write_text("synthetic process boundary\n")
            if "--python" not in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(b"denoised PFM")
                return 1
            job_path = Path(argv[-1])
            job = core.load_json(job_path, max_bytes=1048576)
            if job["mode"] == "render":
                for task in job["tasks"]:
                    directory = job_path.parent / task["dir"]
                    directory.mkdir(parents=True)
                    for name in ("raw.png", "colour.pfm", "albedo.pfm", "normal.pfm"):
                        (directory / name).write_bytes(b"synthetic pass")
                    settings = identity.settings(job, task["frame"], task["samples"])
                    if corrupt:
                        settings["seed"] += 1
                    core.atomic_write_json(directory / "render.json", {
                        "width": 32, "height": 32, "render_ms": 1, "dependencies": [],
                        "settings": settings, "request_sha256": job["request_sha256"],
                        "job_sha256": core.sha256_file(job_path),
                        "view": {"transform": "AgX", "look": "None", "exposure_milli": 0,
                                 "gamma_milli": 1000, "display": "sRGB"}})
            else:
                for task in job["tasks"]:
                    Path(task["output"]).write_bytes(b"tone-mapped PNG")
                    core.atomic_write_json(Path(task["meta"]), {"tone_map_ms": 1})
            return 1
        return run

    def test_r2_execution_metadata_binds_the_published_job_after_directory_move(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                for name in ("scene.blend", "blender", "oidn"):
                    (root / name).write_bytes(b"synthetic " + name.encode())
                    (root / name).chmod(0o700)
                target = root / "out"
                with patch.object(pipeline, "probe_oidn", return_value={"name": "OIDN", "version": "2.5.1", "device": "CPU"}), \
                     patch.object(pipeline, "run", side_effect=self.fake_runner(corrupt=corrupt)):
                    if corrupt:
                        with self.assertRaises(pipeline.Refusal) as caught:
                            pipeline.render(root / "scene.blend", target, oidn=root / "oidn",
                                            blender=root / "blender", start=1, end=2, seed=31)
                        self.assertEqual(caught.exception.rc, 4)
                        self.assertFalse(target.exists())
                    else:
                        record = pipeline.render(root / "scene.blend", target, oidn=root / "oidn",
                                                 blender=root / "blender", start=1, end=2, seed=31)
                        loaded, rows = evaluate.read_run(target, expected_request_sha256=record["request_sha256"])
                        self.assertEqual(loaded, record)
                        self.assertEqual(len(rows), 2)
                        self.assertTrue(all(r["seed"] == 31 for _, r in rows))

    def pixel_fixture(self, root, corruption):
        np, Image = metrics.modules()
        run = fixture(root)
        run["parameters"]["references"] = [1, 2]
        request_hash = identity.request_sha256(run["parameters"], run["toolchain"])
        job_path = root / run["job"]["path"]
        job = core.load_json(job_path, max_bytes=1048576)
        job["request_sha256"] = request_hash
        job["tasks"] += [{"frame": f, "samples": 1024, "dir": "reference/" + str(f)} for f in (1, 2)]
        core.atomic_write_json(job_path, job)
        run["job"]["sha256"] = core.sha256_file(job_path)
        run["request_sha256"] = request_hash
        base = core.load_json(root / run["frames"][0]["path"], max_bytes=1048576)
        # The actual sibling's calibrated pixel count, not a patched metric.
        height, width = 960, 540
        x = np.arange(width, dtype=np.int32)[None, :] * 255 // (width - 1)
        reference = np.repeat(np.repeat(x[:, :, None], height, axis=0), 3, axis=2)
        candidate = corruption(reference.copy())
        pfm = b"PF\n540 960\n-1.0\n" + np.zeros((height, width, 3), dtype="<f4").tobytes()
        rows = []
        for task in job["tasks"]:
            directory = root / task["dir"]
            directory.mkdir(parents=True, exist_ok=True)
            image = candidate if task["samples"] == 16 else reference
            for name in ("raw.png", "denoised.png"):
                Image.fromarray(image.astype("uint8")).save(directory / name)
            for name in ("colour.pfm", "albedo.pfm", "normal.pfm", "denoised.pfm"):
                (directory / name).write_bytes(pfm)
            receipt = copy.deepcopy(base)
            receipt.update(frame=task["frame"], samples=task["samples"], height=height, width=width,
                           request_sha256=request_hash, job_sha256=run["job"]["sha256"])
            receipt["outputs"] = [pipeline.descriptor(directory / name, name, root) for name in (
                "raw.png", "denoised.png", "colour.pfm", "albedo.pfm", "normal.pfm", "denoised.pfm")]
            core.atomic_write_json(directory / "receipt.json", receipt)
            rows.append(pipeline.descriptor(directory / "receipt.json", "render_frame", root))
        run["frames"] = rows
        core.atomic_write_json(root / "receipt.json", run)

    def test_r1_full_evaluator_rejects_inversion_chroma_and_displaced_content(self):
        np, _ = metrics.modules()
        transforms = (lambda a: a, lambda a: 255 - a,
                      lambda a: np.clip(a + np.array([50, -25, 0]), 0, 255),
                      lambda a: np.roll(a, 100, axis=1))
        for index, transform in enumerate(transforms):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.pixel_fixture(root, transform)
                result = evaluate.evaluate(root, vqa_root=pipeline.HERE.parents[2] / "blender-visual-qa")
                self.assertEqual(result["rc"], 0 if index == 0 else 5)
                if index:
                    self.assertIn("rgb_reference_error_above_ceiling", [r["reason"] for r in result["failures"]])


if __name__ == "__main__":
    unittest.main()
