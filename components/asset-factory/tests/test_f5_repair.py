"""Adversarial render regressions: witnesses must reject the planted defect."""
from asset_factory import proc as af_proc
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.render import evaluate, metrics, pipeline
from test_f5_evaluate import fixture


class RenderRepairTests(unittest.TestCase):
    def test_r1_static_inversion_chroma_and_displacement_are_refused(self):
        np, _ = metrics.modules()
        policy = evaluate.load_policy(pipeline.HERE / "policy.json")
        y, x = np.indices((64, 64))
        plane = (x * 4).astype(np.int32)
        reference = np.repeat(plane[:, :, None], 3, axis=2)
        candidates = (255 - reference,
                      np.clip(reference + np.array([50, -25, 0]), 0, 255),
                      np.roll(reference, 20, axis=1))
        for candidate in candidates:
            with self.subTest(corruption=int(candidate.sum())):
                detail = metrics.detail(metrics.luma(candidate), metrics.luma(reference))
                # Current implementation ignores this independent RGB witness.
                error = candidate.astype(np.int64) - reference
                detail["rgb_mse_ppm"] = metrics.ceiling_ppm(
                    (error * error).sum(dtype=np.int64), 255**2 * reference.size)
                result = metrics.gate(noise=0, reference_residual=0,
                    detail_metrics=detail, motion=None, policy=policy)
                self.assertEqual(result["rc"], 5)
                self.assertIn("rgb_reference_error_above_ceiling", result["failed"])

    def test_r2_semantic_receipt_corruption_never_passes(self):
        mutations = {
            "device": lambda r: r.update(device="UNKNOWN_DEVICE"),
            "denoiser": lambda r: r["denoiser"].update(version="UNKNOWN"),
            "resolution": lambda r: r.update(resolution_percentage=-1),
            "timing": lambda r: r["timings_ms"].update(render=-1),
            "role": lambda r: r["inputs"][0].update(role="unrelated_file"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                run = fixture(root)
                evaluate.read_run(root)  # A valid control must be accepted first.
                for item in run["frames"]:
                    path = root / item["path"]
                    record = core.load_json(path, max_bytes=1048576)
                    mutate(record)
                    core.atomic_write_json(path, record)
                    item["sha256"] = core.sha256_file(path)
                core.atomic_write_json(root / "receipt.json", run)
                with self.assertRaises((pipeline.Refusal, core.ContractError)):
                    evaluate.read_run(root)

    def test_r3_owned_descendant_is_dead_after_success_error_and_timeout(self):
        for exit_code in (0, 2, None):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                code = ('import subprocess,sys,time;from pathlib import Path;'
                        'p=subprocess.Popen(["/bin/sleep","30"]);'
                        'Path(sys.argv[1]).write_text(str(p.pid));'
                        + ('time.sleep(30)' if exit_code is None else f'sys.exit({exit_code})'))
                start = time.monotonic()
                pid = None
                try:
                    try:
                        pipeline.run([sys.executable, "-c", code, root / "pid"], root / "log", 0.3)
                    except pipeline.Refusal as exc:
                        self.assertEqual(exit_code, 2)
                        self.assertEqual(exc.rc, 2)
                    except subprocess.TimeoutExpired:
                        self.assertIsNone(exit_code)
                    pid = int((root / "pid").read_text())
                    self.assertLess(time.monotonic() - start, 4)
                    stat = Path(f"/proc/{pid}/stat")
                    deadline = time.monotonic() + 1
                    while stat.exists() and stat.read_text().split(") ")[-1][0] != "Z" and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(not stat.exists() or stat.read_text().split(") ")[-1][0] == "Z",
                                    "owned descendant survived cleanup")
                finally:
                    if pid is None and (root / "pid").exists():
                        pid = int((root / "pid").read_text())
                    if pid is not None:
                        stat = Path(f'/proc/{pid}/stat')
                        self.assertTrue(not stat.exists() or stat.read_text().rsplit(')',1)[1].split()[0] in ('Z','X'),
                                        'owned child survived cleanup')

    def test_r4_cuda_refused_before_any_launch_and_output_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            scene = root / "scene.blend"
            scene.write_bytes(b"scene")
            with patch.object(pipeline, "probe_oidn") as probe, patch.object(pipeline, "run") as launch:
                with self.assertRaises(pipeline.Refusal) as caught:
                    pipeline.render(scene, root / "out", oidn=Path("/bin/true"),
                        blender=Path("/bin/true"), start=1, end=2, device="CUDA")
                self.assertEqual(caught.exception.rc, 3)
                probe.assert_not_called()
                launch.assert_not_called()
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["scene.blend"])

    def test_r5_bad_sibling_export_and_import_are_canonical_errors(self):
        for code in ("# noise_ppm export absent\n", "raise ImportError('broken dependency')\n",
                     "def noise_ppm(:\n", "noise_ppm = 42\n"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "vqa3d").mkdir()
                (root / "vqa3d" / "metrics.py").write_text(code)
                stdout = io.StringIO()
                with patch.object(evaluate, "read_run", return_value=({}, [])):
                    with contextlib.redirect_stdout(stdout):
                        rc = evaluate.main(["--run", d, "--vqa-root", d])
                self.assertEqual(rc, 2)
                result = json.loads(stdout.getvalue())
                self.assertEqual((result["rc"], result["verdict"]), (2, "ERROR"))
                self.assertEqual(stdout.getvalue(), core.canonical_json(result))


if __name__ == "__main__":
    unittest.main()
