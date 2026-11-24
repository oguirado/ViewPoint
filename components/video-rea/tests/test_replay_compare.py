# SPDX-License-Identifier: AGPL-3.0-only
"""Fixture controls for the pure replay comparator."""

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


spec = spec_from_file_location(
    "replay_compare", Path(__file__).resolve().parents[1] / "replay_compare.py"
)
replay_compare = module_from_spec(spec)
spec.loader.exec_module(replay_compare)
compare_replay_events = replay_compare.compare_replay_events


def event(event_id, time_us):
    return {"event_id": event_id, "time_us": time_us}


class ReplayCompareTests(unittest.TestCase):
    def test_aligned_replay_pairs_by_id_independent_of_input_order(self):
        reference = [event("jump", -100), event("goal", 200)]
        candidate = [event("goal", 200), event("jump", -100)]
        report = compare_replay_events(reference, candidate, late_tolerance_us=0)

        self.assertEqual(report["schema"], "rea.replay_event_comparison/v1")
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["coverage"], {
            "reference_count": 2,
            "candidate_count": 2,
            "timing_pass_count": 2,
            "missing_count": 0,
            "unexpected_count": 0,
            "duplicate_count": 0,
            "matched_count": 2,
            "reference_fraction": 1.0,
        })
        self.assertEqual(report["events"], [
            {"event_id": "goal", "reference_us": 200, "candidate_us": 200,
             "delta_us": 0, "verdict": "pass"},
            {"event_id": "jump", "reference_us": -100, "candidate_us": -100,
             "delta_us": 0, "verdict": "pass"},
        ])
        self.assertEqual(reference, [event("jump", -100), event("goal", 200)])
        self.assertEqual(candidate, [event("goal", 200), event("jump", -100)])

    def test_early_goal_fails_with_full_coverage_and_unchanged_final_event(self):
        reference = [event("goal", 200), event("finish", 300)]
        candidate = [event("goal", 199), event("finish", 300)]
        report = compare_replay_events(reference, candidate, late_tolerance_us=10)

        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["coverage"]["reference_fraction"], 1.0)
        self.assertEqual(
            [(row["event_id"], row["delta_us"], row["verdict"]) for row in report["events"]],
            [("finish", 0, "pass"), ("goal", -1, "early")],
        )

    def test_late_tolerance_includes_boundary(self):
        reference = [event("goal", 200)]
        for time_us, expected_event, expected_report in (
            (210, "pass", "pass"), (211, "late", "fail")
        ):
            with self.subTest(time_us=time_us):
                report = compare_replay_events(
                    reference, [event("goal", time_us)], late_tolerance_us=10
                )
                self.assertEqual(report["late_tolerance_us"], 10)
                self.assertEqual(report["verdict"], expected_report)
                self.assertEqual(report["events"][0]["verdict"], expected_event)
                self.assertEqual(report["events"][0]["delta_us"], time_us - 200)

    def test_missing_id_fails_even_with_an_unexpected_event_at_the_same_time(self):
        reference = [event("goal", 200), event("jump", 0)]
        candidate = [event("other", 200), event("jump", 0)]
        report = compare_replay_events(reference, candidate, late_tolerance_us=0)

        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["coverage"]["matched_count"], 1)
        self.assertEqual(report["coverage"]["reference_fraction"], 0.5)
        self.assertEqual(report["events"][0], {
            "event_id": "goal", "reference_us": 200, "candidate_us": None,
            "delta_us": None, "verdict": "missing",
        })
        self.assertEqual(report["events"][2], {
            "event_id": "other", "reference_us": None, "candidate_us": 200,
            "delta_us": None, "verdict": "unexpected",
        })

    def test_unexpected_id_fails_even_with_full_reference_coverage(self):
        reference = [event("goal", 200)]
        candidate = [event("goal", 200), event("extra", 220)]
        report = compare_replay_events(reference, candidate, late_tolerance_us=0)

        self.assertEqual(report["coverage"]["reference_fraction"], 1.0)
        self.assertEqual(report["events"][0]["verdict"], "unexpected")
        self.assertEqual(report["verdict"], "fail")

    def test_empty_traces_do_not_pass(self):
        goal = event("goal", 0)
        for reference, candidate, coverage, verdict in (
            ([], [], None, "incomplete"),
            ([goal], [], 0.0, "fail"),
            ([], [goal], None, "incomplete"),
        ):
            with self.subTest(reference=reference, candidate=candidate):
                report = compare_replay_events(reference, candidate, late_tolerance_us=0)
                self.assertEqual(report["coverage"]["reference_fraction"], coverage)
                self.assertEqual(report["verdict"], verdict)

    def test_malformed_traces_are_rejected_on_both_sides(self):
        valid = [event("goal", 0)]
        invalid_traces = (
            None, {}, "goal", [None], [{"event_id": "goal"}],
            [dict(event("goal", 0), extra=True)],
            [event("", 0)], [event(" ", 0)], [event(" goal", 0)], [event(0, 0)],
            [event("goal", True)], [event("goal", 0.0)], [event("goal", "0")],
            [event("goal", float("nan"))], [event("goal", float("inf"))],
        )
        for invalid in invalid_traces:
            with self.subTest(trace=invalid):
                with self.assertRaisesRegex(ValueError, "reference"):
                    compare_replay_events(invalid, valid, late_tolerance_us=0)
                with self.assertRaisesRegex(ValueError, "candidate"):
                    compare_replay_events(valid, invalid, late_tolerance_us=0)

    def test_inversion_fails_when_each_delta_is_within_tolerance(self):
        reference = [event("first", 100), event("second", 110)]
        candidate = [event("first", 112), event("second", 111)]
        report = compare_replay_events(reference, candidate, late_tolerance_us=20)
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["order_conflicts"], [
            {"earlier_reference_event_id": "first", "later_reference_event_id": "second"}
        ])

    def test_equal_reference_timestamps_do_not_establish_an_order(self):
        reference = [event("first", 100), event("second", 100), event("last", 110)]
        candidate = [event("second", 100), event("first", 105), event("last", 110)]
        report = compare_replay_events(reference, candidate, late_tolerance_us=20)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["order_conflicts"], [])

    def test_duplicate_candidate_occurrences_are_reported(self):
        reference = [event("goal", 100)]
        candidate = [event("goal", 100), event("goal", 110)]
        report = compare_replay_events(reference, candidate, late_tolerance_us=20)
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["coverage"]["duplicate_count"], 1)
        self.assertEqual(report["coverage"]["timing_pass_count"], 0)
        self.assertEqual(report["events"][0]["candidate_occurrences_us"], [100, 110])
        with self.assertRaisesRegex(ValueError, "reference.*duplicates"):
            compare_replay_events(candidate, reference, late_tolerance_us=20)

    def test_invalid_tolerance_is_rejected(self):
        for tolerance in (None, True, -1, 1.0, "1"):
            with self.subTest(tolerance=tolerance):
                with self.assertRaisesRegex(ValueError, "late_tolerance_us"):
                    compare_replay_events([], [], late_tolerance_us=tolerance)


if __name__ == "__main__":
    unittest.main()
