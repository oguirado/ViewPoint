"""Independent A-manifest controls: exact refusals, actual wall and CPU clocks.

Synthetic fixtures prove the validator contract, not real measured performance.
Only the dedicated tiny unittest run constitutes a measured VM baseline here.
"""
import hashlib
import json
import random
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

# Import the production provider lazily, and fail (never skip) if it is absent.
def target():
    from wender.learn import proto1_durations
    return proto1_durations


PIN = "a" * 40
REPO = "example-owner/wender-agent"
CAPS = ["cpu_process_time_ns", "wall_monotonic_ns"]
KEYS = ("schema", "repo", "source_commit", "module", "case_count",
        "elapsed_ns", "cpu_ns", "outcome", "capabilities", "sample_sha256")


def independent_sample(module="tests.alpha", elapsed=7, cpu=4, count=2, **overrides):
    """Do NOT use any production sealing, canonical or digest builder."""
    values = dict(zip(KEYS, (
        "proto1.duration-sample/v1", REPO, PIN, module, count,
        elapsed, cpu, "PASS", list(CAPS), "",
    )))
    values.update(overrides)
    payload = {field: values[field] for field in KEYS[:-1]}
    values["sample_sha256"] = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return values


def declared(*modules):
    return {module: {cap: "separately observed clock availability: " + cap
                      for cap in CAPS} for module in modules}


def baseline_records():
    return [independent_sample("tests.alpha", elapsed=7, cpu=4, count=2),
            independent_sample("tests.beta", elapsed=19, cpu=12, count=1)]


def independent_ledger_bytes(records):
    ordered = sorted(records, key=lambda item: item["module"])
    names = [record["module"] for record in ordered]
    evid = declared(*names)
    body = dict(schema="proto1.duration-ledger/v1", repo=REPO,
                source_commit=PIN, required_modules=names, samples=ordered,
                capability_evidence=evid,
                case_count=sum(item["case_count"] for item in ordered),
                elapsed_ns=sum(item["elapsed_ns"] for item in ordered),
                cpu_ns=sum(item["cpu_ns"] for item in ordered))
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()
    return json.dumps({**body, "identity": digest}, ensure_ascii=False,
                      separators=(",", ":")).encode(), digest


