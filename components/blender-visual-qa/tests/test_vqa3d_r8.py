"""Round 8 (feedback 0715): production frames from a renderer and their own noise limit."""
import struct
import unittest
import zlib

from vqa3d import profile as p
from vqa3d.frame_input import production_rgb8
from blender_visual_qa.drive.png import PNG_SIG, chunk, decode
from tests.test_vqa3d_film import Shot
from tests.test_vqa3d_profiles import selected


def png(w, h, color, depth, rows, interlace=0, plte=False):
    """Encode rows of raw sample bytes, filtering odd rows with Sub so the bytes-per-pixel path is exercised."""
    bpp = max(1, {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color] * depth // 8)
    scan = b""
    for y, row in enumerate(rows):
        if y % 2:
            row = bytes((row[i] - (row[i-bpp] if i >= bpp else 0)) & 255 for i in range(len(row)))
        scan += bytes((y % 2,)) + row
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, color, 0, 0, interlace))
    if plte:
        body += chunk(b"PLTE", b"\0\0\0")
    return PNG_SIG + body + chunk(b"IDAT", zlib.compress(scan)) + chunk(b"IEND", b"")


RGB = [bytes([10, 20, 30, 40, 50, 60]), bytes([70, 80, 90, 200, 210, 220])]


class ProductionFrame(unittest.TestCase):
    def test_rgba_drops_alpha_like_ffmpeg_rgb24(self):
        rows = [bytes(sum(([r[i], r[i+1], r[i+2], a] for i, a in zip((0, 3), (0, 255))), [])) for r in RGB]
        out, info = production_rgb8(png(2, 2, 6, 8, rows))
        self.assertEqual(decode(out), (2, 2, b"".join(RGB)))
        self.assertEqual((info["colour"], info["alpha"], info["converted"]), ("rgba", "dropped", True))

    def test_sixteen_bit_keeps_high_byte(self):
        rows = [bytes(sum(([v, 0x7f] for v in r), [])) for r in RGB]
        out, info = production_rgb8(png(2, 2, 2, 16, rows))
        self.assertEqual(decode(out)[2], b"".join(RGB))
        self.assertEqual(info["bit_depth"], 16)
        rows = [bytes(sum(([r[i], 1, r[i+1], 2, r[i+2], 3, 9, 9] for i in (0, 3)), [])) for r in RGB]
        self.assertEqual(decode(production_rgb8(png(2, 2, 6, 16, rows))[0])[2], b"".join(RGB))

    def test_grey_and_grey_alpha_expand_to_rgb(self):
        self.assertEqual(decode(production_rgb8(png(2, 1, 0, 8, [b"\x05\xfa"]))[0])[2], b"\x05\x05\x05\xfa\xfa\xfa")
        self.assertEqual(decode(production_rgb8(png(2, 1, 4, 8, [b"\x05\x00\xfa\xff"]))[0])[2], b"\x05\x05\x05\xfa\xfa\xfa")

    def test_rgb8_is_unchanged(self):
        out, info = production_rgb8(png(2, 2, 2, 8, RGB))
        self.assertEqual(decode(out)[2], b"".join(RGB))
        self.assertFalse(info["converted"])

    def test_malformed_structure_is_refused_like_captures(self):
        good = png(2, 2, 2, 8, RGB)
        iend = chunk(b"IEND", b"")
        idat_at = good.index(b"IDAT") - 4
        idat = good[idat_at:len(good)-len(iend)]
        half = len(idat) // 2
        split = chunk(b"IDAT", idat[8:8+half]) + chunk(b"tEXt", b"k\0v") + chunk(b"IDAT", idat[8+half:-4])
        for data in (good + b"x", good[:-len(iend)] + chunk(b"IEND", b"x"), good[:idat_at] + split + iend):
            with self.assertRaises(ValueError):
                production_rgb8(data)

    def test_unsupported_input_names_the_conversion(self):
        for data in (png(2, 2, 2, 8, RGB, interlace=1), png(1, 1, 3, 8, [b"\0"], plte=True),
                     png(8, 1, 0, 1, [b"\0"]), b"not a png"):
            with self.assertRaises(ValueError) as caught:
                production_rgb8(data)
            self.assertIn("frame_png_unsupported:", str(caught.exception))
            self.assertIn("ffmpeg -i frame.png -pix_fmt rgb24", str(caught.exception))


