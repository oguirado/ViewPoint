"""Adversarial R1-R5 controls; synthetic bytes and owned CPU children only."""
from asset_factory import proc as af_proc
import copy
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from providers.unirig import adapter, gate, publication
from providers.unirig.common import Refusal, load
from providers.mocapanything_v2 import bvh
from providers.mocapanything_v2.build import recipe
import test_w3_rig_boundary as rig
import test_w3_motion_pipeline as motion


class SceneReferences(unittest.TestCase):
    def inspect(self, mutate=None):
        data = rig.rig_fixture(mutate=mutate)
        return gate.inspect_bytes(data, policy=rig.policy(), probes=rig.probes(data))[0]

    def test_R1_malformed_scene_root_mesh_and_skin_references_are_errors(self):
        mutations = [lambda d, b: d.update(scene=v) for v in ()]
        for value in (999, -1, False, "0"):
            mutations.append(lambda d, b, v=value: d.update(scene=v))
            mutations.append(lambda d, b, v=value: d["scenes"][0].update(nodes=[v]))
            mutations.append(lambda d, b, v=value: d["nodes"][0].update(skin=v))
            mutations.append(lambda d, b, v=value: d["nodes"][0].update(mesh=v))
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.assertEqual(self.inspect(mutate)["rc"], 2)

    def test_R1_empty_or_absent_active_scene_has_no_coverage(self):
        for mutate in (lambda d, b: d.update(scenes=[{"nodes": []}]),
                       lambda d, b: d.pop("scene"),
                       lambda d, b: d["scenes"][0].update(nodes=[1]),
                       lambda d, b: d["scenes"][0].update(nodes=[0])):
            with self.subTest(mutate=mutate):
                result = self.inspect(mutate)
                self.assertEqual((result["rc"], result["verdict"]), (3, "CANNOT_SEE"))

    def test_R1_selected_scene_and_descendant_coverage_pass(self):
        self.assertEqual(self.inspect()["rc"], 0)
        self.assertEqual(self.inspect(lambda d, b: d.update(
            scenes=[{"nodes": []}, {"nodes": [0, 1]}], scene=1))["rc"], 0)

    def test_R1_scene_roots_are_unique_parentless_integer_references(self):
        for roots in ([0, 1, 1], [0, 2], False, "0", [None], [0.0]):
            with self.subTest(roots=roots):
                self.assertEqual(self.inspect(
                    lambda d, b: d["scenes"][0].update(nodes=roots))["rc"], 2)

    def test_R1_descendant_mesh_is_covered_from_selected_scene(self):
        def mutate(doc, binary):
            doc["nodes"].append({"children": [0, 1]})
            doc["scenes"][0]["nodes"] = [3]
        self.assertEqual(self.inspect(mutate)["rc"], 0)

    def test_R1_boolean_buffer_reference_is_not_integer_zero(self):
        self.assertEqual(self.inspect(lambda d, b: d["bufferViews"][0].update(buffer=False))["rc"], 2)


class PublicationAndAdmission(unittest.TestCase):
    def setUp(self):
        self.fixture = rig.OfflineBoundaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_R2_parent_fsync_after_commit_returns_the_committed_result(self):
        request, kw = self.fixture.runtime()
        parent = kw["output_dir"].parent.resolve()
        original = os.fsync

        def fail_parent(fd):
            if Path(os.readlink('/proc/self/fd/' + str(fd))) == parent:
                raise OSError("injected parent fsync failure")
            return original(fd)

        with mock.patch.object(publication.os, "fsync", side_effect=fail_parent):
            result = adapter.run(request, **kw)
        self.assertTrue(kw["output_dir"].exists())
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(load(kw["output_dir"] / "result.json"), result)
        self.assertTrue(result["outputs"])

    def test_R2_parent_open_is_before_commit_and_refusal_preserves_target(self):
        request, kw = self.fixture.runtime()
        original = os.open

        def fail_parent(path, *args, **kwargs):
            if Path(path) == kw["output_dir"].parent:
                self.assertFalse(kw["output_dir"].exists(), "parent opened after commit")
                raise OSError("injected parent open failure")
            return original(path, *args, **kwargs)

        with mock.patch.object(publication.os, "open", side_effect=fail_parent):
            result = adapter.run(request, **kw)
        self.assertEqual(result["rc"], 2, result)
        self.assertFalse(kw["output_dir"].exists())
        self.assertEqual(result["outputs"], [])

    def test_R2_directory_close_after_commit_cannot_report_refusal(self):
        request, kw = self.fixture.runtime()
        parent = kw["output_dir"].parent.resolve()
        original = os.close

        def fail_parent(fd):
            is_parent = Path(os.readlink('/proc/self/fd/' + str(fd))) == parent
            original(fd)
            if is_parent:
                raise OSError("injected parent close failure")

        with mock.patch.object(publication.os, "close", side_effect=fail_parent):
            result = adapter.run(request, **kw)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(load(kw["output_dir"] / "result.json"), result)

    def test_R2_directory_sync_before_commit_preserves_target_on_failure(self):
        request, kw = self.fixture.runtime()
        original = os.fsync

        def fail_stage(fd):
            path = Path(os.readlink('/proc/self/fd/' + str(fd)))
            if path.is_dir() and path.name == "publication":
                raise OSError("injected staged directory fsync failure")
            return original(fd)

        with mock.patch.object(publication.os, "fsync", side_effect=fail_stage):
            result = adapter.run(request, **kw)
        self.assertEqual(result["rc"], 2, result)
        self.assertFalse(kw["output_dir"].exists())
        self.assertEqual(result["outputs"], [])

    def test_R2_publication_reports_durability_separately_from_visibility(self):
        root = self.fixture.root
        stage, target = root / "stage", root / "committed"
        stage.mkdir()
        (stage / "sentinel").write_bytes(b"published")
        original = os.fsync

        def fail_parent(fd):
            if Path(os.readlink('/proc/self/fd/' + str(fd))) == root:
                raise OSError("injected parent fsync failure")
            return original(fd)

        with mock.patch.object(publication.os, "fsync", side_effect=fail_parent):
            durable = publication.publish_directory(stage, target)
        self.assertIs(durable, False)
        self.assertEqual((target / "sentinel").read_bytes(), b"published")
        stage.mkdir()
        self.assertIs(publication.publish_directory(stage, root / "durable"), True)

    def test_R4_nonfixture_host_execution_is_refused_before_launch(self):
        request, kw = self.fixture.runtime()
        request["params"].pop("synthetic_fixture", None)
        kw["admission"]["request_sha256"] = core.sha256_json(request)
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=AssertionError("refusal must precede launch")) as child:
            result = adapter.run(request, **kw)
            child.assert_not_called()
        self.assertEqual(result["rc"], 3, result)
        self.assertIn("container_runtime_unqualified", result["absence_reasons"].values())
        self.assertFalse(kw["output_dir"].exists())

    def test_R4_host_gpu_request_is_refused_before_launch(self):
        request, kw = self.fixture.runtime()
        request["params"].update(synthetic_fixture=True, device="cuda")
        kw["admission"]["request_sha256"] = core.sha256_json(request)
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=AssertionError("refusal must precede launch")) as child:
            result = adapter.run(request, **kw)
            child.assert_not_called()
        self.assertEqual(result["rc"], 3, result)
        self.assertIn("gpu_runtime_unqualified", result["absence_reasons"].values())
        self.assertTrue(kw["admission"]["lease_held"])


