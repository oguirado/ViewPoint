"""Unit and negative-path tests for blender_visual_qa/assets/pbr_validate.py.

Tests covers functions:
- _permille (line 33)
- _srgb_byte (line 37)
- _luma_byte (line 44)
- material_params (line 48)
- check_material (line 58)
- check_albedo_png (line 82)
- validate_glb (line 98)
"""
import json
import os
import struct
import tempfile
import unittest

from blender_visual_qa.assets import glb_core, orm_pack, pbr_validate as pv


def make_minimal_glb(materials=None):
    """Build minimal valid GLB bytes with optional materials list."""
    scene = glb_core.Scene(prims=[
        glb_core.Prim(
            name="p1",
            node_path="/p1",
            world_q16=glb_core.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (1000000, 0, 0), (0, 1000000, 0)],
            indices=[0, 1, 2],
            uvs_q16=[(0, 0), (8192, 0), (0, 8192)],
            material="m1"
        )
    ])
    raw = glb_core.write_glb(scene)
    if materials is not None:
        doc, binbuf = glb_core._split(raw)
        doc["materials"] = materials
        js = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        binbuf += b"\0" * (-len(binbuf) % 4)
        total = 12 + 8 + len(js) + 8 + len(binbuf)
        raw = (
            struct.pack("<4sII", b"glTF", 2, total)
            + struct.pack("<II", len(js), 0x4E4F534A)
            + js
            + struct.pack("<II", len(binbuf), 0x004E4942)
            + binbuf
        )
    return raw


class TestPermille(unittest.TestCase):
    def test_permille_exact_values(self):
        self.assertEqual(pv._permille(0.0), 0)
        self.assertEqual(pv._permille(1.0), 1000)
        self.assertEqual(pv._permille(0.1234), 123)
        self.assertEqual(pv._permille(0.1235), 124)
        self.assertEqual(pv._permille(0.5007), 501)
        self.assertEqual(pv._permille(-0.1234), -123)
        self.assertEqual(pv._permille(-0.1235), -124)

    def test_permille_negative_invalid_input(self):
        with self.assertRaises((TypeError, ValueError)):
            pv._permille("not_a_number")
        with self.assertRaises(TypeError):
            pv._permille(None)


class TestSrgbByte(unittest.TestCase):
    def test_srgb_byte_formula_values(self):
        self.assertEqual(pv._srgb_byte(0.0), 0)
        self.assertEqual(pv._srgb_byte(1.0), 255)
        self.assertEqual(pv._srgb_byte(0.003), 10)
        self.assertEqual(pv._srgb_byte(0.0031308), 10)
        self.assertEqual(pv._srgb_byte(0.2), 124)
        self.assertEqual(pv._srgb_byte(0.5), 188)
        self.assertEqual(pv._srgb_byte(0.8), 231)

    def test_srgb_byte_clamping_and_invalid_input(self):
        # Negative path: clamped out-of-bounds inputs
        self.assertEqual(pv._srgb_byte(-0.5), 0)
        self.assertEqual(pv._srgb_byte(1.5), 255)
        # Invalid input type
        with self.assertRaises(TypeError):
            pv._srgb_byte(None)
        with self.assertRaises(ValueError):
            pv._srgb_byte("invalid")


class TestLumaByte(unittest.TestCase):
    def test_luma_byte_formula_values(self):
        self.assertEqual(pv._luma_byte((0, 0, 0)), 0)
        self.assertEqual(pv._luma_byte((255, 255, 255)), 255)
        self.assertEqual(pv._luma_byte((255, 0, 0)), 54)
        self.assertEqual(pv._luma_byte((0, 255, 0)), 182)
        self.assertEqual(pv._luma_byte((0, 0, 255)), 18)
        self.assertEqual(pv._luma_byte((100, 150, 200)), 142)

    def test_luma_byte_negative_invalid_input(self):
        with self.assertRaises(TypeError):
            pv._luma_byte(123)
        with self.assertRaises(TypeError):
            pv._luma_byte(("a", "b", "c"))


