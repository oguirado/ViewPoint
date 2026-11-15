import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from eval.gui_synth.scenes import build
from eval.v2eval import exploration, incremental, run, run_incremental
from eval.v2eval.incremental import (
    ROW_KEYS,
    ROW_SCHEMA,
    run_variants_incremental,
    safe_variant_name,
    variant_path,
)
from visioncore.desc import TILE_DTYPE
from visioncore.grid import Grid


def _make_fake_scenes():
    return [
        build("static_clean", 0, w=32, h=32, n=3, split="DEV"),
        build("aa_jitter", 0, w=32, h=32, n=3, split="DEV"),
    ]


def _make_stub_analyze(call_log=None):
    def stub_analyze(src, workers=1, cpus=(2, 3, 4)):
        scene = src.scene
        if call_log is not None:
            call_log.append(scene["scene_id"])
        grid = Grid(scene["w"], scene["h"])
        frames = list(src)
        tiles = []
        frame_shas = []
        for frame in frames:
            arr = np.zeros(grid.N, dtype=TILE_DTYPE)
            if frame.i > 0:
                arr["cls"] = 2 if (frame.i % 2 == 1) else 1
            tiles.append(arr)
            frame_shas.append(frame.sha256)
        frames_sha256 = hashlib.sha256(
            "".join(s + "\n" for s in frame_shas).encode("utf-8")
        ).hexdigest()
        return SimpleNamespace(
            tiles=tiles,
            grid=grid,
            frames_sha256=frames_sha256,
        )

    return stub_analyze


