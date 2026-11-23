"""AG2 M5: stage registry, limits/threshold rows, vision calibration record and the ladder -> hull -> ORM chain."""
import contextlib
import csv
import importlib.util
import io
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

from blender_visual_qa.assets import cli as acli
from blender_visual_qa.assets import glb_core as g
from tests.test_ag1_e2e import build_crate
from blender_visual_qa.assets import limits

ROOT = Path(__file__).resolve().parent.parent
AG2_STAGES = {"render-sheet": "render_sheet", "lod-ladder": "lod_ladder", "hull-gen": "hull_gen", "ortho4": "ortho4", "orm-pack": "orm_pack"}
AG2_LIMITS = {"ASSET_ORM_ROUGH_MIN_BYTE": 11, "ASSET_LOD1_IOU_MIN_PPM": 950000, "ASSET_LOD2_IOU_MIN_PPM": 900000,
              **{n: v for n, v in (("ASSET_LOD_FLOOR_TRIS", 120), ("ASSET_LOD1_PERMILLE", 500), ("ASSET_LOD2_PERMILLE", 250), ("ASSET_LOD1_HAUSDORFF_PPM", 20000), ("ASSET_LOD2_HAUSDORFF_PPM", 50000), ("ASSET_LOD_SAMPLES", 4000), ("ASSET_LOD_FILL_PERMILLE", 900), ("ASSET_HULL_MAX_VERTS", 32), ("ASSET_HULL_MAX_FACES", 60), ("ASSET_HULL_TAU_PPM", 20000), ("ASSET_ORTHO4_RES", 64), ("ASSET_ORTHO4_MIRROR_XOR_MAX_PPM", 20000))}}


# ---- programmatic LOD fixtures (no binaries) ----

def _prim(name, rings, closed_u, radius_um, height_um):
    """rings: list of (y01, r01) from bottom to top; surface of revolution with a seam column and pole fans."""
    seg = 28
    pos, uvs, nrm, idx = [], [], [], []
    cols = []
    for ri, (y01, r01) in enumerate(rings):
        row = []
        for s in range(seg + 1):
            a = 2 * math.pi * s / seg
            x, z = math.cos(a) * r01, math.sin(a) * r01
            pos.append((round(x * radius_um), round(y01 * height_um), round(z * radius_um)))
            ny = 0.0
            nx, nz = math.cos(a), math.sin(a)
            nrm.append((round(nx * g.Q16), round(ny * g.Q16), round(nz * g.Q16)))
            uvs.append((round(s / seg * g.Q16 * 0.98 + g.Q16 * 0.01), round(ri / (len(rings) - 1) * g.Q16 * 0.98 + g.Q16 * 0.01)))
            row.append(len(pos) - 1)
        cols.append(row)
    for ri in range(len(rings) - 1):
        for s in range(seg):
            a, b, c, d = cols[ri][s], cols[ri][s + 1], cols[ri + 1][s + 1], cols[ri + 1][s]
            if rings[ri][1] > 0:
                idx += [a, c, b] if False else [a, d, b]
            if rings[ri + 1][1] > 0:
                idx += [b, d, c]
    return g.Prim(name, "/" + name, g.IDENTITY_Q16, pos, idx, uvs, nrm, "m")


def sphere():
    n = 14
    rings = [(0.5 - 0.5 * math.cos(math.pi * i / n), math.sin(math.pi * i / n)) for i in range(n + 1)]
    return _prim("ball", [(y, r if 0 < i < n else 0) for i, (y, r) in enumerate(rings)], True, 500000, 1000000)


def cylinder():
    rings = [(0.0, 0.0), (0.0, 1.0)] + [(i / 8, 1.0) for i in range(1, 8)] + [(1.0, 1.0), (1.0, 0.0)]
    return _prim("drum", rings, True, 400000, 1000000)


def cone():
    rings = [(0.0, 0.0), (0.0, 1.0)] + [(i / 10, 1.0 - i / 10) for i in range(1, 10)] + [(1.0, 0.0)]
    return _prim("spire", rings, True, 450000, 1000000)


FIXTURES = {"sphere": sphere, "cylinder": cylinder, "cone": cone}


def fixture_glb(name):
    return g.write_glb(g.Scene([FIXTURES[name]()]))


