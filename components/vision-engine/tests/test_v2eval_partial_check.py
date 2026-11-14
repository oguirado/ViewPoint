"""Unit tests for eval/v2eval/partial_check.py."""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from eval.gui_synth.score import aggregate, score
from eval.v2eval.exploration import leaderboard
from eval.v2eval.run import add_honesty, lean
from eval.v2eval.partial_check import check_partial_dir


class TestV2EvalPartialCheck(unittest.TestCase):
    def setUp(self):
        self.scenes = [
            {"scene_id": "DEV:scene1", "kind": "static_clean", "w": 320, "h": 180, "n": 10},
            {"scene_id": "DEV:scene2", "kind": "aa_jitter", "w": 320, "h": 180, "n": 10},
        ]
        self.cls = np.zeros((10, 8), dtype=np.int64)
        self.mv = np.zeros((10, 8), dtype=np.int64)
        self.harness_sha = "harness_hash_123"

        self.row1 = lean(score(self.cls, self.mv, self.mv, [], scene=self.scenes[0]))
        self.row2 = lean(score(self.cls, self.mv, self.mv, [], scene=self.scenes[1]))

    def _build_fixture(
        self,
        td_path: Path,
        rows=None,
        complete_variants=None,
        pending_variants=None,
        lb_rows=None,
        lb_scene_ids=None,
        lb_harness_sha=None,
        truncated_line=False,
    ):
        if complete_variants is None:
            complete_variants = ["baseline", "var1"]
        if pending_variants is None:
            pending_variants = ["var2"]
        if lb_scene_ids is None:
            lb_scene_ids = ["DEV:scene1", "DEV:scene2"]
        if lb_harness_sha is None:
            lb_harness_sha = self.harness_sha

        if rows is None:
            rows = [
                {
                    "variant": "baseline",
                    "scene_id": "DEV:scene1",
                    "status": "OK",
                    "error": None,
                    "row": self.row1,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": None,
                    "calls": None,
                    "module_sha256": None,
                    "harness_sha256": self.harness_sha,
                },
                {
                    "variant": "baseline",
                    "scene_id": "DEV:scene2",
                    "status": "OK",
                    "error": None,
                    "row": self.row2,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": None,
                    "calls": None,
                    "module_sha256": None,
                    "harness_sha256": self.harness_sha,
                },
                {
                    "variant": "var1",
                    "scene_id": "DEV:scene1",
                    "status": "OK",
                    "error": None,
                    "row": self.row1,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": 500,
                    "calls": 10,
                    "module_sha256": "mod1",
                    "harness_sha256": self.harness_sha,
                },
                {
                    "variant": "var1",
                    "scene_id": "DEV:scene2",
                    "status": "OK",
                    "error": None,
                    "row": self.row2,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": 500,
                    "calls": 10,
                    "module_sha256": "mod1",
                    "harness_sha256": self.harness_sha,
                },
            ]

        # Write rows.jsonl
        rows_lines = [json.dumps(r) for r in rows]
        if truncated_line:
            rows_lines.append('{"variant": "var2", "scene_id": "DEV:sc')
        (td_path / "rows.jsonl").write_text("\n".join(rows_lines) + "\n")

        # Build recomputed leaderboard if lb_rows not provided
        if lb_rows is None:
            metrics1 = add_honesty(aggregate([self.row1, self.row2]))
            data = {
                "split": "DEV",
                "scene_ids": lb_scene_ids,
                "frame_budget_ms": 1000,
                "cost_scope": "executed Python bytecodes in frame API calls and Python dependencies; NumPy/C internals excluded; baseline uninstrumented",
                "prereg_sha256": "prereg123",
                "harness_sha256": lb_harness_sha,
                "variants": {
                    "baseline": {
                        "status": "OK",
                        "error": None,
                        "false_relabels": 0,
                        "static_jitter_tile_frames": 160,
                        "python_opcodes": None,
                        "calls": None,
                        "module_sha256": None,
                        "metrics": metrics1,
                    },
                    "var1": {
                        "status": "OK",
                        "error": None,
                        "false_relabels": 0,
                        "static_jitter_tile_frames": 160,
                        "python_opcodes": 1000,
                        "calls": 20,
                        "module_sha256": "mod1",
                        "metrics": metrics1,
                    },
                },
            }
            # Only include complete variants in leaderboard data
            data["variants"] = {v: data["variants"][v] for v in complete_variants if v in data["variants"]}
            lb_obj = leaderboard(data)
            lb_rows = lb_obj["rows"]

        lb_partial = {
            "schema": "vqa.v2eval-leaderboard/v1",
            "split": "DEV",
            "scene_ids": lb_scene_ids,
            "frame_budget_ms": 1000,
            "cost_scope": "executed Python bytecodes in frame API calls and Python dependencies; NumPy/C internals excluded; baseline uninstrumented",
            "prereg_sha256": "prereg123",
            "harness_sha256": lb_harness_sha,
            "measurement_tag": "MEASURED",
            "diagnostic_wall_time": "variant_diagnostics.json (DIAGNOSTIC; VM time is not evidence)",
            "complete_variants": complete_variants,
            "pending_variants": pending_variants,
            "rows": lb_rows,
        }
        (td_path / "LEADERBOARD.partial.json").write_text(json.dumps(lb_partial, indent=2) + "\n")

    def test_valid_partial_run(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            self._build_fixture(td_path)
            valid, report = check_partial_dir(td_path)
            self.assertTrue(valid, f"Expected valid partial run, got reasons: {report['reasons']}")

    def test_truncated_last_line_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            self._build_fixture(td_path, truncated_line=True)
            valid, report = check_partial_dir(td_path)
            self.assertTrue(valid, f"Expected valid partial run with truncated last line, got reasons: {report['reasons']}")

    def test_duplicate_variant_scene_pair_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            dup_rows = [
                {
                    "variant": "baseline",
                    "scene_id": "DEV:scene1",
                    "status": "OK",
                    "error": None,
                    "row": self.row1,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": None,
                    "calls": None,
                    "module_sha256": None,
                    "harness_sha256": self.harness_sha,
                },
                {
                    "variant": "baseline",
                    "scene_id": "DEV:scene1",
                    "status": "OK",
                    "error": None,
                    "row": self.row1,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": None,
                    "calls": None,
                    "module_sha256": None,
                    "harness_sha256": self.harness_sha,
                },
            ]
            self._build_fixture(td_path, rows=dup_rows)
            valid, report = check_partial_dir(td_path)
            self.assertFalse(valid)
            self.assertTrue(any("Duplicate" in r for r in report["reasons"]))

    def test_mixed_harness_hashes_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            mixed_rows = [
                {
                    "variant": "baseline",
                    "scene_id": "DEV:scene1",
                    "status": "OK",
                    "error": None,
                    "row": self.row1,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": None,
                    "calls": None,
                    "module_sha256": None,
                    "harness_sha256": "harness_A",
                },
                {
                    "variant": "baseline",
                    "scene_id": "DEV:scene2",
                    "status": "OK",
                    "error": None,
                    "row": self.row2,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": None,
                    "calls": None,
                    "module_sha256": None,
                    "harness_sha256": "harness_B",
                },
            ]
            self._build_fixture(td_path, rows=mixed_rows)
            valid, report = check_partial_dir(td_path)
            self.assertFalse(valid)
            self.assertTrue(any("Multiple harness_sha256" in r for r in report["reasons"]))

    def test_pending_variant_in_leaderboard_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bad_lb_rows = [
                {"variant": "baseline", "status": "OK"},
                {"variant": "var2", "status": "OK"},  # var2 is pending
            ]
            self._build_fixture(td_path, pending_variants=["var2"], lb_rows=bad_lb_rows)
            valid, report = check_partial_dir(td_path)
            self.assertFalse(valid)
            self.assertTrue(any("Pending variant 'var2' appears" in r for r in report["reasons"]))

    def test_test_scene_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bad_rows = [
                {
                    "variant": "baseline",
                    "scene_id": "TEST:scene1",
                    "status": "OK",
                    "error": None,
                    "row": self.row1,
                    "false_relabels": 0,
                    "static_jitter_tile_frames": 80,
                    "python_opcodes": None,
                    "calls": None,
                    "module_sha256": None,
                    "harness_sha256": self.harness_sha,
                },
            ]
            self._build_fixture(td_path, rows=bad_rows)
            valid, report = check_partial_dir(td_path)
            self.assertFalse(valid)
            self.assertTrue(any("does not start with 'DEV:'" in r for r in report["reasons"]))

    def test_tampered_metric_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            metrics1 = add_honesty(aggregate([self.row1, self.row2]))
            data = {
                "split": "DEV",
                "scene_ids": ["DEV:scene1", "DEV:scene2"],
                "frame_budget_ms": 1000,
                "cost_scope": "executed Python bytecodes in frame API calls and Python dependencies; NumPy/C internals excluded; baseline uninstrumented",
                "prereg_sha256": "prereg123",
                "harness_sha256": self.harness_sha,
                "variants": {
                    "baseline": {
                        "status": "OK",
                        "error": None,
                        "false_relabels": 0,
                        "static_jitter_tile_frames": 160,
                        "python_opcodes": None,
                        "calls": None,
                        "module_sha256": None,
                        "metrics": metrics1,
                    },
                    "var1": {
                        "status": "OK",
                        "error": None,
                        "false_relabels": 0,
                        "static_jitter_tile_frames": 160,
                        "python_opcodes": 1000,
                        "calls": 20,
                        "module_sha256": "mod1",
                        "metrics": metrics1,
                    },
                },
            }
            lb_obj = leaderboard(data)
            tampered_rows = lb_obj["rows"]
            # Tamper metric
            tampered_rows[1]["python_opcodes"] = 999999
            self._build_fixture(td_path, lb_rows=tampered_rows)
            valid, report = check_partial_dir(td_path)
            self.assertFalse(valid)
            self.assertTrue(any("Leaderboard row values do not match recomputation" in r for r in report["reasons"]))


if __name__ == "__main__":
    unittest.main()
