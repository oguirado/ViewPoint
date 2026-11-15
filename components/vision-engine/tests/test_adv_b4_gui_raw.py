"""Adversarial tests for B4 path traversal family in vadapt/gui/from_screens.py."""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from vadapt.gui import GuiAdapter, from_screens
from vadapt.synth.pngenc import encode_png
from visioncore.errors import VqaError


class TestAdvB4GuiRaw(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="adv_b4_"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)

        self.outside_dir = self.tmp_dir / "outside"
        self.outside_dir.mkdir(parents=True, exist_ok=True)

        self.run_dir = self.tmp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Sentinel RAW file (4x2 RGB24 = 24 bytes)
        self.sentinel_raw = b"SENTINEL_RAW_BYTES_OUTS!"[:24]
        self.outside_raw_file = self.outside_dir / "outside.raw"
        self.outside_raw_file.write_bytes(self.sentinel_raw)
        self.sentinel_raw_sha = hashlib.sha256(self.sentinel_raw).hexdigest()

        # Sentinel PNG file (10x10 RGB24)
        self.sentinel_rgb = np.full((10, 10, 3), 255, dtype=np.uint8)
        self.sentinel_png_bytes = encode_png(self.sentinel_rgb)
        self.outside_png_file = self.outside_dir / "outside.png"
        self.outside_png_file.write_bytes(self.sentinel_png_bytes)
        self.sentinel_png_sha = hashlib.sha256(self.sentinel_png_bytes).hexdigest()

    def _assert_sentinel_not_leaked(self, frames, shas):
        for f in frames:
            self.assertNotEqual(
                f.rgb.tobytes(),
                self.sentinel_raw,
                "Outside raw sentinel bytes leaked into frame pixels",
            )
            self.assertFalse(
                np.array_equal(f.rgb, self.sentinel_rgb),
                "Outside PNG sentinel pixels leaked into frame",
            )
        for sha in shas:
            self.assertNotEqual(
                sha,
                self.sentinel_raw_sha,
                "Outside raw sentinel SHA leaked into frame metadata",
            )
            self.assertNotEqual(
                sha,
                self.sentinel_png_sha,
                "Outside PNG sentinel SHA leaked into frame metadata",
            )

    @unittest.expectedFailure
    def test_symlink_file_outside_run(self):
        """B4 path traversal: symlink to file outside run."""
        symlink_path = self.run_dir / "symlink.raw"
        symlink_path.symlink_to(self.outside_raw_file)
        (self.run_dir / "raw.json").write_text(
            json.dumps({"width": 4, "height": 2, "file": "symlink.raw"}),
            encoding="utf-8",
        )

        try:
            frames, shas, W, H, files = from_screens._load_raw(self.run_dir)
            self._assert_sentinel_not_leaked(frames, shas)
            self.fail("Expected VqaError (E_IO or E_VC_FRAME_SCHEMA) when loading symlink to outside file")
        except VqaError as cm:
            self.assertIn(cm.code, ("E_IO", "E_VC_FRAME_SCHEMA"))

    @unittest.expectedFailure
    def test_symlink_subdir_outside_run(self):
        """B4 path traversal: symlink subdirectory pointing outside run."""
        sub_symlink = self.run_dir / "sub_symlink"
        sub_symlink.symlink_to(self.outside_dir)
        (self.run_dir / "raw.json").write_text(
            json.dumps({"width": 4, "height": 2, "file": "sub_symlink/outside.raw"}),
            encoding="utf-8",
        )

        try:
            frames, shas, W, H, files = from_screens._load_raw(self.run_dir)
            self._assert_sentinel_not_leaked(frames, shas)
            self.fail("Expected VqaError (E_IO or E_VC_FRAME_SCHEMA) when loading file inside symlinked subdirectory")
        except VqaError as cm:
            self.assertIn(cm.code, ("E_IO", "E_VC_FRAME_SCHEMA"))

    def test_nul_byte_in_name(self):
        """B4 path traversal defense: NUL byte in raw file name."""
        (self.run_dir / "raw.json").write_text(
            '{"width": 4, "height": 2, "file": "frames.raw\\u0000.png"}',
            encoding="utf-8",
        )
        with self.assertRaises(VqaError) as cm:
            from_screens._load_raw(self.run_dir)
        self.assertIn(cm.exception.code, ("E_IO", "E_VC_FRAME_SCHEMA"))

    def test_backslash_dotdot(self):
        """B4 path traversal defense: backslash dot dot traversal."""
        (self.run_dir / "raw.json").write_text(
            json.dumps({"width": 4, "height": 2, "file": "..\\outside.raw"}),
            encoding="utf-8",
        )
        with self.assertRaises(VqaError) as cm:
            from_screens._load_raw(self.run_dir)
        self.assertIn(cm.exception.code, ("E_IO", "E_VC_FRAME_SCHEMA"))

    def test_unicode_lookalike_dots(self):
        """B4 path traversal defense: Unicode lookalike dots."""
        (self.run_dir / "raw.json").write_text(
            json.dumps({"width": 4, "height": 2, "file": "\uff0e\uff0e/outside.raw"}),
            encoding="utf-8",
        )
        with self.assertRaises(VqaError) as cm:
            from_screens._load_raw(self.run_dir)
        self.assertIn(cm.exception.code, ("E_IO", "E_VC_FRAME_SCHEMA"))

    def test_name_equal_to_directory(self):
        """B4 path traversal defense: name equal to a directory."""
        (self.run_dir / "somedir").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "raw.json").write_text(
            json.dumps({"width": 4, "height": 2, "file": "somedir"}),
            encoding="utf-8",
        )
        with self.assertRaises(VqaError) as cm:
            from_screens._load_raw(self.run_dir)
        self.assertIn(cm.exception.code, ("E_IO", "E_VC_FRAME_SCHEMA"))

    @unittest.expectedFailure
    def test_fifo_name(self):
        """B4 path traversal: FIFO named pipe blocking/load."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo not supported on this platform")

        fifo_path = self.run_dir / "test.fifo"
        os.mkfifo(fifo_path)
        (self.run_dir / "raw.json").write_text(
            json.dumps({"width": 4, "height": 2, "file": "test.fifo"}),
            encoding="utf-8",
        )

        res = []

        def target():
            try:
                frames, shas, W, H, files = from_screens._load_raw(self.run_dir)
                res.append(("OK", frames, shas))
            except VqaError as e:
                res.append(("VqaError", e))
            except Exception as e:
                res.append(("EXC", e))

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=0.5)

        if t.is_alive():
            self.fail("Opening FIFO blocked indefinitely without raising VqaError")

        status, *args = res[0]
        if status == "VqaError":
            err = args[0]
            self.assertIn(err.code, ("E_IO", "E_VC_FRAME_SCHEMA"))
        elif status == "OK":
            frames, shas = args
            self._assert_sentinel_not_leaked(frames, shas)
            self.fail("FIFO was unexpectedly opened and loaded")
        else:
            self.fail("FIFO open raised unexpected exception: %r" % (args[0],))

    @unittest.expectedFailure
    def test_png_glob_symlink_outside(self):
        """B4 path traversal: PNG glob entry symlinked to outside PNG."""
        screens_dir = self.run_dir / "screens"
        screens_dir.mkdir(parents=True, exist_ok=True)
        png_symlink = screens_dir / "frame.png"
        png_symlink.symlink_to(self.outside_png_file)

        adapter = GuiAdapter()
        try:
            src = adapter.frames(self.run_dir)
            frames = list(src)
            shas = [f.sha256 for f in frames]
            self._assert_sentinel_not_leaked(frames, shas)
            self.fail("Expected VqaError (E_IO or E_VC_FRAME_SCHEMA) when loading PNG symlink to outside file")
        except VqaError as cm:
            self.assertIn(cm.code, ("E_IO", "E_VC_FRAME_SCHEMA"))


if __name__ == "__main__":
    unittest.main()