class ProductionLimit(unittest.TestCase):
    def frame(self, noisy):
        w, h = 32, 32
        # Sparse speckle scores 15,625 ppm: above the production limit, below the 95,000 preview limit.
        rgb = bytes((255 if noisy and (x * 7 + y * 13) % 61 == 0 else 128)
                    for y in range(h) for x in range(w) for _ in range(3))
        rows = [rgb[y*w*3:(y+1)*w*3] for y in range(h)]
        rgba = [bytes(sum(([r[i], r[i+1], r[i+2], 255] for i in range(0, len(r), 3)), [])) for r in rows]
        return png(w, h, 6, 8, rgba)

    def test_production_frame_uses_its_own_limit(self):
        policy = selected(p.load("film-animation"), "nonblank", "noise")
        records, result, _ = p.run(policy, Shot(), frame_png=self.frame(noisy=True))
        noise = records[-1]
        self.assertEqual((noise["severity"], noise["final_verdict"]), ("gate", "FAIL"))
        self.assertEqual(noise["verdicts"][0]["reason"], "frame_outlier_ppm=15625:max_outlier_ppm=6000")
        self.assertEqual(noise["production_frame"]["alpha"], "dropped")
        # Review finding 2: the receipt holds the limit that judged the frame.
        self.assertEqual(noise["obligation"]["max_outlier_ppm"], 6000)
        self.assertIn("noise", next(iter(result["suggested_actions"])))
        records, _, _ = p.run(policy, Shot(), frame_png=self.frame(noisy=False))
        self.assertEqual(records[-1]["final_verdict"], "PASS")

    def test_production_limit_is_required_and_kept_out_of_the_core_obligation(self):
        policy = selected(p.load("film-animation"), "nonblank", "noise")
        del policy["defaults"]["frame_noise_below"]["production_max_outlier_ppm"]
        records, _, _ = p.run(policy, Shot(), frame_png=self.frame(noisy=False))
        self.assertEqual(records[-1]["final_verdict"], "CANNOT_SEE")
        self.assertIn("production_noise_limit_undeclared", records[-1]["verdicts"][0]["reason"])
        records, _, _ = p.run(selected(p.load("film-animation"), "nonblank", "noise"), Shot())
        self.assertNotIn("production_max_outlier_ppm", records[-1]["obligation"])

    def test_unsupported_frame_is_an_error_with_the_fix(self):
        policy = selected(p.load("film-animation"), "nonblank", "noise")
        records, _, _ = p.run(policy, Shot(), frame_png=png(2, 2, 2, 8, RGB, interlace=1))
        self.assertEqual(records[-1]["final_verdict"], "ERROR")
        self.assertIn("pix_fmt rgb24", records[-1]["verdicts"][0]["reason"])

    def test_scenario_with_profile_keeps_core_obligation_valid(self):
        # Review finding 1: `check --profile` re-applies defaults to scenario obligations.
        from vqa3d.check import validate_obligation
        for name in ("film-animation", "hyperreal-still"):
            validate_obligation(p.apply_defaults({"kind": "frame_noise_below"}, p.load(name)))

    def test_check_params_override_the_default_production_limit(self):
        policy = selected(p.load("film-animation"), "nonblank", "noise")
        index = next(i for i, c in enumerate(policy["checks"]) if c["id"] == "noise")
        policy["checks"][index]["params"]["production_max_outlier_ppm"] = 20000
        policy["provenance"]["/checks/%d/params/production_max_outlier_ppm" % index] = "target"
        records, _, _ = p.run(policy, Shot(), frame_png=self.frame(noisy=True))
        self.assertEqual(records[-1]["final_verdict"], "PASS")

    def test_profile_validation_bounds_the_production_limit(self):
        policy = p.load("film-animation")
        policy["defaults"]["frame_noise_below"]["production_max_outlier_ppm"] = 2000000
        with self.assertRaises(ValueError):
            p.validate(policy)


if __name__ == "__main__":
    unittest.main()
