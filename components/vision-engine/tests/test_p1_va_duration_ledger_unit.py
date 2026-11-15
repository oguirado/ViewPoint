"""Independent byte oracles and real unittest controls for proto1 durations.

The tiny dynamic unittest modules below are executed by the production
probe_module (not replaced with a fake runner or external-clock monkeypatch).
Arithmetic fixtures use deliberately supplied values, not VM measurements.
"""

import copy
import hashlib
import importlib
import sys
from pathlib import Path
import tempfile
import types
import unittest

from eval.v2eval import proto1_durations as durations


REPO = "vision-engine"
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
A = "tests.test_p1_duration_arithmetic_a"
B = "tests.test_p1_duration_arithmetic_b"


def fixture(module=A, *, elapsed=101, cpu=17, count=3, outcome="passed", caps=("cpu",)):
    return durations.make_sample(
        repo=REPO, source_commit=COMMIT, module=module,
        case_count=count, elapsed_ns=elapsed, cpu_ns=cpu,
        outcome=outcome, capabilities=list(caps),
    )


def sample_bytes(module, count, elapsed, cpu, outcome, caps, commit=COMMIT):
    """A literal-order byte oracle; never calls the production encoder."""
    cap_bytes = b",".join(b'"' + c.encode("ascii") + b'"' for c in sorted(caps))
    return (b'{"capabilities":[' + cap_bytes + b'],"case_count":' + str(count).encode()
            + b',"cpu_ns":' + str(cpu).encode() + b',"elapsed_ns":' + str(elapsed).encode()
            + b',"module":"' + module.encode() + b'","outcome":"' + outcome.encode()
            + b'","repo":"vision-engine","schema":"proto1.duration-sample/v1"'
            + b',"source_commit":"' + commit.encode() + b'"}')