class TestDurationLedger(unittest.TestCase):
    def refuse(self, expected, callback):
        with self.assertRaises(target().DurationRefusal) as caught:
            callback()
        self.assertEqual(caught.exception.code, expected)

    def ledger(self, samples=None, evidence=None):
        records = baseline_records() if samples is None else samples
        return target().build_duration_ledger(
            records, repo=REPO, source_commit=PIN,
            required_modules=("tests.alpha", "tests.beta"),
            declared_capabilities=(declared("tests.alpha", "tests.beta")
                                   if evidence is None else evidence),
        )

    def test_independent_sample_bytes_and_sha(self):
        record = independent_sample()
        t = target()
        self.assertEqual(t.validate_duration_sample(record, repo=REPO,
                                                    source_commit=PIN).sample_sha256,
                         record["sample_sha256"])
        self.assertEqual(t.canonical_duration_sample_bytes(record), json.dumps(
            record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(t.seal_duration_sample(repo=REPO, source_commit=PIN,
                                               module="tests.alpha", case_count=2,
                                               elapsed_ns=7, cpu_ns=4), record)

    def test_unequal_elapsed_samples_and_independent_ledger_digest(self):
        samples = baseline_records()
        ledger = self.ledger(list(reversed(samples)))
        self.assertEqual(tuple(s.elapsed_ns for s in ledger.samples), (7, 19))
        self.assertEqual((ledger.case_count, ledger.elapsed_ns, ledger.cpu_ns), (3, 26, 16))
        expected_bytes, expected_hash = independent_ledger_bytes(samples)
        self.assertEqual(ledger.identity, expected_hash)
        self.assertEqual(target().canonical_duration_ledger_bytes(ledger), expected_bytes)
        self.assertEqual(self.ledger(samples).identity, ledger.identity)

    def test_balance_conserves_exact_measured_values_without_average(self):
        ledger = self.ledger()
        plan = target().balance_duration_plan(ledger, workers=2)
        self.assertEqual(plan.groups, (("tests.beta",), ("tests.alpha",)))
        self.assertEqual(plan.elapsed_ns, (19, 7))
        self.assertEqual(plan.case_count, (1, 2))
        self.assertEqual(sum(plan.elapsed_ns), ledger.elapsed_ns)
        self.assertEqual(sum(plan.case_count), ledger.case_count)
        self.assertEqual(plan.ledger_identity, ledger.identity)

    def test_actual_unittest_baseline_uses_real_clocks(self):
        class GenuineTinyBaseline(unittest.TestCase):
            def test_real_one(self):
                self.assertEqual(3 * 7, 21)

            def test_real_two(self):
                self.assertGreater(sum((1, 2, 3, 4)), 6)

        t = target()
        from time import monotonic_ns, process_time_ns
        wall_before, cpu_before = monotonic_ns(), process_time_ns()
        record = t.collect_unittest_baseline(
            GenuineTinyBaseline, repo=REPO, source_commit=PIN,
            module="tests.real_probe")
        cpu_after, wall_after = process_time_ns(), monotonic_ns()
        self.assertEqual(record["case_count"], 2)
        self.assertEqual(record["outcome"], "PASS")
        self.assertEqual(record["capabilities"], CAPS)
        self.assertGreaterEqual(record["elapsed_ns"], 0)
        self.assertGreaterEqual(record["cpu_ns"], 0)
        self.assertLessEqual(record["elapsed_ns"], wall_after - wall_before)
        self.assertLessEqual(record["cpu_ns"], cpu_after - cpu_before)
        t.validate_duration_sample(record, repo=REPO, source_commit=PIN)
        real_evidence = {"tests.real_probe": {
            c: "VM wall and process CPU observed by actual TestCase invocation" for c in CAPS}}
        ledger = t.build_duration_ledger(
            [record], repo=REPO, source_commit=PIN,
            required_modules=("tests.real_probe",),
            declared_capabilities=real_evidence)
        self.assertEqual(ledger.elapsed_ns, record["elapsed_ns"])
        self.assertEqual(t.balance_duration_plan(ledger, workers=1).case_count, (2,))
        # Zero wall duration would be accepted here only if genuinely read as zero.

    def test_failing_skipped_xfail_xpass_and_crashed_probe_refused(self):
        class FailedTinyBaseline(unittest.TestCase):
            def test_failure(self):
                self.fail("failed probe cannot be timing evidence")

        class SkippedTinyBaseline(unittest.TestCase):
            @unittest.skip("unmeasured")
            def test_skip(self):
                self.fail("should not run")

        class ErrorTinyBaseline(unittest.TestCase):
            def test_error(self):
                raise RuntimeError("crashed probe is not baseline")

        class ExpectedFailureTinyBaseline(unittest.TestCase):
            @unittest.expectedFailure
            def test_xfail(self):
                self.fail("xfail is not a successful baseline")

        class UnexpectedSuccessTinyBaseline(unittest.TestCase):
            @unittest.expectedFailure
            def test_xpass(self):
                self.assertTrue(True)

        for klass in (FailedTinyBaseline, SkippedTinyBaseline,
                      ErrorTinyBaseline, ExpectedFailureTinyBaseline,
                      UnexpectedSuccessTinyBaseline):
            with self.subTest(case=klass.__name__):
                self.refuse("PROBE_FAILED", lambda: target().collect_unittest_baseline(
                    klass, repo=REPO, source_commit=PIN, module="tests.bad"))

    def test_absent_elapsed_not_zero_in_object_or_json(self):
        record = independent_sample()
        record.pop("elapsed_ns")
        self.refuse("DURATION_MISSING", lambda: target().validate_duration_sample(
            record, repo=REPO, source_commit=PIN))
        self.refuse("DURATION_MISSING", lambda: target().validate_duration_sample(
            json.dumps(record), repo=REPO, source_commit=PIN))
        self.refuse("DURATION_MISSING", lambda: self.ledger([record, baseline_records()[1]]))

    def test_source_pin_must_match_frozen_expectation(self):
        stale = independent_sample(source_commit="b" * 40)
        self.refuse("STALE_SOURCE_PIN", lambda: target().validate_duration_sample(
            stale, repo=REPO, source_commit=PIN))
        self.refuse("STALE_SOURCE_PIN", lambda: self.ledger([stale, baseline_records()[1]]))
        other_repo = independent_sample(repo="elsewhere/changed")
        self.refuse("STALE_SOURCE_PIN", lambda: self.ledger([other_repo, baseline_records()[1]]))

    def test_negative_elapsed_and_cpu_must_fail(self):
        for field in ("elapsed_ns", "cpu_ns"):
            bad = independent_sample(**{field: -1})
            expected = "NEGATIVE_" + field.upper()
            self.refuse(expected, lambda bad=bad: target().validate_duration_sample(
                bad, repo=REPO, source_commit=PIN))
            self.refuse(expected, lambda bad=bad: self.ledger([bad, baseline_records()[1]]))

    def test_case_count_must_be_positive_exact_integer(self):
        for value in (True, False, 0, -1, 1.0, "2"):
            with self.subTest(count=value):
                record = independent_sample(count=value)
                expected = "NEGATIVE_CASE_COUNT" if value == -1 else "INVALID_CASE_COUNT"
                self.refuse(expected, lambda record=record: target().validate_duration_sample(
                    record, repo=REPO, source_commit=PIN))

    def test_bool_or_float_duration_and_cpu_refused(self):
        for field in ("elapsed_ns", "cpu_ns"):
            for value in (True, False, 1.5):
                record = independent_sample(**{field: value})
                self.refuse("INVALID_" + field.upper(),
                            lambda record=record: self.ledger([record, baseline_records()[1]]))

    def test_failed_probe_record_cannot_become_ledger_baseline(self):
        failed = independent_sample(outcome="FAIL")
        self.refuse("PROBE_FAILED", lambda: self.ledger([failed, baseline_records()[1]]))

    def test_duplicate_json_keys_are_rejected_at_outer_and_nested_depth(self):
        valid = json.dumps(independent_sample())
        tampered = valid.replace('"elapsed_ns": 7,', '"elapsed_ns": 7, "elapsed_ns": 7,')
        self.refuse("DUPLICATE_JSON_KEY", lambda: target().parse_duration_sample_json(tampered))
        valid_caps = valid.replace('"capabilities": [', '"capabilities": [')
        self.assertEqual(target().parse_duration_sample_json(valid_caps).case_count, 2)
        raw_nested = valid.replace('"capabilities": [', '"capabilities": {"a": 1, "a": 2}, "other": [')
        self.refuse("DUPLICATE_JSON_KEY", lambda: target().parse_duration_sample_json(raw_nested))
        self.refuse("DUPLICATE_JSON_KEY", lambda: self.ledger([
            tampered, json.dumps(baseline_records()[1])]))

    def test_changed_old_digest_is_rejected_before_any_plan(self):
        sample = independent_sample()
        sample["elapsed_ns"] += 20
        self.refuse("SAMPLE_DIGEST_MISMATCH", lambda: target().validate_duration_sample(
            sample, repo=REPO, source_commit=PIN))
        self.refuse("SAMPLE_DIGEST_MISMATCH", lambda: self.ledger(
            [sample, baseline_records()[1]]))

    def test_duplicate_and_missing_corpus_samples(self):
        one = baseline_records()[0]
        self.refuse("DUPLICATE_SAMPLE", lambda: self.ledger([one, dict(one)]))
        self.refuse("DURATION_MISSING", lambda: self.ledger([one]))
        self.refuse("DURATION_MISSING", lambda: self.ledger([]))
        self.refuse("DURATION_MISSING", lambda: self.ledger(
            [one, independent_sample("tests.unexpected")]))

    def test_independent_capability_evidence_mandatory(self):
        self.refuse("CAPABILITY_EVIDENCE_MISSING", lambda: self.ledger(evidence={}))
        missing_one = declared("tests.alpha")
        self.refuse("CAPABILITY_EVIDENCE_MISSING", lambda: self.ledger(evidence=missing_one))
        missing_cap = declared("tests.alpha", "tests.beta")
        del missing_cap["tests.beta"][CAPS[0]]
        self.refuse("CAPABILITY_EVIDENCE_MISSING", lambda: self.ledger(evidence=missing_cap))
        whitespace = declared("tests.alpha", "tests.beta")
        whitespace["tests.beta"][CAPS[0]] = "   "
        self.refuse("CAPABILITY_EVIDENCE_MISSING", lambda: self.ledger(evidence=whitespace))

    def test_unmeasured_clocks_invalid_even_if_sealed(self):
        bare = independent_sample(capabilities=[])
        self.refuse("INVALID_CAPABILITIES", lambda: self.ledger([bare, baseline_records()[1]]))
        injected = independent_sample(capabilities=["injected_wall_ns"])
        self.refuse("UNMEASURED_CLOCK", lambda: self.ledger([injected, baseline_records()[1]]))

    def test_modified_ledger_refuses_plan_and_publication(self):
        ledger = self.ledger()
        compromised = replace(ledger, elapsed_ns=ledger.elapsed_ns + 1)
        self.refuse("LEDGER_INTEGRITY", lambda: target().balance_duration_plan(compromised))
        self.refuse("LEDGER_INTEGRITY", lambda: target().canonical_duration_ledger_bytes(
            compromised))
        corrupted = replace(ledger, samples=(replace(ledger.samples[0], elapsed_ns=44),
                                             ledger.samples[1]))
        self.refuse("LEDGER_INTEGRITY", lambda: target().balance_duration_plan(corrupted))
        self.refuse("LEDGER_INTEGRITY", lambda: target().canonical_duration_ledger_bytes(
            corrupted))

    def test_worker_bounds_are_strict(self):
        for invalid in (0, -1, True, 1.0):
            self.refuse("INVALID_WORKERS" if invalid != -1 else "NEGATIVE_WORKERS",
                        lambda invalid=invalid: target().balance_duration_plan(
                            self.ledger(), workers=invalid))

    def test_replay_and_race_with_barrier_and_fixed_seed(self):
        records = baseline_records()
        start = threading.Barrier(4)

        def runner(worker_index):
            rng = random.Random(1701 + worker_index)
            start.wait()
            result = []
            for _ in range(30):
                source = list(records)
                rng.shuffle(source)
                ledger = self.ledger(source)
                plan = target().balance_duration_plan(ledger, workers=2)
                result.append((ledger.identity, plan.groups, plan.elapsed_ns))
            return result

        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(runner, range(4)))
        reference = outputs[0][0]
        for output in outputs:
            self.assertEqual(output, [reference] * 30)


if __name__ == "__main__":
    unittest.main()