class TestMaterialParams(unittest.TestCase):
    def test_material_params_defaults(self):
        # glTF material omitting factors uses spec defaults:
        # baseColorFactor=[1,1,1,1], metallicFactor=1, roughnessFactor=1
        base, metal, rough = pv.material_params({})
        self.assertEqual(base, [1000, 1000, 1000, 1000])
        self.assertEqual(metal, 1000)
        self.assertEqual(rough, 1000)

        base, metal, rough = pv.material_params({"pbrMetallicRoughness": {}})
        self.assertEqual(base, [1000, 1000, 1000, 1000])
        self.assertEqual(metal, 1000)
        self.assertEqual(rough, 1000)

    def test_material_params_custom(self):
        mat = {
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.2, 0.4, 0.6, 0.8],
                "metallicFactor": 0.1,
                "roughnessFactor": 0.5,
            }
        }
        base, metal, rough = pv.material_params(mat)
        self.assertEqual(base, [200, 400, 600, 800])
        self.assertEqual(metal, 100)
        self.assertEqual(rough, 500)

    def test_material_params_negative_malformed_base_color(self):
        with self.assertRaisesRegex(ValueError, "baseColorFactor must have 4 components"):
            pv.material_params({"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1]}})
        with self.assertRaisesRegex(ValueError, "baseColorFactor must have 4 components"):
            pv.material_params({"pbrMetallicRoughness": {"baseColorFactor": "invalid"}})


class TestCheckMaterial(unittest.TestCase):
    def test_check_material_passing_gold_metal(self):
        mat = {
            "name": "gold_metal",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.9, 0.8, 0.5, 1.0],
                "metallicFactor": 1.0,
                "roughnessFactor": 0.3,
            },
        }
        metrics, failed = pv.check_material(mat, index=0)
        self.assertEqual(failed, [])
        self.assertEqual(
            metrics,
            {
                "name": "gold_metal",
                "base_srgb_bytes": [243, 231, 188],
                "metal_permille": 1000,
                "rough_permille": 300,
                "rough_byte": 77,
                "luma_byte": 230,
                "failed": [],
            },
        )

    def test_check_material_passing_dielectric(self):
        mat = {
            "name": "smooth_grey",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.5, 0.5, 0.5, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.5,
            },
        }
        metrics, failed = pv.check_material(mat, index=0)
        self.assertEqual(failed, [])
        self.assertEqual(
            metrics,
            {
                "name": "smooth_grey",
                "base_srgb_bytes": [188, 188, 188],
                "metal_permille": 0,
                "rough_permille": 500,
                "rough_byte": 128,
                "luma_byte": 188,
                "failed": [],
            },
        )

    def test_check_material_flags_out_of_limits(self):
        # 1. param_range
        mat_range = {
            "name": "bad_range",
            "pbrMetallicRoughness": {
                "baseColorFactor": [1.5, 0.0, 0.0, 1.0],
                "metallicFactor": 1.2,
                "roughnessFactor": -0.1,
            },
        }
        metrics, failed = pv.check_material(mat_range)
        self.assertIn("param_range", failed)

        # 2. rough_floor
        mat_rough = {
            "name": "smooth_mirror",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.5, 0.5, 0.5, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.0,
            },
        }
        metrics, failed = pv.check_material(mat_rough)
        self.assertIn("rough_floor", failed)

        # 3. albedo_range (too dark & too bright)
        mat_dark = {
            "name": "dark_dielectric",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.01, 0.01, 0.01, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.5,
            },
        }
        metrics, failed = pv.check_material(mat_dark)
        self.assertIn("albedo_range", failed)

        mat_bright = {
            "name": "bright_dielectric",
            "pbrMetallicRoughness": {
                "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.5,
            },
        }
        metrics, failed = pv.check_material(mat_bright)
        self.assertIn("albedo_range", failed)

        # 4. metal_mid
        mat_mid = {
            "name": "semi_metal",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.5, 0.5, 0.5, 1.0],
                "metallicFactor": 0.5,
                "roughnessFactor": 0.5,
            },
        }
        metrics, failed = pv.check_material(mat_mid)
        self.assertIn("metal_mid", failed)

        # 5. f0_low
        mat_f0 = {
            "name": "dark_metal",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.05, 0.05, 0.05, 1.0],
                "metallicFactor": 1.0,
                "roughnessFactor": 0.5,
            },
        }
        metrics, failed = pv.check_material(mat_f0)
        self.assertIn("f0_low", failed)

    def test_check_material_negative_malformed_input(self):
        with self.assertRaises(ValueError):
            pv.check_material({"pbrMetallicRoughness": {"baseColorFactor": [1, 2]}})


