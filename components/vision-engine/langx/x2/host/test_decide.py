"""Synthetic compare-report tests; fixture timings are not VM observations."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import decide


MAJORITY_SHA256 = hashlib.sha256(b"synthetic canonical output").hexdigest()


def synthetic_report(pages=("page1",)):
    """Build the canonical compare.py shape from small synthetic inputs."""
    seeds = {seed: "PRESENT" for seed in ("py_w1", "py_w2", "ts_w1", "ts_w2")}
    return {
        "seeds": seeds,
        "pages": {
            page: {
                "determinism": {seed: True for seed in seeds},
                "conformance": {
                    "majority_sha256": MAJORITY_SHA256,
                    "dissenting_seeds": [],
                },
                "dissenting_details": {},
            }
            for page in pages
        },
        "timing": {
            "py_w1": {"launch_ms_median": 10, "steps_ms_median": 6},
            "py_w2": {"launch_ms_median": 14, "steps_ms_median": 10},
            "ts_w1": {"launch_ms_median": 20, "steps_ms_median": 12},
            "ts_w2": {"launch_ms_median": 24, "steps_ms_median": 16},
        },
    }


def synthetic_report_sha256(report):
    raw = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def reverse_mapping_order(value):
    if isinstance(value, dict):
        return {key: reverse_mapping_order(value[key]) for key in reversed(value)}
    if isinstance(value, list):
        return [reverse_mapping_order(item) for item in value]
    return value


class DecideTests(unittest.TestCase):
    def render(self, report):
        return decide.render_report(
            report, synthetic_report_sha256(report), report_path="synthetic-report.json"
        )

    def assert_decision(self, markdown, decision):
        lines = [line for line in markdown.splitlines() if line.startswith("Decision:")]
        self.assertEqual(lines, ["Decision: " + decision])

    def table_rows(self, markdown, width):
        rows = []
        for line in markdown.splitlines():
            if line.startswith("|") and line.endswith("|"):
                cells = [cell.strip().strip("`") for cell in line.split("|")[1:-1]]
                if len(cells) == width:
                    rows.append(cells)
        return rows

    def seed_row(self, markdown, page, seed, width):
        rows = [
            row
            for row in self.table_rows(markdown, width)
            if row[:2] == [page, seed]
        ]
        self.assertEqual(len(rows), 1, (page, seed, rows))
        return rows[0]

    def test_clear_py_win_and_median_tables(self):
        report = synthetic_report()
        markdown = self.render(report)
        for heading in (
            "# LANG-X X2 Decision Report",
            "## Inputs",
            "## Conformance",
            "## Determinism",
            "## Timing (VM)",
            "## Decision",
        ):
            self.assertIn(heading, markdown.splitlines())
        self.assertIn(synthetic_report_sha256(report), markdown)
        self.assertIn("synthetic-report.json", markdown)
        self.assertIn(
            "| Language | Qualifying seeds | launch_ms_median (VM) | steps_ms_median (VM) |",
            markdown,
        )
        self.assertIn("| py | py_w1, py_w2 | 12 | 8 |", markdown)
        self.assertIn("| ts | ts_w1, ts_w2 | 22 | 14 |", markdown)
        self.assertEqual(
            self.seed_row(markdown, "page1", "py_w1", 7),
            ["page1", "py_w1", "PRESENT", MAJORITY_SHA256, "YES", "AUSENTE", "AUSENTE"],
        )
        self.assertEqual(self.seed_row(markdown, "page1", "py_w1", 3)[2], "YES")
        self.assertNotIn("MEASURED", markdown)
        self.assert_decision(markdown, "py")

    def test_clear_ts_win(self):
        report = synthetic_report()
        for seed, steps in (("py_w1", 18), ("py_w2", 22), ("ts_w1", 10), ("ts_w2", 14)):
            report["timing"][seed]["steps_ms_median"] = steps
        markdown = self.render(report)
        self.assertIn("| py | py_w1, py_w2 | 12 | 20 |", markdown)
        self.assertIn("| ts | ts_w1, ts_w2 | 22 | 12 |", markdown)
        self.assert_decision(markdown, "ts")

    def test_small_gap_is_tie_with_operator_guidance(self):
        report = synthetic_report()
        for seed, steps in (("py_w1", 8), ("py_w2", 10), ("ts_w1", 9), ("ts_w2", 11)):
            report["timing"][seed]["steps_ms_median"] = steps
        markdown = self.render(report)
        self.assert_decision(markdown, "TIE")
        self.assertIn("operator", markdown.lower())

    def test_twenty_percent_boundary_and_just_below(self):
        for faster_steps, expected in ((8, "py"), (8.01, "TIE")):
            with self.subTest(faster_steps=faster_steps):
                report = synthetic_report()
                for seed in ("py_w1", "py_w2"):
                    report["timing"][seed]["steps_ms_median"] = faster_steps
                for seed in ("ts_w1", "ts_w2"):
                    report["timing"][seed]["steps_ms_median"] = 10
                self.assert_decision(self.render(report), expected)

    def test_large_integer_timings_preserve_exact_threshold_and_table_values(self):
        slower_steps = 10**29
        for faster_steps, expected in ((8 * 10**28 + 1, "TIE"), (8 * 10**28, "py")):
            with self.subTest(faster_steps=faster_steps):
                report = synthetic_report()
                for seed in ("py_w1", "py_w2"):
                    report["timing"][seed]["steps_ms_median"] = faster_steps
                for seed in ("ts_w1", "ts_w2"):
                    report["timing"][seed]["steps_ms_median"] = slower_steps
                markdown = self.render(report)
                self.assertIn("| py | py_w1, py_w2 | 12 | " + str(faster_steps) + " |", markdown)
                self.assertIn("| ts | ts_w1, ts_w2 | 22 | " + str(slower_steps) + " |", markdown)
                self.assert_decision(markdown, expected)

    def test_schema_source_references_are_visible_code_spans(self):
        markdown = self.render(synthetic_report())
        for source_path in (
            "pages.<page>.conformance.majority_sha256",
            "pages.<page>.conformance.dissenting_seeds",
            "pages.<page>.dissenting_details.<seed>.first_differing_byte_offset",
            "pages.<page>.dissenting_details.<seed>.first_differing_json_path",
            "pages.<page>.determinism.<seed>",
            "seeds.<seed>",
            "timing.<seed>.launch_ms_median",
            "timing.<seed>.steps_ms_median",
        ):
            with self.subTest(source_path=source_path):
                self.assertIn("`" + source_path + "`", markdown)

    def test_equal_zero_steps_is_tie(self):
        report = synthetic_report()
        for timing in report["timing"].values():
            timing["steps_ms_median"] = 0
        markdown = self.render(report)
        self.assertIn("| py | py_w1, py_w2 | 12 | 0 |", markdown)
        self.assert_decision(markdown, "TIE")

    def test_dissent_and_absent_seed_are_rendered_and_excluded_from_timing(self):
        report = synthetic_report()
        report["seeds"].update({"py_w3": "PRESENT", "ts_w3": "ABSENT"})
        page = report["pages"]["page1"]
        page["determinism"]["py_w3"] = True
        page["conformance"]["dissenting_seeds"] = ["py_w3"]
        page["dissenting_details"]["py_w3"] = {
            "first_differing_byte_offset": 17,
            "first_differing_json_path": "$.nodes[1].text",
        }
        report["timing"]["py_w3"] = {"launch_ms_median": 1, "steps_ms_median": 1}
        report["timing"]["ts_w3"] = "ABSENT"
        markdown = self.render(report)
        self.assertEqual(
            self.seed_row(markdown, "page1", "py_w3", 7),
            ["page1", "py_w3", "PRESENT", MAJORITY_SHA256, "DISSENT", "17", "$.nodes[1].text"],
        )
        absent = self.seed_row(markdown, "page1", "ts_w3", 7)
        self.assertEqual(absent[2], "ABSENT")
        self.assertEqual(absent[4:], ["ABSENT", "AUSENTE", "AUSENTE"])
        self.assertEqual(self.seed_row(markdown, "page1", "ts_w3", 3)[2], "ABSENT")
        self.assertIn("| py | py_w1, py_w2 | 12 | 8 |", markdown)
        self.assertIn("| ts | ts_w1, ts_w2 | 22 | 14 |", markdown)
        self.assert_decision(markdown, "py")

    def test_missing_dissent_diagnostics_are_ausente(self):
        report = synthetic_report()
        report["pages"]["page1"]["conformance"]["dissenting_seeds"] = ["ts_w2"]
        markdown = self.render(report)
        row = self.seed_row(markdown, "page1", "ts_w2", 7)
        self.assertEqual(row[4:], ["DISSENT", "AUSENTE", "AUSENTE"])
        self.assert_decision(markdown, "py")

    def test_same_seeds_must_pass_every_page(self):
        report = synthetic_report(pages=("page1", "page2"))
        report["seeds"]["py_w3"] = "PRESENT"
        report["timing"]["py_w3"] = {"launch_ms_median": 12, "steps_ms_median": 8}
        for page in report["pages"].values():
            page["determinism"]["py_w3"] = True
        report["pages"]["page1"]["conformance"]["dissenting_seeds"] = ["py_w3"]
        report["pages"]["page2"]["conformance"]["dissenting_seeds"] = ["py_w1"]
        # Each page has two conforming Python seeds, but only py_w2 passes both.
        self.assert_decision(self.render(report), "ts")

    def test_nondeterministic_seed_cannot_qualify(self):
        report = synthetic_report()
        report["pages"]["page1"]["determinism"]["py_w2"] = False
        markdown = self.render(report)
        self.assertEqual(self.seed_row(markdown, "page1", "py_w2", 3)[2], "NO")
        self.assert_decision(markdown, "ts")

    def test_missing_majority_prevents_qualification(self):
        for missing_majority in (None, ""):
            with self.subTest(majority=missing_majority):
                report = synthetic_report()
                report["pages"]["page1"]["conformance"]["majority_sha256"] = missing_majority
                self.assert_decision(self.render(report), "AUSENTE")

    def test_empty_or_missing_inputs_are_ausente(self):
        for key in ("seeds", "timing", "pages"):
            for absent in (False, True):
                with self.subTest(key=key, absent=absent):
                    report = synthetic_report()
                    if absent:
                        del report[key]
                    else:
                        report[key] = {}
                    self.assert_decision(self.render(report), "AUSENTE")
        self.assert_decision(self.render({}), "AUSENTE")

    def test_missing_or_invalid_steps_in_qualifying_language_are_ausente(self):
        invalid_values = (None, "ABSENT", True, -1, float("nan"), float("inf"))
        for invalid in invalid_values:
            with self.subTest(invalid=repr(invalid)):
                report = synthetic_report()
                report["timing"]["py_w1"]["steps_ms_median"] = invalid
                self.assert_decision(self.render(report), "AUSENTE")
        for missing in ("steps", "seed", "seed_absent"):
            with self.subTest(missing=missing):
                report = synthetic_report()
                if missing == "steps":
                    del report["timing"]["py_w1"]["steps_ms_median"]
                elif missing == "seed":
                    del report["timing"]["py_w1"]
                else:
                    report["timing"]["py_w1"] = "ABSENT"
                self.assert_decision(self.render(report), "AUSENTE")

    def test_missing_launch_does_not_block_steps_decision(self):
        report = synthetic_report()
        del report["timing"]["py_w1"]["launch_ms_median"]
        report["timing"]["py_w2"]["launch_ms_median"] = None
        markdown = self.render(report)
        self.assertIn("| py | py_w1, py_w2 | AUSENTE | 8 |", markdown)
        self.assert_decision(markdown, "py")

    def test_unknown_language_prefixes_cannot_qualify(self):
        report = synthetic_report()
        renaming = {
            "py_w1": "pyramid_w1",
            "py_w2": "pyramid_w2",
            "ts_w1": "tshirt_w1",
            "ts_w2": "tshirt_w2",
        }
        for key in ("seeds", "timing"):
            report[key] = {renaming[seed]: value for seed, value in report[key].items()}
        report["pages"]["page1"]["determinism"] = {
            renaming[seed]: value
            for seed, value in report["pages"]["page1"]["determinism"].items()
        }
        self.assert_decision(self.render(report), "AUSENTE")

    def test_rendering_is_deterministic_sorted_and_does_not_mutate_report(self):
        report = synthetic_report(pages=("zeta", "alpha"))
        original = copy.deepcopy(report)
        sha256 = synthetic_report_sha256(report)
        markdown = decide.render_report(report, sha256, report_path="fixture.json")
        reordered = decide.render_report(
            reverse_mapping_order(report), sha256, report_path="fixture.json"
        )
        self.assertEqual(markdown, reordered)
        self.assertEqual(report, original)
        expected = [(page, seed) for page in sorted(report["pages"]) for seed in sorted(report["seeds"])]
        for width in (7, 3):
            actual = [
                (row[0], row[1])
                for row in self.table_rows(markdown, width)
                if row[0] in report["pages"] and row[1] in report["seeds"]
            ]
            self.assertEqual(actual, expected)

    def test_cli_hashes_exact_input_bytes_and_preserves_input(self):
        report = synthetic_report()
        raw = b"\n " + json.dumps(report, indent=3).encode("utf-8") + b"\t"
        exact_sha256 = hashlib.sha256(raw).hexdigest()
        self.assertNotEqual(exact_sha256, synthetic_report_sha256(report))
        with tempfile.TemporaryDirectory(prefix="langx-x2-decide-test-") as directory:
            report_path = Path(directory) / "input report.json"
            output_path = Path(directory) / "decision.md"
            report_path.write_bytes(raw)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(decide.__file__).resolve()),
                    "--report",
                    str(report_path),
                    "--out",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                "stdout:\n" + completed.stdout + "\nstderr:\n" + completed.stderr,
            )
            markdown = output_path.read_text(encoding="utf-8")
            self.assertIn(exact_sha256, markdown)
            self.assertNotIn(synthetic_report_sha256(report), markdown)
            self.assertEqual(report_path.read_bytes(), raw)
            self.assert_decision(markdown, "py")


if __name__ == "__main__":
    unittest.main()
