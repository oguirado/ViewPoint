"""Protected bake gate controls on embedded GLB bytes and registered observations."""
import json
from pathlib import Path
import struct
import tempfile
import unittest

from asset_factory import domains
from test_d18_uv_gates import Store, with_tangents
from tools.blender import bake, look_bake as support, glb_io, uv_projection
from tools.input.evidence import bytes_digest


SIZE = 64
START = 16
END = 24
CLOTH = (12, 15, 18)
FACE = (148, 132, 114)
SAMPLES = {y * SIZE + x for y in range(START, END) for x in range(START, END)
           if x + y + 1 <= START + END}
FACE_TEXELS = {y * SIZE + x for y in range(17, 19) for x in range(17, 19)}
SHADOW_TEXELS = {18 * SIZE + 20, 19 * SIZE + 20}
RELIEF_TEXEL = 16 * SIZE + 17
LINEAR_ORM = (220, 153, 0, 255)
PNG = support.upstream("orm_pack")


def sheet(normal=(0, 0, 65536)):
    glb = support.upstream("glb_core")
    prim = glb.Prim("sheet", "sheet", glb.IDENTITY_Q16,
                    [(0, 0, 0), (1000000, 0, 0), (0, 1000000, 0), (250000, 250000, 0)],
                    [0, 1, 3, 1, 2, 3, 2, 0, 3],
                    [(16384, 16384), (24576, 16384), (16384, 24576), (18432, 18432)],
                    [normal] * 4, "stock")
    return with_tangents(glb.write_glb(glb.Scene([prim])), [-1] * 4, (1, 0, 0))


