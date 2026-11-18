"""Synthetic replay evidence and discriminating negative controls for A5."""

import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

T2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(T2))
import LIVE_t2
import score as t2
from privacy import PrivacyBinding, PrivacyMetadata, PrivacyState

EXAMPLES = T2.parents[1] / "schemas/tests/examples"


def example(name):
    return json.loads((EXAMPLES / f"{name}_v1.valid.json").read_text())


def plan():
    return {
        "workload_sha256": "a" * 64,
        "fixtures": {"fixture": "b" * 64},
        "warmups": 1,
        "wake_samples": 2,
        "combinations": {"fixture": {"profile": "private-xvfb", "backend": "cdp", "op": "observe",
                                     "readback_sha256": example("vp.channel.reply")["value_sha256"]}},
        "pair_tolerance": {"load": 1, "temperature_millic": 1000, "competing_memory_bytes": 20,
                           "competing_cpu_usec": 10},
        "competing_cgroups": ["fixture.scope"],
        "throttle_counters": ["/sys/fixture-counter"],
    }


def cell(start, *, refused=0, failed=0):
    ms = t2.MS
    start += ms
    binding = example("vp.surface_binding")
    session = example("vp.session")
    session["display"] = "xpra:8"
    binding.update(incarnation=session["incarnation"], owner=session["lease"]["holder"])
    resources = {kind: [kind + "-fixture"] for kind in t2.RESOURCE_KINDS}
    value = {
        "start_mono_ns": start + ms, "session": session, "private_display": ":8",
        "attach": {
            "start_mono_ns": start + 2 * ms, "end_mono_ns": start + 3 * ms,
            "receipt": {"session_id": session["session_id"], "binding": binding},
            "before": resources, "after": deepcopy(resources),
        },
        "privacy_denial": {"code": "HUMAN_NEEDED", "acquisitions": 0, "publications": 0},
        "warmups_completed": 1, "operations": [],
        "detach": {"detached": True, "closed_foreign": False},
    }
    request = example("vp.channel.req")
    reply = example("vp.channel.reply")
    frame = example("vp.frame")
    frame.update(width=1920, height=1080)
    stamp = start + 3 * ms
    for index in range(t2.OPERATIONS):
        response = reply.copy()
        if index < refused:
            response.update(ok=False, code="NO_LEASE")
        elif index < refused + failed:
            response["value_sha256"] = "f" * 64
        value["operations"].append({
            "index": index, "request": dict(request, request_id=f"request-{index}", dry_run=False),
            "reply": response,
            "privacy": {"start_mono_ns": stamp, "end_mono_ns": stamp + ms,
                        "frame": dict(frame, captured_mono_ns=stamp)},
            "t_submit": stamp, "t_admitted": stamp + 2 * ms, "t_first_byte": stamp + 3 * ms,
            "t_complete": stamp + 4 * ms,
        })
        stamp += 4 * ms
    rest_receipt = example("vp.transition_receipt")
    rest_receipt.update({"from": "ACTIVE", "to": "ASLEEP", "fresh_observation_id": None,
                         "acks": ["agent-drain", "demand", "stream", "app"]})
    counters = dict.fromkeys(t2.AGENT_COUNTERS, 9)
    value["rest"] = {"receipt": rest_receipt, "ack_mono_ns": stamp, "end_mono_ns": stamp + t2.REST,
                     "before": counters, "after": counters.copy()}
    stamp += t2.REST
    value["wakes"] = {}
    for state in t2.WAKE:
        value["wakes"][state] = []
        for i in range(2):
            wake = dict(example("vp.transition_receipt"), fresh_observation_id=f"wake-{state}-{i}",
                        acks=["identity", "capability", "stream"])
            prior = {"schema": "vp.perception_state/v1", "surface_id": binding["surface_id"],
                     "session_id": session["session_id"], "state": state, "incarnation": binding["incarnation"],
                     "stream_generation": 1, "authority_epoch": binding["authority_epoch"],
                     "obligations": [], "since_mono_ns": stamp}
            value["wakes"][state].append({"receipt": wake, "prior_state": prior, "t_submit": stamp,
                                          "t_admitted": stamp + ms, "t_first_byte": stamp + 2 * ms,
                                          "t_complete": stamp + 3 * ms})
            stamp += 3 * ms
    value["end_mono_ns"] = stamp + ms
    value["focus"] = {"schema": "vp.focus_trace/v1", "surface_id": binding["surface_id"],
                      "start_mono_ns": start, "end_mono_ns": stamp + 2 * ms, "continuous": True,
                      "events": [{"mono_ns": start, "kind": "_NET_ACTIVE_WINDOW", "xid": "0x1"},
                                 {"mono_ns": start + ms, "kind": "XInput2"}]}
    return value


