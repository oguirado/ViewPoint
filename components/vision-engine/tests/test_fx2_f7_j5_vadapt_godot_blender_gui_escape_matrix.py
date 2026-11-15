"""Test suite for vadapt Godot, Blender, and GUI escape matrix and safe-control controls.

Targeting independent Blender and GUI combinations of symlinked ancestors and selected
special leaves using empty filesystem nodes and tracked read, decode, and mapping seams
returning RAM pixels.

Demonstrably missing Blender or GUI contracts are annotated with @unittest.expectedFailure
naming vqa-g1-b4-blender-gui-fix.

Godot tests are limited to an ordinary safe-control composition.
"""

import hashlib
import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from vadapt.blender import BlenderAdapter, adapt
from vadapt.blender import from_render_dir as fbd
from vadapt.godot import GodotAdapter, convert
from vadapt.gui import GuiAdapter, from_screens
from visioncore import frame as vframe
from visioncore.decode import encode_png
from visioncore.errors import VqaError


def _ram_png(seed=1, W=16, H=16):
    """Generate deterministic RAM-backed PNG bytes without disk video files."""
    rgb = ((np.arange(W * H * 3, dtype=np.uint32).reshape(H, W, 3) * (seed + 5)) & 255).astype(np.uint8)
    return encode_png(rgb), rgb


def _run_in_isolated_child(func, args=(), kwargs=None, timeout=2.0):
    """Execute func(*args, **kwargs) in a bounded child process with guaranteed cleanup.

    Protects against potentially blocking FIFO operations.
    """
    if kwargs is None:
        kwargs = {}
    ctx = multiprocessing.get_context("fork") if "fork" in multiprocessing.get_all_start_methods() else multiprocessing.get_context()
    parent_conn, child_conn = ctx.Pipe()

    def target():
        try:
            res = func(*args, **kwargs)
            child_conn.send(("OK", res))
        except VqaError as e:
            child_conn.send(("VqaError", e.code, e.detail))
        except Exception as e:
            child_conn.send(("EXC", str(e)))

    p = ctx.Process(target=target)
    p.start()
    p.join(timeout=timeout)
    if p.is_alive():
        p.terminate()
        p.join()
        return ("BLOCKED", "E_IO", {"reason": "FIFO operation blocked indefinitely"})
    if parent_conn.poll():
        return parent_conn.recv()
    return ("NO_OUTPUT", "E_IO", {"reason": "Child exited without response"})