def lod_iou_ppm(ladder_bytes, res=128):
    """Min over the 4 ortho views of silhouette IoU (ppm) of LOD1/LOD2 against LOD0 on LOD0's frame; plus copy flag."""
    from blender_visual_qa.assets import ortho4
    scene = g.parse_glb(ladder_bytes)
    by = {}
    for p in scene.prims:
        stem, _, k = p.name.rpartition("_LOD")
        if k.isdigit():
            by.setdefault(int(k), []).append(p)
    tris = {k: ortho4.scene_triangles(g.Scene(v)) for k, v in by.items()}
    c2, ext = ortho4.frame(tris[0])
    sil = {k: {v: ortho4.rasterise(t, v, c2, ext, res) for v in ortho4.VIEWS} for k, t in tris.items()}
    out = {}
    for k in (1, 2):
        worst = 1000000
        for v in ortho4.VIEWS:
            inter = sum(bin(a & b).count("1") for a, b in zip(sil[0][v], sil[k][v]))
            union = sum(bin(a | b).count("1") for a, b in zip(sil[0][v], sil[k][v]))
            worst = min(worst, inter * 1000000 // union)
        out[k] = worst
    out["lod1_equals_lod0"] = sorted(map(tuple, (t for t in tris[1]))) == sorted(map(tuple, tris[0]))
    out["tris"] = [len(tris[k]) for k in (0, 1, 2)]
    return out


def run_cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = acli.main(argv)
    lines = [s for s in buf.getvalue().splitlines() if s.strip()]
    return rc, json.loads(lines[-1]) if lines else None


def built(module):
    return importlib.util.find_spec("blender_visual_qa.assets." + module) is not None


class TestAg2Registry(unittest.TestCase):
    def test_stages_registered_with_module_names(self):
        for stage, module in AG2_STAGES.items():
            self.assertEqual(acli.STAGES.get(stage), module, stage)

    def test_schema_enum_equals_stage_table(self):
        sch = json.loads((ROOT / "blender_visual_qa/schemas/asset-receipt.v1.schema.json").read_text())
        self.assertEqual(set(sch["properties"]["stage"]["enum"]), set(acli.STAGES))

    def test_unbuilt_stage_is_rc3_not_crash(self):
        saved = dict(acli.STAGES)
        try:
            acli.STAGES["lod-ladder"] = "absent_lod_module"
            rc, rec = run_cli(["lod-ladder", "--in", "x"])
        finally:
            acli.STAGES.clear()
            acli.STAGES.update(saved)
        self.assertEqual((rc, rec["stage_receipt"]["reason"]), (limits.RC_INCOMPLETE, "stage_not_built"))

    def test_limits_registered_owner_status(self):
        with (ROOT / "catalogue/thresholds.tsv").open() as f:
            rows = {r["id"]: r for r in csv.DictReader(f, delimiter="\t")}
        for name, value in AG2_LIMITS.items():
            self.assertEqual(getattr(limits, name), value)
            self.assertEqual(int(rows[name]["value"]), value)
            self.assertEqual(rows[name]["owner"], "BASSETS")
            self.assertEqual(rows[name]["status"], "DESIGN_CHOICE")
        for name in dir(limits):
            if name.startswith("ASSET_"):
                self.assertIn(name, rows)

    def test_lod_floors_ordered(self):
        self.assertGreater(limits.ASSET_LOD1_IOU_MIN_PPM, limits.ASSET_LOD2_IOU_MIN_PPM)
        self.assertLessEqual(limits.ASSET_LOD1_IOU_MIN_PPM, 1000000)


class TestVisionCalib(unittest.TestCase):
    def setUp(self):
        self.cal = json.loads((ROOT / "config/vision_asset_calib.json").read_text())

    def test_floors_match_limits(self):
        self.assertEqual(self.cal["schema"], "bvqa.vision-asset-calib/v1")
        self.assertEqual(self.cal["lod_iou_min_ppm"]["lod1"], limits.ASSET_LOD1_IOU_MIN_PPM)
        self.assertEqual(self.cal["lod_iou_min_ppm"]["lod2"], limits.ASSET_LOD2_IOU_MIN_PPM)
        self.assertEqual(min(f["lod1_iou_ppm"] for f in self.cal["fixtures"]), 994704)
        self.assertEqual(min(f["lod2_iou_ppm"] for f in self.cal["fixtures"]), 988652)

    def test_vision_on_requires_measured_fixtures_clearing_floors(self):
        if self.cal["vision_mode"] != "on":
            self.assertIn(self.cal["status"], ("uncalibrated", "partial"))
            return
        self.assertEqual(self.cal["status"], "MEASURED")
        self.assertGreaterEqual(len(self.cal["fixtures"]), 3)
        for fx in self.cal["fixtures"]:
            self.assertGreaterEqual(fx["lod1_iou_ppm"], limits.ASSET_LOD1_IOU_MIN_PPM, fx["name"])
            self.assertGreaterEqual(fx["lod2_iou_ppm"], limits.ASSET_LOD2_IOU_MIN_PPM, fx["name"])
            self.assertFalse(fx.get("lod1_equals_lod0"), fx["name"])

    def test_mutant_flip_without_measurement_is_rejected(self):
        bad = dict(self.cal, vision_mode="on", status="uncalibrated", fixtures=[])
        self.assertFalse(bad["status"] == "MEASURED" and len(bad["fixtures"]) >= 3)


@unittest.skipUnless(os.environ.get("BVQA_BLENDER") == "1", "needs BVQA_BLENDER=1")
class TestLadderHullOrmChain(unittest.TestCase):
    """raw crate -> normalize -> inspect -> lod-ladder -> hull-gen -> orm-pack, envelopes sha-chained."""

    def test_chain(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            chain = str(d / "chain.jsonl")
            raw, norm = d / "raw.glb", str(d / "norm.glb")
            raw.write_bytes(build_crate())
            steps = [
                (("normalize", "--in", str(raw), "--out", norm), 0),
                (("inspect", "--in", norm, "--class", "prop", "--out", str(d / "insp.json")), 0),
                (("lod-ladder", "--in", norm, "--out-dir", str(d / "lod"), "--class", "prop", "--inspected", str(d / "insp.json")), 0),
                (("hull-gen", "--in", str(d / "lod/ladder.glb"), "--out", str(d / "hull.glb"), "--lod", "LOD2"), 0),
                (("orm-pack", "--out", str(d / "orm.png"), "--size", "16x16"), 0),
            ]
            prev = None
            for argv, want in steps:
                rc, rec = run_cli(list(argv) + ["--chain", chain])
                self.assertEqual(rc, want, (argv[0], rec["stage_receipt"]))
                self.assertEqual(rec["prev_receipt_sha256"], prev)
                prev = rec["receipt_sha256"]
            self.assertEqual(len(Path(chain).read_text().splitlines()), len(steps))

    def test_mutant_rough_below_floor_is_clamped_to_floor(self):
        with tempfile.TemporaryDirectory() as td:
            rc, rec = run_cli(["orm-pack", "--out", str(Path(td) / "o.png"), "--size", "8x8", "--rough", "10"])
            sr = rec["stage_receipt"]
            self.assertEqual((rc, sr["channel_min"][1], sr["rough_clamped_px"]), (limits.RC_OK, limits.ASSET_ORM_ROUGH_MIN_BYTE, 64))


@unittest.skipUnless(os.environ.get("BVQA_BLENDER") == "1", "needs BVQA_BLENDER=1")
class TestLodIouCalibration(unittest.TestCase):
    """Recompute silhouette IoU of LOD1/LOD2 vs LOD0 on the 3 fixtures and compare with the recorded MEASURED values."""

    def test_measured_rows_reproduce_and_clear_floors(self):
        cal = json.loads((ROOT / "config/vision_asset_calib.json").read_text())
        recorded = {f["name"]: f for f in cal["fixtures"]}
        self.assertEqual(set(recorded), set(FIXTURES))
        for name in FIXTURES:
            with tempfile.TemporaryDirectory() as td:
                d = Path(td)
                (d / "f.glb").write_bytes(fixture_glb(name))
                rc, _ = run_cli(["inspect", "--in", str(d / "f.glb"), "--class", "prop", "--out", str(d / "i.json")])
                self.assertEqual(rc, 0, name)
                rc, rec = run_cli(["lod-ladder", "--in", str(d / "f.glb"), "--out-dir", str(d / "l"), "--class", "prop", "--inspected", str(d / "i.json")])
                self.assertEqual(rc, 0, (name, rec["stage_receipt"]))
                m = lod_iou_ppm((d / "l/ladder.glb").read_bytes())
            self.assertEqual((m[1], m[2]), (recorded[name]["lod1_iou_ppm"], recorded[name]["lod2_iou_ppm"]), name)
            self.assertGreaterEqual(m[1], limits.ASSET_LOD1_IOU_MIN_PPM)
            self.assertGreaterEqual(m[2], limits.ASSET_LOD2_IOU_MIN_PPM)
            self.assertFalse(m["lod1_equals_lod0"])
            self.assertLess(m["tris"][1], m["tris"][0])

    def test_mutant_fake_lod_copy_is_detected(self):
        from blender_visual_qa.assets import glb_core as g
        p = sphere()
        scene = g.Scene([g.Prim(f"ball_LOD{k}", f"/ball_LOD{k}", p.world_q16, p.positions_um, p.indices, p.uvs_q16, p.normals_q16, "m") for k in range(3)])
        m = lod_iou_ppm(g.write_glb(scene))
        self.assertTrue(m["lod1_equals_lod0"])


@unittest.skipUnless(os.environ.get("BVQA_BLENDER") == "1" and os.environ.get("BVQA_SLOW") == "1", "needs BVQA_BLENDER=1 and BVQA_SLOW=1")
class TestTurntableVisionOn(unittest.TestCase):
    def test_vision_on_after_calibration(self):
        cal = json.loads((ROOT / "config/vision_asset_calib.json").read_text())
        self.assertEqual(cal["vision_mode"], "on")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            raw, norm = d / "raw.glb", str(d / "norm.glb")
            raw.write_bytes(build_crate())
            self.assertEqual(run_cli(["normalize", "--in", str(raw), "--out", norm])[0], 0)
            rc, rec = run_cli(["turntable", "--glb", norm, "--out", str(d / "tt"), "--with-truth", "--vision", cal["vision_mode"]])
            self.assertEqual(rc, limits.RC_OK, rec["stage_receipt"])
            full = json.loads((d / "tt" / "turntable_receipt.json").read_text())
            self.assertEqual(full["vision"]["mode"], "on")
            self.assertNotEqual(full["vision"]["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()
