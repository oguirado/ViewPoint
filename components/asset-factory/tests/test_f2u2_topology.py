"""Synthetic contract witnesses and killed in-memory mutants; stdlib only."""
import copy
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# -I deliberately omits cwd; the test owns this explicit component import.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.blender import topology as top
from tools.blender.topology_glb import canonical, digest, inspect_glb, json_digest

POLICY = {"silhouette_iou_min_ppm": 950000, "hausdorff_max_ppm": 20000,
          "mesh_policy": {"sample_budget": 6000, "raster_resolution": 96, "timeout_s": 2}}


def fixture(faces=12, *, uv=True, material=True, marker=0):
    """Hand-built embedded GLB. Geometry assertions use synthetic receipts."""
    positions = b"".join(struct.pack("<fff", *v) for v in
                         ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)))
    indices = b"".join(struct.pack("<HHH", 0, 1, 2) for _ in range(faces))
    tex = b"".join(struct.pack("<ff", i % 2, i // 2) for i in range(4))
    binary = positions + indices
    binary += b"\0" * (-len(binary) % 4)
    tex_offset = len(binary)
    if uv:
        binary += tex
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
             {"buffer": 0, "byteOffset": len(positions), "byteLength": len(indices)}]
    accessors = [{"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": 4},
                 {"bufferView": 1, "componentType": 5123, "type": "SCALAR", "count": faces * 3}]
    attrs = {"POSITION": 0}
    if uv:
        views.append({"buffer": 0, "byteOffset": tex_offset, "byteLength": len(tex)})
        accessors.append({"bufferView": 2, "componentType": 5126, "type": "VEC2", "count": 4})
        attrs["TEXCOORD_0"] = 2
    primitive = {"attributes": attrs, "indices": 1}
    if material:
        primitive["material"] = 0
    doc = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(binary)}],
           "bufferViews": views, "accessors": accessors, "meshes": [{"primitives": [primitive]}],
           "nodes": [{"mesh": 0, "name": "surface"}], "scenes": [{"nodes": [0]}], "scene": 0,
           "extras": {"synthetic_marker": marker}}
    if material:
        doc["materials"] = [{"name": "surface", "pbrMetallicRoughness": {"metallicFactor": 0}}]
    header = json.dumps(doc, separators=(",", ":")).encode()
    header += b" " * (-len(header) % 4)
    chunks = struct.pack("<I4s", len(header), b"JSON") + header
    chunks += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, len(chunks) + 12) + chunks


