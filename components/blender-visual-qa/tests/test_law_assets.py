"""STORAGE LAW (B2): asset layer frames live in a vadapt.ramws workspace; `out` keeps receipts + deliverable maps only.

python3 -m unittest tests.test_law_assets -v
"""
import ast
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blender_visual_qa.assets import bake_pbr as B  # noqa: E402
from blender_visual_qa.assets import delight  # noqa: E402
from blender_visual_qa.assets import render_sheet as rs  # noqa: E402
from blender_visual_qa.assets import turntable as tt  # noqa: E402
from bpy_side.canon import RAMWS_ROOT, require_ram_out as _require_ram_out  # noqa: E402
from tests import test_ag1_turntable as t1  # noqa: E402
from tests import test_ag2_sheet as t2  # noqa: E402
from tests import test_ag3_bake as t3  # noqa: E402

VISION_CONFIG = json.loads((ROOT / "config/vision.json").read_text())
WHEEL = ROOT / VISION_CONFIG["engine_path"]
WHEEL_SHA = VISION_CONFIG["pin"]["wheel_sha256"]
IMAGE_EXT = {".png", ".exr", ".npy", ".npz", ".blend", ".jpg", ".tga", ".bmp"}
RAM_BASE = Path(RAMWS_ROOT) / tt.RAM_TOOL
SCRATCH = ROOT / "build" / "room_LAWB2"


def images(d):
    return sorted(str(p) for p in Path(d).rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT)


class LawBase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.td = Path(tempfile.mkdtemp(prefix="law_", dir=SCRATCH))
        self.addCleanup(shutil.rmtree, self.td, True)
        self.cfg = self.td / "vision.json"
        self.cfg.write_text(json.dumps({"engine_path": str(WHEEL), "pin": {"wheel_sha256": WHEEL_SHA}}))
        self.addCleanup(lambda: self.assertEqual(list(RAM_BASE.glob("*/*")) if RAM_BASE.exists() else [], []))


class PinTests(LawBase):
    def test_loads_pinned_wheel(self):
        mod = tt.load_ramws(self.cfg)
        self.assertTrue(hasattr(mod, "open_run"))

    def test_wrong_sha_fails_closed(self):
        self.cfg.write_text(json.dumps({"engine_path": str(WHEEL), "pin": {"wheel_sha256": "0" * 64}}))
        with self.assertRaises(tt.TurntableError) as cm:
            tt.load_ramws(self.cfg)
        self.assertEqual((cm.exception.rc, cm.exception.code), (2, "E_PIN"))

    def test_wheel_without_ramws_fails_closed(self):
        old = ROOT / "build/room_P/wheels/vision_engine-1.1.0-py3-none-any.whl"
        if not old.is_file():
            self.skipTest("old wheel absent")
        self.cfg.write_text(json.dumps({"engine_path": str(old), "pin": {"wheel_sha256": tt.sha256_file(old)}}))
        with self.assertRaises(tt.TurntableError) as cm:
            tt.load_ramws(self.cfg)
        self.assertEqual(cm.exception.code, "E_PIN")

    def test_quota_is_4_gib(self):
        mod, ws = tt.open_workspace(self.td / "o", config=self.cfg)
        try:
            self.assertEqual(ws.quota, 4 << 30)
            self.assertTrue(str(ws.root).startswith(f"{RAMWS_ROOT}/{tt.RAM_TOOL}/"))
        finally:
            ws.close()


class TurntableLaw(LawBase):
    def test_zero_images_outside_workspace(self):
        glb = self.td / "c.glb"
        glb.write_bytes(t1.make_glb())
        proto = json.loads(tt.PROTOCOL.read_text())
        proto["turntable"].update({"frames": 6, "res": 16})
        pp = self.td / "p.json"
        pp.write_text(json.dumps(proto))
        fb = t1.FakeBlender()
        seen = []
        out = self.td / "out"

        def vision(work, vision):
            seen.append(str(work))
            return {"status": "COMPLETE"}
        rc, rec = tt.run(glb, out, protocol=pp, runner=fb, vision_runner=vision, ram_config=self.cfg)
        self.assertEqual(rc, 0, rec["failed"])
        self.assertEqual(images(self.td), [])  # 0 images anywhere outside the workspace
        for argv in fb.calls:
            self.assertTrue(argv[argv.index("--out") + 1].startswith(RAMWS_ROOT + "/"), argv)
        self.assertTrue(seen and seen[0].startswith(RAMWS_ROOT + "/"))
        self.assertTrue((out / "turntable_receipt.json").is_file() and (out / "frames.manifest.json").is_file())
        ram = json.loads((out / "ram_receipt.json").read_text())
        self.assertEqual((ram["quota"], ram["fstype"]), (4 << 30, "tmpfs"))
        self.assertGreater(ram["ram_peak_bytes"], 0)
        self.assertNotIn("/dev/shm", (out / "turntable_receipt.json").read_text())

    def test_quota_breach_rc5_and_released(self):
        glb = self.td / "c.glb"
        glb.write_bytes(t1.make_glb())
        mod, ws = tt.open_workspace(self.td / "o", quota=4096, config=self.cfg)
        try:
            with self.assertRaises(tt.TurntableError) as cm:
                tt.run(glb, self.td / "o", frames=6, res=64, ws=ws, runner=t1.FakeBlender(), vision="off", selfchecks=False)
            self.assertEqual((cm.exception.rc, cm.exception.code), (5, "E_RAM_QUOTA"))
        finally:
            ws.close()

    def test_keep_frames_opt_in_capped(self):
        glb = self.td / "c.glb"
        glb.write_bytes(t1.make_glb())
        proto = json.loads(tt.PROTOCOL.read_text())
        proto["turntable"].update({"frames": 6, "res": 16})
        pp = self.td / "p.json"
        pp.write_text(json.dumps(proto))
        out = self.td / "kout"
        kept = ROOT / "build/kept_frames" / out.name
        self.addCleanup(shutil.rmtree, kept, True)
        rc, _ = tt.run(glb, out, protocol=pp, runner=t1.FakeBlender(), vision="off", keep_mb=1, ram_config=self.cfg)
        self.assertEqual(rc, 0)
        keep = json.loads((kept / "KEEP.json").read_text())
        self.assertLessEqual(keep["bytes"], 1 << 20)
        self.assertEqual(images(out), [])


