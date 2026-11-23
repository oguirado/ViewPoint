"""AG2 M1: render_sheet host layer + sheet worker. Fake-runner tests always run; the Blender test needs BVQA_BLENDER=1.

BVQA_BLENDER=1 taskset -c 5-9 python3 -m unittest tests.test_ag2_sheet -v
"""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blender_visual_qa.assets import render_sheet as rs  # noqa: E402
from blender_visual_qa.drive import png  # noqa: E402

SCRATCH = ROOT / "build" / "room_M1"
RES = 32


def make_glb(top=2.0, name="crate"):
    """Asymmetric L-shaped solid (two boxes); `top` moves the upper box so LOD fixtures differ in bytes and silhouette."""
    def box(x0, y0, z0, x1, y1, z1, base):
        v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
        quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        idx = []
        for a, b, c, d in quads:
            idx += [base + a, base + b, base + c, base + a, base + c, base + d]
        return v, idx
    v1, i1 = box(-1.0, 0.0, -0.5, 1.0, 1.0, 0.5, 0)
    v2, i2 = box(0.2, 1.0, -0.5, 1.0, top, 0.2, 8)
    verts, idx = v1 + v2, i1 + i2
    pos = b"".join(struct.pack("<3f", *p) for p in verts)
    ind = b"".join(struct.pack("<H", i) for i in idx)
    blob = pos + ind
    lo = [min(p[k] for p in verts) for k in range(3)]
    hi = [max(p[k] for p in verts) for k in range(3)]
    js = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": name}],
          "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
          "buffers": [{"byteLength": len(blob)}],
          "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
                          {"buffer": 0, "byteOffset": len(pos), "byteLength": len(ind)}],
          "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(verts), "type": "VEC3", "min": lo, "max": hi},
                        {"bufferView": 1, "componentType": 5123, "count": len(idx), "type": "SCALAR"}]}
    jb = json.dumps(js, separators=(",", ":")).encode()
    jb += b" " * (-len(jb) % 4)
    blob += b"\0" * (-len(blob) % 4)
    total = 12 + 8 + len(jb) + 8 + len(blob)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(jb), 0x4E4F534A) + jb +
            struct.pack("<II", len(blob), 0x004E4942) + blob)


def arg(argv, name):
    return argv[argv.index(name) + 1]


