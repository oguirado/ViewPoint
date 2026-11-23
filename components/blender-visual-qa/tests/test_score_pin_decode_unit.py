"""Unit tests for truth.score (s4_iou, ppm), truth.pin (origins, configured_wheel),
and truth.decode_worker (parse_cpus).
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import decode_worker, pin, score


class TestScoreUnit(unittest.TestCase):
    def test_ppm_rounding_and_boundaries(self):
        # Zero denominator returns None
        self.assertIsNone(score.ppm(10, 0))
        self.assertIsNone(score.ppm(0, 0))

        # Exact values
        self.assertEqual(score.ppm(0, 100), 0)
        self.assertEqual(score.ppm(1, 1), 1000000)
        self.assertEqual(score.ppm(1, 2), 500000)

        # Truncation / floor division rounding boundaries
        self.assertEqual(score.ppm(1, 3), 333333)
        self.assertEqual(score.ppm(2, 3), 666666)
        self.assertEqual(score.ppm(1, 7), 142857)
        self.assertEqual(score.ppm(999999, 1000000), 999999)

        # Large numbers
        self.assertEqual(score.ppm(500, 1000), 500000)

    def test_s4_iou_synthetic_sequences(self):
        # tl shape (2, 4): 2 frames, 4 tiles per frame
        # Frame 0 is ignored by s4_iou
        # Frame 1: tiles 0,1 are TL_RIGID (3) which is in DIRTY_TL. Tiles 2,3 are C_UNCHANGED (0).
        tl = np.array([
            [3, 3, 3, 3],  # Frame 0 (ignored)
            [3, 3, 0, 0],  # Frame 1
        ], dtype=np.uint8)

        # ob shape (2, 4): truth object IDs
        ob = np.array([
            [1, 1, 1, 1],  # Frame 0
            [10, 10, 20, 0],  # Frame 1: object 10 on tiles 0,1; object 20 on tile 2; object 0 on tile 3
        ], dtype=np.uint16)

        # morton codes for tiles 0..3
        morton = np.array([0, 1, 2, 3], dtype=np.int64)

        # Region sequence for 2 frames
        # Frame 1 has regions:
        # Region A spans tiles 0..1 (morton 0..1) -> tiles {0, 1}
        # Region B spans tiles 1..2 (morton 1..2) -> tiles {1, 2}
        regions_seq = [
            [],  # Frame 0
            [
                {"spans": [["0", "1"]]},  # Region A: {0, 1}
                {"spans": [["1", "2"]]},  # Region B: {1, 2}
            ]
        ]

        # For Frame 1:
        # Object 10 is on dirty tiles {0, 1}.
        # Against Region A ({0, 1}): intersection = {0, 1} (2), union = {0, 1} (2) -> IoU = 2/2 = 1,000,000 ppm.
        # Against Region B ({1, 2}): intersection = {1} (1), union = {0, 1, 2} (3) -> IoU = 1/3 = 333,333 ppm.
        # Max IoU for Object 10 = 1,000,000 ppm.
        # Object 20 is on tile 2, but tile 2 is clean (tl=0) -> not in DIRTY_TL -> no sample for Object 20.
        # Object 0 is ignored.
        ious = score.s4_iou(tl, ob, morton, regions_seq)
        self.assertEqual(ious, [1000000])

    def test_s4_iou_partial_overlap_and_multiple_objects(self):
        # 3 frames, 4 tiles
        tl = np.array([
            [0, 0, 0, 0],  # Frame 0 (ignored)
            [3, 3, 3, 3],  # Frame 1: all dirty (tl=3)
            [4, 4, 0, 0],  # Frame 2: tiles 0,1 dirty (tl=4)
        ], dtype=np.uint8)

        ob = np.array([
            [0, 0, 0, 0],
            [1, 1, 1, 2],  # Frame 1: obj 1 on tiles 0,1,2; obj 2 on tile 3
            [1, 1, 2, 2],  # Frame 2: obj 1 on tiles 0,1 (dirty); obj 2 on tile 2 (clean)
        ], dtype=np.uint16)

        morton = np.array([0, 1, 2, 3], dtype=np.int64)

        regions_seq = [
            [],
            # Frame 1: region covering tiles 0, 1 (morton 0..1)
            [{"spans": [["0", "1"]]}],
            # Frame 2: region covering tile 0 (morton 0..0)
            [{"spans": [["0", "0"]]}],
        ]

        # Frame 1:
        # Obj 1: truth dirty tiles {0, 1, 2}. Region tiles {0, 1}.
        # Intersection = {0, 1} (2), Union = {0, 1, 2} (3) -> IoU = 2/3 = 666,666 ppm.
        # Obj 2: truth dirty tiles {3}. Region tiles {0, 1}.
        # Intersection = {} (0), Union = {0, 1, 3} (3) -> IoU = 0 ppm.

        # Frame 2:
        # Obj 1: truth dirty tiles {0, 1}. Region tiles {0}.
        # Intersection = {0} (1), Union = {0, 1} (2) -> IoU = 1/2 = 500,000 ppm.
        # Obj 2: on tile 2, but tile 2 is clean (tl=0) -> ignored.

        ious = score.s4_iou(tl, ob, morton, regions_seq)
        # Result is sorted list of ppm values
        self.assertEqual(ious, [0, 500000, 666666])


class TestPinUnit(unittest.TestCase):
    def test_origins_returns_only_engine_packages(self):
        res = pin.origins()
        self.assertIsInstance(res, dict)
        for name in res:
            pkg = name.split(".")[0]
            self.assertIn(pkg, pin.ENGINE_PKGS, f"Unexpected package in origins: {name}")

        # Ensure known loaded non-engine modules are not included
        self.assertNotIn("json", res)
        self.assertNotIn("unittest", res)
        self.assertNotIn("numpy", res)

    def test_configured_wheel_failing_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 1. Non-existent config file
            missing_cfg = tmp / "nonexistent.json"
            with self.assertRaises(pin.PinError) as ctx:
                pin.configured_wheel(missing_cfg)
            self.assertIn("E_PIN", str(ctx.exception))

            # 2. Config file pointing to missing wheel
            wheel_file = tmp / "fake_engine.whl"
            cfg_file = tmp / "config.json"
            cfg_file.write_text(json.dumps({
                "engine_path": str(wheel_file),
                "pin": {"wheel_sha256": "0" * 64}
            }))
            with self.assertRaises(pin.PinError) as ctx:
                pin.configured_wheel(cfg_file)
            self.assertIn("E_PIN", str(ctx.exception))

            # 3. Config file with wrong wheel SHA-256
            wheel_file.write_bytes(b"dummy wheel content")
            actual_sha = hashlib.sha256(b"dummy wheel content").hexdigest()
            wrong_sha = "0" * 64
            cfg_file.write_text(json.dumps({
                "engine_path": str(wheel_file),
                "pin": {"wheel_sha256": wrong_sha}
            }))
            with self.assertRaises(pin.PinError) as ctx:
                pin.configured_wheel(cfg_file)
            self.assertIn("E_PIN", str(ctx.exception))
            self.assertIn(wrong_sha, str(ctx.exception))

            # 4. Correct wheel SHA-256 returns absolute path
            cfg_file.write_text(json.dumps({
                "engine_path": str(wheel_file),
                "pin": {"wheel_sha256": actual_sha}
            }))
            path = pin.configured_wheel(cfg_file)
            self.assertEqual(path, str(wheel_file))


class TestDecodeWorkerUnit(unittest.TestCase):
    def test_parse_cpus_valid(self):
        self.assertEqual(decode_worker.parse_cpus("0-4,10-13"), frozenset({0, 1, 2, 3, 4, 10, 11, 12, 13}))
        self.assertEqual(decode_worker.parse_cpus("5"), frozenset({5}))
        self.assertEqual(decode_worker.parse_cpus("3-3"), frozenset({3}))
        self.assertEqual(decode_worker.parse_cpus("5-8,3-4"), frozenset({3, 4, 5, 6, 7, 8}))
        self.assertEqual(decode_worker.parse_cpus("0,1,2"), frozenset({0, 1, 2}))

    def test_parse_cpus_invalid(self):
        invalid_inputs = [
            "abc",
            "10-5",  # lo > hi
            "-1",  # lo < 0
            "1024",  # hi >= 1024
            "1,2,",  # trailing comma
            ",1",  # leading comma
            "1--2",  # double dash
            "",  # empty string
        ]
        for text in invalid_inputs:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    decode_worker.parse_cpus(text)


if __name__ == "__main__":
    unittest.main()
