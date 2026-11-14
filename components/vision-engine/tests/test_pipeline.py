"""L4 pipeline + vcore CLI tests (SPEC_ARCH §3, SPEC_INT R4): end-to-end on vadapt/synth, determinism, refusal."""
import hashlib
import io
import json
import contextlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from visioncore import canon, cli, layout, params as P, pipeline
from visioncore.errors import VqaError
from visioncore.frame import Frame, MemoryFrameSource
from vadapt.synth import SynthAdapter


def make_run(name="scroll", **kw):
    d = tempfile.mkdtemp(prefix="pipe_")
    SynthAdapter().write_run(d, name, 1, **kw)
    return Path(d)


def tree_sha(d):
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(d).rglob("*")) if p.is_file()}


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, json.loads(out.getvalue())


class TestAnalyze(unittest.TestCase):
    def test_end_to_end_scroll(self):
        d = make_run("scroll")
        out = d / "vision"
        run = pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"), None, None, out)
        for f in pipeline.FILES:
            self.assertTrue((out / f).is_file(), f)
        v = canon.loads((out / "vision.json").read_text())
        self.assertEqual(v["schema"], "vqa.vision/v1")
        self.assertEqual(v["params_sha256"], P.params_sha256())
        self.assertEqual(v["n_frames"], 24)
        rows = [canon.loads(l) for l in (out / "frames.jsonl").read_text().splitlines()]
        self.assertEqual([r["frame"] for r in rows], list(range(24)))
        self.assertEqual(rows[1]["global_mv"], [4, 0])
        self.assertEqual(rows[1]["cov_ppm"], sum(rows[1]["n"][2:]) * 1000000 // run.grid.N)
        b = (out / "tiles.bin").read_bytes()
        h = layout.parse_tile_header(b)
        self.assertEqual((h["n_frames"], h["N"], h["W"]), (24, run.grid.N, 256))
        self.assertEqual(len(b), layout.TILE_HDR_SIZE + 24 * run.grid.N * 32)
        layout.check_tile_records(b[layout.TILE_HDR_SIZE:])

    def test_determinism_3x(self):
        d = make_run("flicker")
        shas = set()
        for k in range(3):
            o = d / ("v%d" % k)
            pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"), None, None, o)
            shas.add(json.dumps(tree_sha(o), sort_keys=True).replace("v%d" % k, "v"))
        self.assertEqual(len(shas), 1)

    def test_flicker_found(self):
        d = make_run("flicker")
        run = pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"))
        self.assertTrue(any(r["n"][layout.CLS_ID["FLICKER"]] for r in run.frame_rows))
        self.assertTrue(any(rs for rs in run.regions))

    def test_clean_static_no_dirty(self):
        d = make_run("clean_static")
        run = pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"))
        self.assertEqual(max(r["cov_ppm"] for r in run.frame_rows), 0)

    def test_hints_neutral(self):
        d = make_run("small_overlap")
        src = lambda: pipeline.ManifestSource(d / "frames.manifest.json")
        hints = [{"i": i, "rid": "h:ui:x", "role": "ui", "bbox_px": [0, 0, 64, 64], "z": None} for i in range(24)]
        a = pipeline.analyze(src(), None, None)
        b = pipeline.analyze(src(), None, hints)
        for ta, tb in zip(a.tiles, b.tiles):
            self.assertTrue((ta["cls"] == tb["cls"]).all())
            self.assertTrue(((ta["flags"] & ~np.uint8(0x10)) == (tb["flags"] & ~np.uint8(0x10))).all())
        self.assertTrue(any((t["flags"] & 0x10).any() for t in b.tiles))

    def test_params_sha_in_output(self):
        d = make_run("clean_static")
        t = P.effective({"t0_q4": 40})
        run = pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"), t)
        self.assertEqual(run.vision_json()["params_sha256"], P.params_sha256(t))
        self.assertNotEqual(P.params_sha256(t), P.params_sha256())

    def test_frames_sha_changes_with_pixels(self):
        d = make_run("clean_static")
        a = pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"))
        raw = bytearray((d / "frames.rgb").read_bytes())
        raw[5] ^= 1
        (d / "frames.rgb").write_bytes(bytes(raw))
        with self.assertRaises(VqaError) as c:  # manifest sha no longer matches the decoded frame
            pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"))
        self.assertEqual(c.exception.code, "E_VC_FRAME_SHA")
        self.assertEqual(c.exception.rc, 3)
        self.assertTrue(a.frames_sha256)

    def test_refusal_leaves_out_dir(self):
        d = make_run("clean_static")
        out = d / "vision"
        out.mkdir()
        (out / "guard.txt").write_text("keep")
        (out / "tiles.bin").write_bytes(b"old")
        before = tree_sha(out)
        raw = bytearray((d / "frames.rgb").read_bytes())
        raw[-1] ^= 1
        (d / "frames.rgb").write_bytes(bytes(raw))
        with self.assertRaises(VqaError):
            pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"), None, None, out)
        self.assertEqual(tree_sha(out), before)

    def test_wrong_shape_frame(self):
        f = Frame(0, 0, 0, np.zeros((32, 32, 3), np.uint8))
        with self.assertRaises(VqaError) as c:
            pipeline.analyze(MemoryFrameSource([f], {"width": 64, "height": 48}))
        self.assertEqual(c.exception.code, "E_VC_FRAME_SIZE")

    def test_empty_source(self):
        with self.assertRaises(VqaError) as c:
            pipeline.analyze(MemoryFrameSource([], {"width": 64, "height": 48}))
        self.assertEqual(c.exception.rc, 2)

    def test_bad_params(self):
        d = make_run("clean_static")
        t = dict(P.PARAMS)
        t["NOPE_PX"] = 1
        with self.assertRaises(VqaError):
            pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"), t)


class TestCli(unittest.TestCase):
    def test_selftest(self):
        rc, o = run_cli(["selftest"])
        self.assertEqual((rc, o["code"]), (0, "OK"))
        self.assertEqual(len(o["detail"]["digests"]), 4)

    def test_analyze_cli(self):
        d = make_run("scroll")
        rc, o = run_cli(["analyze", str(d / "frames.manifest.json"), "--out", str(d / "vis")])
        self.assertEqual((rc, o["rc"], o["code"]), (0, 0, "OK"))
        self.assertTrue((d / "vis" / "vision.json").is_file())
        self.assertEqual(o["detail"]["n_frames"], 24)

    def test_usage_rc2(self):
        for argv in ([], ["nope"], ["analyze"], ["analyze", "a", "b"], ["analyze", "x.json", "--bad", "1"], ["selftest", "extra"]):
            rc, o = run_cli(argv)
            self.assertEqual((rc, o["code"]), (2, "E_USAGE"), argv)

    def test_missing_manifest_rc2(self):
        rc, o = run_cli(["analyze", "/nonexistent/m.json"])
        self.assertEqual((rc, o["code"]), (2, "E_IO"))

    def test_bad_profile_rc(self):
        d = make_run("clean_static")
        prof = d / "profile.json"
        prof.write_text('{"bogus":1}')
        rc, o = run_cli(["analyze", str(d / "frames.manifest.json"), "--profile", str(prof)])
        self.assertEqual((rc, o["code"]), (2, "E_VC_PARAM_UNKNOWN"))

    def test_corrupt_frame_rc3_and_out_untouched(self):
        d = make_run("clean_static")
        out = d / "vis"
        raw = bytearray((d / "frames.rgb").read_bytes())
        raw[0] ^= 255
        (d / "frames.rgb").write_bytes(bytes(raw))
        rc, o = run_cli(["analyze", str(d / "frames.manifest.json"), "--out", str(out)])
        self.assertEqual((rc, o["code"]), (3, "E_VC_FRAME_SHA"))
        self.assertFalse(out.exists())

    def test_never_rc1(self):
        for argv in ([], ["selftest"], ["pack"], ["cite-check"], ["analyze", "/x"]):
            self.assertNotEqual(run_cli(argv)[0], 1)

    def test_pack_cite_need_args(self):
        self.assertEqual(run_cli(["cite-check", "/x"])[1]["code"], "E_USAGE")
        self.assertEqual(run_cli(["pack"])[0], 2)

    def test_stdout_is_one_canonical_object(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["nope"])
        self.assertTrue(canon.is_canonical(out.getvalue()))
        self.assertEqual(out.getvalue().count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
