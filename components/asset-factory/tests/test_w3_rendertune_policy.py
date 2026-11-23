import contextlib
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from tools.render import evaluate, policy_sweep as sweep
from tools.render.pipeline import HERE, Refusal


POLICY = evaluate.load_policy(HERE / "policy.json")


def trial(name="good", samples=64, cost=10):
    frame = {"rc": 0, "noise_ppm": 1000, "reference_flicker_ppm": 5000,
        "detail": {"detail_ratio_ppm": 960000, "detail_tile_min_ppm": 600000, "rgb_mse_ppm": 500},
        "motion": None, "timings_ms": {"render": cost, "oidn": 1, "tone_map": 1}}
    return {"id": name, "samples": samples, "denoise": "oidn_guided", "wall_ms": 100,
        "context_sha256": "a" * 64,
        "quality": {"rc": 0, "verdict": "PASS", "policy_sha256": core.sha256_json(POLICY),
            "run_sha256": "b" * 64, "frames": [{**copy.deepcopy(frame), "frame": n} for n in (1, 2)]}}


def choose(rows, ids=None):
    return sweep.choose(rows, required_ids=ids or [r["id"] for r in rows], frames=[1, 2],
        context_sha256="a" * 64, policy=POLICY)


class SelectionTests(unittest.TestCase):
    def manifest(self, root, policy=POLICY, samples=64):
        policy_path = root / "policy.json"
        core.atomic_write_json(policy_path, policy)
        record = {"schema": "af.render-sweep/v1", "policy": {"path": "policy.json", "sha256": core.sha256_file(policy_path)},
            "frames": [1, 2], "context_sha256": "a" * 64, "required_ids": ["timeout"],
            "trials": [{"id": "timeout", "samples": samples, "denoise": "oidn_guided", "wall_ms": 100,
                "run": None, "failure_rc": 6}]}
        path = root / "sweep.json"
        core.atomic_write_json(path, record)
        return path

    def test_real_manifest_failed_attempt_remains_cannot_see(self):
        with tempfile.TemporaryDirectory() as d:
            result = sweep.measure(self.manifest(Path(d)), vqa_root=Path(d))
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["trials"][0]["rc"], 6)
        self.assertIsNone(result["selection"])

    def test_weakening_the_policy_is_stale_even_with_recomputed_hash(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.manifest(Path(d), {**POLICY, "detail_min_ppm": 949999})
            with self.assertRaises(Refusal) as e:
                sweep.measure(path, vqa_root=Path(d))
        self.assertEqual(e.exception.rc, 4)

    def test_invalid_sample_count_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Refusal) as e:
                sweep.measure(self.manifest(Path(d), samples=0), vqa_root=Path(d))
        self.assertEqual(e.exception.rc, 2)

    def test_lowest_cost_passing_candidate_wins(self):
        result = choose([trial("slow", 128, 20), trial("fast", 64, 10)])
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["selection"]["id"], "fast")
        self.assertEqual(result["selection"]["cost_ms"], 24)
        self.assertEqual(result["scope"], "declared_frames_only")

    def test_false_pass_below_detail_floor_red_then_green(self):
        row = trial("cheap", 16, 1)
        row["quality"]["frames"][0]["detail"]["detail_ratio_ppm"] = 949999
        self.assertEqual(choose([row])["rc"], 5)
        result = choose([row, trial()])
        self.assertEqual(result["selection"]["id"], "good")
        row["quality"]["frames"][0]["detail"]["detail_ratio_ppm"] = 950000
        self.assertEqual(choose([row])["rc"], 0)

    def test_noise_local_detail_and_flicker_each_block_selection(self):
        for name, value in (("noise_ppm", 6001), ("reference_flicker_ppm", 10001)):
            row = trial()
            row["quality"]["frames"][0][name] = value
            self.assertEqual(choose([row])["rc"], 5)
        row = trial()
        row["quality"]["frames"][0]["detail"]["detail_tile_min_ppm"] = 499999
        self.assertEqual(choose([row])["rc"], 5)

    def test_missing_metric_cannot_see_even_with_summary_pass(self):
        row = trial()
        row["quality"]["frames"][0]["detail"] = None
        self.assertEqual(choose([row])["rc"], 3)

    def test_empty_frame_summary_pass_refused(self):
        row = trial()
        row["quality"]["frames"] = []
        with self.assertRaises(Refusal) as e:
            choose([row])
        self.assertEqual(e.exception.rc, 3)

    def test_dropped_trial_is_not_silently_removed(self):
        with self.assertRaises(Refusal) as e:
            choose([trial()], ids=["good", "missing"])
        self.assertEqual(e.exception.rc, 3)

    def test_duplicate_trial_refused(self):
        with self.assertRaises(Refusal) as e:
            choose([trial(), trial()], ids=["good"])
        self.assertEqual(e.exception.rc, 2)

    def test_failure_time_stays_in_sweep_cost(self):
        failed = {"id": "timeout", "wall_ms": 999, "quality": None, "failure_rc": 6}
        result = choose([failed, trial()])
        self.assertEqual(result["total_attempt_wall_ms"], 1099)
        self.assertEqual(result["trials"][0]["rc"], 6)
        self.assertEqual(result["selection"]["id"], "good")
        self.assertEqual(choose([failed])["rc"], 3)

    def test_null_or_boolean_cost_never_measured_zero(self):
        for value in (None, True, 0):
            row = trial()
            row["wall_ms"] = value
            with self.assertRaises(Refusal) as e:
                choose([row])
            self.assertEqual(e.exception.rc, 3)

    def test_changed_context_or_policy_is_stale(self):
        for key in ("context_sha256", "policy_sha256"):
            row = trial()
            (row if key == "context_sha256" else row["quality"])[key] = "c" * 64
            with self.assertRaises(Refusal) as e:
                choose([row])
            self.assertEqual(e.exception.rc, 4)

    def test_refusal_preserves_existing_output_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "policy.json"
            target.write_bytes(b"caller sentinel\n")
            with mock.patch.object(sweep, "measure", side_effect=Refusal(3, "no_truth")):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = sweep.main(["--sweep", "missing.json", "--vqa-root", d, "--output", str(target)])
            self.assertEqual(rc, 3)
            self.assertEqual(target.read_bytes(), b"caller sentinel\n")

    def test_success_is_canonical_and_published(self):
        result = choose([trial()])
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "selection.json"
            with mock.patch.object(sweep, "measure", return_value=result):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = sweep.main(["--sweep", "sweep.json", "--vqa-root", d, "--output", str(target)])
            self.assertEqual(rc, 0)
            self.assertEqual(target.read_bytes(), core.canonical_json(result).encode())


if __name__ == "__main__":
    unittest.main()
