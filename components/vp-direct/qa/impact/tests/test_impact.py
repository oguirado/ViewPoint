import argparse
import ast
from collections import Counter
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import measure
import runner
import selector


def imports(source):
    return tuple(node for node in ast.walk(ast.parse(source))
                 if isinstance(node, (ast.Import, ast.ImportFrom)))


class LedgerTests(unittest.TestCase):
    def test_pass_fail_skip_and_subtest_outcomes_have_cpu_and_wall(self):
        class InternalCases(unittest.TestCase):
            def test_pass(self):
                sum(range(1000))

            @unittest.skip("fixture")
            def test_skip(self):
                pass

            def test_subtest(self):
                for number in (1, 2, 3):
                    with self.subTest(number=number):
                        if number == 2:
                            self.fail("seeded assertion")

            def test_error(self):
                raise LookupError("seeded error")

        stream = io.StringIO()
        cases = unittest.defaultTestLoader.loadTestsFromTestCase(InternalCases)
        result = unittest.TextTestRunner(stream=stream, resultclass=measure.TimedResult).run(cases)
        self.assertEqual(result.testsRun, 4)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(Counter(row["result"] for row in result.rows),
                         {"pass": 1, "skip": 1, "fail": 1, "error": 1})
        self.assertTrue(all(row["duration_s"] > 0 and row["cpu_s"] >= 0 for row in result.rows))

    def test_setup_error_preserves_absent_timing(self):
        class BrokenFixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("seeded fixture error")

            def test_unreachable(self):
                pass

        stream = io.StringIO()
        cases = unittest.defaultTestLoader.loadTestsFromTestCase(BrokenFixture)
        result = unittest.TextTestRunner(stream=stream, resultclass=measure.TimedResult).run(cases)
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["result"], "error")
        self.assertIsNone(result.rows[0]["duration_s"])
        self.assertIsNone(result.rows[0]["cpu_s"])

    def test_dropped_outcome_is_rejected(self):
        class InternalCase(unittest.TestCase):
            def runTest(self):
                pass

        stream = io.StringIO()
        with patch.object(measure.TimedResult, "addSuccess", lambda *_: None):
            with self.assertRaisesRegex(RuntimeError, "missing result"):
                unittest.TextTestRunner(stream=stream,
                                        resultclass=measure.TimedResult).run(InternalCase())

    def test_dropped_failure_is_rejected(self):
        class FailingCase(unittest.TestCase):
            def runTest(self):
                self.fail("seeded assertion")

        harness = unittest.TextTestRunner(stream=io.StringIO(), resultclass=measure.TimedResult)
        self.assertEqual([row["result"] for row in harness.run(FailingCase()).rows], ["fail"])
        with patch.object(measure.TimedResult, "addFailure", unittest.TextTestResult.addFailure):
            with self.assertRaisesRegex(RuntimeError, "missing result"):
                harness.run(FailingCase())


