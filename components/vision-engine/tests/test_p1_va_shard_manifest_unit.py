"""Independent byte-level, schedule and typed-refusal controls for A-manifest."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eval.v2eval import proto1_shard_manifest as planner


COMMIT = "a" * 40
A = "tests.test_alpha"
B = "tests.test_beta"
C = "tests.test_gamma"
A1 = A + ".Alpha.test_one"
A2 = A + ".SecondAlpha.test_two"
B1 = B + ".Beta.test_one"
C1 = C + ".Gamma.test_one"
ALL_CASES = [A1, A2, B1, C1]
SOURCE_FILES = {
    "tests/test_alpha.py": b"class Alpha:\n    def test_one(self): pass\nclass SecondAlpha:\n    def test_two(self): pass\n",
    "tests/test_beta.py": b"class Beta:\n    def test_one(self): pass\n",
    "tests/test_gamma.py": b"class Gamma:\n    def test_one(self): pass\n",
}
HA = hashlib.sha256(SOURCE_FILES["tests/test_alpha.py"]).hexdigest()
HB = hashlib.sha256(SOURCE_FILES["tests/test_beta.py"]).hexdigest()
HC = hashlib.sha256(SOURCE_FILES["tests/test_gamma.py"]).hexdigest()

# Explicit, hand-assembled sorted byte strings. These are NOT emitted by the
# production encoder and are not obtained by decoding the production manifest.
# Their SHA-256 digests are separately checked against pinned hexadecimal values.
SUITE_CANON = (
    '{"modules":['
    '{"case_ids":["' + A1 + '","' + A2 + '"],"exclusive_resources":[],"module":"' + A + '",'
    '"path":"tests/test_alpha.py","required_capabilities":["python"],"resource_class":"cpu",'
    '"source_sha256":"' + HA + '"},'
    '{"case_ids":["' + B1 + '"],"exclusive_resources":[],"module":"' + B + '",'
    '"path":"tests/test_beta.py","required_capabilities":["python"],"resource_class":"cpu",'
    '"source_sha256":"' + HB + '"},'
    '{"case_ids":["' + C1 + '"],"exclusive_resources":[],"module":"' + C + '",'
    '"path":"tests/test_gamma.py","required_capabilities":["python"],"resource_class":"cpu",'
    '"source_sha256":"' + HC + '"}'
    '],"repo":"vision-engine","selected_case_ids":["' + A1 + '","' + A2 + '","' + B1 + '","' + C1 + '"]'
    ',"source_commit":"' + COMMIT + '"}'
).encode("utf-8")
LEDGER_CANON = (
    '{"samples":['
    '{"duration_ns":13,"module":"' + A + '","sample_id":"t1","source_sha256":"' + HA + '"},'
    '{"duration_ns":7,"module":"' + B + '","sample_id":"t2","source_sha256":"' + HB + '"},'
    '{"duration_ns":3,"module":"' + C + '","sample_id":"t3","source_sha256":"' + HC + '"}'
    ']}'
).encode("utf-8")
SUITE_SHA = hashlib.sha256(SUITE_CANON).hexdigest()
LEDGER_SHA = hashlib.sha256(LEDGER_CANON).hexdigest()

SHARD0_CANON = (
    '{"case_ids":["' + A1 + '","' + A2 + '"],"estimated_duration_ns":13,"exclusive_resources":[],'
    '"expected_outputs":["artifacts/p1_va/shard-000/receipt.json"],'
    '"gate_argv":["python3","-m","unittest","' + A + '"],"index":0,"modules":["' + A + '"],'
    '"required_capabilities":["python"],"resource_class":"cpu","shard_id":"shard-000"}'
).encode("utf-8")
SHARD1_CANON = (
    '{"case_ids":["' + B1 + '","' + C1 + '"],"estimated_duration_ns":10,"exclusive_resources":[],'
    '"expected_outputs":["artifacts/p1_va/shard-001/receipt.json"],'
    '"gate_argv":["python3","-m","unittest","' + B + '","' + C + '"],'
    '"index":1,"modules":["' + B + '","' + C + '"],'
    '"required_capabilities":["python"],"resource_class":"cpu","shard_id":"shard-001"}'
).encode("utf-8")
S0 = hashlib.sha256(SHARD0_CANON).hexdigest()
S1 = hashlib.sha256(SHARD1_CANON).hexdigest()

# Emitted wire order is prescribed (not alphabetical). The digest domain is
# alphabetical and excludes its own digest field.
SHARD0_EMITTED = (
    '{"shard_id":"shard-000","index":0,"resource_class":"cpu","modules":["' + A + '"],'
    '"case_ids":["' + A1 + '","' + A2 + '"],"estimated_duration_ns":13,'
    '"required_capabilities":["python"],"exclusive_resources":[],'
    '"gate_argv":["python3","-m","unittest","' + A + '"],'
    '"expected_outputs":["artifacts/p1_va/shard-000/receipt.json"],"field_sha256":"' + S0 + '"}'
)
SHARD1_EMITTED = (
    '{"shard_id":"shard-001","index":1,"resource_class":"cpu","modules":["' + B + '","' + C + '"],'
    '"case_ids":["' + B1 + '","' + C1 + '"],"estimated_duration_ns":10,'
    '"required_capabilities":["python"],"exclusive_resources":[],'
    '"gate_argv":["python3","-m","unittest","' + B + '","' + C + '"],'
    '"expected_outputs":["artifacts/p1_va/shard-001/receipt.json"],"field_sha256":"' + S1 + '"}'
)
SHARD0_CANON_WITH_HASH = SHARD0_CANON.decode("utf-8").replace(
    ',"gate_argv":', ',"field_sha256":"' + S0 + '","gate_argv":')
SHARD1_CANON_WITH_HASH = SHARD1_CANON.decode("utf-8").replace(
    ',"gate_argv":', ',"field_sha256":"' + S1 + '","gate_argv":')
MANIFEST_CANON = (
    '{"capabilities":["python"],"duration_ledger_sha256":"' + LEDGER_SHA + '",'
    '"planner_version":"p1-va-lpt/v1","repo":"vision-engine","requested_shards":2,'
    '"schema":"proto1.suite-manifest/v1","seed":42,"selected_case_ids":["' + A1 + '","' + A2 + '","' + B1 + '","' + C1 + '"],'
    '"shards":[' + SHARD0_CANON_WITH_HASH + ',' + SHARD1_CANON_WITH_HASH + '],"source_commit":"' + COMMIT + '",'
    '"suite_sha256":"' + SUITE_SHA + '"}'
).encode("utf-8")
MANIFEST_SHA = hashlib.sha256(MANIFEST_CANON).hexdigest()
GOLDEN_WIRE = (
    '{"schema":"proto1.suite-manifest/v1","repo":"vision-engine","source_commit":"' + COMMIT + '",'
    '"suite_sha256":"' + SUITE_SHA + '","duration_ledger_sha256":"' + LEDGER_SHA + '",'
    '"planner_version":"p1-va-lpt/v1","seed":42,"requested_shards":2,'
    '"selected_case_ids":["' + A1 + '","' + A2 + '","' + B1 + '","' + C1 + '"],"capabilities":["python"],'
    '"shards":[' + SHARD0_EMITTED + ',' + SHARD1_EMITTED + '],"manifest_sha256":"' + MANIFEST_SHA + '"}'
).encode("utf-8")


def fixture():
    """A fresh caller-owned request and raw source snapshot for each test."""
    inventory = []
    for module, path, digest, ids in (
            (A, "tests/test_alpha.py", HA, [A1, A2]),
            (B, "tests/test_beta.py", HB, [B1]),
            (C, "tests/test_gamma.py", HC, [C1])):
        inventory.append({"module": module, "path": path, "source_sha256": digest,
                          "case_ids": ids, "resource_class": "cpu",
                          "required_capabilities": ["python"], "exclusive_resources": []})
    samples = [
        {"sample_id": "t1", "module": A, "duration_ns": 13, "source_sha256": HA},
        {"sample_id": "t2", "module": B, "duration_ns": 7, "source_sha256": HB},
        {"sample_id": "t3", "module": C, "duration_ns": 3, "source_sha256": HC},
    ]
    req = {
        "repo": "vision-engine", "source_commit": COMMIT, "seed": 42,
        "requested_shards": 2, "selected_case_ids": ALL_CASES.copy(),
        "inventory": inventory, "suite_sha256": SUITE_SHA,
        "duration_ledger": {"samples": samples}, "duration_ledger_sha256": LEDGER_SHA,
        "available_capabilities": ["python"],
        "shard_slots": [
            {"slot_id": "worker-a", "resource_class": "cpu",
             "capabilities": ["python"], "exclusive_resources": []},
            {"slot_id": "worker-b", "resource_class": "cpu",
             "capabilities": ["python"], "exclusive_resources": []},
        ],
    }
    return req, SOURCE_FILES.copy()


def independent_suite_seal(request):
    # Re-seal a deliberately altered inventory in resource-class tests only.
    modules = sorted(request["inventory"], key=lambda item: item["module"])
    normalized = []
    for item in modules:
        normalized.append({**item,
                           "case_ids": sorted(item["case_ids"]),
                           "required_capabilities": sorted(item["required_capabilities"]),
                           "exclusive_resources": sorted(item["exclusive_resources"])})
    data = {"repo": "vision-engine", "source_commit": request["source_commit"],
            "selected_case_ids": sorted(request["selected_case_ids"]), "modules": normalized}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def independent_ledger_seal(samples):
    # Only non-golden special cases call this external, stdlib serializer.
    data = {"samples": sorted(samples, key=lambda sample: (sample["module"], sample["sample_id"]))}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class ManifestBehavior(unittest.TestCase):
    def assert_refusal(self, code, change, *, snapshots=None, blocked=()):
        request, sources = fixture()
        source_before = sources.copy()
        change(request)
        after_change = copy.deepcopy(request)
        files_arg = sources if snapshots is None else snapshots
        snapshot_copy = files_arg.copy()
        with tempfile.TemporaryDirectory() as temp:
            sentinel = Path(temp) / "registration_state.json"
            sentinel.write_bytes(b'{"registered":0,"reserved":0,"published":0,"sent":0}')
            initial = sentinel.read_bytes()
            initial_files = sorted(item.name for item in Path(temp).iterdir())
            with mock.patch("subprocess.run") as run, mock.patch("urllib.request.urlopen") as network:
                with self.assertRaises(planner.ManifestRefusal) as caught:
                    planner.build_manifest(request, source_files=files_arg)
                run.assert_not_called()
                network.assert_not_called()
            self.assertEqual(initial, sentinel.read_bytes())
            self.assertEqual(initial_files, sorted(item.name for item in Path(temp).iterdir()))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.blocked_case_ids, blocked)
        self.assertEqual(request, after_change)  # validator did not mutate caller-owned inputs
        self.assertEqual(sources, source_before)
        self.assertEqual(files_arg, snapshot_copy)

    def test_golden_exact_bytes_hashes_and_bounds(self):
        # Independent precomputed constants, not derived from the production code.
        self.assertEqual(hashlib.sha256(SUITE_CANON).hexdigest(), EXPECTED_SHA_SUITE)
        self.assertEqual(hashlib.sha256(LEDGER_CANON).hexdigest(), EXPECTED_SHA_LEDGER)
        self.assertEqual(hashlib.sha256(SHARD0_CANON).hexdigest(), EXPECTED_SHA_SHARD0)
        self.assertEqual(hashlib.sha256(SHARD1_CANON).hexdigest(), EXPECTED_SHA_SHARD1)
        self.assertEqual(hashlib.sha256(MANIFEST_CANON).hexdigest(), EXPECTED_SHA_MANIFEST)
        request, files = fixture()
        request_copy = copy.deepcopy(request)
        plan = planner.build_manifest(request, source_files=files)
        self.assertEqual(plan.manifest, GOLDEN_WIRE)
        self.assertFalse(plan.manifest.endswith(b"\n"))
        self.assertEqual(plan.longest_module_lower_bound_ns, 13)
        self.assertEqual(plan.average_load_lower_bound_ns, 12)
        self.assertEqual(plan.makespan_lower_bound_ns, 13)
        self.assertEqual(plan.estimated_makespan_ns, 13)
        self.assertEqual(plan.oversized_modules, (A,))
        self.assertEqual(request, request_copy)
        self.assertEqual(files, SOURCE_FILES)

    def test_exact_disjoint_coverage_and_intact_module_class_fixture(self):
        request, files = fixture()
        plan = planner.build_manifest(request, source_files=files)
        obj = json.loads(plan.manifest)
        self.assertEqual([s["modules"] for s in obj["shards"]], [[A], [B, C]])
        self.assertEqual([s["estimated_duration_ns"] for s in obj["shards"]], [13, 10])
        self.assertEqual(sorted(x for s in obj["shards"] for x in s["case_ids"]), ALL_CASES)
        self.assertEqual(len([x for s in obj["shards"] for x in s["case_ids"]]), len(ALL_CASES))
        self.assertEqual(obj["shards"][0]["case_ids"], [A1, A2])
        self.assertEqual(obj["shards"][0]["gate_argv"], ["python3", "-m", "unittest", A])
        self.assertEqual(obj["shards"][1]["gate_argv"], ["python3", "-m", "unittest", B, C])
        self.assertEqual(obj["shards"][1]["expected_outputs"], ["artifacts/p1_va/shard-001/receipt.json"])
        self.assertEqual(list(obj), list(planner._MANIFEST_KEYS))
        self.assertEqual(list(obj["shards"][0]), list(planner._SHARD_KEYS))

    def test_permuting_all_unordered_inputs_is_byte_identical(self):
        req, sources = fixture()
        expected = planner.build_manifest(req, source_files=sources).manifest
        req["inventory"].reverse()
        req["inventory"][2]["case_ids"].reverse()
        req["selected_case_ids"].reverse()
        req["duration_ledger"]["samples"].reverse()
        req["shard_slots"].reverse()
        req = dict(reversed(list(req.items())))
        sources = dict(reversed(list(sources.items())))
        self.assertEqual(planner.build_manifest(req, source_files=sources).manifest, expected)
        self.assertEqual(planner.build_manifest(json.dumps(req).encode(), source_files=sources).manifest, expected)

    def test_validator_exact_bytes_and_tamper(self):
        request, sources = fixture()
        good = planner.build_manifest(request, source_files=sources)
        self.assertEqual(planner.verify_manifest(good.manifest, request, source_files=sources), good)
        self.assertEqual(planner.ManifestRefusal.__bases__, (ValueError,))
        tampered = good.manifest.replace(b'"seed":42', b'"seed":43')
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.verify_manifest(tampered, request, source_files=sources)
        self.assertEqual(caught.exception.code, "E_MANIFEST_TAMPER")
        duplicate = good.manifest.replace(b'"seed":42', b'"seed":42,"seed":42')
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.verify_manifest(duplicate, request, source_files=sources)
        self.assertEqual(caught.exception.code, "E_DUPLICATE_KEY")

    def test_refuses_duplicate_cases(self):
        self.assert_refusal("E_DUPLICATE_CASE", lambda req: req["inventory"][0]["case_ids"].append(A1))

    def test_refuses_absent_duration(self):
        self.assert_refusal("E_DURATION_MISSING", lambda req: req["duration_ledger"]["samples"].pop())

    def test_refuses_changed_source_under_old_seal(self):
        request, files = fixture()
        edited = files.copy()
        edited["tests/test_alpha.py"] += b"# changed source\n"
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.build_manifest(request, source_files=edited)
        self.assertEqual(caught.exception.code, "E_SOURCE_MISMATCH")
        self.assertEqual(files, SOURCE_FILES)
        self.assertEqual(request, fixture()[0])

    def test_refuses_resealed_source_with_old_suite_digest(self):
        request, files = fixture()
        edited = files.copy()
        edited["tests/test_alpha.py"] += b"# changed source\n"
        request["inventory"][0]["source_sha256"] = hashlib.sha256(edited["tests/test_alpha.py"]).hexdigest()
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.build_manifest(request, source_files=edited)
        self.assertEqual(caught.exception.code, "E_SUITE_SEAL")

    def test_refuses_impossible_shard_count(self):
        self.assert_refusal("E_SHARDS", lambda req: req.__setitem__("requested_shards", 4))

    def test_refuses_incompatible_resources(self):
        self.assert_refusal("E_RESOURCE_INCOMPATIBLE", lambda req: [slot.__setitem__("resource_class", "gpu")
                                                                 for slot in req["shard_slots"]])

    def test_refuses_bool_shard_count(self):
        self.assert_refusal("E_TYPE", lambda req: req.__setitem__("requested_shards", True))

    def test_refuses_empty_discovery(self):
        self.assert_refusal("E_EMPTY", lambda req: req.__setitem__("inventory", []))

    def test_refuses_unknown_fields_top_and_nested(self):
        self.assert_refusal("E_UNKNOWN_FIELD", lambda req: req.__setitem__("unexpected", "value"))
        self.assert_refusal("E_UNKNOWN_FIELD", lambda req: req["inventory"][0].__setitem__("extra", 3))
        self.assert_refusal("E_UNKNOWN_FIELD", lambda req: req["duration_ledger"]["samples"][0].__setitem__("extra", 3))
        self.assert_refusal("E_UNKNOWN_FIELD", lambda req: req["shard_slots"][0].__setitem__("extra", 3))

    def test_refuses_wrong_exact_coverage(self):
        self.assert_refusal("E_CASE_COVERAGE", lambda req: req["selected_case_ids"].pop())
        self.assert_refusal("E_CASE_COVERAGE", lambda req: req["selected_case_ids"].append(
            "tests.foreign.Class.test_foreign"))
        self.assert_refusal("E_CASE_COVERAGE", lambda req: req["inventory"][0]["case_ids"].__setitem__(
            0, "tests.foreign.Class.test_foreign"))

    def test_refuses_noncanonical_path_and_bad_snapshot(self):
        self.assert_refusal("E_PATH", lambda req: req["inventory"][0].__setitem__(
            "path", "tests/../test_alpha.py"))
        req, files = fixture()
        files.pop("tests/test_alpha.py")
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.build_manifest(req, source_files=files)
        self.assertEqual(caught.exception.code, "E_SOURCE_SNAPSHOT")

    def test_refuses_duplicate_json_keys_and_floats(self):
        req, files = fixture()
        raw = json.dumps(req, separators=(",", ":"))
        duplicate = raw.replace('"seed":42', '"seed":42,"seed":42')
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.build_manifest(duplicate, source_files=files)
        self.assertEqual(caught.exception.code, "E_DUPLICATE_KEY")
        self.assert_refusal("E_TYPE", lambda request: request.__setitem__("seed", 42.0))
        self.assert_refusal("E_TYPE", lambda request: request["duration_ledger"]["samples"][0].__setitem__(
            "duration_ns", True))

    def test_refuses_ledger_change_under_old_ledger_digest(self):
        self.assert_refusal("E_LEDGER_SEAL", lambda req: req["duration_ledger"]["samples"][1].__setitem__(
            "duration_ns", 8))

    def test_live_unavailable_capability_remains_blocked(self):
        self.assert_refusal("E_CAPABILITY_BLOCKED", lambda req: req.__setitem__(
            "available_capabilities", []), blocked=tuple(ALL_CASES))

    def test_ties_place_alphabetical_modules_then_slot_index(self):
        req, files = fixture()
        for sample in req["duration_ledger"]["samples"]:
            sample["duration_ns"] = 7
        req["duration_ledger_sha256"] = independent_ledger_seal(req["duration_ledger"]["samples"])
        out = json.loads(planner.build_manifest(req, source_files=files).manifest)
        self.assertEqual([s["modules"] for s in out["shards"]], [[A, C], [B]])
        self.assertEqual([s["estimated_duration_ns"] for s in out["shards"]], [14, 7])

    def test_oversized_module_kept_intact(self):
        req, files = fixture()
        req["duration_ledger"]["samples"][0]["duration_ns"] = 100
        req["duration_ledger"]["samples"][1]["duration_ns"] = 1
        req["duration_ledger"]["samples"][2]["duration_ns"] = 1
        req["duration_ledger_sha256"] = independent_ledger_seal(req["duration_ledger"]["samples"])
        p = planner.build_manifest(req, source_files=files)
        out = json.loads(p.manifest)
        self.assertEqual([s["estimated_duration_ns"] for s in out["shards"]], [100, 2])
        self.assertEqual(p.longest_module_lower_bound_ns, 100)
        self.assertEqual(p.average_load_lower_bound_ns, 51)
        self.assertEqual(p.makespan_lower_bound_ns, 100)
        self.assertEqual(p.oversized_modules, (A,))
        self.assertEqual(out["shards"][0]["case_ids"], [A1, A2])

    def test_multiple_duration_samples_integer_median_and_seal(self):
        req, files = fixture()
        samples = req["duration_ledger"]["samples"]
        samples.extend([{"sample_id": "t4", "module": A, "duration_ns": 14, "source_sha256": HA},
                        {"sample_id": "t5", "module": B, "duration_ns": 9, "source_sha256": HB}])
        req["duration_ledger_sha256"] = independent_ledger_seal(samples)
        out = json.loads(planner.build_manifest(req, source_files=files).manifest)
        self.assertEqual([s["estimated_duration_ns"] for s in out["shards"]], [14, 11])
        # Alpha upper-rounded median (13+14+1)//2=14; beta median (7+9)//2=8; gamma=3.

    def test_resource_classes_and_exclusive_ownership(self):
        req, files = fixture()
        req["shard_slots"][0]["exclusive_resources"] = ["gpu0"]
        req["shard_slots"][1]["exclusive_resources"] = ["gpu0"]
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.build_manifest(req, source_files=files)
        self.assertEqual(caught.exception.code, "E_RESOURCE_CONFLICT")

    def test_admits_distinct_capability_classes_and_exclusive_resources(self):
        req, files = fixture()
        req["inventory"][0]["resource_class"] = "gpu"
        req["inventory"][0]["required_capabilities"] = ["gpu", "python"]
        req["inventory"][0]["exclusive_resources"] = ["gpu0"]
        req["suite_sha256"] = independent_suite_seal(req)
        req["available_capabilities"] = ["python", "gpu"]
        req["shard_slots"][0] = {"slot_id": "worker-a", "resource_class": "gpu",
                                 "capabilities": ["python", "gpu"],
                                 "exclusive_resources": ["gpu0"]}
        obj = json.loads(planner.build_manifest(req, source_files=files).manifest)
        self.assertEqual(obj["shards"][0]["resource_class"], "gpu")
        self.assertEqual(obj["shards"][0]["exclusive_resources"], ["gpu0"])
        self.assertEqual(obj["shards"][0]["required_capabilities"], ["gpu", "python"])
        self.assertEqual(obj["shards"][0]["modules"], [A])
        self.assertEqual(obj["shards"][1]["resource_class"], "cpu")
        self.assertEqual(obj["shards"][1]["modules"], [B, C])
        self.assertEqual(obj["capabilities"], ["gpu", "python"])

    def test_refuses_range_and_missing_keys(self):
        self.assert_refusal("E_RANGE", lambda req: req.__setitem__("requested_shards", 0))
        self.assert_refusal("E_RANGE", lambda req: req.__setitem__("requested_shards", 65))
        self.assert_refusal("E_MISSING_FIELD", lambda req: req.pop("requested_shards"))
        self.assert_refusal("E_TYPE", lambda req: req.__setitem__("source_commit", "A" * 40))

    def test_no_empty_shards_when_slot_compatibility_forces_packing(self):
        req, files = fixture()
        req["shard_slots"][0]["capabilities"] = ["python"]
        req["shard_slots"][1]["capabilities"] = []
        with self.assertRaises(planner.ManifestRefusal) as caught:
            planner.build_manifest(req, source_files=files)
        self.assertEqual(caught.exception.code, "E_SHARDS_EMPTY")


# Pinned independently computed constants are inserted below once, by an
# external offline checksum step before the gate is executed.
EXPECTED_SHA_SUITE = "e524d861a94e972f047e13da154b07c50c2b15d72ea88d416835ff6831c5ecb8"
EXPECTED_SHA_LEDGER = "1bf4dacbdc687100ccbf4d395614514605d4405c2fdf111a4f245547614c73f5"
EXPECTED_SHA_SHARD0 = "f5aab3901edcc9bc00c9e51b949abbaaac24ead7d236976ca389404853818a6a"
EXPECTED_SHA_SHARD1 = "031f4e2c9b09e79b6779a6fb66cbd75dc297a19bfdafedccd6d68cee655dc880"
EXPECTED_SHA_MANIFEST = "397478a7f1e6f439da7befaf89a1f5c7f0da3b82b7d8469db6868618c2465e58"


class SemanticMutationControls(unittest.TestCase):
    """Fresh behavior suites must fail by assertion, not import/error/skip."""

    @staticmethod
    def _execute(names):
        suite = unittest.TestSuite(ManifestBehavior(name) for name in names)
        result = unittest.TestResult()
        suite.run(result)
        return result

    def test_semantic_mutants_killed_then_restored(self):
        names = ["test_golden_exact_bytes_hashes_and_bounds",
                 "test_exact_disjoint_coverage_and_intact_module_class_fixture"]
        baseline = self._execute(names)
        self.assertEqual(baseline.testsRun, 2)
        self.assertEqual(len(baseline.failures), 0)
        self.assertEqual(len(baseline.errors), 0)
        self.assertEqual(len(baseline.skipped), 0)
        mutants = (
            ("reverse-slot-tie", "_choose_slot",
             lambda candidates, loads: min(candidates, key=lambda index: (loads[index], -index))),
            ("forged-field-hash", "_digest", None),
            ("sorted-wire-fields", "_json_bytes", None),
        )
        killed = 0
        for name, function_name, replacement in mutants:
            if name == "forged-field-hash":
                # Alter shard hash only; retain input seals, so controls reach
                # a semantic byte mismatch rather than an unrelated exception.
                original = planner._digest
                def tamper_shard(value):
                    if type(value) is dict and "shard_id" in value and "gate_argv" in value:
                        return "0" * 64
                    return original(value)
                replacement = tamper_shard
            if name == "sorted-wire-fields":
                original_wire = planner._json_bytes
                def emit_sorted_wire(value, *, sort_keys):
                    return original_wire(value, sort_keys=True)
                replacement = emit_sorted_wire
            with mock.patch.object(planner, function_name, replacement):
                mutation = self._execute(names)
            self.assertEqual(mutation.testsRun, 2, name)
            self.assertEqual(len(mutation.errors), 0, name)
            self.assertEqual(len(mutation.skipped), 0, name)
            self.assertGreater(len(mutation.failures), 0, name)
            killed += 1
        self.assertEqual((killed, len(mutants)), (3, 3))
        restored = self._execute(names)
        self.assertEqual(restored.testsRun, 2)
        self.assertEqual(len(restored.failures), 0)
        self.assertEqual(len(restored.errors), 0)
        self.assertEqual(len(restored.skipped), 0)
