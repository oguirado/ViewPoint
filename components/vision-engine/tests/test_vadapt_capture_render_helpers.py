"""Unit tests for private helpers of Godot and Blender vision adapters.

Godot: vadapt/godot/from_capture_dir.py
  - _load_json
  - _slug
  - _roots
  - _resolve
  - _rect_px
  - _rows_for

Blender: vadapt/blender/from_render_dir.py
  - _slug
  - _read_json
  - _list_pngs
  - _scene_meta
  - _hint_rows
"""
import tempfile
import unittest
from pathlib import Path

from vadapt.blender import from_render_dir as fbd
from vadapt.godot import from_capture_dir as fcd
from visioncore.errors import VqaError

Q = 65536  # 1 << 16 for px_q16 coordinates


class TestGodotHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # 1. _load_json
    def test_load_json_positive(self):
        p = self.root / "valid.json"
        p.write_text('{"fps": 30, "items": [1, 2, 3]}', encoding="utf-8")
        data = fcd._load_json(p)
        self.assertEqual(data, {"fps": 30, "items": [1, 2, 3]})

    def test_load_json_negative_missing_file(self):
        p = self.root / "nonexistent.json"
        with self.assertRaises(VqaError) as c:
            fcd._load_json(p)
        self.assertEqual(c.exception.code, "E_IO")
        self.assertEqual(c.exception.rc, 2)

    def test_load_json_negative_invalid_syntax(self):
        p = self.root / "bad.json"
        p.write_text("{bad json: 123", encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            fcd._load_json(p)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")
        self.assertEqual(c.exception.rc, 2)

    # 2. _slug
    def test_slug_positive(self):
        self.assertEqual(fcd._slug(" Player-Character_1! ", 10), "player_cha")
        self.assertEqual(fcd._slug("Test App", 32), "test_app")

    def test_slug_negative_empty_and_special(self):
        self.assertEqual(fcd._slug("!!!", 10), "unknown")
        self.assertEqual(fcd._slug("", 5), "unknown")
        self.assertEqual(fcd._slug(None, 10), "none")

    # 3. _roots
    def test_roots_positive_layout1(self):
        run_dir = self.root / "run1"
        cap_dir = run_dir / "capture"
        cap_dir.mkdir(parents=True)
        man = cap_dir / "manifest.json"
        man.touch()
        found_man, found_root = fcd._roots(run_dir)
        self.assertEqual(found_man, man)
        self.assertEqual(found_root, run_dir)

    def test_roots_positive_layout2_capture_dirname(self):
        cap_dir = self.root / "run2" / "capture"
        cap_dir.mkdir(parents=True)
        man = cap_dir / "manifest.json"
        man.touch()
        found_man, found_root = fcd._roots(cap_dir)
        self.assertEqual(found_man, man)
        self.assertEqual(found_root, self.root / "run2")

    def test_roots_positive_layout2_direct_manifest(self):
        run_dir = self.root / "run3"
        run_dir.mkdir(parents=True)
        man = run_dir / "manifest.json"
        man.touch()
        found_man, found_root = fcd._roots(run_dir)
        self.assertEqual(found_man, man)
        self.assertEqual(found_root, run_dir)

    def test_roots_negative_missing_manifest(self):
        empty_dir = self.root / "empty_run"
        empty_dir.mkdir(parents=True)
        with self.assertRaises(VqaError) as c:
            fcd._roots(empty_dir)
        self.assertEqual(c.exception.code, "E_IO")

    # 4. _resolve
    def test_resolve_positive(self):
        run_dir = self.root / "run"
        cap_dir = run_dir / "capture"
        frames_dir = cap_dir / "frames"
        frames_dir.mkdir(parents=True)
        mp = cap_dir / "manifest.json"
        mp.touch()
        frame = frames_dir / "f0001.png"
        frame.touch()

        # Resolve relative to root
        resolved = fcd._resolve(run_dir, mp, "capture/frames/f0001.png")
        self.assertEqual(resolved, frame)

        # Resolve relative to manifest_path.parent
        resolved2 = fcd._resolve(run_dir, mp, "frames/f0001.png")
        self.assertEqual(resolved2, frame)

    def test_resolve_negative_missing_file(self):
        run_dir = self.root / "run"
        cap_dir = run_dir / "capture"
        cap_dir.mkdir(parents=True)
        mp = cap_dir / "manifest.json"
        mp.touch()
        with self.assertRaises(VqaError) as c:
            fcd._resolve(run_dir, mp, "nonexistent.png")
        self.assertEqual(c.exception.code, "E_IO")

    def test_resolve_refuses_escaping_root(self):
        """Exposes bug: _resolve does not verify if the target escapes root."""
        run_dir = self.root / "run"
        cap_dir = run_dir / "capture"
        cap_dir.mkdir(parents=True)
        mp = cap_dir / "manifest.json"
        mp.touch()

        outside_file = self.root / "outside.txt"
        outside_file.touch()

        # Path attempts to escape root via parent directory traversal
        rel_escape = "../../outside.txt"
        with self.assertRaises(VqaError) as c:
            fcd._resolve(run_dir, mp, rel_escape)
        self.assertEqual(c.exception.code, "E_IO")

    # 5. _rect_px
    def test_rect_px_positive_floors_origin_ceils_faredge(self):
        # x=1.25px (81920 q16), y=2.5px (163840 q16), w=3.1px (203161 q16), h=4.9px (321126 q16)
        # x0 = 1, y0 = 2
        # x1 = ceil(1.25 + 3.1) = ceil(4.35) = 5
        # y1 = ceil(2.5 + 4.9) = ceil(7.4) = 8
        r = [Q + Q // 4, 2 * Q + Q // 2, 3 * Q + 6553, 4 * Q + 58982]
        res = fcd._rect_px(r, 64, 48)
        self.assertEqual(res, [1, 2, 4, 6])  # [x0, y0, x1-x0, y1-y0] = [1, 2, 4, 6]

    def test_rect_px_positive_clips_to_frame(self):
        # Negative origin, far edge exceeds frame limits
        r = [-4 * Q, -2 * Q, 20 * Q, 60 * Q]
        res = fcd._rect_px(r, 10, 40)
        self.assertEqual(res, [0, 0, 10, 40])

    def test_rect_px_negative_invalid_inputs(self):
        # Non-list / tuple
        self.assertIsNone(fcd._rect_px("not a list", 64, 48))
        # Wrong length
        self.assertIsNone(fcd._rect_px([0, 0, Q], 64, 48))
        # Non-integer element
        self.assertIsNone(fcd._rect_px([0.0, 0, Q, Q], 64, 48))
        # Boolean element (bool is subclass of int, but explicitly excluded)
        self.assertIsNone(fcd._rect_px([True, 0, Q, Q], 64, 48))
        # Zero width / completely outside frame
        self.assertIsNone(fcd._rect_px([100 * Q, 0, Q, Q], 64, 48))
        self.assertIsNone(fcd._rect_px([0, 0, 0, Q], 64, 48))

    # 6. _rows_for
    def test_rows_for_positive(self):
        tel = {
            "ui": [
                {"id": "Button 1", "visible": True, "rect_px_q16": [Q, Q, 10 * Q, 5 * Q]},
                {"id": "Button 1", "visible": True, "rect_px_q16": [Q, Q, 10 * Q, 5 * Q]},  # duplicate rid
            ],
            "actors": [
                {"id": "hero_unit", "visible": True, "alive": True, "frustum_inside": True,
                 "in_front": True, "screen_bounds_px_q16": [2 * Q, 3 * Q, 4 * Q, 5 * Q]}
            ]
        }
        rows = fcd._rows_for(tel, 0, 64, 48)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["rid"] for r in rows], ["h:actor:hero_unit", "h:ui:button_1"])
        self.assertEqual(rows[0], {"i": 0, "rid": "h:actor:hero_unit", "role": "actor",
                                   "bbox_px": [2, 3, 4, 5], "z": None})
        self.assertEqual(rows[1], {"i": 0, "rid": "h:ui:button_1", "role": "ui",
                                   "bbox_px": [1, 1, 10, 5], "z": None})

    def test_rows_for_negative_filters_invalid(self):
        tel = {
            "ui": [
                {"id": "Hidden UI", "visible": False, "rect_px_q16": [Q, Q, Q, Q]},
                {"id": "Bad Rect", "visible": True, "rect_px_q16": [0, 0, 0, 0]}
            ],
            "actors": [
                {"id": "Dead Actor", "visible": True, "alive": False, "frustum_inside": True,
                 "in_front": True, "screen_bounds_px_q16": [Q, Q, Q, Q]},
                {"id": "Outside Frustum", "visible": True, "alive": True, "frustum_inside": False,
                 "in_front": True, "screen_bounds_px_q16": [Q, Q, Q, Q]},
                {"id": "Behind Camera", "visible": True, "alive": True, "frustum_inside": True,
                 "in_front": False, "screen_bounds_px_q16": [Q, Q, Q, Q]},
            ]
        }
        rows = fcd._rows_for(tel, 0, 64, 48)
        self.assertEqual(rows, [])


class TestBlenderHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # 1. _slug
    def test_slug_positive(self):
        self.assertEqual(fbd._slug("My Special Scene!", 16), "my_special_scene")

    def test_slug_negative_default_fallback(self):
        self.assertEqual(fbd._slug("!!!", 10, default="custom_fallback"), "custom_fallback")

    # 2. _read_json
    def test_read_json_positive(self):
        p = self.root / "test.json"
        p.write_text('{"app": "blender", "fps_num": 24}', encoding="utf-8")
        res = fbd._read_json(p)
        self.assertEqual(res, {"app": "blender", "fps_num": 24})

    def test_read_json_negative_missing_file(self):
        p = self.root / "missing.json"
        with self.assertRaises(VqaError) as c:
            fbd._read_json(p)
        self.assertEqual(c.exception.code, "E_IO")

    def test_read_json_negative_invalid_syntax(self):
        p = self.root / "invalid.json"
        p.write_text("not json content", encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            fbd._read_json(p)
        self.assertEqual(c.exception.code, "E_SCHEMA")

    # 3. _list_pngs
    def test_list_pngs_positive_with_empty_placeholders(self):
        d = self.root / "renders"
        d.mkdir()
        # Empty placeholder PNG files
        (d / "frame_0002.png").touch()
        (d / "frame_0001.png").touch()
        pngs = fbd._list_pngs(d)
        self.assertEqual(len(pngs), 2)
        self.assertEqual([n for n, _ in pngs], [1, 2])
        self.assertEqual(pngs[0][1].name, "frame_0001.png")
        self.assertEqual(pngs[1][1].name, "frame_0002.png")

    def test_list_pngs_negative_not_a_directory(self):
        p = self.root / "not_a_dir"
        p.touch()
        with self.assertRaises(VqaError) as c:
            fbd._list_pngs(p)
        self.assertEqual(c.exception.code, "E_IO")

    def test_list_pngs_negative_exr_refused(self):
        d = self.root / "exr_renders"
        d.mkdir()
        (d / "render_0001.exr").touch()
        with self.assertRaises(VqaError) as c:
            fbd._list_pngs(d)
        self.assertEqual(c.exception.code, "E_VC_PNG")

    def test_list_pngs_negative_no_frame_number(self):
        d = self.root / "no_num"
        d.mkdir()
        (d / "render.png").touch()
        with self.assertRaises(VqaError) as c:
            fbd._list_pngs(d)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

    def test_list_pngs_negative_duplicate_frame_numbers(self):
        d = self.root / "dup_nums"
        d.mkdir()
        (d / "camA_0001.png").touch()
        (d / "camB_0001.png").touch()
        with self.assertRaises(VqaError) as c:
            fbd._list_pngs(d)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")
        self.assertIn("duplicate frame 1", c.exception.to_obj()["detail"].get("reason", ""))

    # 4. _scene_meta
    def test_scene_meta_positive(self):
        d = self.root / "scene_ok"
        d.mkdir()
        (d / "scene.json").write_text('{"app": "blender_test"}', encoding="utf-8")
        self.assertEqual(fbd._scene_meta(d), {"app": "blender_test"})

    def test_scene_meta_positive_missing_returns_empty_dict(self):
        d = self.root / "no_scene"
        d.mkdir()
        self.assertEqual(fbd._scene_meta(d), {})

    def test_scene_meta_negative_non_dict(self):
        d = self.root / "bad_scene"
        d.mkdir()
        (d / "scene.json").write_text('[1, 2, 3]', encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            fbd._scene_meta(d)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

    # 5. _hint_rows
    def test_hint_rows_positive_includes_bounds_outside_frame(self):
        d = self.root / "hints_ok"
        d.mkdir()
        # Include bounds row outside standard frame dimensions (e.g. negative origin)
        (d / "bounds.jsonl").write_text(
            '{"frame": 10, "name": "offscreen_obj", "role": "object", "bbox_px": [-100, -50, 200, 300], "z": 5}\n',
            encoding="utf-8"
        )
        frame_to_i = {10: 0}
        rows = fbd._hint_rows(d, frame_to_i)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            "schema": "vqa.region-hint/v1",
            "i": 0,
            "rid": "h:object:offscreen_obj",
            "role": "object",
            "bbox_px": [-100, -50, 200, 300],
            "z": 5
        })

    def test_hint_rows_positive_missing_returns_none(self):
        d = self.root / "no_hints"
        d.mkdir()
        self.assertIsNone(fbd._hint_rows(d, {1: 0}))

    def test_hint_rows_negative_invalid_rows(self):
        d = self.root / "bad_hints"
        d.mkdir()

        # Unknown frame number
        (d / "bounds.jsonl").write_text('{"frame": 999, "name": "cube", "bbox_px": [0,0,10,10]}\n', encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            fbd._hint_rows(d, {1: 0})
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

        # Invalid role
        (d / "bounds.jsonl").write_text('{"frame": 1, "name": "cube", "role": "invalid_role", "bbox_px": [0,0,10,10]}\n', encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            fbd._hint_rows(d, {1: 0})
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

        # Invalid name format (e.g. contains spaces/uppercase)
        (d / "bounds.jsonl").write_text('{"frame": 1, "name": "Invalid Name", "role": "object", "bbox_px": [0,0,10,10]}\n', encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            fbd._hint_rows(d, {1: 0})
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

        # Invalid bbox (width < 1)
        (d / "bounds.jsonl").write_text('{"frame": 1, "name": "cube", "role": "object", "bbox_px": [0,0,0,10]}\n', encoding="utf-8")
        with self.assertRaises(VqaError) as c:
            fbd._hint_rows(d, {1: 0})
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")


if __name__ == "__main__":
    unittest.main()
