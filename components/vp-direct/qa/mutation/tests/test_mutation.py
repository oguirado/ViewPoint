"""Discriminating controls for edit generation, probe activation, and scoring."""

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / (name + ".py"))
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


class MutationTests(unittest.TestCase):
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

    def test_subtest_failure_matches_its_activated_test_method(self):
        baseline = {"tests": 2, "skipped": 0, "fixed_outcomes": 0}
        run = dict(baseline, rc=1, failures=1, errors=0, hit_cases=["test.Case.test_guard"],
                   failed_cases=["test.Case.test_guard (value=2)"])
        self.assertEqual(mutation.disposition(baseline, run), "killed")
        run["failed_cases"] = ["test.Case.test_other (value=2)"]
        self.assertEqual(mutation.disposition(baseline, run), "inconclusive")

    def test_none_identity_comparison_tracks_specialized_branch_instruction(self):
        source = b"def f(path):\n    if path is None:\n        return 1\n    return 2\n"
        edit = next(row for row in mutation.candidates("toy.py", source) if row.family == "compare")
        code = next(code for code in probe.code_objects(edit.apply(source), "toy.py") if code.co_name == "f")
        self.assertTrue(probe.instruction_offsets(code, vars(edit)))

    def test_multiline_comparison_instruction_contains_edit_on_its_last_line(self):
        source = b"def f(resolve):\n    return resolve(\n        'fixture') is not None\n"
        edit = next(row for row in mutation.candidates("toy.py", source) if row.family == "compare")
        code = next(code for code in probe.code_objects(edit.apply(source), "toy.py") if code.co_name == "f")
        self.assertEqual(edit.line, 3)
        self.assertTrue(probe.instruction_offsets(code, vars(edit)))

    def test_probe_and_oracle_require_activation_and_discriminating_failure(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
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
            config = {"root": directory, "watch": watch, "mutant": None}
            arguments = ["discover", "-s", str(tests), "-p", "test_*.py", "-q"]
            baseline, rc = probe.run(config, arguments)
            self.assertEqual(rc, 0)
            self.assertEqual(baseline["coverage"], watch)
            for family, expected in (("compare", "killed"), ("boolean", "survived")):
                with self.subTest(family=family):
                    edit = next(row for row in mutation.candidates("toy.py", SOURCE) if row.family == family)
                    source.write_bytes(edit.apply(SOURCE))
                    config["mutant"] = vars(edit)
                    config["sha256"] = mutation.sha256(source.read_bytes()).hexdigest()
                    sys.modules.pop("test_toy", None)
                    result, rc = probe.run(config, arguments)
                    result["rc"] = rc
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
            sys.modules.pop("test_toy", None)
            result, rc = probe.run(config, arguments)
            result["rc"] = rc
            self.assertEqual(result["hit_cases"], [])
            self.assertEqual(mutation.disposition(baseline, result), "unactivated")
            result["tests"] = 0
            self.assertEqual(mutation.disposition(baseline, result), "invalid")
            config["sha256"] = "wrong"
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                probe.run(config, arguments)
