"""Adversarial tests for B4 HIGH path traversal family in vadapt/godot/from_capture_dir.py.

Tests path traversal vulnerabilities in _resolve, _roots, frames, and hints against
both resolution bases (root and manifest_path.parent).
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from vadapt.godot import GodotAdapter
from vadapt.godot import from_capture_dir as fcd
from vadapt.synth.pngenc import encode_png
from visioncore.errors import VqaError


class TestAdvB4GodotResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adv_b4_godot_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Hierarchy for testing traversal depths 1 to 4 above run_dir:
        # self.tmp/
        #   d4/
        #     sentinel_d4.png
        #     d3/
        #       sentinel_d3.png
        #       d2/
        #         sentinel_d2.png
        #         d1/
        #           sentinel_d1.png
        #           sentinel_telemetry.jsonl
        #           run/
        #             capture/
        #               manifest.json
        #               frames/
        #                 f000000.png

        self.d4 = self.tmp / "d4"
        self.d3 = self.d4 / "d3"
        self.d2 = self.d3 / "d2"
        self.d1 = self.d2 / "d1"
        self.run_dir = self.d1 / "run"
        self.cap_dir = self.run_dir / "capture"
        self.frames_dir = self.cap_dir / "frames"
        self.frames_dir.mkdir(parents=True)

        # Sentinel file bytes and PNGs
        self.sentinel_bytes = b"SENTINEL_SECRET_DATA_ADV_B4_GODOT_9876543210"
        self.sentinel_sha = hashlib.sha256(self.sentinel_bytes).hexdigest()

        # Create PNG frame for sentinels
        self.sentinel_rgb = np.full((16, 16, 3), 200, dtype=np.uint8)
        self.sentinel_png_bytes = encode_png(self.sentinel_rgb)
        self.sentinel_png_sha = hashlib.sha256(self.sentinel_png_bytes).hexdigest()

        # Place sentinel files at depths 1 to 4
        self.sentinels = {}
        dirs = [self.d1, self.d2, self.d3, self.d4]
        for depth, parent_dir in enumerate(dirs, 1):
            s_file = parent_dir / f"sentinel_d{depth}.png"
            s_file.write_bytes(self.sentinel_png_bytes)
            self.sentinels[depth] = s_file

        # Absolute path sentinel file outside run root
        self.abs_sentinel = self.tmp / "abs_sentinel.png"
        self.abs_sentinel.write_bytes(self.sentinel_png_bytes)

        # Telemetry sentinel file outside run root
        self.tel_sentinel = self.d1 / "sentinel_telemetry.jsonl"
        tel_row = {
            "schema": "vqa.probe/v1",
            "render_index": 0,
            "viewport_px": [16, 16],
            "ui": [{"id": "SENTINEL_UI", "role": "hud", "visible": True, "rect_px_q16": [0, 0, 65536, 65536]}],
        }
        self.tel_sentinel.write_text(json.dumps(tel_row) + "\n", encoding="utf-8")

        # Create valid frame inside run_dir
        self.valid_rgb = np.full((16, 16, 3), 50, dtype=np.uint8)
        self.valid_png_bytes = encode_png(self.valid_rgb)
        self.valid_png_sha = hashlib.sha256(self.valid_png_bytes).hexdigest()
        (self.frames_dir / "f000000.png").write_bytes(self.valid_png_bytes)

        # Valid manifest inside capture dir
        self.mp = self.cap_dir / "manifest.json"
        self.manifest_data = {
            "schema": "vqa.capture/v1",
            "adapter_id": "test_adapter",
            "ruleset_id": "test_ruleset",
            "fps": 30,
            "viewport_px": [16, 16],
            "telemetry": {"path": "capture/telemetry.jsonl"},
            "frames": [
                {
                    "render_index": 0,
                    "file": {"path": "capture/frames/f000000.png", "sha256": self.valid_png_sha},
                    "decoded_rgb_sha256": hashlib.sha256(self.valid_rgb.tobytes()).hexdigest(),
                }
            ],
        }
        self.mp.write_text(json.dumps(self.manifest_data), encoding="utf-8")

        # Symlinks for testing
        self.sym_frame_file = self.frames_dir / "sym_frame.png"
        os.symlink(self.abs_sentinel, self.sym_frame_file)

        self.outside_frames_dir = self.tmp / "outside_frames"
        self.outside_frames_dir.mkdir()
        (self.outside_frames_dir / "out_frame.png").write_bytes(self.sentinel_png_bytes)
        self.sym_dir = self.cap_dir / "sym_dir"
        os.symlink(self.outside_frames_dir, self.sym_dir)

    def _assert_resolve_refuses_escape(self, rel_path):
        with self.assertRaises(VqaError) as c:
            p = fcd._resolve(self.run_dir, self.mp, rel_path)
            # If resolve returned a path, verify sentinel bytes were not accessed
            if p.is_file():
                content = p.read_bytes()
                self.assertNotIn(self.sentinel_bytes, content)
                self.assertNotIn(self.sentinel_png_bytes, content)
        self.assertEqual(c.exception.code, "E_IO")

    def _assert_frames_refuses_escape(self, rel_path):
        cap = dict(self.manifest_data)
        cap["frames"] = [
            {
                "render_index": 0,
                "file": {"path": rel_path, "sha256": self.sentinel_png_sha},
            }
        ]
        self.mp.write_text(json.dumps(cap), encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            src = GodotAdapter().frames(self.run_dir)
            frames_list = list(src)
            if frames_list:
                fr = frames_list[0]
                self.assertNotEqual(src.meta["frames"][0].get("file_sha256"), self.sentinel_png_sha)
                self.assertFalse(np.array_equal(fr.rgb, self.sentinel_rgb))
        self.assertEqual(c.exception.code, "E_IO")

    # 1. Dot dot depth 1 to 4 - Root base
    def test_dotdot_depth1_root_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 1 dot dot traversal escaping root."""
        self._assert_resolve_refuses_escape("../sentinel_d1.png")

    def test_dotdot_depth2_root_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 2 dot dot traversal escaping root."""
        self._assert_resolve_refuses_escape("../../sentinel_d2.png")

    def test_dotdot_depth3_root_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 3 dot dot traversal escaping root."""
        self._assert_resolve_refuses_escape("../../../sentinel_d3.png")

    def test_dotdot_depth4_root_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 4 dot dot traversal escaping root."""
        self._assert_resolve_refuses_escape("../../../../sentinel_d4.png")

    # 1. Dot dot depth 1 to 4 - Manifest parent base
    def test_dotdot_depth1_manifest_parent_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 1 escape via manifest parent base."""
        self._assert_resolve_refuses_escape("../../sentinel_d1.png")

    def test_dotdot_depth2_manifest_parent_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 2 escape via manifest parent base."""
        self._assert_resolve_refuses_escape("../../../sentinel_d2.png")

    def test_dotdot_depth3_manifest_parent_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 3 escape via manifest parent base."""
        self._assert_resolve_refuses_escape("../../../../sentinel_d3.png")

    def test_dotdot_depth4_manifest_parent_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows depth 4 escape via manifest parent base."""
        self._assert_resolve_refuses_escape("../../../../../sentinel_d4.png")

    # 1. Dot dot depth 1 to 4 - GodotAdapter.frames
    def test_dotdot_depth1_frames(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.frames reads frame outside run root via depth 1 dot dot."""
        self._assert_frames_refuses_escape("../sentinel_d1.png")

    def test_dotdot_depth2_frames(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.frames reads frame outside run root via depth 2 dot dot."""
        self._assert_frames_refuses_escape("../../sentinel_d2.png")

    def test_dotdot_depth3_frames(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.frames reads frame outside run root via depth 3 dot dot."""
        self._assert_frames_refuses_escape("../../../sentinel_d3.png")

    def test_dotdot_depth4_frames(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.frames reads frame outside run root via depth 4 dot dot."""
        self._assert_frames_refuses_escape("../../../../sentinel_d4.png")

    # 2. Absolute path
    def test_absolute_path_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve allows absolute path to file outside run root."""
        self._assert_resolve_refuses_escape(str(self.abs_sentinel))

    def test_absolute_path_frames(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.frames reads frame via absolute path outside run root."""
        self._assert_frames_refuses_escape(str(self.abs_sentinel))

    # 3. Symlinked frame file pointing outside
    def test_symlinked_frame_file_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve follows symlinked frame file pointing outside run root."""
        self._assert_resolve_refuses_escape("capture/frames/sym_frame.png")

    def test_symlinked_frame_file_frames(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.frames reads symlinked frame file pointing outside run root."""
        self._assert_frames_refuses_escape("capture/frames/sym_frame.png")

    # 4. Symlinked frames directory pointing outside
    def test_symlinked_frames_dir_resolve(self):
        """Exposes B4 HIGH path traversal bug family: _resolve follows symlinked frames directory pointing outside run root."""
        self._assert_resolve_refuses_escape("capture/sym_dir/out_frame.png")

    def test_symlinked_frames_dir_frames(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.frames reads frame in symlinked directory pointing outside run root."""
        self._assert_frames_refuses_escape("capture/sym_dir/out_frame.png")

    # 5. NUL byte in path (Guarantees that hold - plain passing)
    def test_nul_byte_in_path_resolve(self):
        self._assert_resolve_refuses_escape("capture/frames/f000000.png\x00extra")
        self._assert_resolve_refuses_escape("../sentinel_d1.png\x00.png")

    def test_nul_byte_in_path_frames(self):
        self._assert_frames_refuses_escape("capture/frames/f000000.png\x00extra")

    # 6. Unicode normalisation variants (Guarantees that hold - plain passing)
    def test_unicode_fullwidth_dot_resolve(self):
        self._assert_resolve_refuses_escape("．．/sentinel_d1.png")
        self._assert_resolve_refuses_escape("\uff0e\uff0e/sentinel_d1.png")

    def test_unicode_nfd_resolve(self):
        # Path with combining accents that does not match any file
        self._assert_resolve_refuses_escape("../caf\u0065\u0301/sentinel_d1.png")

    # 7. Case variants of run directory name (Guarantees that hold - plain passing)
    def test_case_variant_run_dir_resolve(self):
        self._assert_resolve_refuses_escape("../RUN/../sentinel_d1.png")
        self._assert_resolve_refuses_escape("../Run/../sentinel_d1.png")

    # 8. Backslash separators (Guarantees that hold - plain passing on POSIX)
    def test_backslash_separators_resolve(self):
        self._assert_resolve_refuses_escape("..\\sentinel_d1.png")
        self._assert_resolve_refuses_escape("capture\\frames\\..\\..\\sentinel_d1.png")

    # 9. Percent-encoded dot dot (Guarantees that hold - plain passing)
    def test_percent_encoded_dotdot_resolve(self):
        self._assert_resolve_refuses_escape("%2e%2e/sentinel_d1.png")
        self._assert_resolve_refuses_escape("capture/%2e%2e/sentinel_d1.png")

    # 10. Telemetry path in hints that escapes
    def test_telemetry_path_escape_hints(self):
        """Exposes B4 HIGH path traversal bug family: GodotAdapter.hints loads telemetry file escaping run root."""
        cap = dict(self.manifest_data)
        cap["telemetry"] = {"path": "../sentinel_telemetry.jsonl"}
        self.mp.write_text(json.dumps(cap), encoding="utf-8")

        with self.assertRaises(VqaError) as c:
            rows = list(GodotAdapter().hints(self.run_dir) or [])
            if any("SENTINEL" in r.get("rid", "") for r in rows):
                self.fail("hints read outside telemetry file")
        self.assertEqual(c.exception.code, "E_IO")

    # _roots path traversal
    def test_roots_nonexistent_escape(self):
        with self.assertRaises(VqaError) as c:
            fcd._roots(self.tmp / "nonexistent_run")
        self.assertEqual(c.exception.code, "E_IO")


if __name__ == "__main__":
    unittest.main()
