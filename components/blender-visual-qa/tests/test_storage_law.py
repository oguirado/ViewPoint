"""STORAGE LAW v1 (B1): run_bench frames live in a vadapt.ramws RAM workspace; the disk keeps evidence only.

A fake Blender writes PNG / NONE-EXR / truth .npy files where the real one would; the oracle (driver below, run in a
fresh interpreter on a scratch copy of the package) asserts: no frame file under --out, the workspace is gone, the RAM
peak is bounded by one EXR chunk plus the resident unit. bm.storage.* mutants of catalogue/mutants/bm_manifest.json
must make that oracle exit 5.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "catalogue" / "mutants" / "bm_manifest.json"
sys.path.insert(0, str(ROOT))

DRIVER = r'''
import json, os, sys
from pathlib import Path
import numpy as np
from blender_visual_qa.truth import run_bench as rb
from vadapt import ramws
EXR = 1 << 20
def fake(cmd, scratch, log, timeout=0):
    cmd = [str(c) for c in cmd]
    run = Path(scratch)
    if any(c.endswith("bench_scenes.py") for c in cmd):
        b = cmd[cmd.index("--bench") + 1]
        (run / ("%s.scene.json" % b)).write_text(json.dumps({"seed_mode": "fixed"}))
        Path(cmd[cmd.index("--out") + 1]).write_bytes(b"BLEND" * 100)
    elif "-a" in cmd:
        o, lo, hi = cmd[cmd.index("-o") + 1], int(cmd[cmd.index("-s") + 1]), int(cmd[cmd.index("-e") + 1])
        for f in range(lo, hi + 1):
            Path(o.replace("####", "%04d" % f)).write_bytes(rb.decode.encode_png(np.full((4, 4, 3), f, np.uint8)))
    elif any(c.endswith("passes.py") for c in cmd):
        lo, hi = (int(v) for v in cmd[cmd.index("--frames") + 1].split(":"))
        exr = Path(cmd[cmd.index("--exr-dir") + 1]) if "--exr-dir" in cmd else run / "truth" / "_exr"
        exr.mkdir(parents=True, exist_ok=True)
        (run / "truth").mkdir(exist_ok=True)
        for f in range(lo, hi + 1):
            (exr / ("Z_%04d.exr" % f)).write_bytes(b"\0" * EXR)
            np.save(run / "truth" / ("f%06d_z.npy" % f), np.full(16, f, np.uint32))
    return 0
def fake_analyze(bench, run, spp, table, t_wall=None, seed=0, clean=None):  # rows only; capture_sha is real-smoke evidence
    (Path(run) / "labels").mkdir(exist_ok=True)
    (Path(run) / "labels" / "tiles_truth.bin").write_bytes(b"T" * 64)
    return {"bench": bench, "seed": seed, "grid": "g", "frames": 16, "cpu_ms_per_frame": 0, "wall_s": 0,
            "row": {"bench": bench, "seed": seed, "capture_sha256": rb.capture_sha(run)},
            "metrics": {"tl": {}, "dirty_precision_ppm": None, "jitter_fp_ppm": None, "mv_exact_ppm": None,
                        "mv_err_p95_px": None, "class_precision_ppm": {}}}
rb.run_blender, rb.analyze_bench = fake, fake_analyze
out = Path(sys.argv[1])
rb.BLENDER = sys.executable
rc = rb.main(["--out", str(out), "--benches", "B1", "--frames", "16", "--run-id", sys.argv[2], "--campaign", "unittest"])
rec = json.loads((out / "ram_receipt.json").read_text())
rows = [json.loads(l) for l in (out / "rows.jsonl").read_text().splitlines()]
bad = []
if ramws.check_disk(out):
    bad.append("frames_on_disk:%r" % ramws.check_disk(out)[:3])
if (Path(ramws.ROOT) / "bvqa" / "unittest" / sys.argv[2]).exists():
    bad.append("workspace_left")
if not 0 < rec["ram_peak_bytes"] <= rb.CHUNK_FRAMES * EXR + (2 << 20):
    bad.append("ram_peak %d" % rec["ram_peak_bytes"])
if not rows:
    bad.append("rows_absent")
if "/dev/shm" in (out / "rows.jsonl").read_text():
    bad.append("workspace_path_in_rows")
if not (out / "B1" / "labels" / "tiles_truth.bin").is_file():
    bad.append("evidence_not_persisted")
print(json.dumps({"rc": rc, "bad": bad, "receipt": rec}, sort_keys=True))
sys.exit(5 if bad else 0)
'''


def run_oracle(pkg_root, out, run_id):
    wheel = json.loads((ROOT / "config" / "vision.json").read_text())["engine_path"]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(pkg_root), str(ROOT)]), PYTHONDONTWRITEBYTECODE="1",
               BVQA_TEST_ENGINE=str(ROOT / wheel))
    env.pop("BVQA_CPU_CAP", None)
    return subprocess.run([sys.executable, "-B", "-c", DRIVER, str(out), run_id], env=env, cwd=str(pkg_root),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)


def scratch_pkg(d, mutant=None):
    pkg = Path(d) / "pkg"
    shutil.copytree(ROOT / "blender_visual_qa", pkg / "blender_visual_qa",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "config", pkg / "config")
    if mutant is not None:
        f = pkg / mutant["file"]
        src = f.read_text()
        if src.count(mutant["find"]) != 1:
            raise AssertionError("mutant %s anchor not unique/absent" % mutant["id"])
        f.write_text(src.replace(mutant["find"], mutant["replace"]))
    return pkg


class StorageLawOracleTests(unittest.TestCase):
    def test_clean_flow_passes_oracle(self):
        with tempfile.TemporaryDirectory() as d:
            p = run_oracle(scratch_pkg(d), Path(d) / "out", "ut-clean-%d" % os.getpid())
            self.assertEqual(p.returncode, 0, p.stdout.decode()[-2000:])

    def test_storage_mutants_are_killed(self):
        mutants = [m for m in json.loads(MANIFEST.read_text())["mutants"] if m["id"].startswith("bm.storage.")]
        self.assertEqual({m["id"] for m in mutants}, {"bm.storage.chunk.01", "bm.storage.out.02"})
        for m in mutants:
            with self.subTest(m["id"]), tempfile.TemporaryDirectory() as d:
                p = run_oracle(scratch_pkg(d, m), Path(d) / "out", "ut-%s-%d" % (m["id"].replace(".", "-"), os.getpid()))
                self.assertEqual(p.returncode, m["expect_rc"], p.stdout.decode()[-2000:])


class StorageLawUnitTests(unittest.TestCase):
    def setUp(self):
        from blender_visual_qa.truth import run_bench
        self.rb = run_bench

    def test_persist_unit_copies_evidence_only_and_trims_logs(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "s", Path(d) / "o"
            for rel, data in (("render/frame_0001.png", b"p"), ("truth/f000001_z.npy", b"n"), ("vision/tiles.bin", b"t"),
                              ("_exr/Z_0001.exr", b"e"), ("labels/tiles_truth.bin", b"k"), ("truth/frames.jsonl", b"{}"),
                              ("blender.log", b"x" * (self.rb.LOG_CAP + 10)), ("B1.blend", b"b"), ("xdg/a.json", b"{}")):
                (src / rel).parent.mkdir(parents=True, exist_ok=True)
                (src / rel).write_bytes(data)
            self.rb.persist_unit(src, dst)
            got = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file())
            self.assertEqual(got, ["blender.log", "labels/tiles_truth.bin", "truth/frames.jsonl"])
            self.assertEqual((dst / "blender.log").stat().st_size, self.rb.LOG_CAP)

    def test_quota_flag_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            for argv, code in ((["--ram-quota-mb", "9000"], "E_RAM_QUOTA"), (["--keep-frames", "300"], "E_BUDGET")):
                with self.subTest(argv=argv):
                    a = self.rb.parse_args(["--out", d, "--benches", "B6", "--frames", "2"] + argv)
                    old = self.rb.BLENDER
                    self.rb.BLENDER = sys.executable
                    try:
                        with self.assertRaises(self.rb.BenchError) as cm:
                            self.rb.validate(a)
                    finally:
                        self.rb.BLENDER = old
                    self.assertEqual(cm.exception.code, code)

    def test_decode_worker_cpu_list(self):
        from blender_visual_qa.truth import decode_worker as dw
        self.assertEqual(dw.parse_cpus("5-9,12"), frozenset({5, 6, 7, 8, 9, 12}))
        with self.assertRaises(ValueError):
            dw.parse_cpus("9-5")

    def test_gates_ramws_pin_and_registration(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import gates
        self.assertEqual(gates.g_ramws_pin(ROOT), [])
        self.assertTrue({"g_disk_frames", "g_disk_size", "g_ramws_pin"} <= set(gates.GATES))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(ROOT / "config", root / "config")
            config_path = root / "config" / "vision.json"
            cfg = json.loads(config_path.read_text())
            wheel = root / cfg["engine_path"]
            wheel.parent.mkdir(parents=True)
            shutil.copy2(ROOT / cfg["engine_path"], wheel)
            self.assertEqual(gates.g_ramws_pin(root), [])
            cfg["pin"]["wheel_sha256"] = "0" * 64
            config_path.write_text(json.dumps(cfg))
            self.assertEqual(gates.g_ramws_pin(root), ["wheel_sha_mismatch"])

    def test_disk_frames_gate_flags_frames_and_leftovers(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import gates
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            out.mkdir()
            (out / "rows.jsonl").write_text('{"p":"/dev/shm/x"}\n')
            fake = {"rc": 5, "log": "", "out": out, "since": 0, "run_id": "x", "disk": ["room/f.png"], "ws_left": True,
                    "procs": ["1"], "receipt": {"ram_peak_bytes": 10, "quota": 5, "fstype": "ext4"},
                    "report": {"failures": []}}
            gates._SMOKE[Path(d)] = fake
            got = gates.g_disk_frames(Path(d))
            for want in ("frame_on_disk:room/f.png", "ramws_not_released", "ram_peak_out_of_quota", "ramws_not_tmpfs",
                         "procs_alive:1", "workspace_path_in_rows"):
                self.assertIn(want, got)
            gates._SMOKE.pop(Path(d))


if __name__ == "__main__":
    unittest.main()
