"""W9 S1: real grid, pass-index ranking, new benches, EXR codec/dir/decode switches, frames.jsonl provenance."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

from tests.test_bpy_side import BLENDER, BPY, ROOT, blender, build, jlines, jload, truth

sys.path.insert(0, BPY)
import bench_scenes as bsc  # noqa: E402
import decode_exr as dx  # noqa: E402
import passes  # noqa: E402

VE = os.environ.get("BVQA_VISION_ENGINE_SRC", os.path.expanduser("~/Documentos/vision-engine"))


def ob(name, typ, idx):
    return types.SimpleNamespace(name=name, type=typ, pass_index=idx)


class PureTests(unittest.TestCase):
    def test_grid_ident_matches_visioncore(self):  # IMPL-GAP-1 (no visioncore inside Blender: pure-int mirror)
        for w, h in ((160, 90), (640, 360), (1280, 720), (1920, 1080), (1024, 1024), (320, 180)):
            r = subprocess.run([sys.executable, "-I", "-c", "import sys;sys.path.insert(0,%r);"
                                "from visioncore import layout;print(layout.grid_ident(%d,%d))" % (VE, w, h)],
                               capture_output=True, text=True)
            if r.returncode:
                self.skipTest("vision-engine not importable")
            self.assertEqual(dx.grid_ident(w, h), r.stdout.strip())

    def test_grid_ident_refuses_bad_size(self):
        with self.assertRaises(dx.TruthError) as c:
            dx.grid_ident(0, 10)
        self.assertEqual(c.exception.name, "E_TRUTH_ARGS")

    def test_resolve_grid_never_null_and_mismatch_refused(self):
        sc = types.SimpleNamespace(render=types.SimpleNamespace(resolution_x=160, resolution_y=90))
        self.assertEqual(passes.resolve_grid(sc, types.SimpleNamespace(grid=None)), "grid:160x90:s16")
        with self.assertRaises(dx.TruthError) as c:
            passes.resolve_grid(sc, types.SimpleNamespace(grid="grid:640x360:s16"))
        self.assertEqual(c.exception.name, "E_TRUTH_ARGS")

    def test_rank_types_cover_curve_surface_meta_font_hidden(self):  # IMPL-GAP-10
        objs = [ob("a_mesh", "MESH", 0), ob("b_curve", "CURVE", 0), ob("c_surf", "SURFACE", 0),
                ob("d_meta", "META", 0), ob("e_font", "FONT", 0), ob("f_cam", "CAMERA", 0)]
        scene = types.SimpleNamespace(objects=objs)
        bpy = types.SimpleNamespace(data=types.SimpleNamespace(materials=[]))
        got, _ = bsc.assign_pass_index(bpy, scene)
        self.assertEqual([o.name for o in got], ["a_mesh", "b_curve", "c_surf", "d_meta", "e_font"])
        self.assertEqual([o.pass_index for o in got], [1, 2, 3, 4, 5])
        self.assertEqual(objs[-1].pass_index, 0)
        valid, _ = passes.valid_indices(scene, materials=[])
        self.assertEqual(valid, {1, 2, 3, 4, 5})  # the camera is not a ranked type

    def test_bench_list_and_defaults(self):
        for b in ("B1x", "B4f", "B4n", "B8"):
            self.assertIn(b, bsc.BENCHES)
        self.assertEqual(bsc.DEFAULT_FRAMES["B4n"], 32)

    def test_pending_merge_by_frame(self):
        d = tempfile.mkdtemp(prefix="bvqa_pend_")
        try:
            p = os.path.join(d, "pending.jsonl")
            passes.write_pending(p, [{"frame": 2, "x": 1}, {"frame": 1, "x": 1}])
            passes.write_pending(p, [{"frame": 2, "x": 2}, {"frame": 3, "x": 3}])
            rows = jlines(p)
            self.assertEqual([(r["frame"], r["x"]) for r in rows], [(1, 1), (2, 2), (3, 3)])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_blend_in_git(self):  # every bench is procedural
        r = subprocess.run(["git", "ls-files", "*.blend", "*.blend1"], cwd=ROOT, capture_output=True, text=True)
        tracked = [f for f in r.stdout.split() if not f.startswith("examples/")]  # examples/cube3.blend predates W9
        self.assertEqual(tracked, [])

    def test_seg_bounds_partition(self):
        for n in (8, 12, 60):  # below ~6 frames the segments may overlap by design
            stop = max(2, n // 4)
            segs = bsc.seg_bounds(n, stop)
            self.assertEqual(segs[0][0], stop + 1)
            self.assertEqual(segs[-1][1], n)
            for (a, b), (a2, _) in zip(segs, segs[1:]):
                self.assertEqual(a2, b + 1)


@unittest.skipUnless(os.path.exists(BLENDER), "Blender not installed")
class BlenderW9Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bvqa_w9_")
        cls.bs, cls.bs_json = build("BS", cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_truth(self, name, frames="1:2", extra=()):
        run = os.path.join(self.tmp, name)
        r = truth(self.bs, self.bs_json, run, frames, extra)
        return run, r

    def test_grid_written_and_provenance(self):  # IMPL-GAP-1 + FC-CBLD-6
        run, r = self.run_truth("run_grid", "1:2", ["--threads", "2"])
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        for l in jlines(os.path.join(run, "truth", "frames.jsonl")):
            self.assertEqual(l["grid"], "grid:160x90:s16")
            self.assertEqual((l["cycles_device"], l["threads"], l["exr_codec"]), ("CPU", 2, "ZIP"))
            self.assertRegex(l["blender_version"], r"^\d+\.\d+")

    def test_grid_mismatch_refused_nothing_decoded(self):
        run, r = self.run_truth("run_gridbad", "1:1", ["--grid", "grid:640x360:s16"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("E_TRUTH_ARGS", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(run, "truth", "frames.jsonl")))

    def test_external_decode_pending_and_nonecodec_sha_equal(self):  # FC-CBLD-1
        run_a, ra = self.run_truth("run_inb", "1:2")
        shm = tempfile.mkdtemp(prefix="bvqa_w9_exr_", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        try:
            run_b, rb = self.run_truth("run_ext", "1:2", ["--decode", "external", "--exr-codec", "NONE", "--exr-dir", shm])
            self.assertEqual((ra.returncode, rb.returncode), (0, 0), rb.stderr[-400:])
            pend = jlines(os.path.join(run_b, "truth", "pending.jsonl"))
            self.assertEqual([p["frame"] for p in pend], [1, 2])
            p0 = pend[0]
            self.assertEqual((p0["schema"], p0["exr_codec"], p0["with_uv"]), ("bvqa.truth-pending/v1", "NONE", True))
            self.assertEqual(set(p0["passes"]), {"ob", "ma", "mv", "z", "n", "uv"})
            for path in p0["passes"].values():
                self.assertTrue(path.startswith(shm) and os.path.getsize(path) > 0, path)
            self.assertEqual(p0["frame_line"]["files"], {})
            self.assertEqual(p0["frame_line"]["grid"], "grid:160x90:s16")
            self.assertEqual(p0["valid_ob"], [1])
            self.assertFalse(os.path.exists(os.path.join(run_b, "truth", "frames.jsonl")))
            # the in-Blender NONE decode must equal the ZIP decode (codec is lossless)
            run_c, rc = self.run_truth("run_none", "1:2", ["--exr-codec", "NONE"])
            self.assertEqual(rc.returncode, 0, rc.stderr[-400:])
            fa = [l["files"] for l in jlines(os.path.join(run_a, "truth", "frames.jsonl"))]
            fc = [l["files"] for l in jlines(os.path.join(run_c, "truth", "frames.jsonl"))]
            self.assertEqual(fa, fc)
        finally:
            shutil.rmtree(shm, ignore_errors=True)

    def test_inblender_default_has_no_pending(self):
        run, r = self.run_truth("run_nopend", "1:1")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(run, "truth", "pending.jsonl")))

    def test_beauty_sha_equal_with_truth_on_off(self):  # G-T4: truth never touches the beauty path
        def beauty(tag):
            out = os.path.join(self.tmp, "beauty_" + tag)
            os.makedirs(out, exist_ok=True)
            r = blender([self.bs, "-o", os.path.join(out, "f_####"), "-F", "PNG", "-s", "2", "-e", "2", "-a"], out)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            with open(os.path.join(out, "f_0002.png"), "rb") as fh:
                data = fh.read()
            idat, i = b"", 8  # compare pixel data only: the PNG tEXt chunk carries a render timestamp
            while i < len(data):
                n = int.from_bytes(data[i:i + 4], "big")
                if data[i + 4:i + 8] == b"IDAT":
                    idat += data[i + 8:i + 8 + n]
                i += 12 + n
            return idat
        before = beauty("off")
        _, r = self.run_truth("run_g4", "1:2")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(before, beauty("on"))

    def build_n(self, name, frames):
        d = os.path.join(self.tmp, "bn_%s_%d" % (name, frames))
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, name + ".blend")
        r = blender(["--python", os.path.join(BPY, "bench_scenes.py"), "--", "--bench", name, "--out", out,
                     "--res", "160x90", "--frames", str(frames)], d)
        self.assertIn("BENCH_OK", r.stdout, r.stdout[-500:] + r.stderr[-500:])
        return out, jload(out[:-6] + ".scene.json")

    def test_b4_temporal_journal_reaches_60(self):
        _, m = self.build_n("B4", 60)
        self.assertEqual(m["frame_range"], [1, 60])
        self.assertEqual(max(a["frame"] for a in m["animated"]), 60)
        self.assertTrue(all(t["frames"][1] == 60 for t in m["temporal"] if t["kind"] in ("flicker", "drift")))
        frames = {a["frame"] for a in m["animated"] if a["name"] == "b4_ramp0"}
        self.assertEqual(frames, set(range(2, 61)))

    def test_b1x_fast_boxes(self):
        _, m = self.build_n("B1x", 4)
        self.assertEqual([o["name"] for o in m["objects"]], ["b1x_box48", "b1x_box64", "b1x_box80"])

    def test_b4f_coverage_strata_and_freeze(self):
        _, m = self.build_n("B4f", 12)
        self.assertEqual([c["ppm"] for c in m["coverage"]], [30000, 80000, 150000])
        cov = m["coverage"]
        self.assertEqual(cov[0]["frames"][0], 4)  # stop = 12 // 4 = 3
        self.assertEqual(cov[-1]["frames"][1], 12)
        self.assertEqual([t["kind"] for t in m["temporal"]], ["frozen"])
        self.assertEqual(m["temporal"][0]["frames"], [4, 12])

    def test_b4n_static_noise_control(self):
        _, m = self.build_n("B4n", 32)
        self.assertEqual(m["seed_mode"], "per_frame")
        self.assertEqual((m["animated"], m["temporal"]), ([], []))
        self.assertEqual(m["frame_range"], [1, 32])

    def test_b8_cuts_and_marker_bound_camera(self):
        blend, m = self.build_n("B8", 8)
        self.assertEqual(m["cuts"], [{"frame": 4, "kind": "full"}, {"frame": 6, "kind": "half"}])
        expr = ("import bpy;s=bpy.context.scene;r=[]\nfor f in (1,3,4,8):\n s.frame_set(f);r.append(s.camera.name)\n"
                "print('CAMS',r)")
        r = blender([blend, "--python-expr", expr], os.path.dirname(blend))
        self.assertIn("CAMS ['cam_a', 'cam_a', 'cam_b', 'cam_b']", r.stdout, r.stdout[-400:] + r.stderr[-400:])

    def test_new_benches_have_bijective_pass_index(self):
        for b in ("B1x", "B4f", "B4n", "B8"):
            with self.subTest(bench=b):
                _, m = self.build_n(b, 8)
                for key in ("objects", "materials"):
                    idx = [o["pass_index"] for o in m[key]]
                    self.assertEqual(idx, list(range(1, len(idx) + 1)))

    def test_new_benches_render_truth(self):  # camera cut + hidden movers must still decode against the manifest
        for b in ("B4f", "B8"):
            with self.subTest(bench=b):
                blend, m = self.build_n(b, 8)
                run = os.path.join(self.tmp, "run_" + b)
                r = truth(blend, blend[:-6] + ".scene.json", run, "1:8")
                self.assertEqual(r.returncode, 0, r.stdout[-300:] + r.stderr[-500:])


class PureDecoderBoundary(unittest.TestCase):
    """BINT9 'Pure decoder exception': decode_exr.py has no bpy import at any depth (gate's own AST check)."""

    def gates(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("w9d_gates", os.path.join(ROOT, "tools/gates.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_real_tree_boundary_gate_accepts_pure_decoder(self):
        self.assertEqual(self.gates().g_bpy_boundary(__import__("pathlib").Path(ROOT)), [])

    def test_bpy_import_added_to_decoder_fails_gate(self):
        import pathlib
        g = self.gates()
        base = pathlib.Path(ROOT, "bpy_side/decode_exr.py").read_text()
        scratch = pathlib.Path(ROOT, "build/room_D")
        scratch.mkdir(parents=True, exist_ok=True)
        for extra in ("import bpy\n", "def f():\n    import bpy\n"):
            with tempfile.TemporaryDirectory(dir=str(scratch)) as d:
                root = pathlib.Path(d)
                (root / "bpy_side").mkdir()
                (root / "blender_visual_qa/truth").mkdir(parents=True)
                (root / "bpy_side/q.py").write_text("")
                (root / "bpy_side/canon.py").write_text("")
                (root / "bpy_side/decode_exr.py").write_text(base + "\n" + extra)
                (root / "blender_visual_qa/truth/decode_worker.py").write_text("from bpy_side import decode_exr as dx\n")
                self.assertTrue(any("impure_decoder" in f for f in g.g_bpy_boundary(root)), extra)

    def test_loader_uses_caller_bpy_or_fails_closed(self):
        path = os.path.join(BPY, "x.exr")
        saved = sys.modules.pop("bpy", None)
        try:
            with self.assertRaises(dx.TruthError):
                dx.load_exr_rgba(path)
        finally:
            if saved is not None:
                sys.modules["bpy"] = saved
        import numpy as np
        px = np.arange(2 * 3 * 4, dtype=np.float32)

        class Img:
            size = (3, 2)
            colorspace_settings = types.SimpleNamespace(name="")
            alpha_mode = ""
            pixels = types.SimpleNamespace(foreach_get=lambda buf: buf.__setitem__(slice(None), px))
        fake = types.SimpleNamespace(data=types.SimpleNamespace(
            images=types.SimpleNamespace(load=lambda p, check_existing=False: Img(), remove=lambda i: None)))
        sys.modules["bpy"] = fake
        try:
            out = dx.load_exr_rgba(path)
        finally:
            sys.modules.pop("bpy", None)
            if saved is not None:
                sys.modules["bpy"] = saved
        self.assertEqual(out.shape, (2, 3, 4))
        self.assertTrue(np.array_equal(out.reshape(-1), px))


if __name__ == "__main__":
    unittest.main()
