"""Unit tests for L4 pipeline helpers in visioncore/pipeline.py (SPEC_ARCH §3)."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from visioncore import canon, desc, layout, pipeline
from visioncore.errors import VqaError
from visioncore.frame import Frame, write_manifest
from visioncore.grid import Grid


class TestFrameNumber(unittest.TestCase):
    def test_frame_number_prefers_render_index(self):
        f = Frame(i=5, t_us=100, render_index=100, rgb=np.zeros((16, 16, 3), dtype=np.uint8))
        res = pipeline.frame_number(f)
        self.assertEqual(res, 100)
        self.assertNotEqual(res, f.i)  # Negative case: does not return dense index when render_index is present

    def test_frame_number_fallback_to_dense_i(self):
        f = Frame(i=5, t_us=100, render_index=None, rgb=np.zeros((16, 16, 3), dtype=np.uint8))
        res = pipeline.frame_number(f)
        self.assertEqual(res, 5)
        self.assertNotEqual(res, 100)  # Negative case: does not return arbitrary non-i value when render_index is None


class TestShmRunDir(unittest.TestCase):
    def test_shm_run_dir_valid(self):
        root = Path("/tmp/test_shm")
        res = pipeline.shm_run_dir("test_run_123", root=root)
        self.assertEqual(res, Path("/tmp/test_shm/vqa_test_run_123"))
        self.assertEqual(pipeline.shm_run_dir("valid_id"), Path("/dev/shm/vqa_valid_id"))

    def test_shm_run_dir_invalid_ids(self):
        invalid_ids = [
            "../bad_run",
            "UPPERCASE",
            "run-with-hyphen",
            "run/with/slash",
            "",
            "a" * 49,
            123,
            None,
        ]
        for bad_id in invalid_ids:
            with self.assertRaises(VqaError) as ctx:
                pipeline.shm_run_dir(bad_id)
            self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")


class TestManifestSource(unittest.TestCase):
    def _make_valid_manifest_dict(self, count=1, storage="png_dir"):
        return {
            "schema": "vqa.frame/v1",
            "width": 32,
            "height": 32,
            "channels": 3,
            "pixel_format": "rgb24",
            "fps_num": 30,
            "fps_den": 1,
            "count": count,
            "storage": storage,
            "raw_path": "frames.rgb24" if storage == "rgb24_raw" else None,
            "frame_bytes": 32 * 32 * 3,
            "frames": [
                {
                    "i": i,
                    "t_us": i * 33333,
                    "render_index": i * 2,
                    "file": f"frame_{i}.png",
                    "sha256": "0" * 64,
                    "file_sha256": "0" * 64,
                }
                for i in range(count)
            ],
            "hints": None,
            "still_probe": None,
            "scope": {"app": "test", "scene": "unit", "res": "32x32", "build_seq": None},
            "provenance": {"adapter_id": "test", "adapter_version": "1", "capture_receipt_sha256": None},
        }

    def test_missing_manifest_raises_e_io(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "nonexistent.json"
            with self.assertRaises(VqaError) as ctx:
                pipeline.ManifestSource(missing)
            self.assertEqual(ctx.exception.code, "E_IO")

    def test_unknown_storage_raises_e_vc_frame_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "frames.manifest.json"
            m = self._make_valid_manifest_dict()
            m["storage"] = "unknown_storage_kind"
            canon.write_atomic(manifest_path, canon.dumps(m))
            with self.assertRaises(VqaError) as ctx:
                pipeline.ManifestSource(manifest_path)
            self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")

    def test_frame_count_mismatch_raises_e_vc_frame_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "frames.manifest.json"
            m = self._make_valid_manifest_dict(count=2)
            m["frames"] = m["frames"][:1]  # Only 1 frame provided for count=2
            canon.write_atomic(manifest_path, canon.dumps(m))
            with self.assertRaises(VqaError) as ctx:
                pipeline.ManifestSource(manifest_path)
            self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")

    def test_valid_manifest_source_len_and_properties(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "frames.manifest.json"
            m = self._make_valid_manifest_dict(count=2, storage="rgb24_raw")
            # Write raw array file
            arr = np.zeros((2, 32, 32, 3), dtype=np.uint8)
            (Path(tmpdir) / "frames.rgb24").write_bytes(arr.tobytes())
            zero_frame_sha = hashlib.sha256(arr[0].tobytes()).hexdigest()
            for i in range(2):
                m["frames"][i]["sha256"] = zero_frame_sha
                m["frames"][i]["file_sha256"] = zero_frame_sha
            write_manifest(manifest_path, m)

            src = pipeline.ManifestSource(manifest_path)
            self.assertEqual(len(src), 2)
            self.assertEqual(src.meta["storage"], "rgb24_raw")
            self.assertEqual(src.meta["width"], 32)
            self.assertEqual(src.meta["height"], 32)
            self.assertIsNotNone(src.raw_array)
            frames = list(src)
            self.assertEqual(len(frames), 2)
            self.assertEqual(frames[0].i, 0)
            self.assertEqual(frames[0].render_index, 0)
            self.assertEqual(frames[1].i, 1)
            self.assertEqual(frames[1].render_index, 2)


class TestOutputOccupied(unittest.TestCase):
    def test_nonexistent_path_is_free(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "does_not_exist"
            self.assertFalse(pipeline._output_occupied(p))

    def test_empty_directory_is_free(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "empty_dir"
            p.mkdir()
            self.assertFalse(pipeline._output_occupied(p))

    def test_non_empty_directory_is_occupied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "non_empty_dir"
            p.mkdir()
            (p / "file.txt").write_text("data")
            self.assertTrue(pipeline._output_occupied(p))

    def test_file_is_occupied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "some_file.txt"
            p.write_text("data")
            self.assertTrue(pipeline._output_occupied(p))

    def test_symlink_is_occupied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target_dir"
            target.mkdir()
            symlink = Path(tmpdir) / "symlink_dir"
            os.symlink(target, symlink)
            self.assertTrue(pipeline._output_occupied(symlink))


class TestApplyHints(unittest.TestCase):
    def test_apply_hints_ui_and_actor_flags(self):
        grid = Grid(64, 64)  # s=16, gw=4, gh=4, N=16
        tiles = np.zeros(grid.N, dtype=desc.TILE_DTYPE)
        tiles["y_q4"] = 123
        tiles["co_h"] = 45
        tiles["flags"] = 0x01  # EDGE flag set

        original_tiles = tiles.copy()

        # Hint covering tile (0,0) with role ui [x, y, w, h] = [0, 0, 10, 10]
        # and hint covering tile (1,1) with role actor [x, y, w, h] = [16, 16, 10, 10]
        hints = [
            {"i": 0, "role": "ui", "bbox_px": [0, 0, 10, 10]},
            {"i": 0, "role": "actor", "bbox_px": [16, 16, 10, 10]},
        ]

        pipeline._apply_hints(tiles, grid, hints)

        # Tile (0,0) index in morton order: tx=0, ty=0
        tile_0_idx = int(grid.order[0, 0])
        # Tile (1,1) index: tx=1, ty=1
        tile_1_1_idx = int(grid.order[1, 1])

        # UI_OCC bit is 1 << 4 = 0x10, ACTOR_OCC bit is 1 << 5 = 0x20
        self.assertEqual(tiles["flags"][tile_0_idx], 0x01 | 0x10)
        self.assertEqual(tiles["flags"][tile_1_1_idx], 0x01 | 0x20)

        # Negative case: check that all fields besides flags in updated tiles are byte-identical
        for f in desc.TILE_DTYPE.names:
            if f != "flags":
                np.testing.assert_array_equal(tiles[f], original_tiles[f])

        # Negative case: check tiles outside hint bboxes are 100% byte-identical
        unaffected_mask = np.ones(grid.N, dtype=bool)
        unaffected_mask[tile_0_idx] = False
        unaffected_mask[tile_1_1_idx] = False
        np.testing.assert_array_equal(tiles[unaffected_mask], original_tiles[unaffected_mask])

    def test_apply_hints_negative_cases(self):
        grid = Grid(64, 64)
        tiles = np.zeros(grid.N, dtype=desc.TILE_DTYPE)
        tiles["flags"] = 0x02
        original_tiles = tiles.copy()

        # Unknown role, empty/negative bbox w or h
        hints = [
            {"i": 0, "role": "unknown_role", "bbox_px": [0, 0, 32, 32]},
            {"i": 0, "role": "ui", "bbox_px": [0, 0, 0, 10]},  # w <= 0
            {"i": 0, "role": "actor", "bbox_px": [0, 0, 10, -5]},  # h <= 0
        ]

        pipeline._apply_hints(tiles, grid, hints)

        # Negative assertion: tiles must be completely unchanged
        np.testing.assert_array_equal(tiles, original_tiles)


if __name__ == "__main__":
    unittest.main()
