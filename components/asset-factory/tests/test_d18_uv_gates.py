import json
import struct
import unittest

from asset_factory.domains import FRAME, pair
from tools.blender import uv, uv_projection, uv_quality
from tools.blender.bake_support import upstream
from tools.input.evidence import InputIssue, bytes_digest, canonical_bytes


class Store:
    def __init__(self):
        self.records = {}

    def put(self, role, content):
        data = content if isinstance(content, bytes) else canonical_bytes(content)
        digest = bytes_digest(data)
        self.records[role, digest] = data
        return digest

    def read(self, role, digest):
        return self.records.get((role, digest))


def mesh(layout, rear=False, outward=True, front_occluder=False, tilt=(1000000, 0)):
    glb = upstream("glb_core")
    uv_corners = [(x * 4096, y * 4096) for x, y in layout]
    indices = [0, 1, 2] if outward or rear else [0, 2, 1]
    prims = [glb.Prim("front", "front", glb.IDENTITY_Q16,
                      [(0, 0, 0), (1000000, 0, 0), (0, *tilt)], indices, uv_corners)]
    if rear:
        rear_uv = [(x * 4096, y * 4096) for x, y in [(10, 10), (14, 10), (10, 14)]]
        prims.append(glb.Prim("back", "back", glb.IDENTITY_Q16,
                              [(0, 0, -500000), (0, 1000000, -500000), (1000000, 0, -500000)]
                              if outward else
                              [(0, 0, -500000), (1000000, 0, -500000), (0, 1000000, -500000)],
                              [0, 1, 2], rear_uv))
    if front_occluder:
        prims.append(glb.Prim("occluder", "occluder", glb.IDENTITY_Q16,
                              [(0, 0, 500000), (2000000, 0, 500000), (0, 2000000, 500000)],
                              [0, 1, 2], [(4096, 9 * 4096), (5 * 4096, 9 * 4096), (4096, 13 * 4096)]))
    return glb.write_glb(glb.Scene(prims))


def texels(left, top):
    return {y * 16 + x for y in range(top, top + 4) for x in range(left, left + 4)
            if x + y + 1 <= left + top + 4}


def with_tangents(raw, handedness, tangent=(0, 0, -1)):
    doc, binary = upstream("glb_core")._split(raw)
    tangent_data = b"".join(struct.pack("<ffff", *tangent, w) for w in handedness)
    doc["accessors"].append({"bufferView": len(doc["bufferViews"]), "componentType": 5126,
                             "type": "VEC4", "count": len(handedness)})
    doc["meshes"][0]["primitives"][0]["attributes"]["TANGENT"] = len(doc["accessors"]) - 1
    doc["bufferViews"].append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(tangent_data)})
    binary += tangent_data
    doc["buffers"][0]["byteLength"] = len(binary)
    encoded = json.dumps(doc, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 4)
    return (struct.pack("<4sIIII", b"glTF", 2, 28 + len(encoded) + len(binary), len(encoded), 0x4E4F534A)
            + encoded + struct.pack("<II", len(binary), 0x004E4942) + binary)


class AtlasTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.layout = [(4, 4), (8, 4), (4, 8)]
        self.mips = [bytes([60, 90, 20, 255]) * 256, bytes([60, 90, 20, 255]) * 64]
        self.sampler = {"filter": "bilinear", "wrap": "clamp-to-edge", "mip": "linear-box", "level": 1}

    def run_atlas(self, layout=None, *, mips=None, sampler=None, gutters=True, orientation=1,
                  color_space="linear", handedness=None, tangent=None):
        raw = mesh(layout or self.layout)
        if handedness is not None:
            if tangent is None:
                tangent = (0, 1, 0) if orientation == -1 else (1, 0, 0)
            raw = with_tangents(raw, handedness, tangent)
        coverage = texels(4, 4)
        witness = {"schema": "af.uv-layout/v1", "mesh_digest": bytes_digest(raw), "frame": FRAME,
                   "size": [16, 16], "charts": [1], "orientation": [orientation],
                   "gutters": [[i, min(coverage)] for i in range(256) if i not in coverage] if gutters else [],
                   "sampler": self.sampler, "stretch": [2, 1], "density": [4, 1000000],
                   "density_factor": [6, 5], "min_packing_ppm": 1, "max_faces": 8,
                   "max_work": 10000, "max_mesh_bytes": 100000}
        if color_space is not None:
            witness["color_space"] = color_space
        pin = self.store.put("uv-layout", witness)
        return uv.check_uv_quality(raw, witness_digest=pin, read_checked=self.store.read,
                                   sampler=sampler or self.sampler, mips=mips or self.mips)

    def test_exact_atlas_and_mips(self):
        rec = self.run_atlas()
        self.assertEqual(rec["source_rc"], 0, rec)
        self.assertEqual(rec["metrics"]["uv_k2_upper"], [[1, 1], [1, 1]])
        self.assertEqual(rec["metrics"]["density_ratio_lower_ppm"], [[1, 1], [1, 1]])

    def test_stretch_and_density_independent(self):
        stretched = self.run_atlas([(4, 4), (8, 4), (4, 5)])
        dense = self.run_atlas([(4, 4), (12, 4), (4, 12)])
        self.assertIn("uv_stretch_exceeded", stretched["reason_codes"])
        self.assertIn("uv_density_exceeded", dense["reason_codes"])

    def test_single_axis_reflection_and_collapse(self):
        for layout, code in [([(4, 4), (4, 8), (8, 4)], "uv_flip"),
                             ([(4, 4), (5, 4), (6, 4)], "uv_collapsed")]:
            with self.subTest(code=code):
                self.assertIn(code, self.run_atlas(layout)["reason_codes"])

    def test_poisoned_base_gutter_and_mip(self):
        for level in (0, 1):
            bad = self.mips[:]
            # Both coordinates are actual bilinear taps of the chart.
            offset = 4 * (67 if level == 0 else 17)
            bad[level] = bad[level][:offset] + b"\xff" + bad[level][offset + 1:]
            with self.subTest(level=level):
                rec = self.run_atlas(mips=bad)
                self.assertEqual(rec["source_rc"], 5, rec)

    def test_encoded_tail_requires_a_whole_block(self):
        chain = [bytes(64), bytes(16), bytes(16)]
        blocks = {"block_width": 4, "block_height": 4, "block_bytes": 16}
        self.assertEqual(uv_quality.encoded_mip_byte_count([7, 5], chain, **blocks), 96)
        with self.assertRaisesRegex(InputIssue, "uv_encoded_mip_byte_length"):
            uv_quality.encoded_mip_byte_count([7, 5], [*chain[:-1], chain[-1][:-1]], **blocks)

    def test_uv_interiors_cannot_overlap(self):
        first = ((0, 0), (2, 0), (0, 2))
        touches = ((2, 0), (2, 2), (0, 2))
        self.assertFalse(uv_quality.overlaps(first, touches))
        with self.assertRaisesRegex(InputIssue, "uv_overlap"):
            uv_quality.check_nonoverlap([((), first), ((), ((1, 0), (3, 0), (1, 2)))], lambda n: None)

    def test_missing_gutter_and_unpinned_wrap(self):
        self.assertIn("uv_footprint_escape", self.run_atlas(gutters=False)["reason_codes"])
        rec = self.run_atlas(sampler={**self.sampler, "wrap": "repeat"})
        self.assertIn("uv_sampler_drift", rec["reason_codes"])

    def test_explicit_linear_layout_refuses_srgb_and_missing_pin(self):
        for color_space, reason in (("srgb", "uv_color_space_unsupported"), (None, "uv_color_space_missing")):
            with self.subTest(color_space=color_space):
                rec = self.run_atlas(color_space=color_space)
                self.assertEqual(rec["source_rc"], 3, rec)
                self.assertEqual(rec["reason_codes"], [reason])
        self.assertEqual(self.run_atlas()["color_space"], "linear")

    def test_mirrored_chart_uses_tangent_w_at_every_corner(self):
        mirrored = [(4, 4), (4, 8), (8, 4)]
        for handedness, rc, reason in ((None, 3, "uv_mirrored_tangent_missing"),
                                       ([1, -1, 1], 5, "uv_mirrored_tangent_invalid")):
            with self.subTest(handedness=handedness):
                rec = self.run_atlas(mirrored, orientation=-1, handedness=handedness)
                self.assertEqual(rec["source_rc"], rc, rec)
                self.assertEqual(rec["reason_codes"], [reason])
                self.assertEqual(rec["metrics"]["mirrored_faces"], [[1, 1], [1, 1]])
        rec = self.run_atlas(mirrored, orientation=-1, handedness=[1, 1, 1])
        self.assertEqual(rec["source_rc"], 0, rec)
        self.assertEqual(rec["metrics"]["mirrored_faces"], [[1, 1], [1, 1]])

    def test_tangent_direction_agrees_with_face_and_uv(self):
        mirrored = [(4, 4), (4, 8), (8, 4)]
        for tangent in ((0, 0, -1), (1, 0, 0), (0, -1, 0), (0, 0, 0)):
            with self.subTest(tangent=tangent):
                rec = self.run_atlas(mirrored, orientation=-1, handedness=[1, 1, 1], tangent=tangent)
                self.assertEqual((rec["source_rc"], rec["reason_codes"]), (5, ["uv_mirrored_tangent_invalid"]))
        positive = self.run_atlas(handedness=[-1, -1, -1])
        self.assertEqual(positive["source_rc"], 0, positive)
        averaged = self.run_atlas(handedness=[-1, -1, -1], tangent=(1, 0.2, 0))
        self.assertEqual(averaged["source_rc"], 0, averaged)
        outside_floor = self.run_atlas(handedness=[-1, -1, -1], tangent=(1, 0.4, 0))
        self.assertEqual((outside_floor["source_rc"], outside_floor["reason_codes"]),
                         (5, ["uv_tangent_invalid"]))
        off_plane = self.run_atlas(handedness=[-1, -1, -1], tangent=(1, 0, 0.001))
        self.assertEqual((off_plane["source_rc"], off_plane["reason_codes"]),
                         (5, ["uv_tangent_invalid"]))
        reversed_u = self.run_atlas(handedness=[-1, -1, -1], tangent=(-1, 0, 0))
        self.assertEqual((reversed_u["source_rc"], reversed_u["reason_codes"]),
                         (5, ["uv_tangent_invalid"]))

    def test_tangents_reject_wrong_or_mixed_w_at_any_corner(self):
        # With the stored UVs winding positive, increasing tangent-space V
        # points opposite to increasing raster V.
        for handedness in ([1, 1, 1], [-1, 1, -1]):
            with self.subTest(handedness=handedness):
                rec = self.run_atlas(handedness=handedness)
                self.assertEqual((rec["source_rc"], rec["reason_codes"]), (5, ["uv_tangent_invalid"]))


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.front = texels(4, 4)
        self.back = texels(10, 10)
        self.colors = {"front": b"\xd0\x20\x10", "back": b"\x05\x24\xf0"}
        self.pose = bytes_digest(b"registered test pose")
        self.raw = mesh([(4, 4), (8, 4), (4, 8)], rear=True)

    def setup_projection(self, *, views=("front", "back"), wrong=False, eroded=False):
        selected, registrations = {}, {}
        for i, view in enumerate(views):
            rotation = [[1, 0, 0], [0, 0, -1], [0, 1, 0]] if view == "front" else [
                [-1, 0, 0], [0, 0, -1], [0, -1, 0]]
            rgb_pin = self.store.put("uv-source-rgb", self.colors[view] * 64)
            selected[view] = {"view": view, "id": view + "-frame", "index": i, "pts": [i, 25],
                              "camera": {"rotation": [[pair(n) for n in row] for row in rotation],
                                         "translation_um": [[0, 1], [0, 1], [3000000, 1]],
                                         "intrinsics": [[12 if view == "front" else 10, 1]] * 2 +
                                         [[5 if view == "front" else 11, 2], [11, 2]]},
                              "pose_digest": self.pose, "frame_digest": rgb_pin,
                              "crop_transform": [[1, 1], [1, 1], [0, 1], [0, 1]],
                              "crop": {"rect": [0, 0, 8, 8], "size": [8, 8]}}
            part = 1 if view == "front" else 2
            if wrong and view == "back":
                part = 1
            registrations[view] = {
                "parts_digest": self.store.put("uv-source-parts", bytes([part]) * 64),
                "confidence_digest": self.store.put("uv-source-confidence", b"\xff" * 64)}
        selection = {"schema": "af.input-frame-selection/v1", "raster": [8, 8], "selected": selected,
                     "source_digest": bytes_digest(b"synthetic source capture"), "stream": 0,
                     "plan_digest": bytes_digest(b"synthetic view plan"), "invented_details": []}
        if eroded:
            self.front = self.front - {min(self.front)}
        expected = bytes(1 if i in self.front else 2 if i in self.back else 0 for i in range(256))
        return self.store.put("uv-projection-layout", {
            "schema": "af.uv-projection-layout/v1", "size": [16, 16], "parts": [1, 2],
            "mesh_digest": bytes_digest(self.raw), "pose_digest": self.pose,
            "selection_digest": self.store.put("uv-input-selection", selection),
            "required_views": list(views), "views": registrations, "frame": FRAME,
            "support_digest": self.store.put("uv-atlas-parts", expected),
            "min_confidence_ppm": 800000, "min_cos_incidence": [1, 2],
            "min_source_pixels_per_texel": [1, 8], "max_view_disagreement_ppm": 0,
            "max_faces": 8, "max_work": 20000, "max_mesh_bytes": 100000})

    def project(self, pin, candidate=None):
        return uv_projection.project_registered(self.raw, layout_digest=pin, read_checked=self.store.read,
                                                candidate_rgba=candidate)

    def front_observations(self, cameras, *, coincident=False, **policy):
        view_layout = json.loads(self.store.read("uv-projection-layout", self.setup_projection(views=("front",))))
        selection = json.loads(self.store.read("uv-input-selection", view_layout["selection_digest"]))
        template = selection["selected"]["front"]
        selection.update(raster=[64, 64], selected={})
        registrations = {}
        for index, (view, rgb, focal, offset) in enumerate(cameras):
            selection["selected"][view] = {
                **template, "view": view, "id": view + "-frame", "index": index, "pts": [index, 25],
                "frame_digest": self.store.put("uv-source-rgb", rgb * 4096),
                "camera": {**template["camera"], "intrinsics": [[focal, 1], [focal, 1], [20, 1], [44, 1]],
                           "translation_um": [[offset, 1], [0, 1], [3000000, 1]]},
                "crop": {"rect": [0, 0, 64, 64], "size": [64, 64]},
            }
            registrations[view] = {
                "parts_digest": self.store.put("uv-source-parts", b"\x01" * 4096),
                "confidence_digest": self.store.put("uv-source-confidence", b"\xff" * 4096),
            }
        support = self.front | self.back if coincident else self.front
        view_layout.update(parts=[1, 1] if coincident else [1], required_views=list(registrations),
                           selection_digest=self.store.put("uv-input-selection", selection),
                           views=registrations, min_confidence_ppm=1,
                           support_digest=self.store.put("uv-atlas-parts", bytes(i in support for i in range(256))))
        view_layout.update(policy)
        return self.store.put("uv-projection-layout", view_layout)

    def test_distinct_registered_views_with_provenance(self):
        rec, rgba = self.project(self.setup_projection())
        self.assertEqual(rec["source_rc"], 0, rec)
        for i in self.back:
            self.assertEqual(rgba[4*i:4*i+4], self.colors["back"] + b"\xff")
        self.assertEqual({p["frame_id"] for p in rec["texels"]}, {"front-frame", "back-frame"})
        self.assertTrue(all(p["occlusion"] == "visible" for p in rec["texels"]))

    def test_copied_front_and_missing_rear(self):
        pin = self.setup_projection()
        _, rgba = self.project(pin)
        copied = bytearray(rgba)
        for i in self.back:
            copied[4*i:4*i+3] = self.colors["front"]
        rec, _ = self.project(pin, bytes(copied))
        self.assertIn("uv_projection_pixels_changed", rec["reason_codes"])
        rec, rgba = self.project(self.setup_projection(views=("front",)))
        self.assertEqual(rec["source_rc"], 3, rec)
        self.assertEqual(rec["inferred_texels"], len(self.back))
        self.assertTrue(all(rgba[4*i:4*i+4] == bytes(4) for i in self.back))

    def test_front_facing_rear_surface_is_still_occluded(self):
        self.raw = mesh([(4, 4), (8, 4), (4, 8)], rear=True, outward=False)
        rec, rgba = self.project(self.setup_projection(views=("front",)))
        self.assertEqual(rec["source_rc"], 3, rec)
        back = [entry for entry in rec["texels"] if entry["part"] == 2]
        self.assertTrue(all(entry["view_checks"]["front"] == "occluded" for entry in back))
        self.assertTrue(all(rgba[4*i:4*i+4] == bytes(4) for i in self.back))

    def test_required_view_and_work_budget_fail_closed(self):
        pin = self.setup_projection(views=("front",))
        witness = json.loads(self.store.read("uv-projection-layout", pin))
        witness["required_views"].append("back")
        rec, _ = self.project(self.store.put("uv-projection-layout", witness))
        self.assertEqual(rec["reason_codes"], ["uv_required_view_missing"])
        witness["required_views"].pop()
        witness["max_work"] = 1
        rec, rgba = self.project(self.store.put("uv-projection-layout", witness))
        self.assertEqual(rec["source_rc"], 6)
        self.assertIsNone(rgba)

    def test_occluder_blocks_front_ray_even_with_matching_source_part(self):
        self.raw = mesh([(4, 4), (8, 4), (4, 8)], rear=True, front_occluder=True)
        pin = self.setup_projection()
        witness = json.loads(self.store.read("uv-projection-layout", pin))
        witness["parts"].append(3)
        occ = texels(1, 9)
        witness["support_digest"] = self.store.put("uv-atlas-parts",
            bytes(1 if i in self.front else 2 if i in self.back else 3 if i in occ else 0 for i in range(256)))
        rec, rgba = self.project(self.store.put("uv-projection-layout", witness))
        self.assertTrue(any("occluded" in p["view_checks"].values() for p in rec["texels"]))
        self.assertTrue(all(rgba[4*i:4*i+4] == bytes(4) for i in self.front))

    def test_populated_wrong_part_and_eroded_support(self):
        # This atlas is fully populated on both protected charts.
        claimed = b"".join((self.colors["front"] + b"\xff") if i in self.front else
                           (self.colors["back"] + b"\xff") if i in self.back else bytes(4)
                           for i in range(256))
        rec, _ = self.project(self.setup_projection(wrong=True), claimed)
        self.assertEqual(rec["source_rc"], 5, rec)
        self.assertIn("uv_projection_wrong_part", rec["reason_codes"])
        rec, _ = self.project(self.setup_projection(eroded=True))
        self.assertIn("uv_atlas_support_changed", rec["reason_codes"])

    def test_grazing_incidence_refuses_and_oblique_confidence_is_scaled(self):
        layout = [(4, 4), (8, 4), (4, 8)]
        views = [("front", self.colors["front"], 96, 0)]
        self.raw = mesh(layout, tilt=(173648, -984808))
        rec, rgba = self.project(self.front_observations(views))
        self.assertEqual(rec["source_rc"], 3, rec)
        self.assertTrue(all(texel["view_checks"]["front"] == "grazing_incidence" for texel in rec["texels"]))
        self.assertEqual(rgba, bytes(1024))
        self.assertIsNone(rec["hit_distance_upper_um"])

        self.raw = mesh(layout, tilt=(866025, -500000))
        views = [("front", self.colors["front"], 104, 0)]
        rec, _ = self.project(self.front_observations(views))
        self.assertEqual(rec["source_rc"], 0, rec)
        self.assertTrue(all(0 < texel["confidence_ppm"] < 1000000 for texel in rec["texels"]))
        self.assertGreater(rec["hit_distance_upper_um"], 0)
        for texel in rec["texels"]:
            sample = texel["samples"][0]
            n, d = sample["cos_incidence_squared"]
            confidence = sample["confidence_ppm"]
            self.assertLessEqual(confidence ** 2 * d, n * 1000000 ** 2)
            self.assertGreater((confidence + 1) ** 2 * d, n * 1000000 ** 2)

    def test_insufficient_source_footprint_infers_even_at_head_on_incidence(self):
        self.raw = mesh([(4, 4), (8, 4), (4, 8)])
        pin = self.front_observations([("front", self.colors["front"], 96, 0)],
                                      min_source_pixels_per_texel=[100, 1])
        rec, rgba = self.project(pin)
        self.assertEqual(rec["source_rc"], 3, rec)
        self.assertTrue(all(texel["view_checks"]["front"] == "source_footprint_too_small" for texel in rec["texels"]))
        self.assertEqual(rgba, bytes(1024))

    def test_disagreeing_views_retain_both_samples_and_require_a_policy_limit(self):
        self.raw = mesh([(4, 4), (8, 4), (4, 8)])
        # Face precedes front by both name and time. Front has less oblique rays.
        views = [("face", b"\x10\xd0\x20", 48, 500000), ("front", self.colors["front"], 96, 0)]
        rec, rgba = self.project(self.front_observations(views))
        self.assertEqual(rec["source_rc"], 5, rec)
        self.assertEqual(rec["reason_codes"], ["uv_view_disagreement"])
        self.assertGreater(rec["disputed_texels"], 0)
        self.assertEqual(rec["view_disagreement_upper_ppm"], 752942)
        for texel in rec["texels"]:
            if texel["occlusion"] == "uncertain":
                self.assertCountEqual([sample["view"] for sample in texel["samples"]], ["face", "front"])
                self.assertEqual(texel["color_spread_rgb8"], [192, 176, 16])
                self.assertTrue(texel["inferred"])
                offset = texel["index"] * 4
                self.assertEqual(rgba[offset:offset + 4], bytes(4))
        rec, _ = self.project(self.front_observations(views, max_view_disagreement_ppm=752942))
        self.assertEqual(rec["source_rc"], 0, rec)
        self.assertTrue(all(texel["view"] == "front" for texel in rec["texels"] if len(texel["samples"]) == 2))

    def test_projection_reports_ray_miss_and_ambiguous_hits_separately(self):
        views = [("front", self.colors["front"], 96, 0)]
        self.raw = mesh([(4, 4), (8, 4), (4, 8)], tilt=(69756, -997564))
        rec, _ = self.project(self.front_observations(views))
        self.assertGreater(rec["ray_misses"], 0)
        self.assertEqual(rec["ambiguous_hits"], 0)

        glb = upstream("glb_core")
        xyz = [(0, 0, 0), (1000000, 0, 0), (0, 1000000, 0)]
        self.raw = glb.write_glb(glb.Scene([
            glb.Prim(str(offset), str(offset), glb.IDENTITY_Q16, xyz, [0, 1, 2],
                     [(x * 4096, y * 4096) for x, y in ((offset, offset), (offset + 4, offset), (offset, offset + 4))])
            for offset in (4, 10)
        ]))
        rec, _ = self.project(self.front_observations(views, coincident=True))
        self.assertEqual(rec["source_rc"], 3, rec)
        self.assertGreater(rec["ambiguous_hits"], 0)
        self.assertEqual(rec["ray_misses"], 0)
