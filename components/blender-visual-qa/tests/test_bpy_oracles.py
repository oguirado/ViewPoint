"""Pure-stdlib tests of the bpy_side oracle kernel (no Blender needed): BSPEC_BORACLES section 2.1/8."""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bpy_side"))
import scene_checks as sc  # noqa: E402

FACES = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]


def cube(side, origin=(0, 0, 0), base=0, flip=False, faces=FACES):
    h = side / 2
    P = [tuple(sc.q16(origin[k] + (h if (i >> k) & 1 else -h)) for k in range(3)) for i in range(8)]
    polys = [tuple(f) for f in faces]
    if flip:
        polys = [tuple(reversed(f)) for f in polys]
    return P, polys


def merge(*parts):
    P, polys = [], []
    for p, f in parts:
        off = len(P)
        P += p
        polys += [tuple(v + off for v in poly) for poly in f]
    return P, polys


def tris_of(polys):
    return [(p[0], p[k], p[k + 1]) for p in polys for k in range(1, len(p) - 1)]


def kernel(P, polys, edges=(), degen=sc.DEFAULTS["BO_DEGEN_N2"]):
    return sc.mesh_kernel(P, polys, tris_of(polys), list(edges), degen)


class KernelTests(unittest.TestCase):
    def test_cube_outward_clean(self):
        P, f = cube(0.5)
        m = kernel(P, f)
        self.assertEqual((m["shells"], m["inv_shells"], m["conflict_edges"], m["nm_edges"], m["pinched_verts"],
                          m["boundary_edges"], m["degen_tris"], m["dup_faces"]), (1, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(m["sigmas"], [211106232532992])  # 6 * 0.125 * 2^48, VG-0229 4.5

    def test_shell_m5_global_sum_hides_inverted_shell(self):
        small, big = cube(0.5), cube(2.0, (5, 0, 0))
        P, f = merge(small, big)
        self.assertEqual(kernel(P, f)["sigmas"], [211106232532992, 13510798882111488])
        P, f = merge(cube(0.5, flip=True), big)
        m = kernel(P, f)
        self.assertEqual(m["sigmas"], [-211106232532992, 13510798882111488])
        self.assertEqual(m["inv_shells"], 1)
        self.assertGreater(sum(m["sigmas"]), 0)  # a single global accumulator would pass this

    def test_all_flipped_and_zero_volume(self):
        P, f = cube(0.5, flip=True)
        self.assertEqual(kernel(P, f)["inv_shells"], 1)
        # double-sided flat quad: closed (every edge valence 2), volume exactly 0 -> Sigma <= 0 must FAIL
        P = [(0, 0, 0), (65536, 0, 0), (65536, 65536, 0), (0, 65536, 0)]
        m = kernel(P, [(0, 1, 2, 3), (3, 2, 1, 0)])
        self.assertEqual((m["sigmas"], m["inv_shells"], m["conflict_edges"], m["boundary_edges"]), ([0], 1, 0, 0))

    def test_partial_flip_conflicts(self):
        P, f = cube(0.5)
        f = [tuple(reversed(f[0]))] + f[1:]
        self.assertEqual(kernel(P, f)["conflict_edges"], 4)  # M7

    def test_open_plane_and_deleted_face(self):
        P, f = cube(0.5)
        m = kernel(P, f[1:])
        self.assertEqual((m["boundary_edges"], m["inv_shells"]), (4, 0))  # open shell is not judged by sigma
        P = [(0, 0, 0), (65536, 0, 0), (65536, 65536, 0), (0, 65536, 0)]
        self.assertEqual(kernel(P, [(0, 1, 2, 3)])["boundary_edges"], 4)

    def test_bowtie_is_pinched(self):  # M6
        P, f = cube(0.5)
        P = P + [(P[0][0] - 9000, P[0][1] - 9000, P[0][2]), (P[0][0] - 9000, P[0][1], P[0][2] - 9000)]
        m = kernel(P, f + [(0, 8, 9)])
        self.assertEqual((m["pinched_verts"], m["nm_edges"], m["boundary_edges"]), (1, 0, 3))

    def test_nonmanifold_edge_valence_three(self):
        P, f = cube(0.5)
        P = P + [(0, 0, 100000)]
        m = kernel(P, f + [(0, 1, 8)])
        self.assertEqual(m["nm_edges"], 1)

    def test_loose_vertex_and_edge(self):
        P, f = cube(0.5)
        P = P + [(1, 2, 3), (4, 5, 6), (7, 8, 9)]
        m = kernel(P, f, edges=[(8, 9)])
        self.assertEqual((m["loose_verts"], m["loose_edges"]), (1, 1))

    def test_degenerate_boundary_is_exact(self):  # C3: |n|^2 <= 7378
        for (a, b), want in ((((0, 5, 8), (9, 4, 9)), 1), (((0, 1, 9), (5, 9, 8)), 0)):
            m = kernel([(0, 0, 0), a, b], [(0, 1, 2)])
            self.assertEqual(m["degen_tris"], want, (a, b))
        m = kernel([(0, 0, 0), (0, 5, 8), (9, 4, 9)], [(0, 1, 2)], degen=7377)
        self.assertEqual(m["degen_tris"], 0)  # mutant cm.degen_lt analogue: strict < would pass 7378

    def test_duplicate_faces(self):
        P, f = cube(0.5)
        self.assertEqual(kernel(P, f + [tuple(reversed(f[0]))])["dup_faces"], 1)

    def test_negative_determinant_local_sigma_unchanged(self):
        # local-space kernel: mirroring is a matrix_world property, so local sigma stays positive (.18 equivalent)
        P, f = cube(0.5)
        self.assertEqual(kernel(P, f)["inv_shells"], 0)


class UvTests(unittest.TestCase):
    @staticmethod
    def grid(n, scale_faces=()):
        P, polys, uv, loops = [], [], [], []
        for j in range(n + 1):
            for i in range(n + 1):
                P.append((i * 65536, j * 65536, 0))
        for j in range(n):
            for i in range(n):
                a = j * (n + 1) + i
                polys.append((a, a + 1, a + n + 2, a + n + 1))
        for k, poly in enumerate(polys):
            c = [(P[v][0], P[v][1]) for v in poly]
            f = 8 if k in scale_faces else 1
            cx, cy = sum(x for x, _ in c) // 4, sum(y for _, y in c) // 4
            loops.append(list(range(len(uv), len(uv) + 4)))
            uv += [(cx + (x - cx) * f, cy + (y - cy) * f) for x, y in c]
        return P, uv, polys, loops

    def test_clean_grid_pass_and_stretched_grid_fail(self):
        clean = sc.uv_stats(*self.grid(4))
        self.assertEqual((clean["tv_ppm"], clean["degenerate_ppm"], clean["sigma_q16"]), (0, 0, 0))
        bad = sc.uv_stats(*self.grid(4, scale_faces=range(0, 16, 2)))
        self.assertGreater(bad["tv_ppm"], sc.DEFAULTS["BO_UV_TV_PPM"])
        self.assertTrue(sc.relative_sigma_fails(bad["sigma_q16"], clean["sigma_q16"], sc.DEFAULTS["BO_UV_SIGMA_MULT_Q16"]))

    def test_vg0252_witness_multiplier_boundary(self):
        self.assertEqual(sc.sigma_q16([0, 2]), 1)
        self.assertEqual(sc.sigma_q16([0, 4]), 2)
        self.assertFalse(sc.relative_sigma_fails(2, 1, 131072))
        self.assertTrue(sc.relative_sigma_fails(2, 1, 131071))

    def test_empty_list_fails(self):
        self.assertTrue(sc.relative_sigma_fails(0, 0, 131072, empty=True))
        self.assertEqual(sc.uv_stats([], [], [], [])["empty"], 1)

    def test_degenerate_uv_faces(self):
        P, uv, polys, loops = self.grid(2)
        uv = [(0, 0)] * 4 + uv[4:]
        s = sc.uv_stats(P, uv, polys, loops)
        self.assertEqual(s["degenerate_ppm"], 250000)


class SourceGateTests(unittest.TestCase):
    def test_no_float_literal_in_comparisons(self):  # g_nofloat
        for name in ("scene_checks.py", "mutate_ops.py", "apply_mutant.py"):
            with open(os.path.join(ROOT, "bpy_side", name)) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    for part in ast.walk(node):
                        self.assertFalse(isinstance(part, ast.Constant) and type(part.value) is float, (name, node.lineno))

    def test_load_thresholds_defaults_and_override(self):
        th = sc.load_thresholds(os.path.join(ROOT, "catalogue", "thresholds.tsv"))
        self.assertEqual(th["BO_UV_TV_PPM"], 150000)
        self.assertEqual(th["BO_DEGEN_N2"], 7378)

    def test_thresholds_have_no_silent_fallback(self):  # JB3-8
        tsv = os.path.join(ROOT, "catalogue", "thresholds.tsv")
        with open(tsv) as fh:
            lines = fh.read().splitlines()
        ids = {l.split("\t")[0] for l in lines[1:]}
        self.assertEqual(sorted(set(sc.DEFAULTS) - ids), [])  # DEFAULTS <-> catalogue coverage
        for k, v in sc.DEFAULTS.items():  # catalogue carries the specified values
            self.assertEqual(sc.load_thresholds(tsv)[k], v, k)
        self.assertEqual(sc.load_thresholds(tsv)["BO_CLIP_FAIL_CORNERS"], 8)  # JB3-4, BORACLES 2.2
        import tempfile
        with self.assertRaises(sc.DriveError) as c:
            sc.load_thresholds("")
        self.assertEqual(c.exception.code, "E_SCHEMA")
        for k in sorted(sc.DEFAULTS):  # deleting ANY required row must fail
            with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, dir=os.path.join(ROOT, "build")) as tmp:
                tmp.write("\n".join(l for l in lines if l.split("\t")[0] != k) + "\n")
            try:
                with self.assertRaises(sc.DriveError, msg=k) as c:
                    sc.load_thresholds(tmp.name)
                self.assertIn(k, c.exception.reason)
            finally:
                os.unlink(tmp.name)

    def test_rows_trailer_and_cannot_see_count(self):
        r = sc.Rows()
        r.cannot("bo.mesh.non_manifold", "B", "untagged")
        r.sd("bo.mesh.degenerate", "A", None, {"degen_tris": 1}, {}, True, "x")
        out = r.final()
        self.assertEqual([x["oracle_id"] for x in out[:-1]], ["bo.mesh.degenerate", "bo.mesh.non_manifold"])
        self.assertEqual(out[-1]["summary"], {"rows": 2, "fail": 1, "cannot_see_count": 1, "advisory": 0})

    def test_ops_cover_manifest_scene_operations(self):
        import json
        sys.modules.setdefault("bpy", type(sys)("bpy"))
        try:
            import mutate_ops
        finally:
            if not hasattr(sys.modules["bpy"], "data"):
                del sys.modules["bpy"]
        with open(os.path.join(ROOT, "adapters", "example_scene", "mutant-manifest.json")) as f:
            m = json.load(f)
        ops = {e["fault"]["operation"] for e in m["mutants"] if e["kind"] == "scene"}
        self.assertEqual(ops - set(mutate_ops.OPS), set())


class ZFightGeometryTests(unittest.TestCase):
    def test_tri_overlap_2d(self):
        A = [(0, 0), (10, 0), (0, 10)]
        self.assertTrue(sc.tri_overlap_2d(A, [(1, 1), (5, 1), (1, 5)]))
        self.assertFalse(sc.tri_overlap_2d(A, [(10, 0), (20, 0), (10, 10)]))  # touching at a vertex
        self.assertFalse(sc.tri_overlap_2d(A, [(20, 20), (30, 20), (20, 30)]))


def mutated(old, new):
    """Reviewer code mutant: scene_checks.py source with one edit, as a fresh module (must contain `old` exactly once)."""
    import types
    with open(os.path.join(ROOT, "bpy_side", "scene_checks.py")) as fh:
        src = fh.read()
    assert src.count(old) == 1, old
    mod = types.ModuleType("scene_checks_mut")
    mod.__file__ = os.path.join(ROOT, "bpy_side", "scene_checks.py")
    exec(compile(src.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    return mod


class ReviewerMutantKernelTests(unittest.TestCase):
    """BORACLES section 8 pure-kernel reviewer mutants: each must be caught by a named fixture assertion (JB3-7)."""

    def run_fixtures(self, mod):
        P, f = merge(cube(0.5, flip=True), cube(2.0, (5, 0, 0)))
        shell = mod.mesh_kernel(P, f, tris_of(f), [], 7378)["inv_shells"] == 1  # T-BO-SHELL
        Pz = [(0, 0, 0), (65536, 0, 0), (65536, 65536, 0), (0, 65536, 0)]
        fz = [(0, 1, 2, 3), (3, 2, 1, 0)]
        zero = mod.mesh_kernel(Pz, fz, tris_of(fz), [], 7378)["inv_shells"] == 1  # zero-volume closed shell
        P1, f1 = cube(0.5)
        P1 = P1 + [(P1[0][0] - 9000, P1[0][1] - 9000, P1[0][2]), (P1[0][0] - 9000, P1[0][1], P1[0][2] - 9000)]
        f1 = f1 + [(0, 8, 9)]
        bow = mod.mesh_kernel(P1, f1, tris_of(f1), [], 7378)["pinched_verts"] == 1  # .20
        degen = mod.mesh_kernel([(0, 0, 0), (0, 5, 8), (9, 4, 9)], [(0, 1, 2)], [(0, 1, 2)], [], 7378)["degen_tris"] == 1
        return {"shell": shell, "zero": zero, "bow": bow, "degen": degen}

    def test_nominal_passes_all_fixtures(self):
        self.assertEqual(self.run_fixtures(sc), {"shell": True, "zero": True, "bow": True, "degen": True})

    def test_cm_le_zero_killed(self):
        m = mutated("sigma[r] <= 0)", "sigma[r] < 0)")
        self.assertFalse(self.run_fixtures(m)["zero"])

    def test_cm_degen_lt_killed(self):
        m = mutated("_dot(n, n) <= degen_n2", "_dot(n, n) < degen_n2")
        self.assertFalse(self.run_fixtures(m)["degen"])

    def test_cm_no_pinch_killed(self):
        m = mutated("if len({cf(fi) for fi, _, _ in inc}) > 1 and worst <= 2:", "if False:")
        self.assertFalse(self.run_fixtures(m)["bow"])


if __name__ == "__main__":
    unittest.main()
