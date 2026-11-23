"""AG3 INT: stage registry, limits/threshold rows, schema enum and the noisy fixture -> heal -> mesh-gate chain."""
import csv
import importlib.util
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from blender_visual_qa.assets import cli as acli
from blender_visual_qa.assets import glb_core as g
from blender_visual_qa.assets import limits
from tests.test_ag1_e2e import build_crate
from tests.test_ag2_e2e import run_cli

ROOT = Path(__file__).resolve().parent.parent
AG3_STAGES = {"heal": "heal", "self-isect": "self_isect", "bake-pbr": "bake_pbr", "uv-gate": "uv_gate",
              "pbr-validate": "pbr_validate", "delight": "delight"}
AG3_LIMITS = {"ASSET_HEAL_MAX_PASSES": 4, "ASSET_HEAL_CULL_MAX_PERMILLE": 300, "ASSET_HEAL_WELD_UM": 10,
              "ASSET_HEAL_IOU_MIN_PPM": 990000, "ASSET_HEAL_MAX_HOLE_EDGES": 64,
              "ASSET_UV_FLIP_MAX_PPM": 0, "ASSET_UV_STRETCH_MAX_PERMILLE": 4000, "ASSET_UV_STRETCH_TRIS_MAX_PPM": 50000,
              "ASSET_PBR_ALBEDO_MIN_BYTE": 30, "ASSET_PBR_ALBEDO_MAX_BYTE": 240, "ASSET_PBR_ALBEDO_OUT_MAX_PPM": 10000,
              "ASSET_PBR_METAL_MID_LO_PERMILLE": 100, "ASSET_PBR_METAL_MID_HI_PERMILLE": 900, "ASSET_PBR_F0_MIN_BYTE": 170,
              "ASSET_DELIGHT_FLOOR_Q16": 4096, "ASSET_DELIGHT_BAKED_FRAC_Q16": 6554}


def noisy_crate():
    """Unit-cube crate (normalized pose) with a hole (one triangle removed), a duplicated triangle and a sliver fan."""
    scene = g.parse_glb(build_crate())
    prim = scene.prims[0]
    pos, uvs, nrm, idx = list(prim.positions_um), list(prim.uvs_q16), list(prim.normals_q16), list(prim.indices)
    hole = idx[0:3]
    idx = idx[3:]                                # hole: one front triangle gone
    idx += idx[3:6]                              # duplicate face
    a, b = pos[idx[0]], pos[idx[1]]
    for k in range(1, 4):                        # sliver fan: collinear (zero-area) triangles along edge a-b
        pos.append(tuple((a[i] * (4 - k) + b[i] * k) // 4 for i in range(3)))
        uvs.append(uvs[idx[0]])
        nrm.append(nrm[idx[0]])
        idx += [idx[0], idx[1], len(pos) - 1]
    noisy = replace(prim, positions_um=pos, indices=idx, uvs_q16=uvs, normals_q16=nrm)
    return g.write_glb(replace(scene, prims=[noisy])), hole


def built(module):
    return importlib.util.find_spec("blender_visual_qa.assets." + module) is not None


class TestAg3Registry(unittest.TestCase):
    def test_stages_registered_with_module_names(self):
        for stage, module in AG3_STAGES.items():
            self.assertEqual(acli.STAGES.get(stage), module, stage)

    def test_schema_enum_equals_stage_table(self):
        sch = json.loads((ROOT / "blender_visual_qa/schemas/asset-receipt.v1.schema.json").read_text())
        self.assertEqual(set(sch["properties"]["stage"]["enum"]), set(acli.STAGES))

    def test_unbuilt_stage_is_rc3_not_crash(self):
        saved = dict(acli.STAGES)
        try:
            acli.STAGES["heal"] = "absent_heal_module"
            rc, rec = run_cli(["heal", "--in", "x"])
        finally:
            acli.STAGES.clear()
            acli.STAGES.update(saved)
        self.assertEqual((rc, rec["stage_receipt"]["reason"]), (limits.RC_INCOMPLETE, "stage_not_built"))

    def test_limits_registered_owner_status(self):
        with (ROOT / "catalogue/thresholds.tsv").open() as f:
            rows = {r["id"]: r for r in csv.DictReader(f, delimiter="\t")}
        for name, value in AG3_LIMITS.items():
            self.assertEqual(getattr(limits, name), value, name)
            self.assertEqual(int(rows[name]["value"]), value, name)
            self.assertEqual((rows[name]["owner"], rows[name]["status"]), ("BASSETS", "DESIGN_CHOICE"), name)
        for name in dir(limits):
            if name.startswith("ASSET_"):
                self.assertIn(name, rows, name)

    def test_every_built_stage_has_main(self):
        for stage, module in AG3_STAGES.items():
            if built(module):
                mod = importlib.import_module("blender_visual_qa.assets." + module)
                self.assertTrue(callable(getattr(mod, "main", None)), stage)


class TestNoisyFixtureHeal(unittest.TestCase):
    """noisy fixture (hole, duplicate face, sliver fan): mesh-gate rc 5 -> heal -> mesh-gate rc 0, sha-chained."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.chain = str(self.d / "chain.jsonl")
        self.noisy = self.d / "noisy.glb"
        self.noisy.write_bytes(noisy_crate()[0])

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, *argv):
        return run_cli(list(argv) + ["--chain", self.chain])

    def test_noisy_fixture_fails_mesh_gate(self):
        rc, rec = self.step("mesh-gate", "--mesh", str(self.noisy))
        self.assertEqual(rc, limits.RC_FAIL, rec["stage_receipt"])

    @unittest.skipUnless(built("heal"), "heal stage not built")
    def test_noisy_heal_then_mesh_gate_passes(self):
        healed = str(self.d / "healed.glb")
        rc0, r0 = self.step("mesh-gate", "--mesh", str(self.noisy))
        rc1, r1 = self.step("heal", "--in", str(self.noisy), "--out", healed)
        self.assertEqual((rc0, rc1), (limits.RC_FAIL, limits.RC_OK), r1["stage_receipt"])
        rc2, r2 = self.step("mesh-gate", "--mesh", healed)
        self.assertEqual(rc2, limits.RC_OK, r2["stage_receipt"])
        self.assertEqual(r1["prev_receipt_sha256"], r0["receipt_sha256"])
        self.assertEqual(r2["prev_receipt_sha256"], r1["receipt_sha256"])
        self.assertNotEqual(r1["input_sha256"], r2["input_sha256"])

    @unittest.skipUnless(built("heal"), "heal stage not built")
    def test_mutant_unhealed_copy_still_fails(self):
        # mutant: a "heal" that returns the input unchanged must not satisfy the gate
        copy = self.d / "copy.glb"
        copy.write_bytes(self.noisy.read_bytes())
        rc, _ = self.step("mesh-gate", "--mesh", str(copy))
        self.assertEqual(rc, limits.RC_FAIL)

    @unittest.skipUnless(built("self_isect"), "self_isect stage not built")
    def test_self_isect_stage_runs_on_clean_crate(self):
        clean = self.d / "clean.glb"
        clean.write_bytes(build_crate())
        rc, rec = self.step("self-isect", "--mesh", str(clean))
        self.assertIn(rc, (limits.RC_OK, limits.RC_INCOMPLETE, limits.RC_FAIL), rec)
        self.assertEqual(rec["stage"], "self-isect")


if __name__ == "__main__":
    unittest.main()
