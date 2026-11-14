"""Independent offline acceptance tests for proto1 capability admission and runner."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import threading
import unittest


COMMIT = "a932d0c9b85ae4eec7d018489354758d02d38ee9"
LEDGER = "0" * 64
FILE_PURE = "1" * 64
FILE_RAM = "2" * 64
FILE_GPU = "3" * 64


def module_spec(name, cases, kind, duration, *, capabilities=(), resources=(), sha=FILE_PURE):
    # Independent fixture declaration; not constructed using the production API.
    return {
        "module": name,
        "source_path": name.replace(".", "/") + ".py",
        "source_sha256": sha,
        "case_ids": list(cases),
        "capability_class": kind,
        "estimated_duration_ns": duration,
        "required_capabilities": list(capabilities),
        "exclusive_resources": list(resources),
    }


def fixtures():
    return [
        module_spec("tests.test_pure", (
            "tests.test_pure.Check.test_alpha", "tests.test_pure.Check.test_beta",
        ), "pure", 100),
        module_spec("tests.test_ram", (
            "tests.test_ram.Check.test_delta", "tests.test_ram.Check.test_gamma",
        ), "ram", 250, resources=("ram_large",), sha=FILE_RAM),
        module_spec("tests.test_accel", (
            "tests.test_accel.Check.test_device",
        ), "capability", 190, capabilities=("gpu_compute",),
                    resources=("gpu_queue",), sha=FILE_GPU),
    ]


def selected_for(modules):
    return sorted(c for module in modules for c in module["case_ids"])


def independent_hash(obj):
    # Independent golden digest implementation (neither production nor imports).
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def request(modules=None, **changes):
    specs = copy.deepcopy(fixtures() if modules is None else modules)
    result = dict(repo="example-owner/wender-agent", source_commit=COMMIT,
                  duration_ledger_sha256=LEDGER, modules=specs,
                  selected_case_ids=selected_for(specs),
                  capability_evidence={"gpu_compute": True},
                  requested_shards=2, seed=7201, profile="offline")
    result.update(changes)
    return result


class Acceptance(unittest.TestCase):
    def setUp(self):
        # Import the actual production owner, not a substitute or a test helper.
        self.prod = importlib.import_module("wender.learn.proto1_capabilities")

    def refuse(self, code, args):
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.plan_manifest(**args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.exit_code, 4)

    def manifest(self, **changes):
        result = self.prod.plan_manifest(**request(**changes))
        self.assertTrue(result.allowed, result.obligations)
        return result.manifest

    def test_positive_manifest_exact_order_integrity_and_identities(self):
        manifest = self.manifest()
        assert manifest is not None
        self.assertEqual(list(manifest), [
            "schema", "repo", "source_commit", "suite_sha256",
            "duration_ledger_sha256", "planner_version", "seed", "requested_shards",
            "selected_case_ids", "capabilities", "shards", "manifest_sha256",
        ])
        self.assertEqual(manifest["schema"], "proto1.suite-manifest/v1")
        self.assertEqual(manifest["capabilities"], {"gpu_compute": True})
        self.assertEqual(manifest["selected_case_ids"], selected_for(fixtures()))
        self.assertEqual(len(manifest["shards"]), 2)
        self.assertEqual(sum(s["estimated_duration_ns"] for s in manifest["shards"]), 540)
        self.assertEqual({s["resource_class"] for s in manifest["shards"]}, {"ram", "mixed"})
        seen = []
        for i, shard in enumerate(manifest["shards"]):
            self.assertEqual(list(shard), [
                "shard_id", "index", "resource_class", "modules", "case_ids",
                "estimated_duration_ns", "required_capabilities", "exclusive_resources",
                "gate_argv", "expected_outputs", "field_sha256",
            ])
            self.assertEqual(shard["shard_id"], f"shard-{i:04d}")
            self.assertEqual(shard["index"], i)
            self.assertEqual(shard["field_sha256"], independent_hash({
                k: v for k, v in shard.items() if k != "field_sha256"
            }))
            names = [m["module"] for m in shard["modules"]]
            self.assertEqual(shard["gate_argv"], ["python3", "-m", "unittest", *names])
            for m in shard["modules"]:
                seen.append(m)
                self.assertEqual(m["source_path"], m["module"].replace(".", "/") + ".py")
        seen.sort(key=lambda m: m["module"])
        self.assertEqual(seen, sorted(fixtures(), key=lambda m: m["module"]))
        independent_suite_identity = {
            "cases": selected_for(fixtures()),
            "sources": sorted((m["source_path"], m["source_sha256"]) for m in fixtures()),
            "capability_classes": sorted((m["module"], m["capability_class"]) for m in fixtures()),
        }
        self.assertEqual(manifest["suite_sha256"], independent_hash(independent_suite_identity))
        self.assertEqual(manifest["manifest_sha256"], independent_hash({
            k: v for k, v in manifest.items() if k != "manifest_sha256"
        }))
        raw = self.prod.serialize_manifest(manifest)
        self.assertFalse(raw.endswith(b"\n"))
        self.assertEqual(raw, json.dumps(manifest, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8"))
        self.assertEqual(self.prod.parse_manifest(raw), manifest)

    def test_positive_execution_reports_all_cases_and_no_skips(self):
        manifest = self.manifest()
        launches = []
        def launcher(argv, expected, ids):
            launches.append((argv, expected, ids))
            return {"returncode": 0, "case_ids": list(ids)}
        result = self.prod.execute_manifest(manifest, capability_evidence={"gpu_compute": True},
                                            profile="offline", launcher=launcher)
        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(sorted(result.executed_case_ids), selected_for(fixtures()))
        self.assertEqual(len(result.executed_case_ids), 5)
        self.assertEqual(len(result.launched_shards), 2)
        self.assertEqual(len(launches), 2)
        self.assertEqual(sorted([c for _, _, ids in launches for c in ids]), selected_for(fixtures()))

    def test_unknown_evidence_obligation_and_no_manifest(self):
        outcome = self.prod.plan_manifest(**request(capability_evidence={}))
        self.assertEqual(outcome.exit_code, 4)
        self.assertFalse(outcome.allowed)
        self.assertIsNone(outcome.manifest)
        self.assertEqual(outcome.obligations, (
            self.prod.BlockedObligation("tests.test_accel", "gpu_compute", "MISSING_CAPABILITY_EVIDENCE"),
        ))
        self.assertEqual(outcome.selected_case_ids, tuple(selected_for(fixtures())))

    def test_unknown_none_is_not_false_or_empty_resource_set(self):
        outcome = self.prod.plan_manifest(**request(capability_evidence={"gpu_compute": None}))
        self.assertEqual(outcome.obligations, (
            self.prod.BlockedObligation("tests.test_accel", "gpu_compute", "MISSING_CAPABILITY_EVIDENCE"),
        ))
        self.assertEqual(outcome.exit_code, 4)
        self.assertIsNone(outcome.manifest)
        self.assertEqual(fixtures()[-1]["exclusive_resources"], ["gpu_queue"])

    def test_multiple_unknown_capabilities_are_distinct_obligations(self):
        specs = [module_spec("tests.test_net", ["tests.test_net.Test.test_connect"],
                             "capability", 12, capabilities=("db_local", "serial_port"),
                             resources=("db_lock",))]
        outcome = self.prod.plan_manifest(**request(specs, requested_shards=1,
                                                     capability_evidence={}))
        self.assertEqual(outcome.exit_code, 4)
        self.assertIsNone(outcome.manifest)
        self.assertEqual(outcome.obligations, (
            self.prod.BlockedObligation("tests.test_net", "db_local", "MISSING_CAPABILITY_EVIDENCE"),
            self.prod.BlockedObligation("tests.test_net", "serial_port", "MISSING_CAPABILITY_EVIDENCE"),
        ))
        self.assertEqual(outcome.selected_case_ids, ("tests.test_net.Test.test_connect",))

    def test_unavailable_to_available_transition_preserves_cases(self):
        args = request(capability_evidence={"gpu_compute": False})
        before = self.prod.plan_manifest(**args)
        self.assertEqual(before.obligations, (
            self.prod.BlockedObligation("tests.test_accel", "gpu_compute", "CAPABILITY_UNAVAILABLE"),
        ))
        self.assertIsNone(before.manifest)
        args["capability_evidence"] = {"gpu_compute": True}
        after = self.prod.plan_manifest(**args)
        self.assertTrue(after.allowed)
        self.assertEqual(after.selected_case_ids, before.selected_case_ids)
        self.assertIn("tests.test_accel.Check.test_device", after.manifest["selected_case_ids"])

    def test_offline_live_only_even_if_explicitly_available(self):
        specs = [module_spec("tests.test_gui", ["tests.test_gui.Window.test_view"],
                             "capability", 5, capabilities=("live_gui",), resources=("display_0",))]
        req = request(specs, requested_shards=1, capability_evidence={"live_gui": True})
        blocked = self.prod.plan_manifest(**req)
        self.assertEqual(blocked.exit_code, 4)
        self.assertIsNone(blocked.manifest)
        self.assertEqual(blocked.obligations, (
            self.prod.BlockedObligation("tests.test_gui", "live_gui", "OFFLINE_LIVE_REQUIRED"),
        ))
        # Even live profile cannot run without independent observed evidence.
        req["profile"] = "live"
        req["capability_evidence"] = {}
        self.assertEqual(self.prod.plan_manifest(**req).obligations[0].reason,
                         "MISSING_CAPABILITY_EVIDENCE")
        req["capability_evidence"] = {"live_gui": True}
        self.assertTrue(self.prod.plan_manifest(**req).allowed)

    def test_runner_preflight_preserves_unknown_obligation_no_launch(self):
        manifest = self.manifest()
        launched = []
        def launcher(argv, expected, ids):
            launched.append(ids)
            return {"returncode": 0, "case_ids": list(ids)}
        result = self.prod.execute_manifest(manifest, capability_evidence={},
                                            profile="offline", launcher=launcher)
        self.assertEqual(result.exit_code, 4)
        self.assertFalse(result.success)
        self.assertEqual(result.executed_case_ids, ())
        self.assertEqual(result.launched_shards, ())
        self.assertEqual(launched, [])
        self.assertEqual(result.obligations, (
            self.prod.BlockedObligation("tests.test_accel", "gpu_compute", "MISSING_CAPABILITY_EVIDENCE"),
        ))

    def test_runner_all_shards_preflight_before_any_child(self):
        manifest = self.manifest()
        launched = []
        result = self.prod.execute_manifest(manifest, capability_evidence={"gpu_compute": False},
                                            profile="offline", launcher=lambda *args: launched.append(args))
        self.assertEqual(result.exit_code, 4)
        self.assertEqual(result.executed_case_ids, ())
        self.assertFalse(result.success)
        self.assertFalse(launched)
        self.assertEqual(result.obligations[0].reason, "CAPABILITY_UNAVAILABLE")

    def test_exclusive_resource_conflict_is_exact_refusal(self):
        specs = fixtures()[:2]
        specs[0]["exclusive_resources"] = ["ram_large"]
        self.refuse("EXCLUSIVE_RESOURCE_CONFLICT", request(specs, requested_shards=2))
        # Co-locating resource users within one sequential shard is feasible.
        co_located = self.prod.plan_manifest(**request(specs, requested_shards=1))
        self.assertTrue(co_located.allowed)
        self.assertEqual(co_located.manifest["shards"][0]["exclusive_resources"], ["ram_large"])

    def test_transitive_exclusive_resource_co_location(self):
        specs = fixtures()
        specs[0]["exclusive_resources"] = ["r1"]
        specs[1]["exclusive_resources"] = ["r1", "r2"]
        specs[2]["exclusive_resources"] = ["r2"]
        self.refuse("EXCLUSIVE_RESOURCE_CONFLICT", request(specs, requested_shards=2))

    def test_explicit_feasible_shards_required(self):
        self.refuse("INVALID_INTEGER", request(requested_shards=0))
        self.refuse("INVALID_INTEGER", request(requested_shards=True))
        self.refuse("INFEASIBLE_SHARDS", request(requested_shards=4))
        self.assertTrue(self.prod.plan_manifest(**request(requested_shards=3)).allowed)

    def test_bool_integers_and_negative_values_are_refused(self):
        for field, value in (("seed", True), ("seed", -1), ("requested_shards", False),
                             ("seed", 1.0), ("requested_shards", 1.0)):
            with self.subTest(field=field, value=value):
                self.refuse("INVALID_INTEGER", request(**{field: value}))
        specs = fixtures()
        specs[0]["estimated_duration_ns"] = True
        self.refuse("INVALID_INTEGER", request(specs))
        specs[0]["estimated_duration_ns"] = -7
        self.refuse("INVALID_INTEGER", request(specs))

    def test_malformed_capability_declarations_never_planned(self):
        cases = [(["gpu_compute", "gpu_compute"], "DUPLICATE_DECLARATION"),
                 (["bad capability"], "MALFORMED_DECLARATION"),
                 ([], "CAPABILITY_CLASS_MISMATCH")]
        for caps, code in cases:
            specs = fixtures()
            specs[-1]["required_capabilities"] = caps
            with self.subTest(caps=caps):
                self.refuse(code, request(specs))
        self.refuse("MALFORMED_EVIDENCE", request(capability_evidence={"gpu_compute": 1}))
        self.refuse("MALFORMED_EVIDENCE", request(capability_evidence={"bad name": True}))
        specs = fixtures()
        specs[-1]["exclusive_resources"] = ["gpu_queue", "gpu_queue"]
        self.refuse("DUPLICATE_DECLARATION", request(specs))

    def test_malformed_module_sha_commit_and_path(self):
        specs = fixtures()
        specs[1]["source_sha256"] = "g" * 64
        self.refuse("INVALID_DIGEST", request(specs))
        specs = fixtures()
        specs[1]["source_path"] = "../secret.py"
        self.refuse("MALFORMED_DECLARATION", request(specs))
        self.refuse("INVALID_DIGEST", request(source_commit="not-a-commit"))
        self.refuse("INVALID_DIGEST", request(duration_ledger_sha256="no"))

    def test_partial_and_unknown_case_rejected(self):
        partial = selected_for(fixtures())
        partial.remove("tests.test_ram.Check.test_gamma")
        self.refuse("PARTIAL_MODULE_SELECTION", request(selected_case_ids=partial))
        unknown = selected_for(fixtures()) + ["tests.test_missing.Check.test_ghost"]
        self.refuse("UNKNOWN_CASE_ID", request(selected_case_ids=unknown))

    def test_identity_includes_source_hash_and_capability_class(self):
        original = self.manifest(requested_shards=3)
        specs = fixtures()
        specs[0]["source_sha256"] = "e" * 64
        changed = self.manifest(modules=specs, requested_shards=3)
        self.assertNotEqual(original["suite_sha256"], changed["suite_sha256"])
        specs = fixtures()
        specs[0]["capability_class"] = "ram"
        changed = self.manifest(modules=specs, requested_shards=3)
        self.assertNotEqual(original["suite_sha256"], changed["suite_sha256"])

    def test_tampered_field_sha_refused_before_any_child(self):
        manifest = self.manifest()
        manifest["shards"][0]["field_sha256"] = "a" * 64
        self.assertNotEqual(manifest["shards"][0]["field_sha256"], independent_hash({
            k: v for k, v in manifest["shards"][0].items() if k != "field_sha256"}))
        launches = []
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.execute_manifest(manifest, capability_evidence={"gpu_compute": True},
                                       profile="offline", launcher=lambda *args: launches.append(args))
        self.assertEqual(caught.exception.code, "INVALID_FIELD_DIGEST")
        self.assertEqual(launches, [])

    def test_tampered_manifest_sha_refused(self):
        manifest = self.manifest()
        manifest["manifest_sha256"] = "a" * 64
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.validate_manifest(manifest)
        self.assertEqual(caught.exception.code, "INVALID_MANIFEST_DIGEST")

    def test_missing_case_detected_even_with_recomputed_shard_hash(self):
        manifest = self.manifest()
        shard = next(s for s in manifest["shards"] if len(s["case_ids"]) > 1)
        shard["case_ids"] = shard["case_ids"][:-1]
        shard["field_sha256"] = independent_hash({k: v for k, v in shard.items()
                                                  if k != "field_sha256"})
        manifest["manifest_sha256"] = independent_hash({k: v for k, v in manifest.items()
                                                       if k != "manifest_sha256"})
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.validate_manifest(manifest)
        self.assertEqual(caught.exception.code, "COVERAGE_MISMATCH")

    def test_bool_index_rejected_even_with_recomputed_digests(self):
        manifest = self.manifest()
        shard = manifest["shards"][1]
        shard["index"] = True
        shard["field_sha256"] = independent_hash({k: v for k, v in shard.items()
                                                  if k != "field_sha256"})
        manifest["manifest_sha256"] = independent_hash({k: v for k, v in manifest.items()
                                                       if k != "manifest_sha256"})
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.validate_manifest(manifest)
        self.assertEqual(caught.exception.code, "INVALID_MANIFEST")

    def test_validator_blocks_cross_shard_resource_collision_even_resealed(self):
        manifest = self.manifest(requested_shards=3)
        left, right = manifest["shards"][0:2]
        # Invent a collision and independently re-seal both digests. Validation
        # must reject the semantic conflict, not merely stale hashes.
        right["modules"][0]["exclusive_resources"] = ["resource_shared"]
        left["modules"][0]["exclusive_resources"] = ["resource_shared"]
        for shard in (left, right):
            shard["exclusive_resources"] = sorted(set(
                r for m in shard["modules"] for r in m["exclusive_resources"]))
            shard["field_sha256"] = independent_hash({k: v for k, v in shard.items()
                                                       if k != "field_sha256"})
        manifest["manifest_sha256"] = independent_hash({k: v for k, v in manifest.items()
                                                        if k != "manifest_sha256"})
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.validate_manifest(manifest)
        self.assertEqual(caught.exception.code, "EXCLUSIVE_RESOURCE_CONFLICT")

    def test_runner_returncode_without_cases_cannot_pass(self):
        manifest = self.manifest()
        result = self.prod.execute_manifest(manifest, capability_evidence={"gpu_compute": True},
                                            profile="offline", launcher=lambda *args: {
                                                "returncode": 0, "case_ids": []})
        self.assertEqual(result.exit_code, 4)
        self.assertFalse(result.success)
        self.assertEqual(result.refusal, "RUNNER_CASE_MISMATCH")

    def test_runner_bad_status_and_bad_types_fail_closed(self):
        manifest = self.manifest()
        for reported, expected_code in (({"returncode": 1, "case_ids": []}, "CHILD_FAILED"),
                                        ({"returncode": False, "case_ids": []}, "INVALID_RUNNER_RESULT")):
            with self.subTest(reported=reported):
                outcome = self.prod.execute_manifest(manifest, capability_evidence={"gpu_compute": True},
                                                     profile="offline", launcher=lambda *args: reported)
                self.assertFalse(outcome.success)
                self.assertEqual(outcome.refusal, expected_code)

    def test_json_duplicate_key_and_float_refused(self):
        manifest = self.manifest()
        raw = self.prod.serialize_manifest(manifest)
        duplicate = raw.replace(b'"seed":7201,', b'"seed":7201,"seed":7201,')
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.parse_manifest(duplicate)
        self.assertEqual(caught.exception.code, "INVALID_MANIFEST")
        floating = raw.replace(b'"seed":7201,', b'"seed":1.0,')
        with self.assertRaises(self.prod.ContractRefusal) as caught:
            self.prod.parse_manifest(floating)
        self.assertEqual(caught.exception.code, "INVALID_INTEGER")

    def test_deterministic_concurrency_with_barrier(self):
        barrier = threading.Barrier(2)
        args = request(requested_shards=3)
        def worker(_):
            barrier.wait(timeout=10)
            manifest = self.prod.plan_manifest(**copy.deepcopy(args)).manifest
            return self.prod.serialize_manifest(manifest)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertIn(b'"requested_shards":3', results[0])


if __name__ == "__main__":
    unittest.main()
