"""decode.py + frame.py tests (SPEC_ARCH §4): PNG filters 0-4, RGBA alpha drop, rejects, raw memmap, manifest."""
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

from visioncore import canon, decode, frame
from visioncore.errors import VqaError


def chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def make_png(img, ftype_of_row, ctype=2, interlace=0):
    """Reference encoder (test-only): forward filter per row, filter type = ftype_of_row(y)."""
    H, W, bpp = img.shape
    rows = img.reshape(H, W * bpp).astype(int)
    raw = bytearray()
    for y in range(H):
        ft = ftype_of_row(y)
        cur, prev = rows[y], (rows[y - 1] if y else np.zeros(W * bpp, int))
        out = []
        for x in range(W * bpp):
            a = cur[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            pred = (0, a, b, (a + b) // 2, paeth(a, b, c))[ft if ft < 5 else 0]
            out.append((cur[x] - pred) & 255)
        raw += bytes([ft]) + bytes(out)
    ihdr = struct.pack(">IIBBBBB", W, H, 8, ctype, 0, 0, interlace)
    return decode._SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def rnd(H, W, c=3, seed=1):
    return np.random.RandomState(seed).randint(0, 256, (H, W, c)).astype(np.uint8)


class TestDecode(unittest.TestCase):
    def test_all_filters(self):
        img = rnd(17, 23)
        for ft in range(5):
            self.assertTrue(np.array_equal(decode.png(make_png(img, lambda y: ft)), img), ft)
        self.assertTrue(np.array_equal(decode.png(make_png(img, lambda y: y % 5)), img))

    def test_rgba_alpha_dropped(self):
        a = rnd(9, 11, 4, 2)
        b = a.copy()
        b[..., 3] = 7
        da, db = decode.png(make_png(a, lambda y: 4, ctype=6)), decode.png(make_png(b, lambda y: 1, ctype=6))
        self.assertEqual(da.shape, (9, 11, 3))
        self.assertTrue(np.array_equal(da, db) and np.array_equal(da, a[..., :3]))

    def test_roundtrip_encode(self):
        img = rnd(40, 50, 3, 3)
        self.assertTrue(np.array_equal(decode.png(decode.encode_png(img)), img))
        self.assertEqual(decode.encode_png(img), decode.encode_png(img))

    def test_path_input(self):
        img = rnd(8, 8)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            p.write_bytes(decode.encode_png(img))
            self.assertTrue(np.array_equal(decode.png(p), img))
            with self.assertRaises(VqaError) as c:
                decode.png(Path(d) / "missing.png")
            self.assertEqual(c.exception.code, "E_IO")

    def rejects(self, data):
        with self.assertRaises(VqaError) as c:
            decode.png(data)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_VC_PNG", 3))

    def test_rejects(self):
        img = rnd(8, 8)
        self.rejects(make_png(img, lambda y: 0, interlace=1))  # interlaced
        self.rejects(b"not a png at all")
        good = bytearray(decode.encode_png(img))
        bad = bytearray(good)
        bad[40] ^= 0xFF  # corrupt IDAT payload -> crc
        self.rejects(bytes(bad))
        self.rejects(bytes(good[:-20]))  # truncated
        gray = decode._SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 0, 0, 0, 0)) + \
            chunk(b"IDAT", zlib.compress(bytes(8 * 9))) + chunk(b"IEND", b"")
        self.rejects(gray)  # greyscale
        d16 = decode._SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 16, 2, 0, 0, 0)) + \
            chunk(b"IDAT", zlib.compress(bytes(8 * 49))) + chunk(b"IEND", b"")
        self.rejects(d16)  # 16-bit
        self.rejects(make_png(img, lambda y: 5))  # unknown filter type

    def test_rgb24_raw(self):
        a = rnd(6, 10, 3, 4)
        b = rnd(6, 10, 3, 5)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.rgb"
            p.write_bytes(a.tobytes() + b.tobytes())
            m = decode.rgb24_raw(p, 10, 6, 2)
            self.assertTrue(np.array_equal(m[0], a) and np.array_equal(m[1], b))
            with self.assertRaises(VqaError) as c:
                decode.rgb24_raw(p, 10, 6, 3)
            self.assertEqual(c.exception.code, "E_VC_FRAME_SIZE")