class DurationLedgerUnit(unittest.TestCase):
    def refusal(self, code, fn):
        with self.assertRaises(durations.DurationRefusal) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def install_live_module(self, suffix, test_case=None):
        """Create a real importable unittest module without a disk fixture."""
        pkg = importlib.import_module("tests")
        name = "tests." + suffix
        module = types.ModuleType(name)
        if test_case is not None:
            setattr(module, test_case.__name__, test_case)
        absent = object()
        old_module = sys.modules.get(name, absent)
        old_attribute = vars(pkg).get(suffix, absent)
        sys.modules[name] = module
        setattr(pkg, suffix, module)

        def cleanup():
            if old_module is absent:
                del sys.modules[name]
            else:
                sys.modules[name] = old_module
            if old_attribute is absent:
                delattr(pkg, suffix)
            else:
                setattr(pkg, suffix, old_attribute)

        self.addCleanup(cleanup)
        return name

    def install_disk_modules(self, sources):
        """A temporary source-only package imported by a fresh subprocess."""
        tmp = tempfile.TemporaryDirectory(prefix="proto1-tiny-modules-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        package = root / "tests"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        for suffix, code in sources.items():
            (package / (suffix + ".py")).write_text(
                "import unittest\n" + code + "\n", encoding="utf-8")
        return root

    def test_canonical_exact_bytes_sorted_no_newline_and_large_int(self):
        self.assertEqual(durations.canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        value = 9223372036854775807
        self.assertEqual(durations.canonical_bytes({"n": value}),
                         b'{"n":9223372036854775807}')
        self.assertEqual(durations.decode_canonical(b'{"a":1,"b":2}'),
                         {"a": 1, "b": 2})
        self.assertEqual(durations.canonical_bytes({"label": "é"}),
                         b'{"label":"\xc3\xa9"}')

    def test_canonical_strict_parser_bad_encodings(self):
        cases = (
            (b'{"a":1,"a":2}', "E_DUPLICATE_KEY"),
            (b'{"a":1.5}', "E_JSON_TYPE"),
            (b'{"a":NaN}', "E_JSON_TYPE"),
            (b'{"a":true}', "E_JSON_TYPE"),
            (b'{"a":2}\n', "E_NONCANONICAL_JSON"),
            (b'{"b":2,"a":1}', "E_NONCANONICAL_JSON"),
            (b'{"a": 1}', "E_NONCANONICAL_JSON"),
            (b'not-json', "E_JSON_SYNTAX"),
            (b'\xff', "E_JSON_TYPE"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.refusal(code, lambda r=raw: durations.decode_canonical(r))

    def test_independent_arithmetic_sample_digest_oracle(self):
        sample = fixture()
        literal = sample_bytes(A, 3, 101, 17, "passed", ("cpu",))
        self.assertEqual(sample["sample_sha256"], hashlib.sha256(literal).hexdigest())
        self.assertEqual(len(sample["sample_sha256"]), 64)
        self.assertEqual(sample["schema"], "proto1.duration-sample/v1")
        self.assertEqual(sample["elapsed_ns"], 101)
        self.assertEqual(sample["cpu_ns"], 17)
        self.assertNotEqual(sample["elapsed_ns"], sample["cpu_ns"])
        self.assertEqual(durations.validate_sample(sample, repo=REPO, source_commit=COMMIT), sample)

    def test_independent_ledger_digest_oracle_actual_nested_records(self):
        sample = fixture()
        ledger = durations.make_ledger([sample], repo=REPO, source_commit=COMMIT,
                                       required_modules=[A])
        nested = sample_bytes(A, 3, 101, 17, "passed", ("cpu",)).replace(
            b',"schema":', b',"sample_sha256":"' + sample["sample_sha256"].encode()
            + b'","schema":', 1)
        literal = (b'{"repo":"vision-engine","required_modules":["' + A.encode() + b'"],'
                   b'"samples":[' + nested + b'],'
                   b'"schema":"proto1.duration-ledger/v1","source_commit":"'
                   + COMMIT.encode() + b'"}')
        self.assertEqual(ledger["ledger_sha256"], hashlib.sha256(literal).hexdigest())
        self.assertEqual(durations.lookup_duration(
            ledger, A, repo=REPO, source_commit=COMMIT,
            required_modules=[A], available_capabilities=["cpu"]), 101)
        self.assertEqual(ledger["samples"], [sample])

    def test_supplied_unequal_arithmetic_vectors_are_not_host_measurements(self):
        a, b = fixture(), fixture(B, elapsed=239, cpu=23, count=2)
        before = copy.deepcopy([a, b])
        ledger = durations.make_ledger([b, a], repo=REPO, source_commit=COMMIT,
                                       required_modules=[B, A])
        self.assertEqual([s["module"] for s in ledger["samples"]], [A, B])
        self.assertEqual([s["elapsed_ns"] for s in ledger["samples"]], [101, 239])
        self.assertEqual(sum(s["elapsed_ns"] for s in ledger["samples"]), 340)
        self.assertEqual([a, b], before)
        ledger["samples"][0]["elapsed_ns"] = 999
        self.assertEqual([a, b], before)

    def test_absent_sample_must_fail_and_no_default_timing(self):
        journal = durations.DurationJournal(REPO, COMMIT)
        self.refusal("E_SAMPLE_ABSENT", lambda: journal.admit([A]))
        self.assertEqual(journal.admitted_ledgers, 0)
        self.assertEqual(journal.successful_samples, 0)
        self.assertEqual(journal.observations, ())
        self.refusal("E_LEDGER_UNADMITTED", lambda: journal.lookup(
            A, available_capabilities=["cpu"]))

    def test_zero_case_count_must_fail(self):
        self.refusal("E_CASE_COUNT", lambda: fixture(count=0))
        self.refusal("E_CASE_COUNT", lambda: fixture(count=True))
        self.refusal("E_CASE_COUNT", lambda: fixture(count=1.0))
        self.refusal("E_CASE_COUNT", lambda: fixture(count=-1))

    def test_missing_duration_never_synthesized_as_zero(self):
        self.refusal("E_DURATION_MISSING", lambda: fixture(elapsed=None))
        self.refusal("E_DURATION_MISSING", lambda: fixture(cpu=None))
        sample = fixture()
        del sample["elapsed_ns"]
        self.refusal("E_DURATION_MISSING", lambda: durations.validate_sample(
            sample, repo=REPO, source_commit=COMMIT))

    def test_negative_bool_and_float_timings_are_exact_refusals(self):
        for value, code in ((-1, "E_TIME_NEGATIVE"), (True, "E_TIME_TYPE"),
                            (0.0, "E_TIME_TYPE"), ("0", "E_TIME_TYPE")):
            with self.subTest(value=value):
                self.refusal(code, lambda x=value: fixture(elapsed=x))
                self.refusal(code, lambda x=value: fixture(cpu=x))

    def test_changed_source_seal_rejects_even_correctly_rehashed_observation(self):
        sample = durations.make_sample(
            repo=REPO, source_commit=OTHER_COMMIT, module=A, case_count=1,
            elapsed_ns=100, cpu_ns=12, outcome="passed", capabilities=["cpu"])
        before = copy.deepcopy(sample)
        self.refusal("E_SOURCE_SEAL", lambda: durations.make_ledger(
            [sample], repo=REPO, source_commit=COMMIT, required_modules=[A]))
        self.assertEqual(sample, before)

    def test_conflicting_duplicate_must_fail_but_identical_is_deduplicated(self):
        a = fixture()
        changed = fixture(elapsed=102)
        before = copy.deepcopy([a, changed])
        self.refusal("E_DUPLICATE_CONFLICT", lambda: durations.make_ledger(
            [a, changed], repo=REPO, source_commit=COMMIT,
            required_modules=[A]))
        self.assertEqual([a, changed], before)
        ledger = durations.make_ledger([a, copy.deepcopy(a)], repo=REPO,
                                       source_commit=COMMIT, required_modules=[A])
        self.assertEqual(len(ledger["samples"]), 1)

    def test_failed_or_censored_offered_to_planning_is_refused(self):
        for outcome in ("failed", "censored"):
            with self.subTest(outcome=outcome):
                sample = fixture(outcome=outcome)
                before = copy.deepcopy(sample)
                self.refusal("E_UNSUCCESSFUL", lambda: durations.make_ledger(
                    [sample], repo=REPO, source_commit=COMMIT,
                    required_modules=[A]))
                self.assertEqual(sample, before)

    def test_malformed_hash_unknown_keys_and_unsorted_caps(self):
        sample = fixture()
        bad = copy.deepcopy(sample)
        bad["elapsed_ns"] += 1
        self.refusal("E_SAMPLE_HASH", lambda: durations.validate_sample(
            bad, repo=REPO, source_commit=COMMIT))
        bad = copy.deepcopy(sample)
        bad["unapproved"] = 1
        self.refusal("E_KEYS", lambda: durations.validate_sample(
            bad, repo=REPO, source_commit=COMMIT))
        bad = copy.deepcopy(sample)
        bad["capabilities"] = ["x11", "cpu"]
        self.refusal("E_CAPABILITIES", lambda: durations.validate_sample(
            bad, repo=REPO, source_commit=COMMIT))
        self.refusal("E_CAPABILITIES", lambda: fixture(caps=("cpu", "cpu")))

    def test_noncanonical_module_and_source_rejected(self):
        for name in ("../tests/test_x.py", "tests/test_x.py", "tests..test_x", "tests.other", "tests.test_X/../t"):
            with self.subTest(name=name):
                self.refusal("E_MODULE_PATH", lambda n=name: fixture(module=n))
        self.refusal("E_SOURCE_COMMIT", lambda: durations.make_sample(
            repo=REPO, source_commit=COMMIT.upper(), module=A, case_count=1,
            elapsed_ns=1, cpu_ns=1, outcome="passed", capabilities=[]))

    def test_ledger_hash_source_and_unknown_fields_rejected(self):
        ledger = durations.make_ledger([fixture()], repo=REPO,
                                       source_commit=COMMIT, required_modules=[A])
        wrong_hash = copy.deepcopy(ledger)
        wrong_hash["ledger_sha256"] = "f" * 64
        self.refusal("E_LEDGER_HASH", lambda: durations.validate_ledger(
            wrong_hash, repo=REPO, source_commit=COMMIT, required_modules=[A]))
        changed_source = copy.deepcopy(ledger)
        changed_source["source_commit"] = OTHER_COMMIT
        self.refusal("E_SOURCE_SEAL", lambda: durations.validate_ledger(
            changed_source, repo=REPO, source_commit=COMMIT, required_modules=[A]))
        unknown = copy.deepcopy(ledger)
        unknown["extra"] = []
        self.refusal("E_KEYS", lambda: durations.validate_ledger(
            unknown, repo=REPO, source_commit=COMMIT, required_modules=[A]))
        self.assertEqual(ledger["samples"][0]["elapsed_ns"], 101)

    def test_infeasible_coverage_and_empty_module_set_refused(self):
        self.refusal("E_MODULE_SET", lambda: durations.make_ledger(
            [fixture()], repo=REPO, source_commit=COMMIT, required_modules=[]))
        self.refusal("E_SAMPLE_ABSENT", lambda: durations.make_ledger(
            [fixture()], repo=REPO, source_commit=COMMIT,
            required_modules=[A, B]))
        self.refusal("E_MODULE_SET", lambda: durations.make_ledger(
            [fixture()], repo=REPO, source_commit=COMMIT,
            required_modules=[A, A]))

    def test_live_real_unittest_positive_control_with_independent_hash(self):
        class TestTiny(unittest.TestCase):
            def test_one(self):
                self.assertEqual(11 * 11, 121)

            def test_two(self):
                self.assertEqual(sum((2, 3, 4)), 9)

        module = self.install_live_module("test_p1_duration_live_good", TestTiny)
        observation = durations.probe_module(
            module, repo=REPO, source_commit=COMMIT, capabilities=["cpu"])
        sample = observation.sample
        self.assertIsNone(observation.reason)
        self.assertEqual(sample["outcome"], "passed")
        self.assertEqual(sample["case_count"], 2)
        self.assertGreater(sample["elapsed_ns"], 0)
        self.assertGreater(sample["cpu_ns"], 0)
        literal = sample_bytes(module, 2, sample["elapsed_ns"],
                               sample["cpu_ns"], "passed", ("cpu",))
        self.assertEqual(sample["sample_sha256"], hashlib.sha256(literal).hexdigest())
        journal = durations.DurationJournal(REPO, COMMIT)
        journal.record(observation)
        ledger = journal.admit([module])
        self.assertEqual(journal.admitted_ledgers, 1)
        self.assertEqual(journal.successful_samples, 1)
        self.assertEqual(journal.lookup(module, available_capabilities=["cpu"]),
                         sample["elapsed_ns"])
        ledger["samples"][0]["elapsed_ns"] = 7
        self.assertEqual(journal.lookup(module, available_capabilities=["cpu"]),
                         sample["elapsed_ns"])
        self.refusal("E_LEDGER_SEALED", lambda: journal.record(observation))

    def test_live_failed_probe_retained_with_reason_not_admitted(self):
        class TestFails(unittest.TestCase):
            def test_bad(self):
                self.assertEqual(2, 3)

        module = self.install_live_module("test_p1_duration_live_fail", TestFails)
        observation = durations.probe_module(
            module, repo=REPO, source_commit=COMMIT, capabilities=[])
        self.assertEqual(observation.sample["outcome"], "failed")
        self.assertIn("failures=1", observation.reason)
        journal = durations.DurationJournal(REPO, COMMIT)
        journal.record(observation)
        before = journal.observations
        self.refusal("E_UNSUCCESSFUL", lambda: journal.admit([module]))
        self.assertEqual(journal.admitted_ledgers, 0)
        self.assertEqual(journal.successful_samples, 0)
        self.assertEqual(journal.observations, before)
        self.assertEqual(len(journal.failed_records), 1)
        self.assertIn("failures=1", journal.failed_records[0][1])
        self.refusal("E_LEDGER_UNADMITTED", lambda: journal.lookup(
            module, available_capabilities=[]))

    def test_live_skipped_probe_censored_not_successful(self):
        class TestSkips(unittest.TestCase):
            @unittest.skip("missing live server")
            def test_skipped(self):
                self.fail("should not execute")

        module = self.install_live_module("test_p1_duration_live_skip", TestSkips)
        observation = durations.probe_module(
            module, repo=REPO, source_commit=COMMIT, capabilities=["x11"])
        self.assertEqual(observation.sample["outcome"], "censored")
        self.assertIn("skipped=1", observation.reason)
        journal = durations.DurationJournal(REPO, COMMIT)
        journal.record(observation)
        self.refusal("E_UNSUCCESSFUL", lambda: journal.admit([module]))
        self.assertEqual(journal.admitted_ledgers, 0)

    def test_live_empty_suite_refused_instead_of_zero_case_baseline(self):
        module = self.install_live_module("test_p1_duration_live_empty")
        self.refusal("E_NO_CASES", lambda: durations.probe_module(
            module, repo=REPO, source_commit=COMMIT, capabilities=[]))

    def test_unavailable_capability_prevents_execution_and_reports_obligation(self):
        visits = []

        class TestLocked(unittest.TestCase):
            def test_external(self):
                visits.append("executed")

        module = self.install_live_module("test_p1_duration_live_locked", TestLocked)
        requirements = {module: ["x11"]}
        before = copy.deepcopy(requirements)
        error = self.refusal("E_CAPABILITY_UNAVAILABLE", lambda: durations.collect_cold_start(
            requirements, repo=REPO, source_commit=COMMIT,
            available_capabilities=["cpu"]))
        self.assertEqual(error.obligations, ("x11",))
        self.assertEqual(visits, [])
        self.assertEqual(requirements, before)

    def test_real_cold_start_multiple_modules_and_capability_lookup(self):
        root = self.install_disk_modules({
            "test_p1_duration_collect_first": (
                "class TestFirst(unittest.TestCase):\n"
                "    def test_one(self):\n"
                "        self.assertEqual(7 * 8, 56)"),
            "test_p1_duration_collect_second": (
                "class TestSecond(unittest.TestCase):\n"
                "    def test_two(self):\n"
                "        self.assertEqual(5 + 9, 14)"),
        })
        module1 = "tests.test_p1_duration_collect_first"
        module2 = "tests.test_p1_duration_collect_second"
        required = {module2: ["cpu"], module1: []}
        before = copy.deepcopy(required)
        journal = durations.collect_cold_start(
            required, repo=REPO, source_commit=COMMIT,
            available_capabilities=["cpu"], module_root=root)
        self.assertEqual(required, before)
        records = journal.observations
        self.assertEqual(len(records), 2)
        self.assertEqual([record["module"] for record in records], [module1, module2])
        self.assertEqual([record["case_count"] for record in records], [1, 1])
        self.assertEqual([record["outcome"] for record in records], ["passed", "passed"])
        self.assertGreater(records[0]["elapsed_ns"], 0)
        self.assertGreater(records[0]["cpu_ns"], 0)
        self.assertEqual(journal.failed_records, ())
        oracle = sample_bytes(module1, 1, records[0]["elapsed_ns"],
                              records[0]["cpu_ns"], "passed", ())
        self.assertEqual(records[0]["sample_sha256"], hashlib.sha256(oracle).hexdigest())
        sealed = journal.admit([module2, module1])
        self.assertEqual(sealed["required_modules"], [module1, module2])
        self.assertEqual(journal.successful_samples, 2)
        self.assertEqual(journal.lookup(module1, available_capabilities=[]),
                         records[0]["elapsed_ns"])
        blocked = self.refusal("E_CAPABILITY_UNAVAILABLE", lambda: journal.lookup(
            module2, available_capabilities=[]))
        self.assertEqual(blocked.obligations, ("cpu",))

    def test_cold_interpreter_crash_never_becomes_timing_sample(self):
        root = self.install_disk_modules({
            "test_p1_duration_crash": "import os\nos._exit(7)",
        })
        self.refusal("E_PROBE_EXEC", lambda: durations.collect_cold_start(
            {"tests.test_p1_duration_crash": []}, repo=REPO,
            source_commit=COMMIT, available_capabilities=[], module_root=root))

    def test_cold_child_protocol_pollution_fails_closed(self):
        root = self.install_disk_modules({
            "test_p1_duration_pollution": (
                "import os\n"
                "class TestPollution(unittest.TestCase):\n"
                "    def test_noise(self):\n"
                "        os.write(1, b'non-JSON-output')"),
        })
        self.refusal("E_JSON_SYNTAX", lambda: durations.collect_cold_start(
            {"tests.test_p1_duration_pollution": []}, repo=REPO,
            source_commit=COMMIT, available_capabilities=[], module_root=root))

    def test_valid_zero_duration_is_distinct_from_absent_duration(self):
        sample = fixture(elapsed=0, cpu=0)
        ledger = durations.make_ledger([sample], repo=REPO, source_commit=COMMIT,
                                       required_modules=[A])
        self.assertEqual(durations.lookup_duration(
            ledger, A, repo=REPO, source_commit=COMMIT,
            required_modules=[A], available_capabilities=["cpu"]), 0)
        self.assertEqual(sample["elapsed_ns"], 0)
        self.assertEqual(sample["cpu_ns"], 0)

    def test_lookup_uncovered_module_refuses_not_zero(self):
        ledger = durations.make_ledger([fixture()], repo=REPO,
                                       source_commit=COMMIT, required_modules=[A])
        self.refusal("E_SAMPLE_ABSENT", lambda: durations.lookup_duration(
            ledger, B, repo=REPO, source_commit=COMMIT,
            required_modules=[A], available_capabilities=["cpu"]))

    def test_live_expected_failure_is_censored_not_accepted(self):
        class TestXfail(unittest.TestCase):
            @unittest.expectedFailure
            def test_expected_failure(self):
                self.assertEqual(11, 12)

        module = self.install_live_module("test_p1_duration_live_xfail", TestXfail)
        observation = durations.probe_module(
            module, repo=REPO, source_commit=COMMIT, capabilities=[])
        self.assertEqual(observation.sample["outcome"], "censored")
        self.assertIn("expected_failures=1", observation.reason)
        journal = durations.DurationJournal(REPO, COMMIT)
        journal.record(observation)
        self.refusal("E_UNSUCCESSFUL", lambda: journal.admit([module]))
        self.assertEqual(journal.admitted_ledgers, 0)
        self.assertEqual(journal.successful_samples, 0)

    def test_mixed_live_failure_and_success_never_admitted(self):
        root = self.install_disk_modules({
            "test_p1_duration_mixed_a": (
                "class TestPassed(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(7 > 2)"),
            "test_p1_duration_mixed_b": (
                "class TestFailed(unittest.TestCase):\n"
                "    def test_bad(self):\n"
                "        self.assertEqual(7, 2)"),
        })
        success = "tests.test_p1_duration_mixed_a"
        failure = "tests.test_p1_duration_mixed_b"
        journal = durations.collect_cold_start(
            {success: [], failure: []}, repo=REPO, source_commit=COMMIT,
            available_capabilities=[], module_root=root)
        self.assertEqual(len(journal.observations), 2)
        self.assertEqual([r["outcome"] for r in journal.observations], ["passed", "failed"])
        self.assertEqual(len(journal.failed_records), 1)
        prior_records = journal.observations
        self.refusal("E_UNSUCCESSFUL", lambda: journal.admit([success, failure]))
        self.assertEqual(journal.observations, prior_records)
        self.assertEqual(journal.admitted_ledgers, 0)
        self.assertEqual(journal.successful_samples, 0)

    def test_journal_detaches_probe_and_failed_record_snapshots(self):
        class TestFailed(unittest.TestCase):
            def test_bad(self):
                self.assertEqual("left", "right")

        module = self.install_live_module("test_p1_duration_detached", TestFailed)
        observation = durations.probe_module(
            module, repo=REPO, source_commit=COMMIT, capabilities=[])
        journal = durations.DurationJournal(REPO, COMMIT)
        journal.record(observation)
        original = journal.observations[0]
        observation.sample["elapsed_ns"] = 999999999
        self.assertEqual(journal.observations[0], original)
        exposed = journal.failed_records[0][0]
        exposed["cpu_ns"] = 101010
        self.assertEqual(journal.failed_records[0][0], original)
        self.refusal("E_UNSUCCESSFUL", lambda: journal.admit([module]))
        self.assertEqual(journal.admitted_ledgers, 0)

    def test_unobserved_fixture_cannot_enter_journal(self):
        journal = durations.DurationJournal(REPO, COMMIT)
        spoof = durations.ProbeResult(fixture(), None)
        self.refusal("E_PROVENANCE", lambda: journal.record(spoof))
        self.assertEqual(journal.observations, ())
        self.assertEqual(journal.admitted_ledgers, 0)
        self.assertEqual(journal.successful_samples, 0)


if __name__ == "__main__":
    unittest.main()
