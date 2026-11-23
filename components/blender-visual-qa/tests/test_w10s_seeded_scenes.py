"""W10S lane S: seeded truth scenes so W10 seeds are independent trials (reports/W10X_DIAG.md 2-3).

bench_scenes --seed k jitters every bench (SplitMix64); --seed absent stays the golden scene of GOLDEN; render_bench
passes k on seed arms; a plan whose seeded scenes would coincide is refused before rendering; an all-zero B6 still probe
is refused by calibrate and the seeded B6 scene bears per-frame noise."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bpy_side"))
import bench_scenes as bsc  # noqa: E402
from blender_visual_qa.truth import calibrate, run_bench as rb  # noqa: E402
from tests.test_bpy_side import BLENDER  # noqa: E402

GOLDEN = "c0f9ea7"  # main before W10S: the --seed-absent scenes must stay these
SEEDS = (0, 1, 10, 11)  # 2 FIT + 2 GATE, the W10S probe plan
RES, FRAMES = (96, 54), 8
CPU = os.environ.get("BVQA_CPU_CAP", "15-19")

DRIVER = r'''
import bpy, hashlib, json, sys
argv = sys.argv[sys.argv.index("--") + 1:]
mod_dir, out_dir, seeds, w, h, nf = argv[0], argv[1], json.loads(argv[2]), int(argv[3]), int(argv[4]), int(argv[5])
sys.path.insert(0, mod_dir)
import bench_scenes as m

def rnd(v):
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, str):
        return v
    if isinstance(v, float):
        return round(v, 6)
    try:
        return [rnd(x) for x in v]
    except TypeError:
        return str(v)

def fcurves(idb):
    ad = getattr(idb, "animation_data", None)
    if not ad or not ad.action:
        return []
    return [[fc.data_path, fc.array_index, [[rnd(k.co[0]), rnd(k.co[1]), k.interpolation] for k in fc.keyframe_points]]
            for fc in ad.action.fcurves]

def state():
    sc = bpy.context.scene
    obs = []
    for o in sorted(sc.objects, key=lambda o: o.name):
        d = {"n": o.name, "t": o.type, "loc": rnd(o.location), "scl": rnd(o.scale), "rot": rnd(o.rotation_euler),
             "hide": o.hide_render, "pi": o.pass_index, "fc": fcurves(o)}
        if o.type == "LIGHT":
            d["light"] = [o.data.type, rnd(o.data.energy), fcurves(o.data)]
        if o.type == "CAMERA":
            d["cam"] = [o.data.type, rnd(o.data.ortho_scale), rnd(o.data.lens)]
        obs.append(d)
    mats = []
    for mt in sorted(bpy.data.materials, key=lambda x: x.name):
        nodes = [[n.bl_idname, [[i.name, rnd(i.default_value)] for i in n.inputs if hasattr(i, "default_value")]]
                 for n in sorted(mt.node_tree.nodes, key=lambda n: n.name)]
        mats.append([mt.name, mt.pass_index, nodes, fcurves(mt.node_tree)])
    marks = sorted([mk.name, mk.frame, mk.camera.name if mk.camera else None] for mk in sc.timeline_markers)
    return {"objects": obs, "materials": mats, "markers": marks, "camera": sc.camera.name}

rows = open(out_dir + "/rows.jsonl", "w")
for b in m.BENCHES:
    for k in seeds:
        out = "%s/%s_%s.blend" % (out_dir, b, k)
        if k is None:
            m.build(b, out, (w, h), nf, 4)
        else:
            m.build(b, out, (w, h), nf, 4, seed=k)
        sj = open(out[:-6] + ".scene.json", "rb").read()
        st = state()
        rows.write(json.dumps({"bench": b, "seed": k, "scene_sha": hashlib.sha256(sj).hexdigest(),
                                    "state_sha": hashlib.sha256(json.dumps(st, sort_keys=True).encode()).hexdigest(),
                                    "manifest": json.loads(sj), "state": st}, sort_keys=True) + "\n")
rows.close()
'''


def blender_lines(args, cwd, timeout=600):
    cmd = ["taskset", "-c", CPU, BLENDER, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "9"] + args
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise AssertionError("blender rc %d: %s" % (r.returncode, (r.stdout + r.stderr)[-2000:]))
    return r.stdout.splitlines()


def build_all(mod_dir, out_dir, seeds):
    drv = Path(out_dir) / "driver.py"
    drv.write_text(DRIVER)
    blender_lines(["--python", str(drv), "--", str(mod_dir), str(out_dir), json.dumps(list(seeds)),
                           str(RES[0]), str(RES[1]), str(FRAMES)], out_dir)
    rows = [json.loads(ln) for ln in (Path(out_dir) / "rows.jsonl").read_text().splitlines()]
    return {(r["bench"], r["seed"]): r for r in rows}


def golden_module(dst):
    """bench_scenes.py + decode_exr.py of GOLDEN into dst (None if the git objects are unavailable)."""
    for name in ("bench_scenes.py", "decode_exr.py"):
        r = subprocess.run(["git", "-C", str(ROOT), "show", "%s:bpy_side/%s" % (GOLDEN, name)], capture_output=True)
        if r.returncode != 0:
            return None
        (Path(dst) / name).write_bytes(r.stdout)
    return dst


@unittest.skipUnless(os.path.exists(BLENDER), "Blender not installed")
class SeededSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="w10s_")
        base = Path(cls.tmp.name)
        for sub in ("gold_mod", "gold", "new"):
            (base / sub).mkdir()
        cls.gold = None
        if golden_module(base / "gold_mod"):
            cls.gold = build_all(base / "gold_mod", base / "gold", [None])
        cls.new = build_all(ROOT / "bpy_side", base / "new", [None] + list(SEEDS))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_seed_absent_is_byte_identical_to_golden(self):
        if self.gold is None:
            self.skipTest("git object %s unavailable" % GOLDEN)
        for b in bsc.BENCHES:
            with self.subTest(bench=b):
                g, n = self.gold[(b, None)], self.new[(b, None)]
                self.assertEqual(n["scene_sha"], g["scene_sha"])
                self.assertEqual(n["state"], g["state"])

    def test_scene_sha_differs_across_seeds_for_every_bench(self):
        for b in bsc.BENCHES:
            with self.subTest(bench=b):
                scene = {self.new[(b, k)]["scene_sha"] for k in (None,) + SEEDS}
                state = {self.new[(b, k)]["state_sha"] for k in (None,) + SEEDS}
                self.assertEqual(len(scene), 1 + len(SEEDS))
                self.assertEqual(len(state), 1 + len(SEEDS))
                self.assertEqual(self.new[(b, SEEDS[0])]["manifest"]["jitter"]["prng"], "splitmix64")

    def test_seeded_scenes_keep_the_semantic_class(self):
        """Timeline, temporal/coverage/cut truth, seed mode and every keyed displacement (velocities) are the golden ones."""
        keep = ("animated", "temporal", "coverage", "cuts", "seed_mode", "frame_range", "res")
        for b in bsc.BENCHES:
            g = self.new[(b, None)]
            for k in SEEDS:
                with self.subTest(bench=b, seed=k):
                    s = self.new[(b, k)]
                    self.assertEqual({x: s["manifest"].get(x) for x in keep}, {x: g["manifest"].get(x) for x in keep})
                    names = {o["n"] for o in s["state"]["objects"]} - {"b6_lit", "b6_light"}
                    self.assertEqual(names, {o["n"] for o in g["state"]["objects"]})
                    self.assertEqual(s["manifest"]["scene_seed"], k)
                    for go in g["state"]["objects"]:
                        so = next(o for o in s["state"]["objects"] if o["n"] == go["n"])
                        self.assertEqual(len(so["fc"]), len(go["fc"]))
                        for gf, sf in zip(go["fc"], so["fc"]):
                            gk, sk = gf[2], sf[2]
                            self.assertEqual([p[0] for p in sk], [p[0] for p in gk])
                            if gf[0] == "location":
                                self.assertEqual([round(p[1] - sk[0][1], 5) for p in sk],
                                                 [round(p[1] - gk[0][1], 5) for p in gk])
                            else:
                                self.assertEqual([p[1] for p in sk], [p[1] for p in gk])

    def test_seeded_b6_still_probe_bears_per_frame_noise(self):
        """Golden B6 renders two identical frames under two per-frame seeds (degenerate probe); seeded B6 does not."""
        import numpy as np
        from visioncore import decode
        diffs = {}
        for k in (None, SEEDS[0]):
            blend = Path(self.tmp.name) / "new" / ("B6_%s.blend" % k)
            out = Path(self.tmp.name) / ("b6_render_%s" % k)
            out.mkdir()
            expr = ("import bpy;s=bpy.context.scene;s.cycles.device='CPU';s.cycles.samples=4;"
                    "bpy.app.handlers.render_pre.append(lambda sc,*a:setattr(sc.cycles,'seed',7919*sc.frame_current))")
            blender_lines([str(blend), "--python-expr", expr, "-o", str(out / "f_####"), "-F", "PNG", "-s", "1", "-e", "2",
                           "-a"], str(out))
            a, b = (decode.png(str(out / ("f_%04d.png" % f))).astype(np.int64) for f in (1, 2))
            diffs[k] = int(np.abs(a - b).sum())
        self.assertEqual(diffs[None], 0)
        self.assertGreater(diffs[SEEDS[0]], 0)


class PlumbingTests(unittest.TestCase):
    def test_scene_slot_twin_matches_bench_scenes(self):
        for b in rb.BENCHES:
            for k in list(range(200)) + [524287, (1 << 31) - 1]:
                self.assertEqual(rb.scene_slot(b, k), bsc.scene_slot(rb.BENCH_NO[b], k))
        self.assertEqual(bsc.BENCHES, rb.BENCHES)

    def test_scene_slot_is_injective_on_planned_seeds(self):
        for b in rb.BENCHES:
            self.assertEqual(len({bsc.scene_slot(rb.BENCH_NO[b], k) for k in range(169)}), 169)
        rb.check_scene_collisions(rb.BENCHES, list(range(0, 10)) + list(range(10, 40)))

    def test_fit_gate_scene_overlap_is_refused(self):
        with self.assertRaises(rb.BenchError) as ctx:
            rb.check_scene_collisions(["B6"], [0, 169])
        self.assertIn("scene collision", str(ctx.exception))
        with patch.object(rb, "BLENDER", sys.executable), self.assertRaises(rb.BenchError):
            rb.validate(rb.parse_args(["--out", "/nonexistent/w10s", "--benches", "B1", "--frames", "2",
                                       "--seeds", "fit:0,gate:169"]))

    def test_jitter_prng_is_documented_splitmix64(self):
        self.assertEqual(bsc.splitmix64(0)[1], 0xE220A8397B1DCDAF)  # reference SplitMix64 output for seed 0
        a, b = bsc.Jitter("B1", 3), bsc.Jitter("B1", 3)
        self.assertEqual([a.randint(-4, 4) for _ in range(32)], [b.randint(-4, 4) for _ in range(32)])
        self.assertNotEqual(bsc.Jitter("B1", 3).cam + bsc.Jitter("B1", 3).place("x"),
                            bsc.Jitter("B1", 4).cam + bsc.Jitter("B1", 4).place("x"))

    def _render_cmds(self, scene_seed):
        cmds = []
        with tempfile.TemporaryDirectory() as d:
            def fake(cmd, scratch, log, timeout=0):
                cmds.append([str(c) for c in cmd])
                if any(str(c).endswith("bench_scenes.py") for c in cmd):
                    (Path(scratch) / "B1.scene.json").write_text(json.dumps({"seed_mode": "fixed"}))
                return 0
            with patch.object(rb, "run_blender", side_effect=fake):
                rb.render_bench("B1", d, 32, 18, 2, 4, 7, *(() if scene_seed is None else (scene_seed,)))
        return next(c for c in cmds if c[-1].endswith("bench_scenes.py") or any(x.endswith("bench_scenes.py") for x in c))

    def test_render_bench_passes_the_scene_seed(self):
        cmd = self._render_cmds(7)
        self.assertEqual(cmd[cmd.index("--seed") + 1], "7")
        self.assertNotIn("--seed", self._render_cmds(None))

    def test_seed_arms_render_seeded_scenes_and_legacy_stays_golden(self):
        seen = []

        def spy(bench, root, w, h, nframes, spp, table, seed=0, analyze=True, scene_seed=None):
            seen.append((Path(root).parts[-2] if "seed_" in str(root) else "legacy", seed, scene_seed))
            raise rb.BenchError("E_SPY", "stop")
        for seeds in ("fit:0-1,gate:10-11", None):
            with tempfile.TemporaryDirectory() as d, patch.object(rb, "BLENDER", sys.executable), \
                    patch.object(rb, "run_one", side_effect=spy):
                argv = ["--out", d, "--benches", "B1", "--frames", "2"] + (["--seeds", seeds] if seeds else [])
                a = rb.parse_args(argv)
                benches, w, h = rb.validate(a)
                rb.run_tier(a, benches, w, h, 4, Path(d) / "spp_4")
        # FIT failures stop the tier before GATE renders; both arms share the seeded-scene branch of run_tier
        self.assertEqual(sorted(x for x in seen if x[0] != "legacy"), [("fit", 0, 0), ("fit", 1, 1)])
        self.assertEqual([x for x in seen if x[0] == "legacy"], [("legacy", 0, None)])


class StillProbeTests(unittest.TestCase):
    def _rows(self):
        rows = []
        for k in (0, 1, 10, 11):
            rows.append({"bench": "B6", "seed": k, "capture_sha256": "c%02d" % k, "t0_q4": 32})
        return rows

    def test_all_zero_still_probe_is_refused(self):
        sd = {"fit": [0, 1], "gate": [10, 11]}
        meta = {"seeds": [0, 1], "capture_sha256": ["c00", "c01"]}
        _, rep = calibrate.calibrate(self._rows(), [0] * 64, "blender", "r", "g", "src", True, sd, meta)
        self.assertIn("still_probe_degenerate", rep["split_violations"])
        self.assertEqual(rep["refusal"], "split_leak")
        _, ok = calibrate.calibrate(self._rows(), [0] * 63 + [5], "blender", "r", "g", "src", True, sd, meta)
        self.assertNotIn("still_probe_degenerate", ok["split_violations"])


if __name__ == "__main__":
    unittest.main()
