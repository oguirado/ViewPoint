"""ER-1 / ER-2: raw X11 captures are frames for check_disk; GuiAdapter ingests raw RGB24 byte-identically to PNG."""
import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from visioncore import frame as vframe
from visioncore import pipeline
from visioncore.errors import VqaError
from vadapt import ramws
from vadapt.gui import GuiAdapter
from vadapt.synth.pngenc import encode_png

ROOT = Path(__file__).resolve().parent.parent


def shot(k, W=64, H=48):
    rgb = np.full((H, W, 3), 10 * k, np.uint8)
    rgb[:, :, 1] = np.arange(W, dtype=np.uint8)
    rgb[k : k + 8, 8:24, 0] = 200
    return rgb


class CheckDiskRaw(unittest.TestCase):
    def test_raw_x11_names_flagged(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as b:
            b = Path(b)
            for rel in ("a/cap.raw", "a/cap.XWD", "frames.raw", "x/Xvfb_screen0", "rows.jsonl"):
                (b / rel).parent.mkdir(parents=True, exist_ok=True)
                (b / rel).write_bytes(b"x")
            self.assertEqual(ramws.check_disk(b), ["a/cap.XWD", "a/cap.raw", "frames.raw", "x/Xvfb_screen0"])


class RawIngest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="vraw_"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.n, self.W, self.H = 5, 64, 48
        self.shots = [shot(k, self.W, self.H) for k in range(self.n)]

    def png_run(self):
        r = self.d / "png"
        (r / "screens").mkdir(parents=True)
        for k, s in enumerate(self.shots):
            (r / "screens" / ("s%d.png" % k)).write_bytes(encode_png(s))
        return r

    def raw_run(self, mode="single", stride=None):
        r = self.d / ("raw_" + mode)
        r.mkdir()
        stride = stride or self.W * 3
        pad = stride - self.W * 3

        def b(s):
            return b"".join(row.tobytes() + b"\xee" * pad for row in s)
        meta = {"width": self.W, "height": self.H, "stride": stride}
        if mode == "single":
            (r / "frames.raw").write_bytes(b"".join(b(s) for s in self.shots))
            meta["file"] = "frames.raw"
        else:
            for k, s in enumerate(self.shots):
                (r / ("f%d.raw" % k)).write_bytes(b(s))
            meta["files"] = ["f%d.raw" % k for k in range(self.n)]
        (r / "raw.json").write_text(json.dumps(meta))
        return r

    def check_equal(self, rawdir):
        a = GuiAdapter()
        ps, rs = a.frames(self.png_run()), a.frames(rawdir)
        self.assertEqual(rs.meta["storage"], "rgb24_raw")
        self.assertEqual(rs.meta["frame_bytes"], self.W * self.H * 3)
        self.assertEqual(a.scope(rawdir), a.scope(self.d / "png"))
        for p, r in zip(ps, rs):
            self.assertEqual(p.sha256, r.sha256)
            self.assertTrue(np.array_equal(p.rgb, r.rgb))
        vframe.validate_manifest(rs.meta)
        return ps, rs

    def test_single_file(self):
        self.check_equal(self.raw_run("single"))

    def test_per_frame_files(self):
        self.check_equal(self.raw_run("files"))

    def test_stride_padding(self):
        self.check_equal(self.raw_run("single", stride=self.W * 3 + 16))

    def test_zero_copy_view(self):
        _, rs = self.check_equal(self.raw_run("single"))
        f = list(rs)[2]
        self.assertFalse(f.rgb.flags.writeable)
        self.assertIsNotNone(f.rgb.base)  # a view, not an owned copy

    def test_bad_size_rejected(self):
        r = self.raw_run("single")
        with open(r / "frames.raw", "ab") as fh:
            fh.write(b"zz")
        with self.assertRaises(VqaError) as c:
            GuiAdapter().frames(r)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SIZE")

    def test_analysis_identical_after_adapt(self):
        a = GuiAdapter()
        outs = {}
        for name, run in (("png", self.png_run()), ("raw", self.raw_run("single"))):
            out = self.d / ("out_" + name)
            a.adapt(run, out)
            res = out / "res"
            pipeline.analyze(pipeline.ManifestSource(out / "frames.manifest.json"), out_dir=res)
            outs[name] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(res.iterdir()) if p.is_file()}
        self.assertTrue(outs["png"])
        self.assertEqual(outs["png"], outs["raw"])

    def test_timing_log_only(self):
        W, H, n = 1280, 720, 6
        rng = np.random.default_rng(1)
        base = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
        r = self.d / "t_png"
        (r / "screens").mkdir(parents=True)
        rr = self.d / "t_raw"
        rr.mkdir()
        raw = bytearray()
        for k in range(n):
            s = np.roll(base, k, axis=1)
            (r / "screens" / ("s%d.png" % k)).write_bytes(encode_png(s))
            raw += s.tobytes()
        (rr / "frames.raw").write_bytes(bytes(raw))
        (rr / "raw.json").write_text(json.dumps({"width": W, "height": H, "stride": W * 3, "file": "frames.raw"}))
        t = {}
        for name, d in (("png", r), ("raw", rr)):
            t0 = time.perf_counter()
            for _ in GuiAdapter().frames(d):
                pass
            t[name] = (time.perf_counter() - t0) * 1000 / n
        print("\n[ER-2 timing 1280x720] png %.1f ms/frame, raw %.1f ms/frame" % (t["png"], t["raw"]))


if __name__ == "__main__":
    unittest.main()
