"""Tests that run bpy_side/*.py inside Blender 4.0.2 (skipped if /usr/bin/blender is missing)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

BLENDER = "/usr/bin/blender"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BPY = os.path.join(ROOT, "bpy_side")
RES = "160x90"


def jload(path):
    with open(path) as fh:
        return json.load(fh)


def jlines(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def blender(args, cwd, timeout=240):
    cmd = [BLENDER, "-b", "--factory-startup", "-noaudio"] + args
    if shutil.which("taskset"):
        cmd = ["taskset", "-c", "15-19"] + cmd
    env = dict(os.environ, HOME=cwd, XDG_CONFIG_HOME=cwd, XDG_CACHE_HOME=cwd, TZ="UTC", LC_ALL="C")
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def build(bench, d, extra=()):
    out = os.path.join(d, bench + ".blend")
    r = blender(["--python", os.path.join(BPY, "bench_scenes.py"), "--", "--bench", bench, "--out", out,
                 "--res", RES, "--frames", "4"] + list(extra), d)
    assert "BENCH_OK" in r.stdout, r.stdout[-800:] + r.stderr[-800:]
    return out, out[:-6] + ".scene.json"


def truth(blend, scene_json, run, frames="1:3", extra=(), pre=()):
    return blender([blend] + list(pre) + ["--python", os.path.join(BPY, "passes.py"), "--", "--run", run,
                    "--frames", frames, "--scene-json", scene_json] + list(extra), os.path.dirname(run))


@unittest.skipUnless(os.path.exists(BLENDER), "Blender not installed")
class BpySideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bvqa_t1_")
        cls.bs, cls.bs_json = build("BS", cls.tmp)
        cls.run_bs = os.path.join(cls.tmp, "run_bs")
        r = truth(cls.bs, cls.bs_json, cls.run_bs)
        assert r.returncode == 0 and "TRUTH_OK" in r.stdout, r.stdout[-600:] + r.stderr[-600:]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def npy(self, name, run=None):
        return np.load(os.path.join(run or self.run_bs, "truth", name))

    def test_sign_rigid_motion(self):  # T-TR-SIGN: +3 px x, +2 px up => (48, -32) q4 in image coords
        ob, mv = self.npy("f000002.ob.npy"), self.npy("f000002.mv.npy")
        m = ob == 1
        self.assertGreater(m.sum(), 100)
        self.assertEqual(np.unique(mv[m].reshape(-1, 2), axis=0).tolist(), [[48, -32]])
        self.assertTrue(np.all(mv[ob == 0] == 0))

    def test_yflip_top_left(self):  # T-TR-YFLIP
        ob = self.npy("f000001.ob.npy")
        ys, xs = np.nonzero(ob == 1)
        self.assertLess(ys.mean(), 90 / 2)
        self.assertLess(xs.mean(), 160 / 2)

    def test_quant_dtypes_and_manifest_subset(self):  # T-TR-QUANT
        manifest = jload(self.bs_json)
        valid = {o["pass_index"] for o in manifest["objects"]} | {0}
        ob = self.npy("f000001.ob.npy")
        self.assertEqual(ob.dtype, np.uint16)
        self.assertTrue(set(np.unique(ob).tolist()) <= valid)
        z = self.npy("f000001.z.npy")
        self.assertEqual(z.dtype, np.uint32)
        self.assertEqual(int(z[ob == 1][0]), 9500)  # camera z 10 - box top 0.5
        self.assertTrue(np.all(z[ob == 0] == 0xFFFFFFFF))
        self.assertEqual(self.npy("f000001.n.npy").dtype, np.int8)
        self.assertEqual(self.npy("f000001.uv.npy").dtype, np.uint16)
        self.assertEqual(self.npy("f000001.ma.npy").shape, (90, 160))

    def test_frames_jsonl_and_exr_cleanup(self):
        lines = jlines(os.path.join(self.run_bs, "truth", "frames.jsonl"))
        self.assertEqual([l["render_index"] for l in lines], [1, 2, 3])
        l = lines[0]
        self.assertEqual((l["schema"], l["engine"], l["truth_spp"], l["truth_seed"], l["motion_blur"]),
                         ("bvqa.truth-frame/v1", "CYCLES", 1, 0, False))
        self.assertEqual((l["width"], l["height"]), (160, 90))
        self.assertEqual(set(l["files"]), {"ob", "ma", "mv", "z", "n", "uv"})
        self.assertEqual(len(l["scene_sha256"]), 64)
        left = sorted(os.listdir(os.path.join(self.run_bs, "truth")))
        self.assertFalse([f for f in left if f.endswith(".exr") or f.startswith("_exr")])

    def test_determinism_two_runs(self):  # T-TR-DET (repeat + threads 1 vs 4)
        shas = []
        for threads in ("1", "4"):
            run = os.path.join(self.tmp, "det_t" + threads)
            r = truth(self.bs, self.bs_json, run, "1:2", ["--threads", threads, "--determinism-check"])
            self.assertEqual(r.returncode, 0, r.stdout[-500:] + r.stderr[-500:])
            self.assertTrue(jload(os.path.join(run, "truth", "determinism.json"))["identical"])
            shas.append([l["files"] for l in jlines(os.path.join(run, "truth", "frames.jsonl"))])
        self.assertEqual(shas[0], shas[1])

    def test_motion_blur_refused_nothing_written(self):  # T-TR-MB
        blend, sj = build("B6", self.tmp, ["--motion-blur"])
        run = os.path.join(self.tmp, "run_mb")
        r = truth(blend, sj, run)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stderr.strip().splitlines()[-1])["error"], "E_TRUTH_MB")
        self.assertFalse(os.path.exists(run))

    def test_eevee_refused(self):  # T-TR-EEVEE
        run = os.path.join(self.tmp, "run_ee")
        r = truth(self.bs, self.bs_json, run, "1:1", pre=["-E", "BLENDER_EEVEE"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("E_TRUTH_ENGINE", r.stderr)
        self.assertFalse(os.path.exists(run))

    def test_bad_frame_range_rejected(self):
        run = os.path.join(self.tmp, "run_bad")
        r = truth(self.bs, self.bs_json, run, "1:9")
        self.assertEqual(r.returncode, 2)
        self.assertIn("E_TRUTH_ARGS", r.stderr)

    def test_all_benches_build_with_bijective_pass_index(self):
        for b in ["B1", "B2", "B3", "B4", "B5", "B5o", "B6", "B7"]:
            with self.subTest(bench=b):
                d = os.path.join(self.tmp, "bench_" + b)
                os.makedirs(d)
                _, sj = build(b, d)
                m = jload(sj)
                self.assertEqual(m["schema"], "bvqa.scene/v1")
                for key in ("objects", "materials"):
                    idx = [o["pass_index"] for o in m[key]]
                    names = [o["name"] for o in m[key]]
                    self.assertEqual(idx, list(range(1, len(idx) + 1)))
                    self.assertEqual(names, sorted(names))

    def test_static_bench_has_zero_motion(self):  # feeds T-TR-STATIC
        blend, sj = build("B6", self.tmp)
        run = os.path.join(self.tmp, "run_b6")
        r = truth(blend, sj, run, "1:3")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        for f in (2, 3):
            self.assertTrue(np.all(self.npy("f%06d.mv.npy" % f, run) == 0))
            np.testing.assert_array_equal(self.npy("f%06d.ob.npy" % f, run), self.npy("f000001.ob.npy", run))

    def test_occlusion_visibility_changes(self):  # B2 hide_render toggles ids
        blend, sj = build("B2", self.tmp)
        run = os.path.join(self.tmp, "run_b2")
        r = truth(blend, sj, run, "1:4")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        a, b = (set(np.unique(self.npy("f%06d.ob.npy" % f, run)).tolist()) for f in (1, 4))
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
