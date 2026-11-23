"""AG2 M3: hull_gen (host verifier + worker contract) and ortho4 (VG-0249). Fake-runner tests always run; the real
bmesh worker needs BVQA_BLENDER=1:
BVQA_BLENDER=1 taskset -c 5-9 python3 -m unittest tests.test_ag2_hull -v
"""
import json
import math
import os
import shutil
import tempfile
import unittest
from unittest import mock

from blender_visual_qa.assets import glb_core, hull_gen, ortho4

UM = 1000000
CUBE_FACES = (  # outward quads over corner sign signatures (sx, sy, sz)
    ((-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)),
    ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)),
    ((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)),
    ((-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)),
    ((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)),
    ((1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)),
)


def prim(name, pts, idx):
    return glb_core.Prim(name=name, node_path="/" + name, world_q16=glb_core.IDENTITY_Q16,
                         positions_um=[tuple(p) for p in pts], indices=list(idx))


def cube(half, centre=(0, 0, 0)):
    sig = [(sx, sy, sz) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    pts = [tuple(centre[i] + s[i] * half for i in range(3)) for s in sig]
    idx = []
    for a, b, c, d in CUBE_FACES:
        q = [sig.index(v) for v in (a, b, c, d)]
        idx += [q[0], q[1], q[2], q[0], q[2], q[3]]
    return pts, idx


def pyramid(n, r=UM, h=UM):
    """n-gon pyramid: n + 1 vertices, 2n - 2 outward triangles."""
    pts = [(int(r * math.cos(2 * math.pi * k / n)), 0, int(r * math.sin(2 * math.pi * k / n))) for k in range(n)]
    pts.append((0, h, 0))
    idx = []
    for k in range(n):
        idx += [n, (k + 1) % n, k]
    for k in range(1, n - 1):
        idx += [0, k, k + 1]
    return pts, idx


def tetra(sx=2 * UM, sy=UM, sz=UM, off=(0, 0, 0)):
    pts = [off, (off[0] + sx, off[1], off[2]), (off[0], off[1] + sy, off[2]), (off[0], off[1], off[2] + sz)]
    return pts, [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3]


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def write(path, prims):
    with open(path, "wb") as fh:
        fh.write(glb_core.write_glb(glb_core.Scene(prims=prims)))
    return path


def cube_runner(transform=None):
    """Fake worker: answers the exact cube triangulation of the 8 points it is given (indices into the point set)."""
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        with open(argv[argv.index("--points") + 1]) as fh:
            pts = json.load(fh)["points_um"]
        runner.sent.append([tuple(p) for p in pts])
        cen = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
        sig = {tuple(1 if p[i] > cen[i] else -1 for i in range(3)): k for k, p in enumerate(pts)}
        tris = []
        for face in CUBE_FACES:
            q = [sig[v] for v in face]
            tris += [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]
        if transform:
            tris = transform(tris)
        return 0, "BVQA_HULL " + json.dumps({"tris": tris, "exact": True}) + "\n", ""
    runner.calls = calls
    runner.sent = []
    return runner


class HullBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ag2_hull_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def p(self, name):
        return os.path.join(self.tmp, name)

    def ladder(self, name="crate.glb"):
        lod0 = prim("crate_LOD0", *cube(3 * UM // 4))
        lod2 = prim("crate_LOD2", *cube(UM // 2))
        col = prim("crate-col", *cube(UM))
        return write(self.p(name), [lod0, lod2, col])


class TestHullGen(HullBase):
    def test_ladder_hull_pass_and_reparse(self):
        r = cube_runner()
        rc, rec = hull_gen.run(["--in", self.ladder(), "--out", self.p("out/hull.glb")], runner=r)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["node"], "crate-convcol")
        self.assertEqual((rec["metrics"]["V"], rec["metrics"]["F"]), (8, 12))
        self.assertEqual(rec["metrics"]["containment_ppm"], 0)
        self.assertEqual(rec["metrics"]["support_um"], 0)
        hs = glb_core.read_glb(self.p("out/hull.glb"))
        self.assertEqual([p.name for p in hs.prims], ["crate-convcol"])
        on_disk = json.loads(read(self.p("out/hull_receipt.json")))
        self.assertEqual(on_disk, rec)
        self.assertEqual(rec["artefact_sha256"], glb_core.sha256(read(self.p("out/hull.glb"))))
        self.assertEqual(rec["schema"], "bvqa.asset-hull-gen/v1")
        # LOD0 and the -col helper (bigger) are never part of the source: the worker gets the 8 LOD2 points only
        self.assertEqual(len(r.calls), 1)
        self.assertEqual(r.sent, [sorted(set(cube(UM // 2)[0]))])

    def test_prev_receipt_chain(self):
        rc, rec = hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb")], runner=cube_runner())
        rc2, rec2 = hull_gen.run(["--in", self.ladder(), "--out", self.p("h2.glb"), "--receipt", self.p("r2.json"),
                                  "--prev-receipt", self.p("hull_receipt.json")], runner=cube_runner())
        self.assertEqual(rc2, 0)
        self.assertEqual(rec2["prev_sha256"], glb_core.sha256(read(self.p("hull_receipt.json"))))

    def test_mutant_collision_input_refused(self):
        src = write(self.p("crate_collision.glb"), [prim("crate", *cube(UM // 2))])
        r = cube_runner()
        rc, rec = hull_gen.run(["--in", src, "--out", self.p("h.glb")], runner=r)
        self.assertEqual(rc, 5)
        self.assertEqual(rec["failed"], ["collision_helper_input"])
        self.assertFalse(os.path.exists(self.p("h.glb")))
        self.assertEqual(r.calls, [])
        helpers_only = write(self.p("crate.glb"), [prim("crate-convcol", *cube(UM // 2))])
        self.assertEqual(hull_gen.run(["--in", helpers_only, "--out", self.p("h.glb")], runner=r)[0], 5)

    def test_mutant_inward_winding(self):
        r = cube_runner(lambda tris: [[a, c, b] for a, b, c in tris])
        rc, rec = hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb")], runner=r)
        self.assertEqual(rc, 5)
        self.assertIn("outward", rec["failed"])

    def test_mutant_open_hull(self):
        r = cube_runner(lambda tris: tris[:-1])
        rc, rec = hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb")], runner=r)
        self.assertEqual(rc, 5)
        self.assertIn("watertight", rec["failed"])

    def test_degenerate_and_worker_errors(self):
        flat = lambda argv, t: (0, 'BVQA_HULL {"error": "degenerate input: zero-volume point set"}\n', "")
        rc, rec = hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb")], runner=flat)
        self.assertEqual((rc, rec["failed"]), (5, ["hull_degenerate"]))
        crash = lambda argv, t: (1, "", "Traceback")
        self.assertEqual(hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb")], runner=crash)[0], 2)
        oob = lambda argv, t: (0, 'BVQA_HULL {"tris": [[0, 1, 99]]}\n', "")
        self.assertEqual(hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb")], runner=oob)[0], 2)

        def slow(argv, t):
            raise hull_gen.HullError(6, "E_TIMEOUT", "blender worker")
        self.assertEqual(hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb")], runner=slow)[0], 6)

    def test_usage_io_empty(self):
        self.assertEqual(hull_gen.run(["--in", self.p("missing.glb"), "--out", self.p("h.glb")])[0], 2)
        with open(self.p("junk.glb"), "wb") as fh:
            fh.write(b"not a glb")
        self.assertEqual(hull_gen.run(["--in", self.p("junk.glb"), "--out", self.p("h.glb")])[0], 2)
        self.assertEqual(hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb"), "--max-verts", "3"])[0], 2)
        self.assertEqual(hull_gen.run(["--verify-only"])[0], 2)
        empty = write(self.p("empty.glb"), [])
        self.assertEqual(hull_gen.run(["--in", empty, "--out", self.p("h.glb")])[0], 3)
        self.assertEqual(hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb"), "--lod", "LOD1"])[0], 3)


class TestVerifyOnly(HullBase):
    def verify(self, hull_prims, source_prims=None, extra=()):
        h = write(self.p("hull.glb"), hull_prims)
        argv = ["--verify-only", "--hull", h] + list(extra)
        if source_prims:
            argv += ["--source", write(self.p("src.glb"), source_prims), "--lod", "ALL"]
        return hull_gen.run(argv)

    def test_cube_hull_pass(self):
        rc, rec = self.verify([prim("box-convcol", *cube(UM))], [prim("box", *cube(UM))])
        self.assertEqual(rc, 0, rec)

    def test_mutant_hull_33_verts(self):
        rc, rec = self.verify([prim("box-convcol", *pyramid(32))])
        self.assertEqual(rc, 5)
        self.assertIn("vertices_range", rec["failed"])
        self.assertEqual(rec["metrics"]["V"], 33)
        self.assertEqual(self.verify([prim("box-convcol", *pyramid(31))])[0], 0)  # boundary: V = 32, F = 60
        self.assertEqual(self.verify([prim("box-convcol", *pyramid(16))])[0], 0)

    def test_mutant_dented_not_convex(self):
        pts, idx = cube(UM)
        pts[7] = (UM // 4, UM // 4, UM // 4)  # corner (+,+,+) pushed inside
        rc, rec = self.verify([prim("box-convcol", pts, idx)])
        self.assertEqual(rc, 5)
        self.assertIn("convex", rec["failed"])

    def test_mutant_support_exceeded(self):
        rc, rec = self.verify([prim("box-convcol", *cube(UM // 2))], [prim("box", *cube(UM))])
        self.assertEqual(rc, 5)
        self.assertIn("support", rec["failed"])
        self.assertGreater(rec["metrics"]["containment_ppm"], 0)
        # within tau: a hull 1% larger than the source passes
        self.assertEqual(self.verify([prim("box-convcol", *cube(UM + UM // 100))], [prim("box", *cube(UM))])[0], 0)

    def test_mutant_name_and_multi_node(self):
        rc, rec = self.verify([prim("box_hull", *cube(UM))])
        self.assertEqual(rc, 5)
        self.assertIn("convcol_name", rec["failed"])
        rc, rec = self.verify([prim("a-convcol", *cube(UM)), prim("b-convcol", *cube(UM, (3 * UM, 0, 0)))])
        self.assertIn("single_hull_node", rec["failed"])
        rc, rec = self.verify([prim("a-convcol", *cube(UM)), prim("b-convcol", *cube(UM, (3 * UM, 0, 0)))],
                              extra=["--hull-node", "a-convcol"])
        self.assertEqual(rc, 0, rec)

    def test_flat_hull_fails_volume(self):
        pts = [(0, 0, 0), (UM, 0, 0), (0, UM, 0), (UM, UM, 0)]
        rc, rec = self.verify([prim("f-convcol", pts, [0, 1, 2, 2, 1, 3, 0, 2, 1, 2, 3, 1])])
        self.assertEqual(rc, 5)
        self.assertIn("volume_nonzero", rec["failed"])


class TestOrtho4(HullBase):
    def gate(self, prims, extra=()):
        return ortho4.run(["--in", write(self.p("o.glb"), prims)] + list(extra))

    def test_tetra_pass_rig_consistent(self):
        rc, rec = self.gate([prim("t", *tetra())])
        self.assertEqual(rc, 0, rec)
        self.assertFalse(rec["rig_fault"] or rec["asset_fault"])
        self.assertEqual(rec["schema"], "bvqa.asset-ortho4/v1")
        for c in rec["checks"]:
            if c["id"].startswith("mirror_"):
                self.assertEqual(c["value"], 0)
        self.assertEqual(self.gate([prim("c", *cube(UM))])[0], 0)

    def test_silhouette_shapes(self):
        tris = ortho4.scene_triangles(glb_core.Scene(prims=[prim("t", *tetra())]))
        sil = ortho4.silhouettes(tris, 8)
        front = ["".join("#" if (r >> c) & 1 else "." for c in range(8)) for r in sil["front"]]
        self.assertEqual(front[0], "........")  # x extent 2, y extent 1: the top quarter is empty
        self.assertTrue(front[5].startswith("#"))  # right angle bottom-left (+x to the right)
        self.assertEqual(sil["back"], ortho4.mirror(sil["front"], 8))

    def test_mutant_back_view_not_mirrored(self):
        axes = dict(ortho4.VIEW_AXES, back=(0, 1, 1, 1))
        with mock.patch.object(ortho4, "VIEW_AXES", axes):
            rc, rec = self.gate([prim("t", *tetra())])
        self.assertEqual(rc, 5)
        self.assertTrue(rec["rig_fault"])
        self.assertIn("mirror_front_back", rec["failed"])

    def test_mutant_right_camera_rolled(self):
        axes = dict(ortho4.VIEW_AXES, right=(2, -1, 0, 1))
        with mock.patch.object(ortho4, "VIEW_AXES", axes):
            rc, rec = self.gate([prim("t", *tetra())])
        self.assertEqual(rc, 5)
        self.assertTrue(rec["rig_fault"])
        self.assertIn("equal_row_sets", rec["failed"])

    def test_mutant_detached_accessory(self):
        rc, rec = self.gate([prim("t", *tetra()), prim("acc", *tetra(UM // 4, UM // 4, UM // 4, (4 * UM, 0, 0)))])
        self.assertEqual(rc, 5)
        self.assertTrue(rec["asset_fault"])
        self.assertFalse(rec["rig_fault"])
        self.assertEqual(self.gate([prim("t", *tetra()), prim("acc", *tetra(UM // 4, UM // 4, UM // 4, (4 * UM, 0, 0)))],
                                   ["--max-components", "2"])[0], 0)

    def test_empty_flat_usage(self):
        self.assertEqual(self.gate([])[0], 3)
        self.assertEqual(ortho4.run(["--in", self.p("nope.glb")])[0], 2)
        self.assertEqual(ortho4.run([])[0], 2)

    def test_deterministic(self):
        a = self.gate([prim("t", *tetra())])[1]
        b = self.gate([prim("t", *tetra())])[1]
        self.assertEqual(ortho4.canon(a), ortho4.canon(b))


def uv_sphere(r, seg, rings):
    pts = [(0, r, 0), (0, -r, 0)]
    for i in range(1, rings):
        th = math.pi * i / rings
        for j in range(seg):
            ph = 2 * math.pi * j / seg
            pts.append((int(r * math.sin(th) * math.cos(ph)), int(r * math.cos(th)), int(r * math.sin(th) * math.sin(ph))))
    idx = []
    ring = lambda i, j: 2 + (i - 1) * seg + j % seg
    for j in range(seg):
        idx += [0, ring(1, j + 1), ring(1, j)]
        idx += [1, ring(rings - 1, j), ring(rings - 1, j + 1)]
    for i in range(1, rings - 1):
        for j in range(seg):
            a, b, c, d = ring(i, j), ring(i, j + 1), ring(i + 1, j + 1), ring(i + 1, j)
            idx += [a, b, c, a, c, d]
    return pts, idx


@unittest.skipUnless(os.environ.get("BVQA_BLENDER") == "1", "needs BVQA_BLENDER=1 (CPU Blender)")
class TestHullBlender(HullBase):
    def test_real_cube_exact(self):
        rc, rec = hull_gen.run(["--in", self.ladder(), "--out", self.p("h.glb"), "--timeout", "120"])
        self.assertEqual(rc, 0, rec)
        self.assertEqual((rec["metrics"]["V"], rec["metrics"]["F"]), (8, 12))

    def test_real_sphere_greedy_budget(self):
        src = write(self.p("ball.glb"), [prim("ball_LOD2", *uv_sphere(UM, 16, 12))])
        rc, rec = hull_gen.run(["--in", src, "--out", self.p("h.glb"), "--timeout", "120"])
        ok = {c["id"]: c["ok"] for c in rec["checks"]}
        for cid in ("vertices_range", "faces_range", "watertight", "outward", "convex", "volume_nonzero", "convcol_name"):
            self.assertTrue(ok[cid], (cid, rec))
        self.assertLessEqual(rec["metrics"]["V"], 32)
        self.assertLessEqual(rec["metrics"]["F"], 60)
        self.assertEqual(rec["node"], "ball-convcol")
        # an inner approximation of a 170-point sphere is reported honestly against tau, and passes a looser tau
        self.assertEqual(rc, 0 if ok["support"] else 5)
        rc2, rec2 = hull_gen.run(["--in", src, "--out", self.p("h2.glb"), "--timeout", "120", "--tau-ppm", "100000"])
        self.assertEqual(rc2, 0, rec2)

    def test_real_flat_degenerate(self):
        pts = [(0, 0, 0), (UM, 0, 0), (0, 0, UM), (UM, 0, UM)]
        src = write(self.p("flat.glb"), [prim("flat", pts, [0, 2, 1, 1, 2, 3])])
        rc, rec = hull_gen.run(["--in", src, "--out", self.p("h.glb"), "--timeout", "120"])
        self.assertEqual((rc, rec.get("failed")), (5, ["hull_degenerate"]), rec)
        self.assertFalse(os.path.exists(self.p("h.glb")))


if __name__ == "__main__":
    unittest.main()
