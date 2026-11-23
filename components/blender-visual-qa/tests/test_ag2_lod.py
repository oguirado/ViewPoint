"""AG2 M2: lod_ladder host verifier + export_runtime Blender worker (router §3.5). Blender tests need BVQA_BLENDER=1."""
import contextlib
import io
import json
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blender_visual_qa.assets import glb_core as g
from blender_visual_qa.assets import inspect_gate as ig
from blender_visual_qa.assets import lod_ladder as L

Q = 65536
BLENDER = os.environ.get("BVQA_BLENDER") == "1" and shutil.which("blender")


def sphere(name, seg, rings, r_um=500000, bump_um=0):
    """Closed UV sphere resting on Y=0 (bottom-centre pivot), seam-split UVs, unit normals. 2*seg*(rings-1) tris."""
    pos, uvs, nrm, idx, grid = [], [], [], [], {}

    def vert(i, j):
        th, ph = math.pi * i / rings, 2 * math.pi * (j % seg) / seg
        n = (math.sin(th) * math.cos(ph), math.cos(th), math.sin(th) * math.sin(ph))
        pos.append(tuple(int(round(c * r_um)) + (r_um if k == 1 else 0) for k, c in enumerate(n)))
        nrm.append(tuple(int(round(c * Q)) for c in n))
        uvs.append((int(round(Q * j / seg)), int(round(Q * i / rings))))
        return len(pos) - 1
    for i in range(1, rings):
        for j in range(seg + 1):
            grid[i, j] = vert(i, j)
    for j in range(seg):
        top, bot = vert(0, j), vert(rings, j)
        idx += [top, grid[1, j + 1], grid[1, j], bot, grid[rings - 1, j], grid[rings - 1, j + 1]]
    for i in range(1, rings - 1):
        for j in range(seg):
            a, b, c, d = grid[i, j], grid[i, j + 1], grid[i + 1, j + 1], grid[i + 1, j]
            idx += [a, b, c, a, c, d]
    if bump_um:  # push one equator vertex (and its seam twin) outward along +X
        k = grid[rings // 2, 0]
        pos[k] = pos[grid[rings // 2, seg]] = (pos[k][0] + bump_um, pos[k][1], pos[k][2])
    return g.Prim(name, "/" + name, g.IDENTITY_Q16, pos, idx, uvs, nrm, "paint")


def glb(*prims):
    return g.write_glb(g.Scene(list(prims)))


def ladder(lod0=(24, 12), lod1=(16, 8), lod2=(12, 6), prefix="ball", extra=(), bump1=0):
    prims = [sphere(prefix + "_LOD0", *lod0)]
    if lod1:
        prims.append(sphere(prefix + "_LOD1", *lod1, bump_um=bump1))
    if lod2:
        prims.append(sphere(prefix + "_LOD2", *lod2))
    return glb(*(prims + list(extra)))


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="bvqa_ag2_lod_"))
        self.addCleanup(shutil.rmtree, self.td, True)

    def write(self, name, data):
        p = self.td / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return str(p)

    def source(self, seg=24, rings=12, name="ball", cls="prop", receipt_edit=None, tag=""):
        data = glb(sphere(name, seg, rings))
        rc, rec = ig.run(data, cls)
        self.assertEqual(rc, 0, rec["reason"])
        if receipt_edit:
            receipt_edit(rec)
        return self.write("src%s.glb" % tag, data), self.write("inspect%s.json" % tag, ig.canon(rec))

    def verify(self, lods, src=None, insp=None, cls="prop"):
        argv = ["--verify-only", "--lods", self.write("ladder.glb", lods), "--class", cls]
        if src:
            argv += ["--source", src]
        if insp:
            argv += ["--inspected", insp]
        return L.run(argv)


class BudgetTest(unittest.TestCase):
    def test_budget_formula(self):
        self.assertEqual(L.budget(1000, 500), 500)
        self.assertEqual(L.budget(1001, 500), 501)  # (1001*500 + 500)//1000 rounds half up
        self.assertEqual(L.budget(200, 250), 120)   # floor
        self.assertEqual(L.budget(100, 250), 100)   # never above f0
        self.assertEqual(L.caps_for(528), [528, 264, 132])


class VerifyTest(Base):
    def test_valid_ladder_passes_with_bounded_hausdorff(self):
        src, insp = self.source()
        rc, rec = self.verify(ladder(), src, insp)
        self.assertEqual(rc, 0, rec["reason"])
        per = rec["per_prefix"]["ball"]
        self.assertEqual(per["tris"], [528, 224, 120])
        self.assertTrue(all(h["ratio_ppm"] <= h["bound_ppm"] for h in per["hausdorff"]))
        self.assertEqual(rec["prev_receipt_sha256"], rec["inspected_sha256"])
        self.assertEqual(rec["schema"], "bvqa.asset-lod-ladder/v1")

    def test_mutant_fake_lod_copy(self):
        src, insp = self.source()
        rc, rec = self.verify(ladder(lod1=(24, 12)), src, insp)  # LOD1 == LOD0 byte-equal geometry
        self.assertEqual(rc, 5)
        self.assertIn("ball:lod1_rule", rec["failed"])
        self.assertTrue(rec["per_prefix"]["ball"]["transitions"][0]["geometry_identical"])

    def test_mutant_hausdorff_bound_x10(self):
        src, insp = self.source()
        bumped = ladder(bump1=200000)  # 0.2 m spike on LOD1: ~ 0.11 x diag
        rc, rec = self.verify(bumped, src, insp)
        self.assertEqual(rc, 5)
        self.assertEqual(rec["failed"], ["ball:hausdorff_lod1"])
        ratio = rec["per_prefix"]["ball"]["hausdorff"][0]["ratio_ppm"]
        self.assertGreater(ratio, L.HAUSDORFF_PPM[1])
        # Discriminating: the mutant module (bound x10) would wrongly pass this ladder.
        with mock.patch.object(L, "HAUSDORFF_PPM", tuple(10 * b for b in L.HAUSDORFF_PPM)):
            rc_mut, _ = self.verify(bumped, src, insp)
        self.assertEqual(rc_mut, 0)

    def test_mutant_missing_lod2(self):
        src, insp = self.source()
        rc, rec = self.verify(ladder(lod2=None), src, insp)
        self.assertEqual(rc, 5)
        self.assertIn("ball:complete", rec["failed"])

    def test_mutant_lod1_over_cap(self):
        src, insp = self.source()
        rc, rec = self.verify(ladder(lod1=(20, 10)), src, insp)  # 360 tris > cap 264
        self.assertEqual(rc, 5)
        self.assertIn("ball:lod1_rule", rec["failed"])

    def test_mutant_lod0_decimated_not_source(self):
        src, insp = self.source()
        rc, rec = self.verify(ladder(lod0=(20, 10), lod1=(16, 8)), src, insp)
        self.assertEqual(rc, 5)
        self.assertIn("ball:lod0_is_source", rec["failed"])

    def test_mutant_helper_collision_lod_and_stray_node(self):
        src, insp = self.source()
        helper = sphere("ball_collision_LOD0", 8, 4)
        rc, rec = self.verify(ladder(extra=[helper]), src, insp)
        self.assertEqual(rc, 5)
        self.assertIn("no_helper_lods", rec["failed"])
        rc, rec = self.verify(ladder(extra=[sphere("ball", 8, 4)]), src, insp)
        self.assertEqual(rc, 5)
        self.assertIn("no_stray_visual_nodes", rec["failed"])
        # hull nodes belong to hull_gen: ignored here, recorded
        rc, rec = self.verify(ladder(extra=[sphere("ball-convcol", 8, 4)]), src, insp)
        self.assertEqual(rc, 0, rec["reason"])
        self.assertEqual(rec["per_prefix"]["_ignored_helpers"], ["ball-convcol"])

    def test_floor_preserved_small_source(self):
        src, insp = self.source(8, 6)  # 80 tris <= floor 120
        rc, rec = self.verify(ladder((8, 6), (8, 6), (8, 6)), src, insp)
        self.assertEqual(rc, 0, rec["reason"])
        self.assertEqual([t["flag"] for t in rec["per_prefix"]["ball"]["transitions"]], ["floor_preserved"] * 2)

    def test_inspected_receipt_binding(self):
        src, insp = self.source()
        _, other = self.source(16, 8, tag="_other")  # receipt of another input
        rc, rec = self.verify(ladder(), src, other)
        self.assertEqual((rc, rec["verdict"]), (4, "STALE"))
        _, failing = self.source(receipt_edit=lambda r: r.update(verdict="FAIL", rc=5), tag="_fail")
        rc, _ = self.verify(ladder(), src, failing)
        self.assertEqual(rc, 3)
        rc, rec = self.verify(ladder(), src, insp, cls="character")  # receipt bound to class prop
        self.assertEqual(rc, 4)
        rc, _ = L.run(["--verify-only", "--lods", self.write("x.glb", b"garbage"), "--class", "prop"])
        self.assertEqual(rc, 2)
        rc, _ = L.run(["--in", src, "--class", "prop"])  # build mode without --inspected/--out-dir
        self.assertEqual(rc, 2)

    def test_skinned_source_incomplete(self):
        p = sphere("ball", 24, 12)
        p.skinned = True
        with mock.patch.object(L.glb_core, "parse_glb", return_value=g.Scene([p])):
            rc, rec = self.verify(ladder(), self.write("s.glb", glb(sphere("ball", 24, 12))))
        self.assertEqual(rc, 3)


class BuildRunnerTest(Base):
    """Build mode with an injected runner: the worker report is never trusted, the GLB is re-parsed."""

    def build(self, ladder_bytes, report=None, rc=0, exc=None):
        src, insp = self.source()
        out = self.td / "out"

        def runner(argv, timeout):
            if exc:
                raise exc
            Path(argv[argv.index("--out") + 1]).write_bytes(ladder_bytes)
            plan = json.loads(Path(argv[argv.index("--plan") + 1]).read_text())
            self.assertEqual(plan["ball"]["caps"], [528, 264, 132])
            return rc, "BVQA_LOD " + json.dumps(report or {"prefixes": {}}), ""
        return L.run(["--in", src, "--out-dir", str(out), "--class", "prop", "--inspected", insp], runner=runner)

    def test_lying_worker_fake_lod_rc5(self):
        rc, rec = self.build(ladder(lod1=(24, 12)), {"prefixes": {"ball": {"tris": [528, 264, 132]}}})
        self.assertEqual(rc, 5)
        self.assertIn("ball:lod1_rule", rec["failed"])

    def test_good_build_and_timeout_and_worker_error(self):
        rc, rec = self.build(ladder())
        self.assertEqual(rc, 0, rec["reason"])
        self.assertEqual(rec["ladder_sha256"], g.sha256((self.td / "out" / "ladder.glb").read_bytes()))
        rc, _ = self.build(ladder(), exc=L.LodError(L.RC_BUDGET, "timeout"))
        self.assertEqual(rc, 6)
        rc, _ = self.build(ladder(), rc=1)
        self.assertEqual(rc, 2)

    def test_main_writes_receipt_deterministically(self):
        src, insp = self.source()
        lods = self.write("ladder.glb", ladder())
        outs = []
        for i in range(2):
            out = self.td / ("r%d.json" % i)
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(L.main(["--verify-only", "--lods", lods, "--source", src, "--inspected", insp,
                                         "--class", "prop", "--out", str(out)]), 0)
            self.assertEqual(buf.getvalue().encode(), out.read_bytes())
            outs.append(out.read_bytes())
        self.assertEqual(outs[0], outs[1])


@unittest.skipUnless(BLENDER, "BVQA_BLENDER=1 and blender on PATH required")
class BlenderTest(Base):
    def test_real_decimate_ladder_and_determinism(self):
        src, insp = self.source(32, 16)  # 960 tris -> caps 480 / 240
        shas = []
        for i in range(2):
            out = self.td / ("out%d" % i)
            rc, rec = L.run(["--in", src, "--out-dir", str(out), "--class", "prop", "--inspected", insp,
                             "--timeout", "300"])
            self.assertEqual(rc, 0, rec["reason"])
            per = rec["per_prefix"]["ball"]
            self.assertTrue(per["tris"][2] < per["tris"][1] < per["tris"][0] == 960)
            self.assertTrue(per["tris"][1] <= 480 and per["tris"][2] <= 240)
            shas.append(per["geom_sha256"])
        self.assertEqual(shas[0], shas[1])

    def test_real_ladder_mutated_to_fake_lod_is_killed(self):
        src, insp = self.source(32, 16)
        out = self.td / "out"
        rc, _ = L.run(["--in", src, "--out-dir", str(out), "--class", "prop", "--inspected", insp, "--timeout", "300"])
        self.assertEqual(rc, 0)
        scene = g.parse_glb((out / "ladder.glb").read_bytes())
        lod0 = next(p for p in scene.prims if p.name == "ball_LOD0")
        fake = [p for p in scene.prims if p.name != "ball_LOD1"]
        fake.append(g.Prim("ball_LOD1", "/ball_LOD1", lod0.world_q16, lod0.positions_um, lod0.indices,
                           lod0.uvs_q16, lod0.normals_q16, lod0.material))
        rc, rec = self.verify(g.write_glb(g.Scene(fake)), src, insp)
        self.assertEqual(rc, 5)
        self.assertIn("ball:lod1_rule", rec["failed"])


if __name__ == "__main__":
    unittest.main()
