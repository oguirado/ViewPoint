"""Adversarial tests for B4 path traversal family in vadapt/blender/from_render_dir.py.

Tests cover:
  - _list_pngs
  - _scene_meta
  - _hint_rows
  - still_probe
  - adapt
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from vadapt.blender import BlenderAdapter, adapt
from vadapt.blender import from_render_dir as fbd
from visioncore import decode
from visioncore.errors import VqaError


def _img(seed=1, W=32, H=32):
    """Generate in-RAM frame buffer from fixed seed."""
    return ((np.arange(W * H * 3, dtype=np.uint32).reshape(H, W, 3) * (seed + 7)) & 255).astype(np.uint8)


class AdvB4BlenderDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.outside = self.root / "outside"
        self.outside.mkdir()

        # Sentinel files outside the run directory
        self.sentinel_png = self.outside / "sentinel_frame.png"
        self.sentinel_png_bytes = decode.encode_png(_img(seed=999))
        self.sentinel_png.write_bytes(self.sentinel_png_bytes)

        self.sentinel_json = self.outside / "sentinel_scene.json"
        self.sentinel_json.write_text('{"app": "SENTINEL_LEAK", "scene": "SECRET"}', encoding="utf-8")

        self.sentinel_jsonl = self.outside / "sentinel_bounds.jsonl"
        self.sentinel_jsonl.write_text(
            '{"frame": 1, "name": "sentinel_obj", "role": "object", "bbox_px": [0, 0, 10, 10]}\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _mk_frame(self, d, name="frame_0001.png", seed=1):
        p = Path(d) / name
        p.write_bytes(decode.encode_png(_img(seed=seed)))
        return p

    def test_symlinked_png_refused(self):
        """Reject a PNG symlink that points outside the run directory."""
        run_dir = self.root / "run_sym_png"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_0001.png", seed=1)
        (run_dir / "frame_0002.png").symlink_to(self.sentinel_png)

        with self.assertRaises(VqaError):
            src = BlenderAdapter().frames(run_dir)
            for frame in src:
                self.assertNotIn("SENTINEL", str(frame))

    def test_symlinked_still_dir_refused(self):
        """Reject a still directory symlink that points outside the run directory."""
        run_dir = self.root / "run_sym_still"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_0001.png", seed=1)

        outside_still = self.outside / "still_secret"
        outside_still.mkdir()
        self._mk_frame(outside_still, "frame_0001.png", seed=888)
        (run_dir / "still").symlink_to(outside_still)

        with self.assertRaises(VqaError):
            BlenderAdapter().still_probe(run_dir)

    def test_symlinked_scene_json_refused(self):
        """Reject a scene.json symlink that points outside the run directory."""
        run_dir = self.root / "run_sym_scene"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_0001.png", seed=1)
        (run_dir / "scene.json").symlink_to(self.sentinel_json)

        with self.assertRaises(VqaError):
            fbd._scene_meta(run_dir)

    def test_symlinked_bounds_jsonl_refused(self):
        """Reject a bounds.jsonl symlink that points outside the run directory."""
        run_dir = self.root / "run_sym_bounds"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_0001.png", seed=1)
        (run_dir / "bounds.jsonl").symlink_to(self.sentinel_jsonl)

        with self.assertRaises(VqaError):
            fbd._hint_rows(run_dir, {1: 0})

    @unittest.expectedFailure
    def test_dir_named_like_png_refused(self):
        """B4 directory traversal: directory named like a PNG is silently ignored instead of raising VqaError."""
        run_dir = self.root / "run_dir_png"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_0001.png", seed=1)
        (run_dir / "frame_0002.png").mkdir()

        with self.assertRaises(VqaError):
            fbd._list_pngs(run_dir)

    @unittest.expectedFailure
    def test_case_variant_png_extension_refused(self):
        """B4 filename canonicalisation: case variants of .png extension are accepted instead of raising VqaError."""
        run_dir = self.root / "run_case_ext"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_0001.PNG", seed=1)

        with self.assertRaises(VqaError):
            fbd._list_pngs(run_dir)

    @unittest.expectedFailure
    def test_unicode_digits_frame_number_refused(self):
        """B4 digit parsing: unicode digits in trailing frame number accepted instead of raising VqaError."""
        run_dir = self.root / "run_unicode_digit"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_\u0661.png", seed=1)

        with self.assertRaises(VqaError):
            fbd._list_pngs(run_dir)

    def test_duplicate_frame_unicode_norm_refused(self):
        """Duplicate frame numbers differing only by unicode normalisation trigger VqaError."""
        run_dir = self.root / "run_dup_norm"
        run_dir.mkdir()
        self._mk_frame(run_dir, "frame_0001.png", seed=1)
        self._mk_frame(run_dir, "frame_\u0661.png", seed=2)

        with self.assertRaises(VqaError) as c:
            fbd._list_pngs(run_dir)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")


if __name__ == "__main__":
    unittest.main()
