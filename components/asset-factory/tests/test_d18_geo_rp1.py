"""Registration, projected support, exact witnesses and spatial pruning regressions."""
import copy
from fractions import Fraction
import json
import struct
import unittest

import test_d18_geo_certificate as fixture
from test_d18_input_contract import glb
from tools.mesh import certificate as geo


def nonindexed(blob):
    size = struct.unpack_from("<I", blob, 12)[0]
    doc = json.loads(blob[20:20 + size])
    del doc["meshes"][0]["primitives"][0]["indices"]
    payload = json.dumps(doc).encode()
    payload += b" " * (-len(payload) % 4)
    tail = blob[20 + size:]
    return blob[:8] + struct.pack("<III", 20 + len(payload) + len(tail),
                                  len(payload), 0x4e4f534a) + payload + tail


class Repairs(unittest.TestCase):
    def setUp(self):
        self.g = fixture.Gates()
        self.g.setUp()

    def test_swapped_raw_registration(self):
        g = self.g
        g.policy["regions_um"].update(face=19999, right_hand=19999)
        candidate = glb(fixture.TET[:3] + [(0, 0, 0.999)], fixture.FACES)
        raw_pin = g.registration(g.raw, None, g.patches)
        g.policy["raw_registration_digest"] = raw_pin
        domain = g.store.add("geo-domain", g.policy)
        swapped = dict(g.patches, face=["0:0", [3]], left_hand=["0:0", [0]])
        def check(raw_reg, patches):
            regs = [raw_reg, g.registration(candidate, domain, patches)]
            return geo.certify([g.raw, candidate], domain_digest=domain,
                               registration_digests=regs, read_checked=g.store.read)
        actual = check(raw_pin, g.patches)
        self.assertEqual(actual["rc"], 5, actual["reason_codes"])
        self.assertIn("region:left_hand", actual["reason_codes"][0])
        swapped_raw_pin = g.registration(g.raw, None, swapped)
        for candidate_patches in (g.patches, swapped):
            with self.subTest(candidate_patches=candidate_patches):
                result = check(swapped_raw_pin, candidate_patches)
                self.assertEqual((result["rc"], result["reason_codes"]),
                                 (4, ["raw_geometry_registration_drift"]))

    def test_unpinned_legacy_domain_is_incomplete(self):
        g = self.g
        g.policy.pop("raw_registration_digest", None)
        domain = g.store.add("geo-domain", g.policy)
        reg = g.registration(g.raw, domain, g.patches)
        report = geo.certify([g.raw]*2, domain_digest=domain, registration_digests=[reg]*2,
                             read_checked=g.store.read)
        self.assertEqual(report["measurement_rc"], 3)
        self.assertIn("raw_geometry_registration_missing", report["reason_codes"])

    def test_each_anatomical_limit_strict(self):
        for region in self.g.patches:
            for loosened in (20000, 10**9):
                with self.subTest(region=region, tolerance=loosened):
                    self.g.policy["regions_um"][region] = loosened
                    result = self.g.measure()
                    self.g.policy["regions_um"][region] = 0
                    self.assertEqual((result["rc"], result["reason_codes"]),
                                     (2, ["protected_region_limit_not_tighter:" + region]))
        self.g.policy["reference_length_um"] += 1
        self.g.policy["regions_um"]["face"] = 20000
        self.assertEqual(self.g.measure()["measurement_rc"], 0)

    def test_seam_authority_indexed_and_nonindexed(self):
        g = self.g
        corners = fixture.FACES
        split = glb([fixture.TET[i] for i in corners], list(range(len(corners))))
        reps = [corners.index(i) for i in corners]
        for blob in (split, nonindexed(split)):
            g.raw = blob
            g.policy["source"] = geo.bytes_digest(blob)
            original = fixture.Gates.registration.__get__(g)
            def register(subject, domain, patches):
                pin = original(subject, domain, patches)
                if welds is None:
                    return pin
                doc = json.loads(g.store.read("geo-registration", pin))
                doc["welded_index_maps"] = welds
                return g.store.add("geo-registration", doc)

            g.registration = register
            for welds, expected in [
                (None, "coincident_index_seams_unqualified"),
                ({"0:0": reps}, None),
                ({"0:0": reps[:-1]}, "invalid_welded_index_map"),
                ({"0:0": [True] + reps[1:]}, "invalid_welded_index_map"),
                ({"0:0": list(range(len(reps)))}, "coincident_index_seams_unqualified"),
                ({"0:0": [0]*len(reps)}, "welded_vertex_position_drift"),
                ({"0:0": reps, "0:1": reps}, "welded_primitive_drift"),
                ([], "invalid_welded_index_maps"),
            ]:
                with self.subTest(indexed=blob == split, welds=welds):
                    result = g.measure()
                    if expected is None:
                        self.assertEqual(result["measurement_rc"], 0, result["reason_codes"])
                    else:
                        self.assertIn(expected, result["reason_codes"])

    def test_feature_endpoint_witness(self):
        g = self.g
        def point(x, y):
            return [[x, 1], [y, 1], [0, 1]]
        endpoints = [point(0, 0), point(1000000, 0)]
        original = copy.deepcopy(endpoints)
        original[0] = point(0, 101)
        reference = {"schema":"af.feature-curves/v1", "source":g.policy["source"],
                     "frame":g.policy["frame"], "curves":{"ridge":{"shell":"0:0",
                     "points_um":original, "segments":[{"arc_upper_um":1000001,
                                                       "curvature_upper_per_um":[0,1]}]}}}
        g.feature_edges = {"ridge":endpoints}
        g.policy["features"] = {"reference":g.store.add("geo-feature-reference", reference),
                                "limits_um":{"ridge":100}}
        result = g.measure()
        self.assertEqual((result["rc"], result["reason_codes"]), (5, ["feature_endpoint_witness:ridge"]))

    def test_edge_on_sheet_and_nonzero_subpixel_area(self):
        g = self.g
        g.raw = glb([(0,0,0),(1,0,0),(1,1,0),(0,1,0),(0.5,0.5,0)],
                    [0,1,4, 1,2,4, 2,3,4, 3,0,4])
        g.policy.update(source=geo.bytes_digest(g.raw), reference_length_um=900000)
        g.policy["shells"]["0:0"].update(loops=1, orientation=0)
        g.intent["extent_ranges_um"] = [[900000,2000000], [0,2000000], [0,2000000]]
        g.policy["intent"] = g.store.add("raw-intent", g.intent)
        result = g.measure()
        self.assertEqual(result["measurement_rc"], 0, result["reason_codes"])
        view = result["pairs"][0]["views"]["left"]
        self.assertEqual(view["raster_exemption"], "zero_projected_area")
        self.assertIsNone(view["iou_ppm"])
        self.assertIn("projection:forward", view["bounds_px"])
        # A visible planar projection too small to hit a pixel still needs evidence.
        for view in g.policy["views"].values():
            view["camera"]["intrinsics"][:2] = [[1, 1000000]] * 2
        g.policy["selection"] = g.store.add("geo-input-selection", g.selection)
        result = g.measure()
        self.assertEqual(result["rc"], 3)
        self.assertIn("empty_silhouette:", result["reason_codes"][0])

    def test_pruned_bounds_match_exhaustive_corner_and_cell_oracle(self):
        def triangle(x):
            return ((x,0,0), (x+2,0,0), (x,2,0))
        target = [triangle(x*10) for x in range(256)]
        source = [tuple((x,y,1) for x,y,z in triangle(i*10)) for i in range(0,256,8)]
        source.append(((0,0,1), (10,0,1), (0,2,1)))
        expected_low = expected_high = 0
        for cell in source:
            ds = [[geo.triangle_d2(p, t) for p in cell] for t in target]
            expected_low = max(expected_low, max(map(min, zip(*ds))))
            expected_high = max(expected_high, min(map(max, ds)))
        # Exhaustive traversal would require 25,344 exact corner tests.
        work = geo.Work(12000)
        self.assertEqual(geo.directed_bounds(source, target, Fraction(100), 0, work),
                         (expected_low, expected_high))
        with self.assertRaisesRegex(geo.InputIssue, "geometry_work_budget"):
            geo.directed_bounds(source, target, Fraction(100), 0, geo.Work(32))
