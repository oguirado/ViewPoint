"""Unit and negative-path tests for blender_visual_qa.assets.inspect_gate."""
import json
from pathlib import Path
import tempfile
import unittest

from blender_visual_qa.assets import glb_core as g
from blender_visual_qa.assets import inspect_gate as ig

Q16 = g.Q16
S = g.UM_PER_M
CELL = 16384
INSET = 1024

FACES = (
    ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)),
    ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)),
    ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)),
    ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)),
    ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)),
    ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)),
)
NORMALS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def make_valid_matrix(classes=None, aliases=None):
    if classes is None:
        pol = {k: 0 for k in ig.POLICY_KEYS}
        pol.update({
            "tris_min": 1,
            "tris_max": 1000,
            "scale_min_um": 1000,
            "scale_max_um": 2000000,
            "pivot_y_tol_um": 1000,
            "pivot_xz_tol_um": 1000,
            "normal_len_tol_q16": 6553,
            "uv_range_slack_q16": 655,
            "materials_max": 10,
            "unassigned_material_max": 0,
            "skinned_allowed": 0,
        })
        classes = {"prop": pol}
    matrix = {
        "schema": "bvqa.class-gate-matrix/v1",
        "aliases": aliases or {},
        "classes": classes,
    }
    matrix["signed"] = {"body_sha256": ig.body_sha256(matrix)}
    return matrix


def make_unit_cube_prim(name="cube", world=g.IDENTITY_Q16, material="mat0"):
    # Hand-built closed unit cube (1x1x1 m = 1,000,000 um)
    # 24 vertices (4 per face) with distinct non-overlapping UV cells per face
    origin = (-S // 2, 0, -S // 2)
    pos, idx, uvs, nrm = [], [], [], []
    for f, quad in enumerate(FACES):
        cell = f
        cu, cv = (cell % 4) * CELL + INSET, (cell // 4) * CELL + INSET
        k = CELL - 2 * INSET
        n = len(pos)
        for c, uv in zip(quad, ((0, 0), (k, 0), (k, k), (0, k))):
            pos.append(tuple(origin[i] + c[i] * S for i in range(3)))
            uvs.append((cu + uv[0], cv + uv[1]))
            nrm.append(tuple(x * Q16 for x in NORMALS[f]))
        idx += [n, n + 1, n + 2, n, n + 2, n + 3]
    return g.Prim(name=name, node_path="/" + name, world_q16=tuple(world),
                  positions_um=pos, indices=idx, uvs_q16=uvs, normals_q16=nrm,
                  material=material)


class TestMatrixError(unittest.TestCase):
    def test_matrix_error_attributes_and_str(self):
        err = ig.MatrixError(ig.RC_USAGE, "test reason")
        self.assertIsInstance(err, Exception)
        self.assertEqual(err.rc, ig.RC_USAGE)
        self.assertEqual(err.reason, "test reason")
        self.assertEqual(str(err), "test reason")


class TestBodySha256(unittest.TestCase):
    def test_key_order_independence_and_signed_exclusion(self):
        m1 = {"schema": "v1", "b": 2, "a": 1, "signed": {"body_sha256": "ignore_me"}}
        m2 = {"a": 1, "schema": "v1", "b": 2}
        sha1 = ig.body_sha256(m1)
        sha2 = ig.body_sha256(m2)
        self.assertEqual(sha1, sha2)
        self.assertEqual(len(sha1), 64)

    def test_body_sha256_negative_case_diff_content(self):
        m1 = {"a": 1}
        m2 = {"a": 2}
        self.assertNotEqual(ig.body_sha256(m1), ig.body_sha256(m2))


class TestLoadPolicy(unittest.TestCase):
    def test_load_policy_success(self):
        matrix = make_valid_matrix(aliases={"prop_alias": "prop"})
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "matrix.json"
            p.write_text(json.dumps(matrix))

            pol, sha = ig.load_policy(str(p), "prop")
            self.assertEqual(pol["_class"], "prop")
            self.assertEqual(sha, ig.body_sha256(matrix))

            # Test alias resolution
            pol_alias, sha_alias = ig.load_policy(str(p), "prop_alias")
            self.assertEqual(pol_alias["_class"], "prop")
            self.assertEqual(sha_alias, sha)

    def test_load_policy_rc2_malformed_json_and_io(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "matrix.json"
            p.write_text("{bad json")
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

            non_existent = Path(tmpdir) / "non_existent.json"
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(non_existent), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

    def test_load_policy_rc2_invalid_schema_and_types(self):
        m = make_valid_matrix()
        m["schema"] = "wrong.schema/v1"
        m["signed"]["body_sha256"] = ig.body_sha256(m)
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

    def test_load_policy_rc2_signed_block_malformed(self):
        m = make_valid_matrix()
        m["signed"] = "not_a_dict"
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

    def test_load_policy_rc2_class_not_found(self):
        m = make_valid_matrix()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "nonexistent_class")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

    def test_load_policy_rc2_unknown_and_missing_keys(self):
        m = make_valid_matrix()
        m["classes"]["prop"]["unknown_key"] = 10
        m["signed"]["body_sha256"] = ig.body_sha256(m)
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

        m = make_valid_matrix()
        del m["classes"]["prop"]["closed"]
        m["signed"]["body_sha256"] = ig.body_sha256(m)
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

    def test_load_policy_rc2_non_integer_policy_value(self):
        m = make_valid_matrix()
        m["classes"]["prop"]["tris_min"] = 1.5
        m["signed"]["body_sha256"] = ig.body_sha256(m)
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_USAGE)

    def test_load_policy_rc3_unsigned_matrix(self):
        m = make_valid_matrix()
        del m["signed"]
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_INCOMPLETE)

    def test_load_policy_rc4_stale_sha_and_expected_sha_mismatch(self):
        m = make_valid_matrix()
        m["signed"]["body_sha256"] = "a" * 64
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop")
            self.assertEqual(cm.exception.rc, ig.RC_STALE)

        m = make_valid_matrix()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "m.json"
            p.write_text(json.dumps(m))
            with self.assertRaises(ig.MatrixError) as cm:
                ig.load_policy(str(p), "prop", expect_sha="b" * 64)
            self.assertEqual(cm.exception.rc, ig.RC_STALE)


