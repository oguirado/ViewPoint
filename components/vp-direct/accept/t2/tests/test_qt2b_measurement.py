"""Check the cell-local measurement path against the stock QT2 scorer."""

from contextlib import redirect_stdout
from hashlib import sha256
import importlib.util
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_t2 import cell, plan, t2

ENTRY = Path(__file__).resolve().parents[1] / "qt2-evidence" / "measurement_live.py"
SPEC = importlib.util.spec_from_file_location("qt2b_measurement", ENTRY)
measurement = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measurement)
VERIFY_SPEC = importlib.util.spec_from_file_location("qt2b_verify", ENTRY.with_name("verify_measurements.py"))
verify = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify)
TIMER = "timer-1/cdp/observe"


class QT2bMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.plan["combinations"][TIMER] = self.plan["combinations"].pop("fixture")
        self.plan["fixtures"] = {"timer.html": "b" * 64, "login.html": "c" * 64}
        self.plan.update(dict.fromkeys(measurement.ABSENT_CONTROLS))

    def test_absent_controls_cannot_qualify_or_be_replaced_by_zero(self):
        measurement.plan_check(self.plan)
        with self.assertRaises((TypeError, t2.EvidenceError)):
            t2.plan_check(self.plan)
        result = t2.score(self.plan, [])
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertFalse(result["available"])
        for key, substitute in (("competing_cgroups", []), ("throttle_counters", []),
                                ("pair_tolerance", {"load": 0})):
            with self.subTest(key=key), self.assertRaisesRegex(t2.EvidenceError, "explicit absence"):
                measurement.plan_check({**self.plan, key: substitute})

    def test_mutated_workload_or_fixture_bytes_fail_the_pins(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = root / "adapter.py"
            adapter.write_bytes(b"synthetic adapter")
            self.plan["workload_sha256"] = sha256(adapter.read_bytes()).hexdigest()
            for name in self.plan["fixtures"]:
                path = root / name
                path.write_bytes(name.encode())
                self.plan["fixtures"][name] = sha256(path.read_bytes()).hexdigest()
            plan_path = root / "plan.json"
            measurement.verify_pins(self.plan, plan_path, adapter)
            timer = root / "timer.html"
            timer.write_bytes(b"mutated fixture")
            with self.assertRaisesRegex(t2.EvidenceError, "fixture bytes changed"):
                measurement.verify_pins(self.plan, plan_path, adapter)
            timer.write_bytes(b"timer.html")
            adapter.write_bytes(b"mutated adapter")
            with self.assertRaisesRegex(t2.EvidenceError, "workload source changed"):
                measurement.verify_pins(self.plan, plan_path, adapter)

    def test_measurement_keeps_all_outcomes_and_rejects_a_dropped_refusal(self):
        start = t2.SECOND
        recorded = cell(start, refused=3, failed=2)
        expected = self.plan["combinations"][TIMER]
        run = {"start_mono_ns": start}
        summary = measurement.cell_summary(run, recorded, expected, self.plan)
        self.assertEqual(summary["row_result"], "PASS")
        self.assertEqual(summary["verdict"], "unavailable")
        self.assertEqual((summary["verified"], summary["refused"], summary["failed"]), (95, 3, 2))
        self.assertEqual(summary["operation_timings"]["end_to_end_ns"]["n"], 100)
        del recorded["operations"][0]
        with self.assertRaisesRegex(t2.EvidenceError, "operation manifest incomplete"):
            measurement.cell_summary(run, recorded, expected, self.plan)

    def test_measurement_preserves_verified_rate_and_source_exclusion_gates(self):
        start = t2.SECOND
        recorded = cell(start)
        expected = self.plan["combinations"][TIMER]
        run = {"start_mono_ns": start}
        summary = measurement.cell_summary(run, cell(start, refused=6), expected, self.plan)
        self.assertEqual(summary["row_result"], "FAIL")
        self.assertEqual(summary["row_reason"], "verified ratio below target")
        self.assertEqual(summary["verdict"], "not qualified")
        recorded["privacy_denial"]["acquisitions"] = 1
        with self.assertRaisesRegex(t2.EvidenceError, "source exclusion"):
            measurement.cell_summary(run, recorded, expected, self.plan)

    def test_focus_after_the_sampling_window_keeps_metrics_and_fails_the_row(self):
        start = t2.SECOND
        recorded = cell(start)
        recorded["focus"]["end_mono_ns"] = start + t2.WINDOW + 1
        summary = measurement.cell_summary(
            {"start_mono_ns": start}, recorded, self.plan["combinations"][TIMER], self.plan,
        )
        self.assertEqual(summary["verified"], 100)
        self.assertEqual(summary["verdict"], "not qualified")
        self.assertEqual(summary["row_reason"], "focus trace outside sampling window")

    def test_timer_order_uses_numeric_suffix_across_two_digits(self):
        self.assertEqual(measurement.timer_names(12)[8:11],
                         ["timer-9/cdp/observe", "timer-10/cdp/observe", "timer-11/cdp/observe"])

    def test_unavailable_observations_remain_null(self):
        available = {"mem_available_bytes": 123, "thermal_millic": None,
                     "throttle": {}, "competing": {}}
        with patch.object(measurement.HostTelemetry, "read", return_value=available):
            sample = measurement.AvailableTelemetry(Path("/unused")).read()
        self.assertEqual(sample, {"mem_available_bytes": 123, "thermal_millic": None,
                                  "throttle": None, "competing": None})

    def test_sampler_preserves_actual_slot_indices_after_a_miss(self):
        start, interval = t2.SECOND, t2.INTERVAL
        stamp = start + 2 * interval
        samples = [{"mono_ns": value, "end_mono_ns": value + 1} for value in (start, stamp)]
        with TemporaryDirectory() as temp:
            path = Path(temp) / "slots.jsonl"
            with (
                patch.object(measurement, "signature", return_value=({}, Path(temp))),
                patch.object(measurement, "WINDOW", 3 * interval),
                patch.object(measurement.sys, "stdin", StringIO(f"{start}\n")),
                patch.object(measurement, "wait_until"),
                patch.object(measurement, "now", side_effect=[start, stamp, stamp]),
                patch.object(measurement.AvailableTelemetry, "read", side_effect=samples),
                redirect_stdout(StringIO()),
            ):
                measurement.sample_host(path)
            self.assertEqual([json.loads(line)["slot_index"] for line in path.read_text().splitlines()], [0, 2])

    def test_completed_operations_preserve_latency_counts_after_drain_failure(self):
        recorded = cell(t2.SECOND, refused=3, failed=2)
        del recorded["rest"], recorded["focus"]
        summary = verify.record_completed_attempts(self.plan, TIMER, recorded, {"reason": "LifecycleError"})
        self.assertEqual((summary["verified"], summary["refused"], summary["failed"]), (95, 3, 2))
        self.assertEqual(summary["operation_timings"]["end_to_end_ns"]["n"], 100)
        self.assertEqual(summary["protected_observation"]["n"], 100)
        self.assertEqual(summary["row_verdict"], "not qualified")
        self.assertIsNone(summary["focus_continuous"])
        self.assertEqual(summary["wake_p95_ns"], dict.fromkeys(t2.WAKE))
