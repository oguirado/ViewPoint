"""AG3 N5: delight (VG-0252) fixed-point de-baking divide. PNG fixtures built in-test (no binaries)."""
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from blender_visual_qa.assets import delight as dl
from blender_visual_qa.assets import orm_pack as op


def gray_png(w, h, pix):
    rgba = bytearray()
    for v in pix:
        rgba += bytes((v, v, v, 255))
    return op.encode_png(w, h, bytes(rgba))


class DelightCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def path(self, name, data=None):
        p = os.path.join(self.td.name, name)
        if data is not None:
            with open(p, "wb") as fh:
                fh.write(data)
        return p

    def delight(self, diffuse, irr, w=None, *extra):
        w = w or len(diffuse)
        d, i = self.path("d.png", gray_png(w, 1, diffuse)), self.path("i.png", gray_png(w, 1, irr))
        out = self.path("o.png")
        rc, rec = dl.run(["--diffuse", d, "--irradiance", i, "--out", out, *extra])
        return rc, rec, out

    def pixels(self, out):
        with open(out, "rb") as fh:
            return list(op.decode_png(fh.read())[3][0::4])


class TestKernel(DelightCase):
    def test_divide_known_values(self):
        # d=100, a=200 -> r = d/a = 0.5 -> byte 127 (Q16 rounding of 100/200*255)
        self.assertEqual(dl.from_q16(dl.delight_q16(dl.to_q16(100), dl.to_q16(200))), 127)
        # full irradiance leaves the texel alone
        self.assertEqual(dl.from_q16(dl.delight_q16(dl.to_q16(77), dl.to_q16(255))), 77)

    def test_gain_cap_and_zero_irradiance(self):
        # a=0 must not divide by zero; gain is capped by the floor (65536/4096 = 16x)
        self.assertEqual(dl.delight_q16(1000, 0), 16000)
        self.assertEqual(dl.delight_q16(60000, 0), dl.Q16_ONE)

    def test_never_darkens(self):
        for d in range(0, 256, 5):
            for a in range(0, 256, 5):
                self.assertGreaterEqual(dl.delight_q16(dl.to_q16(d), dl.to_q16(a)), dl.to_q16(d))

    def test_end_to_end_removes_baked_ao(self):
        albedo = [120, 120, 120, 120]
        ao = [255, 128, 64, 255]
        diffuse = [a * x // 255 for a, x in zip(albedo, ao)]
        rc, rec, out = self.delight(diffuse, ao)
        got = self.pixels(out)
        self.assertTrue(all(abs(g - 120) <= 2 for g in got), got)
        self.assertGreater(rec["baked_frac_q16"], dl.ASSET_DELIGHT_BAKED_FRAC_Q16)
        self.assertTrue(rec["baked"])
        self.assertEqual(rec["darkened_texels"], 0)

    def test_clean_asset_not_baked(self):
        rc, rec, _ = self.delight([90, 90, 90], [255, 255, 255])
        self.assertFalse(rec["baked"])
        self.assertEqual(rec["baked_frac_q16"], 0)


class TestStatusAndChain(DelightCase):
    def test_preview_until_calibrated(self):
        rc, rec, _ = self.delight([50, 60], [128, 128])
        self.assertEqual((rc, rec["status"], rec["calibrated"]), (3, "PREVIEW", False))
        with mock.patch.object(dl, "ASSET_DELIGHT_CALIBRATED", True):
            rc, rec, _ = self.delight([50, 60], [128, 128])
        self.assertEqual((rc, rec["status"]), (0, "PASS"))

    def test_fail_baked_rc5(self):
        with mock.patch.object(dl, "ASSET_DELIGHT_CALIBRATED", True):
            rc, rec, _ = self.delight([30, 30], [64, 64], None, "--fail-baked")
            self.assertEqual((rc, rec["status"]), (5, "FAIL"))
            rc, _rec, _ = self.delight([30, 30], [64, 64])
            self.assertEqual(rc, 0)

    def test_deterministic_and_chain(self):
        _rc, r1, o1 = self.delight([40, 80, 120], [100, 150, 200])
        _rc, r2, o2 = self.delight([40, 80, 120], [100, 150, 200])
        self.assertEqual(r1["out_sha256"], r2["out_sha256"])
        prev = self.path("prev.json", op.canon(r1).encode())
        _rc, r3, _ = self.delight([40, 80, 120], [100, 150, 200], None, "--prev-receipt", prev)
        self.assertEqual(r3["prev_sha256"], hashlib.sha256(op.canon(r1).encode()).hexdigest())
        self.assertEqual(r1["schema"], "bvqa.asset-delight/v1")

    def test_usage_errors_rc2(self):
        d = self.path("d.png", gray_png(2, 1, [1, 2]))
        i = self.path("i.png", gray_png(3, 1, [1, 2, 3]))
        self.assertEqual(dl.run(["--diffuse", d, "--irradiance", i, "--out", self.path("o.png")]), (2, None))
        self.assertEqual(dl.run(["--diffuse", d, "--irradiance", self.path("none.png"), "--out", self.path("o.png")]), (2, None))
        self.assertEqual(dl.run([])[0], 2)


class TestAzimuthVariance(unittest.TestCase):
    def test_delit_variance_drops(self):
        """Baked shading varies per azimuth; the de-lit albedo is constant across the 8 views."""
        albedo = 120
        shade = [255, 200, 128, 90, 64, 90, 128, 200]
        before = [bytes([albedo * s // 255] * 4) for s in shade]
        after = [dl.delight_plane(f, bytes([s] * 4))[0] for f, s in zip(before, shade)]
        vb, va = dl.azimuth_variance(before), dl.azimuth_variance(after)
        self.assertGreater(vb, 0)
        self.assertLess(va * 100, vb)  # at least a 100x drop

    def test_needs_eight_planes(self):
        with self.assertRaises(op.OrmError):
            dl.azimuth_variance([b"\0"] * 7)


class TestMutants(DelightCase):
    def test_mutant_no_divide_detected(self):
        """Mutant: kernel returns d unchanged. Albedo stays shaded, which the end-to-end assertion rejects."""
        with mock.patch.object(dl, "delight_q16", lambda d, a, floor=None: d):
            _rc, rec, out = self.delight([30, 15], [255, 128])
        self.assertEqual(self.pixels(out), [30, 15])
        self.assertEqual(rec["baked_frac_q16"], 0)  # real kernel gives >0 here
        _rc, real, _ = self.delight([30, 15], [255, 128])
        self.assertGreater(real["baked_frac_q16"], 0)

    def test_mutant_darkening_kernel_rc5(self):
        with mock.patch.object(dl, "delight_q16", lambda d, a, floor=None: d // 2):
            rc, rec, _ = self.delight([100, 100], [200, 200])
        self.assertEqual((rc, rec["status"]), (5, "FAIL"))
        self.assertEqual(rec["darkened_texels"], 6)

    def test_mutant_floor_one_uncapped_gain(self):
        """Mutant: floor 1 lets a=0 texels explode to white; the real floor caps the gain at 16x."""
        d = [4]
        _rc, _rec, out = self.delight(d, [0])
        self.assertEqual(self.pixels(out), [dl.from_q16(dl.delight_q16(dl.to_q16(4), 0))])
        self.assertLess(self.pixels(out)[0], 255)
        with mock.patch.object(dl, "ASSET_DELIGHT_FLOOR_Q16", 1):
            _rc, _rec, out2 = self.delight(d, [0])
        self.assertEqual(self.pixels(out2), [255])


if __name__ == "__main__":
    unittest.main()
