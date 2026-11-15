"""AG3 N1: bounded healing driver (VG-0238). Fixtures are built in-test (no binaries); each mutant pairs with a passing base.

Noisy fixture = 2x2-subdivided unit cube with per-face unshared, jittered vertices (duplicate verts), one quad
removed (hole), a whole face replaced by a ring hole fill case and a near-collinear sliver fan along an edge.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from blender_visual_qa.assets import glb_core, heal, mesh_gate

S = 1000000
HALF = S // 2
CELL = 3000  # q16 UV cell for the source atlas (6 faces x 3 cells < 1.0)
BLENDER_ON = os.environ.get("BVQA_BLENDER") == "1" and shutil.which("blender") is not None

# face: (origin, u axis, v axis) with u x v pointing outward
FACES = [((0, 0, 0), (0, 1, 0), (1, 0, 0)), ((0, 0, S), (1, 0, 0), (0, 1, 0)),
         ((0, 0, 0), (1, 0, 0), (0, 0, 1)), ((0, S, 0), (0, 0, 1), (1, 0, 0)),
         ((0, 0, 0), (0, 0, 1), (0, 1, 0)), ((S, 0, 0), (0, 1, 0), (0, 0, 1))]


def _pt(o, u, v, i, j, jit):
    return tuple(o[k] + u[k] * i * HALF + v[k] * j * HALF + jit for k in range(3))


def noisy_tris(hole=True, slivers=True, jitter=True, invert=False):
    """List of (positions3, uvs3) triangles."""
    out = []
    for f, (o, u, v) in enumerate(FACES):
        jit = (f % 4) * 2 if jitter else 0  # <= 6 um apart across face copies (weld tol 10 um)
        for i in range(2):
            for j in range(2):
                if hole and f == 1 and (i, j) == (1, 1):
                    continue
                q = [_pt(o, u, v, i + di, j + dj, jit) for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1))]
                base = (f * 3 * CELL + i * CELL, j * CELL)
                uq = [(base[0] + di * CELL, base[1] + dj * CELL) for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1))]
                for a, b, c in ((0, 1, 2), (0, 2, 3)):
                    t = (q[a], q[b], q[c]) if not invert else (q[a], q[c], q[b])
                    uv = (uq[a], uq[b], uq[c]) if not invert else (uq[a], uq[c], uq[b])
                    out.append((t, uv))
    if slivers:
        # fan from corner (0,0,0) along the x edge, 3 um off the line: altitude << weld tolerance
        pts = [(0, 0, 0)] + [(k * S // 4, 3, -3) for k in (1, 2, 3)] + [(S, 0, 0)]
        for k in range(1, 4):
            out.append(((pts[0], pts[k], pts[k + 1]), ((200 * CELL, 0), (201 * CELL, 0), (200 * CELL, CELL))))
    return out


def scene_of(tris, name="crate"):
    P, I, U = [], [], []
    for t, uv in tris:
        for p, c in zip(t, uv):
            I.append(len(P))
            P.append(p)
            U.append(c)
    return glb_core.Scene(prims=[glb_core.Prim(name=name, node_path="/" + name, world_q16=glb_core.IDENTITY_Q16,
                                               positions_um=P, indices=I, uvs_q16=U, material="m")])


class HealCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bvqa_ag3_heal_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def glb(self, scene, name="in.glb"):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(glb_core.write_glb(scene))
        return p

    def heal(self, src, *extra):
        out = os.path.join(self.tmp, "healed.glb")
        rc, rec = heal.run(["--in", src, "--out", out] + list(extra))
        return rc, rec, out

    def gate(self, path):
        return mesh_gate.run(["--mesh", path])

    # ------------------------------------------------------------------ exit criterion of the card
    def test_noisy_fixture_rc5_then_heal_then_mesh_gate_rc0(self):
        src = self.glb(scene_of(noisy_tris()))
        grc, grec = self.gate(src)
        self.assertEqual(grc, 5, grec)
        rc, rec, out = self.heal(src)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["status"], "PASS")
        st = rec["prims"][0]
        self.assertGreater(st["merged_vertices"], 0)
        self.assertGreaterEqual(st["culled_faces"], 3)
        self.assertEqual(st["patches"], 1)
        self.assertLessEqual(st["passes"], heal.MAX_PASSES)
        self.assertGreaterEqual(rec["silhouette_iou_ppm"], heal.IOU_MIN_PPM)
        grc2, grec2 = self.gate(out)
        self.assertEqual(grc2, 0, grec2)
        self.assertEqual(grec2["prims"][0]["genus"], 0)
        with open(out, "rb") as fh:
            self.assertEqual(rec["artefact_sha256"], glb_core.sha256(fh.read()))
        with open(src, "rb") as fh:
            self.assertEqual(rec["input_sha256"], glb_core.sha256(fh.read()))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "heal_receipt.json")))

    def test_original_uvs_kept_patch_uvs_synthesized_right_of_atlas(self):
        src = self.glb(scene_of(noisy_tris(slivers=False)))
        rc, rec, out = self.heal(src)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["prims"][0]["uv_synth_faces"], 2)  # the 2 patch triangles of the removed quad
        s = glb_core.read_glb(out)
        us = [uv[0] for uv in s.prims[0].uvs_q16]
        self.assertEqual(sum(1 for u in us if u >= glb_core.Q16), 6)

    def test_clean_cube_is_a_fixed_point(self):
        src = self.glb(scene_of(noisy_tris(hole=False, slivers=False, jitter=False)))
        self.assertEqual(self.gate(src)[0], 0)
        rc, rec, out = self.heal(src)
        self.assertEqual(rc, 0, rec)
        st = rec["prims"][0]
        self.assertEqual((st["culled_faces"], st["patches"], st["flipped_faces"], st["uv_synth_faces"]), (0, 0, 0, 0))
        self.assertEqual(st["converged"], 1)
        self.assertEqual(rec["silhouette_iou_ppm"], heal.PPM)

    def test_receipt_chain_and_determinism(self):
        src = self.glb(scene_of(noisy_tris()))
        prev = os.path.join(self.tmp, "prev.json")
        with open(prev, "w") as fh:
            fh.write("{}\n")
        rc1, r1, _ = self.heal(src, "--prev-receipt", prev)
        rc2, r2, _ = self.heal(src, "--prev-receipt", prev)
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(heal.canon(r1), heal.canon(r2))
        self.assertEqual(r1["schema"], "bvqa.asset-heal/v1")
        self.assertEqual(len(r1["prev_sha256"]), 64)

    # ------------------------------------------------------------------ usage / bounds
    def test_pass_budget_above_p_is_usage(self):
        src = self.glb(scene_of(noisy_tris()))
        rc, rec, out = self.heal(src, "--max-passes", str(heal.MAX_PASSES + 1))
        self.assertEqual(rc, 2, rec)
        self.assertFalse(os.path.exists(out))

    def test_driver_never_exceeds_pass_budget(self):
        calls = []
        real = heal.weld

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        with mock.patch.object(heal, "fill", lambda pos, tris, m: (list(tris) + [(0, 1, 2)] * 0, 0, 0, 0, 0)), \
                mock.patch.object(heal, "weld", counting):
            # a driver that never converges (orient keeps flipping) must still stop at P
            with mock.patch.object(heal, "orient", lambda pos, tris, r=False: ([(t[0], t[2], t[1]) for t in tris], len(tris))):
                heal.heal_mesh([(0, 0, 0), (S, 0, 0), (0, S, 0)], [(0, 1, 2)])
        self.assertEqual(len(calls), heal.MAX_PASSES)

    def test_missing_and_empty_input(self):
        rc, rec, _ = self.heal(os.path.join(self.tmp, "nope.glb"))
        self.assertEqual(rc, 2)
        src = self.glb(glb_core.Scene(prims=[]))
        rc, rec, _ = self.heal(src)
        self.assertEqual(rc, 3, rec)

    # ------------------------------------------------------------------ named mutants (each killed: rc != 0)
    def test_mutant_no_weld_killed(self):
        src = self.glb(scene_of(noisy_tris()))
        ident = lambda pos, tris, tol: (list(tris), 0, list(range(len(pos))))
        with mock.patch.object(heal, "weld", ident):
            rc, rec, out = self.heal(src)
        self.assertEqual(rc, 5, rec)
        self.assertEqual(rec["status"], "REGENERATE")
        self.assertFalse(os.path.exists(out))

    def test_mutant_no_hole_fill_killed(self):
        src = self.glb(scene_of(noisy_tris(slivers=False)))
        with mock.patch.object(heal, "fill", lambda pos, tris, m: (list(tris), 0, 0, 0, 0)):
            rc, rec, _ = self.heal(src)
        self.assertEqual(rc, 5, rec)
        self.assertIn("mesh_gate:open_boundary", rec["failed"])

    def test_mutant_patch_wound_forward_killed(self):
        src = self.glb(scene_of(noisy_tris(slivers=False)))
        real = heal.fill_loop

        def forward(pos, loop):
            fan, c = real(pos, loop)
            return [(t[0], t[2], t[1]) for t in fan], c
        with mock.patch.object(heal, "fill_loop", forward), mock.patch.object(heal, "orient", lambda p, t, r=False: (list(t), 0)):
            rc, rec, _ = self.heal(src)
        self.assertEqual(rc, 5, rec)
        self.assertIn("mesh_gate:twin_winding", rec["failed"])

    def test_inverted_shell_flipped_and_mutant_no_orient_killed(self):
        src = self.glb(scene_of(noisy_tris(hole=False, slivers=False, jitter=False, invert=True)))
        self.assertIn("inward_winding", self.gate(src)[1]["failed"])
        rc, rec, out = self.heal(src)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["prims"][0]["flipped_faces"], 48)
        with mock.patch.object(heal, "orient", lambda p, t, r=False: (list(t), 0)):
            rc, rec, _ = self.heal(src)
        self.assertEqual(rc, 5, rec)
        self.assertIn("mesh_gate:inward_winding", rec["failed"])

    def test_cull_budget_regenerate_and_mutant_budget_ignored(self):
        tris = noisy_tris(hole=False, slivers=False, jitter=False)
        junk = [((( k * 1000, 0, 0), (k * 1000 + 500, 0, 0), (k * 1000 + 250, 2, 0)), ((0, 0), (CELL, 0), (0, CELL)))
                for k in range(30)]  # 30 slivers on 48 faces: 384 permille culled > 300
        src = self.glb(scene_of(tris + junk))
        rc, rec, out = self.heal(src)
        self.assertEqual(rc, 5, rec)
        self.assertEqual(rec["status"], "REGENERATE")
        self.assertIn("cull_budget", rec["failed"])
        self.assertFalse(os.path.exists(out))
        # mutant: budget check disabled -> the same input would be emitted (rc 0), so the budget is what kills it
        rc, rec, _ = self.heal(src, "--cull-max-permille", "1000")
        self.assertEqual(rc, 0, rec)

    def test_mutant_shape_drift_killed_by_silhouette_iou(self):
        src = self.glb(scene_of(noisy_tris()))
        real = heal.build_prim

        def shifted(prim, pos, tris, lookup):
            p, n = real(prim, pos, tris, lookup)
            p.positions_um = [(x + S // 5, y, z) for x, y, z in p.positions_um]
            return p, n
        with mock.patch.object(heal, "build_prim", shifted):
            rc, rec, _ = self.heal(src)
        self.assertEqual(rc, 5, rec)
        self.assertIn("silhouette_iou", rec["failed"])
        self.assertLess(rec["silhouette_iou_ppm"], heal.IOU_MIN_PPM)

    def test_unit_operators(self):
        pos = [(0, 0, 0), (S, 0, 0), (0, S, 0), (5, 5, 0)]
        tris, merged, rep = heal.weld(pos, [(3, 1, 2)], 10)
        self.assertEqual((tris, merged), ([(0, 1, 2)], 1))
        kept, culled = heal.cull(pos, [(0, 1, 2), (2, 1, 0), (0, 1, 3)], 10)
        self.assertEqual((kept, culled), ([(0, 1, 2)], 2))
        loops, bad = heal.boundary_loops([(0, 1, 2)])
        self.assertEqual((loops, bad), ([[0, 1, 2]], 0))
        fan, c = heal.fill_loop(list(pos), [0, 1, 2])
        self.assertEqual((fan, c), ([(0, 2, 1)], None))


@unittest.skipUnless(BLENDER_ON, "BVQA_BLENDER=1 and blender on PATH required")
class HealBlenderCase(HealCase):
    def heal(self, src, *extra):
        return super().heal(src, "--engine", "blender", "--timeout", "240", *extra)

    def _skip(self):
        self.skipTest("host-operator mutant; not applicable to the bmesh engine")

    test_mutant_no_weld_killed = test_mutant_no_hole_fill_killed = _skip
    test_mutant_patch_wound_forward_killed = test_driver_never_exceeds_pass_budget = _skip
    test_inverted_shell_flipped_and_mutant_no_orient_killed = test_original_uvs_kept_patch_uvs_synthesized_right_of_atlas = _skip
    test_clean_cube_is_a_fixed_point = test_receipt_chain_and_determinism = test_unit_operators = _skip
    test_cull_budget_regenerate_and_mutant_budget_ignored = _skip

    def test_noisy_fixture_rc5_then_heal_then_mesh_gate_rc0(self):
        src = self.glb(scene_of(noisy_tris()))
        self.assertEqual(self.gate(src)[0], 5)
        rc, rec, out = self.heal(src)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["engine"], "blender")
        self.assertEqual(self.gate(out)[0], 0)
        self.assertGreaterEqual(rec["silhouette_iou_ppm"], heal.IOU_MIN_PPM)

    def test_worker_bad_args_rc2(self):
        r = subprocess.run(["blender", "-b", "--factory-startup", "-noaudio", "--python", str(heal.WORKER), "--",
                            "--mesh", os.path.join(self.tmp, "absent.json"), "--weld-um", "10",
                            "--max-hole-edges", "64", "--max-passes", "4"],
                           capture_output=True, text=True, timeout=240)
        self.assertIn("BVQA_HEAL_ERROR", r.stdout)


if __name__ == "__main__":
    unittest.main()