def arm(arm_name, start, manifest):
    pressure = {"some": [0, 0, 0], "full": [0, 0, 0]}
    samples = [{"mono_ns": stamp, "end_mono_ns": stamp + t2.MS, "mem_available_bytes": 100,
                "scope_memory_current": 5, "scope_memory_peak": 6, "load": [1, 2, 3],
                "cpu_psi": pressure, "memory_psi": pressure, "scope_cpu_psi": pressure, "scope_memory_psi": pressure,
                "thermal_millic": 50000, "throttle": {"/sys/fixture-counter": 7},
                "competing": {"fixture.scope": {"memory_bytes": 42, "cpu_usec": index}}}
               for index, stamp in enumerate(range(start, start + t2.WINDOW, t2.INTERVAL))]
    return {"arm": arm_name, "plan_sha256": t2.digest(manifest),
            "commit": ("a" if arm_name == "baseline" else "b") * 40, "error": None,
            "numeric_evidence": "TARGET", "boot_id": "same-boot",
            "scope": {"affinity": t2.CPUS, "cpuset": list(range(20)), "memory_high": 6 * 1024**3,
                      "memory_max": 8 * 1024**3},
            "start_mono_ns": start, "end_mono_ns": start + t2.WINDOW, "samples": samples,
            "cells": {"fixture": cell(start, refused=3, failed=2)}}


