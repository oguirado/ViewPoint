"""vadapt.godot: capture dir -> vqa.frame/v1 + region hints (no Godot)."""
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

from vadapt.godot import GodotAdapter, convert, main
from vadapt.godot import from_capture_dir as fcd
from visioncore.decode import png as read_png
from vadapt.synth.pngenc import encode_png
from visioncore import frame as vframe
from visioncore.errors import VqaError

Q = 65536


def make_capture(root, n=4, W=64, H=48, fps=10, ui_rect=(Q * 4, Q * 2, Q * 8, Q * 6), still=0):
    root = Path(root)
    (root / "capture" / "frames").mkdir(parents=True)
    recs, tel = [], []
    for k in range(n):
        rgb = np.full((H, W, 3), 10 * k, np.uint8)
        rgb[:, :, 1] = np.arange(W, dtype=np.uint8)
        data = encode_png(rgb)
        name = "f%06d.png" % (k * 2)  # sparse render indices
        (root / "capture" / "frames" / name).write_bytes(data)
        recs.append({"evidence_id": "frame:%d" % (k * 2), "render_index": k * 2, "tick": k,
                     "file": {"path": "capture/frames/" + name, "sha256": hashlib.sha256(data).hexdigest()},
                     "decoded_rgb_sha256": hashlib.sha256(rgb.tobytes()).hexdigest(), "viewport_px": [W, H]})
        tel.append({"schema": "vqa.probe/v1", "render_index": k * 2, "tick": k, "viewport_px": [W, H],
                    "ui": [{"id": "Hud Label", "role": "hud", "visible": True, "rect_px_q16": list(ui_rect)},
                           {"id": "hidden", "visible": False, "rect_px_q16": list(ui_rect)},
                           {"id": "off", "visible": True, "rect_px_q16": [Q * 500, 0, Q, Q]}],
                    "actors": [{"id": "hero", "alive": True, "visible": True, "frustum_inside": True,
                                "in_front": True, "screen_bounds_px_q16": [Q * 10 + 1, Q * 10, Q * 5, Q * 5]},
                               {"id": "behind", "alive": True, "visible": True, "in_front": False,
                                "screen_bounds_px_q16": [0, 0, Q, Q]}],
                    "camera": {"transform": [1.0, 0.5]}})
    cap = {"schema": "vqa.capture/v1", "adapter_id": "Operator Zero", "ruleset_id": "combat-hud", "fps": fps,
           "viewport_px": [W, H], "telemetry": {"path": "capture/telemetry.jsonl", "sha256": "0" * 64}, "frames": recs}
    (root / "capture" / "manifest.json").write_text(json.dumps(cap))
    (root / "capture" / "telemetry.jsonl").write_text("\n".join(json.dumps(t) for t in tel) + "\n")
    if still:
        (root / "still_probe").mkdir()
        for k in range(still):
            (root / "still_probe" / ("s%02d.png" % k)).write_bytes(encode_png(np.full((H, W, 3), 7, np.uint8)))
    return root