class OwnedDescendants(unittest.TestCase):
    def test_R4_runner_receives_explicit_cpu_mask(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = root / "runner.py"
            runner.write_text("import os\nfrom pathlib import Path\n"
                              "Path('mask').write_text(repr(os.environ.get('CUDA_VISIBLE_DEVICES')))\n")
            with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "0"}):
                adapter.invoke(runner, root / "request", root / "output", root, 3, root)
            self.assertEqual((root / "mask").read_text(), "''")

    def escaped_child(self, timeout):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = root / "runner.py"
            runner.write_text(
                "import os,subprocess,sys,time\nfrom pathlib import Path\n"
                "child=subprocess.Popen([sys.executable,'-I','-c',"
                "'import os,time; os.fork(); time.sleep(30)'],start_new_session=True)\n"
                "Path('pid').write_text(str(child.pid))\n" +
                ("time.sleep(30)\n" if timeout else ""))
            try:
                if timeout:
                    with self.assertRaises(Refusal) as failure:
                        adapter.invoke(runner, root / "request", root / "output", root, 1, root)
                    self.assertEqual((failure.exception.rc, failure.exception.code), (6, "deadline_exceeded"))
                else:
                    adapter.invoke(runner, root / "request", root / "output", root, 3, root)
                pid = int((root / "pid").read_text())
                # A zombie is not successful release either: owned children must be reaped.
                self.assertFalse(Path('/proc', str(pid)).exists(), "escaped child survives cleanup")
            finally:
                if (root / "pid").exists():
                    self.assertFalse(Path('/proc', (root/'pid').read_text()).exists(),
                                     'owned child was not reaped')

    def test_R3_new_session_descendants_are_reaped_on_success(self):
        self.escaped_child(False)

    def test_R3_new_session_descendants_are_reaped_on_timeout(self):
        self.escaped_child(True)


class MotionDeadlines(unittest.TestCase):
    DEADLINES = ((0, 6), (-1, 6), (False, 2), (True, 2), ("1", 2), (1.5, 2))

    def test_R5_parser_deadlines_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = motion.fixture(temp)
            for deadline, rc in self.DEADLINES:
                with self.subTest(deadline=deadline):
                    with self.assertRaises(bvh.Refusal) as failure:
                        bvh.parse(fixture["data"], deadline_ns=deadline)
                    self.assertEqual(failure.exception.rc, rc)

    def test_R5_inventory_deadlines_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            data = b"weights"
            (Path(temp) / "weights").write_bytes(data)
            inventory = {"revision": "a" * 40, "files": [
                {"path": "weights", "bytes": len(data), "sha256": motion.digest(data)}]}
            for deadline, rc in self.DEADLINES:
                with self.subTest(deadline=deadline):
                    with self.assertRaises(bvh.Refusal) as failure:
                        recipe.verify_inventory(temp, inventory, deadline_ns=deadline)
                    self.assertEqual(failure.exception.rc, rc)

    def test_R5_expired_and_invalid_deadlines_in_all_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = motion.fixture(temp)
            data = b"weights"
            (Path(temp) / "weights").write_bytes(data)
            inventory = {"revision": "a" * 40, "files": [
                {"path": "weights", "bytes": len(data), "sha256": motion.digest(data)}]}
            for deadline, rc in ((0, 6), (-1, 6), (False, 2), (True, 2), ("1", 2), (1.5, 2)):
                with self.subTest(deadline=deadline):
                    report = motion.run_gate(fixture, deadline_ns=deadline)
                    self.assertEqual(report["rc"], rc, report)
                    for call in (lambda: bvh.parse(fixture["data"], deadline_ns=deadline),
                                 lambda: recipe.verify_inventory(temp, inventory, deadline_ns=deadline)):
                        with self.assertRaises(bvh.Refusal) as failure:
                            call()
                        self.assertEqual(failure.exception.rc, rc)