class ShardTests(unittest.TestCase):
    def test_cpu_list_and_validation_default(self):
        with patch.object(runner, "run", return_value=0) as run:
            argv = ["shard", "--weights", "serial", "--output", "output"]
            self.assertEqual(runner.main(argv), 0)
            self.assertEqual(run.call_args.args[0].cpuset, [range(12, 16)])
            self.assertEqual(runner.main(argv + ["--cpuset", "2,4-8:2,11-12"]), 0)
            parsed = run.call_args.args[0].cpuset
            self.assertEqual([cpu for group in parsed for cpu in group], [2, 4, 6, 8, 11, 12])

    def test_invalid_cpu_list_is_rejected(self):
        for value in ("", "12,,13", "15-12", "12-15:0", "-1", "twelve"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                runner.cpu_ranges(value)

    def test_cpuset_narrows_and_verifies_effective_affinity(self):
        with (
            patch.object(runner.os, "sched_getaffinity", side_effect=[{12, 13, 14, 15}, {12, 14}]),
            patch.object(runner.os, "sched_setaffinity") as set_affinity,
            patch.object(runner.os, "nice", side_effect=[0, 19, 19]) as nice,
        ):
            self.assertEqual(runner.schedule_affinity(runner.cpu_ranges("12,14")), ([12, 14], 19))
        set_affinity.assert_called_once_with(0, {12, 14})
        self.assertEqual([call.args[0] for call in nice.call_args_list], [0, 19, 0])

    def test_cpuset_rejects_unavailable_reserved_and_clipped_affinity(self):
        for cpuset, inherited, effective, detail in (
            ("12-15", {12, 13}, {12, 13}, "outside inherited affinity"),
            ("16-19", set(range(12, 20)), set(range(16, 20)), "reserved CPUs"),
            ("12-15", set(range(12, 20)), {12, 13}, "effective CPU affinity"),
        ):
            with (
                self.subTest(cpuset=cpuset, effective=effective),
                patch.object(runner.os, "sched_getaffinity", side_effect=[inherited, effective]),
                patch.object(runner.os, "sched_setaffinity"),
                patch.object(runner.os, "nice", return_value=19),
                self.assertRaisesRegex(ValueError, detail),
            ):
                runner.schedule_affinity(runner.cpu_ranges(cpuset))

    def test_explicit_prepinned_cpu_list_can_include_reserved_cpus(self):
        cpus = {15, 16, 19}
        with (
            patch.object(runner.os, "sched_getaffinity", return_value=cpus),
            patch.object(runner.os, "sched_setaffinity") as set_affinity,
            patch.object(runner.os, "nice") as nice,
        ):
            self.assertEqual(runner.schedule_affinity(runner.cpu_ranges("15-16,19")), ([15, 16, 19], None))
        set_affinity.assert_not_called()
        nice.assert_not_called()

    def test_worker_refuses_affinity_mismatch_before_running_suites(self):
        request = Mock()
        request.read_text.return_value = '{"cpu_affinity": [12, 13], "nice": 19}'
        with (
            patch.object(runner, "verify_scope"),
            patch.object(runner.os, "sched_getaffinity", return_value={12}),
            patch.object(runner, "run_suite") as run_suite,
            self.assertRaisesRegex(ValueError, "effective CPU affinity"),
        ):
            runner.run_worker(request)
        run_suite.assert_not_called()

    def test_lpt_is_deterministic_and_uses_whole_suites(self):
        weights = {"beta": 6, "epsilon": 2, "theta": 9, "alpha": 6, "gamma": 4}
        expected = [["theta"], ["alpha", "gamma"], ["beta", "epsilon"]]
        self.assertEqual(runner.pack_lpt(weights, 3), expected)
        self.assertEqual(runner.pack_lpt(dict(reversed(list(weights.items()))), 3), expected)

    def test_partition_oracle_rejects_drop_and_duplicate_mutants(self):
        for bad in ([["a"]], [["a", "b", "b"]], [["a", "c"]]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "exactly once"):
                runner.verify_partition(("a", "b"), bad)
        runner.verify_partition(("a", "b"), [["b"], ["a"]])

    def test_full_timing_receipt_supports_subset_and_rejects_absent_weight(self):
        receipt = {"revision": "base", "mode": "collect", "rc": 0, "suites": [
            {"suite": "alpha", "duration_s": 3}, {"suite": "beta", "duration_s": 7},
        ]}
        with patch.object(runner, "read_receipt", return_value=(receipt, {})):
            with self.assertRaisesRegex(ValueError, "pinned weights revision"):
                runner.load_weights(Path("ignored"), ("beta",), "another")
            self.assertEqual(runner.load_weights(Path("ignored"), ("beta",), "base"), {"beta": 7})
            with self.assertRaisesRegex(ValueError, "missing"):
                runner.load_weights(Path("ignored"), ("gamma",), "base")

    def test_missing_or_invalid_timings_refuse_sharding(self):
        for weights, workers in (({"a": 1}, 0), ({}, 3), ({"a": float("nan")}, 2), ({"a": 0}, 2)):
            with self.subTest(weights=weights, workers=workers), self.assertRaises(ValueError):
                runner.pack_lpt(weights, workers)


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.suites = ("alpha", "beta", "gamma")
        direct = selector.DIRECT
        self.leaf = direct + "beta/leaf.py"
        self.a_test = direct + "alpha/tests/test_a.py"
        sources = {
            self.a_test: "from alpha import gateway",
            direct + "alpha/gateway.py": "from . import indirect",
            direct + "alpha/indirect.py": "from beta import leaf",
            self.leaf: "answer = 42",
            direct + "beta/tests/test_b.py": "from beta import leaf",
            direct + "gamma/tests/test_c.py": "pass",
            "data/unknown.py": "pass",
        }
        self.files = {path: ("100644", "blob", path) for path in sources}
        self.syntax = {path: imports(code) for path, code in sources.items()}
        self.graph = selector.Imports(self.files, self.syntax, self.suites)
        self.traces = {name: [] for name in self.suites}

    def select(self, path, traces=None, graphs=None):
        return selector.pick(self.suites, [path], self.files, self.files,
                             graphs or [self.graph], self.traces if traces is None else traces)

    def test_transitive_and_relative_import_select_consumer_and_owner(self):
        self.assertEqual(self.select(self.leaf)["suites"], ["alpha", "beta"])

    def test_transitive_oracle_detects_direct_only_mutant(self):
        class DirectOnly(selector.Imports):
            def closure(self, suite, observed):
                return set(self.entries[suite]).union(
                    *(self.dependencies(path) for path in self.entries[suite]))

        broken = DirectOnly(self.files, self.syntax, self.suites)
        actual = {"alpha", "beta"}
        self.assertFalse(actual - set(self.select(self.leaf)["suites"]))
        self.assertEqual(actual - set(self.select(self.leaf, graphs=[broken])["suites"]), {"alpha"})

    def test_parent_and_child_graphs_capture_disappearing_edges(self):
        changed = dict(self.syntax)
        changed[selector.DIRECT + "alpha/indirect.py"] = imports("pass")
        child = selector.Imports(self.files, changed, self.suites)
        self.assertEqual(self.select(self.leaf, graphs=[self.graph, child])["suites"], ["alpha", "beta"])

    def test_runtime_input_selects_dynamic_consumer(self):
        self.traces["gamma"] = [self.leaf]
        result = self.select(self.leaf)
        self.assertEqual(result["suites"], ["alpha", "beta", "gamma"])
        self.assertEqual(result["matches"]["gamma"], [self.leaf])

    def test_shared_schema_fixture_toolchain_and_unmapped_path_force_full(self):
        for path in ("contracts/v0.2/example.schema.json", "tests/fixtures/shared.json",
                     "components/vp-direct/config/wl0.json", "deps.lock", "data/unknown.py",
                     "new-unknown.py", "../escape.py", self.leaf):
            traces = {} if path == self.leaf else self.traces
            with self.subTest(path=path):
                result = self.select(path, traces=traces)
                self.assertEqual(result["mode"], "full")
                self.assertEqual(result["suites"], list(self.suites))

    def test_impact_path_invalidation(self):
        path = selector.DIRECT + "qa/impact/runner.py"
        self.files[path] = ("100644", "blob", "runner")
        self.syntax["runner"] = imports("pass")
        self.traces["alpha"] = [path]
        result = self.select(path)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["suites"], list(self.suites))
        self.assertIn(path, result["reason"])
        self.assertEqual(self.select(self.leaf, traces={name: [] for name in self.suites})["mode"],
                         "selected")

    def test_shadow_oracle_reports_a_missed_seeded_failure(self):
        rows = {"suites": [{"suite": "alpha", "inputs": [self.leaf]},
                           {"suite": "beta", "inputs": []}]}
        key = ("beta", "test_b.Test.test_changed")
        impact = selector.observed_impact([self.leaf], rows, rows, {key: "pass"}, {key: "fail"})
        self.assertEqual(set(impact), {"alpha", "beta"})
        self.assertEqual(impact["beta"]["changed_tests"], [key[1]])
        self.assertEqual(impact.keys() - {"alpha"}, {"beta"})


if __name__ == "__main__":
    unittest.main()
