"""AG1 L4: turntable host layer. Fake-runner tests always run; Blender tests need BVQA_BLENDER=1 (72 frames: BVQA_SLOW=1).

BVQA_BLENDER=1 taskset -c 5-9 python3 -m unittest tests.test_ag1_turntable -v
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blender_visual_qa.assets import turntable as tt  # noqa: E402
from blender_visual_qa.drive import png  # noqa: E402

SCRATCH = ROOT / "build" / "room_L4"
SMALL_RES = 16
SMALL_FRAMES = 6


def make_glb():
    """An asymmetric L-shaped solid (two boxes) as a minimal GLB: no azimuth looks like another."""
    def box(x0, y0, z0, x1, y1, z1, base):
        v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
        quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        idx = []
        for a, b, c, d in quads:
            idx += [base + a, base + b, base + c, base + a, base + c, base + d]
        return v, idx
    v1, i1 = box(-1.0, 0.0, -0.5, 1.0, 1.0, 0.5, 0)
    v2, i2 = box(0.2, 1.0, -0.5, 1.0, 2.0, 0.2, 8)
    verts, idx = v1 + v2, i1 + i2
    pos = b"".join(struct.pack("<3f", *p) for p in verts)
    ind = b"".join(struct.pack("<H", i) for i in idx)
    blob = pos + ind
    lo = [min(p[k] for p in verts) for k in range(3)]
    hi = [max(p[k] for p in verts) for k in range(3)]
    js = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "crate"}],
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
    """Stands in for the Blender subprocess: writes distinct small PNGs per frame (content = f(frame, scale))."""

    def __init__(self, dup_from=None, ignore_scale=False, flaky=False):
        self.dup_from, self.ignore_scale, self.flaky, self.calls = dup_from, ignore_scale, flaky, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        out, res = Path(arg(argv, "--out")), int(arg(argv, "--res"))
        first, last = int(arg(argv, "--first")), int(arg(argv, "--last"))
        scale = int(argv[argv.index("--scale-x-permille") + 1]) if "--scale-x-permille" in argv else 0
        out.mkdir(parents=True, exist_ok=True)
        for n in range(first, last + 1):
            seed = n - 1 if (self.dup_from is None or n != self.dup_from) else n - 2
            if scale and not self.ignore_scale:
                seed += 1000
            if self.flaky and len(self.calls) > 1:
                seed += 7000
            rgb = bytes((seed * 7 + i) % 256 for i in range(res * res * 3))
            (out / ("%06d.png" % n)).write_bytes(png.encode(res, res, rgb))
        info = {"frames": list(range(first, last + 1)), "blender": "fake", "centre_um": [0, 0, 0], "radius_um": 1}
        return 0, "BVQA_ORBIT " + json.dumps(info) + "\n", ""


def fake_vision(out, vision):
    return {"status": "COMPLETE", "mode": vision}


class HostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix="tt_", dir=SCRATCH)
        cls.root = Path(cls.tmp.name)
        cls.glb = cls.root / "crate.glb"
        cls.glb.write_bytes(make_glb())
        proto = json.loads(tt.PROTOCOL.read_text())
        proto["turntable"].update({"frames": SMALL_FRAMES, "res": SMALL_RES})
        cls.proto = cls.root / "protocol.json"
        cls.proto.write_text(json.dumps(proto))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def go(self, name, runner=None, **kw):
        runner = runner or FakeBlender()
        kw.setdefault("protocol", self.proto)
        kw.setdefault("vision_runner", fake_vision)
        rc, rec = tt.run(self.glb, self.root / name, runner=runner, **kw)
        return rc, rec, runner

    def test_protocol_defaults(self):
        t = tt.load_protocol()["turntable"]
        self.assertEqual((t["frames"], t["step_mdeg"], t["res"], t["spp"], t["pitch_mdeg"]), (72, 5000, 512, 64, 15000))

    def test_pass_receipt_and_layout(self):
        # STORAGE LAW: frames live in the ramws workspace (ws.path("turntable/")); `out` keeps receipts + manifests
        mod, ws = tt.open_workspace(self.root / "ok")
        self.addCleanup(ws.close)
        rc, rec, run = self.go("ok", ws=ws)
        self.assertEqual((rc, rec["verdict"], rec["failed"]), (tt.RC_OK, "PASS", []))
        d = self.root / "ok"
        self.assertEqual(list((d / "render").glob("*.png")), [])
        self.assertEqual(sorted(p.name for p in (ws.path("turntable/") / "render").glob("*.png")), ["%06d.png" % n for n in range(1, SMALL_FRAMES + 1)])
        self.assertEqual(rec["input_sha256"], hashlib.sha256(self.glb.read_bytes()).hexdigest())
        self.assertEqual([r["az_mdeg"] for r in rec["frames"]], [i * 5000 for i in range(SMALL_FRAMES)])
        on_disk = (d / "turntable_receipt.json").read_text()
        self.assertEqual(on_disk, tt.canonical(json.loads(on_disk)))
        self.assertEqual(json.loads((d / "frames.manifest.json").read_text())["layout"], "render/%06d.png")
        names = {c["check"] for c in rec["checks"]}
        self.assertTrue({"sequence_complete", "adjacent_differ", "rerender_identical", "scale_x_detected"} <= names)
        self.assertEqual(rec["vision"]["status"], "COMPLETE")
        self.assertEqual(len(run.calls), 3)  # full run + re-render + scale-X mutant

    def test_receipt_chain(self):
        _, first, _ = self.go("chain1")
        sha = hashlib.sha256((self.root / "chain1" / "turntable_receipt.json").read_bytes()).hexdigest()
        _, second, _ = self.go("chain2", prev=self.root / "chain1" / "turntable_receipt.json")
        self.assertIsNone(first["prev_receipt_sha256"])
        self.assertEqual(second["prev_receipt_sha256"], sha)

    def test_mutant_duplicated_frame_rc5(self):
        rc, rec, _ = self.go("dup", FakeBlender(dup_from=3))
        self.assertEqual(rc, tt.RC_GATE)
        self.assertIn("adjacent_differ", rec["failed"])

    def test_mutant_step_4999_is_preview_rc3(self):
        rc, rec, _ = self.go("step", step_mdeg=4999)
        self.assertEqual((rc, rec["verdict"]), (tt.RC_PREVIEW, "PREVIEW"))
        self.assertTrue(any("step_mdeg" in r for r in rec["preview_reasons"]))
        self.assertEqual(rec["failed"], [])

    def test_reduced_frames_preview_rc3(self):
        rc, rec, _ = self.go("short", frames=SMALL_FRAMES - 2)
        self.assertEqual(rc, tt.RC_PREVIEW)

    def test_mutant_scale_x_not_detected_rc5(self):
        rc, rec, _ = self.go("scale", FakeBlender(ignore_scale=True))
        self.assertEqual(rc, tt.RC_GATE)
        self.assertIn("scale_x_detected", rec["failed"])

    def test_mutant_nondeterministic_rerender_rc5(self):
        rc, rec, _ = self.go("flaky", FakeBlender(flaky=True))
        self.assertEqual(rc, tt.RC_GATE)
        self.assertIn("rerender_identical", rec["failed"])

    def test_wrap_around_rejected_without_rendering(self):
        rc, rec, run = self.go("wrap", step_mdeg=60001)
        self.assertEqual(rc, tt.RC_GATE)
        self.assertFalse(run.calls)
        self.assertFalse((self.root / "wrap" / "render").exists())

    def test_zero_frames_rc3(self):
        rc, rec, run = self.go("zero", frames=0)
        self.assertEqual(rc, tt.RC_PREVIEW)
        self.assertFalse(run.calls)

    def test_missing_glb_rc2_and_vision_mode(self):
        with self.assertRaises(tt.TurntableError) as cm:
            tt.run(self.root / "none.glb", self.root / "x", runner=FakeBlender())
        self.assertEqual(cm.exception.rc, tt.RC_IO)
        with self.assertRaises(tt.TurntableError):
            tt.run(self.glb, self.root / "x", vision="maybe", runner=FakeBlender())

    def test_worker_failure_rc2(self):
        with self.assertRaises(tt.TurntableError) as cm:
            tt.run(self.glb, self.root / "bad", protocol=self.proto, runner=lambda a, t: (1, "", "boom"), vision="off")
        self.assertEqual(cm.exception.rc, tt.RC_IO)

    def test_vision_on_failure_gates_shadow_failure_advisory(self):
        def boom(out, vision):
            raise ValueError("E_VISION: x")
        rc, rec, _ = self.go("vs", vision="shadow", vision_runner=boom)
        self.assertEqual((rc, rec["vision"]["status"]), (tt.RC_OK, "ERROR"))
        rc, rec, _ = self.go("von", vision="on", vision_runner=boom)
        self.assertEqual(rc, tt.RC_GATE)
        self.assertIn("vision_stage", rec["failed"])

    def test_cli_exit_codes(self):
        self.assertEqual(tt.main(["--glb", str(self.root / "none.glb"), "--out", str(self.root / "c")]), tt.RC_IO)
        self.assertEqual(tt.main(["--bogus"]), tt.RC_IO)

    def test_png_pixel_sha_rejects_garbage(self):
        bad = self.root / "bad.png"
        bad.write_bytes(b"not a png")
        with self.assertRaises(tt.TurntableError):
            tt.png_pixel_sha(bad)

    def test_real_vision_stage_on_synthetic_frames(self):
        rc, rec, _ = self.go("real", vision_runner=None, vision="shadow")
        self.assertEqual(rc, tt.RC_OK)
        self.assertEqual(rec["vision"]["status"], "COMPLETE", rec["vision"])


@unittest.skipUnless(os.environ.get("BVQA_BLENDER") == "1", "needs BVQA_BLENDER=1 (CPU Blender)")
class BlenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix="ttb_", dir=SCRATCH)
        cls.root = Path(cls.tmp.name)
        cls.glb = cls.root / "crate.glb"
        cls.glb.write_bytes(make_glb())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_reduced_real_render_is_preview(self):
        rc, rec = tt.run(self.glb, self.root / "r", frames=3, res=64, spp=4, threads=4, vision="off")
        self.assertEqual((rc, rec["failed"]), (tt.RC_PREVIEW, []), rec["checks"])
        self.assertEqual(len(rec["frames"]), 3)
        self.assertEqual(rec["blender"], "4.0.2")

    def test_reduced_real_render_with_truth(self):
        rc, rec = tt.run(self.glb, self.root / "t", frames=3, res=64, spp=4, threads=4, vision="off", with_truth=True)
        self.assertEqual((rc, rec["failed"]), (tt.RC_PREVIEW, []), rec["checks"])
        self.assertTrue(any(c["check"] == "truth" and c["ok"] for c in rec["checks"]))

    @unittest.skipUnless(os.environ.get("BVQA_SLOW") == "1", "72 frames: BVQA_SLOW=1")
    def test_full_protocol_72_frames(self):
        rc, rec = tt.run(self.glb, self.root / "full", threads=4, vision="shadow")
        self.assertEqual((rc, rec["verdict"], rec["failed"]), (tt.RC_OK, "PASS", []), rec["checks"])
        self.assertEqual(len(rec["frames"]), 72)
        self.assertEqual(rec["vision"]["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
