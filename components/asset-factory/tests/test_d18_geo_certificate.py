"""Exact CPU geometry controls, including independent source/candidate registrations."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from fractions import Fraction as F

from asset_factory import domains
from test_d18_input_contract import glb, ProtectedStore, view_camera
from tools.input.evidence import bytes_digest
from tools.mesh import certificate as geo


TET = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
FACES = [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3]


def camera():
    return {"rotation": [[[int(i == j), 1] for j in range(3)] for i in range(3)],
            "translation_um": [[0, 1], [0, 1], [4000000, 1]],
            "intrinsics": [[64, 1], [64, 1], [73, 3], [169, 7]]}


class Gates(unittest.TestCase):
    def setUp(self):
        self.store = ProtectedStore()
        self.raw = glb(TET, FACES)
        self.intent = {"schema": "af.raw-intent/v1",
                       "forbidden_boxes_um": [[[-1200000, -100, -100], [-800000, 100, 100]]],
                       "component_counts": {"0:0": {"role": "body", "count": 1}},
                       "extent_ranges_um": [[900000, 2000000]] * 3,
                       "max_degenerate_faces": 0, "max_glb_bytes": 20000, "max_faces": 200, "max_box_checks": 200}
        self.policy = {
            "schema": "af.geometry-domain/v1", "frame": domains.FRAME,
            "source": bytes_digest(self.raw), "intent": self.store.add("raw-intent", self.intent),
            "reference_length_um": 1000000, "shells": {"0:0": {"role": "body", "genus": 0,
                                                               "loops": 0, "orientation": 1}},
            "regions_um": {"face": 0, "left_hand": 0, "right_hand": 0},
            "views": {"shot": {"camera": camera(), "size": [48, 48], "near_um": 1,
                               "far_um": 9000000}},
            "normal_cos2_min_ppm": 990000, "min_q2_ppm": 100, "max_aspect2": 100,
            "iou_min_ppm": 950000, "distance_max_ppm": 20000, "switch_max_um": 10000,
            "projected_max_millipx": 250, "raster_support_max_millipx": 1000, "max_work": 1000000, "refinements": 2,
            "face_budgets": [4]}
        self.patches = {"face": ["0:0", [0]], "left_hand": ["0:0", [3]],
                        "right_hand": ["0:0", [2]]}

        self.pose = bytes_digest(b"rest")
        rows = {}
        views = self.policy["views"] = {}
        for name in domains.REQUIRED_VIEWS:
            cam = camera()
            cam["rotation"] = view_camera(name)["rotation"]
            views[name] = {"camera": cam, "size": [48, 48], "near_um": 1, "far_um": 9000000}
            crop = {"rect": [0, 0, 48, 48], "size": [48, 48]}
            rows[name] = {"view": name, "camera": cam, "pose_digest": self.pose, "crop": crop,
                          "crop_transform": domains.crop_transform(crop, [48, 48])}
        self.selection = {"schema": "af.input-frame-selection/v1", "selected": rows,
                          "raster": [48, 48], "pose_digest": self.pose}
        self.policy["pose"] = self.pose
        self.policy["selection"] = self.store.add("geo-input-selection", self.selection)

    def registration(self, blob, domain, patches):
        raw_reg = self.store.add("raw-registration", {
            "schema": "af.raw-registration/v1", "subject_digest": bytes_digest(blob),
            "intent_digest": self.policy["intent"], "frame": domains.FRAME, "roles": {"0:0": "body"}})
        return self.store.add("geo-registration", {
            "schema": "af.geometry-registration/v1", "subject": bytes_digest(blob),
            "domain": domain, "raw_registration": raw_reg, "shells": {"0:0": ["0:0", "all"]}, "regions": patches,
            **({"feature_edges":self.feature_edges} if hasattr(self,"feature_edges") else {})})

    def domain_pin(self):
        self.policy["raw_registration_digest"] = self.registration(self.raw, None, self.patches)
        return self.store.add("geo-domain", self.policy)

    def measure(self, candidate=None, maps=None):
        domain = self.domain_pin()
        blobs = [self.raw, self.raw if candidate is None else candidate]
        regs = [self.policy["raw_registration_digest"],
                self.registration(blobs[1], domain, self.patches if maps is None else maps)]
        return geo.certify(blobs, domain_digest=domain, registration_digests=regs,
                           read_checked=self.store.read)

    def test_feature_reference_and_actual_edge(self):
        # An external witness fixes arc length, curvature and independent endpoints.
        pts = [[[0,1],[0,1],[0,1]], [[1000000,1],[0,1],[0,1]]]
        ref = {"schema":"af.feature-curves/v1", "source":bytes_digest(self.raw), "frame":domains.FRAME,
               "curves":{"ridge":{"shell":"0:0", "points_um":pts, "segments":[
                   {"arc_upper_um":1000000, "curvature_upper_per_um":[0,1]}]}}}
        self.policy["features"] = {"reference":self.store.add("geo-feature-reference",ref),
                                   "limits_um":{"ridge":100}}
        self.feature_edges = {"ridge":pts}
        nominal = self.measure()
        self.assertEqual(nominal["measurement_rc"], 0, nominal)
        self.assertEqual(nominal["levels"][1]["features_um"]["ridge"], [0,1])
        ref["curves"]["ridge"]["segments"][0]["curvature_upper_per_um"] = [1,1000000]
        self.policy["features"]["reference"] = self.store.add("geo-feature-reference",ref)
        inconclusive = self.measure()
        self.assertEqual(inconclusive["rc"], 3, inconclusive)
        self.assertIn("feature_curve_inconclusive:ridge",inconclusive["reason_codes"])
        self.feature_edges = {"ridge":[pts[0],[[4,1],[0,1],[0,1]]]}
        self.assertIn("registered_feature_edge_missing:ridge",self.measure()["reason_codes"])

    def test_closed_positive_and_target_disposition(self):
        r = self.measure()
        self.assertEqual(r["measurement_rc"], 0, r)
        self.assertEqual((r["rc"], r["verdict"]), (3, "CANNOT_SEE"))
        self.assertEqual(r["pairs"][0]["views"]["front"]["iou_ppm"], 1000000)

    def test_connected_floor(self):
        self.policy["face_budgets"] = [5]
        r = self.measure(glb(TET + [(-1, 0, 0)], FACES + [0, 2, 4]))
        self.assertIn("forbidden_support_intersection", r["reason_codes"])

    def test_unregistered_camera_scale(self):
        pin = self.store.add("geo-domain", self.policy)
        altered = copy.deepcopy(self.policy)
        altered["reference_length_um"] *= 10
        altered["views"]["front"]["camera"]["intrinsics"][0][0] *= 10
        self.store[("geo-domain", pin)] = geo.canonical_bytes(altered)
        r = geo.certify([self.raw, self.raw], domain_digest=pin, registration_digests=[],
                        read_checked=self.store.read)
        self.assertEqual(r["rc"], 4, r)

    def test_hand_loss_smaller_than_global_allowance(self):
        verts = TET.copy()
        verts[3] = (0, 0, 0.999)
        r = self.measure(glb(verts, FACES))
        self.assertEqual(r["rc"], 5, r)
        self.assertIn("region:left_hand", r["reason_codes"][0])

    def test_missing_protected_hand(self):
        patches = dict(self.patches)
        del patches["right_hand"]
        self.assertEqual(self.measure(maps=patches)["rc"], 3)

    def test_missing_handle(self):
        self.policy["shells"]["0:0"]["genus"] = 1
        self.assertIn("shell_topology", self.measure()["reason_codes"][0])

    def test_orientation(self):
        self.assertEqual(self.measure(glb(TET, list(reversed(FACES))))["rc"], 5)

    def test_false_lod(self):
        self.policy["face_budgets"] = [5, 4]
        domain = self.domain_pin()
        reg = self.registration(self.raw, domain, self.patches)
        regs = [self.policy["raw_registration_digest"], reg, reg]
        r = geo.certify([self.raw]*3, domain_digest=domain, registration_digests=regs,
                        read_checked=self.store.read)
        self.assertIn("lod_reduction", r["reason_codes"])

    def test_tiny_spike_between_old_samples(self):
        a, b, c = ((F(x), F(y), F(0)) for x, y in [(0, 0), (100, 0), (0, 100)])
        tip = (F(1, 1000), F(1, 1000), F(9))
        plain = [(a, b, c)]
        spike = [(a, b, tip), (b, c, tip), (c, a, tip)]
        low, high = geo.directed_bounds(spike, plain, F(1), 0, geo.Work(1000))
        self.assertEqual((low, high), (81, 81))

    def test_unresolved_cover(self):
        a, b, c, d = [(F(x), F(y), F(0)) for x, y in [(0, 0), (2, 0), (0, 2), (2, 2)]]
        lo, hi = geo.directed_bounds([(a, b, d)], [(a, b, c), (b, c, d)], 0, 0, geo.Work(100))
        self.assertEqual(lo, 0)
        self.assertGreater(hi, 0)
        with self.assertRaisesRegex(geo.InputIssue, "inconclusive"):
            geo.decide(lo, hi, 0, "distance")

    def test_per_view_empty_mask(self):
        self.policy["views"]["empty"] = copy.deepcopy(self.policy["views"]["front"])
        self.policy["views"]["empty"]["camera"]["intrinsics"][0] = [1, 1000]
        self.assertEqual(self.measure()["rc"], 4)

    def test_substituted_camera_even_with_new_domain_pin(self):
        self.policy["views"]["left"]["camera"]["intrinsics"][0] = [8, 1]
        self.assertEqual(self.measure()["rc"], 4)

    def test_inconsistent_view_pose(self):
        self.selection["selected"]["back"]["pose_digest"] = bytes_digest(b"another pose")
        self.policy["selection"] = self.store.add("geo-input-selection", self.selection)
        self.assertEqual(self.measure()["rc"], 4)

    def test_wrong_single_view(self):
        good = self.policy["views"].pop("back")
        self.assertEqual(self.measure()["rc"], 4)
        self.policy["views"]["incorrect"] = good
        self.assertEqual(self.measure()["rc"], 4)

    def test_adjacent_motion_exceeds_switch_allocation(self):
        self.policy["face_budgets"] = [6, 4]
        self.policy["regions_um"] = dict.fromkeys(self.patches, 19999)
        self.policy["switch_max_um"] = 1000
        self.policy["refinements"] = 8
        self.policy["projected_max_millipx"] = 5000
        # Export an intermediate subdivision of edge (0,1). Both LODs stay
        # inside the raw allowance but move to opposite sides of the reference.
        refined = [0,2,4, 4,2,1, 0,4,3, 4,1,3, 0,3,2, 1,2,3]
        vertices = TET + [(0.5,0,0)]
        master = glb([(x-1/256,y,z) for x,y,z in vertices], refined)
        lod = glb([(x+1/256,y,z) for x,y,z in TET], FACES)
        patches = {"face":["0:0",[0,1]], "left_hand":["0:0",[5]], "right_hand":["0:0",[4]]}
        dom = self.domain_pin()
        blobs = [self.raw, master, lod]
        regs = [self.policy["raw_registration_digest"], self.registration(master, dom, patches),
                self.registration(lod, dom, self.patches)]
        result = geo.certify(blobs, domain_digest=dom, registration_digests=regs, read_checked=self.store.read)
        self.assertEqual(result["rc"], 5, result)
        self.assertEqual([(p["from"],p["to"]) for p in result["pairs"]], [(0,1),(0,2),(1,2)], result["reason_codes"])
        self.assertIn("shell:0:0", result["reason_codes"][0])
        self.policy["switch_max_um"] = 10000
        dom = self.domain_pin()
        regs = [self.policy["raw_registration_digest"], self.registration(master, dom, patches),
                self.registration(lod, dom, self.patches)]
        result = geo.certify(blobs, domain_digest=dom, registration_digests=regs, read_checked=self.store.read)
        self.assertEqual(result["measurement_rc"], 0, result)

    def test_base_roundtrip_and_binding(self):
        from test_d18_base_evidence import EvidenceTests
        from tools.blender.topology import certify_chain

        dom = self.domain_pin()
        reg = self.registration(self.raw, dom, self.patches)
        args = dict(domain_digest=dom, registration_digests=[self.policy["raw_registration_digest"], reg],
                    read_checked=self.store.read)
        with TemporaryDirectory() as tmp:
            source = Path(tmp)/"source.glb"
            source.write_bytes(self.raw)
            native = certify_chain(source, [source], **args)
        self.assertEqual(native["measurement_rc"], 0, native)
        base = EvidenceTests()
        base.setUp()
        base.store = self.store
        base.profile.update(native["base_scope"])
        base.profile["criteria"] = []
        base.receipt.update(native["base_scope"])
        base.receipt.pop("role")
        base.receipt.pop("operation")
        base.receipt["subject_digest"] = bytes_digest(self.raw)
        control = {k: base.profile[k] for k in ("policy_digest","domain_digest","gate_id",
                                                "evaluator_digest","toolchain_digest")}
        control.update(expected_rc=5, observed_rc=5, witness_digest=bytes_digest(b"fixture-control"))
        controls = [base.store_object("negative-control", control)]
        base.profile["negative_control_receipt_refs"] = controls
        base.receipt["negative_control_receipt_refs"] = controls
        base.receipt["work_counts"] = {"geometry": native["work"]}
        base.receipt["observations"] = []
        for key, check in native["checks"].items():
            rule = {k:v for k,v in check.items() if k not in ("lower","upper")}
            rule.update(id=key, method_digest=base.profile["evaluator_digest"],
                        min_confidence_ppm=None, target_reason=None, proof_receipt_digest=None,
                        calibration_receipt_digest=None, uncertainty_kind="bounded")
            base.qualify(rule)
            base.profile["criteria"].append(rule)
            base.receipt["observations"].append(dict(id=key, unit=rule["unit"], method_digest=rule["method_digest"],
                lower=check["lower"], upper=check["upper"], confidence_ppm=None, certificate_ref=None))
        profile = base.store_object("profile",base.profile)
        base.receipt["profile_digest"] = profile
        for obs in base.receipt["observations"]:
            cert = {k:v for k,v in obs.items() if k!="certificate_ref"}
            cert.update(subject_digest=bytes_digest(self.raw), profile_digest=profile,
                        gate_id=base.profile["gate_id"],domain_digest=dom,witness_digest=native["native_digest"])
            obs["certificate_ref"] = base.store_object("certificate",cert)
        def run():
            return geo.certify([self.raw,self.raw],**args,profile_digest=profile,
                               stage_digest=base.store_object("stage",base.receipt))
        self.assertEqual(run()["rc"],0)
        base.receipt["observations"][-1]["upper"] = [10000000,1]
        self.assertEqual(run()["rc"],4)
        base.receipt["observations"].pop()
        self.assertEqual(run()["rc"],3)

    def test_reference_denominator_exceeds_independent_extent(self):
        self.policy["reference_length_um"] *= 10
        self.assertEqual(self.measure()["rc"], 4)

    def test_triangle_conditioning(self):
        self.policy["max_aspect2"] = 3
        result = self.measure()
        self.assertEqual(result["rc"], 5)
        self.assertIn("triangle_conditioning", result["reason_codes"])

    def test_camera_quaternion_and_clipping(self):
        cam = self.policy["views"]["face"]["camera"]
        del cam["rotation"]
        cam["rotation_quaternion_xyzw"] = [[1,1],[0,1],[0,1],[1,1]]
        self.policy["selection"] = self.store.add("geo-input-selection", self.selection)
        result = self.measure()
        self.assertEqual(result["measurement_rc"], 0, result["reason_codes"])
        self.policy["views"]["face"]["near_um"] = 4000000
        self.assertEqual(self.measure()["rc"], 3)

    def test_witnessed_per_view_iou(self):
        # A small translation crosses raster sample centres on this fixed camera.
        cam = self.policy["views"]["back"]["camera"]
        cam["intrinsics"][2:] = [[24,1],[24,1]]
        self.policy["selection"] = self.store.add("geo-input-selection", self.selection)
        self.policy["regions_um"] = dict.fromkeys(self.patches, 19999)
        verts = [(x-1/256,y,z) for x,y,z in TET]
        result = self.measure(glb(verts,FACES))
        self.assertEqual(result["reason_codes"], ["silhouette:back"])
        self.assertLess(result["pairs"][-1]["views"]["back"]["iou_ppm"], self.policy["iou_min_ppm"])

    def test_budget(self):
        self.policy["max_work"] = 1
        self.assertEqual(self.measure()["rc"], 6)


class SurfaceControls(unittest.TestCase):
    def test_positive_genus_then_actual_handle_loss(self):
        # Boundary of an extruded square annulus, linked across both rims.
        outer = [(-2,-2), (2,-2), (2,2), (-2,2)]
        inner = [(-1,-1), (1,-1), (1,1), (-1,1)]
        rows = []
        def quad(*v):
            rows.extend(([v[0], v[1], v[2]], [v[0], v[2], v[3]]))
        for i in range(4):
            j = (i+1) % 4
            a, b, c, d = outer[i], outer[j], inner[j], inner[i]
            quad((*a,1), (*b,1), (*c,1), (*d,1))
            quad((*d,0), (*c,0), (*b,0), (*a,0))
            quad((*a,0), (*b,0), (*b,1), (*a,1))
            quad((*c,0), (*d,0), (*d,1), (*c,1))
        expect = {"loops": 0, "orientation": 1, "genus": 1}
        self.assertEqual(geo._shell(rows, expect, geo.Work(500))["genus"], 1)
        tetra = [tuple(TET[k] for k in FACES[i:i+3]) for i in range(0,len(FACES),3)]
        with self.assertRaisesRegex(geo.InputIssue, "shell_topology"):
            geo._shell(tetra, expect, geo.Work(500))

    def test_coplanar_adjacent_overlap_and_disjoint_shell_crossing(self):
        a, b, c = (0,0,0), (4,0,0), (0,4,0)
        triangle = (a,b,c)
        for shell, intersecting in [("a", (b,a,(1,1,0))), ("b", ((1,1,-1), (1,1,1), (3,1,1)))]:
            with self.subTest(shell=shell), self.assertRaisesRegex(geo.InputIssue, "intersection"):
                geo._embedded([(geo._box(triangle),"a",triangle),
                               (geo._box(intersecting),shell,intersecting)], geo.Work(100))

    def test_projection_single_bad_view_and_normal_reversal(self):
        a, b, c = (F(0),F(0),F(0)), (F(1),F(0),F(0)), (F(0),F(1),F(0))
        with self.assertRaisesRegex(geo.InputIssue, "normal"):
            geo.normal_cover([(a,b,c)], [(a,c,b)], 1, 990000, 0, geo.Work(100))
        self.assertEqual(geo.normal_cover([(a,b,c)], [(a,b,c)], 0, 1000000, 0, geo.Work(100)), 0)

    def test_thin_finger_cut_before_sampling(self):
        a,b,c = (F(0),F(0),F(0)), (F(10),F(0),F(0)), (F(0),F(10),F(0))
        tip = (F(0), F(0), F(100))
        narrow = (F(1,10000),F(0),F(0))
        raw, cut = [(a,b,c), (a,tip,narrow)], [(a,b,c)]
        bounds = {}
        with self.assertRaisesRegex(geo.InputIssue, "forward:witness"):
            geo._distance(raw,cut,1,{"refinements":0},geo.Work(100),bounds,"finger")
        self.assertEqual(set(bounds), {"finger:forward","finger:reverse"})


class PipelineBindings(unittest.TestCase):
    def test_proposal_cannot_publish_without_declared_geo_witness(self):
        from test_f2u2_topology import TopologyTests
        from test_w3_remesh_stage import RemeshStageTests

        scope = dict(domain_digest=bytes_digest(b"missing-geo"), registration_digests=[],
                     read_checked=lambda role, pin: None)
        for suite, method in [(TopologyTests,"invoke"),(RemeshStageTests,"run_stage")]:
            with self.subTest(suite=suite.__name__):
                fixture = suite()
                fixture.setUp()
                try:
                    rc, result = getattr(fixture,method)(geometry_scope=scope)
                    self.assertEqual(rc, 3, result)
                    self.assertIn("missing_geo-domain",result["reason_codes"])
                    self.assertEqual(result["artifacts"], [])
                    self.assertFalse((fixture.out if method=="invoke" else fixture.dest).exists())
                finally:
                    fixture.doCleanups()

    def test_cavity_and_cross_shell_negative(self):
        outer = [tuple(TET[k] for k in FACES[i:i+3]) for i in range(0,12,3)]
        def scaled(p):
            return tuple(F(x+1,8) for x in p)
        inner = [tuple(scaled(p) for p in reversed(t)) for t in outer]
        shells = {"body":{"parent":None, "loops":0}, "cavity":{"parent":"body", "loops":0}}
        geo._containment({"body":outer, "cavity":inner}, shells, geo.Work(1000))
        shells["cavity"]["parent"] = None
        with self.assertRaisesRegex(geo.InputIssue, "shell_containment"):
            geo._containment({"body":outer,"cavity":inner}, shells, geo.Work(1000))


class QuadCages(unittest.TestCase):
    def test_split_positive_fold_warp_and_substituted_cage(self):
        points = [(0,0,0),(1,0,0),(1,1,0),(0,1,0)]
        def operation(ids, points, ref, faces):
            cage = {"schema":"af.quad-cage/v1", "frame":domains.FRAME,
                    "vertices_um":[[domains.pair(v) for v in p] for p in points],
                    "quads":[ids], "triangles":[[0,1,2],[0,2,3]], "triangle_quads":[0,0]}
            return geo.quad_metrics(cage, [ref], faces, geo.Work(100))
        triangles = [tuple(points[i] for i in f) for f in ((0,1,2),(0,2,3))]
        metrics = operation([0,1,2,3], points, [0,0,1], triangles)
        self.assertEqual(metrics["minimum_jacobian2"], [1,1])
        self.assertEqual(metrics["minimum_split_cosine2"], [1,1])
        with self.assertRaises(geo.InputIssue):
            operation([0,2,1,3], points, [0,0,1], triangles)
        with self.assertRaisesRegex(geo.InputIssue, "jacobian"):
            operation([0,1,2,3], points, [0,0,-1], triangles)
        bent = points.copy()
        bent[3] = (0,1,1)
        with self.assertRaisesRegex(geo.InputIssue, "cage_export"):
            operation([0,1,2,3], bent, [0,0,1], triangles)
        bent_triangles = [tuple(bent[i] for i in f) for f in ((0,1,2),(0,2,3))]
        self.assertLess(domains.rational(operation([0,1,2,3],bent,[0,0,1],bent_triangles)
                                         ["minimum_split_cosine2"]), 1)