class FakeBlender:
    """Stands in for the sheet worker: one RGB PNG per (view, pass); content = f(glb, view, pass, scale), mask = centred square."""

    def __init__(self, drop=None, dup_view=None, ignore_scale=False, flaky=False, half=None, same_pixels=False, empty_mask=False):
        self.drop, self.dup_view, self.ignore_scale, self.flaky = drop, dup_view, ignore_scale, flaky
        self.half, self.same_pixels, self.empty_mask, self.calls = half or {}, same_pixels, empty_mask, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        out, res = Path(arg(argv, "--out")), int(arg(argv, "--res"))
        glb = Path(arg(argv, "--glb")).read_bytes()
        key = 0 if self.same_pixels else int(hashlib.sha256(glb).hexdigest()[:4], 16)
        views = [v.split(":")[0] for v in arg(argv, "--views").split(",")]
        passes = arg(argv, "--passes").split(",")
        scale = "--scale-x-permille" in argv and not self.ignore_scale
        out.mkdir(parents=True, exist_ok=True)
        files = []
        for vi, v in enumerate(views):
            vid = rs.VIEWS.index(next(x for x in rs.VIEWS if x[0] == v))
            if v == self.dup_view:
                vid = 0
            for p in passes:
                name = "%s_%s.png" % (v, p)
                if name == self.drop:
                    continue
                if p == "mask":
                    h = self.half.get(hashlib.sha256(glb).hexdigest(), res // 4)
                    lo, hi = res // 2 - h, res // 2 + h
                    on = 0 if self.empty_mask else 255
                    rgb = bytes(on if lo <= x < hi and lo <= y < hi else 0 for y in range(res) for x in range(res) for _ in range(3))
                else:
                    seed = key + vid * 31 + rs.PASSES.index(p) * 7 + (1000 if scale else 0)
                    if self.flaky and len(self.calls) > 1:
                        seed += 5
                    rgb = bytes((seed + i) % 256 for i in range(res * res * 3))
                (out / name).write_bytes(png.encode(res, res, rgb))
                files.append(name)
        return 0, "BVQA_SHEET " + json.dumps({"files": files, "blender": "fake", "radius_um": 1, "tris": 24}) + "\n", ""


class SheetFakeTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=SCRATCH))
        self.glb = self.tmp / "crate.glb"
        self.glb.write_bytes(make_glb())
        self.lod1 = self.tmp / "lod1.glb"
        self.lod1.write_bytes(make_glb(top=1.9))
        patcher = mock.patch.object(rs, "SHEET_RES", RES)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def go(self, runner, **kw):
        return rs.run(self.glb, self.tmp / "out", runner=runner, **kw)

    def test_full_sheet_pass_and_receipt(self):
        rc, rec = self.go(FakeBlender())
        self.assertEqual((rc, rec["verdict"], rec["failed"]), (0, "PASS", []))
        self.assertEqual(len(rec["images"]), rs.SHEET_VIEWS * len(rs.PASSES))
        self.assertEqual(rec["schema"], "bvqa.asset-sheet/v1")
        self.assertEqual(rec["input_sha256"], hashlib.sha256(self.glb.read_bytes()).hexdigest())
        on_disk = json.loads((self.tmp / "out" / "render_receipt.json").read_text())
        self.assertEqual(on_disk, rec)
        self.assertEqual(sorted(rec["sheets"]), sorted(rs.PASSES))

    def test_sheet_is_lossless_copy(self):
        # STORAGE LAW: frames and sheets live in the ramws workspace (ws.path("sheet/")), not in `out`
        mod, ws = rs.open_workspace(self.tmp / "out")
        self.addCleanup(ws.close)
        self.go(FakeBlender(), ws=ws)
        work = ws.path("sheet/")
        self.assertFalse((self.tmp / "out" / "sheet_clay.png").exists())
        w, h, rgb = png.decode((work / "sheet_clay.png").read_bytes())
        self.assertEqual((w, h), (RES * rs.SHEET_COLS, RES * rs.SHEET_VIEWS // rs.SHEET_COLS))
        cw, ch, cell = png.decode((work / "lod0" / "P5_clay.png").read_bytes())  # index 5 -> col 1, row 1
        for y in range(ch):
            off = ((RES + y) * w + RES) * 3
            self.assertEqual(rgb[off:off + cw * 3], cell[y * cw * 3:(y + 1) * cw * 3])

    def test_prev_receipt_chained(self):
        prev = self.tmp / "prev.json"
        prev.write_text("{}")
        rc, rec = self.go(FakeBlender(), prev=prev)
        self.assertEqual(rc, 0)
        self.assertEqual(rec["prev_receipt_sha256"], hashlib.sha256(b"{}").hexdigest())

    def test_reduced_views_is_preview(self):
        rc, rec = self.go(FakeBlender(), views=["P0", "P1", "O0"])
        self.assertEqual((rc, rec["verdict"], rec["failed"]), (3, "PREVIEW", []))

    def test_low_spp_is_preview(self):
        rc, rec = self.go(FakeBlender(), spp=2)
        self.assertEqual((rc, rec["verdict"]), (3, "PREVIEW"))

    def test_usage_errors(self):
        with self.assertRaises(rs.TurntableError) as cm:
            rs.run(self.tmp / "nope.glb", self.tmp / "out", runner=FakeBlender())
        self.assertEqual(cm.exception.rc, 2)
        bad = self.tmp / "bad.glb"
        bad.write_bytes(b"garbage")
        with self.assertRaises(rs.TurntableError) as cm:
            rs.run(bad, self.tmp / "out", runner=FakeBlender())
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(rs.main(["--glb", str(self.tmp / "nope.glb"), "--out", str(self.tmp / "o")]), 2)

    def test_lod_pass_and_iou(self):
        rc, rec = self.go(FakeBlender(), lods=[self.lod1])
        self.assertEqual((rc, rec["failed"]), (0, []))
        self.assertEqual(rec["lod_iou_ppm"]["lod1"], [rs.PPM] * len(rs.LOD_VIEWS))
        self.assertEqual(len([r for r in rec["images"] if r["lod"] == 1]), len(rs.LOD_VIEWS) * len(rs.LOD_PASSES))

    # ---- mutants: each must be killed (rc 5, named failed check)
    def assertKilled(self, res, name):
        rc, rec = res
        self.assertEqual(rc, 5, rec["failed"])
        self.assertIn(name, rec["failed"])

    def test_mutant_view_count_15(self):
        with mock.patch.object(rs, "VIEWS", rs.VIEWS[:15]):
            self.assertKilled(self.go(FakeBlender()), "sheet_views")

    def test_mutant_worker_drops_image(self):
        self.assertKilled(self.go(FakeBlender(drop="H1_normal_ws.png")), "artefacts_complete")

    def test_mutant_duplicate_view(self):
        self.assertKilled(self.go(FakeBlender(dup_view="P3")), "beauty_views_distinct")

    def test_mutant_scale_x_ignored(self):
        self.assertKilled(self.go(FakeBlender(ignore_scale=True)), "scale_x_detected")

    def test_mutant_nondeterministic_rerender(self):
        self.assertKilled(self.go(FakeBlender(flaky=True)), "rerender_identical")

    def test_mutant_subject_invisible(self):
        self.assertKilled(self.go(FakeBlender(empty_mask=True)), "mask_coverage")

    def test_mutant_fake_lod_copy(self):
        copy = self.tmp / "copy.glb"
        copy.write_bytes(self.glb.read_bytes())
        self.assertKilled(self.go(FakeBlender(), lods=[copy]), "lod1_not_copy")
        self.assertKilled(self.go(FakeBlender(same_pixels=True), lods=[self.lod1]), "lod1_not_copy")

    def test_mutant_lod_silhouette_shrunk(self):
        half = {hashlib.sha256(self.lod1.read_bytes()).hexdigest(): RES // 4 - 2}
        self.assertKilled(self.go(FakeBlender(half=half), lods=[self.lod1]), "lod1_iou")


class SheetWorkerCameraTests(unittest.TestCase):
    def test_direction_matches_orbit_rule(self):
        spec = importlib.util.spec_from_file_location("sheet_worker", ROOT / "bpy_side/assets/sheet_worker.py")
        sys.path.insert(0, str(ROOT / "bpy_side"))
        try:
            sw = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sw)
        finally:
            sys.path.remove(str(ROOT / "bpy_side"))
        pitch = math.radians(15.0)
        for k in range(8):
            az = math.radians(45.0 * k)
            want = (math.sin(az) * math.cos(pitch), -math.cos(az) * math.cos(pitch), math.sin(pitch))  # orbit_worker.place()
            got = sw.direction(45000 * k, 15000)
            for a, b in zip(got, want):
                self.assertAlmostEqual(a, b, places=12)
        self.assertEqual(len(rs.VIEWS), rs.SHEET_VIEWS)
        self.assertEqual(len({v[0] for v in rs.VIEWS}), rs.SHEET_VIEWS)


@unittest.skipUnless(os.environ.get("BVQA_BLENDER") == "1" and shutil.which("blender"), "BVQA_BLENDER=1 + blender needed")
class SheetBlenderTests(unittest.TestCase):
    def test_real_sheet_small(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=SCRATCH))
        try:
            glb = tmp / "crate.glb"
            glb.write_bytes(make_glb())
            rc, rec = rs.run(glb, tmp / "out", res=RES, spp=2, threads=4, timeout=600)
            self.assertEqual((rc, rec["verdict"], rec["failed"]), (3, "PREVIEW", []), rec["checks"])
            self.assertEqual(len(rec["images"]), rs.SHEET_VIEWS * len(rs.PASSES))
            self.assertTrue((tmp / "out" / "sheet_beauty.png").is_file())
            self.assertTrue(rec["blender"].startswith("4."))
        finally:
            shutil.rmtree(tmp, True)


if __name__ == "__main__":
    unittest.main()