class T(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="vgodot_"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cap = make_capture(self.d / "run")

    def test_frames_manifest(self):
        src = GodotAdapter().frames(self.cap)
        self.assertEqual(len(src), 4)
        m = src.meta
        vframe.validate_manifest(m)
        self.assertEqual((m["width"], m["height"], m["storage"]), (64, 48, "png_dir"))
        self.assertEqual([e["render_index"] for e in m["frames"]], [0, 2, 4, 6])
        self.assertEqual(m["frames"][1]["t_us"], 2 * 1000000 // 10)
        self.assertEqual(m["scope"]["app"], "operator_zero")
        self.assertEqual(m["scope"]["scene"], "combat_hud")
        self.assertEqual(m["provenance"]["adapter_id"], "godot")
        fr = list(src)
        self.assertEqual(fr[2].sha256, m["frames"][2]["sha256"])

    def test_capture_dir_itself_accepted(self):
        src = GodotAdapter().frames(self.cap / "capture")
        self.assertEqual(len(src), 4)

    def test_hints(self):
        rows = list(GodotAdapter().hints(self.cap))
        f0 = [r for r in rows if r["i"] == 0]
        self.assertEqual([r["rid"] for r in f0], ["h:actor:hero", "h:ui:hud_label"])
        ui = [r for r in f0 if r["role"] == "ui"][0]
        self.assertEqual(ui["bbox_px"], [4, 2, 8, 6])
        act = [r for r in f0 if r["role"] == "actor"][0]
        self.assertEqual(act["bbox_px"], [10, 10, 6, 5])  # origin floored, far edge ceiled
        self.assertEqual(len(rows), 8)  # hidden, off-screen and behind-camera dropped

    def test_convert_writes_files(self):
        out = self.d / "out"
        res = convert(self.cap, out)
        self.assertEqual(res["rc"], 0)
        m = vframe.read_manifest(out / "frames.manifest.json")
        self.assertTrue((out / m["frames"][0]["file"]).is_file())
        lines = (out / "region_hints.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertEqual(json.loads(lines[0])["i"], 0)

    def test_deterministic(self):
        a, b = self.d / "a", self.d / "b"
        convert(self.cap, a)
        convert(self.cap, b)
        for n in ("frames.manifest.json", "region_hints.jsonl"):
            self.assertEqual((a / n).read_bytes(), (b / n).read_bytes())

    def test_no_telemetry_no_hints(self):
        (self.cap / "capture" / "telemetry.jsonl").unlink()
        self.assertIsNone(GodotAdapter().hints(self.cap))
        out = self.d / "o"
        convert(self.cap, out)
        self.assertFalse((out / "region_hints.jsonl").exists())
        self.assertIsNone(vframe.read_manifest(out / "frames.manifest.json")["hints"])

    def test_still_probe(self):
        self.assertIsNone(GodotAdapter().still_probe(self.cap))
        c2 = make_capture(self.d / "run2", still=5)
        sp = GodotAdapter().still_probe(c2)
        self.assertEqual(len(sp), 5)

    def test_scope(self):
        sc = GodotAdapter().scope(self.cap)
        self.assertEqual(sc["res"], "64x48")
        self.assertIsNone(sc["build_seq"])

    # negatives
    def test_missing_manifest(self):
        with self.assertRaises(VqaError) as c:
            GodotAdapter().frames(self.d / "nothing")
        self.assertEqual((c.exception.code, c.exception.rc), ("E_IO", 2))

    def test_flipped_png_byte(self):
        p = self.cap / "capture" / "frames" / "f000002.png"
        b = bytearray(p.read_bytes())
        b[-20] ^= 1
        p.write_bytes(bytes(b))
        with self.assertRaises(VqaError) as c:
            GodotAdapter().frames(self.cap)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SHA")

    def test_decoded_sha_mismatch(self):
        mp = self.cap / "capture" / "manifest.json"
        cap = json.loads(mp.read_text())
        cap["frames"][0]["decoded_rgb_sha256"] = "1" * 64
        mp.write_text(json.dumps(cap))
        with self.assertRaises(VqaError) as c:
            GodotAdapter().frames(self.cap)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SHA")

    def test_mixed_sizes(self):
        p = self.cap / "capture" / "frames" / "f000004.png"
        data = encode_png(np.zeros((48, 32, 3), np.uint8))
        p.write_bytes(data)
        mp = self.cap / "capture" / "manifest.json"
        cap = json.loads(mp.read_text())
        cap["frames"][2]["file"]["sha256"] = hashlib.sha256(data).hexdigest()
        cap["frames"][2].pop("decoded_rgb_sha256")
        mp.write_text(json.dumps(cap))
        with self.assertRaises(VqaError) as c:
            GodotAdapter().frames(self.cap)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SIZE")

    def test_refusal_leaves_out_untouched(self):
        out = self.d / "out"
        convert(self.cap, out)
        before = (out / "frames.manifest.json").read_bytes()
        p = self.cap / "capture" / "frames" / "f000000.png"
        p.write_bytes(p.read_bytes()[:-9] + b"x" * 9)
        with self.assertRaises(VqaError):
            convert(self.cap, out)
        self.assertEqual((out / "frames.manifest.json").read_bytes(), before)

    def test_cli(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--in", str(self.cap), "--out", str(self.d / "c")])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["code"], "OK")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--in", str(self.d / "zz"), "--out", str(self.d / "c2")])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(buf.getvalue())["code"], "E_IO")

    def test_no_godot_import(self):
        src = Path(fcd.__file__).read_text()
        self.assertNotIn("import godot", src)
        self.assertNotIn("from godot", src)

    def test_rect_clip(self):
        self.assertIsNone(fcd._rect_px([0, 0, 0, Q], 64, 48))
        self.assertEqual(fcd._rect_px([-Q * 4, 0, Q * 8, Q], 64, 48), [0, 0, 4, 1])
        self.assertIsNone(fcd._rect_px([0, 0, 1.5, 2], 64, 48))



class PngFilters(unittest.TestCase):
    def test_all_filters_roundtrip(self):
        img = (np.arange(8 * 9 * 3, dtype=np.uint16).reshape(8, 9, 3) * 7 % 256).astype(np.uint8)
        for ft in range(5):
            self.assertTrue(np.array_equal(read_png_bytes(encode_png(img, filt=ft)), img), ft)

    def test_rgba_drops_alpha(self):
        img = np.full((4, 4, 3), 90, np.uint8)
        a = np.arange(16, dtype=np.uint8).reshape(4, 4)
        self.assertTrue(np.array_equal(read_png_bytes(encode_png(img, alpha=a)), img))

    def test_garbage_rejected(self):
        with self.assertRaises(VqaError) as c:
            read_png_bytes(b"nope")
        self.assertEqual(c.exception.code, "E_VC_PNG")


def read_png_bytes(b):
    d = tempfile.mkdtemp(prefix="vpng_")
    try:
        p = Path(d) / "x.png"
        p.write_bytes(b)
        return read_png(p)
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    unittest.main()