def manifest(frames, W=16, H=16):
    fr = [{"i": k, "t_us": 1000 * k, "render_index": k, "file": "frames/%06d.png" % k,
           "sha256": f.sha256, "file_sha256": f.sha256} for k, f in enumerate(frames)]
    return {"schema": "vqa.frame/v1", "width": W, "height": H, "channels": 3, "pixel_format": "rgb24",
            "fps_num": 30, "fps_den": 1, "count": len(fr), "storage": "png_dir", "raw_path": None,
            "frame_bytes": W * H * 3, "frames": fr, "hints": None, "still_probe": None,
            "scope": {"app": "synth", "scene": "t", "res": "%dx%d" % (W, H), "build_seq": 0},
            "provenance": {"adapter_id": "synth", "adapter_version": "1", "capture_receipt_sha256": None}}


class TestFrame(unittest.TestCase):
    def setUp(self):
        self.frames = [frame.Frame(k, 1000 * k, k, rnd(16, 16, 3, k)) for k in range(3)]

    def test_frame_sha_and_readonly(self):
        f = self.frames[0]
        import hashlib
        self.assertEqual(f.sha256, hashlib.sha256(f.rgb.tobytes()).hexdigest())
        with self.assertRaises(ValueError):
            f.rgb[0, 0, 0] = 1
        with self.assertRaises(VqaError) as c:
            frame.Frame(0, 0, 0, rnd(16, 16), sha256="0" * 64)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_VC_FRAME_SHA", 3))
        with self.assertRaises(VqaError) as c:
            frame.Frame(0, 0, 0, np.zeros((4, 4, 4), np.uint8))
        self.assertEqual(c.exception.code, "E_VC_FRAME_SHAPE")

    def test_manifest_roundtrip_canonical(self):
        m = manifest(self.frames)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "frames.manifest.json"
            frame.write_manifest(p, m)
            self.assertTrue(canon.is_canonical(p.read_text()))
            self.assertEqual(frame.read_manifest(p), m)
            self.assertFalse((Path(d) / "frames.manifest.json.tmp").exists())

    def test_manifest_negatives(self):
        def bad(mut, code):
            m = manifest(self.frames)
            mut(m)
            with self.assertRaises(VqaError) as c:
                frame.validate_manifest(m)
            self.assertEqual(c.exception.code, code)
        bad(lambda m: m.update(width=8193), "E_VC_FRAME_SHAPE")
        bad(lambda m: m.update(schema="vqa.frame/v2"), "E_VC_FRAME_SCHEMA")
        bad(lambda m: m.update(count=2), "E_VC_FRAME_SCHEMA")
        bad(lambda m: m["frames"][1].update(i=5), "E_VC_FRAME_SCHEMA")
        bad(lambda m: m["frames"][2].update(t_us=0), "E_VC_FRAME_SCHEMA")
        bad(lambda m: m["frames"][0].update(sha256="xyz"), "E_VC_FRAME_SCHEMA")
        bad(lambda m: m["scope"].update(app="Bad-App"), "E_VC_FRAME_SCHEMA")
        bad(lambda m: m.update(channels=4), "E_VC_FRAME_SCHEMA")

    def test_refusal_leaves_output(self):
        m = manifest(self.frames)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            frame.write_manifest(p, m)
            before = p.read_bytes()
            m["count"] = 99
            with self.assertRaises(VqaError):
                frame.write_manifest(p, m)
            self.assertEqual(p.read_bytes(), before)

    def test_frames_sha256_and_source(self):
        a = frame.frames_sha256([f.sha256 for f in self.frames])
        self.assertEqual(a, frame.frames_sha256([f.sha256 for f in self.frames]))
        self.assertNotEqual(a, frame.frames_sha256([f.sha256 for f in self.frames[::-1]]))
        src = frame.MemoryFrameSource(self.frames, {"width": 16})
        self.assertEqual((len(src), [f.i for f in src], src.meta["width"]), (3, [0, 1, 2], 16))


if __name__ == "__main__":
    unittest.main()
