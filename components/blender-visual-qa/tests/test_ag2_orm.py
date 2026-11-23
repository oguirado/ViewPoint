"""AG2 M4: orm_pack channel packing + roughness floor. PNG fixtures are built in-test by an independent writer (no binaries)."""
import hashlib
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

from blender_visual_qa.assets import orm_pack as op

FLOOR = 11


def chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", op.zlib.crc32(tag + body) & 0xFFFFFFFF)


def png(w, h, ch, pix, filt=0):
    """Independent PNG writer. pix: bytes w*h*ch. filt 0..4 applied to every row (so the decoder's filters are exercised)."""
    ctype = {1: 0, 3: 2, 4: 6}[ch]
    stride = w * ch
    raw, prev = bytearray(), bytes(stride)
    for y in range(h):
        row = pix[y * stride:(y + 1) * stride]
        raw.append(filt)
        for i in range(stride):
            a = row[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            paeth = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            pred = (0, a, b, (a + b) // 2, paeth)[filt]
            raw.append((row[i] - pred) & 255)
        prev = row
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", op.zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


class OrmCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def path(self, name, data=None):
        p = os.path.join(self.td.name, name)
        if data is not None:
            with open(p, "wb") as fh:
                fh.write(data)
        return p

    def pack(self, *extra, out="orm.png"):
        o = self.path(out)
        rc, rec = op.run(["--out", o, *extra])
        return rc, rec, o

    def orm_pixels(self, o):
        with open(o, "rb") as fh:
            w, h, ch, px = op.decode_png(fh.read())
        self.assertEqual(ch, 4)
        return w, h, px


class TestPack(OrmCase):
    def test_constants_pack_and_floor(self):
        rc, rec, o = self.pack("--ao", "1000", "--rough", "0", "--metal", "0", "--size", "4x2")
        self.assertEqual(rc, 0)
        w, h, px = self.orm_pixels(o)
        self.assertEqual((w, h), (4, 2))
        self.assertEqual(set(px[0::4]), {255})
        self.assertEqual(set(px[1::4]), {FLOOR})  # rough 0 lifted to the floor
        self.assertEqual(set(px[2::4]), {0})
        self.assertEqual(set(px[3::4]), {255})
        self.assertEqual(rec["rough_clamped_px"], 8)
        self.assertEqual(rec["schema"], "bvqa.asset-orm-pack/v1")

    def test_maps_channel_mapping_and_clamp(self):
        ao = png(3, 1, 1, bytes([10, 20, 30]))
        rough = png(3, 1, 3, bytes([0, 0, 0, 10, 0, 0, 200, 0, 0]))  # first channel used: 0,10,200
        metal = png(3, 1, 4, bytes([7, 0, 0, 255, 8, 0, 0, 255, 9, 0, 0, 255]))
        rc, rec, o = self.pack("--ao", self.path("ao.png", ao), "--rough", self.path("r.png", rough),
                               "--metal", self.path("m.png", metal))
        self.assertEqual(rc, 0)
        _w, _h, px = self.orm_pixels(o)
        self.assertEqual(list(px[0::4]), [10, 20, 30])          # R = AO
        self.assertEqual(list(px[1::4]), [FLOOR, FLOOR, 200])   # G = roughness, floored
        self.assertEqual(list(px[2::4]), [7, 8, 9])             # B = metallic
        self.assertEqual(rec["rough_clamped_px"], 2)

    def test_png_filters_decode(self):
        pix = bytes((x * 37 + y * 11) & 255 for y in range(3) for x in range(5))
        for f in range(5):
            w, h, ch, got = op.decode_png(png(5, 3, 1, pix, filt=f))
            self.assertEqual((w, h, ch, got), (5, 3, 1, pix), "filter %d" % f)

    def test_deterministic_and_chain(self):
        _rc, r1, o1 = self.pack("--size", "8x8", out="a.png")
        _rc, r2, o2 = self.pack("--size", "8x8", out="b.png")
        with open(o1, "rb") as f1, open(o2, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        prev = self.path("prev.json", op.canon(r1).encode())
        _rc, r3, _o = self.pack("--size", "8x8", "--prev-receipt", prev)
        self.assertEqual(r3["prev_sha256"], hashlib.sha256(op.canon(r1).encode()).hexdigest())
        self.assertEqual(r1["prev_sha256"], "")
        self.assertEqual(r1["input_sha256"], r2["input_sha256"])

    def test_usage_errors_rc2(self):
        a, b = png(2, 2, 1, bytes(4)), png(3, 2, 1, bytes(6))
        rc, rec, _ = self.pack("--ao", self.path("a.png", a), "--rough", self.path("b.png", b))
        self.assertEqual((rc, rec), (2, None))  # dims differ
        rc, rec, _ = self.pack("--ao", self.path("bad.png", b"not a png at all"))
        self.assertEqual((rc, rec), (2, None))
        rc, rec, _ = self.pack("--rough", os.path.join(self.td.name, "missing.png"))
        self.assertEqual((rc, rec), (2, None))
        rc, rec, _ = self.pack("--size", "0x4")
        self.assertEqual((rc, rec), (2, None))
        self.assertEqual(op.run([])[0], 2)


class TestValidateAndMutants(OrmCase):
    def orm(self, g):
        return self.path("v%d.png" % g, png(2, 2, 4, bytes([255, g, 0, 255] * 4)))

    def test_validate_floor_boundary(self):
        rc, rec = op.run(["--validate-only", "--out", self.orm(FLOOR)])
        self.assertEqual((rc, rec["status"], rec["violations"]), (0, "PASS", 0))
        rc, rec = op.run(["--validate-only", "--out", self.orm(FLOOR - 1)])  # rough byte 10
        self.assertEqual((rc, rec["status"], rec["violations"], rec["first_bad_pixel"]), (5, "FAIL", 4, 0))

    def test_mutant_floor_10_is_detected(self):
        """Mutant: floor weakened to 10. The real module rejects the byte-10 ORM, the mutant accepts it."""
        f = self.orm(10)
        self.assertEqual(op.run(["--validate-only", "--out", f])[0], 5)
        with mock.patch.object(op, "ASSET_ORM_ROUGH_MIN_BYTE", 10):
            self.assertEqual(op.run(["--validate-only", "--out", f])[0], 0)  # mutant survives only on this input...
        # ...so a pack at rough 0 under the mutant yields G=10, which the real validator kills:
        with mock.patch.object(op, "ASSET_ORM_ROUGH_MIN_BYTE", 10):
            rc, _rec, o = self.pack("--rough", "0", "--size", "2x2")
        self.assertEqual(rc, 0)
        self.assertEqual(op.run(["--validate-only", "--out", o])[0], 5)

    def test_mutant_no_clamp_killed_by_selfcheck(self):
        def no_clamp(ao, rough, metal, w, h, floor=None):
            out = bytearray()
            for i in range(w * h):
                out += bytes((ao[i], rough[i], metal[i], 255))
            return bytes(out), 0
        with mock.patch.object(op, "pack_orm", no_clamp):
            rc, rec, _o = self.pack("--rough", "0", "--size", "2x2")
        self.assertEqual(rc, 5)
        self.assertEqual(rec["status"], "FAIL")
        self.assertEqual(self.pack("--rough", "0", "--size", "2x2")[0], 0)  # base passes

    def test_mutant_channel_swap_changes_output(self):
        _rc, _r, base = self.pack("--ao", "400", "--rough", "600", "--metal", "800", "--size", "1x1")
        _w, _h, px = self.orm_pixels(base)
        self.assertEqual(tuple(px), (102, 153, 204, 255))  # R=AO, G=rough, B=metal
        _rc, _r, sw = self.pack("--ao", "600", "--rough", "400", "--metal", "800", "--size", "1x1", out="sw.png")
        self.assertNotEqual(tuple(self.orm_pixels(sw)[2]), (102, 153, 204, 255))

    def test_validate_rejects_gray_and_garbage(self):
        self.assertEqual(op.run(["--validate-only", "--out", self.path("g.png", png(2, 2, 1, bytes(4)))])[0], 2)
        self.assertEqual(op.run(["--validate-only", "--out", self.path("x.png", b"junk")])[0], 2)

    def test_main_prints_canonical_receipt(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = op.main(["--out", self.path("m.png"), "--size", "2x2"])
        self.assertEqual(rc, 0)
        rec = json.loads(buf.getvalue())
        self.assertEqual(buf.getvalue(), op.canon(rec))


if __name__ == "__main__":
    unittest.main()
