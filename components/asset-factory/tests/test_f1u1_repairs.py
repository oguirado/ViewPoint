"""Adversarial regressions: real tiny repositories, no installs or model work."""
from asset_factory import proc as af_proc
import ast
import copy
import io
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import types
import unittest
import zipfile
from unittest import mock
import test_f1u1_build as original
ROOT, RECIPE, build, probe, child = original.ROOT, original.RECIPE, original.build, original.probe, original.child

REPOS = Path("/home/admin/Documentos/The-Construct_v1/docker/trellis2/repos")


def wheel_bytes(top="synthetic"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as wheel:
        wheel.writestr("synthetic-1.dist-info/METADATA", "Name: synthetic\nVersion: 1\n")
        wheel.writestr("synthetic-1.dist-info/top_level.txt", top + "\n")
        wheel.writestr(top + "/__init__.py", "")
    return stream.getvalue()


def deb_bytes(package="synthetic", payload="usr/share/synthetic/data"):
    def archive(name, data):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
        return stream.getvalue()
    output = b"!<arch>\n"
    for name, data in (("debian-binary", b"2.0\n"),
                       ("control.tar.gz", archive("control", ("Package: " + package + "\nVersion: 1\nArchitecture: arm64\n").encode())),
                       ("data.tar.gz", archive(payload, b"test"))):
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        output += header + data + (b"\n" if len(data) % 2 else b"")
    return output


class RepairTests(unittest.TestCase):
    setUp = original.BuildTests.setUp
    refusal = original.BuildTests.refusal
    complete = original.BuildTests.complete

    def git(self, root, *args):
        return build.run_owned(["git", "-C", str(root), *args], cwd=self.root).decode().strip()

    def synthetic(self):
        lock = self.complete()
        sources = self.root / "repos"
        sources.mkdir()
        for name, source in lock["sources"].items():
            root = sources / source["directory"]
            root.mkdir(parents=True, exist_ok=True)
            self.git(root, "init", "-q")
            (root / "LICENSE").write_text("synthetic licence\n")
            (root / "code.py").write_text("value = 1\n")
            self.git(root, "add", "LICENSE", "code.py")
            self.git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
            source["revision"] = self.git(root, "rev-parse", "HEAD")
            source["licence_sha256"] = build.sha((root / "LICENSE").read_bytes())
            source["gitlinks"] = {}
            archive_sha256 = build.sha(build.run_owned(["git", "-C", str(root), "archive", "--format=tar", source["revision"]], cwd=self.root))
            if "archive_sha256" in self.lock["sources"][name]:
                source["archive_sha256"] = archive_sha256
        # An alternate recipe must be used for both integrity checks and application.
        import difflib
        diff = "".join(difflib.unified_diff(["value = 1\n"], ["value = 2\n"], fromfile="a/code.py", tofile="b/code.py"))
        (self.recipe / "alternate.patch").write_text(diff)
        lock["patches"] = [{"source": "trellis2", "path": "alternate.patch", "sha256": build.sha(diff.encode()),
                            "files": [{"path": "code.py", "before_sha256": build.sha(b"value = 1\n"), "after_sha256": build.sha(b"value = 2\n")}]}]
        packages = self.root / "packages"
        wheel = wheel_bytes()
        deb = deb_bytes()
        lock["wheels"][0]["sha256"] = build.sha(wheel)
        for group in lock["debs"].values():
            group[0]["sha256"] = build.sha(deb)
        for folder, filename, data in (("wheels", lock["wheels"][0]["path"], wheel),
                                       ("debs/builder", "synthetic.deb", deb), ("debs/runtime", "synthetic.deb", deb)):
            path = packages / folder
            path.mkdir(parents=True)
            (path / filename).write_bytes(data)
        return lock, sources, packages

    def export(self, lock, sources, label="export"):
        stage = self.root / label
        stage.mkdir()
        if "recipe" in inspect.signature(build.export_sources).parameters:
            build.export_sources(lock, sources, stage, self.recipe)
        else:
            build.export_sources(lock, sources, stage)
        return stage

    def test_h1_no_environment_still_opens_upstream(self):
        case = original.BuildTests("test_published_patches_against_local_revisions")
        case.setUp()
        self.addCleanup(case.doCleanups)
        safety = {key: os.environ[key] for key in ('AF_PROC_BACKEND', 'AF_PROC_PIDNS_PROC') if key in os.environ}
        with mock.patch.dict(os.environ, safety, clear=True), mock.patch.object(build, "run_owned", wraps=build.run_owned) as opened:
            case.test_published_patches_against_local_revisions()
        self.assertGreater(opened.call_count, 0, "source proof returned without opening any source")

    def test_h2_needs_names_are_not_synthetic_folds(self):
        for name in ("test_build_arm64_runtime_links", "test_geometry_glb_from_declared_input",
                     "test_missing_receipt_or_malformed_glb_rejected", "test_gate_detects_planted_failure"):
            self.assertFalse(hasattr(original.BuildTests, name), name + " must remain reserved for real evaluators")
        main = ast.parse((RECIPE / "runtime_probe.py").read_text())
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "acceptance"
                            for fn in main.body if isinstance(fn, ast.FunctionDef) and fn.name == "main" for node in ast.walk(fn)))

    def test_h3_abi_exception_emits_canonical_fail(self):
        ptxas = self.recipe / "ptxas"
        ptxas.write_text("#!/bin/sh\nprintf 'ptxas release 12.9\\n'\n")
        ptxas.chmod(0o755)
        self.lock["compiler_sha256"] = build.sha(ptxas.read_bytes())
        (self.recipe / "build-inputs.json").write_text(build.canonical(self.lock))
        (self.recipe / "cumesh.py").write_text("")
        for exception in ("RuntimeError", "AssertionError", "AttributeError"):
            with self.subTest(exception=exception):
                (self.recipe / "flex_gemm.py").write_text("raise " + exception + "('planted ABI failure')\n")
                rc, output = child([sys.executable, "-I", "-B", "-S", str(self.recipe / "runtime_probe.py"), "--ptxas", str(ptxas), "--cache-arch", str(self.root / "arch")])
                self.assertEqual(5, rc, output)
                result = json.loads(output)
                self.assertEqual(["extension_import_failed"], result["reason_codes"])
                self.assertEqual(build.canonical(result), output)

    def test_h3_deep_lock_emits_error(self):
        path = self.root / "nested.json"
        path.write_text("[" * 3000 + "]" * 3000)
        rc, output = child([sys.executable, "-I", "-B", str(RECIPE / "build.py"), "--lock", str(path)])
        self.assertEqual(2, rc, output)
        self.assertEqual("ERROR", json.loads(output)["status"])

    def test_m5_export_rejects_info_attributes(self):
        lock, sources, _ = self.synthetic()
        root = sources / lock["sources"]["trellis2"]["directory"]
        (root / ".git/info/attributes").write_text("code.py export-ignore\n")
        with mock.patch.object(build, "apply_patches"):
            self.assertEqual("source_attributes_present", self.refusal(4, self.export, lock, sources))

    def test_m5_archive_hash_is_checked(self):
        lock, sources, _ = self.synthetic()
        lock["sources"]["trellis2"]["archive_sha256"] = "0" * 64
        with mock.patch.object(build, "apply_patches"):
            self.assertEqual("source_archive_stale", self.refusal(4, self.export, lock, sources))

    def test_m5_replacement_objects_are_disabled(self):
        with mock.patch.object(build.af_proc, "popen", wraps=build.af_proc.popen) as popen:
            build.run_owned([sys.executable, "-c", "pass"], cwd=self.root)
        self.assertEqual("1", popen.call_args.kwargs["env"].get("GIT_NO_REPLACE_OBJECTS"))
        self.assertEqual("1", popen.call_args.kwargs["env"].get("GIT_ATTR_NOSYSTEM"))

    def test_m6_publication_seals_and_syncs(self):
        lock, _, packages = self.synthetic()
        output = self.root / "context"
        with mock.patch.object(build, "export_sources"), mock.patch.object(build.os, "fsync", wraps=build.os.fsync) as synced:
            result = build.prepare(lock, sources_root=self.root, packages_root=packages, output=output, recipe=self.recipe)
        self.assertGreater(synced.call_count, 0)
        self.assertIn("context_sha256", result)

    def test_m6_real_prepare_binds_context_and_fsyncs(self):
        lock, sources, packages = self.synthetic()
        output = self.root / "published"
        with mock.patch.object(build.os, "fsync", wraps=build.os.fsync) as fsync:
            result = build.prepare(lock, sources_root=sources, packages_root=packages, output=output, recipe=self.recipe)
        self.assertEqual(0, result["rc"])
        self.assertEqual("value = 2\n", (output / "sources/trellis2/code.py").read_text())
        self.assertEqual(build.sha((output / "context.sha256").read_bytes()), result["context_sha256"])
        self.assertIn("CONTEXT_SHA256=" + result["context_sha256"], result["build_argv"])
        self.assertGreater(fsync.call_count, 2)
        build.verify_context(output, result["context_sha256"])
        for name in ("debs/builder/extra.deb", "sources/trellis2/code.py"):
            target = output / name
            saved = target.read_bytes() if target.exists() else None
            target.write_bytes(b"planted mutation")
            self.refusal(4, build.verify_context, output, result["context_sha256"])
            if saved is None:
                target.unlink()
            else:
                target.write_bytes(saved)
        docker = (output / "Dockerfile").read_text()
        self.assertEqual(2, docker.count("sha256sum -c context.sha256"))
        self.assertIn("context.paths", docker)

    def test_m6_docker_verification_shell_rejects_extra_and_changed_files(self):
        lock, sources, packages = self.synthetic()
        output = self.root / "shell-context"
        result = build.prepare(lock, sources_root=sources, packages_root=packages, output=output, recipe=self.recipe)
        docker = (output / "Dockerfile").read_text()
        command = docker.split("RUN printf ", 1)[1].split("\nRUN dpkg", 1)[0]
        command = "printf " + command.replace("\\\n", "")
        command = command.replace("/tmp/context.paths", str(self.root / "actual.paths")).replace("/tmp/context.links", str(self.root / "actual.links"))
        env = dict(os.environ, CONTEXT_SHA256=result["context_sha256"])
        def verify():
            return subprocess.run(["/bin/sh", "-c", command], cwd=output, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=10).returncode
        self.assertEqual(0, verify())
        extra = output / "debs/builder/extra.deb"
        extra.write_bytes(b"not declared")
        self.assertEqual(1, verify())
        extra.unlink()
        (output / "sources/trellis2/code.py").write_text("value = 999\n")
        self.assertEqual(1, verify())

    def test_m7_group_signals_precede_reaping(self):
        from systemd_fake import FakeRunner
        fake = FakeRunner(auto_exit=0)
        jobs, trace = [], []
        original_spawn = af_proc.popen
        def spawn(*args, **kwargs):
            job = original_spawn(*args, **kwargs)
            jobs.append(job)
            return job
        def dispose(unit):
            self.assertIsNone(jobs[0].returncode)
            trace.append('drained_before_result')
        fake.stop_hook = dispose
        with af_proc.using_runner(fake), mock.patch.object(af_proc, 'popen', side_effect=spawn):
            build.run_owned([sys.executable, '-c', 'pass'], cwd=self.root)
        self.assertEqual(trace, ['drained_before_result'])
        self.assertEqual(fake.units, {})

    def test_m8_alternate_recipe_real_export(self):
        lock, sources, _ = self.synthetic()
        stage = self.export(lock, sources)
        self.assertEqual("value = 2\n", (stage / "sources/trellis2/code.py").read_text())

    def test_m9_missing_source_is_incomplete(self):
        lock = self.complete()
        stage = self.root / "stage"
        stage.mkdir()
        self.refusal(3, build.export_sources, lock, self.root / "absent", stage)

    def test_m9_signal_death_is_error(self):
        # A-SAFE: R44 forbids a parent-directed relay. A native abort in the
        # owned command still proves signal-death translation without a target.
        self.refusal(2, build.run_owned, [sys.executable, "-c",
                     "import os,resource;resource.setrlimit(resource.RLIMIT_CORE,(0,0));os.abort()"], cwd=self.root)

    def test_supervised_command_has_own_session(self):
        script = "import json,os;print(json.dumps([os.getpid(),os.getpgrp(),os.getsid(0)]))"
        pid, pgid, sid = json.loads(build.run_owned([sys.executable, "-I", "-B", "-c", script], cwd=self.root))
        self.assertGreater(pid, 1)
        self.assertEqual([pid, pid], [pgid, sid])

    def test_ancestor_targets_are_refused_before_syscalls(self):
        script = '''
import os,sys
from unittest import mock
sys.path.insert(0,os.environ['AF_PROCESS_KERNEL_ROOT'])
from asset_factory import proc
assert os.getpid() == os.getpgrp() == os.getsid(0)
parent = os.getppid()
assert parent > 1
for method, target in ((proc.signal_pid,parent), (proc.signal_group,os.getpgid(parent))):
    with mock.patch.object(proc.os,'kill') as one, mock.patch.object(proc.os,'killpg') as group:
        try:
            method(target,9)
        except proc.SignalRefusal as exc:
            assert exc.code == 'group_not_owned', exc.code
        else:
            raise AssertionError('ancestor admitted')
        one.assert_not_called()
        group.assert_not_called()
'''
        rc, output = child([sys.executable, "-I", "-B", "-c", script])
        self.assertEqual(0, rc, output)

    def test_missing_ancestry_is_refused_before_syscalls(self):
        with mock.patch.object(af_proc.os, "getppid", return_value=900003), \
             mock.patch.object(af_proc.Path, "read_text", side_effect=FileNotFoundError), \
             mock.patch.object(af_proc.os, "kill") as one, \
             mock.patch.object(af_proc.os, "killpg") as group:
            with self.assertRaises(af_proc.SignalRefusal) as refusal:
                af_proc.signal_pid(900004, signal.SIGKILL)
            self.assertEqual("group_not_owned", refusal.exception.code)
            one.assert_not_called()
            group.assert_not_called()

    def test_low_gitlink_order_independent(self):
        # The real lock is sorted into the dependency-opposite order.
        lock = copy.deepcopy(self.lock)
        lock["sources"] = dict(reversed(list(lock["sources"].items())))
        stage = self.root / "reordered"
        stage.mkdir()
        build.export_sources(lock, REPOS, stage)
        self.assertTrue((stage / "sources/cumesh/third_party/cubvh/third_party/eigen").is_symlink())

    def test_low_wheel_filename_bound_to_requirement(self):
        lock = self.complete()
        lock["wheels"][0]["path"] = "nvdiffrast-1-py3-none-any.whl"
        self.refusal(5, build.check_inputs, lock, self.recipe)

    def test_low_hidden_restricted_wheel_and_deb(self):
        lock, sources, packages = self.synthetic()
        for group, data, item in (("wheels", wheel_bytes("nvdiffrast"), lock["wheels"][0]),
                                  ("debs/builder", deb_bytes(payload="usr/lib/python3/nvdiffrec/__init__.py"), lock["debs"]["builder"][0])):
            original = (packages / group / item["path"]).read_bytes()
            item["sha256"] = build.sha(data)
            (packages / group / item["path"]).write_bytes(data)
            with mock.patch.object(build, "export_sources"):
                self.refusal(5, build.prepare, lock, sources_root=sources, packages_root=packages, output=self.root / "bad-output", recipe=self.recipe)
            (packages / group / item["path"]).write_bytes(original)
            item["sha256"] = build.sha(original)

    def test_low_proc_esrch_is_not_cleanup_failure(self):
        from systemd_fake import FakeRunner
        with af_proc.using_runner(FakeRunner(auto_exit=0)):
            job = af_proc.popen(['/usr/bin/true'], start_new_session=True,
                                memory_limits=af_proc.MemoryLimits(1024, 2048))
            job.wait(1)
            with mock.patch.object(Path, 'read_text', side_effect=ProcessLookupError) as read:
                build.wait_group_empty(job)
            read.assert_not_called()

    def test_low_scratch_is_outside_component(self):
        self.assertFalse(self.root.is_relative_to(ROOT))

    def test_low_setsid_descendant_is_released(self):
        pidfile = self.root / "escaped.pid"
        inner = "import json,os,pathlib,sys,time;os.setsid();pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'pid_namespace':os.readlink('/proc/self/ns/pid')}));time.sleep(60)"
        script = "import pathlib,subprocess,sys,time;subprocess.Popen([sys.executable,'-I','-B','-c',sys.argv[1],sys.argv[2]]);p=pathlib.Path(sys.argv[2]);\nwhile not p.exists():time.sleep(.01)"
        build.run_owned([sys.executable, "-I", "-B", "-c", script, inner, str(pidfile)], cwd=self.root)
        self.assertTrue(pidfile.exists())
        original.assert_namespace_released(self, pidfile)

    def test_interface_canonical_rejects_float(self):
        self.refusal(2, build.canonical, {"metric": 1.5})
        self.assertEqual('{"enabled":true}\n', build.canonical({"enabled": True}))

    def test_interface_module_cli_and_success_verdict(self):
        lock = self.complete()
        path = self.root / "valid.json"
        path.write_text(build.canonical(lock))
        rc, output = child([sys.executable, "-B", "-m", "providers.trellis2.build.build", "--lock", str(path)])
        self.assertEqual(0, rc, output)
        self.assertEqual("PASS", json.loads(output)["verdict"])

    def test_low_test_import_preserves_process_import_state(self):
        script = "import sys;before=sys.path[:];sentinel=object();sys.modules['build']=sentinel;import test_f1u1_build;assert sys.path==before;assert sys.modules['build'] is sentinel"
        rc, output = child([sys.executable, "-B", "-c", script], env=dict(os.environ, PYTHONPATH=str(ROOT / "tests")))
        self.assertEqual(0, rc, output)

    def test_guard_bad_invocation_has_no_forbidden_rc(self):
        rc, output = child([sys.executable, "-I", "-B", str(RECIPE / "process_guard.py")])
        self.assertEqual(2, rc, output)
        self.assertEqual(2, json.loads(output)["rc"])

    def test_low_readme_limits_pin_claim(self):
        text = (RECIPE / "README.md").read_text()
        self.assertIn("format", text)
        self.assertIn("does not verify image availability", text)


class UpstreamContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RepairTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.lock = self.fixture.lock
        self.repos = Path(os.environ.get("AF_F1U1_REPOS", str(REPOS)))
        self.assertTrue(self.repos.is_dir(), "upstream proofs unavailable: CANNOT_SEE")

    def blob(self, path):
        source = self.lock["sources"]["trellis2"]
        return build.run_owned(["git", "-C", str(self.repos / source["directory"]), "show", source["revision"] + ":" + path], cwd=self.root).decode()

    def test_h4_actual_inherited_loader_selects_shape_models(self):
        base = ast.parse(self.blob("trellis2/pipelines/base.py"))
        upstream = ast.parse(self.blob("trellis2/pipelines/trellis2_image_to_3d.py"))
        base_class = next(node for node in base.body if isinstance(node, ast.ClassDef))
        upstream_class = next(node for node in upstream.body if isinstance(node, ast.ClassDef))
        loaded = []
        def model_loader(path):
            loaded.append(path)
            return types.SimpleNamespace(eval=lambda: None)
        noop = lambda **kwargs: object()
        factories = types.SimpleNamespace(Synthetic=noop)
        torch = types.SimpleNamespace(no_grad=lambda: lambda fn: fn)
        namespace = {"torch": torch, "models": types.SimpleNamespace(from_pretrained=model_loader),
                     "samplers": factories, "image_feature_extractor": factories, "rembg": factories}
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), base_class, upstream_class], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "pinned-loader", "exec"), namespace)
        exec("from __future__ import annotations\n" + original.added_class(), namespace)
        cls = namespace["Trellis2GeometryPipeline"]
        names = namespace["Trellis2ImageTo3DPipeline"].model_names_to_load
        args = {key: {"name": "Synthetic", "args": {}, "params": {}} for key in
                ("sparse_structure_sampler", "shape_slat_sampler", "tex_slat_sampler", "image_cond_model", "rembg_model")}
        args.update(models={name: name for name in names}, shape_slat_normalization={}, tex_slat_normalization={})
        config = self.root / "pipeline.json"
        config.write_text(json.dumps({"args": args}))
        pipeline = cls.from_pretrained(str(self.root))
        self.assertIs(type(pipeline), cls)
        self.assertEqual(set(cls.model_names_to_load), set(pipeline.models))
        self.assertEqual(set(cls.model_names_to_load), {Path(path).name for path in loaded})
        self.assertFalse(any("tex_" in path for path in loaded))
        # The negative changes only the selected subclass filter.
        cls.model_names_to_load = names
        loaded.clear()
        contaminated = cls.from_pretrained(str(self.root))
        self.assertTrue(any(name.startswith("tex_") for name in contaminated.models))

    def test_h4_pinned_sampler_call_signatures(self):
        tree = ast.parse(self.blob("trellis2/pipelines/trellis2_image_to_3d.py"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        signatures = {node.name: [arg.arg for arg in node.args.args][1:] for node in cls.body if isinstance(node, ast.FunctionDef)}
        expected = {
            "sample_sparse_structure": ["cond", "resolution", "num_samples", "sampler_params"],
            "sample_shape_slat": ["cond", "flow_model", "coords", "sampler_params"],
            "sample_shape_slat_cascade": ["lr_cond", "cond", "flow_model_lr", "flow_model", "lr_resolution", "resolution", "coords", "sampler_params", "max_num_tokens"],
        }
        for name, arguments in expected.items():
            self.assertEqual(arguments, signatures[name])
        # These traces now assert every condition, model, sparse resolution and cascade argument.
        case = original.BuildTests()
        case.assert_parameters(original.added_class())
        with self.assertRaises(AssertionError):
            case.assert_parameters(original.added_class().replace("cond_512, cond_1024,", "cond_1024, cond_512,"))

    def test_h4_eager_import_closure_and_restricted_mutant(self):
        stage = self.root / "closure"
        stage.mkdir()
        build.export_sources(self.lock, self.repos, stage)
        sources = stage / "sources"
        packages = {"trellis2": sources / "trellis2/trellis2", "o_voxel": sources / "trellis2/o-voxel/o_voxel",
                    "flex_gemm": sources / "flexgemm/flex_gemm", "cumesh": sources / "cumesh/cumesh"}
        def eager_closure():
            seen = set()
            def path_for(name):
                parts = name.split(".")
                if parts[0] not in packages:
                    return None
                base = packages[parts[0]].joinpath(*parts[1:])
                if base.with_suffix(".py").is_file():
                    return base.with_suffix(".py")
                if (base / "__init__.py").is_file():
                    return base / "__init__.py"
                return None
            def visit(name):
                if name in seen:
                    return
                seen.add(name)
                self.assertFalse(any(part.startswith(("nvdiffrast", "nvdiffrec")) for part in name.split(".")), name)
                path = path_for(name)
                if path is None:
                    return  # External/compiled dependencies require a separate runtime proof.
                parts = name.split(".")
                for index in range(1, len(parts)):
                    visit(".".join(parts[:index]))
                tree = ast.parse(path.read_bytes())
                package = name if path.name == "__init__.py" else name.rpartition(".")[0]
                def imports(nodes):
                    for node in nodes:
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                visit(alias.name)
                        elif isinstance(node, ast.ImportFrom):
                            prefix = package.split(".")[:len(package.split(".")) - node.level + 1] if node.level else []
                            base = ".".join(prefix + ([node.module] if node.module else []))
                            visit(base)
                            for alias in node.names:
                                candidate = base + "." + alias.name
                                if path_for(candidate):
                                    visit(candidate)
                                parent = path_for(base)
                                if parent and parent.name == "__init__.py":
                                    parent_tree = ast.parse(parent.read_bytes())
                                    for assignment in parent_tree.body:
                                        if isinstance(assignment, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__attributes" for t in assignment.targets):
                                            mapping = ast.literal_eval(assignment.value)
                                            if alias.name in mapping:
                                                visit(base + "." + mapping[alias.name])
                        elif isinstance(node, ast.If) and "__name__" not in ast.unparse(node.test):
                            imports(node.body + node.orelse)
                imports(tree.body)
            visit("trellis2.pipelines.trellis2_image_to_3d")
            # The selected shape decoder is resolved dynamically by the model loader.
            visit("trellis2.models.sc_vaes.fdg_vae")
            self.assertIn("o_voxel.convert", seen)
            self.assertNotIn("o_voxel.postprocess", seen)
            return seen
        seen = eager_closure()
        self.assertIn("trellis2.pipelines.base", seen)
        initializer = packages["trellis2"] / "pipelines/__init__.py"
        initializer.write_text("import nvdiffrast.torch\n" + initializer.read_text())
        with self.assertRaises(AssertionError):
            eager_closure()
