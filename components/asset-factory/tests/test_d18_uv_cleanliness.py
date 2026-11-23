import unittest

from gates.texture_cleanliness import check_texture_cleanliness
from tools.input.evidence import bytes_digest, canonical_bytes


class CleanlinessTests(unittest.TestCase):
    def check(self, change=None, authorize=True, global_limit=60000, *, size=8, color_space="srgb",
              self_witness=False, shared_lineage=False, missing_lineage=False, texture_role="albedo",
              reference_change=None, footprint=None, **policy):
        reference = bytearray()
        for y in range(size):
            for x in range(size):
                # Independently authored intentional high-contrast stripes.
                reference.extend((200 if x % 2 else 30, 60, 40, 255))
        reference[-4:] = bytes(4)
        if reference_change is not None:
            reference_change(reference)
        reference = bytes(reference)
        candidate = bytearray(reference)
        if change is not None:
            change(candidate)
        else:
            candidate[4 * (size * (size - 2) + 1)] += 1
        candidate = bytes(candidate)
        if self_witness:
            reference = candidate
        reference_lineage = bytes_digest(b"approved camera reference")
        candidate_lineage = None if missing_lineage else (
            reference_lineage if shared_lineage else bytes_digest(b"projection attempt lineage"))
        footprint = bytes([1]) * (size * size) if footprint is None else footprint
        fields = {
            "schema": "af.texture-cleanliness-witness/v1", "reference_digest": bytes_digest(reference),
            "reference_lineage_digest": reference_lineage,
            "size": [size, size], "protected_rects": {"face": [0, 0, 4, 4]}, "tile_size": 2,
            "color_space": color_space, "max_global_mae_ppm": global_limit,
            "max_local_channel_ppm": 20000, "max_tile_mae_ppm": 10000,
            "outlier_channel_ppm": 400000, "max_outlier_ppm": 0,
            "allowed_stripe_rects": [], "max_outlier_clusters": 0, "max_outlier_cluster_px": 0,
            **policy,
        }
        if texture_role is not None:
            fields["texture_role"] = texture_role
        fields.setdefault("filter_footprint_digest", bytes_digest(footprint))
        witness = canonical_bytes(fields)
        entries = {("uv-cleanliness-witness", bytes_digest(witness)): witness,
                   ("uv-cleanliness-reference", bytes_digest(reference)): reference,
                   ("uv-cleanliness-filter-footprint", bytes_digest(footprint)): footprint}
        def read(role, pin):
            if not authorize and role == "uv-cleanliness-reference":
                return None
            return entries.get((role, pin))

        return check_texture_cleanliness(candidate, witness_digest=bytes_digest(witness), read_checked=read,
                                         candidate_lineage_digest=candidate_lineage)

    def test_intentional_stripe_and_untrusted_self_witness(self):
        self.assertEqual(self.check()["source_rc"], 0)
        self.assertEqual(self.check(authorize=False)["source_rc"], 3)

    def test_one_speckle_pixel_and_support(self):
        def speckle(data):
            data[4 * 48:4 * 48 + 3] = b"\xff\xff\xff"

        self.assertIn("texture_outlier_limit", self.check(speckle)["reason_codes"])
        self.assertIn("texture_support_changed",
                      self.check(lambda data: data.__setitem__(slice(3, None, 4), b"\0" * 63 + b"\xff"))
                      ["reason_codes"])

    def test_fractional_ppm_boundary_rounds_toward_refusal(self):
        def one_step(data):
            data[4 * 48] += 1

        rec = self.check(one_step, global_limit=20, color_space="linear")
        self.assertEqual(rec["measurements"]["global_mae_upper_ppm"], 21)
        self.assertEqual(rec["reason_codes"], ["texture_global_colour_limit"])

    def test_srgb_decodes_before_protected_residuals(self):
        def one_step(data):
            data[0] += 1

        linear = self.check(one_step, color_space="linear", max_local_channel_ppm=2000)
        srgb = self.check(one_step, color_space="srgb", max_local_channel_ppm=2000)
        self.assertEqual(linear["measurements"]["max_channel_error_upper_ppm"], 3922)
        self.assertIn("texture_protected_channel_limit", linear["reason_codes"])
        self.assertLess(srgb["measurements"]["max_channel_error_upper_ppm"], 2000)
        self.assertEqual(srgb["source_rc"], 0)

    def test_data_texture_roles_require_linear(self):
        def orm_reference(data):
            data[:4] = bytes((255, 20, 0, 255))

        def change_roughness(data):
            data[1] = 40

        for role in ("orm", "roughness", "metallic", "normal"):
            with self.subTest(role=role):
                srgb = self.check(change_roughness, reference_change=orm_reference, texture_role=role)
                self.assertEqual(srgb["source_rc"], 5, srgb)
                self.assertEqual(srgb["reason_codes"], ["texture_role_requires_linear"])
                self.assertNotIn("measurements", srgb)
                linear = self.check(change_roughness, reference_change=orm_reference,
                                    texture_role=role, color_space="linear")
                self.assertEqual(linear["texture_role"], role)
                self.assertIn("texture_protected_channel_limit", linear["reason_codes"])
                self.assertEqual(self.check(texture_role=role, color_space="linear")["source_rc"], 0)
        missing = self.check(texture_role=None)
        self.assertEqual((missing["source_rc"], missing["reason_codes"]), (3, ["texture_role_missing"]))
        invalid = self.check(texture_role="unknown")
        self.assertEqual((invalid["source_rc"], invalid["reason_codes"]), (2, ["texture_role_invalid"]))

    def test_refuses_reference_with_same_bytes_or_lineage(self):
        for kwargs in ({"self_witness": True}, {"shared_lineage": True}):
            with self.subTest(**kwargs):
                rec = self.check(**kwargs)
                self.assertEqual(rec["source_rc"], 5)
                self.assertEqual(rec["reason_codes"], ["texture_self_witness"])
        rec = self.check(missing_lineage=True)
        self.assertEqual(rec["source_rc"], 3)
        self.assertEqual(rec["reason_codes"], ["texture_candidate_lineage_missing"])

    def test_speckle_count_and_size_survive_a_nonzero_outlier_budget(self):
        offset = 4 * (40 * 64 + 40)

        def one(data):
            data[offset:offset + 3] = b"\xff\xff\xff"

        def two(data):
            one(data)
            data[offset + 4:offset + 7] = b"\xff\xff\xff"

        for change, count, cluster_px in ((one, 0, 1), (two, 1, 2)):
            with self.subTest(cluster_px=cluster_px):
                rec = self.check(change, size=64, max_outlier_ppm=1000,
                                 max_outlier_clusters=count, max_outlier_cluster_px=1)
                self.assertEqual(rec["outlier_clusters"], [cluster_px])
                self.assertEqual(rec["reason_codes"], ["texture_speckle_cluster"])
        rec = self.check(two, size=64, max_outlier_ppm=1000, allowed_stripe_rects=[[40, 40, 42, 41]])
        self.assertEqual(rec["outlier_clusters"], [])
        self.assertGreater(rec["measurements"]["texture_outlier_upper_ppm"], 0)
        self.assertEqual(rec["source_rc"], 0)

    def test_filter_footprint_outliers_span_transparent_gutter(self):
        def transparent_column(data):
            for y in range(64):
                data[4 * (y * 64 + 32) + 3] = 0

        def speckle(data):
            for x in (31, 32, 33):
                offset = 4 * (40 * 64 + x)
                data[offset:offset + 3] = bytes([255]) * 3

        for reference_change in (transparent_column, None):
            with self.subTest(transparent=reference_change is not None):
                rec = self.check(speckle, size=64, reference_change=reference_change,
                                 max_outlier_ppm=1000, max_outlier_clusters=2, max_outlier_cluster_px=1)
                self.assertEqual((rec["source_rc"], rec["reason_codes"]), (5, ["texture_speckle_cluster"]))
                self.assertEqual(rec["outlier_clusters"], [3])
                self.assertEqual(rec["measurements"]["texture_outlier_upper_ppm"], 733)

    def test_filter_footprint_rejects_missing_visible_support(self):
        mask = bytes(1) + bytes([1]) * 63
        rec = self.check(footprint=mask)
        self.assertEqual((rec["source_rc"], rec["reason_codes"]), (5, ["texture_filter_support_missing"]))
        invalid = self.check(footprint=bytes([2]) + mask[1:])
        self.assertEqual((invalid["source_rc"], invalid["reason_codes"]), (2, ["texture_filter_footprint_invalid"]))

    def test_rgb_outside_the_filter_footprint_is_ignored(self):
        def transparent_border(data):
            for y in range(8):
                for x in range(5, 8):
                    data[4 * (y * 8 + x) + 3] = 0

        def speckle(data):
            data[-4:-1] = bytes([255]) * 3

        mask = (bytes([1]) * 6 + bytes(2)) * 8
        rec = self.check(speckle, reference_change=transparent_border, footprint=mask)
        self.assertEqual(rec["source_rc"], 0, rec)
        self.assertEqual(rec["outlier_clusters"], [])
        self.assertEqual(rec["measurements"]["texture_outlier_upper_ppm"], 0)

    def test_protected_face_smear_can_pass_the_global_mean(self):
        def smear(data):
            for y in range(4):
                for x in range(4):
                    data[(y * 8 + x) * 4] = 115

        rec = self.check(smear)
        self.assertLess(rec["measurements"]["global_mae_upper_ppm"], 60000)
        self.assertIn("texture_protected_channel_limit", rec["reason_codes"])
        self.assertIn("texture_protected_tile_limit", rec["reason_codes"])