class TestCheckAlbedoPng(unittest.TestCase):
    def test_check_albedo_png_pass(self):
        # 4x4 RGBA image with pixel RGB values = 128 (in [30, 240])
        raw_png = orm_pack.encode_png(4, 4, b"\x80\x80\x80\xff" * 16)
        metrics, failed = pv.check_albedo_png(raw_png)
        self.assertEqual(failed, [])
        self.assertEqual(
            metrics,
            {
                "width": 4,
                "height": 4,
                "pixels": 16,
                "out_of_range_px": 0,
                "out_of_range_ppm": 0,
                "channels": 4,
            },
        )

    def test_check_albedo_png_refuses_too_dark_and_too_bright(self):
        # Too dark: pixel RGB values = 5 (< 30)
        dark_png = orm_pack.encode_png(4, 4, b"\x05\x05\x05\xff" * 16)
        metrics_dark, failed_dark = pv.check_albedo_png(dark_png)
        self.assertEqual(failed_dark, ["albedo_texture"])
        self.assertEqual(metrics_dark["out_of_range_px"], 16)
        self.assertEqual(metrics_dark["out_of_range_ppm"], 1000000)

        # Too bright: pixel RGB values = 250 (> 240)
        bright_png = orm_pack.encode_png(4, 4, b"\xfa\xfa\xfa\xff" * 16)
        metrics_bright, failed_bright = pv.check_albedo_png(bright_png)
        self.assertEqual(failed_bright, ["albedo_texture"])
        self.assertEqual(metrics_bright["out_of_range_px"], 16)
        self.assertEqual(metrics_bright["out_of_range_ppm"], 1000000)

    def test_check_albedo_png_negative_corrupt_bytes(self):
        with self.assertRaises(orm_pack.OrmError):
            pv.check_albedo_png(b"not a png image bytes")


class TestValidateGlb(unittest.TestCase):
    def test_validate_glb_minimal_pass(self):
        mat_def = {
            "name": "valid_mat",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.5, 0.5, 0.5, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.5,
            },
        }
        raw_glb = make_minimal_glb([mat_def])
        metrics, failed = pv.validate_glb(raw_glb)
        self.assertEqual(failed, set())
        self.assertEqual(len(metrics), 1)
        self.assertEqual(
            metrics[0],
            {
                "name": "valid_mat",
                "base_srgb_bytes": [188, 188, 188],
                "metal_permille": 0,
                "rough_permille": 500,
                "rough_byte": 128,
                "luma_byte": 188,
                "failed": [],
            },
        )

    def test_validate_glb_negative_non_list_materials(self):
        raw_glb = make_minimal_glb("not a list")
        with self.assertRaises(glb_core.GlbError) as ctx:
            pv.validate_glb(raw_glb)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")
        self.assertIn("materials is not a list", str(ctx.exception))

    def test_validate_glb_negative_malformed_material(self):
        raw_glb = make_minimal_glb([{"pbrMetallicRoughness": {"baseColorFactor": [1]}}])
        with self.assertRaises(glb_core.GlbError) as ctx:
            pv.validate_glb(raw_glb)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")
        self.assertIn("material 0:", str(ctx.exception))

    def test_validate_glb_refuses_no_materials_in_cli(self):
        td = tempfile.TemporaryDirectory()
        try:
            raw_glb = make_minimal_glb([])
            glb_path = os.path.join(td.name, "nomat.glb")
            with open(glb_path, "wb") as fh:
                fh.write(raw_glb)

            metrics, failed = pv.validate_glb(raw_glb)
            self.assertEqual(metrics, [])
            self.assertEqual(failed, set())

            # When run via CLI, zero materials yields status INCOMPLETE_ZERO_CHECKS / RC_EMPTY (3)
            rc, rec = pv.run(["--glb", glb_path])
            self.assertEqual(rc, pv.RC_EMPTY)
            self.assertEqual(rec["status"], "INCOMPLETE_ZERO_CHECKS")
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