def fixture(*, high=None, relief=False, measured_orm=True, shadow_rgb=CLOTH, shadow_attenuation=255):
    raw = sheet()
    high = raw if high is None else high
    store = Store()
    high_digest = bytes_digest(high)
    geometry = bytes_digest(raw)
    pose = bytes_digest(b"look-fixture-pose")
    selected = {}
    registrations = {}
    for ordinal, (view, shift) in enumerate((("front", 0), ("face", 5))):
        raster = bytearray(bytes(CLOTH) * SIZE * SIZE)
        # Registered cameras have equal depth and different optical centres.
        # The plane projects to x = atlas_x - 8 + shift, y = 55 - atlas_y.
        for i in FACE_TEXELS | SHADOW_TEXELS:
            row, column = divmod(i, SIZE)
            offset = 3 * ((55 - row) * SIZE + column - 8 + shift)
            raster[offset:offset + 3] = bytes(FACE if i in FACE_TEXELS else shadow_rgb)
        rgb = store.put("uv-source-rgb", bytes(raster))
        selected[view] = {
            "view": view, "id": view + "-frame", "index": ordinal, "pts": [ordinal, 1],
            "pose_digest": pose, "frame_digest": rgb,
            "camera": {
                "rotation": [[[v, 1] for v in axis] for axis in ((1, 0, 0), (0, 0, -1), (0, 1, 0))],
                "translation_um": [[shift * 125000, 1], [0, 1], [3000000, 1]],
                "intrinsics": [[24, 1], [24, 1], [8, 1], [40, 1]],
            },
            "crop": {"rect": [0, 0, SIZE, SIZE], "size": [SIZE, SIZE]},
            "crop_transform": [[1, 1], [1, 1], [0, 1], [0, 1]],
        }
        registrations[view] = {
            "parts_digest": store.put("uv-source-parts", b"\1" * SIZE**2),
            "confidence_digest": store.put("uv-source-confidence", b"\xff" * SIZE**2),
        }
    selection = store.put("uv-input-selection", {
        "schema": "af.input-frame-selection/v1", "source_digest": bytes_digest(b"fixture capture"),
        "plan_digest": bytes_digest(b"fixture plan"), "stream": 0, "invented_details": [],
        "raster": [SIZE, SIZE], "selected": selected})
    mask = bytes(i in SAMPLES for i in range(SIZE * SIZE))
    projection = store.put("uv-projection-layout", {
        "schema": "af.uv-projection-layout/v1", "mesh_digest": geometry, "frame": domains.FRAME,
        "size": [SIZE, SIZE], "parts": [1, 1, 1], "pose_digest": pose,
        "selection_digest": selection, "required_views": sorted(selected), "views": registrations,
        "support_digest": store.put("uv-atlas-parts", mask), "max_faces": 4, "max_work": 100000,
        "max_mesh_bytes": 100000, "min_confidence_ppm": 1, "min_cos_incidence": [1, 2],
        "min_source_pixels_per_texel": [1, 4], "max_view_disagreement_ppm": 0})
    sampler = {"filter": "bilinear", "wrap": "clamp-to-edge", "mip": "linear-box", "level": 1}
    gutters = []
    for y in range(12, 28):
        for x in range(12, 28):
            i = y * SIZE + x
            if i not in SAMPLES:
                j = min(SAMPLES, key=lambda v: ((v // SIZE - y)**2 + (v % SIZE - x)**2, v))
                gutters.append([i, j])
    atlas = store.put("uv-layout", {
        "schema": "af.uv-layout/v1", "mesh_digest": geometry, "frame": domains.FRAME,
        "size": [SIZE, SIZE], "charts": [1, 1, 1], "orientation": [1, 1, 1], "color_space": "linear",
        "gutters": gutters, "sampler": sampler, "stretch": [2, 1], "density": [8, 1000000],
        "density_factor": [6, 5], "min_packing_ppm": 1, "max_faces": 4, "max_work": 100000,
        "max_mesh_bytes": 100000})
    observation, rgba = uv_projection.project_registered(raw, layout_digest=projection, read_checked=store.read)
    if observation["source_rc"]:
        raise AssertionError(observation)
    assert all(rgba[4*i:4*i+3] == bytes(FACE) for i in FACE_TEXELS), observation["texels"][:12]
    # The analytic reference is independently authored from the fixture design.
    reference = bytearray(SIZE**2 * 4)
    for i in SAMPLES:
        reference[4*i:4*i+4] = bytes((*(FACE if i in FACE_TEXELS else CLOTH), 255))
    atten = bytes(shadow_attenuation if i in SHADOW_TEXELS else 255 if i in SAMPLES else 0
                  for i in range(SIZE**2))
    mask = bytes(1 if i in FACE_TEXELS else 2 if i in SHADOW_TEXELS else 0 for i in range(SIZE**2))
    lights = [bytes((255 if i % SIZE < 20 else 64) if i in SAMPLES else 0 for i in range(SIZE**2)),
              bytes((64 if i % SIZE < 20 else 255) if i in SAMPLES else 0 for i in range(SIZE**2))]
    from test_d18_geo_certificate import Gates
    geo = Gates()
    geo.setUp()
    geo.raw = high
    geo.policy.update(source=high_digest, reference_length_um=900000, face_budgets=[3])
    geo.policy["shells"]["0:0"].update(loops=1, orientation=0)
    geo.intent["extent_ranges_um"] = [[900000, 2000000], [0, 2000000], [0, 2000000]]
    geo.policy["intent"] = geo.store.add("raw-intent", geo.intent)
    geo.patches = {"face": ["0:0", [0]], "left_hand": ["0:0", [1]], "right_hand": ["0:0", [2]]}
    geometry_result = geo.measure(raw)
    assert geometry_result["measurement_rc"] == 0, geometry_result["reason_codes"]
    geometry_receipt = store.put("look-geometry-receipt", geometry_result)
    witness = {
        "schema": "af.look-bake-witness/v1", "mesh_digest": geometry, "high_digest": high_digest,
        "atlas_digest": atlas, "projection_digest": projection, "geometry_receipt_digest": geometry_receipt,
        "protected_regions_digest": store.put("look-protected-regions", mask),
        "albedo_reference_digest": store.put("look-independent-albedo", bytes(reference)),
        "attenuation_digest": store.put("look-capture-attenuation", atten), "attenuation_floor_byte": 32,
        "light_digests": [store.put("look-illumination", light) for light in lights],
        "tile_px": 4, "face_max_ppm": 8000, "face_tile_ppm": 4000, "shadow_max_ppm": 300}
    if relief:
        # In glTF the independent high normal leans toward -Y. The low frame
        # has N=+Z and T=+X, so cross(N,T) points toward +Y.
        witness["normal_probe"] = {"texel": RELIEF_TEXEL, "cross_nt_sign": -1}
    if measured_orm:
        witness["orm_reference_digest"] = store.put("look-linear-orm-reference",
                                                    bytes(LINEAR_ORM) * SIZE**2)
    witness = store.put("look-bake-witness", witness)
    return raw, store, witness, rgba, gutters


def with_gutters(rgba, gutters):
    out = bytearray(rgba)
    for dst, src in gutters:
        out[4*dst:4*dst+4] = rgba[4*src:4*src+4]
    return bytes(out)


class LookBakeControls(unittest.TestCase):
    def setUp(self):
        self.raw, self.store, self.witness, rgba, self.gutters = fixture()
        self.maps = {
            "base_color": with_gutters(rgba, self.gutters),
            "normal": with_gutters(b"".join(bytes((128, 128, 255, 255)) if i in SAMPLES else bytes(4)
                                            for i in range(SIZE**2)), self.gutters),
            "orm": with_gutters(b"".join(bytes(LINEAR_ORM) if i in SAMPLES else bytes(4)
                                         for i in range(SIZE**2)), self.gutters)}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "textured.glb"

    def check(self, *, role=None, texels=(), rgba=None):
        if role:
            pixels = bytearray(self.maps[role])
            for i in texels:
                pixels[4*i:4*i+4] = rgba
            self.maps[role] = with_gutters(bytes(pixels), self.gutters) if set(texels) <= SAMPLES else bytes(pixels)
        self.path.write_bytes(support.embed_look_maps(self.raw, {
            name: PNG.encode_png(SIZE, SIZE, pixels) for name, pixels in self.maps.items()}))
        return self.verify()

    def mips(self):
        return {role: b"".join(support.look_mips(rgba, SIZE, 1, role)[1:])
                for role, rgba in self.maps.items()}

    def verify(self, **overrides):
        args = dict(high=self.raw, geometry=self.raw, witness_digest=self.witness,
                    read_checked=self.store.read, mip_bytes=self.mips())
        return bake.verify_look_bake(self.path, **(args | overrides))

    def test_valid_dark_cloth_and_distinct_bindings(self):
        rec = self.check()
        self.assertEqual(rec["rc"], 0, rec)
        self.assertEqual(rec["map_encodings"], {"base_color": "srgb", "normal": "linear", "orm": "linear"})
        self.assertEqual(rec["relighting"]["worst_face_tile_upper_ppm"], 0)
        self.assertEqual(len(rec["projection"]["texels"]), len(SAMPLES))
        for texel in rec["projection"]["texels"]:
            self.assertEqual({sample["view"] for sample in texel["samples"]}, {"front", "face"})
            self.assertFalse(texel["inferred"])
            self.assertEqual(texel["occlusion"], "visible")

    def test_poisoned_gutter_refuses(self):
        rec = self.check(role="orm", texels=[self.gutters[0][0]], rgba=(1, 2, 3, 255))
        self.assertEqual((rec["rc"], rec["reason_codes"]), (5, ["uv_gutter_pixels_changed"]), rec)

    def test_stationary_baked_shadow_refuses_under_changed_light(self):
        rec = self.check(role="base_color", texels=SHADOW_TEXELS, rgba=(2, 3, 4, 255))
        self.assertEqual((rec["rc"], rec["reason_codes"]), (5, ["look_stationary_baked_shadow"]), rec)
        self.assertGreater(rec["relighting"]["changed_shadow_max_upper_ppm"], 300)

    def test_localized_face_smear_refuses(self):
        rec = self.check(role="base_color", texels=FACE_TEXELS, rgba=(*CLOTH, 255))
        self.assertEqual((rec["rc"], rec["reason_codes"]), (5, ["look_protected_face_changed"]), rec)
        self.assertGreater(rec["relighting"]["worst_face_tile_upper_ppm"], 4000)

    def test_srgb_checker_mip_is_filtered_in_linear_light(self):
        raster = bytes((0, 0, 0, 255, 255, 255, 255, 255)) * 2
        self.assertEqual(support.look_mips(raster, 2, 1, "base_color")[1], bytes((188, 188, 188, 255)))
        self.assertEqual(support.look_mips(raster, 2, 1, "orm")[1], bytes((128, 128, 128, 255)))

    def test_material_role_swizzle_refuses_after_reopening(self):
        self.assertEqual(self.check()["rc"], 0)
        doc, binary = glb_io.decode_glb(self.path.read_bytes())
        doc["materials"][0]["normalTexture"]["index"] = 0
        self.path.write_bytes(glb_io.encode_glb(doc, binary))
        rec = self.verify()
        self.assertEqual((rec["rc"], rec["reason_codes"]), (5, ["look_texture_role_alias"]), rec)

    def test_poisoned_srgb_mip_refuses(self):
        self.assertEqual(self.check()["rc"], 0)
        mips = {role: support.look_mips(rgba, SIZE, 1, role)[1] for role, rgba in self.maps.items()}
        base = mips["base_color"]
        mips["base_color"] = b"\xff" + base[1:]
        rec = self.verify(mip_bytes=mips)
        self.assertEqual((rec["rc"], rec["reason_codes"]), (5, ["look_mip_pixels_changed"]), rec)

    def test_debake_requires_attenuation_floor(self):
        rgb = bytes((6, 6, 6, 255))
        self.assertEqual(support.debake(rgb, {0}, b"\x80", 32), bytes((12, 12, 12, 255)))
        with self.assertRaisesRegex(support.Refusal, "look_attenuation_unresolved"):
            support.debake(rgb, {0}, b"\x1f", 32)

    def test_missing_required_observation_refuses(self):
        self.assertEqual(self.check()["rc"], 0)
        witness = json.loads(self.store.read("look-bake-witness", self.witness))
        projection = json.loads(self.store.read("uv-projection-layout", witness["projection_digest"]))
        del projection["views"]["face"]
        witness["projection_digest"] = self.store.put("uv-projection-layout", projection)
        pin = self.store.put("look-bake-witness", witness)
        rec = self.verify(witness_digest=pin)
        self.assertEqual((rec["rc"], rec["reason_codes"]), (3, ["uv_required_view_missing"]), rec)

    def test_linear_normal_and_orm_invalid_values_refuse(self):
        i = min(SAMPLES)
        for role, bad, reason in (("normal", (128, 128, 128, 255), "look_linear_normal_invalid"),
                                   ("orm", (220, 0, 0, 255), "look_linear_orm_invalid")):
            with self.subTest(role=role):
                original = self.maps[role]
                rec = self.check(role=role, texels=[i], rgba=bad)
                self.assertEqual((rec["rc"], rec["reason_codes"]), (5, [reason]), rec)
                self.maps[role] = original

    def test_unprotected_observed_texel_still_binds_to_source(self):
        i = min(SAMPLES - FACE_TEXELS - SHADOW_TEXELS)
        rec = self.check(role="base_color", texels=[i], rgba=(100, 100, 100, 255))
        self.assertEqual((rec["rc"], rec["reason_codes"]), (5, ["look_source_colour_drift"]), rec)

    def test_wrong_geometry_and_unchanged_light_cannot_qualify(self):
        self.assertEqual(self.check()["rc"], 0)
        rec = self.verify(high=b"wrong bytes")
        self.assertEqual((rec["rc"], rec["reason_codes"]), (4, ["look_subject_drift"]), rec)
        witness = json.loads(self.store.read("look-bake-witness", self.witness))
        witness["light_digests"][1] = witness["light_digests"][0]
        pin = self.store.put("look-bake-witness", witness)
        rec = self.verify(witness_digest=pin)
        self.assertEqual((rec["rc"], rec["reason_codes"]), (3, ["look_changed_light_missing"]), rec)

    def test_worker_readback_rejects_uv_drift(self):
        look = bake._look_inputs(self.raw, self.raw, witness_digest=self.witness, read_checked=self.store.read)
        doc, binary = glb_io.decode_glb(self.raw)
        uv = doc["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]
        accessor = doc["accessors"][uv]
        view = doc["bufferViews"][accessor["bufferView"]]
        data = bytearray(binary)
        struct.pack_into("<f", data, view.get("byteOffset", 0) + accessor.get("byteOffset", 0), 0.251)
        with self.assertRaisesRegex(support.Refusal, "look_worker_atlas_drift"):
            bake._check_preserved_atlas(glb_io.encode_glb(doc, bytes(data)), look)

    def test_atlas_cap(self):
        res = bake.bake_bundle(self.path, self.path, self.path, res=2048, look_scope={})
        self.assertEqual((res["rc"], res["reason_codes"]), (2, ["policy_invalid"]))


if __name__ == "__main__":
    unittest.main()