def evidence(raw, candidate, *, policy):
    return 0, {"schema": "af.mesh-regression/v1", "rc": 0, "status": "OK",
               "input_sha256": digest(raw.read_bytes()), "candidate_sha256": digest(candidate.read_bytes()),
               "policy_sha256": json_digest(policy), "toolchain_sha256": "c" * 64,
               "failed": [], "unavailable": [],
               "metrics": {"raw_diag_um": 1_000_000, "hausdorff_forward_um": 100,
                           "hausdorff_backward_um": 200, "silhouette_iou_ppm": 999000}}


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.raw = self.base / "input.glb"
        self.raw.write_bytes(fixture())
        self.original = self.raw.read_bytes()
        self.out = self.base / "accepted"
        self.calls = []

    def runner(self, argv, timeout):
        self.assertEqual(argv[:len(top.SCOPE_ARGV)], top.SCOPE_ARGV)
        self.assertIn("-b", argv)
        self.assertGreater(timeout, 0)
        plan = json.loads(Path(argv[-1]).read_text())
        self.calls.append(plan)
        target = plan["target_faces"]
        if plan["method"] == "quadriflow":
            target *= 2
        Path(plan["candidate"]).write_bytes(fixture(target, marker=len(self.calls)))
        return 0, "AF_TOPOLOGY_RC 0"

    def invoke(self, **kw):
        options = dict(method="decimate", target_faces=8, seed=42, policy=copy.deepcopy(POLICY),
                       expected_source_sha256=digest(self.original), runner=self.runner, comparator=evidence,
                       allow_test_comparator=True)
        options.update(kw)
        return top.run(self.raw, self.out, **options)

    def refusal(self, rc, reason, **kw):
        code, receipt = self.invoke(**kw)
        self.assertEqual(code, rc)
        self.assertIn(reason, receipt["reason_codes"])
        self.assertEqual(receipt["artifacts"], [])
        self.assertFalse(self.out.exists())
        self.assertEqual(self.raw.read_bytes(), self.original)
        return receipt

    def test_identity_retains_raw_sha256(self):
        rc, record = self.invoke(method="identity", target_faces=12)
        self.assertEqual(rc, 0, record)
        self.assertEqual((self.out / "raw.glb").read_bytes(), self.original)
        self.assertEqual((self.out / "master.glb").read_bytes(), self.original)
        self.assertEqual((self.out / "LOD0.glb").read_bytes(), self.original)
        self.assertEqual(self.calls, [])
        self.assertEqual(record["scope"], "geometry-only")
        self.assertEqual(record["schema"], "af.topology-stage/v1")

    def test_decimate_master_regression_pass(self):
        rc, record = self.invoke()
        self.assertEqual(rc, 0, record)
        self.assertEqual(record["artifacts"][0]["requested_faces"], 8)
        self.assertEqual(inspect_glb(self.out / "master.glb")["faces"], 8)
        self.assertEqual(self.raw.read_bytes(), self.original)

    def test_quadriflow_master_regression_pass(self):
        rc, record = self.invoke(method="quadriflow")
        self.assertEqual(rc, 0, record)
        self.assertEqual(self.calls[0]["method"], "quadriflow")
        self.assertEqual(self.calls[0]["seed"], 42)
        self.assertEqual(record["parameters"]["quadriflow_target_quads"], 4)

    def test_each_lod_measured_against_raw(self):
        log = self.base / "bindings.jsonl"
        def compare(raw, candidate, *, policy):
            with log.open("a") as stream:
                stream.write(json.dumps([digest(raw.read_bytes()), candidate.name]) + "\n")
            return evidence(raw, candidate, policy=policy)
        rc, record = self.invoke(lod_targets=[6, 4], comparator=compare)
        self.assertEqual(rc, 0, record)
        bindings = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(bindings, [[digest(self.original), name] for name in ("master.glb", "LOD1.glb", "LOD2.glb")])

    def test_ladder_uses_accepted_master_as_source(self):
        rc, record = self.invoke(lod_targets=[6, 4])
        self.assertEqual(rc, 0, record)
        self.assertTrue(all(Path(p["source"]).name == "master.glb" for p in self.calls[1:]))
        self.assertEqual((self.out / "LOD0.glb").read_bytes(), (self.out / "master.glb").read_bytes())
        self.assertTrue(all(a["source_sha256"] == record["candidate_sha256"] for a in record["artifacts"][1:]))

    def test_receipt_roundtrip_and_repeatability(self):
        first, record = self.invoke()
        self.assertEqual(first, 0, record)
        stored = (self.out / "receipt.json").read_bytes()
        self.assertEqual(stored, canonical(record).encode())
        self.out = self.base / "second"
        self.calls.clear()
        rc, second = self.invoke()
        self.assertEqual(rc, 0, second)
        record.pop("wall_ms")
        second.pop("wall_ms")
        self.assertEqual(record, second)

    def test_collapsed_master_with_target_faces_rejected(self):
        def collapsed(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            report["metrics"]["silhouette_iou_ppm"] = 100000
            return rc, report
        self.refusal(5, "silhouette_regression", comparator=collapsed)

    def test_shortened_limb_rejected(self):
        def shortened(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            report["metrics"]["hausdorff_backward_um"] = 20001
            return rc, report
        self.refusal(5, "surface_regression", comparator=shortened)

    def test_forward_distance_rejected(self):
        def expanded(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            report["metrics"]["hausdorff_forward_um"] = 20001
            return rc, report
        self.refusal(5, "surface_regression", comparator=expanded)

    def test_later_lod_regression_rejected(self):
        def compare(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            if candidate.name == "LOD2.glb":
                report["metrics"]["hausdorff_backward_um"] = 30000
            return rc, report
        self.refusal(5, "surface_regression", lod_targets=[6, 4], comparator=compare)

    def test_missing_metric_cannot_pass(self):
        def missing(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            report["metrics"]["silhouette_iou_ppm"] = None
            return rc, report
        self.refusal(3, "regression_metric_missing", comparator=missing)

    def test_tampered_source_is_stale(self):
        self.refusal(4, "source_stale", expected_source_sha256="0" * 64)
        self.assertEqual(self.calls, [])

    def test_wrong_receipt_binding_is_stale(self):
        def stale(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            report["candidate_sha256"] = "0" * 64
            return rc, report
        self.refusal(4, "regression_candidate_stale", comparator=stale)

    def test_raw_output_alias_refused(self):
        self.out = self.base / "alias"
        self.out.symlink_to(self.raw)
        rc, report = self.invoke()
        self.assertEqual(rc, 2)
        self.assertIn("raw_output_alias", report["reason_codes"])
        self.assertEqual(self.raw.read_bytes(), self.original)

    def test_raw_parent_output_refused(self):
        self.out = self.base
        rc, report = self.invoke()
        self.assertEqual(rc, 2)
        self.assertIn("raw_output_alias", report["reason_codes"])

    def test_existing_destination_preserved(self):
        self.out.mkdir()
        marker = self.out / "caller"
        marker.write_bytes(b"accepted before this call")
        rc, report = self.invoke()
        self.assertEqual(rc, 2)
        self.assertIn("destination_exists", report["reason_codes"])
        self.assertEqual(marker.read_bytes(), b"accepted before this call")
        self.assertEqual(self.calls, [])

    def test_fake_lod_copy_rejected(self):
        def fake(argv, timeout):
            plan = json.loads(Path(argv[-1]).read_text())
            shutil.copyfile(plan["source"], plan["candidate"])
            return 0, "OK"
        self.refusal(5, "face_budget_unmet_or_fake_lod", runner=fake)

    def test_missing_lod_rejected(self):
        def missing(argv, timeout):
            plan = json.loads(Path(argv[-1]).read_text())
            return (0, "claimed success") if Path(plan["candidate"]).name == "LOD1.glb" else self.runner(argv, timeout)
        # Frozen INTERFACES_F0 C12 overrides the earlier NEEDS rc-5 proposal.
        self.refusal(3, "candidate_missing", lod_targets=[6], runner=missing)

    def test_texture_stripping_rejected(self):
        def stripping(argv, timeout):
            plan = json.loads(Path(argv[-1]).read_text())
            Path(plan["candidate"]).write_bytes(fixture(8, uv=False, material=False))
            return 0, "OK"
        self.refusal(5, "material_or_uv_stripped", runner=stripping)

    def test_backend_unavailable_no_fallback(self):
        self.refusal(3, "backend_unavailable", method="quadriflow",
                     runner=lambda *_: (2, "AF_TOPOLOGY_CAPABILITY quadriflow_unavailable"))

    def test_worker_failure_preserves_outputs(self):
        def failed(argv, timeout):
            self.runner(argv, timeout)
            return 2, "failed after writing candidate"
        self.refusal(2, "backend_failed", runner=failed)

    def test_worker_timeout_preserves_outputs(self):
        def timeout(argv, seconds):
            self.runner(argv, seconds)
            raise subprocess.TimeoutExpired(argv, seconds)
        self.refusal(6, "deadline_exceeded", runner=timeout)

    def test_evaluator_timeout_is_bounded(self):
        def sleeping(*args, **kwargs):
            time.sleep(10)
        start = time.monotonic()
        self.refusal(6, "deadline_exceeded", comparator=sleeping, timeout_s=1)
        self.assertLess(time.monotonic() - start, 4)

    def test_raw_changed_during_comparison_is_stale(self):
        def changing(raw, candidate, *, policy):
            self.raw.write_bytes(fixture(marker=99))
            return evidence(raw, candidate, policy=policy)
        rc, record = self.invoke(comparator=changing)
        self.assertEqual(rc, 4)
        self.assertIn("source_stale", record["reason_codes"])
        self.assertFalse(self.out.exists())

    def test_weakened_policy_refused(self):
        policy = copy.deepcopy(POLICY)
        policy["hausdorff_max_ppm"] = 20001
        self.refusal(2, "policy_weakened", policy=policy)

    def test_nonmonotonic_lod_targets_refused(self):
        self.refusal(2, "lod_budgets_not_decreasing", lod_targets=[6, 7])

    def test_bool_integer_and_float_receipt_refused(self):
        self.refusal(2, "invalid_integer", target_faces=True)
        def floating(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            report["metrics"]["raw_diag_um"] = 1.5
            return rc, report
        self.refusal(2, "regression_contract", comparator=floating)

    def test_missing_evaluator_cannot_pass(self):
        with mock.patch.object(top.importlib, "import_module", side_effect=ModuleNotFoundError):
            self.refusal(3, "regression_evaluator_unavailable", comparator=None)

    def test_toolchain_drift_refused(self):
        self.refusal(4, "toolchain_stale", expected_toolchain_sha256="0" * 64)

    def test_pinned_toolchain_passes(self):
        rc, report = self.invoke()
        self.assertEqual(rc, 0, report)
        self.out = self.base / "pinned"
        self.calls.clear()
        rc, report = self.invoke(expected_toolchain_sha256=report["toolchain_sha256"])
        self.assertEqual(rc, 0, report)

    def test_non_success_evaluator_never_promoted(self):
        for code in (2, 3, 4, 5, 6):
            with self.subTest(code=code):
                def refusal(raw, candidate, *, policy):
                    _, report = evidence(raw, candidate, policy=policy)
                    report.update(rc=code, status=top.STATUS[code])
                    return code, report
                self.refusal(code, "regression_refused", comparator=refusal)

    def test_zero_scale_cannot_pass(self):
        def zero(raw, candidate, *, policy):
            rc, report = evidence(raw, candidate, policy=policy)
            report["metrics"]["raw_diag_um"] = 0
            return rc, report
        self.refusal(3, "raw_scale_unknown", comparator=zero)

    def test_tampered_published_candidate_cannot_pass(self):
        def tamper(raw, candidate, *, policy):
            code, report = evidence(raw, candidate, policy=policy)
            candidate.write_bytes(fixture(8, marker=123))
            return code, report
        self.refusal(4, "candidate_stale", comparator=tamper)

    def test_odd_quadriflow_budget_is_explicit_error(self):
        self.refusal(2, "quadriflow_even_triangle_budget_required", method="quadriflow", target_faces=7)

    def test_missing_gate_detail_cannot_pass(self):
        def unknown(raw, candidate, *, policy):
            code, report = evidence(raw, candidate, policy=policy)
            report.pop("failed")
            return code, report
        self.refusal(2, "regression_contract", comparator=unknown)

    def test_foreign_rc_one_is_error(self):
        def foreign(raw, candidate, *, policy):
            _, report = evidence(raw, candidate, policy=policy)
            report["rc"] = 1
            return 1, report
        self.refusal(2, "regression_contract", comparator=foreign)

    def test_publication_error_preserves_absent_destination(self):
        with mock.patch.object(top.os, "replace", side_effect=OSError("injected rename failure")):
            self.refusal(2, "io_or_contract_error")

    def test_evaluator_budget_exceeded_has_rc_six(self):
        def too_large(raw, candidate, *, policy):
            code, report = evidence(raw, candidate, policy=policy)
            report["oversize"] = "x" * (2**20)
            return code, report
        self.refusal(6, "regression_budget_exceeded", comparator=too_large)

    def test_default_runner_kills_owned_group(self):
        marker = self.base / "late-write"
        script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(10)"
        child = "import time; from pathlib import Path; time.sleep(0.6); Path(%r).write_text('leaked')" % str(marker)
        with self.assertRaises(subprocess.TimeoutExpired):
            top.default_runner([sys.executable, "-I", "-c", script, child], 0.1)
        time.sleep(0.8)
        self.assertFalse(marker.exists(), "owned descendant survived timeout")

    def test_cli_usage_never_rc_one(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(top.main([]), 2)


class MutationProofs(unittest.TestCase):
    def test_temporarily_broken_rules_go_red(self):
        original_gate = top._gate_regression
        def no_reverse(rc, report, *args):
            report["metrics"]["hausdorff_backward_um"] = 0
            return original_gate(rc, report, *args)
        def no_forward(rc, report, *args):
            report["metrics"]["hausdorff_forward_um"] = 0
            return original_gate(rc, report, *args)
        def no_iou(rc, report, *args):
            report["metrics"]["silhouette_iou_ppm"] = 1000000
            return original_gate(rc, report, *args)
        require, hash_match = top._require, top._hash_match
        def ignore_hash(ignored):
            def replacement(actual, expected, reason):
                if reason != ignored:
                    hash_match(actual, expected, reason)
            return replacement
        def ignore_required(ignored):
            def replacement(condition, rc, reason):
                if reason != ignored:
                    require(condition, rc, reason)
            return replacement
        def later_lod_only(rc, report, *args):
            if report["metrics"]["hausdorff_backward_um"] == 30000:
                report["metrics"]["hausdorff_backward_um"] = 0
            return original_gate(rc, report, *args)
        mutations = [
            ("reverse-distance", "test_shortened_limb_rejected", "_gate_regression", no_reverse),
            ("forward-distance", "test_forward_distance_rejected", "_gate_regression", no_forward),
            ("face-target-bypass", "test_collapsed_master_with_target_faces_rejected", "_gate_regression", no_iou),
            ("raw-binding", "test_tampered_source_is_stale", "_hash_match", ignore_hash("source_stale")),
            ("receipt-binding", "test_wrong_receipt_binding_is_stale", "_hash_match", ignore_hash("regression_candidate_stale")),
            ("fake-reduction", "test_fake_lod_copy_rejected", "_check_reduction", lambda *a: None),
            ("material-loss", "test_texture_stripping_rejected", "_check_preservation", lambda *a: None),
            ("worker-failure", "test_worker_failure_preserves_outputs", "_check_worker", lambda *a: None),
            ("missing-metrics", "test_missing_metric_cannot_pass", "_require", ignore_required("regression_metric_missing")),
            ("cumulative-lod", "test_later_lod_regression_rejected", "_gate_regression", later_lod_only),
            ("raw-alias", "test_raw_parent_output_refused", "_check_destination", lambda *a: None),
            ("evaluator-timeout", "test_evaluator_timeout_is_bounded", "_bounded_compare",
             lambda comparator, raw, candidate, policy, timeout: evidence(raw, candidate, policy=policy)),
        ]
        require = top._require
        reasons = [
            ("missing-lod", "test_missing_lod_rejected", "candidate_missing"),
            ("existing-destination", "test_existing_destination_preserved", "destination_exists"),
            ("policy-floor", "test_weakened_policy_refused", "policy_weakened"),
            ("decreasing-budgets", "test_nonmonotonic_lod_targets_refused", "lod_budgets_not_decreasing"),
            ("zero-diagonal", "test_zero_scale_cannot_pass", "raw_scale_unknown"),
            ("quad-budget-unit", "test_odd_quadriflow_budget_is_explicit_error", "quadriflow_even_triangle_budget_required"),
            ("upstream-refusal", "test_non_success_evaluator_never_promoted", "regression_refused"),
        ]
        for label, name, ignored in reasons:
            def disabled(condition, rc, reason, ignored=ignored):
                if reason != ignored:
                    require(condition, rc, reason)
            mutations.append((label, name, "_require", disabled))
        for label, name, attr, replacement in mutations:
            with self.subTest(rule=label), mock.patch.object(top, attr, replacement):
                result = unittest.TestResult()
                TopologyTests(name).run(result)
                self.assertTrue(result.failures or result.errors, "mutant survived: " + label)
                print("MUTANT RED", label)
        with mock.patch.object(top.os, "killpg", lambda *args: None):
            result = unittest.TestResult()
            TopologyTests("test_default_runner_kills_owned_group").run(result)
            self.assertTrue(result.failures or result.errors, "process cleanup mutant survived")
            print("MUTANT RED process-group-cleanup")


if __name__ == "__main__":
    unittest.main()
