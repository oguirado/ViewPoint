"""Unit tests for tools/fp_cause_fixtures.py.

Asserts generated synthetic fixture trees are readable by score.read_truth_tiles
and noise_rescore.decode_regions, that expected_causes.json counts match injected
tiles across all 5 FP causes (SHADE, EDGE_BLEED, GLOBAL_EXPOSURE, AA_JITTER, UNATTRIBUTED),
and that output is byte-identical for identical seeds.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
from blender_visual_qa.truth import score
from tools import fp_cause_fixtures as fp_fx
from tools import noise_rescore as noise
from visioncore import grid as vgrid


def _tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class TestFpCauseFixtures(unittest.TestCase):
    def test_truth_tiles_readable_by_score(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / "run"
            fp_fx.generate_fixtures(out_dir, seed=42)

            # Check truth tiles for fit unit and gate unit
            fit_labels = out_dir / "fit" / "seed_000042" / "B6" / "labels" / "tiles_truth.bin"
            gate_labels = out_dir / "gate" / "seed_000142" / "B1" / "labels" / "tiles_truth.bin"

            self.assertTrue(fit_labels.is_file())
            self.assertTrue(gate_labels.is_file())

            fit_truth = score.read_truth_tiles(fit_labels)
            gate_truth = score.read_truth_tiles(gate_labels)

            self.assertEqual(fit_truth["hdr"]["n_frames"], 4)
            self.assertEqual(gate_truth["hdr"]["n_frames"], 4)
            self.assertEqual(gate_truth["tl"].shape, (4, 16))

    def test_regions_decodable_by_noise_rescore(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / "run"
            fp_fx.generate_fixtures(out_dir, seed=42)

            g = vgrid.Grid(64, 64)
            gate_unit = out_dir / "gate" / "seed_000142" / "B1"

            frame_rows = [json.loads(line) for line in (gate_unit / "vision" / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            region_rows = [json.loads(line) for line in (gate_unit / "vision" / "regions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

            decoded = noise.decode_regions(frame_rows, region_rows, g)
            self.assertEqual(decoded["cls"].shape, (4, 16))
            self.assertEqual(decoded["mixed_class_tiles"], 0)

    def test_expected_causes_counts_and_all_causes_present(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / "run"
            fp_fx.generate_fixtures(out_dir, seed=42)

            exp_path = out_dir / "expected_causes.json"
            self.assertTrue(exp_path.is_file())

            doc = json.loads(exp_path.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "bvqa.fp-cause-expected/v1")
            self.assertEqual(doc["seed"], 42)
            self.assertEqual(doc["timings"]["generation"], "VM")

            counts = doc["counts"]
            expected_causes = {"SHADE", "EDGE_BLEED", "GLOBAL_EXPOSURE", "AA_JITTER", "UNATTRIBUTED"}
            self.assertEqual(set(counts.keys()), expected_causes)

            for cause in expected_causes:
                self.assertGreater(counts[cause], 0, f"Cause {cause} must have at least 1 injected tile")

            injected = doc["injected_tiles"]
            self.assertEqual(len(injected), doc["total_injected_tiles"])
            self.assertEqual(sum(counts.values()), doc["total_injected_tiles"])

            # Verify that cause breakdown in injected_tiles list matches counts dict exactly
            actual_counts = {c: 0 for c in expected_causes}
            for tile_record in injected:
                cause = tile_record["cause"]
                self.assertIn(cause, expected_causes)
                actual_counts[cause] += 1

            self.assertEqual(actual_counts, counts)

    def test_byte_identical_output_for_same_seed(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            out1 = Path(d1) / "run"
            out2 = Path(d2) / "run"

            fp_fx.generate_fixtures(out1, seed=123)
            fp_fx.generate_fixtures(out2, seed=123)

            bytes1 = _tree_bytes(out1)
            bytes2 = _tree_bytes(out2)

            self.assertEqual(set(bytes1.keys()), set(bytes2.keys()))
            for path in bytes1:
                self.assertEqual(bytes1[path], bytes2[path], f"File mismatch for {path}")

    def test_different_seeds_produce_different_paths(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            out1 = Path(d1) / "run"
            out2 = Path(d2) / "run"

            fp_fx.generate_fixtures(out1, seed=123)
            fp_fx.generate_fixtures(out2, seed=456)

            doc1 = json.loads((out1 / "expected_causes.json").read_text(encoding="utf-8"))
            doc2 = json.loads((out2 / "expected_causes.json").read_text(encoding="utf-8"))

            self.assertEqual(doc1["seed"], 123)
            self.assertEqual(doc2["seed"], 456)


if __name__ == "__main__":
    unittest.main()
