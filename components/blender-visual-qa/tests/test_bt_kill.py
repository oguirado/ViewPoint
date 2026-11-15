"""JW9-6/JW9b-4: every bt.* mutant is the BTRUTH 8 edit of the production code path, sha-bound, executed against a green
nominal oracle (rc 5, named check).

`python3 tests/test_bt_kill.py --oracle` is the oracle (rc 0 green / rc 5 a named check failed / rc 2 setup error).
The unittest copies blender_visual_qa/ + bpy_side/ and the PINNED engine source (git archive of config/vision.json
pin.commit: visioncore + vadapt) to a scratch dir, applies the manifest patch to the repo tree (`tree: repo`) or the engine
tree (`tree: engine`, pre-patch file sha bound), runs the oracle there with the scratch engine first on PYTHONPATH and
requires rc 5 whose failed checks include expect_fail. Control (unpatched copy, same pinned engine) must be rc 0, so no kill
is a setup error. Local analogues of the spec edits live under `unit_mutants` (ids bu.*) and are never counted as bt.*."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tests" / "golden" / "truth" / "bt_manifest.json"
VE = Path(os.environ.get("BVQA_VISION_ENGINE_SRC", os.path.expanduser("~/Documentos/vision-engine")))


def hunks_sha(hunks):
    return hashlib.sha256(json.dumps(hunks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def oracle_checks():
    import numpy as np
    from blender_visual_qa.truth import calibrate, labels as L, score
    from bpy_side import decode_exr, drive_common
    from tests.test_calibrate import good_rows, split_set, evidence_rows
    from tests.test_labels import frame
    from tests.test_labels_w9 import make_run
    from tests.test_score import synth, row
    c = {}
    def strong(t0):
        with tempfile.TemporaryDirectory() as d:
            g = make_run(d, anim=True)
            L.run(d, {"T0_Q4": t0}, os.path.join(d, "o"), np.full((2, g.N), 70, np.int64))
            with open(os.path.join(d, "o", "tiles_truth.bin"), "rb") as fh:
                _, arrs = L.parse_truth_tiles(fh.read(), g)
            return int((arrs[1]["tl"] == L.TL_ID["SHADE_STRONG"]).sum())
    c["shade_strong_label"] = strong(32) > 0 and strong(128) == 0
    c["hyst_band"] = L.thresholds({"T0_Q4": 100}) == (110, 90)
    def both(fit, gate):  # FIT arm carries full evidence too: a mutant gating on FIT seeds must differ only by its verdict
        r, sd_, m_ = split_set(fit, gate)
        return r + evidence_rows(sd_["fit"]), sd_, m_
    rows, sd, meta = both(good_rows(fp_resid=110), good_rows(fp_resid=110))
    p, rep = calibrate.calibrate(rows, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
    c["t0_fp_least"] = rep["T0_fp"] == 100
    c["t0_covers_fp"] = p is not None and rep["T0_Q4"] * 1100000 // 1000000 >= 110
    tl, cls, mv = synth()
    c["mv_exact_sign"] = score.summarize([row(tl, cls, mv, mv.copy())])["mv_exact_ppm"] == 1000000
    c["moved_class_ok"] = score.summarize([row(tl, cls, mv, mv.copy())])["tl"]["3"]["cls_ok_ppm"] == 1000000
    c["round_half_away"] = decode_exr._rnd([0.5, -0.5, 1.5, -1.5, 2.4]).tolist() == [1, -1, 2, -2, 2]
    a = np.arange(6, dtype=np.float32).reshape(3, 2)
    c["flip_rows"] = bool((decode_exr._flip(a) == a[::-1]).all())
    ob = frame((16, 16, 64, 48))["ob"]
    c["edge_ring_mask"] = int(L.edge_mask(ob, L.ring_from_filter(None)).sum()) > 0
    g = L.vgrid.Grid(96, 64)
    lab = L.label_frame(frame((16, 16, 48, 48)), frame((24, 16, 56, 48), mv=(8, 0)), g)
    c["frozen_cov_truth"] = L.frame_meta(2, lab)["cov_ppm"] > 0
    ev, sd2, meta2 = both(good_rows(), good_rows())
    for r in ev:
        if "c5_select" in r:
            r["c5_select"] = {"n": 100, "hit": 99}
    pc5, _ = calibrate.calibrate(ev, [4], "blender", "r", "g", "ab", seeds=sd2, still_meta=meta2)
    c["c5_refuses_99"] = pc5 is None
    c["c5_select_engine"], _DETAIL["c5"], _DETAIL["c5_failed_checks"] = c5_engine(both)
    c["b4f_frozen_label_golden"] = b4f_frozen_sha() == json.loads(MANIFEST.read_text())["golden"]["b4f_frozen_tiles_truth_sha256"]
    c.update(argon_checks())
    c["seed_no_additive_collision"] = drive_common.cycles_seed(1, 3, 5) != drive_common.cycles_seed(1, 4, 4)
    gate = good_rows(n=1)
    gate[0]["tl"]["0"]["dirty"] = 1
    rows, sd, meta = both(good_rows(n=1), gate)
    pg, _ = calibrate.calibrate(rows, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
    c["gate_fp_refused"] = pg is None
    return c


_DETAIL = {}


def c5_engine(both):
    """C5 end to end: engine tiles -> score.c5_select (visioncore.pyramid.Pyramid.select, B in 8/16/32) -> GATE rows ->
    calibrate. Nominal: n > 0, hit == n, profile written. 4x4 grid: Morton code == tile index == truth column."""
    import numpy as np
    from blender_visual_qa.truth import calibrate, score
    from tests.test_calibrate import good_rows
    g = score_grid(64, 64)
    n = 16
    dt = np.dtype([("morton", "<u4"), ("resid", "u1"), ("cls", "u1")])
    tl = np.zeros((2, n), np.int64)
    seq = []
    for f in range(2):
        t = np.zeros(n, dt)
        t["morton"] = np.arange(n)
        if f:
            for m in (0, 1, 2, 3, 5, 9, 14):
                t["resid"][m], t["cls"][m], tl[f, m] = 60, score.C_CHANGED, 4  # OCCLUDE, resid > R_SEL(100) = 22
        seq.append(t)
    c5 = score.c5_select(seq, g, tl, 100)
    rows, sd, meta = both(good_rows(), good_rows())
    for r in rows:
        if "c5_select" in r:
            r["c5_select"] = dict(c5)
    p, rep = calibrate.calibrate(rows, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
    failed = sorted(x["name"] for x in rep.get("checks", []) if x.get("status") != "PASS")
    return bool(c5["n"] > 0 and c5["hit"] == c5["n"] and p is not None), {k: int(v) for k, v in c5.items()}, failed


def argon_checks():
    """v12ar ADV_W9 surviving mutants bt.argon.01/05/08/10: nominal-green boundary checks on the production functions."""
    import numpy as np
    from blender_visual_qa.truth import calibrate, labels as L, run_bench as rb, score
    from tests.test_score import synth, row
    c = {}
    lab = {"tl": np.array([3, 0], np.uint8), "ob_major": np.array([1, 1])}
    L.apply_temporal(lab, 2, {"temporal": [{"name": "box", "kind": "frozen", "frames": [2, 2]}]}, {"box": 1})
    c["frozen_override_only_static"] = lab["tl"].tolist() == [3, L.TL_ID["FROZEN_T"]]
    iou = [{"bench": "B1", "seed": s, "s4_iou_ppm": v} for s, v in zip((1, 2, 3), (950000, 600000, 900000))]
    c["s4_iou_median_sorted"] = dict((n, v) for n, v, *_ in calibrate.required_checks({}, iou, [1, 2, 3]))["s4_iou_median_ppm"] == 900000
    tl = np.array([[0, 3], [0, 3]], np.uint8)
    cls = np.array([[0, 0], [score.C_CHANGED, score.C_MOVED]], np.uint8)
    mv = np.zeros((2, 2, 2), np.int64)
    c["dirty_precision_clean_fp"] = rb.bench_metrics(row(tl, cls, mv, mv.copy()), rb.confusion(tl, cls))["dirty_precision_ppm"] == 500000
    tl, cls, mv = synth()
    cls[:, 8] = score.C_FLICKER  # tl 8 SHADE_BAND is EXCLUDED: never temporal FP evidence
    c["temporal_fp_ignores_excluded"] = row(tl, cls, mv, mv.copy())["flicker_fp_tiles"] == 0
    return c


def score_grid(w, h):
    from blender_visual_qa.truth import labels as L
    return L.vgrid.Grid(w, h)


def b4f_frozen_sha():
    """B4f shape: a static object journaled `frozen` with zero moving coverage keeps FROZEN_T truth (BTRUTH 4)."""
    from blender_visual_qa.truth import labels as L
    from tests.test_labels_w9 import make_run
    with tempfile.TemporaryDirectory() as d:
        make_run(d)
        with open(os.path.join(d, "journal.json"), "w") as jf:
            json.dump({"temporal": [{"name": "box", "kind": "frozen", "frames": [2, 2]}]}, jf)
        return L.run(d, {"T0_Q4": 100}, os.path.join(d, "o"))["sha256"]


def oracle_main():
    import blender_visual_qa
    if os.environ.get("BT_EXPECT_ROOT") and not str(Path(blender_visual_qa.__file__).resolve()).startswith(os.environ["BT_EXPECT_ROOT"]):
        print(json.dumps({"setup_error": "oracle imported the live tree"}))
        return 2
    if os.environ.get("BT_EXPECT_ENGINE"):
        import visioncore.pyramid
        if not str(Path(visioncore.pyramid.__file__).resolve()).startswith(os.environ["BT_EXPECT_ENGINE"]):
            print(json.dumps({"setup_error": "oracle imported an unpinned engine"}))
            return 2
    try:
        checks = oracle_checks()
    except Exception as e:  # setup error, never a kill
        print(json.dumps({"setup_error": repr(e)}))
        return 2
    failed = sorted(k for k, v in checks.items() if not v)
    print(json.dumps({"detail": _DETAIL, "failed": failed, "n": len(checks)}, sort_keys=True))
    return 5 if failed else 0


def run_oracle(tree):
    eng = Path(tree) / "engine"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(eng), str(tree), str(ROOT)]), PYTHONDONTWRITEBYTECODE="1",
               BT_EXPECT_ROOT=str(tree), BT_EXPECT_ENGINE=str(eng.resolve()))
    p = subprocess.run([sys.executable, "-B", str(ROOT / "tests" / "test_bt_kill.py"), "--oracle"], cwd=str(tree), env=env,
                       capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        out = {"setup_error": p.stderr[-300:]}
    return p.returncode, out


def pinned_commit():
    return json.loads((ROOT / "config" / "vision.json").read_text())["pin"]["commit"]


def copy_tree(dst):
    """Repo packages + the pinned engine source (never the live engine checkout, which may be ahead of the pin)."""
    for pkg in ("blender_visual_qa", "bpy_side"):
        shutil.copytree(ROOT / pkg, Path(dst) / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    eng = Path(dst) / "engine"
    eng.mkdir()
    a = subprocess.run(["git", "-C", str(VE), "archive", "--format=tar", pinned_commit(), "visioncore", "vadapt"],
                       capture_output=True, timeout=60, check=True)
    subprocess.run(["tar", "-x", "-C", str(eng)], input=a.stdout, capture_output=True, timeout=60, check=True)


def mutated_tree(dst, entry):
    copy_tree(dst)
    f = Path(dst) / ("engine" if entry.get("tree") == "engine" else "") / entry["file"]
    if entry.get("file_sha256") and hashlib.sha256(f.read_bytes()).hexdigest() != entry["file_sha256"]:
        raise AssertionError("%s: pre-patch %s sha differs from the manifest" % (entry["id"], entry["file"]))
    src = f.read_text(encoding="utf-8")
    for h in entry["hunks"]:
        if src.count(h["old"]) != 1:
            raise AssertionError("%s: patch anchor occurs %d times in %s" % (entry["id"], src.count(h["old"]), entry["file"]))
        src = src.replace(h["old"], h["new"])
    f.write_text(src, encoding="utf-8")


class BtKillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.man = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_bound_and_complete(self):
        ids = [m["id"] for m in self.man["mutants"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(self.man["required_ids"]))
        self.assertEqual(len([i for i in ids if not i.startswith("bt.argon.")]), 13)  # BTRUTH 8 rows
        self.assertTrue({"bt.argon.01", "bt.argon.05", "bt.argon.08", "bt.argon.10"} <= set(ids))
        for m in self.man["mutants"]:
            self.assertEqual(hunks_sha(m["hunks"]), m["patch_sha256"], m["id"])
            self.assertTrue(m["expect_fail"])

        self.assertEqual(len({m["id"] for m in self.man["unit_mutants"]} & set(ids)), 0)
        for u in self.man["unit_mutants"]:
            self.assertTrue(u["id"].startswith("bu."), u["id"])
            self.assertEqual(hunks_sha(u["hunks"]), u["patch_sha256"], u["id"])

    def test_spec_edits_on_production_paths(self):  # JW9b-4: BTRUTH 8 edit, not a local analogue
        m = {x["id"]: x for x in self.man["mutants"]}
        sel, cov = m["bt.select.01"], m["bt.covtruth.01"]
        self.assertEqual((sel["tree"], sel["file"]), ("engine", "visioncore/pyramid.py"))
        self.assertEqual([h["new"] for h in sel["hunks"]], ["        self.last_visits = visits\n        return out[:-1]\n"])
        self.assertNotIn("calibrate", sel["file"])
        self.assertEqual(sel["expect_refusal"], ["c5_select_ppm"])
        self.assertEqual(cov["file"], "blender_visual_qa/truth/labels.py")
        self.assertIn("300000", cov["hunks"][0]["new"])
        self.assertNotIn("COV_TL = ()", cov["hunks"][0]["new"])
        a = subprocess.run(["git", "-C", str(VE), "show", "%s:%s" % (pinned_commit(), sel["file"])], capture_output=True,
                           timeout=60, check=True).stdout
        self.assertEqual(hashlib.sha256(a).hexdigest(), sel["file_sha256"])

    def test_control_green(self):
        with tempfile.TemporaryDirectory() as d:
            copy_tree(d)
            rc, out = run_oracle(d)
            self.assertEqual((rc, out.get("failed")), (0, []), out)
            self.assertGreater(out["detail"]["c5"]["n"], 0)
            self.assertEqual(out["detail"]["c5"]["hit"], out["detail"]["c5"]["n"])

    def test_every_bt_mutant_killed_rc5_named_check(self):
        for m in self.man["mutants"]:
            with self.subTest(m["id"]), tempfile.TemporaryDirectory() as d:
                mutated_tree(d, m)
                rc, out = run_oracle(d)
                self.assertNotIn("setup_error", out, m["id"])
                self.assertEqual(rc, 5, (m["id"], out))
                self.assertTrue(set(m["expect_fail"]) <= set(out["failed"]), (m["id"], out))
                if m.get("expect_refusal"):  # semantic C5 refusal by calibrate on the produced metric
                    self.assertLess(out["detail"]["c5"]["hit"], out["detail"]["c5"]["n"], out)
                    self.assertEqual(set(out["detail"]["c5_failed_checks"]) & set(m["expect_refusal"]), set(m["expect_refusal"]), out)

    def test_unit_analogues_killed(self):
        for m in self.man["unit_mutants"]:
            with self.subTest(m["id"]), tempfile.TemporaryDirectory() as d:
                mutated_tree(d, m)
                rc, out = run_oracle(d)
                self.assertNotIn("setup_error", out, m["id"])
                self.assertEqual(rc, 5, (m["id"], out))
                self.assertTrue(set(m["expect_fail"]) <= set(out["failed"]), (m["id"], out))


if __name__ == "__main__":
    if "--oracle" in sys.argv:
        sys.path.insert(0, os.getcwd())
        sys.exit(oracle_main())
    unittest.main()
