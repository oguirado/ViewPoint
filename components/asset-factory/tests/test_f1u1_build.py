"""Host-only build tests. Synthetic proofs never stand in for runtime evidence."""
import ast
import copy
import difflib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
try:
    from asset_factory import proc as af_proc
except ModuleNotFoundError as exc:
    if exc.name != "asset_factory":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from asset_factory import proc as af_proc
    finally:
        sys.path.pop(0)
RECIPE = Path(os.environ.get("AF_F1U1_RECIPE", ROOT / "providers/trellis2/build"))
spec = importlib.util.spec_from_file_location("trellis_build", RECIPE / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
spec = importlib.util.spec_from_file_location("trellis_probe", RECIPE / "runtime_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe.Refusal = build.Refusal


def child(argv, *, env=None, timeout=30):
    env = dict(os.environ if env is None else env)
    env["AF_PROCESS_KERNEL_ROOT"] = str(ROOT)
    process = af_proc.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True, env=env)
    try:
        output, _ = af_proc.communicate(process, timeout=timeout)
    except subprocess.TimeoutExpired:
        af_proc.signal_group(process, signal.SIGKILL)
        af_proc.communicate(process)
        raise
    return process.returncode, output.decode()


def assert_namespace_released(case, identity_file):
    """A nested PID is not an outer signal target; inspect namespace identity."""
    identity = json.loads(identity_file.read_text())
    case.assertGreater(identity["pid"], 1)
    namespace = identity["pid_namespace"]
    if os.environ.get('AF_PROC_BACKEND') == 'pidns':
        case.assertEqual(os.readlink('/proc/self/ns/pid'), namespace)
        case.assertFalse(Path('/proc', str(identity['pid'])).exists(), 'owned descendant not reaped')
        return
    case.assertNotEqual(os.readlink("/proc/self/ns/pid"), namespace)
    live = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / "ns/pid") != namespace:
                continue
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            continue
        if state not in ("Z", "X"):
            live.append(entry.name)
    case.assertEqual([], live, "owned namespace still has running descendants")


def added_class(recipe=RECIPE):
    text = (recipe / "patches/trellis2-geometry.patch").read_text()
    added = "".join(line[1:] for line in text.splitlines(True) if line.startswith("+") and not line.startswith("+++"))
    return added[added.index("class Trellis2GeometryPipeline"):]


def lazy_init(recipe=RECIPE):
    text = (recipe / "patches/trellis2-geometry.patch").read_text().split("--- a/trellis2/", 1)[0]
    return "".join(line[1:] for line in text.splitlines(True) if line.startswith("+") and not line.startswith("+++"))