class IncrementalEvalTests(unittest.TestCase):
    def setUp(self):
        self.scenes = _make_fake_scenes()
        self.toy_specs = [
            "gm=tests.test_v2eval.toy_variants",
            "fl=tests.test_v2eval.toy_variants",
            "st=tests.test_v2eval.toy_variants",
        ]

    def test_kill_after_k_rows_and_resume_matches_uninterrupted_and_oneshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_oneshot = root / "oneshot"
            out_oneshot.mkdir()
            out_full = root / "full"
            scratch_full = root / "scratch_full"
            out_inc = root / "incremental"
            scratch_inc = root / "scratch_inc"

            oneshot_analyze_calls = []
            stub_oneshot = _make_stub_analyze(oneshot_analyze_calls)
            with patch.object(exploration, "analyze", stub_oneshot), patch.object(
                exploration, "OUT", out_oneshot
            ), patch.object(exploration, "RESULTS", out_oneshot / "variant_results.json"):
                oneshot_data = exploration.run_variants(
                    self.scenes, self.toy_specs, 3000
                )
                exploration.write_leaderboard(oneshot_data, out=out_oneshot)

            full_analyze_calls = []
            stub_full = _make_stub_analyze(full_analyze_calls)
            with patch.object(incremental, "analyze", stub_full):
                run_variants_incremental(
                    self.scenes,
                    self.toy_specs,
                    3000,
                    out=out_full,
                    resume=False,
                    scratch=scratch_full,
                )
            self.assertEqual(len(full_analyze_calls), len(self.scenes))
            self.assertEqual(
                (out_full / "LEADERBOARD.json").read_bytes(),
                (out_oneshot / "LEADERBOARD.json").read_bytes(),
            )
            self.assertEqual(
                (out_full / "LEADERBOARD.md").read_bytes(),
                (out_oneshot / "LEADERBOARD.md").read_bytes(),
            )
            self.assertEqual(
                (out_full / "variant_results.json").read_bytes(),
                (out_oneshot / "variant_results.json").read_bytes(),
            )

            # Kill after k=3 rows: baseline finishes both scenes (2 rows),
            # first variant finishes 1st scene (1 row), then raise before 4th scene evaluation.
            k = 3
            inc_analyze_calls = []
            stub_inc = _make_stub_analyze(inc_analyze_calls)
            eval_pairs_run1 = []
            real_evaluate = incremental.evaluate_scene

            def crashing_evaluate(
                variant_name, scene, budget_ms, harness_sha, prereg_sha, scratch=None
            ):
                if len(eval_pairs_run1) >= k:
                    raise RuntimeError("simulated kill after k rows")
                eval_pairs_run1.append((variant_name, scene["scene_id"]))
                return real_evaluate(
                    variant_name,
                    scene,
                    budget_ms,
                    harness_sha,
                    prereg_sha,
                    scratch=scratch,
                )

            with patch.object(incremental, "analyze", stub_inc), patch.object(
                incremental, "evaluate_scene", crashing_evaluate
            ):
                with self.assertRaises(RuntimeError):
                    run_variants_incremental(
                        self.scenes,
                        self.toy_specs,
                        3000,
                        out=out_inc,
                        resume=False,
                        scratch=scratch_inc,
                    )

            self.assertEqual(len(eval_pairs_run1), k)
            # Baseline ran outermost first across both scenes.
            self.assertEqual(
                eval_pairs_run1[:2],
                [
                    ("baseline", self.scenes[0]["scene_id"]),
                    ("baseline", self.scenes[1]["scene_id"]),
                ],
            )
            # Engine analyze called only once per scene (2 scenes total).
            self.assertEqual(len(inc_analyze_calls), len(self.scenes))

            # Verify rows.jsonl has k valid rows with schema vqa.v2eval-row/v1.
            rows_lines = (out_inc / "rows.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows_lines), k)
            for line in rows_lines:
                row_obj = json.loads(line)
                self.assertEqual(row_obj["schema"], ROW_SCHEMA)
                self.assertEqual(set(row_obj.keys()), set(ROW_KEYS))

            # Verify variant_baseline.json exists and partial leaderboard is valid.
            self.assertTrue(variant_path(out_inc, "baseline").exists())
            partial_json = json.loads(
                (out_inc / "LEADERBOARD.partial.json").read_text(encoding="utf-8")
            )
            self.assertEqual(partial_json["complete_variants"], ["baseline"])
            self.assertIn(eval_pairs_run1[2][0], partial_json["pending_variants"])
            self.assertEqual(partial_json["scenes_done"][eval_pairs_run1[2][0]], 1)
            self.assertEqual([r["variant"] for r in partial_json["rows"]], ["baseline"])
            self.assertTrue((out_inc / "LEADERBOARD.partial.md").exists())
            self.assertFalse((out_inc / "LEADERBOARD.json").exists())

            # Now resume and verify done pairs are NOT re-executed and final LEADERBOARD.json matches byte-for-byte.
            eval_pairs_run2 = []

            def tracking_evaluate(
                variant_name, scene, budget_ms, harness_sha, prereg_sha, scratch=None
            ):
                eval_pairs_run2.append((variant_name, scene["scene_id"]))
                return real_evaluate(
                    variant_name,
                    scene,
                    budget_ms,
                    harness_sha,
                    prereg_sha,
                    scratch=scratch,
                )

            with patch.object(incremental, "analyze", stub_inc), patch.object(
                incremental, "evaluate_scene", tracking_evaluate
            ):
                run_variants_incremental(
                    self.scenes,
                    self.toy_specs,
                    3000,
                    out=out_inc,
                    resume=True,
                    scratch=scratch_inc,
                )

            # None of the first k pairs were re-executed, and no extra engine analyze calls occurred.
            self.assertTrue(set(eval_pairs_run1).isdisjoint(set(eval_pairs_run2)))
            all_variant_count = 1 + len(set(self.toy_specs) | exploration.REFERENCES)
            self.assertEqual(
                len(eval_pairs_run1) + len(eval_pairs_run2),
                all_variant_count * len(self.scenes),
            )
            self.assertEqual(len(inc_analyze_calls), len(self.scenes))

            self.assertEqual(
                (out_inc / "LEADERBOARD.json").read_bytes(),
                (out_full / "LEADERBOARD.json").read_bytes(),
            )
            self.assertEqual(
                (out_inc / "LEADERBOARD.md").read_bytes(),
                (out_full / "LEADERBOARD.md").read_bytes(),
            )
            self.assertEqual(
                (out_inc / "variant_results.json").read_bytes(),
                (out_full / "variant_results.json").read_bytes(),
            )

    def test_failed_variant_stops_consuming_scenes_and_caches_tiles_sha_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "out"
            scratch_dir = root / "scratch"
            analyze_calls = []
            stub = _make_stub_analyze(analyze_calls)
            bad_spec = "fl=tests.test_v2eval.bad_variants"
            good_spec = "fl=tests.test_v2eval.toy_variants"

            with patch.object(incremental, "analyze", stub):
                data = run_variants_incremental(
                    self.scenes,
                    [bad_spec, good_spec],
                    3000,
                    out=out_dir,
                    resume=False,
                    scratch=scratch_dir,
                )

            self.assertEqual(data["variants"][bad_spec]["status"], "FAILED")
            self.assertIsNone(data["variants"][bad_spec]["metrics"])
            self.assertEqual(data["variants"][good_spec]["status"], "OK")
            self.assertTrue(variant_path(out_dir, bad_spec).exists())

            rows = [
                json.loads(line)
                for line in (out_dir / "rows.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            bad_rows = [r for r in rows if r["variant"] == bad_spec]
            # Failed on first scene and stopped consuming subsequent scenes.
            self.assertEqual(len(bad_rows), 1)
            self.assertEqual(bad_rows[0]["scene_id"], self.scenes[0]["scene_id"])
            self.assertEqual(bad_rows[0]["status"], "FAILED")
            self.assertIsNone(bad_rows[0]["row"])

            # Corrupt cached baseline sha for scene 0 and verify baseline_tiles recomputes it.
            before_calls = len(analyze_calls)
            ident0 = run.identity(self.scenes[0])
            meta_path = scratch_dir / (ident0 + ".baseline.json")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["engine_tiles_sha256"] = "0" * 64
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with patch.object(incremental, "analyze", stub):
                incremental.baseline_tiles(self.scenes[0], scratch=scratch_dir)
            self.assertEqual(len(analyze_calls), before_calls + 1)

    def test_resume_tolerates_truncated_last_line_and_refuses_mismatched_seals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "out"
            scratch_dir = root / "scratch"
            stub = _make_stub_analyze()
            real_evaluate = incremental.evaluate_scene
            calls = []

            def stop_after_two(
                variant_name, scene, budget_ms, harness_sha, prereg_sha, scratch=None
            ):
                if len(calls) >= 2:
                    raise RuntimeError("stop after baseline")
                calls.append((variant_name, scene["scene_id"]))
                return real_evaluate(
                    variant_name,
                    scene,
                    budget_ms,
                    harness_sha,
                    prereg_sha,
                    scratch=scratch,
                )

            with patch.object(incremental, "analyze", stub), patch.object(
                incremental, "evaluate_scene", stop_after_two
            ):
                with self.assertRaises(RuntimeError):
                    run_variants_incremental(
                        self.scenes,
                        ["gm=tests.test_v2eval.toy_variants"],
                        3000,
                        out=out_dir,
                        resume=False,
                        scratch=scratch_dir,
                    )

            rows_path = out_dir / "rows.jsonl"
            original_rows = rows_path.read_text(encoding="utf-8")
            # Append a truncated JSON line at the end of rows.jsonl.
            rows_path.write_text(
                original_rows + '{"schema": "vqa.v2eval-row/v1", "variant": "gm=',
                encoding="utf-8",
            )

            # Changed harness_sha256 must be refused.
            with patch.object(incremental, "analyze", stub), patch.object(
                incremental, "harness_sha256", return_value="f" * 64
            ):
                with self.assertRaises(ValueError):
                    run_variants_incremental(
                        self.scenes,
                        ["gm=tests.test_v2eval.toy_variants"],
                        3000,
                        out=out_dir,
                        resume=True,
                        scratch=scratch_dir,
                    )

            # Changed prereg sha must be refused.
            with patch.object(incremental, "analyze", stub), patch.object(
                incremental.prereg, "sha256", return_value="e" * 64
            ):
                with self.assertRaises(ValueError):
                    run_variants_incremental(
                        self.scenes,
                        ["gm=tests.test_v2eval.toy_variants"],
                        3000,
                        out=out_dir,
                        resume=True,
                        scratch=scratch_dir,
                    )

            # Changed scene list must be refused.
            with patch.object(incremental, "analyze", stub):
                with self.assertRaises(ValueError):
                    run_variants_incremental(
                        self.scenes[:1],
                        ["gm=tests.test_v2eval.toy_variants"],
                        3000,
                        out=out_dir,
                        resume=True,
                        scratch=scratch_dir,
                    )

            # With matching seals, the truncated last line is ignored and the run completes cleanly.
            with patch.object(incremental, "analyze", stub):
                resumed = run_variants_incremental(
                    self.scenes,
                    ["gm=tests.test_v2eval.toy_variants"],
                    3000,
                    out=out_dir,
                    resume=True,
                    scratch=scratch_dir,
                )
            self.assertIn("gm=tests.test_v2eval.toy_variants", resumed["variants"])
            self.assertTrue((out_dir / "LEADERBOARD.json").exists())

    def test_cli_run_incremental_checks_taskset_and_seals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "cli_out"
            scratch_dir = root / "cli_scratch"
            stub = _make_stub_analyze()

            # Reject when CPU affinity is outside {2, 3, 4}.
            with patch.object(
                run_incremental.os, "sched_getaffinity", return_value={0, 1}
            ):
                with self.assertRaises(ValueError):
                    run_incremental.main(
                        [
                            "--variant",
                            "gm=tests.test_v2eval.toy_variants",
                            "--quick",
                            "1",
                            "--out",
                            str(out_dir),
                        ]
                    )

            with patch.object(
                run_incremental.os, "sched_getaffinity", return_value={2, 3, 4}
            ), patch.object(run_incremental, "SCRATCH", scratch_dir), patch.object(
                incremental, "SCRATCH", scratch_dir
            ), patch.object(
                incremental, "analyze", stub
            ), patch.object(
                run_incremental, "load_dev", return_value={"prereg_sha": "abc"}
            ), patch.object(
                run_incremental, "verify_source_seal", return_value={"s": "1"}
            ), patch.object(
                run_incremental, "selection", return_value=self.scenes
            ), patch.object(
                run_incremental.prereg, "source_hashes", return_value={"s": "1"}
            ):
                result = run_incremental.main(
                    [
                        "--variant",
                        "gm=tests.test_v2eval.toy_variants",
                        "--quick",
                        "1",
                        "--frame-budget-ms",
                        "2500",
                        "--out",
                        str(out_dir),
                    ]
                )
            self.assertEqual(len(result["scene_ids"]), 1)
            self.assertEqual(result["frame_budget_ms"], 2500.0)
            self.assertTrue((out_dir / "LEADERBOARD.json").exists())
            self.assertEqual(
                safe_variant_name("gm=tests.test_v2eval.toy_variants"),
                "gm_tests.test_v2eval.toy_variants",
            )


if __name__ == "__main__":
    unittest.main()
