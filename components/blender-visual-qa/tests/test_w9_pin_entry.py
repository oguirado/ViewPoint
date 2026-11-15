"""JW9c-1/JW9c-2 (W9 P round 4): one pin module admits the engine for every truth entry point.

Fresh processes, PYTHONPATH = the repo only (no sitecustomize, no pre-imports), VISIONCORE_PATH and the test-only
injection unset. A valid live vision-engine checkout is put on PYTHONPATH where the contract says it must be ignored
(pinned tree) or refused (configless tree)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/vision.json").read_text())
WHEEL = Path(CFG["engine_path"])
WHEEL = WHEEL if WHEEL.is_absolute() else ROOT / WHEEL
SCRATCH = ROOT / "build/room_P"
W10 = ["--out", "OUT", "--frames", "60", "--spp", "4,16,64", "--seeds", "fit:0-9,gate:10-39", "--jobs", "4",
       "--strict"]
ORIGINS = ("import json,sys;print('@@'+json.dumps({n:str(getattr(m,'__file__','') or '') for n,m in "
           "sys.modules.items() if n.split('.')[0] in ('visioncore','vmem','vadapt')}))")


def env_for(*paths):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join(str(p) for p in paths))
    for k in ("VISIONCORE_PATH", "BVQA_TEST_ENGINE", "BT_EXPECT_ENGINE", "PYTHONSTARTUP"):
        env.pop(k, None)
    return env


def run(code, cwd, env):
    return subprocess.run([sys.executable, "-B", "-c", code], cwd=cwd, env=env, text=True, capture_output=True,
                          timeout=120)


def live_checkout(dst):
    """A valid, importable engine source tree that is NOT the wheel (stands in for the operator's vision-engine checkout)."""
    with zipfile.ZipFile(WHEEL) as z:
        z.extractall(dst)
    return Path(dst)


def origins_of(stdout):
    return json.loads([l for l in stdout.splitlines() if l.startswith("@@")][-1][2:])


class W10CliStartsPinned(unittest.TestCase):
    def test_plain_w10_cli_validates_with_wheel_only(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            live = live_checkout(Path(tmp) / "live")
            argv = [a.replace("OUT", str(Path(tmp) / "out")) for a in W10]
            code = ("import sys;from blender_visual_qa.truth import run_bench as rb;a=rb.parse_args(%r);"
                    "b,w,h=rb.validate(a);assert a.seed_sets['fit']==list(range(10)) and len(a.seed_sets['gate'])==30;"
                    "assert a.spp_tiers==[4,16,64] and a.jobs==4 and a.cpu_plan=='w10';" % argv) + ORIGINS
            for paths in ((ROOT,), (live, ROOT), (ROOT, live)):
                r = run(code, ROOT, env_for(*paths))
                self.assertEqual(r.returncode, 0, r.stderr[-800:])
                o = origins_of(r.stdout)
                self.assertIn("vadapt.blender", o)
                self.assertTrue(o and all(v.startswith(str(WHEEL) + "/") for v in o.values()), o)

    def test_module_cli_dry_run_starts(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            argv = [a.replace("OUT", str(Path(tmp) / "out")) for a in W10] + ["--dry-run"]
            r = subprocess.run([sys.executable, "-B", "-m", "blender_visual_qa.truth.run_bench"] + argv, cwd=ROOT,
                               env=env_for(ROOT), text=True, capture_output=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            out = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(out["code"], "DRY_RUN")
            self.assertEqual(out["engine"], str(WHEEL))
            self.assertEqual(out["engine_source"], "wheel")
            self.assertTrue(all(v.startswith(str(WHEEL) + "/") for v in out["engine_origins"].values()))
            self.assertFalse((Path(tmp) / "out").exists())


class EntryPointsShareOnePin(unittest.TestCase):
    def test_each_entry_point_imports_wheel_with_live_first(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            live = live_checkout(Path(tmp) / "live")
            for mod in ("labels", "score", "calibrate", "run_bench"):
                r = run("from blender_visual_qa.truth import %s;" % mod + ORIGINS, ROOT, env_for(live, ROOT))
                self.assertEqual(r.returncode, 0, (mod, r.stderr[-800:]))
                o = origins_of(r.stdout)
                self.assertTrue(o and all(v.startswith(str(WHEEL) + "/") for v in o.values()), (mod, o))

    def test_no_live_checkout_literal_in_entry_points(self):
        for mod in ("labels", "score", "calibrate", "run_bench", "pin"):
            src = (ROOT / ("blender_visual_qa/truth/%s.py" % mod)).read_text()
            self.assertNotIn("/vision-engine", src, mod)
            self.assertNotIn("VISIONCORE_PATH", src, mod)

    def test_pin_is_first_engine_import(self):
        import ast
        for mod in ("labels", "score", "run_bench"):
            tree = ast.parse((ROOT / ("blender_visual_qa/truth/%s.py" % mod)).read_text())
            names = []
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    names.append((node.module or "") + ":" + ",".join(a.name for a in node.names))
                elif isinstance(node, ast.Import):
                    names.extend(a.name for a in node.names)
            first = next(i for i, n in enumerate(names) if "pin" in n.split(":")[-1].split(",") or n.endswith(".pin"))
            eng = [i for i, n in enumerate(names) if n.split(":")[0].split(".")[0] in ("visioncore", "vadapt", "vmem")
                   or n.split(":")[0] in ("calibrate", "labels", "score") or n in (":calibrate,labels,score",)]
            self.assertTrue(all(first < i for i in eng), (mod, names))


class ConfiglessFailsClosed(unittest.TestCase):
    def _tree(self, tmp):
        repo = Path(tmp) / "repo"
        for pkg in ("blender_visual_qa", "bpy_side"):
            shutil.copytree(ROOT / pkg, repo / pkg, ignore=shutil.ignore_patterns("__pycache__"))
        return repo

    def test_configless_with_live_engine_on_path_refuses(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            repo, live = self._tree(tmp), live_checkout(Path(tmp) / "live")
            for mod in ("labels", "score", "calibrate", "run_bench"):
                r = run("from blender_visual_qa.truth import %s" % mod, repo, env_for(repo, live))
                self.assertNotEqual(r.returncode, 0, mod)
                self.assertIn("E_PIN", r.stderr, mod)
                self.assertNotIn("ModuleNotFoundError", r.stderr, mod)

    def test_configless_import_fails_before_loading_live_engine(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            repo, live = self._tree(tmp), live_checkout(Path(tmp) / "live")
            code = ("import sys\ntry:\n from blender_visual_qa.truth import score\nexcept RuntimeError as e:\n"
                    " assert 'E_PIN' in str(e), e\n print('@@'+__import__('json').dumps(sorted(n for n in sys.modules "
                    "if n.split('.')[0] in ('visioncore','vmem','vadapt'))))\n")
            r = run(code, repo, env_for(repo, live))
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            self.assertEqual(origins_of(r.stdout), [])

    def test_explicit_test_engine_flag_admits_injected_engine(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            repo, live = self._tree(tmp), live_checkout(Path(tmp) / "live")
            env = env_for(repo)
            env["BVQA_TEST_ENGINE"] = str(live)
            r = run("from blender_visual_qa.truth import score;" + ORIGINS, repo, env)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            o = origins_of(r.stdout)
            self.assertTrue(o and all(str(Path(v).resolve()).startswith(str(live.resolve()) + "/") for v in o.values()), o)


class WrongShaAndPreload(unittest.TestCase):
    def test_wrong_sha_refused_by_every_entry_point(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            repo = Path(tmp) / "repo"
            for pkg in ("blender_visual_qa", "bpy_side"):
                shutil.copytree(ROOT / pkg, repo / pkg, ignore=shutil.ignore_patterns("__pycache__"))
            (repo / "config").mkdir()
            cfg = json.loads(json.dumps(CFG))
            cfg["engine_path"] = str(WHEEL)
            cfg["pin"]["wheel_sha256"] = "0" * 64
            (repo / "config/vision.json").write_text(json.dumps(cfg))
            for mod in ("labels", "score", "calibrate", "run_bench"):
                r = run("from blender_visual_qa.truth import %s" % mod, repo, env_for(repo))
                self.assertNotEqual(r.returncode, 0, mod)
                self.assertIn("E_PIN", r.stderr, mod)

    def test_preloaded_live_engine_refused_by_labels_and_run_bench(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            live = live_checkout(Path(tmp) / "live")
            for mod in ("labels", "run_bench"):
                code = "import sys;sys.path.insert(0,%r);import visioncore;from blender_visual_qa.truth import %s" % (
                    str(live), mod)
                r = run(code, ROOT, env_for(ROOT))
                self.assertNotEqual(r.returncode, 0, mod)
                self.assertIn("E_PIN", r.stderr, mod)


if __name__ == "__main__":
    unittest.main()
