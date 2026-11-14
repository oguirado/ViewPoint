"""Adversarial tests targeting GodotAdapter.still_probe and manifest-relative B4 path containment.

Coverage extends GodotAdapter.still_probe (both still_probe and probe directories) and
manifest-relative resolution (_resolve / frames).
Uses verified /dev/shm RAM storage (tmpfs/ramfs), empty filesystem nodes, inert sentinels,
and tracked RAM-returning read_png seams without writing frame files to disk.
VM timings only.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from vadapt.godot import GodotAdapter
from vadapt.godot import from_capture_dir as fcd
from visioncore.errors import VqaError


def _is_verified_ram_storage(path="/dev/shm"):
    if not os.path.exists(path) or not os.access(path, os.W_OK):
        return False
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == path:
                    if parts[2] in ("tmpfs", "ramfs"):
                        return True
    except Exception:
        pass
    return False


def _run_bounded(fn, timeout_s=1.0):
    outcome = {}

    def _target():
        try:
            outcome["result"] = fn()
        except BaseException as exc:
            outcome["exc"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise AssertionError("Operation blocked beyond bounded timeout")
    if "exc" in outcome:
        raise outcome["exc"]
    return outcome.get("result")


class TestGodotStillProbeEscape(unittest.TestCase):
    def setUp(self):
        if not _is_verified_ram_storage("/dev/shm"):
            self.skipTest("Verified /dev/shm RAM storage unavailable")

        # Create isolated temporary directory under verified /dev/shm
        self.tmp_dir = tempfile.TemporaryDirectory(dir="/dev/shm", prefix="adv_b4_godot_probe_")
        self.addCleanup(self.tmp_dir.cleanup)
        self.root = Path(self.tmp_dir.name)

        # Sibling directories: run and outside
        self.run_dir = self.root / "run"
        self.outside_dir = self.root / "outside"
        self.run_dir.mkdir()
        self.outside_dir.mkdir()

        # Deterministic 16x16 RGB arrays in RAM only (never encoded or written to disk)
        self.valid_rgb = np.full((16, 16, 3), 120, dtype=np.uint8)
        self.sentinel_rgb = np.full((16, 16, 3), 220, dtype=np.uint8)

        # Inert empty sentinel filesystem node outside run_dir
        self.sentinel_file = self.outside_dir / "0001.png"
        self.sentinel_file.touch()

        # Tracked RAM-returning read_png seam
        self.read_calls = []
        self.rgb_by_path = {}
        outside_real = os.path.realpath(self.outside_dir)

        def _tracked_read_png(path):
            p = Path(path)
            self.read_calls.append(p)
            if p in self.rgb_by_path:
                return self.rgb_by_path[p].copy()
            p_real = os.path.realpath(p)
            try:
                if os.path.commonpath([outside_real, p_real]) == outside_real:
                    return self.sentinel_rgb.copy()
            except ValueError:
                pass
            return self.valid_rgb.copy()

        patcher = mock.patch.object(fcd, "read_png", side_effect=_tracked_read_png)
        self.mock_read_png = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # Verify outside inert sentinel node remains empty and untouched
        if hasattr(self, "sentinel_file") and self.sentinel_file.exists():
            self.assertEqual(os.lstat(self.sentinel_file).st_size, 0)
            self.assertEqual(self.sentinel_file.read_bytes(), b"")

    def test_still_probe_normal_local_control(self):
        """Passing control test: normal local still_probe empty node inside run_dir via RAM read_png seam."""
        probe_dir = self.run_dir / "still_probe"
        probe_dir.mkdir()
        frame_file = probe_dir / "0001.png"
        frame_file.touch()

        src = GodotAdapter().still_probe(self.run_dir)
        self.assertIsNotNone(src)
        frames_list = list(src)
        self.assertEqual(len(src), 1)
        self.assertEqual(len(frames_list), 1)
        fr = frames_list[0]
        self.assertEqual(fr.rgb.shape, (16, 16, 3))
        self.assertTrue(np.array_equal(fr.rgb, self.valid_rgb))
        self.assertEqual(self.read_calls, [frame_file])

    def test_b4_godot_probe_still_probe_dir_symlink(self):
        """B4-Godot-Probe: still_probe directory symlink to outside directory must raise E_IO."""
        sym_dir = self.run_dir / "still_probe"
        os.symlink(self.outside_dir, sym_dir)

        with self.assertRaises(VqaError) as c:
            src = GodotAdapter().still_probe(self.run_dir)
            if src is not None:
                self.fail("B4-Godot-Probe: vulnerable decoded output produced instead of refusal")
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

    def test_b4_godot_probe_still_probe_file_symlink(self):
        """B4-Godot-Probe: still_probe/0001.png file symlink to outside sentinel must raise E_IO."""
        probe_dir = self.run_dir / "still_probe"
        probe_dir.mkdir()
        sym_file = probe_dir / "0001.png"
        os.symlink(self.sentinel_file, sym_file)

        with self.assertRaises(VqaError) as c:
            src = GodotAdapter().still_probe(self.run_dir)
            if src is not None:
                self.fail("B4-Godot-Probe: vulnerable decoded output produced instead of refusal")
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

    def test_b4_godot_probe_probe_dir_symlink(self):
        """B4-Godot-Probe: probe directory symlink to outside directory must raise E_IO."""
        sym_dir = self.run_dir / "probe"
        os.symlink(self.outside_dir, sym_dir)

        with self.assertRaises(VqaError) as c:
            src = GodotAdapter().still_probe(self.run_dir)
            if src is not None:
                self.fail("B4-Godot-Probe: vulnerable decoded output produced instead of refusal")
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

    def test_b4_godot_probe_probe_file_symlink(self):
        """B4-Godot-Probe: probe/0001.png file symlink to outside sentinel must raise E_IO."""
        probe_dir = self.run_dir / "probe"
        probe_dir.mkdir()
        sym_file = probe_dir / "0001.png"
        os.symlink(self.sentinel_file, sym_file)

        with self.assertRaises(VqaError) as c:
            src = GodotAdapter().still_probe(self.run_dir)
            if src is not None:
                self.fail("B4-Godot-Probe: vulnerable decoded output produced instead of refusal")
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

    def test_b4_godot_probe_escaping_ancestor_controls(self):
        """Independent ancestor controls: symlinked or dotdot-escaping ancestors must raise E_IO before discovery."""
        outside_nested_probe = self.outside_dir / "nested" / "still_probe"
        outside_nested_probe.mkdir(parents=True)
        (outside_nested_probe / "0001.png").touch()

        sym_anc = self.run_dir / "sym_anc"
        os.symlink(self.outside_dir, sym_anc)

        for target in (sym_anc, sym_anc / "nested", Path(os.fspath(self.run_dir) + "/../outside/nested")):
            with self.subTest(target=str(target)):
                with self.assertRaises(VqaError) as c:
                    GodotAdapter().still_probe(target)
                self.assertEqual(c.exception.code, "E_IO")
                self.assertEqual(self.read_calls, [])

        # Manifest-relative _resolve with escaping or symlinked ancestors
        (self.outside_dir / "nested" / "manifest.json").touch()
        for bad_mp in (
            sym_anc / "nested" / "manifest.json",
            self.outside_dir / "nested" / "manifest.json",
            Path(os.fspath(self.run_dir) + "/../outside/nested/manifest.json"),
        ):
            with self.subTest(manifest_path=str(bad_mp)):
                with self.assertRaises(VqaError) as c:
                    fcd._resolve(self.run_dir, bad_mp, "still_probe/0001.png")
                self.assertEqual(c.exception.code, "E_IO")
                self.assertEqual(self.read_calls, [])

    def test_b4_godot_probe_special_leaf_controls(self):
        """Independent special-leaf controls: directory named *.png or non-directory probe node must raise E_IO."""
        probe_dir = self.run_dir / "still_probe"
        (probe_dir / "0001.png").mkdir(parents=True)

        with self.assertRaises(VqaError) as c:
            GodotAdapter().still_probe(self.run_dir)
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

        # _resolve with directory leaf named *.png
        cap_dir = self.run_dir / "capture"
        cap_dir.mkdir(parents=True, exist_ok=True)
        mp = cap_dir / "manifest.json"
        mp.touch()
        with self.assertRaises(VqaError) as c2:
            fcd._resolve(self.run_dir, mp, "still_probe/0001.png")
        self.assertEqual(c2.exception.code, "E_IO")

        # Non-directory still_probe node must refuse and never fall back to valid probe/
        (probe_dir / "0001.png").rmdir()
        probe_dir.rmdir()
        probe_dir.touch()
        fallback_dir = self.run_dir / "probe"
        fallback_dir.mkdir()
        (fallback_dir / "0001.png").touch()

        with self.assertRaises(VqaError) as c3:
            GodotAdapter().still_probe(self.run_dir)
        self.assertEqual(c3.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

    def test_b4_godot_probe_bounded_fifo_controls(self):
        """Bounded FIFO controls: nonregular FIFO selected frame or probe node rejected with E_IO without blocking."""
        probe_dir = self.run_dir / "still_probe"
        probe_dir.mkdir()
        fifo_leaf = probe_dir / "0001.png"
        os.mkfifo(fifo_leaf)

        with self.assertRaises(VqaError) as c:
            _run_bounded(lambda: GodotAdapter().still_probe(self.run_dir), timeout_s=1.0)
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

        # Manifest-relative _resolve with FIFO at root/rel must refuse and never fall back to manifest_path.parent/rel
        fifo_leaf.unlink()
        probe_dir.rmdir()
        (self.run_dir / "frames").mkdir()
        os.mkfifo(self.run_dir / "frames" / "0001.png")
        cap_frames = self.run_dir / "capture" / "frames"
        cap_frames.mkdir(parents=True)
        (cap_frames / "0001.png").touch()
        mp = self.run_dir / "capture" / "manifest.json"
        mp.touch()

        with self.assertRaises(VqaError) as c2:
            _run_bounded(lambda: fcd._resolve(self.run_dir, mp, "frames/0001.png"), timeout_s=1.0)
        self.assertEqual(c2.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

    def test_b4_godot_probe_pre_read_rejection_and_no_fallback_controls(self):
        """Pre-read rejection controls: reject before reading/hashing/decoding any frame and never fall back."""
        # 1. Safe 0001.png followed by unsafe 0002.png in still_probe -> zero read_png calls
        probe_dir = self.run_dir / "still_probe"
        probe_dir.mkdir()
        (probe_dir / "0001.png").touch()
        os.symlink(self.sentinel_file, probe_dir / "0002.png")

        with self.assertRaises(VqaError) as c:
            GodotAdapter().still_probe(self.run_dir)
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

        # 2. Unsafe still_probe symlink to empty outside dir with valid fallback probe/0001.png -> no fallback
        (probe_dir / "0002.png").unlink()
        (probe_dir / "0001.png").unlink()
        probe_dir.rmdir()
        empty_outside = self.outside_dir / "empty_probe"
        empty_outside.mkdir()
        os.symlink(empty_outside, probe_dir)
        valid_fallback = self.run_dir / "probe"
        valid_fallback.mkdir()
        (valid_fallback / "0001.png").touch()

        with self.assertRaises(VqaError) as c2:
            GodotAdapter().still_probe(self.run_dir)
        self.assertEqual(c2.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

        # 3. Manifest-relative still_probe spec escaping root -> E_IO before reading
        probe_dir.unlink()
        cap_dir = self.run_dir / "capture"
        cap_dir.mkdir(parents=True, exist_ok=True)
        mp = cap_dir / "manifest.json"
        mp.write_text(json.dumps({"still_probe": "../outside/0001.png"}), encoding="utf-8")
        with self.assertRaises(VqaError) as c_sp:
            GodotAdapter().still_probe(self.run_dir)
        self.assertEqual(c_sp.exception.code, "E_IO")
        self.assertEqual(self.read_calls, [])

        # 4. Manifest-relative frames() rejects second unsafe frame before hashing or decoding first valid frame
        cap_frames = cap_dir / "frames"
        cap_frames.mkdir(parents=True, exist_ok=True)
        (cap_frames / "0001.png").touch()
        os.symlink(self.sentinel_file, cap_frames / "0002.png")
        mp.write_text(
            json.dumps(
                {
                    "schema": "vqa.capture/v1",
                    "adapter_id": "godot",
                    "ruleset_id": "probe_test",
                    "fps": 30,
                    "viewport_px": [16, 16],
                    "frames": [
                        {"render_index": 0, "file": {"path": "capture/frames/0001.png"}},
                        {"render_index": 1, "file": {"path": "capture/frames/0002.png"}},
                    ],
                }
            ),
            encoding="utf-8",
        )
        sha_calls = []
        orig_sha_file = fcd._sha_file
        with mock.patch.object(fcd, "_sha_file", side_effect=lambda p: (sha_calls.append(Path(p)), orig_sha_file(p))[1]):
            with self.assertRaises(VqaError) as c3:
                GodotAdapter().frames(self.run_dir)
        self.assertEqual(c3.exception.code, "E_IO")
        self.assertEqual(sha_calls, [])
        self.assertEqual(self.read_calls, [])

    def test_still_probe_ordering_metadata_and_accepted_in_root_hardlink(self):
        """Safe ordering, metadata and public adapter behavior preserved, including accepted in-root regular hardlinks."""
        probe_dir = self.run_dir / "still_probe"
        probe_dir.mkdir()
        f2 = probe_dir / "0002.png"
        f2.touch()
        f1 = probe_dir / "0001.png"
        os.link(f2, f1)

        rgb1 = np.full((16, 16, 3), 40, dtype=np.uint8)
        rgb2 = np.full((16, 16, 3), 80, dtype=np.uint8)
        self.rgb_by_path[f1] = rgb1
        self.rgb_by_path[f2] = rgb2

        src = GodotAdapter().still_probe(self.run_dir)
        self.assertIsNotNone(src)
        self.assertEqual(self.read_calls, [f1, f2])
        self.assertEqual(
            src.meta,
            {
                "schema": "vqa.frame/v1",
                "width": 16,
                "height": 16,
                "channels": 3,
                "pixel_format": "rgb24",
                "storage": "png_dir",
                "count": 2,
            },
        )
        frames_list = list(src)
        self.assertEqual(len(frames_list), 2)
        self.assertEqual((frames_list[0].i, frames_list[0].t_us, frames_list[0].render_index), (0, 0, None))
        self.assertEqual((frames_list[1].i, frames_list[1].t_us, frames_list[1].render_index), (1, 33333, None))
        self.assertTrue(np.array_equal(frames_list[0].rgb, rgb1))
        self.assertTrue(np.array_equal(frames_list[1].rgb, rgb2))

        # In-root regular hardlink also accepted by _resolve
        cap_dir = self.run_dir / "capture"
        cap_dir.mkdir()
        mp = cap_dir / "manifest.json"
        mp.touch()
        self.assertEqual(fcd._resolve(self.run_dir, mp, "still_probe/0001.png"), f1)


if __name__ == "__main__":
    unittest.main()
