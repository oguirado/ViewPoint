"""Discriminating controls for edit generation, probe activation, and scoring."""

from contextlib import redirect_stderr
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
AUDITED_REVISION = "c86979ca"
SWEEP_REVISION = "cf6c7e085293203343b10e595cf2ec52062c84ee"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE.parent / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mutation = load("mutate")
probe = load("probe_strict")

SOURCE = b"""def decide(count, ready):
    label = "HOLD"
    if count >= 2 and ready:
        return "NO_LEASE"
    return label
"""
COMPARE_SOURCE = b"def decide(a, b):\n    return a != b\n"


def load_audited(name):
    path = (HERE.parent / f"{name}.py").relative_to(mutation.REPO)
    module = ModuleType(f"audited_{name}")
    module.__file__ = str(mutation.REPO / path)
    source = mutation.git("show", f"{AUDITED_REVISION}:{path}")
    with patch.dict(sys.modules, {module.__name__: module}):
        exec(compile(source, str(path), "exec"), vars(module))
    return module


def run_probe(module, config, tests):
    arguments = ["discover", "-s", str(tests), "-p", "test_*.py", "-q"]
    with (
        patch.object(module, "scope_receipt", return_value={}),
        patch.object(sys, "path", sys.path[:]),
        redirect_stderr(StringIO()),
    ):
        try:
            result, rc = module.run(config, arguments)
        finally:
            sys.modules.pop("test_toy", None)
    return {**result, "rc": rc}


def write_fixture_test(directory, body):
    directory.mkdir(parents=True)
    (directory / "test_toy.py").write_text(f"""import runpy, unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "toy.py"
toy = runpy.run_path(str(source))

class ToyCase(unittest.TestCase):
    def test_boundary(self):
{body}
""", encoding="utf-8")


class MutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audited_probe = load_audited("probe_strict")
        cls.audited_mutation = load_audited("mutate")

    def compare_probes(self, original, family, test_body, expected):
        with TemporaryDirectory(dir=HERE) as directory:
            root = Path(directory)
            source = root / "toy.py"
            source.write_bytes(original)
            tests = root / "tests"
            write_fixture_test(tests, test_body)
            edit, = [row for row in mutation.candidates(source.name, original) if row.family == family]
            config = {"root": directory, "watch": {source.name: [edit.line]}, "mutant": None, "cpus": []}
            baseline = run_probe(probe, config, tests)
            self.assertEqual(baseline["rc"], 0)
            self.assertEqual((baseline["tests"], baseline["failures"], baseline["errors"]), (1, 0, 0))
            changed = edit.apply(original)
            source.write_bytes(changed)
            config.update(mutant=vars(edit), sha256=mutation.sha256(changed).hexdigest())
            old = run_probe(self.audited_probe, config, tests)
            new = run_probe(probe, config, tests)
            for run in (old, new):
                self.assertEqual((run["rc"], run["tests"], run["failures"], run["errors"]), (1, 1, 1, 0))
            self.assertEqual(self.audited_mutation.disposition(baseline, old), "killed")
            self.assertEqual(mutation.disposition(baseline, new), expected)
            self.assertEqual(new["activation_kind"], "instruction")
            return new

    def test_ast_operators_are_local_and_discriminating(self):
        expected = {
            "compare": (2, True, "HOLD"),
            "boolean": (0, True, "NO_LEASE"),
            "off_by_one": (2, True, "HOLD"),
            "remove_guard": (2, True, "HOLD"),
            "return_code": (2, True, "HOLD"),
        }
        baseline = {}
        exec(SOURCE, baseline)
        rows = mutation.candidates("source.py", SOURCE)
        self.assertEqual({row.family for row in rows}, set(expected))
        for row in rows:
            with self.subTest(row=row):
                count, ready, answer = expected[row.family]
                mutated = {}
                changed = row.apply(SOURCE)
                exec(changed, mutated)
                self.assertEqual(mutated["decide"](count, ready), answer)
                self.assertNotEqual(baseline["decide"](count, ready), answer)
                self.assertEqual(len(SOURCE.splitlines()), len(changed.splitlines()))

    def test_stale_or_uncovered_edit_cannot_pass_selection(self):
        edits = mutation.candidates("source.py", SOURCE)
        with self.assertRaisesRegex(ValueError, "stale edit"):
            edits[0].apply(SOURCE.replace(b">=", b"=="))
        self.assertEqual(mutation.select(edits, {}, 10), [])
        select = mutation.select(edits, {"source.py": [3, 4]}, 10)
        self.assertEqual(select, mutation.select(reversed(edits), {"source.py": [3, 4]}, 10))
        self.assertEqual(len({row.line for row in select}), len(select))

    def test_short_circuited_boolean_does_not_score_as_kill(self):
        run = self.compare_probes(b"def decide(a, b, c):\n    return a and (b or c)\n", "boolean", """
        self.assertFalse(toy["decide"](False, True, False))
        self.assertIn(b"b or c", source.read_bytes())""", "unactivated")
        self.assertEqual(run["hit_cases"], [])

    def test_raising_left_operand_does_not_score_as_kill(self):
        run = self.compare_probes(b"def decide(check, ready):\n    return check() and ready\n", "boolean", """
        def check():
            raise LookupError("before the boolean decision")
        with self.assertRaises(LookupError):
            toy["decide"](check, True)
        self.assertIn(b"and ready", source.read_bytes())""", "unactivated")
        self.assertEqual(run["hit_cases"], [])

    def test_reached_boolean_condition_scores_as_kill(self):
        run = self.compare_probes(b"""def decide(a, b, c):
    if a and (b or c):
        return True
    return False
""", "boolean", """
        self.assertTrue(toy["decide"](True, True, False))""", "killed")
        self.assertEqual(run["activated_failed_cases"], ["test_toy.ToyCase.test_boundary"])

    def test_sibling_subtest_failure_does_not_score_as_kill(self):
        for second_label in (1, 2):
            with self.subTest(second_label=second_label):
                run = self.compare_probes(COMPARE_SOURCE, "compare", f"""
        with self.subTest(value=1):
            self.assertIn(b"!=", source.read_bytes())
        with self.subTest(value={second_label}):
            toy["decide"](1, 2)""", "inconclusive")
                case = "test_toy.ToyCase.test_boundary"
                self.assertEqual(run["failed_cases"], [f"{case} (value=1)"])
                self.assertEqual(run["hit_cases"], [f"{case} (value={second_label})"])
                self.assertEqual(run["activated_failed_cases"], [])

    def test_same_subtest_failure_scores_as_kill(self):
        run = self.compare_probes(COMPARE_SOURCE, "compare", """
        with self.subTest(value=1):
            self.assertTrue(toy["decide"](1, 2))""", "killed")
        self.assertEqual(run["activated_failed_cases"], ["test_toy.ToyCase.test_boundary (value=1)"])

    def test_outer_multiline_comparison_does_not_score_as_inner_kill(self):
        run = self.compare_probes(b"""def decide(flag, y, a, b):
    return (
        y if flag else (a != b)
    ) is not None
""", "compare", """
        self.assertTrue(toy["decide"](True, 0, 1, 2))
        self.assertIn(b"a != b", source.read_bytes())""", "unactivated")
        self.assertEqual(run["hit_cases"], [])

    def test_reached_folded_guard_scores_as_kill(self):
        run = self.compare_probes(b"""def decide(ready):
    if ready:
        return True
""", "remove_guard", """
        self.assertTrue(toy["decide"](True))""", "killed")
        self.assertEqual(run["activated_failed_cases"], ["test_toy.ToyCase.test_boundary"])

    def test_folded_guard_in_loop_still_activates(self):
        run = self.compare_probes(b"""def decide(values):
    for value in values:
        if value:
            return True
    return False
""", "remove_guard", """
        self.assertTrue(toy["decide"]([True]))""", "killed")
        self.assertEqual(run["activated_failed_cases"], ["test_toy.ToyCase.test_boundary"])

    def test_none_identity_comparison_tracks_specialized_branch_instruction(self):
        source = b"def f(path):\n    if path is None:\n        return 1\n    return 2\n"
        edit = next(row for row in mutation.candidates("toy.py", source) if row.family == "compare")
        changed = edit.apply(source)
        code = next(code for code in probe.code_objects(changed, "toy.py") if code.co_name == "f")
        self.assertTrue(probe.instruction_offsets(code, vars(edit), probe.activation_spans(changed, vars(edit))))

    def test_multiline_comparison_instruction_contains_edit_on_its_last_line(self):
        source = b"def f(resolve):\n    return resolve(\n        'fixture') is not None\n"
        edit = next(row for row in mutation.candidates("toy.py", source) if row.family == "compare")
        changed = edit.apply(source)
        code = next(code for code in probe.code_objects(changed, "toy.py") if code.co_name == "f")
        self.assertEqual(edit.line, 3)
        self.assertTrue(probe.instruction_offsets(code, vars(edit), probe.activation_spans(changed, vars(edit))))

    def test_probe_and_oracle_require_activation_and_discriminating_failure(self):
        with TemporaryDirectory(dir=HERE) as directory:
            root = Path(directory)
            source = root / "toy.py"
            source.write_bytes(SOURCE)
            tests = root / "tests"
            tests.mkdir()
            test = tests / "test_toy.py"
            test.write_text(f"""import runpy, unittest
toy = runpy.run_path({str(source)!r})
class ToyCase(unittest.TestCase):
    def test_boundary(self):
        self.assertEqual(toy["decide"](2, True), "NO_LEASE")
""")
            watch = {"toy.py": [2, 3, 4]}
            config = {"root": directory, "watch": watch, "mutant": None, "cpus": []}
            baseline = run_probe(probe, config, tests)
            self.assertEqual(baseline["rc"], 0)
            self.assertEqual(baseline["coverage"], watch)
            for family, expected in (("compare", "killed"), ("remove_guard", "killed"), ("boolean", "survived")):
                with self.subTest(family=family):
                    edit = next(row for row in mutation.candidates("toy.py", SOURCE) if row.family == family)
                    source.write_bytes(edit.apply(SOURCE))
                    config["mutant"] = vars(edit)
                    config["sha256"] = mutation.sha256(source.read_bytes()).hexdigest()
                    result = run_probe(probe, config, tests)
                    self.assertEqual(result["hit_cases"], ["test_toy.ToyCase.test_boundary"])
                    self.assertEqual(mutation.disposition(baseline, result), expected)
            # Same filename and line coordinates, but different code from disk.
            # The in-memory control must not count as activation.
            test.write_text(f"""import unittest
scope = {{}}
exec(compile({SOURCE!r}, {str(source)!r}, "exec"), scope)
class ToyCase(unittest.TestCase):
    def test_boundary(self):
        self.assertEqual(scope["decide"](2, True), "NO_LEASE")
""")
            result = run_probe(probe, config, tests)
            self.assertEqual(result["hit_cases"], [])
            self.assertEqual(mutation.disposition(baseline, result), "unactivated")
            result["tests"] = 0
            self.assertEqual(mutation.disposition(baseline, result), "invalid")
            config["sha256"] = "wrong"
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                run_probe(probe, config, tests)

    def test_snapshot_is_pinned_and_isolated(self):
        file = mutation.DIRECT / "channel/core/capabilities.py"
        checkout = mutation.REPO / file
        pinned = mutation.git("show", f"{SWEEP_REVISION}:{file}")
        checkout_digest = mutation.sha256(checkout.read_bytes()).hexdigest()
        with TemporaryDirectory(dir=HERE) as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            mutation.snapshot(SWEEP_REVISION, first)
            self.assertEqual((first / file).read_bytes(), pinned)
            self.assertFalse((first / mutation.DIRECT / "qa").exists())
            (first / file).write_bytes(b"modified copy\n")
            mutation.snapshot(SWEEP_REVISION, second)
            self.assertEqual((second / file).read_bytes(), pinned)
        self.assertEqual(mutation.sha256(checkout.read_bytes()).hexdigest(), checkout_digest)

    def test_measure_keeps_sources_and_publishes_unsplit_receipt(self):
        surfaces = ("channel/core", "control/core")
        with TemporaryDirectory(dir=HERE) as directory:
            work = Path(directory)
            fixture = work / "fixture"
            output = work / "complete.json"
            for surface in surfaces:
                folder = fixture / mutation.DIRECT / surface
                source = folder / "toy.py"
                write_fixture_test(folder / "tests", """
        with self.subTest(value=1):
            self.assertIn(b"!=", source.read_bytes())
        with self.subTest(value=2):
            toy["decide"](1, 2)""")
                source.write_bytes(COMPARE_SOURCE)
            safe_run = fixture / mutation.DIRECT / "channel/core/tests/safe_run.sh"
            safe_run.write_text("#!/bin/sh\nexec systemd-run " + " ".join(mutation.CAP)
                                + ' -- python3 -I -B -m unittest discover -s "$(dirname "$0")"\n', encoding="utf-8")
            initial = {path: mutation.sha256(path.read_bytes()).hexdigest()
                       for path in fixture.rglob("*") if path.is_file()}
            snapshots = []

            def copy_snapshot(revision, destination):
                self.assertEqual(revision, SWEEP_REVISION)
                self.assertFalse(output.exists())
                snapshots.append(destination)
                shutil.copytree(fixture, destination)

            cpus = sorted(os.sched_getaffinity(0))
            with patch.object(mutation, "snapshot", side_effect=copy_snapshot):
                report = mutation.measure(SWEEP_REVISION, surfaces, 1, 30, output, cpus=cpus)
            self.assertEqual(report, json.loads(output.read_text()))
            self.assertIsNone(report["requested_mutant_id"])
            probe_digest = mutation.sha256((HERE.parent / "probe_strict.py").read_bytes()).hexdigest()
            self.assertEqual(report["probe_sha256"], probe_digest)
            self.assertEqual(set(report["surfaces"]), set(surfaces))
            for row in report["surfaces"].values():
                self.assertEqual(row["baseline"]["rc"], 0)
                self.assertEqual(row["selected_ids"], [1])
                mutant, = row["mutants"]
                self.assertEqual(mutant["status"], "inconclusive")
                self.assertEqual(mutant["suite"]["activated_failed_cases"], [])
                self.assertEqual(mutant["suite"]["limits"]["cpus"], cpus)
            self.assertEqual(len(snapshots), 4)
            self.assertTrue(all(not path.exists() for path in snapshots))
            current = {path: mutation.sha256(path.read_bytes()).hexdigest() for path in initial}
            self.assertEqual(current, initial)