class TestAnalyseGroup(unittest.TestCase):
    def test_analyse_group_clean_cube(self):
        cube_prim = make_unit_cube_prim()
        res = ig.analyse_group([cube_prim])
        self.assertEqual(res["tris"], 12)
        self.assertEqual(res["vertices"], 8)
        self.assertEqual(res["edges"], 18)
        self.assertEqual(res["degenerate"], 0)
        self.assertEqual(res["duplicate"], 0)
        self.assertEqual(res["nonmanifold_edges"], 0)
        self.assertEqual(res["twin_winding_edges"], 0)
        self.assertEqual(res["boundary_edges"], 0)
        self.assertEqual(res["pinched"], 0)
        # Volume of 1 m3 cube in um3 = 1e18 um3, volume6_um3 = 6 * 1e18 um3 = 6000000000000000000
        self.assertEqual(res["volume6_um3"], 6 * (S ** 3))

    def test_analyse_group_degenerate_duplicate_nonmanifold_twin_pinched(self):
        # 1. Degenerate triangle (collinear points)
        deg_prim = g.Prim(
            name="deg", node_path="/deg", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (10, 0, 0), (20, 0, 0)],
            indices=[0, 1, 2]
        )
        res_deg = ig.analyse_group([deg_prim])
        self.assertEqual(res_deg["tris"], 0)
        self.assertEqual(res_deg["degenerate"], 1)

        # 2. Duplicate faces
        dup_prim = g.Prim(
            name="dup", node_path="/dup", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (S, 0, 0), (0, S, 0)],
            indices=[0, 1, 2, 0, 1, 2]
        )
        res_dup = ig.analyse_group([dup_prim])
        self.assertEqual(res_dup["tris"], 2)
        self.assertEqual(res_dup["duplicate"], 1)

        # 3. Nonmanifold edge (edge shared by 3 triangles)
        # Triangles: (0,1,2), (0,1,3), (0,1,4) share edge (0,1)
        nm_prim = g.Prim(
            name="nm", node_path="/nm", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (S, 0, 0), (0, S, 0), (0, -S, 0), (0, 0, S)],
            indices=[0, 1, 2, 0, 1, 3, 0, 1, 4]
        )
        res_nm = ig.analyse_group([nm_prim])
        self.assertEqual(res_nm["nonmanifold_edges"], 1)

        # 4. Twin winding edge (two directed edges 0->1)
        tw_prim = g.Prim(
            name="tw", node_path="/tw", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (S, 0, 0), (0, S, 0), (0, -S, 0)],
            indices=[0, 1, 2, 0, 1, 3]
        )
        res_tw = ig.analyse_group([tw_prim])
        self.assertEqual(res_tw["twin_winding_edges"], 1)

        # 5. Pinched vertex (bowtie topology)
        # Two triangles sharing ONLY vertex 0: (0,1,2) and (0,3,4)
        p_prim = g.Prim(
            name="pinched", node_path="/pinched", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (S, 0, 0), (0, S, 0), (-S, 0, 0), (0, -S, 0)],
            indices=[0, 1, 2, 0, 3, 4]
        )
        res_p = ig.analyse_group([p_prim])
        self.assertEqual(res_p["pinched"], 1)