def parameter_trace(code):
    seeds, calls = [], []
    torch = types.SimpleNamespace(no_grad=lambda: lambda f: f, manual_seed=seeds.append,
                                  cuda=types.SimpleNamespace(empty_cache=lambda: None))
    class Base:
        models = {"shape_slat_flow_model_512": "model512", "shape_slat_flow_model_1024": "model1024"}
        def __init__(self):
            self.sparse_calls = []
        def get_cond(self, image, res):
            return res
        def sample_sparse_structure(self, cond, res, n, params):
            self.sparse_calls.append((cond, res, n, params))
            return "coords"
        def sample_shape_slat(self, cond, model, coords, params):
            calls.append((cond, model, params))
            return "shape"
        def sample_shape_slat_cascade(self, c1, c2, m1, m2, low, high, coords, params, budget):
            calls.append((high, budget, c1, c2, m1, m2, low, coords, params))
            return "shape", high
        def decode_shape_slat(self, shape, resolution):
            return [(shape, resolution)], None
    namespace = {"torch": torch, "Trellis2ImageTo3DPipeline": Base, "Optional": __import__("typing").Optional,
                 "List": list, "Mesh": object}
    exec(compile(ast.parse(code), "geometry-patch", "exec"), namespace)
    instance = namespace["Trellis2GeometryPipeline"]()
    results = []
    for index, kind in enumerate(("512", "1024", "1024_cascade", "1536_cascade")):
        results.append(instance.run("synthetic-image", seed=101 + index, pipeline_type=kind,
                                    preprocess_image=False, max_num_tokens=123,
                                    shape_slat_sampler_params={"steps": 7}))
    return seeds, calls, results, instance


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="af-f1u1-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipe = self.root / "recipe"
        shutil.copytree(RECIPE, self.recipe)
        self.lock = json.loads((self.recipe / "build-inputs.json").read_text())

    def refusal(self, rc, function, *args, **kwargs):
        with self.assertRaises(build.Refusal) as caught:
            function(*args, **kwargs)
        self.assertEqual(rc, caught.exception.rc)
        return caught.exception.reason

    def complete(self):
        lock = copy.deepcopy(self.lock)
        lock["bases"] = {"builder": "example.invalid/cuda-devel@sha256:" + "a" * 64,
                         "runtime": "example.invalid/cuda-runtime@sha256:" + "b" * 64}
        lock["compiler_sha256"] = "c" * 64
        lock["wheels"] = [{"path": "synthetic-1-py3-none-any.whl", "sha256": build.sha(b"synthetic-wheel"),
                           "requirement": "synthetic==1"}]
        lock["debs"] = {stage: [{"path": "synthetic.deb", "sha256": build.sha(b"synthetic-deb")}]
                         for stage in ("builder", "runtime")}
        return lock

    def synthetic_patch(self, before="value = 1\n", after="value = 2\n"):
        roots = {"example": self.root / "source"}
        roots["example"].mkdir()
        (roots["example"] / "code.py").write_text(before)
        patch = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                            fromfile="a/code.py", tofile="b/code.py"))
        (self.recipe / "synthetic.patch").write_text(patch)
        lock = {"sources": {"example": {}}, "patches": [
            {"source": "example", "path": "synthetic.patch", "sha256": build.sha(patch.encode()),
             "files": [{"path": "code.py", "before_sha256": build.sha(before.encode()),
                        "after_sha256": build.sha(after.encode())}]}]}
        return lock, roots

    def test_build_inputs_are_pinned(self):
        build.check_inputs(self.complete(), self.recipe)
        self.assertEqual(40, len(self.lock["sources"]["trellis2"]["revision"]))
        build.check_inputs(self.lock, self.recipe)
        # Null is an absence, never a zero-sized or implicitly pinned input.
        missing = copy.deepcopy(self.lock)
        missing["compiler_sha256"] = None
        self.assertIn("inputs_absent", self.refusal(3, build.check_inputs, missing, self.recipe))

    def test_unpinned_base_or_changed_patch_rejected(self):
        lock = self.complete()
        lock["bases"]["builder"] = "cuda:latest"
        self.refusal(4, build.check_inputs, lock, self.recipe)
        lock = self.complete()
        patch = self.recipe / lock["patches"][0]["path"]
        patch.write_bytes(patch.read_bytes() + b"\n")
        self.refusal(4, build.check_inputs, lock, self.recipe)

    def test_patches_apply_and_python_parses(self):
        lock, roots = self.synthetic_patch()
        build.apply_patches(lock, roots, self.recipe)
        self.assertEqual("value = 2\n", (roots["example"] / "code.py").read_text())
        ast.parse(lazy_init())
        ast.parse(added_class())

    def test_wrong_revision_or_literal_echo_escape_rejected(self):
        lock, roots = self.synthetic_patch(after="value = 2\\nprint('bad')\n")
        # Both postimage/patch digests match: only the syntax gate can reject it.
        self.refusal(5, build.apply_patches, lock, roots, self.recipe)
        lock = self.complete()
        lock["sources"]["trellis2"]["revision"] = "main"
        self.refusal(4, build.check_inputs, lock, self.recipe)

    def test_source_drift_rejected(self):
        lock, roots = self.synthetic_patch()
        (roots["example"] / "code.py").write_text("value = 9\n")
        self.assertEqual("source_bytes_stale", self.refusal(4, build.apply_patches, lock, roots, self.recipe))
        self.assertEqual("value = 9\n", (roots["example"] / "code.py").read_text())

    def import_boundary(self, initializer):
        package = self.root / "import-case" / "o_voxel"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(initializer)
        (package / "convert.py").write_text("def geometry():\n    return 'synthetic-shape'\n")
        (package / "postprocess.py").write_text("import nvdiffrast\n")
        script = '''import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
try:
    assert importlib.util.find_spec('nvdiffrast') is None
    assert importlib.util.find_spec('nvdiffrec') is None
    from o_voxel import convert
    assert convert.geometry() == 'synthetic-shape'
    assert 'o_voxel.postprocess' not in sys.modules
    rc = 0
except (ImportError, AssertionError):
    rc = 5
print(json.dumps({'rc':rc}, sort_keys=True))
sys.exit(rc)
'''
        rc, output = child([sys.executable, "-I", "-B", "-S", "-c", script, str(package.parent)])
        self.assertEqual(rc, json.loads(output)["rc"])
        return rc

    def test_geometry_dependency_boundary(self):
        # Execute the actual patched initializer, with synthetic child modules.
        # This proves lazy import behavior; real CUDA geometry remains unproven.
        self.assertEqual(0, self.import_boundary(lazy_init()))
        self.assertEqual(3, probe.acceptance(0))

    def test_reintroduced_eager_postprocess_import_rejected(self):
        self.assertEqual(5, self.import_boundary("from . import convert, postprocess\n"))

    def test_acceptance_fold_synthetic_links(self):
        probe.check_links("aarch64", {name: True for name in probe.EXTENSIONS}, True, "sm_121", "sm_121")
        # Synthetic prerequisite evidence cannot prove hardware execution.
        self.assertEqual(3, probe.acceptance(0, None, None))

    def test_missing_extension_or_false_ptxas_success_rejected(self):
        self.refusal(3, probe.check_links, "aarch64", {"cumesh": True}, True, "sm_121", "sm_121")
        self.refusal(3, probe.check_links, "aarch64", {x: True for x in probe.EXTENSIONS}, False, "sm_121", "sm_121")
        self.refusal(4, probe.check_links, "aarch64", {x: True for x in probe.EXTENSIONS}, True, "sm_120", "sm_121")
        self.refusal(4, probe.check_links, "aarch64_be", {x: True for x in probe.EXTENSIONS}, True, "sm_121", "sm_121")

    def test_acceptance_fold_complete_and_missing(self):
        self.assertEqual(0, probe.acceptance(0, 0, 0))  # Synthetic evaluator statuses only.
        self.assertEqual(3, probe.acceptance(0, 0, None))
        self.assertEqual(3, probe.acceptance(0, None, 0))

    def test_acceptance_fold_invalid_and_refused(self):
        for geometry, glb, expected in ((None, None, 3), (0, 5, 5), (3, 0, 3), (4, 0, 3), (6, 0, 3), (1, 0, 2), (True, 0, 2)):
            self.assertEqual(expected, probe.acceptance(0, geometry, glb))

    def assert_parameters(self, code):
        seeds, calls, results, instance = parameter_trace(code)
        self.assertEqual([101, 102, 103, 104], seeds)
        self.assertEqual([512, 1024, 1024, 1536], [result[0][1] for result in results])
        self.assertEqual({"steps": 7}, calls[-1][-1])
        self.assertEqual(123, calls[-1][1])
        self.assertEqual([(512, "model512", {"steps": 7}), (1024, "model1024", {"steps": 7}),
                          (1024, 123, 512, 1024, "model512", "model1024", 512, "coords", {"steps": 7}),
                          (1536, 123, 512, 1024, "model512", "model1024", 512, "coords", {"steps": 7})], calls)
        self.assertEqual([(512, 32, 1, {}), (512, 64, 1, {}), (512, 32, 1, {}), (512, 32, 1, {})], instance.sparse_calls)
        self.assertTrue(all(not name.startswith("tex_") for name in instance.model_names_to_load))
        with self.assertRaises(TypeError):
            instance.run("image", pipeline_type="512")
        with self.assertRaises(TypeError):
            instance.run("image", seed=7)
        with self.assertRaises(ValueError):
            instance.run("image", seed=True, pipeline_type="512")
        with self.assertRaises(ValueError):
            instance.run("image", seed=7, pipeline_type="latest")

    def test_parameters_reach_pipeline(self):
        self.assert_parameters(added_class())

    def test_hardcoded_seed_or_pipeline_type_rejected(self):
        with self.assertRaises(AssertionError):
            self.assert_parameters(added_class().replace("torch.manual_seed(seed)", "torch.manual_seed(42)"))
        with self.assertRaises(AssertionError):
            self.assert_parameters(added_class().replace("if type(seed) is not int:", "pipeline_type = '512'\n        if type(seed) is not int:"))

    def test_prepares_only_validated_context(self):
        from test_f1u1_repairs import RepairTests
        fixture = RepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        lock, sources, packages = fixture.synthetic()
        output = fixture.root / "published"
        # Observe a real source export, with no replacement implementation.
        with mock.patch.object(build, "export_sources", wraps=build.export_sources) as exported:
            result = build.prepare(lock, sources_root=sources, packages_root=packages, output=output, recipe=fixture.recipe)
        exported.assert_called_once()
        self.assertEqual(0, result["rc"])
        self.assertFalse(result["build_executed"])
        self.assertIn("--network=none", result["build_argv"])
        self.assertEqual(build.canonical(lock), (output / "build-inputs.json").read_text())
        self.assertIn("--hash=sha256:", (output / "requirements.lock").read_text())

    def test_refusal_preserves_outputs(self):
        output = self.root / "caller-output"
        output.write_bytes(b"preserve-me")
        self.refusal(2, build.prepare, self.complete(), sources_root=self.root, packages_root=self.root,
                     output=output, recipe=self.recipe)
        self.assertEqual(b"preserve-me", output.read_bytes())

    def test_invalid_schema_or_timeout_keeps_caller_bytes(self):
        output = self.root / "caller-output"
        output.write_bytes(b"preserve-me")
        bad = self.complete()
        bad["unexpected"] = True
        self.refusal(2, build.prepare, bad, sources_root=self.root, packages_root=self.root,
                     output=output, recipe=self.recipe)
        self.refusal(6, build.run_owned, [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
                     cwd=self.root, timeout_s=1)
        self.assertEqual(b"preserve-me", output.read_bytes())

    def test_timeout_releases_owned_descendants(self):
        pidfile = self.root / "child.pid"
        script = "import json,os,pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)']);pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':p.pid,'pid_namespace':os.readlink('/proc/self/ns/pid')}));time.sleep(60)"
        self.refusal(6, build.run_owned, [sys.executable, "-I", "-B", "-c", script, str(pidfile)],
                     cwd=self.root, timeout_s=1)
        self.assertTrue(pidfile.is_file())
        assert_namespace_released(self, pidfile)

    def test_paths_json_and_dependencies_fail_closed(self):
        self.assertEqual(self.root / "safe", build.inside(self.root, "safe"))
        for path in ("../outside", "/absolute", "x/../outside", "a\\b", "nul\x00"):
            self.refusal(2, build.inside, self.root, path)
        (self.root / "escape").symlink_to(self.root.parent, target_is_directory=True)
        self.refusal(2, build.inside, self.root, "escape/file")
        for contents in ('{"a":1,"a":2}', '{"a":1.5}', '{"a":NaN}'):
            path = self.root / "bad.json"
            path.write_text(contents)
            self.refusal(2, build.read_json, path)
        lock = self.complete()
        lock["wheels"][0]["requirement"] = "nvdiffrast==1"
        self.refusal(5, build.check_inputs, lock, self.recipe)

    def test_cli_and_recipe_do_not_build(self):
        rc, output = child([sys.executable, "-I", "-B", str(RECIPE / "build.py")])
        self.assertEqual(0, rc)
        result = json.loads(output)
        self.assertFalse(result["build_executed"])
        self.assertEqual(build.canonical(result), output)
        missing = copy.deepcopy(self.lock)
        missing["compiler_sha256"] = None
        lock_path = self.root / "missing-inputs.json"
        lock_path.write_text(build.canonical(missing))
        rc, output = child([sys.executable, "-I", "-B", str(RECIPE / "build.py"), "--lock", str(lock_path)])
        self.assertEqual(3, rc)
        self.assertFalse(json.loads(output)["build_executed"])
        rc, output = child([sys.executable, "-I", "-B", str(RECIPE / "runtime_probe.py")])
        self.assertEqual(3, rc)
        self.assertEqual("CANNOT_SEE", json.loads(output)["verdict"])
        docker = (RECIPE / "Dockerfile").read_text()
        for forbidden in ("nvdiffrast", "nvdiffrec", "|| true", "echo -e", "sed -i", "apt-get", "git clone", "pip install ."):
            self.assertNotIn(forbidden, docker)
        self.assertIn("--require-hashes", docker)
        self.assertIn("--no-build-isolation", docker)

    def test_published_patches_against_local_revisions(self):
        # Opt-in local source proof is read-only. No vendor tree is committed.
        self.assertEqual(3, probe.acceptance(0))  # Preserve the existing refusal assertion; source proof follows.
        source_root = Path(os.environ.get("AF_F1U1_REPOS", ""))
        self.assertTrue(source_root.is_absolute() and source_root.is_dir(),
                        "Set AF_F1U1_REPOS to the upstream source corpus: CANNOT_SEE")
        roots = {}
        for name, source in self.lock["sources"].items():
            directory = source_root / source["directory"]
            actual = build.run_owned(["git", "-C", str(directory), "rev-parse", "HEAD"], cwd=self.root).decode().strip()
            self.assertEqual(source["revision"], actual)
            roots[name] = self.root / name
            roots[name].mkdir()
        for patch in self.lock["patches"]:
            source = self.lock["sources"][patch["source"]]
            for item in patch["files"]:
                data = build.run_owned(["git", "-C", str(source_root / source["directory"]), "show",
                                        source["revision"] + ":" + item["path"]], cwd=self.root)
                target = roots[patch["source"]] / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        build.apply_patches(self.lock, roots, self.recipe)

    def test_local_source_export_and_gitlink_closure(self):
        self.assertEqual(3, probe.acceptance(0))  # Preserve the existing refusal assertion; source proof follows.
        source_root = Path(os.environ.get("AF_F1U1_REPOS", ""))
        self.assertTrue(source_root.is_absolute() and source_root.is_dir(),
                        "Set AF_F1U1_REPOS to the upstream source corpus: CANNOT_SEE")
        stage = self.root / "exported"
        stage.mkdir()
        build.export_sources(self.lock, source_root, stage)
        self.assertTrue((stage / "sources/cumesh/third_party/cubvh/LICENSE").is_file())
        self.assertEqual("/usr/include/eigen3", os.readlink(stage / "sources/cumesh/third_party/cubvh/third_party/eigen"))
        self.assertEqual("/usr/include/eigen3", os.readlink(stage / "sources/trellis2/o-voxel/third_party/eigen"))
        self.assertIn("class Trellis2GeometryPipeline", (stage / "sources/trellis2/trellis2/pipelines/trellis2_image_to_3d.py").read_text())
        for field, value, reason in (("revision", "0" * 40, "source_revision_stale"),
                                     ("licence_sha256", "0" * 64, "licence_stale")):
            lock = copy.deepcopy(self.lock)
            lock["sources"]["trellis2"][field] = value
            rejected = self.root / ("rejected-" + field)
            rejected.mkdir()
            self.assertEqual(reason, self.refusal(4, build.export_sources, lock, Path(source_root), rejected))

    def test_acceptance_fold_planted_failure(self):
        self.assertEqual(5, probe.acceptance(0, 5, 0))
        self.assertEqual(3, probe.acceptance(0, None, 0))
        self.assertEqual(2, probe.acceptance(1, 0, 0))

    def test_mutation_controls_go_red(self):
        mutants = (
            ("missing-inputs", "build.py", "if missing:", "if False and missing:", "test_build_inputs_are_pinned"),
            ("patch-integrity", "build.py", 'sha(path.read_bytes()) != patch["sha256"]', "False", "test_unpinned_base_or_changed_patch_rejected"),
            ("source-integrity", "build.py", 'sha(path.read_bytes()) != item["before_sha256"]', "False", "test_source_drift_rejected"),
            ("syntax", "build.py", 'ast.parse(data, filename=item["path"])', "None", "test_wrong_revision_or_literal_echo_escape_rejected"),
            ("seed", "patches/trellis2-geometry.patch", "torch.manual_seed(seed)", "torch.manual_seed(42)", "test_parameters_reach_pipeline"),
            ("pipeline-type", "patches/trellis2-geometry.patch", '+        if type(seed) is not int:', "+        pipeline_type = '512'\n+        if type(seed) is not int:", "test_parameters_reach_pipeline"),
            ("eager-import", "patches/trellis2-geometry.patch", '+import importlib\n', '+import importlib\n+from . import postprocess\n', "test_geometry_dependency_boundary"),
            ("missing-link", "runtime_probe.py", 'raise Refusal(3, "extension_link_unproven")', "pass", "test_missing_extension_or_false_ptxas_success_rejected"),
            ("missing-runtime", "runtime_probe.py", "return 3\n    return 0", "return 0\n    return 0", "test_acceptance_fold_planted_failure"),
            ("caller-bytes", "build.py", "output = output.absolute()", "output = output.absolute()\n    if output.is_file():\n        output.write_bytes(b'broken')", "test_refusal_preserves_outputs"),
            ("timeout-cleanup", "build.py", "wait_group_empty(process)", "pass", "test_timeout_releases_owned_descendants"),
            ("failing-leg", "runtime_probe.py", "if 5 in values:\n        return 5", "if 5 in values:\n        return 0", "test_acceptance_fold_planted_failure"),
            ("compiler-proof", "runtime_probe.py", 'raise Refusal(3, "compiler_identity_unproven")', "pass", "test_missing_extension_or_false_ptxas_success_rejected"),
            ("stale-cache", "runtime_probe.py", 'raise Refusal(4, "cache_architecture_stale")', "pass", "test_missing_extension_or_false_ptxas_success_rejected"),
        )
        for name, filename, before, after, test in mutants:
            with self.subTest(mutant=name):
                candidate = self.root / ("mutant-" + name)
                shutil.copytree(RECIPE, candidate)
                path = candidate / filename
                text = path.read_text()
                self.assertEqual(1, text.count(before))
                path.write_text(text.replace(before, after))
                env = dict(os.environ, AF_F1U1_RECIPE=str(candidate), PYTHONDONTWRITEBYTECODE="1")
                script = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('unittest',run_name='__main__')"
                rc, output = child([sys.executable, "-I", "-B", "-c", script, str(ROOT / "tests"),
                                    "test_f1u1_build.BuildTests." + test], env=env)
                self.assertEqual(1, rc, output)  # Declared unittest boundary: foreign 1 -> stage 5.
                self.assertIn("Ran 1 test", output)
                self.assertIn("FAILED (failures=1)", output)
                print("MUTANT " + name + ": child_rc=" + str(rc) + "; unittest assertion failed as required", flush=True)


if __name__ == "__main__":
    unittest.main()
