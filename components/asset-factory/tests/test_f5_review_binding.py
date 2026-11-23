"""Red controls for the render review; no external renderer or GPU is used."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.render import evaluate, metrics, pipeline
from test_f5_evaluate import fixture
import test_f5_repair


class ReviewBindingTests(unittest.TestCase):
    def rewrite_frames(self, root, run, mutate):
        for item in run["frames"]:
            path = root / item["path"]
            record = core.load_json(path, max_bytes=1048576)
            mutate(record)
            core.atomic_write_json(path, record)
            item["sha256"] = core.sha256_file(path)
        core.atomic_write_json(root / "receipt.json", run)

    def test_r1_rgb_metric_is_required_even_when_gradient_and_temporal_match(self):
        np, _ = metrics.modules()
        y, x = np.indices((64, 64))
        image = ((x * x + y * 13) % 160 + 40).astype(np.int32)
        d = metrics.detail(image, image)
        result = metrics.gate(noise=0, reference_residual=0, detail_metrics=d,
                              motion=None, policy=evaluate.load_policy(pipeline.HERE / "policy.json"))
        self.assertEqual(result["rc"], 3)
        self.assertIn("rgb_reference_unavailable", result["unavailable"])

    def test_r2_frame_run_device_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            run["parameters"]["device"] = "CUDA"
            core.atomic_write_json(root / "receipt.json", run)
            with self.assertRaises(pipeline.Refusal):
                evaluate.read_run(root)

    def test_r2_missing_toolchain_provenance_is_cannot_see(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            run["toolchain"] = []
            core.atomic_write_json(root / "receipt.json", run)
            with self.assertRaises(pipeline.Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 3)

    def test_r2_nonpositive_dimensions_and_gamma_are_errors(self):
        for key in ("width", "height", "gamma_milli"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                run = fixture(root)
                for item in run["frames"]:
                    path = root / item["path"]
                    record = core.load_json(path, max_bytes=1048576)
                    (record["view"] if key == "gamma_milli" else record)[key] = 0
                    core.atomic_write_json(path, record)
                    item["sha256"] = core.sha256_file(path)
                core.atomic_write_json(root / "receipt.json", run)
                with self.assertRaises(pipeline.Refusal) as caught:
                    evaluate.read_run(root)
                self.assertEqual(caught.exception.rc, 2)

    def test_r4_oidn_environment_is_explicitly_cpu_only(self):
        with patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": ""}):
            self.assertEqual(pipeline.oidn_environment("/bin/true")["CUDA_VISIBLE_DEVICES"], "")

    def test_r2_seed_resolution_and_request_drift_are_stale(self):
        for key, value in (("seed", 99), ("resolution_percentage", 25),
                           ("request_sha256", "a" * 64), ("job_sha256", "b" * 64)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                run = fixture(root)
                self.rewrite_frames(root, run, lambda r: r.update({key: value}))
                with self.assertRaises(pipeline.Refusal) as caught:
                    evaluate.read_run(root)
                self.assertEqual(caught.exception.rc, 4)

    def test_r2_expected_request_must_match(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            evaluate.read_run(root, expected_request_sha256=run["request_sha256"])
            with self.assertRaises(pipeline.Refusal) as caught:
                evaluate.read_run(root, expected_request_sha256="f" * 64)
            self.assertEqual(caught.exception.rc, 4)

    def test_r2_job_settings_tamper_is_stale_even_after_rehash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            job_path = root / run["job"]["path"]
            job = core.load_json(job_path, max_bytes=1048576)
            job["seed"] = 99
            core.atomic_write_json(job_path, job)
            run["job"]["sha256"] = core.sha256_file(job_path)
            self.rewrite_frames(root, run, lambda r: r.update(job_sha256=run["job"]["sha256"]))
            with self.assertRaises(pipeline.Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 4)

    def test_r2_old_unbound_receipt_cannot_see(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = fixture(root)
            del run["job"]
            del run["request_sha256"]
            core.atomic_write_json(root / "receipt.json", run)
            with self.assertRaises(pipeline.Refusal) as caught:
                evaluate.read_run(root)
            self.assertEqual(caught.exception.rc, 3)

    def test_r1_rgb_error_boundary_and_null_are_not_rounded_to_pass(self):
        np, _ = metrics.modules()
        image = np.arange(64 * 64, dtype=np.int32).reshape(64, 64) % 256
        detail = metrics.detail(image, image)
        policy = evaluate.load_policy(pipeline.HERE / "policy.json")
        for value, rc in ((None, 3), (1000, 0), (1001, 5)):
            detail["rgb_mse_ppm"] = value
            self.assertEqual(metrics.gate(noise=0, reference_residual=0, detail_metrics=detail,
                                         motion=None, policy=policy)["rc"], rc)
        a = np.zeros((64, 64, 3), dtype=np.int32)
        b = a.copy()
        b[0, 0, 0] = 1
        self.assertEqual(metrics.rgb_mse_ppm(a, a), 0)
        self.assertGreater(metrics.rgb_mse_ppm(a, b), 0)

    def test_r3_disabling_cleanup_makes_the_descendant_control_red(self):
        children = []
        spawn = pipeline.af_proc.popen
        def capture(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child
        result = unittest.TestResult()
        try:
            with patch.object(pipeline.af_proc, "popen", side_effect=capture), \
                 patch.object(pipeline, "reap_group", return_value=None):
                test_f5_repair.RenderRepairTests("test_r3_owned_descendant_is_dead_after_success_error_and_timeout").run(result)
            self.assertFalse(result.wasSuccessful(), "cleanup-removal mutant was invisible")
        finally:
            for child in children:
                pipeline.reap_group(child)


if __name__ == "__main__":
    unittest.main()