class TestUvTriOverlapAndStats(unittest.TestCase):
    def test_uv_tri_overlap_exact_cases(self):
        # Unit right triangle in q16: vertices (0,0), (65536, 0), (0, 65536)
        t1 = ((0, 0), (Q16, 0), (0, Q16))
        # Strictly interior overlapping triangle
        t2 = ((Q16 // 4, Q16 // 4), (Q16 // 2, 0), (0, Q16 // 2))
        self.assertTrue(ig._uv_tri_overlap(t1, t2))

        # Disjoint triangle
        t3 = ((2 * Q16, 2 * Q16), (3 * Q16, 2 * Q16), (2 * Q16, 3 * Q16))
        self.assertFalse(ig._uv_tri_overlap(t1, t3))

        # Touching at edge/vertex: touching tri sharing edge (0, Q16) to (Q16, 0)
        t4 = ((Q16, Q16), (Q16, 0), (0, Q16))
        self.assertFalse(ig._uv_tri_overlap(t1, t4))

    def test_uv_stats_clean_and_negatives(self):
        # Clean unit square quad made of 2 triangles
        uvs_clean = [(0, 0), (Q16, 0), (Q16, Q16), (0, Q16)]
        indices = [0, 1, 2, 0, 2, 3]
        p_clean = g.Prim(
            name="clean_uv", node_path="/clean_uv", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0)] * 4, indices=indices, uvs_q16=uvs_clean, material="mat0"
        )
        stats = ig.uv_stats([p_clean], slack=655)
        self.assertEqual(stats["uv_tris"], 2)
        self.assertEqual(stats["uv_degenerate"], 0)
        self.assertEqual(stats["uv_out_of_range"], 0)
        self.assertEqual(stats["uv_overlap_pairs"], 0)

        # Degenerate UV
        uvs_deg = [(0, 0), (10, 0), (20, 0)]
        p_deg = g.Prim(
            name="deg_uv", node_path="/deg_uv", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0)] * 3, indices=[0, 1, 2], uvs_q16=uvs_deg, material="mat0"
        )
        stats_deg = ig.uv_stats([p_deg], slack=655)
        self.assertEqual(stats_deg["uv_tris"], 1)
        self.assertEqual(stats_deg["uv_degenerate"], 1)

        # Out of range UV
        uvs_oor = [(-1000, 0), (Q16 + 1000, 0), (0, Q16)]
        p_oor = g.Prim(
            name="oor_uv", node_path="/oor_uv", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0)] * 3, indices=[0, 1, 2], uvs_q16=uvs_oor, material="mat0"
        )
        stats_oor = ig.uv_stats([p_oor], slack=655)
        self.assertEqual(stats_oor["uv_out_of_range"], 2)

        # Overlapping UVs across distinct layouts with same material
        p_ov1 = g.Prim(
            name="ov1", node_path="/ov1", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0)] * 3, indices=[0, 1, 2],
            uvs_q16=[(0, 0), (Q16, 0), (0, Q16)], material="mat0"
        )
        p_ov2 = g.Prim(
            name="ov2", node_path="/ov2", world_q16=g.IDENTITY_Q16,
            positions_um=[(0, 0, 0)] * 3, indices=[0, 1, 2],
            uvs_q16=[(0, 0), (Q16, 0), (Q16, Q16)], material="mat0"
        )
        stats_ov = ig.uv_stats([p_ov1, p_ov2], slack=655)
        self.assertEqual(stats_ov["uv_overlap_pairs"], 1)


class TestInspectScene(unittest.TestCase):
    def test_inspect_scene_empty(self):
        scene = g.Scene(prims=[])
        pol = make_valid_matrix()["classes"]["prop"]
        checks, metrics = ig.inspect_scene(scene, pol)
        self.assertEqual(checks, [])
        self.assertEqual(metrics, {"tris_raw": 0})

    def test_inspect_scene_clean_and_broken(self):
        pol = make_valid_matrix()["classes"]["prop"]

        # Clean scene with unit cube
        clean_prim = make_unit_cube_prim()
        clean_scene = g.Scene(prims=[clean_prim])
        checks_clean, metrics_clean = ig.inspect_scene(clean_scene, pol)

        self.assertEqual(len(checks_clean), 24)
        self.assertTrue(all(c["ok"] for c in checks_clean))
        self.assertEqual(metrics_clean["tris"], 12)
        self.assertEqual(metrics_clean["tris_raw"], 12)

        # Broken scene:
        # Scale too small (10 um cube instead of >=1000 um), unassigned material
        origin = (-5, 0, -5)
        pos_small = []
        for f, quad in enumerate(FACES):
            for c in quad:
                pos_small.append(tuple(origin[i] + c[i] * 10 for i in range(3)))
        broken_prim = g.Prim(
            name="small_cube", node_path="/small_cube", world_q16=g.IDENTITY_Q16,
            positions_um=pos_small, indices=clean_prim.indices,
            uvs_q16=clean_prim.uvs_q16, normals_q16=clean_prim.normals_q16,
            material=None  # Unassigned material
        )
        broken_scene = g.Scene(prims=[broken_prim])
        checks_broken, _ = ig.inspect_scene(broken_scene, pol)

        failed_ids = [c["id"] for c in checks_broken if not c["ok"]]
        self.assertIn("scale_units", failed_ids)
        self.assertIn("materials_assigned", failed_ids)


if __name__ == "__main__":
    unittest.main()
