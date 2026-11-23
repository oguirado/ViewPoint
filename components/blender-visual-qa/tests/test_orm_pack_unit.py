"""Unit and negative-path tests for blender_visual_qa/assets/orm_pack.py."""
import hashlib
import struct
import tempfile
import unittest
import zlib

from blender_visual_qa.assets import orm_pack as op


def _make_png(w, h, ch, pixels, ctype=None, depth=8, interlace=0, truncate_idat=False):
    """Construct PNG bytes in memory for testing decode_png / load_map."""
    if ctype is None:
        ctype = {1: 0, 3: 2, 4: 6}.get(ch, 0)
    stride = w * ch
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0
        raw.extend(pixels[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, interlace)
    idat = zlib.compress(bytes(raw))
    if truncate_idat:
        idat = idat[: len(idat) // 2]
    return op.PNG_SIG + op._chunk(b"IHDR", ihdr) + op._chunk(b"IDAT", idat) + op._chunk(b"IEND", b"")


class TestChunkUnit(unittest.TestCase):
    def test_chunk_valid_crc(self):
        tag = b"IHDR"
        body = b"\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06\x00\x00\x00"
        chunk_data = op._chunk(tag, body)
        length = struct.unpack(">I", chunk_data[:4])[0]
        self.assertEqual(length, len(body))
        self.assertEqual(chunk_data[4:8], tag)
        self.assertEqual(chunk_data[8 : 8 + length], body)
        crc = struct.unpack(">I", chunk_data[-4:])[0]
        expected_crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        self.assertEqual(crc, expected_crc)

    def test_chunk_negative_invalid_type(self):
        with self.assertRaises(TypeError):
            op._chunk("IHDR", b"body")  # tag must be bytes
        with self.assertRaises(TypeError):
            op._chunk(b"IHDR", "body")  # body must be bytes


class TestPaethUnit(unittest.TestCase):
    def test_paeth_png_spec(self):
        # pa <= pb and pa <= pc -> returns a
        self.assertEqual(op._paeth(10, 20, 20), 10)
        # pb <= pc -> returns b
        self.assertEqual(op._paeth(20, 10, 20), 10)
        # pc < pa and pc < pb -> returns c
        self.assertEqual(op._paeth(10, 100, 50), 50)
        # exact equality paeth(0, 0, 0) -> 0
        self.assertEqual(op._paeth(0, 0, 0), 0)

    def test_paeth_negative_invalid_type(self):
        with self.assertRaises(TypeError):
            op._paeth("10", 20, 30)


class TestConstByteUnit(unittest.TestCase):
    def test_const_byte_conversions_and_clamping(self):
        self.assertEqual(op._const_byte("0"), 0)
        self.assertEqual(op._const_byte("1000"), 255)
        # (500 * 255 + 500) // 1000 = 128
        self.assertEqual(op._const_byte("500"), 128)
        # Clamping
        self.assertEqual(op._const_byte("-100"), 0)
        self.assertEqual(op._const_byte("2000"), 255)

    def test_const_byte_negative_bad_string(self):
        with self.assertRaises(op.OrmError) as cm:
            op._const_byte("invalid")
        self.assertIn("bad constant 'invalid' (integer permille expected)", str(cm.exception))


class TestLoadMapUnit(unittest.TestCase):
    def test_load_map_constant_spec(self):
        w, h, byte_val, src = op.load_map("500")
        self.assertIsNone(w)
        self.assertIsNone(h)
        self.assertEqual(byte_val, 128)
        self.assertEqual(src, b"const:500")

    def test_load_map_grey_png_file(self):
        grey_data = bytes([10, 20, 30, 40])
        png_bytes = _make_png(2, 2, 1, grey_data)
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/grey.png"
            with open(path, "wb") as fh:
                fh.write(png_bytes)
            w, h, channel_bytes, raw_src = op.load_map(path)
            self.assertEqual((w, h), (2, 2))
            self.assertEqual(channel_bytes, grey_data)
            self.assertEqual(raw_src, png_bytes)

    def test_load_map_negative_nonexistent_file(self):
        with self.assertRaises(op.OrmError) as cm:
            op.load_map("/nonexistent_path/map.png")
        self.assertTrue(str(cm.exception).startswith("cannot read /nonexistent_path/map.png"))

    def test_load_map_negative_truncated_png(self):
        grey_data = bytes([10, 20, 30, 40])
        truncated_bytes = _make_png(2, 2, 1, grey_data, truncate_idat=True)
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/trunc.png"
            with open(path, "wb") as fh:
                fh.write(truncated_bytes)
            with self.assertRaises(op.OrmError) as cm:
                op.load_map(path)
            self.assertTrue(str(cm.exception).startswith("bad IDAT:"))

    def test_load_map_negative_unsupported_colour_png(self):
        # ctype 3 is indexed color (palette), which decode_png rejects
        colour_data = bytes([1, 2, 3, 4])
        bad_png = _make_png(2, 2, 1, colour_data, ctype=3)
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/bad_ctype.png"
            with open(path, "wb") as fh:
                fh.write(bad_png)
            with self.assertRaises(op.OrmError) as cm:
                op.load_map(path)
            self.assertEqual(str(cm.exception), "unsupported PNG (need 8-bit gray/RGB/RGBA, non-interlaced)")

    def test_load_map_negative_idat_size_mismatch(self):
        # Construct IDAT with incorrect raw length
        raw_short = zlib.compress(bytes([0, 10, 20]))  # missing 2nd row
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 0, 0, 0, 0)
        mismatch_png = (
            op.PNG_SIG
            + op._chunk(b"IHDR", ihdr)
            + op._chunk(b"IDAT", raw_short)
            + op._chunk(b"IEND", b"")
        )
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/mismatch.png"
            with open(path, "wb") as fh:
                fh.write(mismatch_png)
            with self.assertRaises(op.OrmError) as cm:
                op.load_map(path)
            self.assertEqual(str(cm.exception), "IDAT size mismatch")


class TestPackOrmUnit(unittest.TestCase):
    def test_pack_orm_channels_and_floor(self):
        ao = bytes([100, 200])
        rough = bytes([5, 150])  # 5 < floor(11), so clamped to 11
        metal = bytes([50, 250])
        rgba, clamped = op.pack_orm(ao, rough, metal, 2, 1)
        self.assertEqual(clamped, 1)
        # R=AO, G=Rough(floored), B=Metal, A=255
        expected = bytes([100, 11, 50, 255, 200, 150, 250, 255])
        self.assertEqual(rgba, expected)

    def test_pack_orm_negative_length_mismatch(self):
        ao = bytes([100])
        rough = bytes([150, 200])  # mismatched length
        metal = bytes([50, 250])
        with self.assertRaises(IndexError):
            op.pack_orm(ao, rough, metal, 2, 1)


class TestValidateOrmUnit(unittest.TestCase):
    def test_validate_orm_pass(self):
        # Valid ORM RGBA PNG
        rgba_pixels = bytes([100, 20, 50, 255, 200, 150, 250, 255])
        png_bytes = op.encode_png(2, 1, rgba_pixels)
        violations, first_bad, w, h = op.validate_orm(png_bytes)
        self.assertEqual(violations, 0)
        self.assertIsNone(first_bad)
        self.assertEqual((w, h), (2, 1))

    def test_validate_orm_negative_low_roughness(self):
        # Roughness channel (G) = 5, which is < floor 11
        rgba_pixels = bytes([100, 5, 50, 255, 200, 150, 250, 255])
        png_bytes = op.encode_png(2, 1, rgba_pixels)
        violations, first_bad, w, h = op.validate_orm(png_bytes)
        self.assertEqual(violations, 1)
        self.assertEqual(first_bad, 0)
        self.assertEqual((w, h), (2, 1))

    def test_validate_orm_negative_insufficient_channels(self):
        # Grey PNG has 1 channel (< 3 channels required for ORM)
        grey_data = bytes([10, 20, 30, 40])
        png_bytes = _make_png(2, 2, 1, grey_data)
        with self.assertRaises(op.OrmError) as cm:
            op.validate_orm(png_bytes)
        self.assertEqual(str(cm.exception), "ORM needs RGB(A), got 1 channel(s)")


class TestShaUnit(unittest.TestCase):
    def test_sha_valid_bytes(self):
        data = b"test payload"
        expected = hashlib.sha256(data).hexdigest()
        self.assertEqual(op._sha(data), expected)

    def test_sha_negative_invalid_type(self):
        with self.assertRaises(TypeError):
            op._sha("string_not_bytes")


class TestSizeUnit(unittest.TestCase):
    def test_size_parsing(self):
        self.assertEqual(op._size("256x256"), (256, 256))
        self.assertEqual(op._size("1X1"), (1, 1))
        self.assertEqual(op._size("16384x16384"), (16384, 16384))

    def test_size_negative_invalid_format(self):
        with self.assertRaises(op.OrmError) as cm:
            op._size("invalid_size")
        self.assertEqual(str(cm.exception), "bad --size 'invalid_size' (WxH)")

    def test_size_negative_out_of_range(self):
        with self.assertRaises(op.OrmError) as cm:
            op._size("0x256")
        self.assertEqual(str(cm.exception), "--size out of range")

        with self.assertRaises(op.OrmError) as cm:
            op._size("16385x256")
        self.assertEqual(str(cm.exception), "--size out of range")


if __name__ == "__main__":
    unittest.main()