class T2Tests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.start = t2.SECOND
        self.cell = cell(self.start)

    def check_cell(self):
        return t2.check_cell(self.cell, self.plan["combinations"]["fixture"], self.plan,
                             self.start, self.start + t2.WINDOW)

    def test_complete_abba_retains_every_outcome(self):
        runs = [arm(name, (i + 1) * (t2.WINDOW + t2.SECOND), self.plan)
                for i, name in enumerate(("baseline", "candidate", "candidate", "baseline"))]
        result = t2.score(self.plan, runs)
        self.assertEqual(result["status"], "PASS", result)
        self.assertFalse(result["available"])
        summary = result["runs"][1]["cells"]["fixture"]
        self.assertEqual((summary["verified"], summary["refused"], summary["failed"]), (95, 3, 2))
        self.assertEqual(summary["resource_deltas"], dict.fromkeys(t2.RESOURCE_KINDS, 0))
        self.assertEqual(summary["operation_timings"]["end_to_end_ns"]["n"], t2.OPERATIONS)
        self.assertEqual(summary["wake_timings"]["OFF"]["wake_ns"]["n"], 2)
        self.assertEqual(result["margin_percent"], [100, 100])
        self.assertEqual(t2.score(self.plan, runs[1:])["status"], "UNAVAILABLE")
        runs[1]["commit"] = runs[0]["commit"]
        self.assertIn("commit changed", t2.score(self.plan, runs)["reason"])

    def test_refusal_drop_mutant(self):
        self.cell = cell(self.start, refused=6)
        summary = self.check_cell()
        self.assertEqual(summary["refused"], 6)
        with self.assertRaisesRegex(t2.EvidenceError, "verified ratio"):
            t2.check_thresholds(summary)

        # Mutated collector filters refusals and renumbers the successes.
        rows = [row for row in self.cell["operations"] if row["reply"]["ok"]]
        for i, row in enumerate(rows):
            row["index"] = i
        self.cell["operations"] = rows
        self.assertEqual(sum(row["reply"]["ok"] for row in rows), len(rows))
        with self.assertRaisesRegex(t2.EvidenceError, "manifest incomplete"):
            self.check_cell()

    def test_latency_thresholds_are_independent(self):
        observed = self.check_cell()
        t2.check_thresholds(observed)
        for key, threshold in (("proposal_p95_ns", 2_000 * t2.MS), ("observation_p95_ns", 500 * t2.MS)):
            with self.subTest(key=key):
                t2.check_thresholds(dict(observed, **{key: threshold}))
                with self.assertRaises(t2.EvidenceError):
                    t2.check_thresholds(dict(observed, **{key: threshold + 1}))
        for state, threshold in t2.WAKE.items():
            with self.subTest(state=state):
                t2.check_thresholds(dict(observed, wake_p95_ns=dict(observed["wake_p95_ns"], **{state: threshold})))
                with self.assertRaises(t2.EvidenceError):
                    t2.check_thresholds(dict(observed, wake_p95_ns=dict(observed["wake_p95_ns"],
                                                                       **{state: threshold + 1})))

    def test_attach_count_only_mutant_and_missing_category(self):
        before = self.cell["attach"]["before"]
        after = self.cell["attach"]["after"]
        after["guests"] = ["replacement"]
        self.assertEqual(len(after["guests"]), len(before["guests"]))
        with self.assertRaisesRegex(t2.EvidenceError, "identities"):
            self.check_cell()
        del after["guests"]
        with self.assertRaisesRegex(t2.EvidenceError, "incomplete resource"):
            self.check_cell()

    def test_endpoint_only_and_restore_focus_mutants(self):
        trace = self.cell["focus"]
        trace["continuous"] = False
        with self.assertRaisesRegex(t2.EvidenceError, "continuous focus"):
            self.check_cell()
        trace["continuous"] = True
        stamp = trace["start_mono_ns"] + 2 * t2.MS
        trace["events"].extend({"kind": "_NET_ACTIVE_WINDOW", "mono_ns": stamp, "xid": xid}
                               for xid in ("0x2", "0x1"))
        with self.assertRaisesRegex(t2.EvidenceError, "unsolicited focus"):
            self.check_cell()

    def test_privacy_before_capture_and_captured_first_mutant(self):
        binding = self.cell["attach"]["receipt"]["binding"]
        identity = PrivacyBinding(binding["surface_id"], binding["incarnation"], binding["authority_epoch"],
                                  "fixture", "witness")

        class LoginDriver:
            def privacy_ports(self, combo, *, login):
                assert login
                return identity, lambda _: PrivacyMetadata(identity, 1, PrivacyState.LOGIN), None, (
                    lambda active, stamp: None), lambda reason: None

        driver = LoginDriver()
        self.assertEqual(LIVE_t2.protected(driver, "fixture", binding, login=True),
                         self.cell["privacy_denial"])

        class CaptureFirst(LIVE_t2.PrivacySentinel):
            def observe(self, acquire, publish):
                acquire(None)
                return super().observe(acquire, publish)

        with patch.object(LIVE_t2, "PrivacySentinel", CaptureFirst):
            self.cell["privacy_denial"] = LIVE_t2.protected(driver, "fixture", binding, login=True)
        with self.assertRaisesRegex(t2.EvidenceError, "source exclusion"):
            self.check_cell()

    def test_missing_marker_and_stale_frame(self):
        row = self.cell["operations"][0]
        first = row["t_first_byte"]
        row["t_first_byte"] = None
        with self.assertRaisesRegex(t2.EvidenceError, "missing boundary"):
            self.check_cell()
        row["t_first_byte"] = first
        row["privacy"]["frame"]["witness"] = {"cdp_target_id": "foreign", "ax_sha256": "a" * 64}
        with self.assertRaisesRegex(t2.EvidenceError, "source"):
            self.check_cell()

    def test_geometry_requires_1920_by_1080(self):
        self.assertEqual(self.check_cell()["verified"], t2.OPERATIONS)
        for row in self.cell["operations"]:
            row["privacy"]["frame"].update(width=1280, height=720)
        with self.assertRaisesRegex(t2.EvidenceError, "display size"):
            self.check_cell()
        with patch.object(t2, "SIZE", (1280, 720)):
            self.assertEqual(self.check_cell()["verified"], t2.OPERATIONS)

    def test_unaccepted_and_deferred_profiles_are_unavailable(self):
        t2.plan_check(self.plan)
        expected = self.plan["combinations"]["fixture"]
        require = t2.require

        def skip_profile_gate(condition, reason):
            if reason != "profile is unavailable":
                require(condition, reason)

        for profile in ("B2-10", "B2-11", "host-composition", "wayland", "occluded", "minimized", None):
            with self.subTest(profile=profile):
                expected["profile"] = profile
                actual = t2.score(self.plan, [])
                self.assertEqual((actual["status"], actual["available"], actual["reason"]),
                                 ("UNAVAILABLE", False, "profile is unavailable"))
                with patch.object(t2, "require", skip_profile_gate):
                    t2.plan_check(self.plan)
        del expected["profile"]
        with self.assertRaisesRegex(t2.EvidenceError, "profile is unavailable"):
            t2.plan_check(self.plan)

    def test_live_runner_compiles_without_running(self):
        source = T2 / "LIVE_t2.py"
        code = source.read_text()
        compile(ast.parse(code, filename=str(source)), str(source), "exec")
        with self.assertRaises(SyntaxError):
            ast.parse(code + "\ndef broken(\n", filename=str(source))

    def test_calibration_receipt_contains_every_abba_arm(self):
        report = json.loads((T2 / "calibration.json").read_bytes())
        order = ["baseline", "candidate", "candidate", "baseline"]

        def verify(receipt):
            raw = receipt["raw_iterations"]
            self.assertEqual(receipt["kind"], "offline fixture calibration")
            self.assertEqual(receipt["scope"]["affinity"], [16, 17, 18, 19])
            self.assertEqual(receipt["paired_order"], order)
            self.assertEqual([row["arm"] for row in raw], order)
            self.assertEqual([row["order_index"] for row in raw], list(range(len(order))))
            self.assertEqual(receipt["iterations"], len(receipt["samples"]))
            self.assertEqual(receipt["iterations"], len(raw))
            self.assertEqual(receipt["failed"], 0)
            self.assertEqual(receipt["refused"], 0)
            previous = receipt["start_mono_ns"]
            for row, sample in zip(raw, receipt["samples"], strict=True):
                start, end = t2.timeline(row, complete=True)
                self.assertLessEqual(previous, start)
                self.assertLessEqual(end, sample["mono_ns"])
                self.assertLessEqual(sample["mono_ns"], sample["end_mono_ns"])
                self.assertEqual(row["result_sha256"], receipt["result_sha256"])
                previous = sample["end_mono_ns"]
            self.assertLessEqual(previous, receipt["end_mono_ns"])

        verify(report)
        repo = T2.parents[3]
        self.assertEqual(sha256((repo / report["fixture"]).read_bytes()).hexdigest(), report["fixture_sha256"])
        report["raw_iterations"].pop()
        with self.assertRaises(AssertionError):
            verify(report)

    def test_rest_and_wake_evidence(self):
        acks = self.cell["rest"]["receipt"]["acks"]
        acks.remove("demand")
        with self.assertRaisesRegex(t2.EvidenceError, "unconfirmed drain"):
            self.check_cell()
        acks.append("demand")
        self.cell["rest"]["after"]["ocr"] += 1
        with self.assertRaisesRegex(t2.EvidenceError, "activity during rest"):
            self.check_cell()
        self.cell["rest"]["after"]["ocr"] -= 1
        self.cell["rest"]["end_mono_ns"] -= 1
        with self.assertRaisesRegex(t2.EvidenceError, "shorter"):
            self.check_cell()
        self.cell["rest"]["end_mono_ns"] += 1
        wakes = self.cell["wakes"]["ASLEEP"]
        wakes[1]["receipt"]["fresh_observation_id"] = wakes[0]["receipt"]["fresh_observation_id"]
        with self.assertRaisesRegex(t2.EvidenceError, "reused wake"):
            self.check_cell()

    def test_resource_samples_and_pair_controls(self):
        raw = arm("baseline", self.start, self.plan)
        base = t2.check_samples(raw, self.plan)
        peer = dict(base, mem_available_min=90)
        self.assertEqual(t2.check_pair(base, peer, self.plan["pair_tolerance"]), 90)
        with self.assertRaisesRegex(t2.EvidenceError, "margin"):
            t2.check_pair(base, dict(peer, mem_available_min=89), self.plan["pair_tolerance"])
        with self.assertRaisesRegex(t2.EvidenceError, "asymmetric"):
            t2.check_pair(base, dict(peer, thermal_max=52000), self.plan["pair_tolerance"])
        with self.assertRaisesRegex(t2.EvidenceError, "competing"):
            t2.check_pair(base, dict(peer, competing={"fixture.scope": (42, 1)}), self.plan["pair_tolerance"])
        counter = raw["samples"][1]["throttle"]
        counter["/sys/fixture-counter"] += 1
        with self.assertRaisesRegex(t2.EvidenceError, "thermal throttle"):
            t2.check_samples(raw, self.plan)
        counter["/sys/fixture-counter"] -= 1
        raw["samples"][1]["end_mono_ns"] += t2.INTERVAL
        with self.assertRaisesRegex(t2.EvidenceError, "exceeded its slot"):
            t2.check_samples(raw, self.plan)
        raw["samples"].pop()
        with self.assertRaisesRegex(t2.EvidenceError, "samples missing"):
            t2.check_samples(raw, self.plan)

    def test_memory_margin_kills_previous_ten_percent_gate(self):
        base = dict(t2.check_samples(arm("baseline", self.start, self.plan), self.plan),
                    mem_available_min=1_000)
        peer = dict(base, mem_available_min=101)
        limits = self.plan["pair_tolerance"]
        with self.assertRaisesRegex(t2.EvidenceError, "memory margin"):
            t2.check_pair(base, peer, limits)
        require = t2.require

        def old_margin(condition, reason):
            if reason == "memory margin below target":
                condition = peer["mem_available_min"] * 10 >= base["mem_available_min"]
            require(condition, reason)

        with patch.object(t2, "require", old_margin):
            self.assertEqual(t2.check_pair(base, peer, limits), 10.1)

    def test_timeline_decomposition(self):
        self.assertEqual(t2.components({"t_submit": 7, "t_admitted": 10, "t_first_byte": 17, "t_complete": 22}),
                         {"queue_ns": 3, "wake_ns": 7, "end_to_end_ns": 15, "service_ns": 5})
        self.assertEqual(t2.p95([1] * 95 + [2] * 5), 1)


if __name__ == "__main__":
    unittest.main()
