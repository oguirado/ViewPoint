import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from visioncore.desc import TILE_DTYPE
from visioncore.grid import Grid
from eval.gui_synth.score import aggregate, score
from eval.v2eval.exploration import leaderboard, quick_selection, write_leaderboard
from eval.v2eval.plugins import Variant, parse_variant, vector
from eval.v2eval.run import add_honesty


class PluginContract(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(32, 32)
        self.tiles = np.zeros(self.grid.N, dtype=TILE_DTYPE)
        self.tiles["cls"] = 2
        self.prev = np.zeros((32, 32), np.uint8)
        self.cur = self.prev.copy()
        self.cur[0, 0] = 99

    def test_each_api_loads_and_claims_without_touching_input(self):
        for kind in ("gm", "fl", "st"):
            with self.subTest(kind=kind):
                adapter = Variant(kind+"=tests.test_v2eval.toy_variants", self.grid, 3000)
                original = self.tiles.copy()
                try:
                    result = adapter.update(self.prev, self.cur, self.tiles)
                    self.assertEqual(adapter.status, "OK", adapter.error)
                    self.assertIsNotNone(result)
                    self.assertTrue((result["cls"] == (4 if kind == "fl" else 3)).any())
                    mask = result["cls"] != self.tiles["cls"]
                    for key in result:
                        np.testing.assert_array_equal(result[key][~mask], self.tiles[key][~mask])
                    np.testing.assert_array_equal(self.tiles, original)
                    self.assertGreater(adapter.ops, 0)
                finally:
                    adapter.close()
                self.assertFalse(adapter.process.is_alive())

    def test_raise_malformed_and_frame_timeout_report_failed(self):
        for kind in ("gm", "fl", "st"):
            with self.subTest(kind=kind):
                adapter = Variant(kind+"=tests.test_v2eval.bad_variants", self.grid, 3000)
                try:
                    self.assertEqual(adapter.status, "OK", adapter.error)
                    adapter.budget = .1
                    self.assertIsNone(adapter.update(self.prev, self.cur, self.tiles))
                    self.assertEqual(adapter.status, "FAILED")
                    self.assertIsNone(adapter.update(self.prev, self.cur, self.tiles))
                    self.assertFalse(adapter.process.is_alive())
                finally:
                    adapter.close()

    def test_bad_consensus_import_and_constructor_are_contained(self):
        for spec in ("gm=tests.test_v2eval.bad_mask", "gm=tests.test_v2eval.absent", "fl=tests.test_v2eval.bad_mask"):
            adapter = Variant(spec, self.grid, 3000)
            try:
                self.assertIsNone(adapter.update(self.prev, self.cur, self.tiles))
                self.assertEqual(adapter.status, "FAILED")
            finally:
                adapter.close()

    def test_subtile_only_claims_baseline_changed(self):
        self.tiles["cls"] = 1
        adapter = Variant("st=tests.test_v2eval.toy_variants", self.grid, 3000)
        try:
            result = adapter.update(self.prev, self.cur, self.tiles)
            np.testing.assert_array_equal(result["cls"], self.tiles["cls"])
        finally:
            adapter.close()

    def test_run_records_reference_rows_and_failed_variant_without_crashing(self):
        from eval.gui_synth.scenes import build
        from eval.v2eval import exploration
        scene = build("static_clean", 0, w=64, h=48, n=3, split="DEV")
        scratch = Path(__file__).resolve().parents[2]/"build/room_E"
        scratch.mkdir(parents=True, exist_ok=True)   # build/ is untracked: clean worktrees lack it
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            out = Path(tmp)
            with patch.object(exploration, "OUT", out), patch.object(exploration, "RESULTS", out/"results.json"):
                data = exploration.run_variants([scene], ["fl=tests.test_v2eval.bad_variants",
                    "fl=tests.test_v2eval.toy_variants"], 3000)
            board = leaderboard(data)
            self.assertEqual(len(board["rows"]), 6)
            self.assertEqual(sum(r["reference"] for r in board["rows"]), 4)
            failed = data["variants"]["fl=tests.test_v2eval.bad_variants"]
            self.assertEqual(failed["status"], "FAILED")
            self.assertIsNone(failed["metrics"])
            self.assertEqual(data["variants"]["fl=tests.test_v2eval.toy_variants"]["status"], "OK")
            self.assertTrue((out/"variant_diagnostics.json").exists())

    def test_vector_rejects_malformed_values(self):
        for value in ((True, 0, 0), (0.5, 0, 0), (3, 0, 0), (1, 0, float("nan")), (1, 0, -1), [1, 2]):
            with self.assertRaises(ValueError):
                vector(value, 2)
        for value in ((0, 0, 1), (1, 0, 0), (1, 0, 1.5)):
            with self.assertRaises(ValueError):
                vector(value, 2, blocks=True)
        for spec in ("oops", "xx=eval.foo", "gm=../foo"):
            with self.assertRaises(ValueError):
                parse_variant(spec)


class SelectionAndBoard(unittest.TestCase):
    def test_stratified_stable_exact_dev_selection_and_negative(self):
        scenes = [{"scene_id": f"DEV:{kind}:{n}", "kind": kind, "split": "DEV", "w": w, "h": w}
                  for kind in ("z", "a", "b") for n,w in enumerate((1920, 320))]
        chosen = quick_selection(scenes, 4)
        self.assertEqual(chosen, quick_selection(list(reversed(scenes)), 4))
        self.assertEqual([s["kind"] for s in chosen[:3]], ["a", "b", "z"])
        self.assertTrue(all(s["w"] == 320 for s in chosen[:3]))
        for n in (0, 7):
            with self.assertRaises(ValueError):
                quick_selection(scenes, n)
        with self.assertRaises(ValueError):
            quick_selection([dict(scenes[0], split="TEST")], 1)

    def test_legacy_postpasses_without_options_keeps_report_cache_path(self):
        from eval.v2eval import run
        scratch = Path(__file__).resolve().parents[2]/"build/room_E"
        scratch.mkdir(parents=True, exist_ok=True)   # build/ is untracked: clean worktrees lack it
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            out = Path(tmp)
            (out/"baseline_verified.json").write_text(json.dumps({"baseline_match": True, "sources": {}}))
            with patch("sys.argv", ["run", "postpasses"]), patch.object(run.os, "sched_getaffinity", return_value={2,3,4}), \
                 patch.object(run, "SCRATCH", out), patch.object(run, "load_dev", return_value={}), \
                 patch.object(run, "verify_source_seal", return_value={}), patch.object(run, "selection", return_value=[]), \
                 patch.object(run.prereg, "source_hashes", return_value={}), patch.object(run, "library"), \
                 patch.object(run, "run_parallel") as parallel:
                run.main()
                parallel.assert_called_once_with(run.postpass_scene, [], "postpasses")

    def fixture(self):
        a = np.zeros((2, 4), np.int64)
        metrics = add_honesty(aggregate([score(a, a, a, [], scene_id="DEV:toy", kind="static_clean")]))
        record = {"status": "OK", "error": None, "metrics": metrics, "false_relabels": 0,
                  "static_jitter_tile_frames": 8, "python_opcodes": 32, "calls": 2}
        return {"split": "DEV", "scene_ids": ["DEV:toy"], "frame_budget_ms": 1000,
                "cost_scope": "test", "variants": {"baseline": record, "gm=tests.test_v2eval.toy_variants": copy.deepcopy(record)}}

    def test_leaderboard_byte_determinism_and_failed_no_partial_score(self):
        data = self.fixture()
        scratch = Path(__file__).resolve().parents[2]/"build/room_E"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            out = Path(tmp)
            write_leaderboard(data, out)
            first = [(out/p).read_bytes() for p in ("LEADERBOARD.json", "LEADERBOARD.md")]
            data["variants"] = dict(reversed(list(data["variants"].items())))
            write_leaderboard(data, out)
            self.assertEqual(first, [(out/p).read_bytes() for p in ("LEADERBOARD.json", "LEADERBOARD.md")])
        data["variants"]["baseline"]["status"] = "FAILED"
        row = leaderboard(data)["rows"][0]
        self.assertIsNone(row["MOVED"])
        self.assertIsNone(row["silent_miss_ppm"])
        self.assertIsNone(row["false_relabels_static_jitter_ppm"])
        data["scene_ids"] = ["TEST:toy"]
        with self.assertRaises(ValueError):
            leaderboard(data)