class TestFx2F7J5VadaptEscapeMatrix(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fx2_f7_j5_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.outside = self.tmp / "outside"
        self.outside.mkdir()

        self.sentinel_bytes = b"SENTINEL_SECRET_RAM_PIXELS_OUTSIDE_12345"
        self.sentinel_file = self.outside / "sentinel.raw"
        self.sentinel_file.write_bytes(self.sentinel_bytes)

        self.sentinel_png_bytes, self.sentinel_rgb = _ram_png(seed=999)
        self.sentinel_png = self.outside / "sentinel.png"
        self.sentinel_png.write_bytes(self.sentinel_png_bytes)

    # -------------------------------------------------------------------------
    # Godot Safe Control
    # -------------------------------------------------------------------------
    def test_godot_ordinary_safe_control(self):
        """Godot safe-control composition: ordinary capture layout passes without error."""
        run_dir = self.tmp / "godot_run"
        cap_dir = run_dir / "capture"
        frames_dir = cap_dir / "frames"
        frames_dir.mkdir(parents=True)

        png_bytes, rgb = _ram_png(seed=10)
        (frames_dir / "f000000.png").write_bytes(png_bytes)

        cap_manifest = {
            "schema": "vqa.capture/v1",
            "adapter_id": "godot_app",
            "ruleset_id": "godot_scene",
            "fps": 30,
            "viewport_px": [16, 16],
            "telemetry": {"path": "capture/telemetry.jsonl"},
            "frames": [
                {
                    "render_index": 0,
                    "file": {"path": "capture/frames/f000000.png", "sha256": hashlib.sha256(png_bytes).hexdigest()},
                    "decoded_rgb_sha256": hashlib.sha256(rgb.tobytes()).hexdigest(),
                }
            ],
        }
        (cap_dir / "manifest.json").write_text(json.dumps(cap_manifest), encoding="utf-8")

        adapter = GodotAdapter()
        src = adapter.frames(run_dir)
        self.assertEqual(len(src), 1)
        self.assertEqual(src.meta["scope"]["app"], "godot_app")
        self.assertEqual(src.meta["scope"]["scene"], "godot_scene")

    # -------------------------------------------------------------------------
    # Blender Controls & Escape Matrix
    # -------------------------------------------------------------------------
    def test_blender_ordinary_safe_control(self):
        """Blender safe-control: ordinary render directory with PNGs and metadata."""
        run_dir = self.tmp / "blender_run"
        run_dir.mkdir()
        png1, _ = _ram_png(seed=1)
        png2, _ = _ram_png(seed=2)
        (run_dir / "frame_0001.png").write_bytes(png1)
        (run_dir / "frame_0002.png").write_bytes(png2)
        (run_dir / "scene.json").write_text('{"app": "blender_test", "scene": "test_shot"}', encoding="utf-8")
        (run_dir / "bounds.jsonl").write_text(
            '{"frame": 1, "name": "cube", "role": "object", "bbox_px": [0, 0, 10, 10]}\n', encoding="utf-8"
        )

        adapter = BlenderAdapter()
        src = adapter.frames(run_dir)
        self.assertEqual(len(src), 2)
        hints = list(adapter.hints(run_dir) or [])
        self.assertEqual(len(hints), 1)

    def test_blender_inroot_hardlink_control(self):
        """Blender control: in-root regular hardlinks within run_dir are accepted."""
        run_dir = self.tmp / "blender_hardlink_run"
        run_dir.mkdir()
        png1, _ = _ram_png(seed=1)
        f1 = run_dir / "frame_0001.png"
        f2 = run_dir / "frame_0002.png"
        f1.write_bytes(png1)
        os.link(f1, f2)

        adapter = BlenderAdapter()
        src = adapter.frames(run_dir)
        self.assertEqual(len(src), 2)

    @unittest.expectedFailure
    def test_blender_symlinked_png_leaf_outside(self):
        """vqa-g1-b4-blender-gui-fix: Blender symlinked PNG leaf pointing outside must raise VqaError(E_IO)."""
        run_dir = self.tmp / "blender_sym_png"
        run_dir.mkdir()
        png1, _ = _ram_png(seed=1)
        (run_dir / "frame_0001.png").write_bytes(png1)
        (run_dir / "frame_0002.png").symlink_to(self.sentinel_png)

        with self.assertRaises(VqaError) as c:
            BlenderAdapter().frames(run_dir)
        self.assertEqual(c.exception.code, "E_IO")

    @unittest.expectedFailure
    def test_blender_symlinked_still_dir_outside(self):
        """vqa-g1-b4-blender-gui-fix: Blender symlinked still_probe directory pointing outside must raise VqaError(E_IO)."""
        run_dir = self.tmp / "blender_sym_still"
        run_dir.mkdir()
        png1, _ = _ram_png(seed=1)
        (run_dir / "frame_0001.png").write_bytes(png1)

        outside_still = self.outside / "still"
        outside_still.mkdir()
        (outside_still / "frame_0001.png").write_bytes(self.sentinel_png_bytes)
        (run_dir / "still").symlink_to(outside_still)

        with self.assertRaises(VqaError) as c:
            BlenderAdapter().still_probe(run_dir)
        self.assertEqual(c.exception.code, "E_IO")

    @unittest.expectedFailure
    def test_blender_symlinked_scene_json_outside(self):
        """vqa-g1-b4-blender-gui-fix: Blender symlinked scene.json pointing outside must raise VqaError(E_IO)."""
        run_dir = self.tmp / "blender_sym_scene"
        run_dir.mkdir()
        png1, _ = _ram_png(seed=1)
        (run_dir / "frame_0001.png").write_bytes(png1)

        outside_json = self.outside / "secret_scene.json"
        outside_json.write_text('{"app": "SECRET_LEAK"}', encoding="utf-8")
        (run_dir / "scene.json").symlink_to(outside_json)

        with self.assertRaises(VqaError) as c:
            fbd._scene_meta(run_dir)
        self.assertEqual(c.exception.code, "E_IO")

    @unittest.expectedFailure
    def test_blender_symlinked_bounds_jsonl_outside(self):
        """vqa-g1-b4-blender-gui-fix: Blender symlinked bounds.jsonl pointing outside must raise VqaError(E_IO)."""
        run_dir = self.tmp / "blender_sym_bounds"
        run_dir.mkdir()
        png1, _ = _ram_png(seed=1)
        (run_dir / "frame_0001.png").write_bytes(png1)

        outside_jsonl = self.outside / "secret_bounds.jsonl"
        outside_jsonl.write_text('{"frame": 1, "name": "secret", "role": "object", "bbox_px": [0,0,1,1]}\n', encoding="utf-8")
        (run_dir / "bounds.jsonl").symlink_to(outside_jsonl)

        with self.assertRaises(VqaError) as c:
            fbd._hint_rows(run_dir, {1: 0})
        self.assertEqual(c.exception.code, "E_IO")

    @unittest.expectedFailure
    def test_blender_fifo_special_leaf(self):
        """vqa-g1-b4-blender-gui-fix: Blender FIFO special leaf frame must raise VqaError(E_IO) without blocking."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available")

        run_dir = self.tmp / "blender_fifo"
        run_dir.mkdir()
        fifo_path = run_dir / "frame_0001.png"
        os.mkfifo(fifo_path)

        res = _run_in_isolated_child(BlenderAdapter().frames, args=(run_dir,))
        status, code, _ = res
        self.assertEqual(status, "VqaError")
        self.assertEqual(code, "E_IO")

    def test_blender_malformed_metadata_diagnostics_preserved(self):
        """Blender malformed metadata diagnostics preserved: invalid bounds.jsonl raises schema error."""
        run_dir = self.tmp / "blender_bad_bounds"
        run_dir.mkdir()
        png1, _ = _ram_png(seed=1)
        (run_dir / "frame_0001.png").write_bytes(png1)
        (run_dir / "bounds.jsonl").write_text("invalid json line\n", encoding="utf-8")

        with self.assertRaises(VqaError) as c:
            BlenderAdapter().hints(run_dir)
        self.assertIn(c.exception.code, ("E_SCHEMA", "E_VC_FRAME_SCHEMA"))

    # -------------------------------------------------------------------------
    # GUI Controls & Escape Matrix
    # -------------------------------------------------------------------------
    def test_gui_ordinary_screens_safe_control(self):
        """GUI safe-control: ordinary screens directory layout passes cleanly."""
        run_dir = self.tmp / "gui_screens_run"
        sd = run_dir / "screens"
        sd.mkdir(parents=True)
        png1, _ = _ram_png(seed=1)
        (sd / "001.png").write_bytes(png1)
        (run_dir / "app.json").write_text('{"app": "gui_app", "scene": "session"}', encoding="utf-8")

        adapter = GuiAdapter()
        src = adapter.frames(run_dir)
        self.assertEqual(len(src), 1)
        self.assertEqual(src.meta["scope"]["app"], "gui_app")

    def test_gui_ordinary_raw_safe_control(self):
        """GUI safe-control: ordinary raw.json layout passes cleanly."""
        run_dir = self.tmp / "gui_raw_run"
        run_dir.mkdir()
        raw_bytes = b"\x00" * (16 * 16 * 3)
        (run_dir / "frames.raw").write_bytes(raw_bytes)
        (run_dir / "raw.json").write_text(json.dumps({"width": 16, "height": 16, "file": "frames.raw"}), encoding="utf-8")

        adapter = GuiAdapter()
        src = adapter.frames(run_dir)
        self.assertEqual(len(src), 1)

    def test_gui_inroot_hardlink_control(self):
        """GUI control: regular in-root hardlinks within screens/ directory are accepted."""
        run_dir = self.tmp / "gui_hardlink_run"
        sd = run_dir / "screens"
        sd.mkdir(parents=True)
        png1, _ = _ram_png(seed=1)
        f1 = sd / "001.png"
        f2 = sd / "002.png"
        f1.write_bytes(png1)
        os.link(f1, f2)

        adapter = GuiAdapter()
        src = adapter.frames(run_dir)
        self.assertEqual(len(src), 2)

    @unittest.expectedFailure
    def test_gui_screens_symlinked_png_outside(self):
        """vqa-g1-b4-blender-gui-fix: GUI screens symlinked PNG pointing outside must raise VqaError(E_IO)."""
        run_dir = self.tmp / "gui_sym_png"
        sd = run_dir / "screens"
        sd.mkdir(parents=True)
        png1, _ = _ram_png(seed=1)
        (sd / "001.png").write_bytes(png1)
        (sd / "002.png").symlink_to(self.sentinel_png)

        with self.assertRaises(VqaError) as c:
            GuiAdapter().frames(run_dir)
        self.assertEqual(c.exception.code, "E_IO")

    @unittest.expectedFailure
    def test_gui_raw_symlinked_file_outside(self):
        """vqa-g1-b4-blender-gui-fix: GUI raw.json referencing symlinked file pointing outside must raise VqaError(E_IO)."""
        run_dir = self.tmp / "gui_sym_raw"
        run_dir.mkdir()
        sym_raw = run_dir / "sym.raw"
        sym_raw.symlink_to(self.sentinel_file)
        (run_dir / "raw.json").write_text(json.dumps({"width": 16, "height": 16, "file": "sym.raw"}), encoding="utf-8")

        with self.assertRaises(VqaError) as c:
            from_screens._load_raw(run_dir)
        self.assertEqual(c.exception.code, "E_IO")

    @unittest.expectedFailure
    def test_gui_raw_fifo_special_leaf(self):
        """vqa-g1-b4-blender-gui-fix: GUI raw FIFO special leaf must raise VqaError(E_IO) without blocking."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available")

        run_dir = self.tmp / "gui_fifo_raw"
        run_dir.mkdir()
        fifo_path = run_dir / "test.fifo"
        os.mkfifo(fifo_path)
        (run_dir / "raw.json").write_text(json.dumps({"width": 16, "height": 16, "file": "test.fifo"}), encoding="utf-8")

        res = _run_in_isolated_child(from_screens._load_raw, args=(run_dir,))
        status, code, _ = res
        self.assertEqual(status, "VqaError")
        self.assertEqual(code, "E_IO")

    def test_gui_malformed_metadata_diagnostics_preserved(self):
        """GUI malformed metadata diagnostics preserved: invalid raw.json width raises E_VC_FRAME_SCHEMA."""
        run_dir = self.tmp / "gui_bad_raw"
        run_dir.mkdir()
        (run_dir / "raw.json").write_text('{"width": "invalid", "height": 16}', encoding="utf-8")

        with self.assertRaises(VqaError) as c:
            from_screens._load_raw(run_dir)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")


if __name__ == "__main__":
    unittest.main()
