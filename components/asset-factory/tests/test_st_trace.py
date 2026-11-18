"""ST-TRACE controls for factory captures and their independent joins."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from tools.telemetry.analyse import TraceError, analyse
from tools.telemetry.recorder import PressureSample, Recorder, pressure_ppm, read_pressure


CONTRACT = Path(__file__).resolve().parents[1] / "contracts/s_toro"
PINS = CONTRACT / "pins.json"
SESSION = "12345678-abcd-4567-8123-123456789abc"
BUILD = "5" * 40
CPUS = list(range(16))
PIN_DIGEST = hashlib.sha256(PINS.read_bytes()).hexdigest()
FIXTURES = json.loads(PINS.read_bytes())["semantic_fixtures"]
VALIDATOR = Draft202012Validator(
    json.loads((CONTRACT / "schemas/telemetry.schema.json").read_bytes())
)


def write_json(path, data):
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class RecorderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "trace.jsonl"
        self.env = {
            "TORO_TELEMETRIA": "1",
            "TORO_TELEMETRIA_JSONL": str(self.path),
            "TORO_TELEMETRIA_SESSION_ID": SESSION,
        }

    def test_off_and_new_file_gate(self):
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env={}) as recorder:
            recorder.event("test")
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):
            Recorder.from_env(git_sha=BUILD, toro_session=1,
                              env={"TORO_TELEMETRIA": "1"})
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env):
            pass
        original = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env)
        self.assertEqual(self.path.read_bytes(), original)

    def test_frozen_operations_and_buffer(self):
        operations = VALIDATOR.schema["oneOf"][0]["properties"]["operation"]["enum"]
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env,
                               batch_size=len(operations) + 1) as recorder:
            for operation in operations:
                recorder.event(operation)
            self.assertEqual(self.path.stat().st_size, 0)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        for row in rows:
            VALIDATOR.validate(row)
        self.assertEqual([row["operation"] for row in rows[:-1]], operations)
        self.assertEqual([row["sequence"] for row in rows[:-1]], list(range(1, len(rows))))
        self.assertEqual(rows[-1]["record_count"], len(operations))
        self.assertTrue(rows[-1]["final"])

    def test_sequences_are_per_producer_thread(self):
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env) as recorder:
            start = Barrier(3)

            def record():
                start.wait()
                recorder.event("worker_query")
                recorder.event("worker_query")

            threads = [Thread(target=record) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        expected = {thread.native_id: [1, 2] for thread in threads}
        observed = {tid: [r["sequence"] for r in rows[:-1] if r["thread"] == tid]
                    for tid in expected}
        self.assertEqual(observed, expected)
        self.assertEqual(rows[-1]["record_count"], 4)

    def test_invalid_event_is_accounted_for(self):
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env) as recorder:
            recorder.event("provider", copy_bytes=1)
            recorder.event("bogus")
            recorder.event("provider")
            self.assertEqual(self.path.stat().st_size, 0)
            with self.assertRaisesRegex(ValueError, "invalid_event"):
                recorder.flush()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["kind"] for row in rows], ["event", "checkpoint"])
        self.assertEqual(rows[0]["sequence"], 1)
        self.assertEqual(rows[-1]["invalid_drops"], 2)
        self.assertEqual(rows[-1]["record_count"], 1)
        for row in rows:
            VALIDATOR.validate(row)

    def test_flush_merges_threads_in_capture_order(self):
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env) as recorder:
            recorder.event("input")
            thread = Thread(target=recorder.event, args=("provider",))
            thread.start()
            thread.join()
            recorder.event("render")
            recorder.flush()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["operation"] for row in rows[:-1]], ["input", "provider", "render"])
        self.assertEqual([row["sequence"] for row in rows[:-1]], [1, 1, 2])

    def test_inherited_recorder_cannot_write(self):
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env) as recorder:
            recorder.event("input")
            child = os.fork()
            if child == 0:
                for operation in (lambda: recorder.event("render"), recorder.flush, recorder.close):
                    try:
                        operation()
                    except RuntimeError as exc:
                        if str(exc) == "recorder_inherited_across_fork":
                            continue
                    os._exit(2)
                child_path = self.path.with_name("child.jsonl")
                env = dict(self.env, TORO_TELEMETRIA_JSONL=str(child_path))
                with Recorder.from_env(git_sha=BUILD, toro_session=1, env=env) as fresh:
                    fresh.event("render")
                os._exit(0)
            _, status = os.waitpid(child, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            recorder.event("mesh")
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["operation"] for row in rows[:-1]], ["input", "mesh"])
        child_rows = [json.loads(line)
                      for line in self.path.with_name("child.jsonl").read_text().splitlines()]
        self.assertEqual(child_rows[0]["process_id"], child)
        self.assertEqual(child_rows[-1]["record_count"], 1)

    def test_invalid_event_during_close_still_seals_drop_count(self):
        recorder = Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env)
        recorder.event("bogus")
        with self.assertRaisesRegex(ValueError, "invalid_event"):
            recorder.close()
        final = json.loads(self.path.read_text())
        VALIDATOR.validate(final)
        self.assertEqual(final["invalid_drops"], 1)

    def test_invalid_event_at_batch_limit_is_reported(self):
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env, batch_size=1) as recorder:
            with self.assertRaisesRegex(ValueError, "invalid_event"):
                recorder.event("bogus")
            recorder.event("input")
        event, final = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(final["invalid_drops"], 1)

    def test_short_psi_quantization_and_sample_aperture(self):
        short = (PressureSample(100_000, (10, 10, 10)),
                 PressureSample(100_500, (11, 10, 10)))
        self.assertEqual(pressure_ppm(short)["cpu_some_ppm"],
                         {"value": 1_000_000, "absence_reason": None})
        aperture = (PressureSample(110_000, (10, 10, 10), 100_000),
                    PressureSample(111_000, (20, 10, 10), 110_000))
        self.assertEqual(pressure_ppm(aperture)["cpu_some_ppm"]["value"], 1_000_000)

    def test_psi_rejects_genuine_skew(self):
        before = PressureSample(100_000, (10, 10, 10))
        after = PressureSample(100_500, (13, 10, 10))
        self.assertEqual(pressure_ppm((before, after))["cpu_some_ppm"],
                         {"value": None, "absence_reason": "pressure_window_invalid"})

    def test_completed_span_measures_actual_placement_and_counters(self):
        pre_start = read_pressure()
        pre_end = read_pressure()
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env) as recorder:
            with recorder.span(
                "test", stream_id="worker", span_id="span", request_id="request",
                contract_pins_sha256=PIN_DIGEST, arm_id="TRACE-job", pair_id="pair",
                variant="B", phase="end_to_end", clock_id="host", boundary_id="stage",
                test_id="focused", pressure_pre=(pre_start, pre_end),
            ) as span:
                span.observe_work()
                span.account(copy_bytes=7, source="cpu", destination="file",
                             cache_hits=1, cache_misses=0, retries=0)
                sum(range(1000))
        event = json.loads(self.path.read_text().splitlines()[0])
        VALIDATOR.validate(event)
        ext = event["extension"]
        self.assertEqual(ext["placement_work"], {"nice": os.getpriority(os.PRIO_PROCESS, 0),
                                                "cpus": sorted(os.sched_getaffinity(0))})
        self.assertGreater(ext["end_ns"], ext["start_ns"])
        self.assertGreater(ext["peak_rss_bytes"]["value"], 0)
        self.assertIsNotNone(ext["cpu_ns"]["value"])
        self.assertIsNone(ext["peak_gpu_bytes"]["value"])
        self.assertEqual(ext["copied_bytes"]["value"], event["copy_bytes"])
        self.assertEqual(ext["cache_hits"]["value"], 1)

    def test_span_cpu_includes_other_threads_in_producer_process(self):
        pre = read_pressure(), read_pressure()
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=self.env) as recorder:
            with patch("time.process_time_ns", side_effect=(100, 700)), \
                    patch("time.thread_time_ns", return_value=50):
                with recorder.span(
                    "render", stream_id="worker", span_id="span", request_id="request",
                    contract_pins_sha256=PIN_DIGEST, arm_id="TRACE-job", pair_id="pair",
                    variant="B", phase="end_to_end", clock_id="host", boundary_id="stage",
                    pressure_pre=pre,
                ) as span:
                    span.observe_work()
        row = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(row["process_id"], os.getpid())
        self.assertEqual(row["extension"]["cpu_ns"]["value"], 600)


class AnalyserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.trace = self.directory / "worker.jsonl"
        self.raw = self.directory / "raw.json"
        self.manifest = self.directory / "manifest.json"
        self.rows = [copy.deepcopy(FIXTURES["records"]["trace"]),
                     copy.deepcopy(FIXTURES["records"]["checkpoint"])]
        event, checkpoint = self.rows
        checkpoint["record_count"] = 1
        checkpoint["timestamp_ns"] = event["timestamp_ns"] + 1
        extension = event["extension"]
        self.match = {key: extension[key] for key in (
            "operation", "stream_id", "parent_span_id", "stage", "worker",
            "worker_generation", "test_id", "request_id", "phase", "clock_id",
        ) if key in extension}
        self.match.update(operation=event["operation"], process_id=event["process_id"],
                          toro_session=event["toro_session"])
        extension["arm_id"] = "TRACE-job"
        self.leg = {"arm_id": extension["arm_id"], "pair_id": extension["pair_id"],
                    "variant": "B", "request_id": extension["request_id"],
                    "workload_sha256": "d" * 64, "root_span_id": extension["span_id"]}
        a_leg = dict(self.leg, variant="A", root_span_id=None, request_id="request-a")
        self.spec = {
            "session_id": SESSION, "git_sha": BUILD, "contract_pins_sha256": PIN_DIGEST,
            "captures": [{"path": self.trace.name, "process_id": event["process_id"],
                          "event_count": 1}],
            "spans": {extension["span_id"]: self.match},
            "legs": [a_leg, self.leg],
        }
        extension["contract_pins_sha256"] = PIN_DIGEST
        self.raw_record = {key: extension[key] for key in (
            "start_ns", "end_ns", "clock_id", "placement_entry", "placement_work",
            "psi", "psi_pre",
        )}
        for leg in self.spec["legs"]:
            path = self.directory / (leg["variant"] + ".raw.json")
            witness = dict(self.raw_record, **leg, session_id=SESSION, git_sha=BUILD,
                           contract_pins_sha256=PIN_DIGEST)
            if leg["variant"] == "A":
                witness["start_ns"] = 400
                witness["end_ns"] = 620
            start, end = witness["start_ns"], witness["end_ns"]
            witness["psi"] = witness["psi_pre"] = {
                key: {"value": 0, "absence_reason": None} for key in extension["psi"]
            }
            witness["pressure_samples"] = [
                {"timestamp_ns": timestamp, "some_us": [3, 4, 5]} for timestamp in (start, end)
            ]
            witness["pressure_pre_samples"] = [
                {"timestamp_ns": timestamp, "some_us": [3, 4, 5]}
                for timestamp in (start - 10, start)
            ]
            leg.update(raw_evidence_path=path.name, raw_evidence_sha256=write_json(path, witness))

    def run_analysis(self):
        write_rows(self.trace, self.rows)
        write_json(self.manifest, self.spec)
        return analyse(self.manifest, pins_path=PINS)

    def test_complete_pair(self):
        result = self.run_analysis()
        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(result["pairs"][0]["wall_a_ns"], 220)
        self.assertEqual(result["pairs"][0]["wall_b_ns"], 200)

    def test_refusing_tampered_captures(self):
        event, checkpoint = self.rows
        extension = event["extension"]
        controls = (
            (checkpoint, "ring_drops", 1, "capture_drops"),
            (event, "sequence", 2, "sequence_gap"),
            (extension["psi"]["io_some_ppm"], "value", None, "missing_psi"),
            (extension["placement_work"], "nice", 19, "placement"),
            (extension, "request_id", "wrong-request", "span_join"),
            (extension, "contract_pins_sha256", "b" * 64, "pins"),
            (extension, "end_ns", extension["start_ns"], "wall"),
            (extension, "dropped", 1, "capture_drops"),
        )
        for obj, key, value, reason in controls:
            with self.subTest(reason=reason):
                before = obj[key]
                obj[key] = value
                if reason == "missing_psi":
                    obj["absence_reason"] = "unavailable"
                with self.assertRaisesRegex(TraceError, reason):
                    self.run_analysis()
                obj[key] = before
                if reason == "missing_psi":
                    obj["absence_reason"] = None

    def test_rejects_placement_measured_on_nice_19_producer(self):
        env = {"TORO_TELEMETRIA": "1", "TORO_TELEMETRIA_JSONL": str(self.trace),
               "TORO_TELEMETRIA_SESSION_ID": SESSION}
        ready = Barrier(2)
        before = PressureSample(1, (0, 0, 0))
        after = PressureSample(10_001, (0, 0, 0))
        outer_start = time.monotonic_ns()
        with Recorder.from_env(git_sha=BUILD, toro_session=1, env=env) as recorder:
            def record():
                with recorder.span(
                    "provider", stream_id="worker", span_id="bad-nice", request_id="req",
                    contract_pins_sha256=PIN_DIGEST, arm_id="TRACE-job", pair_id="pair",
                    variant="B", phase="end_to_end", clock_id="host", boundary_id="gate",
                    pressure_pre=(before, after),
                ) as span:
                    ready.wait()
                    os.setpriority(os.PRIO_PROCESS, 0, 19)
                    span.observe_work()
                    sum(range(10_000))

            producer = Thread(target=record)
            producer.start()
            ready.wait()
            producer.join()
        outer_end = time.monotonic_ns()
        self.rows = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(self.rows[0]["extension"]["placement_work"]["nice"], 19)
        self.spec["captures"][0]["process_id"] = os.getpid()
        self.spec["spans"] = {"bad-nice": {
            "process_id": os.getpid(), "toro_session": 1, "operation": "provider",
            "stream_id": "worker", "parent_span_id": None, "stage": "provider",
            "worker": None, "worker_generation": None, "test_id": None,
            "request_id": "req", "phase": "end_to_end", "clock_id": "host",
        }}
        a, b = self.spec["legs"]
        for leg in (a, b):
            path = self.directory / leg["raw_evidence_path"]
            witness = json.loads(path.read_bytes())
            leg.update(pair_id="pair")
            if leg is b:
                leg.update(request_id="req", root_span_id="bad-nice")
                witness.update(start_ns=outer_start, end_ns=outer_end, clock_id="host")
                witness["pressure_samples"] = [
                    {"timestamp_ns": timestamp, "some_us": [3, 4, 5]}
                    for timestamp in (outer_start, outer_end)
                ]
            else:
                witness["clock_id"] = "host"
            for key in ("pair_id", "request_id", "root_span_id"):
                witness[key] = leg[key]
            leg["raw_evidence_sha256"] = write_json(path, witness)
        with self.assertRaisesRegex(TraceError, "placement"):
            self.run_analysis()

    def test_zero_producer_cpu_is_refused(self):
        self.rows[0]["extension"]["cpu_ns"]["value"] = 0
        with self.assertRaisesRegex(TraceError, "missing_resource_metric"):
            self.run_analysis()

    def test_missing_independently_expected_span_is_refused(self):
        self.spec["spans"]["expected-child"] = dict(self.match)
        with self.assertRaisesRegex(TraceError, "missing_span"):
            self.run_analysis()

    def test_incomplete_pair(self):
        self.spec["legs"].pop(0)
        with self.assertRaisesRegex(TraceError, "incomplete_pair"):
            self.run_analysis()

    def test_missing_tail_even_with_forged_final_count(self):
        event, checkpoint = self.rows
        # The driver expects a root plus a structural event. A dropped last row
        # leaves the root and its joins intact and the checkpoint self-consistent.
        self.spec["captures"][0]["event_count"] = 2
        tail = dict(event, sequence=2, timestamp_ns=event["timestamp_ns"] + 1)
        del tail["extension"]
        self.rows.insert(1, tail)
        checkpoint.update(record_count=2, timestamp_ns=tail["timestamp_ns"] + 1)
        self.assertEqual(self.run_analysis()["event_count"], 2)
        self.rows.pop(1)
        checkpoint["record_count"] = 1
        with self.assertRaisesRegex(TraceError, "event_count"):
            self.run_analysis()

    def test_tampered_raw_pressure_even_with_matching_digest(self):
        path = self.directory / self.leg["raw_evidence_path"]
        data = json.loads(path.read_bytes())
        data["pressure_samples"][1]["some_us"][0] += 1
        self.leg["raw_evidence_sha256"] = write_json(path, data)
        with self.assertRaisesRegex(TraceError, "raw_pressure"):
            self.run_analysis()

    def test_raw_pressure_rejects_impossible_aperture(self):
        path = self.directory / self.leg["raw_evidence_path"]
        data = json.loads(path.read_bytes())
        sample = data["pressure_samples"][0]
        sample["read_start_ns"] = sample["timestamp_ns"] + 1
        self.leg["raw_evidence_sha256"] = write_json(path, data)
        with self.assertRaisesRegex(TraceError, "raw_pressure"):
            self.run_analysis()

    def test_mismatched_raw_evidence(self):
        path = self.directory / self.leg["raw_evidence_path"]
        path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(TraceError, "raw_evidence"):
            self.run_analysis()
