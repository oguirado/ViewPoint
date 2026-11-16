"""Adversarial repair witnesses; fixtures and evaluators are explicitly synthetic."""
import copy
import ast
import inspect
import io
import json
import os
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_f2u2_topology as base
from tools.blender import topology as top, topology_glb as glb


def rewrite(data, mutate=None, binary_mutate=None):
    size = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + size])
    binary = data[28 + size:]
    if mutate:
        result = mutate(doc)
        if result is not None:
            doc = result
    if binary_mutate:
        binary = binary_mutate(doc, binary)
    encoded = json.dumps(doc, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 4)
    chunks = struct.pack("<I4s", len(encoded), b"JSON") + encoded
    chunks += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, len(chunks) + 12) + chunks


class RepairTests(unittest.TestCase):
    setUp = base.TopologyTests.setUp
    runner = base.TopologyTests.runner
    invoke = base.TopologyTests.invoke
    refusal = base.TopologyTests.refusal

    def raw_document(self, mutate):
        self.raw.write_bytes(rewrite(self.original, mutate))
        self.original = self.raw.read_bytes()

    def changed_evidence(self, mutate):
        def compare(raw, candidate, *, policy):
            code, receipt = base.evidence(raw, candidate, policy=policy)
            mutate(receipt)
            return code, receipt
        return compare

    def cli(self, **changes):
        policy_path = self.base / "policy.json"
        policy_path.write_text(json.dumps(base.POLICY))
        argv = ["--in", str(self.raw), "--out-dir", str(self.out), "--source-sha256",
                glb.digest(self.original), "--method", "identity", "--target-faces", "12",
                "--seed", "42", "--policy", str(policy_path)]
        for key, value in changes.items():
            argv += [key, value]
        return argv

    def test_f1_non_object_glb(self):
        self.raw_document(lambda _: [])
        self.refusal(2, "glb_invalid_document")

    def test_f1_node_name_integer(self):
        self.raw_document(lambda doc: doc["nodes"][0].update(name=7))
        self.refusal(2, "glb_invalid_document")

    def test_f1_null_metrics(self):
        self.refusal(2, "regression_contract", comparator=self.changed_evidence(
            lambda receipt: receipt.update(metrics=None)))

    def test_f1_malformed_nested_objects(self):
        for collection in ("nodes", "bufferViews", "accessors"):
            with self.subTest(collection=collection):
                path = self.base / (collection + ".glb")
                path.write_bytes(rewrite(self.original, lambda doc: doc[collection].__setitem__(0, 7)))
                with self.assertRaises(glb.TopologyError) as caught:
                    glb.inspect_glb(path)
                self.assertEqual(caught.exception.rc, 2)

    def test_f1_unexpected_library_exception_is_typed(self):
        with mock.patch.object(top, "inspect_glb", side_effect=RuntimeError("probe")):
            self.refusal(2, "io_or_contract_error")

    def test_f1_unexpected_cli_exception_is_typed(self):
        with mock.patch.object(top, "run", side_effect=RuntimeError("probe")), mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(top.main(self.cli()), 2)

    def test_f2_injected_evaluator_requires_explicit_test_flag(self):
        self.refusal(3, "untrusted_evaluator", allow_test_comparator=False)

    def test_f2_evaluator_hash_required(self):
        self.refusal(2, "regression_toolchain_invalid", comparator=self.changed_evidence(
            lambda receipt: receipt.pop("toolchain_sha256")))

    def test_f2_evaluator_provenance_is_recorded(self):
        rc, record = self.invoke()
        self.assertEqual(rc, 0, record)
        self.assertTrue(record["evaluator"]["test_only"])
        self.assertEqual(record["evaluator"]["module"], base.evidence.__module__)
        self.assertEqual(len(record["evaluator"]["module_sha256"]), 64)
        self.assertEqual(record["checks"][0]["evaluator_toolchain_sha256"], "c" * 64)

    def test_f3_starved_measurement_policy(self):
        for mesh_policy in ({}, {"sample_budget": 1, "raster_resolution": 2}):
            with self.subTest(policy=mesh_policy):
                policy = copy.deepcopy(base.POLICY)
                policy["mesh_policy"] = mesh_policy
                self.refusal(2, "measurement_policy_weakened", policy=policy)
                self.assertEqual(self.calls, [])

    def test_f3_sampling_covers_raw_triangle_corners_and_centres(self):
        policy = copy.deepcopy(base.POLICY)
        policy["mesh_policy"]["sample_budget"] = 47
        self.refusal(2, "measurement_policy_weakened", policy=policy)

    def test_f4_receipt_rules(self):
        cases = [("schema", "foreign/v1", 2, "regression_family"),
                 ("input_sha256", "0" * 64, 4, "regression_raw_stale"),
                 ("policy_sha256", "0" * 64, 4, "regression_policy_stale"),
                 ("failed", ["planted"], 5, "regression_failed_checks"),
                 ("unavailable", ["planted"], 3, "regression_checks_unavailable")]
        for key, value, code, reason in cases:
            with self.subTest(reason=reason):
                self.refusal(code, reason, comparator=self.changed_evidence(
                    lambda receipt, key=key, value=value: receipt.update({key: value})))

    def test_f4_invalid_iou(self):
        self.refusal(2, "iou_invalid", comparator=self.changed_evidence(
            lambda receipt: receipt["metrics"].update(silhouette_iou_ppm=1000001)))

    def test_f4_parameter_rules(self):
        for changes, reason in [({"seed": 2**31}, "seed_out_of_range"),
                                ({"timeout_s": 3601}, "timeout_out_of_range"),
                                ({"target_faces": 11, "lod_targets": list(range(10, 1, -1))}, "invalid_lod_targets"),
                                ({"method": "quadriflow", "quadriflow_target_quads": 5}, "quadriflow_solver_target_invalid"),
                                ({"target_faces": 12}, "target_not_reduction")]:
            with self.subTest(reason=reason):
                self.refusal(2, reason, **changes)

    def test_f4_commit_bindings(self):
        original_hash = top._file_hash
        for filename, reason in [("raw.glb", "scratch_raw_stale"),
                                 ("master.regression.json", "regression_receipt_stale"),
                                 ("LOD0.glb", "lod0_stale")]:
            with self.subTest(reason=reason):
                def drift(path):
                    if path.name == filename:
                        # raw.glb is checked once on staging and again on commit.
                        if filename == "raw.glb":
                            seen[0] += 1
                            if seen[0] == 1:
                                return original_hash(path)
                        return "0" * 64
                    return original_hash(path)
                seen = [0]
                with mock.patch.object(top, "_file_hash", drift):
                    self.refusal(4, reason)

    def test_f4_glb_refusals(self):
        cases = [
            ("version", lambda d: d["asset"].update(version="1.0"), 2, "glb_asset_version"),
            ("skins", lambda d: d.update(skins=[{}]), 3, "unsupported_glb_feature"),
            ("external", lambda d: d["buffers"][0].update(uri="outside.bin"), 2, "external_buffer_refused"),
            ("buffer-length", lambda d: d["buffers"][0].update(byteLength=999999), 2, "glb_buffer_length"),
            ("buffer-index", lambda d: d["bufferViews"][0].update(buffer=1), 2, "glb_buffer_index"),
            ("bounds", lambda d: d["bufferViews"][0].update(byteLength=999999), 2, "glb_view_bounds"),
            ("accessor-bounds", lambda d: d["accessors"][0].update(count=999999), 2, "glb_accessor_bounds"),
            ("sparse", lambda d: d["accessors"][0].update(sparse={}), 3, "unsupported_sparse_accessor"),
            ("helper", lambda d: d["nodes"][0].update(name="surface_col"), 3, "helper_separation_required"),
            ("unreachable", lambda d: d["nodes"].append({"mesh": 0}), 3, "unreachable_geometry"),
            ("cycle", lambda d: d["nodes"][0].update(children=[0]), 2, "glb_node_reuse_or_cycle"),
            ("mode", lambda d: d["meshes"][0]["primitives"][0].update(mode=1), 3, "unsupported_primitive"),
            ("position", lambda d: d["accessors"][0].update(componentType=5123), 2, "glb_position_type"),
            ("index", lambda d: d["accessors"][1].update(componentType=5122), 2, "glb_index_type"),
            ("triangles", lambda d: d["accessors"][1].update(count=35), 2, "glb_nontriangle_count"),
            ("attribute", lambda d: d["accessors"][2].update(count=3), 2, "glb_attribute_count"),
            ("empty", lambda d: d["meshes"][0].update(primitives=[]), 3, "empty_geometry"),
            ("uv-encoding", lambda d: d["accessors"][2].update(componentType=5123), 3, "unsupported_uv_encoding"),
            ("image", lambda d: d.update(images=[{"uri": "outside.png"}]), 2, "external_image_refused"),
        ]
        for label, mutate, code, reason in cases:
            with self.subTest(rule=label):
                path = self.base / (label + ".glb")
                path.write_bytes(rewrite(self.original, mutate))
                with self.assertRaises(glb.TopologyError) as caught:
                    glb.inspect_glb(path)
                self.assertEqual(caught.exception.rc, code)
                self.assertEqual(caught.exception.reason, reason)

    def test_f4_glb_container_refusals(self):
        invalid_chunk = bytearray(self.original)
        struct.pack_into("<I", invalid_chunk, 12, len(self.original))
        wrong_chunks = bytearray(self.original)
        wrong_chunks[16:20] = b"NOPE"
        truncated = bytearray(self.original + b"x")
        struct.pack_into("<I", truncated, 8, len(truncated))
        cases = [(b"invalid", "glb_invalid_header"), (invalid_chunk, "glb_invalid_chunk"),
                 (wrong_chunks, "glb_embedded_chunks_required"), (truncated, "glb_truncated_chunk")]
        for data, reason in cases:
            with self.subTest(reason=reason):
                path = self.base / (reason + ".glb")
                path.write_bytes(data)
                with self.assertRaises(glb.TopologyError) as caught:
                    glb.inspect_glb(path)
                self.assertEqual(caught.exception.rc, 2)
                self.assertEqual(caught.exception.reason, reason)

    def test_f6_partial_uv_is_explicitly_unsupported(self):
        def partial(doc):
            primitive = copy.deepcopy(doc["meshes"][0]["primitives"][0])
            primitive["attributes"].pop("TEXCOORD_0")
            doc["meshes"][0]["primitives"].append(primitive)
        self.raw_document(partial)
        self.refusal(3, "partial_uv_unsupported", method="identity", target_faces=24)

    def test_f6_texture_and_sampler_fingerprints(self):
        self.raw_document(lambda doc: doc.update(textures=[{"sampler": 0}], samplers=[{"wrapS": 33071}]))
        before = glb.inspect_glb(self.raw)
        for field in ("textures", "samplers"):
            with self.subTest(field=field):
                path = self.base / (field + ".glb")
                path.write_bytes(rewrite(self.original, lambda doc: doc[field][0].update(planted=1)))
                after = glb.inspect_glb(path)
                with self.assertRaises(glb.TopologyError) as caught:
                    top._check_preservation(before, after)
                self.assertEqual(caught.exception.rc, 5)

    def test_f6_zeroed_uv_refused(self):
        def zero_uv(doc, binary):
            view = doc["bufferViews"][2]
            start, size = view["byteOffset"], view["byteLength"]
            return binary[:start] + b"\0" * size + binary[start + size:]
        def runner(argv, timeout):
            self.runner(argv, timeout)
            candidate = Path(json.loads(Path(argv[-1]).read_text())["candidate"])
            candidate.write_bytes(rewrite(candidate.read_bytes(), binary_mutate=zero_uv))
            return 0, "OK"
        self.refusal(5, "uv_collapsed", runner=runner)

    def test_f8_contract_error_api(self):
        error = glb.TopologyError(2, "probe")
        self.assertEqual(error.code, "probe")
        self.assertEqual(error.path, "$")
        self.assertEqual(error.as_dict(), {"rc": 2, "status": "ERROR", "reason_codes": ["probe"], "path": "$"})

    def test_f8_closed_receipt_schema(self):
        rc, receipt = self.invoke()
        self.assertEqual(rc, 0, receipt)
        self.assertEqual(top.RECEIPT_SCHEMA["$id"], "urn:af:topology-stage:v1")
        top._validate_receipt(receipt)
        for mutate in (lambda d: d.update(planted=True), lambda d: d.pop("toolchain_sha256")):
            invalid = copy.deepcopy(receipt)
            mutate(invalid)
            with self.assertRaises(glb.TopologyError) as caught:
                top._validate_receipt(invalid)
            self.assertEqual(caught.exception.rc, 2)

    def test_f8_nested_receipt_objects_are_closed(self):
        rc, receipt = self.invoke()
        self.assertEqual(rc, 0, receipt)
        for branch in ("parameters", "evaluator", "artifacts", "checks"):
            with self.subTest(branch=branch):
                changed = copy.deepcopy(receipt)
                value = changed[branch][0] if isinstance(changed[branch], list) else changed[branch]
                value["planted"] = True
                with self.assertRaises(glb.TopologyError) as caught:
                    top._validate_receipt(changed)
                self.assertEqual(caught.exception.rc, 2)

    def test_f8_cli_toolchain_pin(self):
        argv = self.cli(**{"--toolchain-sha256": "0" * 64})
        with mock.patch.object(top, "run", return_value=(4, {"rc": 4, "status": "STALE"})) as run, mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(top.main(argv), 4)
        self.assertEqual(run.call_args.kwargs["expected_toolchain_sha256"], "0" * 64)

    def test_f8_cli_policy_budget_preserves_rc_six(self):
        with mock.patch.object(top, "read_bytes", side_effect=glb.TopologyError(6, "input_budget_exceeded")), mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(top.main(self.cli()), 6)

    def test_f9_group_is_killed_before_leader_is_reaped(self):
        real_waitid, real_killpg = os.waitid, os.killpg
        events = []
        def waitid(*args):
            result = real_waitid(*args)
            if result is not None:
                events.append(("observed", bool(args[2] & os.WNOWAIT)))
            return result
        def killpg(pid, sig):
            observed = real_waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            events.append(("kill", observed is not None))
            return real_killpg(pid, sig)
        with mock.patch.object(top.os, "waitid", waitid), mock.patch.object(top.os, "killpg", killpg):
            rc, _ = top.default_runner([sys.executable, "-I", "-c", "pass"], 3)
        self.assertEqual(rc, 0)
        self.assertIn(("observed", True), events)
        self.assertIn(("kill", True), events)

    def test_f9_process_lookup_does_not_mask_timeout(self):
        proc = mock.Mock(pid=12345)
        proc.wait.side_effect = [subprocess.TimeoutExpired("probe", .01), 0]
        with mock.patch.object(top.subprocess, "Popen", return_value=proc), \
             mock.patch.object(top.os, "waitid", return_value=None), \
             mock.patch.object(top.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(subprocess.TimeoutExpired):
                top.default_runner(["probe"], .01)

    def test_f10_no_empty_destination_reservation(self):
        calls = []
        original_mkdir = Path.mkdir
        def mkdir(path, *args, **kwargs):
            calls.append(path)
            return original_mkdir(path, *args, **kwargs)
        with mock.patch.object(Path, "mkdir", mkdir):
            rc, record = self.invoke()
        self.assertEqual(rc, 0, record)
        self.assertNotIn(self.out, calls)

    def test_f10_publication_race_preserves_caller_directory(self):
        publish = top._publish_directory
        def racing(stage, output):
            output.mkdir()
            (output / "caller").write_bytes(b"do not replace")
            publish(stage, output)
        with mock.patch.object(top, "_publish_directory", racing):
            rc, record = self.invoke()
        self.assertEqual(rc, 2, record)
        self.assertEqual((self.out / "caller").read_bytes(), b"do not replace")
        self.assertEqual(record["artifacts"], [])

    def test_f10_post_rename_fsync_failure_rolls_back(self):
        original_fsync = top.os.fsync
        def fsync(fd):
            if self.out.exists():
                raise OSError("parent sync probe")
            original_fsync(fd)
        with mock.patch.object(top.os, "fsync", fsync):
            self.refusal(2, "io_or_contract_error")

    def test_f11_identity_has_no_blender_dependency(self):
        rc, record = self.invoke(method="identity", target_faces=12, blender="no-such-topology-blender")
        self.assertEqual(rc, 0, record)

    def test_f11_lod0_is_inventory_artifact(self):
        rc, record = self.invoke()
        self.assertEqual(rc, 0, record)
        lod0 = [item for item in record["artifacts"] if item["role"] == "LOD0"]
        self.assertEqual(len(lod0), 1)
        self.assertEqual(lod0[0]["sha256"], record["candidate_sha256"])

    def test_f11_achieved_lods_decrease(self):
        def runner(argv, timeout):
            plan = json.loads(Path(argv[-1]).read_text())
            count = 4 if Path(plan["candidate"]).name == "LOD1.glb" else plan["target_faces"]
            Path(plan["candidate"]).write_bytes(base.fixture(count, marker=len(self.calls) + 1))
            self.calls.append(plan)
            return 0, "OK"
        self.refusal(5, "lod_achieved_not_decreasing", runner=runner, lod_targets=[6, 5])

    def test_f11_solver_failure_is_distinct_from_capability(self):
        self.refusal(5, "solver_failed", method="quadriflow",
                     runner=lambda *_: (2, "AF_TOPOLOGY_SOLVER_FAILED quadriflow_not_finished"))


class RepairMutationProofs(unittest.TestCase):
    def witness(self, name):
        result = unittest.TestResult()
        RepairTests(name).run(result)
        self.assertTrue(result.failures or result.errors, "targeted mutant survived: " + name)

    def test_f4_targeted_host_rules_go_red(self):
        cases = {
            "test_f4_receipt_rules": ["regression_family", "regression_raw_stale", "regression_policy_stale",
                                      "regression_failed_checks", "regression_checks_unavailable"],
            "test_f4_invalid_iou": ["iou_invalid"],
            "test_f4_commit_bindings": ["scratch_raw_stale", "regression_receipt_stale", "lod0_stale"],
            "test_f4_parameter_rules": ["seed_out_of_range", "timeout_out_of_range", "invalid_lod_targets",
                                        "quadriflow_solver_target_invalid", "target_not_reduction"],
            "test_f2_injected_evaluator_requires_explicit_test_flag": ["untrusted_evaluator"],
            "test_f2_evaluator_hash_required": ["regression_toolchain_invalid"],
            "test_f3_starved_measurement_policy": ["measurement_policy_weakened"],
            "test_f6_partial_uv_is_explicitly_unsupported": ["partial_uv_unsupported"],
            "test_f6_zeroed_uv_refused": ["uv_collapsed"],
            "test_f11_achieved_lods_decrease": ["lod_achieved_not_decreasing"],
        }
        require = top._require
        for test, reasons in cases.items():
            for ignored in reasons:
                with self.subTest(reason=ignored):
                    def disabled(condition, rc, reason, ignored=ignored):
                        if reason != ignored:
                            require(condition, rc, reason)
                    with mock.patch.object(top, "_require", disabled):
                        self.witness(test)
                    print("REPAIR MUTANT RED", ignored)

    def test_f4_targeted_glb_rules_go_red(self):
        source = inspect.getsource(glb.inspect_glb)
        reasons = ["glb_asset_version", "unsupported_glb_feature", "external_buffer_refused", "glb_buffer_length",
                   "glb_buffer_index", "glb_view_bounds", "glb_accessor_bounds",
                   "unsupported_sparse_accessor", "helper_separation_required", "unreachable_geometry",
                   "glb_node_reuse_or_cycle", "unsupported_primitive", "glb_position_type", "glb_index_type",
                   "glb_nontriangle_count", "glb_attribute_count", "external_image_refused",
                   "empty_geometry", "unsupported_uv_encoding", "glb_invalid_header", "glb_invalid_chunk",
                   "glb_embedded_chunks_required", "glb_truncated_chunk"]
        for reason in reasons:
            with self.subTest(reason=reason):
                tree = ast.parse(source)
                class Disable(ast.NodeTransformer):
                    def visit_Raise(self, node):
                        if isinstance(node.exc, ast.Call) and any(
                                isinstance(arg, ast.Constant) and arg.value == reason for arg in node.exc.args):
                            return ast.copy_location(ast.Pass(), node)
                        return node
                changed = ast.fix_missing_locations(Disable().visit(tree))
                namespace = dict(vars(glb))
                exec(compile(changed, glb.__file__, "exec"), namespace)
                with mock.patch.object(glb, "inspect_glb", namespace["inspect_glb"]):
                    self.witness("test_f4_glb_container_refusals" if reason in (
                        "glb_invalid_header", "glb_invalid_chunk", "glb_embedded_chunks_required", "glb_truncated_chunk")
                        else "test_f4_glb_refusals")
                print("REPAIR MUTANT RED", reason)


if __name__ == "__main__":
    unittest.main()
