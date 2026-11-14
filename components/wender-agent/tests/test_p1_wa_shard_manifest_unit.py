"""Independent, offline positive and must-FAIL gates for the proto1 manifest owner."""

import copy
import hashlib
import importlib
import json
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor

REPO = "example-owner/wender-agent"
COMMIT = "9c418c4ab2cb4cc43af302a4c4d62639e4ce9bf7"


def independent_sha(obj):
    # Oracle uses only documented JSON encoding, not the production serializer.
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def case(module, *, klass="cpu", needs=(), exclusive=()):
    return {"case_id": module + ".Fixtures.test_one", "module": module,
            "resource_class": klass, "required_capabilities": list(needs),
            "exclusive_resources": list(exclusive),
            "source_path": module.replace(".", "/") + ".py"}


def slot(index, *, klass="cpu", cap=(), exclusive=()):
    return {"index": index, "resource_class": klass,
            "capabilities": [{"name": c, "status": "PASS",
                              "evidence": "independently declared gate " + c,
                              "source_commit": COMMIT} for c in cap],
            "exclusive_resources": list(exclusive)}


def sample(module, duration, *, warm=None, cold=None):
    return {"module": module, "duration_ns": duration,
            "warm_samples_ns": [duration] if warm is None else list(warm),
            "cold_start_samples_ns": [duration] if cold is None else list(cold)}


class ShardManifestUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Our only production import is lazy and mandatory; missing code fails.
        cls.api = importlib.import_module("wender.learn.proto1_shard_manifest")

    def fixture(self, durations=(10, 8, 7, 2), workers=None, cases=None):
        modules = ("tests.a", "tests.b", "tests.c", "tests.d")[:len(durations)]
        cases = [case(name) for name in modules] if cases is None else cases
        source_files = {c["source_path"]: hashlib.sha256(
            ("source:" + c["source_path"]).encode()).hexdigest() for c in cases}
        inventory = self.api.inventory_suite(cases=cases, source_files=source_files,
                                             repo=REPO, source_commit=COMMIT)
        measurements = [sample(name, d) for name, d in zip(modules, durations)]
        ledger = self.api.seal_duration_ledger(inventory=inventory, measurements=measurements)
        workers = [slot(0), slot(1)] if workers is None else workers
        args = dict(inventory=inventory, ledger=ledger, slots=workers,
                    current_source_files=source_files,
                    selected_case_ids=[c["case_id"] for c in cases],
                    requested_shards=len(workers), seed=19)
        return args, cases, source_files, measurements

    def refuse(self, code, callback, *, sender=None):
        events = [] if sender is None else sender
        with self.assertRaises(self.api.ManifestRefusal) as cm:
            callback()
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(events, [], "refusal must occur before queue emission")
        return str(cm.exception)

    def test_exact_independent_inventory_ledger_manifest_and_utf8_bytes(self):
        args, cases, source_files, measurements = self.fixture()
        inv = args["inventory"]
        oracle_cases = sorted(copy.deepcopy(cases), key=lambda c: c["case_id"])
        expected_suite = dict(schema="proto1.suite-inventory/v1", repo=REPO,
                              source_commit=COMMIT, cases=oracle_cases,
                              source_files=dict(sorted(source_files.items())))
        expected_suite["suite_sha256"] = independent_sha(expected_suite)
        self.assertEqual(inv, expected_suite)
        expected_ledger = dict(schema="proto1.duration-ledger/v1", repo=REPO,
                               source_commit=COMMIT,
                               suite_sha256=expected_suite["suite_sha256"],
                               modules=sorted(measurements, key=lambda x: x["module"]))
        expected_ledger["ledger_sha256"] = independent_sha(expected_ledger)
        self.assertEqual(args["ledger"], expected_ledger)
        m = self.api.plan_shards(**args)
        def expected_shard(index, names, duration):
            raw = dict(shard_id=f"shard-{index:02d}", index=index,
                       resource_class="cpu", modules=names,
                       case_ids=[n + ".Fixtures.test_one" for n in names],
                       estimated_duration_ns=duration,
                       required_capabilities=[], exclusive_resources=[],
                       gate_argv=["python3", "-m", "unittest", *names],
                       expected_outputs=["unittest:OK"])
            return dict(raw, field_sha256=independent_sha(raw))
        oracle_manifest = dict(schema="proto1.suite-manifest/v1", repo=REPO,
                               source_commit=COMMIT,
                               suite_sha256=expected_suite["suite_sha256"],
                               duration_ledger_sha256=expected_ledger["ledger_sha256"],
                               planner_version="lpt-indivisible-modules/v1",
                               seed=19, requested_shards=2,
                               selected_case_ids=sorted(c["case_id"] for c in cases),
                               capabilities=[slot(0), slot(1)],
                               shards=[expected_shard(0, ["tests.a", "tests.d"], 12),
                                       expected_shard(1, ["tests.b", "tests.c"], 15)])
        oracle_manifest["manifest_sha256"] = independent_sha(oracle_manifest)
        self.assertEqual(list(m), ["schema", "repo", "source_commit", "suite_sha256",
                                   "duration_ledger_sha256", "planner_version", "seed",
                                   "requested_shards", "selected_case_ids", "capabilities",
                                   "shards", "manifest_sha256"])
        self.assertEqual(list(m["shards"][0]), ["shard_id", "index", "resource_class",
                        "modules", "case_ids", "estimated_duration_ns", "required_capabilities",
                        "exclusive_resources", "gate_argv", "expected_outputs", "field_sha256"])
        self.assertEqual(m, oracle_manifest)
        expected_bytes = json.dumps(oracle_manifest, sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(self.api.canonical_manifest_bytes(m, **{
            k: args[k] for k in ("inventory", "ledger", "slots", "current_source_files")}), expected_bytes)
        self.assertNotIn(b"\n", expected_bytes)
        sent = []
        self.assertEqual(self.api.emit_manifest(m, sender=sent.append, **{
            k: args[k] for k in ("inventory", "ledger", "slots", "current_source_files")}), expected_bytes)
        self.assertEqual(sent, [expected_bytes])

    def test_permuted_inputs_repeat_pins_and_stable_ties(self):
        args, cases, sources, measurements = self.fixture(durations=(9, 9, 4, 4))
        first = self.api.plan_shards(**args)
        self.assertEqual([s["modules"] for s in first["shards"]],
                         [["tests.a", "tests.c"], ["tests.b", "tests.d"]])
        perm = self.api.inventory_suite(cases=list(reversed(cases)),
                                        source_files=dict(reversed(list(sources.items()))),
                                        repo=REPO, source_commit=COMMIT)
        dur = self.api.seal_duration_ledger(inventory=perm,
                                           measurements=list(reversed(measurements)))
        args2 = dict(args, inventory=perm, ledger=dur, slots=list(reversed(args["slots"])),
                     selected_case_ids=list(reversed(args["selected_case_ids"])))
        for _ in range(3):
            self.assertEqual(self.api.plan_shards(**args2), first)

    def test_independent_capability_resource_exclusivity(self):
        cases = [case("tests.a", klass="cpu"),
                 case("tests.b", klass="gpu", needs=("gpu-driver",), exclusive=("gpu0",))]
        args, _, _, _ = self.fixture(durations=(10, 8), cases=cases,
            workers=[slot(0, klass="gpu", cap=("gpu-driver",), exclusive=("gpu0",)),
                     slot(1, klass="cpu")])
        manifest = self.api.plan_shards(**args)
        self.assertEqual([s["modules"] for s in manifest["shards"]],
                         [["tests.b"], ["tests.a"]])
        self.assertEqual(manifest["shards"][0]["exclusive_resources"], ["gpu0"])
        self.assertEqual(manifest["shards"][0]["required_capabilities"], ["gpu-driver"])

    def test_matching_lookahead_keeps_all_shards_nonempty(self):
        cases = [case("tests.a"), case("tests.b", needs=("restricted",))]
        args, _, _, _ = self.fixture(durations=(10, 8), cases=cases,
            workers=[slot(0, cap=("restricted",)), slot(1)])
        manifest = self.api.plan_shards(**args)
        self.assertEqual([s["modules"] for s in manifest["shards"]],
                         [["tests.b"], ["tests.a"]])

    def test_oversized_indivisible_module_lower_bound(self):
        args, _, _, _ = self.fixture(durations=(100, 1, 1))
        manifest = self.api.plan_shards(**args)
        self.assertEqual([s["modules"] for s in manifest["shards"]],
                         [["tests.a"], ["tests.b", "tests.c"]])
        lower = self.api.plan_lower_bounds(**{k: args[k] for k in (
            "inventory", "ledger", "selected_case_ids", "requested_shards", "current_source_files")})
        self.assertEqual(lower, dict(work_ns=102, longest_module_ns=100,
                                     average_lower_bound_ns=51, makespan_lower_bound_ns=100))

    def test_concurrent_repeated_planning_stable_with_barrier(self):
        args, _, _, _ = self.fixture(durations=(9, 9, 4, 4))
        gate = threading.Barrier(5)
        def one(_):
            gate.wait()
            return [self.api.plan_shards(**args) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(one, range(5)))
        self.assertEqual(len(results), 5)
        for group in results:
            self.assertEqual(group, [results[0][0]] * 4)

    def test_actual_unittest_suite_inventory_does_not_execute_cases(self):
        invoked = []
        def probe(self):
            invoked.append(1)
        Probe = type("Probe", (unittest.TestCase,),
                     {"__module__": __name__, "test_one": probe, "test_two": probe})
        suite = unittest.TestSuite([unittest.TestSuite([Probe("test_one")]), Probe("test_two")])
        source = "tests/test_p1_wa_shard_manifest_unit.py"
        inv = self.api.inventory_unittest_suite(suite=suite,
            module_declarations={__name__: {
                "resource_class": "cpu", "required_capabilities": [],
                "exclusive_resources": [], "source_path": source}},
            source_files={source: hashlib.sha256(b"real file bytes supplied by caller").hexdigest()},
            repo=REPO, source_commit=COMMIT)
        self.assertEqual(len(inv["cases"]), 2)
        self.assertEqual(inv["cases"][0]["case_id"], __name__ + ".Probe.test_one")
        self.assertEqual(inv["cases"][1]["case_id"], __name__ + ".Probe.test_two")
        self.assertEqual(invoked, [], "inventory must not execute fixtures or tests")

    def test_reject_duplicate_case_and_empty_discovery(self):
        args, cases, sources, _ = self.fixture()
        self.refuse("DUPLICATE_CASE_ID", lambda: self.api.inventory_suite(
            cases=cases + [copy.deepcopy(cases[0])], source_files=sources,
            repo=REPO, source_commit=COMMIT))
        self.refuse("EMPTY_DISCOVERY", lambda: self.api.inventory_suite(
            cases=[], source_files=sources, repo=REPO, source_commit=COMMIT))

    def test_reject_missing_duration_and_cold_start(self):
        args, _, _, measurements = self.fixture()
        self.refuse("MISSING_DURATION", lambda: self.api.seal_duration_ledger(
            inventory=args["inventory"], measurements=measurements[:-1]))
        bad = copy.deepcopy(measurements)
        del bad[0]["duration_ns"]
        self.refuse("MISSING_DURATION", lambda: self.api.seal_duration_ledger(
            inventory=args["inventory"], measurements=bad))
        bad = copy.deepcopy(measurements)
        bad[0]["cold_start_samples_ns"] = []
        self.refuse("MISSING_COLD_START", lambda: self.api.seal_duration_ledger(
            inventory=args["inventory"], measurements=bad))
        bad = copy.deepcopy(measurements)
        bad[0]["cold_start_samples_ns"] = [100]
        self.refuse("DURATION_NOT_MEASURED", lambda: self.api.seal_duration_ledger(
            inventory=args["inventory"], measurements=bad))

    def test_reject_stale_source_and_old_ledger_seal_before_queue(self):
        args, _, _, _ = self.fixture()
        sealed = self.api.plan_shards(**args)
        changed = dict(args["current_source_files"])
        changed["tests/a.py"] = "a" * 64
        events = []
        self.refuse("STALE_SOURCE", lambda: self.api.emit_manifest(
            sealed, sender=events.append,
            **dict({k: args[k] for k in ("inventory", "ledger", "slots")},
                   current_source_files=changed)), sender=events)
        ledger = copy.deepcopy(args["ledger"])
        ledger["source_commit"] = "b" * 40
        self.refuse("STALE_SOURCE", lambda: self.api.plan_shards(**dict(args, ledger=ledger)))

    def test_reject_infeasible_counts_even_if_slots_supplied(self):
        args, _, _, _ = self.fixture()
        for value in (0, 65, True, -1):
            with self.subTest(value=value):
                self.refuse("INFEASIBLE_SHARD_COUNT",
                            lambda v=value: self.api.plan_shards(**dict(args, requested_shards=v)))
        self.refuse("INFEASIBLE_SHARD_COUNT", lambda: self.api.plan_shards(
            **dict(args, requested_shards=5, slots=[slot(i) for i in range(5)])))
        self.refuse("INFEASIBLE_SHARD_COUNT", lambda: self.api.plan_shards(
            **dict(args, slots=[slot(0)])))

    def test_reject_incompatible_resource_or_noncoverable_matching(self):
        args, _, _, _ = self.fixture()
        self.refuse("INCOMPATIBLE_RESOURCE_CLASS", lambda: self.api.plan_shards(
            **dict(args, slots=[slot(0, klass="gpu"), slot(1, klass="gpu")])))
        self.refuse("INFEASIBLE_SHARD_COUNT", lambda: self.api.plan_shards(
            **dict(args, slots=[slot(0), slot(1, klass="gpu")])))

    def test_reject_field_digest_and_manifest_digest_before_send(self):
        args, _, _, _ = self.fixture()
        original = self.api.plan_shards(**args)
        send_keys = {k: args[k] for k in ("inventory", "ledger", "slots", "current_source_files")}
        altered = copy.deepcopy(original)
        altered["shards"][0]["estimated_duration_ns"] += 1
        events = []
        self.refuse("FIELD_DIGEST_MISMATCH", lambda: self.api.emit_manifest(
            altered, sender=events.append, **send_keys), sender=events)
        altered = copy.deepcopy(original)
        altered["manifest_sha256"] = "a" * 64
        self.refuse("MANIFEST_DIGEST_MISMATCH", lambda: self.api.emit_manifest(
            altered, sender=events.append, **send_keys), sender=events)
        altered = copy.deepcopy(original)
        altered["shards"][0]["estimated_duration_ns"] += 1
        shard = altered["shards"][0]
        shard["field_sha256"] = independent_sha({k: v for k, v in shard.items() if k != "field_sha256"})
        altered["manifest_sha256"] = independent_sha({k: v for k, v in altered.items() if k != "manifest_sha256"})
        self.refuse("MANIFEST_CONTENT_MISMATCH", lambda: self.api.emit_manifest(
            altered, sender=events.append, **send_keys), sender=events)

    def test_reject_partial_module_fixtures_before_send(self):
        cases = [case("tests.a"), case("tests.b")]
        second = copy.deepcopy(cases[0]); second["case_id"] = "tests.a.Fixtures.test_two"
        cases.append(second)
        args, _, _, _ = self.fixture(durations=(10, 8), cases=cases)
        # One of the two A methods cannot be peeled away, even with a seed.
        sel = ["tests.a.Fixtures.test_two", "tests.b.Fixtures.test_one"]
        self.refuse("PARTIAL_MODULE_SELECTION", lambda: self.api.plan_shards(
            **dict(args, selected_case_ids=sel)))

    def test_reject_bool_negative_samples_noncanonical_and_unsafe_paths(self):
        args, cases, sources, measurements = self.fixture()
        self.refuse("INVALID_FIELD", lambda: self.api.plan_shards(**dict(args, seed=True)))
        bad = copy.deepcopy(measurements); bad[0]["warm_samples_ns"] = [True]
        self.refuse("INVALID_FIELD", lambda: self.api.seal_duration_ledger(
            inventory=args["inventory"], measurements=bad))
        bad = copy.deepcopy(cases); bad[0]["source_path"] = "../test.py"
        self.refuse("INVALID_FIELD", lambda: self.api.inventory_suite(
            cases=bad, source_files=sources, repo=REPO, source_commit=COMMIT))
        bad = copy.deepcopy(args["slots"]); bad[0]["capabilities"] = [dict(
            name="x", status="PASS", evidence="proof", source_commit=COMMIT, unexpected=1)]
        self.refuse("INVALID_FIELD", lambda: self.api.plan_shards(**dict(args, slots=bad)))

    def test_reject_missing_capability_stale_capability_and_ambiguous_exclusive(self):
        cases = [case("tests.a", needs=("needed",)), case("tests.b")]
        args, _, _, _ = self.fixture(durations=(10, 8), cases=cases,
                                    workers=[slot(0), slot(1)])
        self.refuse("INCOMPATIBLE_RESOURCE_CLASS", lambda: self.api.plan_shards(**args))
        bad = [slot(0, cap=("needed",)), slot(1)]
        bad[0]["capabilities"][0]["status"] = "AUSENTE"
        self.refuse("MISSING_CAPABILITY_EVIDENCE", lambda: self.api.plan_shards(
            **dict(args, slots=bad)))
        bad = [slot(0, cap=("needed",)), slot(1)]
        bad[0]["capabilities"][0]["source_commit"] = "b" * 40
        self.refuse("STALE_SOURCE", lambda: self.api.plan_shards(**dict(args, slots=bad)))
        bad = [slot(0, cap=("needed",), exclusive=("lock",)),
               slot(1, exclusive=("lock",))]
        self.refuse("AMBIGUOUS_EXCLUSIVE_RESOURCE", lambda: self.api.plan_shards(
            **dict(args, slots=bad)))

    def test_reject_duration_ledger_digest_modification(self):
        args, _, _, _ = self.fixture()
        bad = copy.deepcopy(args["ledger"])
        bad["ledger_sha256"] = "b" * 64
        self.refuse("DURATION_LEDGER_DIGEST_MISMATCH", lambda: self.api.plan_shards(
            **dict(args, ledger=bad)))

    def test_reject_duplicate_module_profile_and_untrusted_discovery_leaf(self):
        args, cases, sources, _ = self.fixture()
        other = copy.deepcopy(cases[0]); other["case_id"] = "tests.a.Fixtures.test_two"
        other["resource_class"] = "gpu"
        self.refuse("INCOMPATIBLE_RESOURCE_CLASS", lambda: self.api.inventory_suite(
            cases=cases + [other], source_files=sources, repo=REPO, source_commit=COMMIT))
        self.refuse("INVALID_DISCOVERY", lambda: self.api.inventory_unittest_suite(
            suite=unittest.TestSuite([lambda: None]),
            module_declarations={}, source_files=sources,
            repo=REPO, source_commit=COMMIT))


if __name__ == "__main__":
    unittest.main()
