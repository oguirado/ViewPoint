"""Legitimate path layout tests for B4 path-traversal fix in vadapt/godot/from_capture_dir.py.

VM timings: VM never MEASURED.

Validates that GodotAdapter accepts, with identical frame content and metadata,
all legitimate path layouts:
- manifest at capture/manifest.json and at manifest.json
- frames in nested subdirectories
- frame file names containing single dots and double dots inside a name (e.g., a..b.png)
- unicode frame file names
- capture root whose own absolute path goes through a symlinked parent directory
- repeated slashes inside relative path (e.g., capture//frames///f000000.png)
- exact error code (E_IO) for a missing frame.
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from vadapt.godot import GodotAdapter, convert
from vadapt.synth.pngenc import encode_png
from visioncore import frame as vframe
from visioncore.errors import VqaError


class TestB4GodotLegitPaths(unittest.TestCase):
    """VM timings: VM never MEASURED."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="b4_godot_legit_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Baseline tiny frame parameters
        self.W, self.H = 16, 16
        self.rgb0 = np.full((self.H, self.W, 3), 50, dtype=np.uint8)
        self.rgb0[:, :, 0] = np.arange(self.W, dtype=np.uint8)
        self.png0_bytes = encode_png(self.rgb0)
        self.png0_sha = hashlib.sha256(self.png0_bytes).hexdigest()
        self.decoded0_sha = hashlib.sha256(self.rgb0.tobytes()).hexdigest()

        self.rgb1 = np.full((self.H, self.W, 3), 120, dtype=np.uint8)
        self.rgb1[:, :, 1] = np.arange(self.W, dtype=np.uint8)
        self.png1_bytes = encode_png(self.rgb1)
        self.png1_sha = hashlib.sha256(self.png1_bytes).hexdigest()
        self.decoded1_sha = hashlib.sha256(self.rgb1.tobytes()).hexdigest()

    def _make_capture(
        self,
        base_dir,
        manifest_rel="capture/manifest.json",
        frame_paths=None,
        root_symlink_parent=False,
    ):
        """Creates a capture layout in base_dir and returns (run_root, manifest_path)."""
        base = Path(base_dir)
        if frame_paths is None:
            frame_paths = ["capture/frames/f000000.png", "capture/frames/f000001.png"]

        manifest_path = base / manifest_rel
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        frames_data = [
            (self.png0_bytes, self.png0_sha, self.decoded0_sha),
            (self.png1_bytes, self.png1_sha, self.decoded1_sha),
        ]

        frame_recs = []
        for i, rel_p in enumerate(frame_paths):
            png_bytes, file_sha, dec_sha = frames_data[i % len(frames_data)]
            f_abs = base / rel_p
            f_abs.parent.mkdir(parents=True, exist_ok=True)
            f_abs.write_bytes(png_bytes)

            frame_recs.append({
                "render_index": i * 2,
                "file": {"path": rel_p, "sha256": file_sha},
                "decoded_rgb_sha256": dec_sha,
            })

        cap = {
            "schema": "vqa.capture/v1",
            "adapter_id": "legit_app",
            "ruleset_id": "legit_scene",
            "fps": 30,
            "viewport_px": [self.W, self.H],
            "frames": frame_recs,
        }
        manifest_path.write_text(json.dumps(cap, ensure_ascii=False), encoding="utf-8")

        run_root = base
        if root_symlink_parent:
            # Create a symlinked parent directory pointing to base's parent
            sym_parent = self.tmp / "sym_parent_dir"
            sym_parent.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(base.parent, sym_parent)
            run_root = sym_parent / base.name

        return run_root, manifest_path

    def _assert_valid_capture_loaded(self, run_root):
        adapter = GodotAdapter()
        src = adapter.frames(run_root)
        self.assertEqual(len(src), 2)
        m = src.meta
        vframe.validate_manifest(m)
        self.assertEqual((m["width"], m["height"]), (16, 16))

        frames = list(src)
        self.assertEqual(len(frames), 2)
        self.assertTrue(np.array_equal(frames[0].rgb, self.rgb0))
        self.assertTrue(np.array_equal(frames[1].rgb, self.rgb1))
        self.assertEqual(frames[0].sha256, self.decoded0_sha)
        self.assertEqual(frames[1].sha256, self.decoded1_sha)

        # Verify convert() also works cleanly
        out_dir = self.tmp / "out_convert" / str(abs(hash(str(run_root))))
        res = convert(run_root, out_dir)
        self.assertEqual(res["rc"], 0)
        self.assertEqual(res["code"], "OK")
        out_manifest = vframe.read_manifest(out_dir / "frames.manifest.json")
        self.assertEqual(out_manifest["count"], 2)

        return src

    def test_manifest_at_capture_manifest_json(self):
        """Standard layout: manifest at capture/manifest.json."""
        run_root, _ = self._make_capture(
            self.tmp / "run_std",
            manifest_rel="capture/manifest.json",
            frame_paths=["capture/frames/f00.png", "capture/frames/f01.png"],
        )
        self._assert_valid_capture_loaded(run_root)

    def test_manifest_at_manifest_json(self):
        """Direct layout: manifest at manifest.json (run root is capture directory or direct layout)."""
        run_root, _ = self._make_capture(
            self.tmp / "run_direct",
            manifest_rel="manifest.json",
            frame_paths=["frames/f00.png", "frames/f01.png"],
        )
        self._assert_valid_capture_loaded(run_root)

    def test_nested_subdirectories(self):
        """Frames located in nested subdirectories inside capture directory."""
        run_root, _ = self._make_capture(
            self.tmp / "run_nested",
            manifest_rel="capture/manifest.json",
            frame_paths=[
                "capture/frames/sub1/sub2/f000000.png",
                "capture/frames/deep/nested/sub/f000001.png",
            ],
        )
        self._assert_valid_capture_loaded(run_root)

    def test_dots_inside_filenames(self):
        """File names containing single and double dots inside the name (e.g. a..b.png)."""
        run_root, _ = self._make_capture(
            self.tmp / "run_dots",
            manifest_rel="capture/manifest.json",
            frame_paths=[
                "capture/frames/frame..001.png",
                "capture/frames/sub.dir/a..b.png",
            ],
        )
        self._assert_valid_capture_loaded(run_root)

    def test_unicode_filenames(self):
        """File names with unicode characters (e.g., CJK, accents, Cyrillic, emoji)."""
        run_root, _ = self._make_capture(
            self.tmp / "run_unicode",
            manifest_rel="capture/manifest.json",
            frame_paths=[
                "capture/frames/帧_001.png",
                "capture/frames/främe_📸_02.png",
            ],
        )
        self._assert_valid_capture_loaded(run_root)

    def test_symlinked_parent_directory_in_capture_root(self):
        """Capture root whose absolute path goes through a symlinked parent directory."""
        real_base = self.tmp / "real_parent" / "run_sym"
        run_root, _ = self._make_capture(
            real_base,
            manifest_rel="capture/manifest.json",
            frame_paths=["capture/frames/f00.png", "capture/frames/f01.png"],
            root_symlink_parent=True,
        )
        self._assert_valid_capture_loaded(run_root)

    def test_repeated_slashes_in_relative_path(self):
        """Relative frame paths containing repeated slashes."""
        run_root, _ = self._make_capture(
            self.tmp / "run_slashes",
            manifest_rel="capture/manifest.json",
            frame_paths=[
                "capture//frames///f000000.png",
                "capture/frames//sub///f000001.png",
            ],
        )
        self._assert_valid_capture_loaded(run_root)

    def test_missing_frame_error_code(self):
        """Assert exact current error code (E_IO, rc=2) for missing frame."""
        base = self.tmp / "run_missing"
        run_root, mp = self._make_capture(
            base,
            manifest_rel="capture/manifest.json",
            frame_paths=["capture/frames/f00.png", "capture/frames/f01.png"],
        )
        # Delete second frame file
        f1_path = base / "capture" / "frames" / "f01.png"
        f1_path.unlink()

        adapter = GodotAdapter()
        with self.assertRaises(VqaError) as c:
            adapter.frames(run_root)

        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(c.exception.rc, 2)

    def test_identical_outputs_across_legitimate_layouts(self):
        """Assert identical frame source outputs across all legitimate layout variants."""
        layouts = [
            ("capture/manifest.json", ["capture/frames/f00.png", "capture/frames/f01.png"]),
            ("manifest.json", ["frames/f00.png", "frames/f01.png"]),
            ("capture/manifest.json", ["capture/frames/sub1/sub2/f0.png", "capture/frames/sub1/sub2/f1.png"]),
            ("capture/manifest.json", ["capture/frames/a..b.png", "capture/frames/c..d.png"]),
            ("capture/manifest.json", ["capture/frames/帧_00.png", "capture/frames/帧_01.png"]),
            ("capture/manifest.json", ["capture//frames///f0.png", "capture//frames///f1.png"]),
        ]

        sources = []
        for i, (m_rel, f_paths) in enumerate(layouts):
            run_root, _ = self._make_capture(
                self.tmp / f"run_variant_{i}",
                manifest_rel=m_rel,
                frame_paths=f_paths,
            )
            src = GodotAdapter().frames(run_root)
            sources.append(src)

        baseline_src = sources[0]
        baseline_frames = list(baseline_src)

        for idx, src in enumerate(sources[1:], 1):
            frames = list(src)
            self.assertEqual(len(frames), len(baseline_frames), f"Layout {idx} count mismatch")
            for k in range(len(baseline_frames)):
                self.assertTrue(
                    np.array_equal(frames[k].rgb, baseline_frames[k].rgb),
                    f"Layout {idx} frame {k} RGB mismatch",
                )
                self.assertEqual(
                    frames[k].sha256,
                    baseline_frames[k].sha256,
                    f"Layout {idx} frame {k} sha256 mismatch",
                )
                self.assertEqual(
                    frames[k].t_us,
                    baseline_frames[k].t_us,
                    f"Layout {idx} frame {k} t_us mismatch",
                )
                self.assertEqual(
                    frames[k].render_index,
                    baseline_frames[k].render_index,
                    f"Layout {idx} frame {k} render_index mismatch",
                )


if __name__ == "__main__":
    unittest.main()
