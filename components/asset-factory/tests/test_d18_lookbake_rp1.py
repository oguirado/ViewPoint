"""LOOK repair regressions on source captures, Blender output and stored sidecars."""
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import test_n18_lookbake_materials as fixture_data
from tools.blender import bake, look_bake


class LookBakeRepair(unittest.TestCase):
    def test_rgb8_normals_require_positive_z_and_unit_length(self):
        control = fixture_data.LookBakeControls()
        control.setUp()
        self.addCleanup(control.doCleanups)
        for normal in ((128, 128, 0, 255), (255, 128, 128, 255), (250, 250, 250, 255)):
            with self.subTest(normal=normal):
                receipt = control.check(role="normal", texels={min(fixture_data.SAMPLES)}, rgba=normal)
                self.assertEqual(receipt["reason_codes"], ["look_linear_normal_invalid"], receipt)
        receipt = control.check(role="normal", texels={min(fixture_data.SAMPLES)}, rgba=(128, 128, 255, 255))
        self.assertEqual(receipt["rc"], 0, receipt)

    def test_srgb_encoded_orm_and_absent_or_gamma_mips_refuse(self):
        control = fixture_data.LookBakeControls()
        control.setUp()
        self.addCleanup(control.doCleanups)
        receipt = control.check(role="orm", texels=fixture_data.SAMPLES, rgba=(239, 203, 0, 255))
        self.assertEqual(receipt["reason_codes"], ["look_linear_orm_reference_changed"], receipt)
        receipt = control.check(role="orm", texels=fixture_data.SAMPLES, rgba=fixture_data.LINEAR_ORM)
        self.assertEqual(receipt["rc"], 0, receipt)
        for incomplete in (None, {k: v for k, v in control.mips().items() if k != "orm"}):
            receipt = control.verify(mip_bytes=incomplete)
            self.assertEqual(receipt["reason_codes"], ["look_mip_bytes_missing"], receipt)
        gamma = b"".join(look_bake.look_mips(control.maps["base_color"], fixture_data.SIZE, 1, "orm")[1:])
        self.assertNotEqual(gamma, control.mips()["base_color"])
        receipt = control.verify(mip_bytes={**control.mips(), "base_color": gamma})
        self.assertEqual(receipt["reason_codes"], ["look_mip_pixels_changed"], receipt)

    def test_shadowed_source_is_recovered_only_with_observed_attenuation(self):
        with TemporaryDirectory() as directory:
            mesh = Path(directory) / "look.glb"
            for attenuation, captured in ((128, (6, 8, 10)), (64, (3, 4, 5))):
                for admitted in (True, False):
                    with self.subTest(attenuation=attenuation, admitted=admitted):
                        raw, store, pin, _, _ = fixture_data.fixture(
                            shadow_rgb=captured, shadow_attenuation=attenuation if admitted else 255)
                        look = bake._look_inputs(raw, raw, witness_digest=pin, read_checked=store.read)
                        expected = fixture_data.CLOTH if admitted else captured
                        for index in fixture_data.SHADOW_TEXELS:
                            self.assertEqual(look["base"][4*index:4*index + 4], bytes((*expected, 255)))
                        size, samples, gutters = look["size"], look["samples"], look["gutters"]
                        maps = {"base_color": look["base"]}
                        for role, value in (("normal", (128, 128, 255, 255)), ("orm", fixture_data.LINEAR_ORM)):
                            maps[role] = look_bake.chart_pixels(bytes(value) * size**2, samples, gutters)
                        images = {role: fixture_data.PNG.encode_png(size, size, rgba) for role, rgba in maps.items()}
                        mesh.write_bytes(look_bake.embed_look_maps(raw, images))
                        receipt = bake.verify_look_bake(
                            mesh, high=raw, geometry=raw, witness_digest=pin, read_checked=store.read,
                            mip_bytes={role: look_bake.look_mips(rgba, size, 1, role)[1]
                                       for role, rgba in maps.items()})
                        self.assertEqual(receipt["rc"], 0 if admitted else 5, receipt)
                        self.assertEqual(receipt["reason_codes"], [] if admitted else ["look_stationary_baked_shadow"])

    def test_real_cpu_bundle_replay_and_controls(self):
        # Independent high shading normal from the plane z=(3/4)y. It has
        # N=(0,-3/5,4/5). Geometry stays on the registered low surface.
        high = fixture_data.sheet((0, -39322, 52429))
        raw, store, pin, _, gutters = fixture_data.fixture(high=high, relief=True, measured_orm=False)
        size = fixture_data.SIZE
        with TemporaryDirectory() as directory:
            root = Path(directory)
            low_path, high_path = root / "low.glb", root / "high.glb"
            low_path.write_bytes(raw)
            high_path.write_bytes(high)
            bundle = root / "bundle"
            result = bake.bake_bundle(high_path, low_path, bundle, res=size, margin_px=2, blender_threads=1,
                                      look_scope={"witness_digest": pin, "read_checked": store.read})
            self.assertEqual(result["rc"], 0, result)
            digest = result["receipt_sha256"]
            check = bake.verify_bundle(bundle, expected_receipt_sha256=digest, read_checked=store.read)
            self.assertEqual((check["rc"], check["scope"]), (0, "protected-look-replay"), check)
            self.assertEqual(bake.verify_bundle(bundle, expected_receipt_sha256=digest)["reason_codes"],
                             ["look_reader_missing"])
            self.assertEqual(bake.verify_bundle(bundle, expected_receipt_sha256=digest, production=True)
                             ["reason_codes"], ["factory_manifest_schema_unassigned"])
            stored = json.loads((bundle / "look_receipt.json").read_bytes())
            exported = (bundle / "textured.glb").read_bytes()
            self.assertEqual(stored["subject_digest"], "sha256:" + bake.sha(exported))
            self.assertEqual(stored["normal_probe"]["tangent_w"], -1)
            self.assertEqual(stored["normal_probe"]["expected_y_sign"], 1)
            self.assertEqual(stored["worker_map_color_spaces"], {
                "albedo": "sRGB", "normal": "Non-Color", "ao": "Non-Color",
                "roughness": "Non-Color", "metal": "Non-Color",
            })
            embedded = look_bake.read_look_maps(exported, raw)
            normal_rgba = look_bake.decode_look_png(embedded["normal"], size)
            green = normal_rgba[4 * fixture_data.RELIEF_TEXEL + 1]
            self.assertGreater(green, 128)
            mutant = root / "mutant.glb"
            genuine_mips = {role: (bundle / (role + ".mips.rgba")).read_bytes() for role in embedded}
            for role, positions, replacement, reason in (
                ("orm", {gutters[0][0]}, (1, 2, 3, 255), "uv_gutter_pixels_changed"),
                ("base_color", fixture_data.SHADOW_TEXELS, (2, 3, 4, 255), "look_stationary_baked_shadow"),
                ("base_color", {min(fixture_data.FACE_TEXELS)}, (*fixture_data.CLOTH, 255),
                 "look_protected_face_changed"),
                ("normal", {fixture_data.RELIEF_TEXEL}, None, "look_normal_handedness_changed"),
            ):
                with self.subTest(reason=reason):
                    rgba = bytearray(look_bake.decode_look_png(embedded[role], size))
                    for index in positions:
                        start = 4 * index
                        if replacement is None:
                            rgba[start + 1] = 255 - rgba[start + 1]
                        else:
                            rgba[start:start + 4] = bytes(replacement)
                    if positions <= fixture_data.SAMPLES:
                        rgba = fixture_data.with_gutters(bytes(rgba), gutters)
                    rgba = bytes(rgba)
                    images = {**embedded, role: fixture_data.PNG.encode_png(size, size, rgba)}
                    mips = {**genuine_mips, role: look_bake.look_mips(rgba, size, 1, role)[1]}
                    mutant.write_bytes(look_bake.embed_look_maps(raw, images))
                    receipt = bake.verify_look_bake(
                        mutant, high=high, geometry=raw, witness_digest=pin, read_checked=store.read,
                        mip_bytes=mips)
                    self.assertEqual((receipt["rc"], receipt["reason_codes"]), (5, [reason]), receipt)

            gamma = look_bake.look_mips(look_bake.decode_look_png(embedded["base_color"], size), size, 1, "orm")[1]
            self.assertNotEqual(gamma, genuine_mips["base_color"])
            worker = json.loads((bundle / "bake_receipt.json").read_bytes())
            worker["worker"]["map_color_spaces"]["roughness"] = "sRGB"
            for filename, payload, reason in (
                ("base_color.mips.rgba", gamma, "look_mip_pixels_changed"),
                ("bake_receipt.json", bake.canonical(worker).encode("utf-8"), "look_worker_data_color_space_invalid"),
            ):
                with self.subTest(reason=reason):
                    changed = root / reason
                    shutil.copytree(bundle, changed)
                    (changed / filename).write_bytes(payload)
                    receipt_path = changed / "bundle_receipt.json"
                    index = json.loads(receipt_path.read_bytes())
                    next(entry for entry in index["files"] if entry["path"] == filename)["sha256"] = bake.sha(payload)
                    receipt_path.write_bytes(bake.canonical(index).encode("utf-8"))
                    replay = bake.verify_bundle(changed, expected_receipt_sha256=bake.sha(receipt_path.read_bytes()),
                                                read_checked=store.read)
                    self.assertEqual((replay["rc"], replay["reason_codes"]), (5, [reason]), replay)
