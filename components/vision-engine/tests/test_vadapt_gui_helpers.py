import tempfile
import unittest
from pathlib import Path

import numpy as np

from visioncore.errors import VqaError
from vadapt.gui.from_screens import (
    _natural,
    _read_jsonl,
    _u,
    _list_pngs,
    _raw_meta,
    _load_raw,
    _slug,
)


class TestVadaptGuiHelpers(unittest.TestCase):

    # -- _natural --------------------------------------------------------------
    def test_natural_sort_order(self):
        paths = [
            Path("frame10.png"),
            Path("frame2.png"),
            Path("frame1.png"),
            Path("a10b2.png"),
            Path("a2b10.png"),
        ]
        sorted_paths = sorted(paths, key=_natural)
        expected = [
            Path("a2b10.png"),
            Path("a10b2.png"),
            Path("frame1.png"),
            Path("frame2.png"),
            Path("frame10.png"),
        ]
        self.assertEqual(sorted_paths, expected)
        # Assert frame2 sorts before frame10 specifically
        self.assertLess(
            sorted_paths.index(Path("frame2.png")),
            sorted_paths.index(Path("frame10.png")),
        )

    def test_natural_edge_cases(self):
        # Negative / boundary cases: non-digit / digit strings, empty path name
        self.assertEqual(_natural(Path("")), [""])
        self.assertEqual(_natural(Path("0")), ["", 0, ""])
        self.assertEqual(_natural(Path("abc")), ["abc"])

    # -- _read_jsonl -----------------------------------------------------------
    def test_read_jsonl_valid_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "test.jsonl"
            p.write_text('{"a": 1}\n\n  \n{"b": 2}\n', encoding="utf-8")
            rows = _read_jsonl(p)
            self.assertEqual(rows, [{"a": 1}, {"b": 2}])

    def test_read_jsonl_negative_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nonexistent.jsonl"
            with self.assertRaises(VqaError) as cm:
                _read_jsonl(p)
            self.assertEqual(cm.exception.code, "E_IO")
            self.assertEqual(cm.exception.detail["path"], str(p))

    def test_read_jsonl_negative_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.jsonl"
            p.write_text('{"a": 1}\n{invalid_json}\n', encoding="utf-8")
            with self.assertRaises(VqaError) as cm:
                _read_jsonl(p)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")
            self.assertEqual(cm.exception.detail, {"path": str(p), "line": 2})

    def test_read_jsonl_negative_non_object_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "non_dict.jsonl"
            p.write_text('{"a": 1}\n[1, 2, 3]\n', encoding="utf-8")
            with self.assertRaises(VqaError) as cm:
                _read_jsonl(p)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")
            self.assertEqual(cm.exception.detail, {"path": str(p), "line": 2})

    # -- _u --------------------------------------------------------------------
    def test_u_positive(self):
        self.assertEqual(_u(0, 0, 10, "val"), 0)
        self.assertEqual(_u(5, 0, 10, "val"), 5)
        self.assertEqual(_u(10, 0, 10, "val"), 10)

    def test_u_negative_cases(self):
        # Booleans
        with self.assertRaises(VqaError) as cm:
            _u(True, 0, 10, "bool_test")
        self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")
        self.assertEqual(cm.exception.detail, {"field": "bool_test", "value": "True"})

        with self.assertRaises(VqaError) as cm:
            _u(False, 0, 10, "bool_test")
        self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")

        # Non-integer types
        with self.assertRaises(VqaError) as cm:
            _u(3.14, 0, 10, "float_test")
        self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")

        with self.assertRaises(VqaError) as cm:
            _u("5", 0, 10, "str_test")
        self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")

        # Out of bounds
        with self.assertRaises(VqaError) as cm:
            _u(-1, 0, 10, "underflow")
        self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")

        with self.assertRaises(VqaError) as cm:
            _u(11, 0, 10, "overflow")
        self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")

    # -- _list_pngs ------------------------------------------------------------
    def test_list_pngs_natural_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "frame10.png").touch()
            (d / "frame2.png").touch()
            (d / "frame1.png").touch()
            (d / "ignore.txt").touch()
            (d / "frame3.jpg").touch()

            pngs = _list_pngs(d)
            expected = [d / "frame1.png", d / "frame2.png", d / "frame10.png"]
            self.assertEqual(pngs, expected)

    def test_list_pngs_negative_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            pngs = _list_pngs(tmp)
            self.assertEqual(pngs, [])

    # -- _raw_meta -------------------------------------------------------------
    def test_raw_meta_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Default stride and file
            (d / "raw.json").write_text('{"width": 4, "height": 2}', encoding="utf-8")
            W, H, stride, names, per_frame = _raw_meta(d)
            self.assertEqual((W, H, stride, names, per_frame), (4, 2, 12, ["frames.raw"], False))

            # Custom stride and single file
            (d / "raw.json").write_text('{"width": 4, "height": 2, "stride": 16, "file": "custom.raw"}', encoding="utf-8")
            W, H, stride, names, per_frame = _raw_meta(d)
            self.assertEqual((W, H, stride, names, per_frame), (4, 2, 16, ["custom.raw"], False))

            # Multiple files
            (d / "raw.json").write_text('{"width": 4, "height": 2, "files": ["f1.raw", "f2.raw"]}', encoding="utf-8")
            W, H, stride, names, per_frame = _raw_meta(d)
            self.assertEqual((W, H, stride, names, per_frame), (4, 2, 12, ["f1.raw", "f2.raw"], True))

    def test_raw_meta_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)

            # Missing raw.json -> OSError / FileNotFoundError
            with self.assertRaises(OSError):
                _raw_meta(d)

            # Non-dict raw.json
            (d / "raw.json").write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(VqaError) as cm:
                _raw_meta(d)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")
            self.assertEqual(cm.exception.detail, {"field": "raw.json"})

            # Missing width / height
            (d / "raw.json").write_text('{"height": 2}', encoding="utf-8")
            with self.assertRaises(VqaError) as cm:
                _raw_meta(d)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")

            # Absolute path in file(s)
            (d / "raw.json").write_text('{"width": 4, "height": 2, "file": "/tmp/test.raw"}', encoding="utf-8")
            with self.assertRaises(VqaError) as cm:
                _raw_meta(d)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")
            self.assertEqual(cm.exception.detail, {"field": "file(s)"})

            # Directory traversal in files
            (d / "raw.json").write_text('{"width": 4, "height": 2, "files": ["../test.raw"]}', encoding="utf-8")
            with self.assertRaises(VqaError) as cm:
                _raw_meta(d)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SCHEMA")
            self.assertEqual(cm.exception.detail, {"field": "file(s)"})

    # -- _load_raw -------------------------------------------------------------
    def test_load_raw_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # 4x2 pixels, stride = 12 (W*3). Each frame is 24 bytes.
            W, H = 4, 2
            frame_bytes = 24
            data_f1 = bytes([k % 256 for k in range(frame_bytes)])
            data_f2 = bytes([(k + 10) % 256 for k in range(frame_bytes)])

            # Single concatenated file
            (d / "raw.json").write_text('{"width": 4, "height": 2}', encoding="utf-8")
            (d / "frames.raw").write_bytes(data_f1 + data_f2)

            frames, shas, w, h, files = _load_raw(d)
            self.assertEqual((w, h), (4, 2))
            self.assertEqual(len(frames), 2)
            self.assertEqual(files, ["frames.raw", "frames.raw"])
            self.assertEqual(frames[0].rgb.shape, (2, 4, 3))
            self.assertTrue(np.array_equal(frames[0].rgb.reshape(-1), np.frombuffer(data_f1, dtype=np.uint8)))

            # Per-frame multiple files with stride padding
            # 2x2 pixels, stride = 8 (W*3 = 6 + 2 bytes padding per row). Each frame row = 8 bytes, frame_bytes = 16.
            (d / "raw.json").write_text(
                '{"width": 2, "height": 2, "stride": 8, "files": ["f1.raw", "f2.raw"]}',
                encoding="utf-8"
            )
            row_padded = bytes([10, 20, 30, 40, 50, 60, 0, 0])
            (d / "f1.raw").write_bytes(row_padded * 2)
            (d / "f2.raw").write_bytes(row_padded * 2)

            frames_p, shas_p, w_p, h_p, files_p = _load_raw(d)
            self.assertEqual((w_p, h_p), (2, 2))
            self.assertEqual(len(frames_p), 2)
            self.assertEqual(files_p, ["f1.raw", "f2.raw"])
            self.assertEqual(frames_p[0].rgb.shape, (2, 2, 3))
            expected_rgb = np.array([[[10, 20, 30], [40, 50, 60]], [[10, 20, 30], [40, 50, 60]]], dtype=np.uint8)
            self.assertTrue(np.array_equal(frames_p[0].rgb, expected_rgb))

    def test_load_raw_negative_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "raw.json").write_text('{"width": 4, "height": 2}', encoding="utf-8")

            # Missing raw binary file
            with self.assertRaises(VqaError) as cm:
                _load_raw(d)
            self.assertEqual(cm.exception.code, "E_IO")

            # Empty raw binary file (mmap raises ValueError -> E_IO)
            (d / "frames.raw").write_bytes(b"")
            with self.assertRaises(VqaError) as cm:
                _load_raw(d)
            self.assertEqual(cm.exception.code, "E_IO")

            # Raw file length not multiple of frame bytes (24)
            (d / "frames.raw").write_bytes(b"x" * 25)
            with self.assertRaises(VqaError) as cm:
                _load_raw(d)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SIZE")
            self.assertEqual(cm.exception.detail["bytes"], 25)

            # Per-frame multiple files, where file length != fb (e.g. 2 frames in single per_frame file)
            (d / "raw.json").write_text('{"width": 4, "height": 2, "files": ["f1.raw"]}', encoding="utf-8")
            (d / "f1.raw").write_bytes(b"x" * 48)  # 2 * fb instead of 1 * fb
            with self.assertRaises(VqaError) as cm:
                _load_raw(d)
            self.assertEqual(cm.exception.code, "E_VC_FRAME_SIZE")

    # -- _slug -----------------------------------------------------------------
    def test_slug_positive(self):
        self.assertEqual(_slug("Open Superficies"), "open_superficies")
        self.assertEqual(_slug("Login Screen!"), "login_screen")
        self.assertEqual(_slug("a" * 50), "a" * 40)

    def test_slug_negative_and_fallback(self):
        self.assertEqual(_slug(""), "node")
        self.assertEqual(_slug("!!!"), "node")
        self.assertEqual(_slug("___"), "node")
        self.assertEqual(_slug(123), "123")
        self.assertEqual(_slug(None), "none")


if __name__ == "__main__":
    unittest.main()
