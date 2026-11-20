# SPDX-License-Identifier: AGPL-3.0-only

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from demo.synth_gui.media import run_media_tool


class MediaGuards(unittest.TestCase):
    def test_inner_proc_cannot_witness_outer_namespace(self):
        with patch.dict(os.environ, AF_PROC_PIDNS_PROC="/proc"):
            with patch("demo.synth_gui.media.subprocess.run") as launch:
                with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                    run_media_tool("ffprobe", ["-version"], cwd=Path.cwd())
                launch.assert_not_called()

    def test_media_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_media_tool("ffprobe", ["-version"], cwd=Path.cwd(), timeout=0.000001)

    def test_override_preserves_deadline(self):
        for timeout in (0, 61, None):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout"):
                    run_media_tool("ffprobe", ["-version"], cwd=Path.cwd(), timeout=timeout)