class SheetLaw(LawBase):
    def test_zero_images_outside_workspace(self):
        glb = self.td / "c.glb"
        glb.write_bytes(t2.make_glb())
        fb = t2.FakeBlender()
        out = self.td / "out"
        rc, rec = rs.run(glb, out, res=16, spp=8, views=["P0", "P1"], runner=fb, ram_config=self.cfg)
        self.assertIn(rc, (0, 3), rec["failed"])
        self.assertEqual(images(self.td), [])
        self.assertTrue(rec["sheets"])  # sheets were still built (in RAM) and hashed
        for argv in fb.calls:
            self.assertTrue(argv[argv.index("--out") + 1].startswith(RAMWS_ROOT + "/"), argv)
        self.assertTrue((out / "render_receipt.json").is_file() and (out / "ram_receipt.json").is_file())


class BakeLaw(LawBase):
    def setUp(self):
        super().setUp()
        self.high, self.low, self.out = self.td / "h.glb", self.td / "l.glb", self.td / "out"
        self.high.write_bytes(t3.g.write_glb(t3.g.Scene([t3.sphere("ball", 16, 8)])))
        self.low.write_bytes(t3.g.write_glb(t3.g.Scene([t3.sphere("ball", 8, 4)])))

    def bake(self, runner, **kw):
        return B.run(["--high", str(self.high), "--low", str(self.low), "--out-dir", str(self.out), "--res", "64"],
                     runner=runner, ram_config=self.cfg, **kw)

    def test_worker_writes_in_ram_and_only_deliverables_persist(self):
        r = t3.FakeRunner(64)
        rc, rec = self.bake(r)
        self.assertEqual((rc, rec["verdict"]), (0, "PASS"), rec["reason"])
        self.assertTrue(r.calls[0][r.calls[0].index("--out-dir") + 1].startswith(RAMWS_ROOT + "/"))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         sorted([m + ".png" for m in B.MAPS] + ["low_uv.glb", "ram_receipt.json"]))
        self.assertLessEqual(rec["deliverable_bytes"], B.DELIVERABLE_CAP)

    def test_failed_bake_delivers_nothing(self):
        rc, rec = self.bake(t3.FakeRunner(64, skip=("normal",)))
        self.assertNotEqual(rc, 0)
        self.assertEqual(images(self.out), [])

    def test_deliverable_cap_fails_closed(self):
        old = B.DELIVERABLE_CAP
        B.DELIVERABLE_CAP = 10
        self.addCleanup(setattr, B, "DELIVERABLE_CAP", old)
        with self.assertRaises(B.BakeError):
            B.deliver(self._fake_work(), self.out)

    def _fake_work(self):
        w = self.td / "w"
        w.mkdir()
        for m in B.MAPS:
            (w / (m + ".png")).write_bytes(b"x" * 8)
        (w / B.LOW_UV).write_bytes(b"x")
        return w


class DelightLaw(unittest.TestCase):
    def test_deliverable_cap(self):
        td = Path(tempfile.mkdtemp(dir=SCRATCH if SCRATCH.exists() else None))
        self.addCleanup(shutil.rmtree, td, True)
        px = delight.encode_png(2, 1, bytes([100, 100, 100, 255] * 2))
        (td / "d.png").write_bytes(px)
        (td / "i.png").write_bytes(px)
        old = delight.DELIVERABLE_CAP
        delight.DELIVERABLE_CAP = 8
        self.addCleanup(setattr, delight, "DELIVERABLE_CAP", old)
        rc, rec = delight.run(["--diffuse", str(td / "d.png"), "--irradiance", str(td / "i.png"), "--out", str(td / "o.png")])
        self.assertEqual((rc, rec), (2, None))
        self.assertFalse((td / "o.png").exists())


class WorkerGuard(unittest.TestCase):
    """The bpy workers import bpy at call time, so extract require_ram_out from source and execute it."""

    def guard(self, name):
        src = (ROOT / "bpy_side/assets" / name).read_text()
        self.assertIn("from canon import RAMWS_ROOT", src, name)
        tree = ast.parse(src)
        keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "require_ram_out"]
        self.assertEqual(len(keep), 1, name)
        ns = {"os": os, "sys": sys, "RAMWS_ROOT": RAMWS_ROOT, "_require_ram_out": _require_ram_out}
        exec(compile(ast.Module(keep, []), name, "exec"), ns)
        return ns["require_ram_out"]

    def test_workers_refuse_disk_and_accept_ram(self):
        for name in ("orbit_worker.py", "sheet_worker.py", "bake_worker.py"):
            g = self.guard(name)
            with self.assertRaises(SystemExit) as cm:
                g("/tmp/not_ram/out")
            self.assertEqual(cm.exception.code, 5, name)
            with self.assertRaises(SystemExit):
                g(RAMWS_ROOT + "/../escape")
            g(f"{RAMWS_ROOT}/bvqa-asset/asset/run/render")
            src = (ROOT / "bpy_side/assets" / name).read_text()
            self.assertIn("require_ram_out(a.out", src)


if __name__ == "__main__":
    unittest.main()
