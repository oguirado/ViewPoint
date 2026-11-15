"""O3: every SD-expected bm.* mutant is killed by its named bo.* check on the tiny base scene (no render needed).

Needs /usr/bin/blender and O1's bpy_side/{apply_mutant,scene_checks}.py; skips cleanly otherwise.
Runs: taskset -c 15-19, one Blender at a time, scratch in build/room_O3_tests.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLENDER = "/usr/bin/blender"
AD = os.path.join(ROOT, "adapters", "example_scene")
APPLY = os.path.join(ROOT, "bpy_side", "apply_mutant.py")
CHECKS = os.path.join(ROOT, "bpy_side", "scene_checks.py")
SCRATCH = os.path.join(ROOT, "build", "room_O3_tests")
TAGS = {"Subject": "closed|subject|animated", "Static": "closed|static", "TwoShell": "closed", "Textured": "open"}


def run_blender(env_dir, args, blend=None, timeout=120):
    cmd = ["taskset", "-c", "15-19", BLENDER, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "9"]
    if blend:
        cmd.append(blend)
    cmd += args
    env = {"PATH": "/usr/bin:/bin", "HOME": env_dir, "XDG_CONFIG_HOME": env_dir + "/c", "XDG_CACHE_HOME": env_dir + "/k",
           "XDG_DATA_HOME": env_dir + "/d", "TZ": "UTC", "LC_ALL": "C", "PYTHONNOUSERSITE": "1", "OMP_NUM_THREADS": "2"}
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


def rows_of(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


@unittest.skipUnless(os.path.exists(BLENDER), "blender missing")
@unittest.skipUnless(os.path.exists(APPLY) and os.path.exists(CHECKS), "lane O1 bpy_side oracles not present")
class MutantKillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(SCRATCH, exist_ok=True)
        cls.tmp = tempfile.mkdtemp(prefix="k_", dir=SCRATCH)
        cls.base = os.path.join(cls.tmp, "base.blend")
        r = run_blender(cls.tmp, ["--python", os.path.join(AD, "make_base.py"), "--", "--out", cls.base])
        if r.returncode != 0 or not os.path.exists(cls.base):
            raise RuntimeError("base build failed: " + r.stderr[-400:])
        cls.scene = os.path.join(cls.tmp, "scene.manifest.json")
        with open(cls.scene, "w") as fh:
            json.dump({"schema": "bvqa.scene/v1", "budgets": {"tris_object": 200000, "tris_scene": 1000000},
                       "objects": [{"name": n, "tags": t.split("|")} for n, t in sorted(TAGS.items())]}, fh)
        cls.journal = os.path.join(cls.tmp, "journal.json")
        with open(cls.journal, "w") as fh:
            json.dump({"schema": "bvqa.journal/v1", "frames": {"list": [1, 2, 3, 4]}, "camera": "Cam", "scene": "Scene", "view_layer": "ViewLayer"}, fh)
        cls.clean = cls.checks(cls.base, "clean")
        with open(os.path.join(AD, "mutant-manifest.json")) as fh:
            cls.entries = {e["id"]: e for e in json.load(fh)["mutants"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def checks(cls, blend, name, baseline=None):
        out = os.path.join(cls.tmp, name + ".jsonl")
        args = ["--python", CHECKS, "--", "--manifest", cls.scene, "--journal", cls.journal,
                "--thresholds", os.path.join(ROOT, "catalogue", "thresholds.tsv"), "--out", out]
        if baseline:
            args += ["--baseline", baseline]
        r = run_blender(cls.tmp, args, blend=blend)
        if r.returncode != 0:
            raise RuntimeError("scene_checks rc %d: %s" % (r.returncode, r.stderr[-400:]))
        return out

    def apply(self, entry):
        mid = entry["id"]
        spec = os.path.join(self.tmp, mid + ".spec.json")
        with open(spec, "w") as fh:
            json.dump({"id": mid, "fault": entry["fault"]}, fh)
        out, receipt = os.path.join(self.tmp, mid + ".blend"), os.path.join(self.tmp, mid + ".receipt.json")
        r = run_blender(self.tmp, ["--python", APPLY, "--", "--spec", spec, "--out", out, "--receipt", receipt], blend=self.base)
        return r, out, receipt

    def test_clean_base_has_no_fail(self):
        rows = [r for r in rows_of(self.clean) if "verdict" in r]
        self.assertTrue(rows)
        self.assertEqual([r["oracle_id"] for r in rows if r["verdict"] == "FAIL"], [])

    def test_sd_mutants_killed_and_equivalent_passes(self):
        # Only SD-expected entries; px-only entries (bm.flicker.16, bm.step_hold.22) are covered by pixel tests.
        for mid, e in sorted(self.entries.items()):
            if not e["expected_sd"]:
                continue
            with self.subTest(mutant=mid):
                r, out, receipt = self.apply(e)
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                with open(receipt) as fh:
                    rc = json.load(fh)
                self.assertEqual(rc["mutant_id"], mid)
                with self.subTest(mutant=mid, part="receipt_sha"):
                    self.assertEqual(rc["fault_spec_sha256"], e["fault_spec_sha256"])
                self.assertNotEqual(rc["witness_before"], rc["witness_after"])
                rows = [x for x in rows_of(self.checks(out, mid, baseline=self.clean)) if "verdict" in x]
                failing = {x["oracle_id"] for x in rows if x["verdict"] == "FAIL"}
                if e["equivalent"]:
                    self.assertEqual(failing, set(), "false positive on equivalent mutant")
                else:
                    self.assertTrue(set(e["expected_sd"]) <= failing, (sorted(e["expected_sd"]), sorted(failing)))

    def test_noop_mutant_is_refused_with_rc5_and_no_output(self):
        e = json.loads(json.dumps(self.entries["bm.hide_render.17"]))
        e["fault"] = {"operation": "scale_object", "selector": {"object": "Subject"}, "parameters": {"permille": 1000}}
        r, out, _ = self.apply(e)
        self.assertEqual(r.returncode, 5, r.stderr[-300:])
        self.assertFalse(os.path.exists(out))

    def test_missing_selector_target_is_rc2(self):
        e = json.loads(json.dumps(self.entries["bm.hide_render.17"]))
        e["fault"]["selector"] = {"object": "DoesNotExist"}
        r, out, _ = self.apply(e)
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
