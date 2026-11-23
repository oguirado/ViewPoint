"""Real CPU render/engine tests plus executable negative-control mutants.

Synthetic GLBs are generated locally, never by a model. Frames are never saved
outside the production evaluator's temporary RAM workspace.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core
from gates.sculpt_metric import api
from tools.mesh import contracts as mc


def mesh_policy(components=1):
    return {"sample_budget": 128, "raster_resolution": 32,
            "view_basis": "gltf-y-up-ortho4", "unit": "metre", "transform": "world",
            "weld_tolerance_um": 0, "allow_open": False, "require_uv": False,
            "component_allowlist": {"body": components}, "timeout_s": 30}


def region_policy():
    return {"resolution": 64, "max_changed_pixels_ppm": 0,
            "max_mean_abs_delta_ppm": 0, "timeout_s": 180}


def box(lo, hi, *, rear_center=False, dent_um=0):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    vertices = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (3, 7, 6), (3, 6, 2),
             (0, 4, 7), (0, 7, 3), (1, 2, 6), (1, 6, 5)]
    if rear_center:
        vertices.append(((x0 + x1) // 2, (y0 + y1) // 2, z0 + dent_um))
        faces = [(0, 8, 1), (1, 8, 2), (2, 8, 3), (3, 8, 0)] + faces[2:]
    return vertices, faces


def glb(parts):
    vertices, indices = [], []
    for points, faces in parts:
        offset = len(vertices)
        vertices.extend(points)
        indices.extend(offset + i for face in faces for i in face)
    # Floating point exists only in glTF storage; all public metrics are integers.
    positions = struct.pack("<" + "f" * (len(vertices) * 3),
                            *(v / 1000000 for point in vertices for v in point))
    triangles = struct.pack("<" + "I" * len(indices), *indices)
    binary = positions + triangles
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"mesh": 0, "name": "body"}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
           "buffers": [{"byteLength": len(binary)}],
           "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
                           {"buffer": 0, "byteOffset": len(positions), "byteLength": len(triangles)}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(vertices), "type": "VEC3",
                          "min": [min(p[a] for p in vertices) / 1000000 for a in range(3)],
                          "max": [max(p[a] for p in vertices) / 1000000 for a in range(3)]},
                         {"bufferView": 1, "componentType": 5125, "count": len(indices), "type": "SCALAR"}]}
    header = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    header += b" " * (-len(header) % 4)
    chunks = struct.pack("<II", len(header), 0x4E4F534A) + header
    chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    return b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks


def valid_vision():
    h = "a" * 64
    return {"rc": 0, "views": [{"view": view, "reference_frame_sha256": h,
            "candidate_frame_sha256": h, "frame_manifest_sha256": h, "params_sha256": h,
            "residual_manifest_sha256": h, "comparison_encoding": "exact_changed_pixels_binary_rgb24/v1",
            "tiles_sha256": h, "regions": [], "pixel_count": 4096, "changed_pixels": 0,
            "reference_foreground_pixels": 1024, "candidate_foreground_pixels": 1024,
            "region_scores": [], "uncovered_changed_pixels": 0,
            "changed_pixels_ppm": 0, "mean_abs_delta_ppm": 0} for view in api.VIEWS]}


def valid_mesh():
    h = "a" * 64
    rec = mc.receipt(h, h, h, h)
    rec.update(rc=0, status="OK", artefact_sha256=h, unavailable={})
    rec["metrics"].update(hausdorff_forward_um=0, hausdorff_backward_um=0, raw_diag_um=1000000,
                          hausdorff_relative_ppm=0, silhouette_iou_ppm=1000000,
                          component_inventories=[{"components": 1, "failed": []}],
                          distance_is_exact=False,
                          silhouette_per_view_ppm={v: 1000000 for v in api.VIEWS[:4]})
    return rec


class ContractTests(unittest.TestCase):
    def test_identity_and_missing_coverage(self):
        self.assertEqual(api.combine(valid_mesh(), valid_vision(), region_policy=region_policy()),
                         (0, [], 1000000))
        vision = valid_vision()
        vision["views"].pop(1)
        with self.assertRaises(api.MetricError) as ctx:
            api.combine(valid_mesh(), vision, region_policy=region_policy())
        self.assertEqual((ctx.exception.rc, ctx.exception.reason), (3, "view_missing"))

    def test_empty_and_duplicate_ring_refused(self):
        for rows in ([], [valid_vision()["views"][0]] * 6):
            with self.assertRaises(api.MetricError):
                api.combine(valid_mesh(), {"rc": 0, "views": rows}, region_policy=region_policy())

    def test_reverse_distance_is_independent_of_cached_verdict(self):
        measured = valid_mesh()
        measured["metrics"]["hausdorff_backward_um"] = 30000
        rc, reasons, score = api.combine(measured, valid_vision(), region_policy=region_policy())
        self.assertEqual(rc, 5)
        self.assertIn("distance_regression", reasons)
        self.assertEqual(score, 970000)

    def test_back_view_regression_not_averaged_away(self):
        measured = valid_mesh()
        measured["metrics"]["silhouette_per_view_ppm"]["back"] = 900000
        self.assertEqual(api.combine(measured, valid_vision(), region_policy=region_policy())[0], 5)

    def test_subthreshold_exact_residual_cannot_hide_behind_empty_regions(self):
        vision = valid_vision()
        back = vision["views"][1]
        back.update(candidate_frame_sha256="b" * 64, changed_pixels=1,
                    changed_pixels_ppm=245, mean_abs_delta_ppm=1)
        self.assertEqual(api.combine(valid_mesh(), vision, region_policy=region_policy())[0], 5)

    def test_null_float_bool_and_invalid_rc_refused(self):
        for value in (None, True, 0.0):
            measured = valid_mesh()
            measured["metrics"]["hausdorff_forward_um"] = value
            try:
                rc = api.combine(measured, valid_vision(), region_policy=region_policy())[0]
            except (api.MetricError, mc.MeshError) as exc:
                rc = exc.rc
            self.assertIn(rc, (2, 3))
        with self.assertRaises(api.MetricError):
            api.combine(valid_mesh(), {"rc": 1}, region_policy=region_policy())

    def test_explicit_pixel_budgets_and_timeout_required(self):
        for key in region_policy():
            p = region_policy()
            del p[key]
            with self.assertRaises(api.MetricError):
                api.validate_region_policy(p)

    def test_rank_deterministic_tie_and_no_incomplete_rank(self):
        a = {"schema": api.SCHEMA, "rc": 0, "candidate_sha256": "a" * 64, "score_ppm": 900000}
        a.update({key: "a" * 64 for key in ("reference_sha256", "mesh_policy_sha256",
                                           "region_policy_sha256", "toolchain_sha256")})
        b = dict(a, candidate_sha256="b" * 64)
        self.assertEqual(api.rank([b, a]), api.rank([a, b]))
        with self.assertRaises(api.MetricError):
            api.rank([dict(a, rc=3)])
        with self.assertRaises(api.MetricError):
            api.rank([])
        with self.assertRaises(api.MetricError) as ctx:
            api.rank([a, dict(b, reference_sha256="b" * 64)])
        self.assertEqual(ctx.exception.rc, 4)
        with self.assertRaises(api.MetricError) as ctx:
            api.rank([a, a])
        self.assertEqual(ctx.exception.rc, 2)

    def test_uncovered_region_is_cannot_see_even_with_permissive_pixel_budget(self):
        vision = valid_vision()
        vision["views"][1].update(candidate_frame_sha256="b" * 64, changed_pixels=1,
                                 changed_pixels_ppm=245, mean_abs_delta_ppm=1,
                                 uncovered_changed_pixels=1)
        policy = dict(region_policy(), max_changed_pixels_ppm=1000000, max_mean_abs_delta_ppm=1000000)
        self.assertEqual(api.combine(valid_mesh(), vision, region_policy=policy)[0], 3)

    def test_real_assertion_goes_red_for_backward_distance_mutant_then_green(self):
        source = Path(api.__file__).read_text()
        old = 'distance = max(m["hausdorff_forward_um"], m["hausdorff_backward_um"])'
        self.assertEqual(source.count(old), 1)
        namespace = {"__name__": "sculpt_test_mutant", "__file__": api.__file__}
        exec(compile(source.replace(old, 'distance = m["hausdorff_forward_um"]'), api.__file__, "exec"), namespace)
        measured = valid_mesh()
        measured["metrics"]["hausdorff_backward_um"] = 30000
        class Probe(unittest.TestCase):
            def runTest(inner):
                try:
                    rc = namespace["combine"](measured, valid_vision(), region_policy=region_policy())[0]
                except (api.MetricError, mc.MeshError) as exc:
                    rc = exc.rc
                inner.assertEqual(rc, 5)
        captured = io.StringIO()
        result = unittest.TextTestRunner(stream=captured).run(Probe())
        self.assertEqual(len(result.failures), 1, captured.getvalue())
        self.assertEqual(api.combine(measured, valid_vision(), region_policy=region_policy())[0], 5)

    def test_refusal_preserves_caller_and_no_render(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.glb"
            path.write_bytes(b"caller bytes")
            with mock.patch.object(api, "_child", side_effect=AssertionError("must not render")):
                result = api.evaluate(path, path, mesh_policy=mesh_policy(), region_policy={})
            self.assertEqual(result["rc"], 2)
            self.assertEqual(path.read_bytes(), b"caller bytes")

    def test_owned_child_deadline_reaps_its_group(self):
        started = time.monotonic()
        with self.assertRaises(api.MetricError) as ctx:
            api._child([sys.executable, "-I", "-c", "import time; time.sleep(10)"],
                       deadline=started + 0.1)
        self.assertEqual(ctx.exception.rc, 6)
        self.assertLess(time.monotonic() - started, 2)

    def test_missing_blender_is_cannot_see(self):
        rec = api.evaluate(Path("missing.glb"), Path("missing.glb"), mesh_policy=mesh_policy(),
                           region_policy=region_policy(), blender=Path("/nonexistent/sculpt-blender"))
        self.assertEqual(rec["rc"], 3)
        self.assertEqual(rec["reason_codes"], ["blender_unavailable"])

    def test_stale_engine_pin_refused_before_any_child(self):
        with mock.patch.object(api, "_engine_pin", side_effect=api.MetricError(4, "engine_pin_stale")), \
                mock.patch.object(api, "_child", side_effect=AssertionError("must not run")):
            rec = api.evaluate(Path("missing.glb"), Path("missing.glb"),
                               mesh_policy=mesh_policy(), region_policy=region_policy())
        self.assertEqual(rec["rc"], 4)
        self.assertEqual(rec["reason_codes"], ["engine_pin_stale"])

    def test_region_pin_and_encoding_required(self):
        for field in ("residual_manifest_sha256", "comparison_encoding"):
            vision = valid_vision()
            del vision["views"][0][field]
            with self.assertRaises(api.MetricError) as ctx:
                api.combine(valid_mesh(), vision, region_policy=region_policy())
            self.assertEqual(ctx.exception.rc, 2)

    def test_blank_capture_cannot_pass(self):
        vision = valid_vision()
        vision["views"][0]["reference_foreground_pixels"] = 0
        with self.assertRaises(api.MetricError) as ctx:
            api.combine(valid_mesh(), vision, region_policy=region_policy())
        self.assertEqual(ctx.exception.rc, 3)

    def test_proven_mesh_fail_not_overwritten_by_incomplete_vision(self):
        rec = api._receipt()
        measured = valid_mesh()
        measured.update(rc=5, status="FAIL")
        measured["failed"] = ["topology:boundary"]
        rec["mesh_receipt"] = measured
        self.assertEqual(api._finish(rec, 3, ["view_missing"])["rc"], 5)

    def test_local_region_guard_not_diluted_by_view_average(self):
        vision = valid_vision()
        row = vision["views"][1]
        row.update(candidate_frame_sha256="b" * 64, changed_pixels=1,
                   changed_pixels_ppm=245, mean_abs_delta_ppm=245,
                   regions=[{"id": "r:test", "spans": [["0000", "0000"]]}],
                   region_scores=[{"id": "r:test", "spans": [["0000", "0000"]],
                                   "pixel_count": 1, "changed_pixels": 1,
                                   "score_ppm": 0, "changed_pixels_ppm": 1000000,
                                   "mean_abs_delta_ppm": 1000000}])
        p = dict(region_policy(), max_changed_pixels_ppm=1000, max_mean_abs_delta_ppm=1000)
        rc, reasons, _ = api.combine(valid_mesh(), vision, region_policy=p)
        self.assertEqual(rc, 5)
        self.assertIn("back:region:changed_pixels_ppm", reasons)


class RealCpuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        body = box((-400000, -600000, -300000), (400000, 600000, 300000))
        arm = box((400000, -150000, -150000), (1400000, 150000, 150000))
        short = box((400000, -150000, -150000), (800000, 150000, 150000))
        cls.paths = {}
        fixtures = {"reference": glb([body, arm]), "shortened": glb([body, short]),
                    "white_cube": glb([box((-600000,) * 3, (600000,) * 3)]),
                    "rear_reference": glb([box((-500000,) * 3, (500000,) * 3, rear_center=True)]),
                    "rear_damaged": glb([box((-500000,) * 3, (500000,) * 3,
                                             rear_center=True, dent_um=30000)])}
        for name, data in fixtures.items():
            cls.paths[name] = cls.root / (name + ".glb")
            cls.paths[name].write_bytes(data)
        cls.results = {}
        for name in ("identity", "repeat", "shortened", "white_cube", "rear_damage"):
            rear = name == "rear_damage"
            raw = cls.paths["rear_reference" if rear else "reference"]
            candidate = cls.paths["rear_damaged" if rear else name if name in cls.paths else "reference"]
            cls.results[name] = api.evaluate(raw, candidate, mesh_policy=mesh_policy(1 if rear else 2),
                                             region_policy=region_policy())
        # Optional evidence destination belongs to the operator-assigned output
        # surface, never a caller path used by the evaluator itself.
        out = os.environ.get("SCULPT_METRIC_EVIDENCE")
        if out:
            target = Path(out)
            target.mkdir(parents=True, exist_ok=True)
            for name, receipt in cls.results.items():
                core.atomic_write_json(target / (name + ".json"), receipt)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_identity_twice_byte_identical(self):
        a, b = self.results["identity"], self.results["repeat"]
        self.assertEqual(a["rc"], 0, a)
        self.assertEqual(a["score_ppm"], 1000000)
        self.assertEqual(core.canonical_json(a), core.canonical_json(b))
        self.assertEqual(len(a["views"]), 6)
        self.assertTrue(all(row["changed_pixels"] == 0 for row in a["views"]))

    def test_real_white_cube_ranks_last_and_shortened_limb_fails(self):
        receipts = [self.results[name] for name in ("white_cube", "identity", "shortened")]
        self.assertEqual(self.results["shortened"]["rc"], 5, self.results["shortened"])
        self.assertEqual(self.results["white_cube"]["rc"], 5, self.results["white_cube"])
        self.assertEqual(api.rank(receipts)[-1]["candidate_sha256"], self.results["white_cube"]["candidate_sha256"])

    def test_real_back_damage_front_unchanged_back_has_regions(self):
        result = self.results["rear_damage"]
        self.assertEqual(result["rc"], 5, result)
        # Dent is below the existing surface-distance guard and does not alter
        # silhouettes: only the actual rear normal witness must catch this case.
        self.assertEqual(result["mesh_receipt"]["rc"], 0, result)
        views = {row["view"]: row for row in result["views"]}
        self.assertEqual(views["front"]["changed_pixels"], 0)
        self.assertGreater(views["back"]["changed_pixels"], 0)
        self.assertTrue(views["back"]["regions"], views["back"])
        self.assertIn("back:changed_pixels_ppm", result["reason_codes"])

    def test_real_source_mutation_cannot_publish_success(self):
        path = self.root / "mutating-input.glb"
        original = self.paths["reference"].read_bytes()
        path.write_bytes(original)
        child = api._child
        count = 0
        def mutate_after_render(*args, **kwargs):
            nonlocal count
            result = child(*args, **kwargs)
            if count == 0:
                path.write_bytes(original + b"concurrent caller edit")
            count += 1
            return result
        with mock.patch.object(api, "_child", side_effect=mutate_after_render):
            rec = api.evaluate(path, path, mesh_policy=mesh_policy(2), region_policy=region_policy())
        self.assertEqual(rec["rc"], 4, rec)
        self.assertEqual(rec["reason_codes"], ["source_stale"])
        self.assertEqual(path.read_bytes(), original + b"concurrent caller edit")


if __name__ == "__main__":
    unittest.main()
