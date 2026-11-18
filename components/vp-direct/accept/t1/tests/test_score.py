"""T1 receipt replay and discriminating mutations using synthetic evidence."""

from copy import deepcopy
from hashlib import sha256
from inspect import getsource
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

T1 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(T1))
import score as scorer
from score import (CYCLES, DIRECT, DURATION_NS, MS, EvidenceError, cycle_plan, digest, score,
                   source_ceilings)
from benchmark import TransitionVerifier, paired_receipt, verify_frame

from admission import AdmissionGate, AdmissionPlan, UsageSnapshot
from admission import gate as p6
from jsonschema import ValidationError
from perception.lifecycle import LifecycleError
from perception.receipts.tests import test_receipts as p7_fixtures


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / f"vp.{name}_v1.valid.json").read_bytes())


def fixture(size):
    rest_counts = (6, 6) if size == 16 else (12, 16)
    roster = [{"surface_id": f"active-{i}", "state": "ACTIVE"} for i in range(4)]
    for i in range(max(rest_counts)):
        roster.extend({"surface_id": f"{state.lower()}-{i}", "state": state}
                      for state, count in zip(("ASLEEP", "OFF"), rest_counts)
                      if i < count)
    incumbents = {}
    cohort = []
    gate = AdmissionGate(lambda: UsageSnapshot(incumbents), scopes=[("fixture", "/fixture")],
                         read_memory=lambda: (100_000, 75_000),
                         inventory=lambda _: {"complete": True, "charged_bytes": 100})
    admissions = {}
    for row in roster:
        surface, state = row["surface_id"], row["state"]
        with gate.reserve(AdmissionPlan(surface, state, "fixture", False)) as decision:
            admissions[surface] = decision
            if decision["admitted"]:
                incumbents[surface] = None
                cohort.append(dict(example("perception_state"), surface_id=surface,
                                   session_id=f"session-{surface}", state=state, since_mono_ns=1))

    start = 1
    plan = cycle_plan(roster)
    rows = []
    for i, (surface, state) in enumerate(plan):
        submit = start + i * (DURATION_NS // CYCLES)
        row = dict(cycle=i, surface_id=surface, state=state, wake_admission=None)
        if not admissions[surface]["admitted"]:
            markers = dict(t_submit=submit, t_admitted=None, t_first_byte=None, t_complete=submit + 1)
            row.update(outcome="capacity_refused", markers=markers)
        else:
            admitted = submit + 500 * MS
            first = admitted + (250 if state == "ASLEEP" else 1000) * MS
            complete = first + MS
            receipt = dict(example("transition_receipt"), fresh_observation_id=f"fresh-{i}",
                           acks=["identity", "capability", "stream"])
            prior = dict(example("perception_state"), surface_id=surface, session_id=f"session-{surface}",
                         state=state, since_mono_ns=submit, stream_generation=i)
            with gate.reserve(AdmissionPlan(surface, state, "fixture", False)) as decision:
                row.update(outcome="verified", wake_admission=decision, p4=receipt, p7=deepcopy(receipt),
                           broker_states=[prior, dict(prior, state="WAKING", since_mono_ns=admitted),
                                          dict(prior, state="ACTIVE", since_mono_ns=first, stream_generation=i+1)],
                           markers=dict(t_submit=submit, t_admitted=admitted, t_first_byte=first,
                                        t_complete=complete))
        rows.append(row)
    return dict(roster=roster, admissions=list(admissions.values()), cycles=rows, surface_ceiling=16,
                source_ceilings={"MaxSurfaces": 16, "MAX_CAPSULES": 16},
                cohort_start=cohort, cohort_end=deepcopy(cohort),
                start_mono_ns=start, end_mono_ns=start + DURATION_NS, plan_sha256=digest(plan))


def fail_cycle(row, outcome="failed"):
    row["outcome"] = outcome
    if outcome == "refused":
        row["markers"].update(t_admitted=None, t_first_byte=None)
        row["wake_admission"] = dict(row["wake_admission"], admitted=False)
    row.pop("p4")
    row.pop("p7")
    row.pop("broker_states")


def comparison_fixture(capture):
    def advance_timestamps(value, offset):
        for key, field in value.items():
            if isinstance(field, dict):
                advance_timestamps(field, offset)
            elif isinstance(field, list):
                for row in field:
                    if isinstance(row, dict):
                        advance_timestamps(row, offset)
            elif field is not None and (key in {"t_submit", "t_admitted", "t_first_byte", "t_complete"}
                                        or key.endswith("_mono_ns")):
                value[key] = field + offset

    plan = {"pair_tolerance": {"load": 1, "temperature_millic": 1000, "competing_memory_bytes": 20,
                               "competing_cpu_usec": 10},
            "competing_cgroups": ["factory.scope"], "throttle_counters": ["/sys/fixture-counter"]}
    captures = []
    for index, arm in enumerate(("baseline", "candidate", "candidate", "baseline")):
        raw = deepcopy(capture)
        advance_timestamps(raw, 100 + index * (DURATION_NS + 200))
        start, end = raw["start_mono_ns"], raw["end_mono_ns"]
        sample = {"load1_5_15": [1, 2, 3], "thermal_max_millic": 50_000,
                  "competing": {"factory.scope": {"memory_bytes": 100, "cpu_usec": 50}},
                  "throttle": {"/sys/fixture-counter": 7}}
        benchmark = {
            "tag": "TARGET", "clock": "CLOCK_MONOTONIC", "arm": arm, "order_index": index,
            "boot_id": "fixture-boot", "commit": ("a" if arm == "baseline" else "b") * 40,
            "workload_sha256": "c" * 64, "fixture_sha256": "d" * 64, "manifest_sha256": "e" * 64,
            "warmups": 1, "samples": CYCLES,
            "placement": {"affinity": [16, 17, 18, 19], "effective_cpuset": "0-19",
                          "memory_high_bytes": 6 << 30, "memory_max_bytes": 8 << 30},
            "warmup_markers": [{"t_submit": start - 3, "t_admitted": start - 3,
                                "t_first_byte": start - 2, "t_complete": start - 1}],
            "before": dict(deepcopy(sample), mono_ns=start - 10),
            "after": dict(deepcopy(sample), mono_ns=end + 10),
        }
        host_samples = [dict(deepcopy(sample), mono_ns=row["markers"]["t_submit"]) for row in raw["cycles"]]
        captures.append(paired_receipt(raw, benchmark, digest(plan), host_samples))
    return captures, plan


class T1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.small = fixture(16)
        cls.large = fixture(32)

    def test_16_apps_pass_both_wake_strata_and_queue_at_bound(self):
        result = score(self.small)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["cycle_denominator"], 100)
        self.assertEqual(result["wake_ns"], {"ASLEEP": {"p50": 250*MS, "p95": 250*MS},
                                            "OFF": {"p50": 1000*MS, "p95": 1000*MS}})
        self.assertEqual(result["queue_p95_ns"], 500 * MS)

    def test_32_apps_report_expected_capacity_refusal(self):
        result = score(self.large)
        self.assertEqual(result["result"], "EXPECTED_CAPACITY_REFUSAL")
        self.assertEqual(result["admissions"], {"admitted": 16, "capacity_refused": 16, "other_refused": 0})
        self.assertEqual(result["outcomes"]["capacity_refused"],
                         sum(row["outcome"] == "capacity_refused" for row in self.large["cycles"]))
        self.assertGreater(result["outcomes"]["capacity_refused"], 0)
        self.assertLess(result["verified_percent"], 95)

    def test_complete_paired_series_excludes_warmup_from_metrics(self):
        captures, plan = comparison_fixture(self.small)
        expected = score(self.small)
        report = scorer.score_comparison(captures, plan)
        self.assertEqual(report, {"result": "PASS", "numeric_evidence": "TARGET", "runs": [expected] * 4})
        with self.assertRaisesRegex(EvidenceError, "Complete ABBA"):
            scorer.score_comparison(captures[:-1], plan)
        with self.assertRaisesRegex(EvidenceError, "Complete ABBA"):
            scorer.score_comparison([captures[0], captures[2], captures[1], captures[3]], plan)

    def assert_pair_rejected_without_bypass(self, captures, plan, reason):
        # Remove exactly the challenged guard. All other checks still run.
        require = scorer.require

        def mutant(condition, message):
            if message != reason:
                require(condition, message)

        with self.assertRaisesRegex(EvidenceError, reason):
            scorer.score_comparison(captures, plan)
        with patch.object(scorer, "require", mutant):
            self.assertEqual(scorer.score_comparison(captures, plan)["result"], "PASS")

    def test_abba_labels_and_commit_drift_mutants(self):
        captures, plan = comparison_fixture(self.small)
        captures[2]["arm"] = "baseline"
        self.assert_pair_rejected_without_bypass(captures, plan, "Complete ABBA series required")
        captures[2]["arm"] = "candidate"
        captures[2]["commit"] = "c" * 40
        self.assert_pair_rejected_without_bypass(captures, plan, "Commit changed within an arm")

    def test_predeclared_pair_plan_pin_mutant(self):
        captures, plan = comparison_fixture(self.small)
        plan["pair_tolerance"]["load"] += 1
        self.assert_pair_rejected_without_bypass(captures, plan, "Predeclared pair plan changed")

    def test_paired_wrapper_refuses_substituted_scenario_boundary(self):
        captures, plan = comparison_fixture(self.small)
        first = captures[0]
        self.assertEqual(set(first) - {"benchmark", "scenario"},
                         {"arm", "numeric_evidence", "commit", "boot_id", "plan_sha256", "scope",
                          "error", "start_mono_ns", "end_mono_ns", "samples"})
        first["end_mono_ns"] += 1
        self.assert_pair_rejected_without_bypass(captures, plan, "Scenario boundaries differ or arm is incomplete")

    def test_paired_cli_reports_each_raw_capture_digest(self):
        captures, plan = comparison_fixture(self.small)
        with TemporaryDirectory(dir=os.environ["P7_TEST_RAM"]) as scratch:
            root = Path(scratch)
            pair_plan = root / "plan.json"
            pair_plan.write_text(json.dumps(plan))
            paths = [root / f"arm-{index}.json" for index in range(len(captures))]
            for path, run in zip(paths, captures, strict=True):
                path.write_text(json.dumps(run) + "\n")
            invoke = [sys.executable, "-I", "-B", str(T1 / "score.py"), "--pair-plan", pair_plan, *paths]
            run = subprocess.run(invoke, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["result"], "PASS")
            self.assertEqual(result["raw_receipts"], [
                {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()} for path in paths
            ])
            run = subprocess.run(invoke[:-1], capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn("Complete ABBA series required", run.stderr)

    def test_warmup_overlap_and_count_mutants(self):
        captures, plan = comparison_fixture(self.small)
        bench = captures[1]["benchmark"]
        bench["warmup_markers"][0]["t_complete"] = captures[1]["start_mono_ns"]
        self.assert_pair_rejected_without_bypass(captures, plan, "Warm-up overlaps the scored window")
        bench["warmup_markers"].clear()
        self.assert_pair_rejected_without_bypass(captures, plan, "Warm-up count differs")

    def test_thermal_state_and_throttle_mutants(self):
        captures, plan = comparison_fixture(self.small)
        peer = captures[1]["samples"][9]
        peer["thermal_max_millic"] = 51_001
        self.assert_pair_rejected_without_bypass(captures, plan, "Thermally asymmetric pair")
        peer["thermal_max_millic"] = 51_000
        peer["throttle"]["/sys/fixture-counter"] += 1
        self.assert_pair_rejected_without_bypass(captures, plan, "Thermal throttle counter changed")

    def test_competing_load_and_cgroup_drift_mutants(self):
        captures, plan = comparison_fixture(self.small)
        peer = captures[1]["samples"][9]
        peer["load1_5_15"][1] += 2
        self.assert_pair_rejected_without_bypass(captures, plan, "Competing host load changed")
        peer["load1_5_15"][1] -= 1
        peer = captures[1]["benchmark"]["after"]
        peer["competing"]["factory.scope"]["cpu_usec"] += 11
        self.assert_pair_rejected_without_bypass(captures, plan, "Competing cgroup work changed")

    def test_rejects_missing_or_untimed_host_samples(self):
        captures, plan = comparison_fixture(self.small)
        samples = captures[1]["samples"]
        last = samples.pop()
        with self.assertRaisesRegex(EvidenceError, "Per-cycle host sample missing"):
            scorer.score_comparison(captures, plan)
        samples.append(dict(last, mono_ns=last["mono_ns"] + 1))
        self.assert_pair_rejected_without_bypass(captures, plan, "Host sample outside its cycle")

    def test_readme_pins_calibration_bytes(self):
        readme = (T1 / "README.md").read_text()
        receipt = (T1 / "tests/calibration.json").read_bytes()
        self.assertIn(sha256(receipt).hexdigest(), readme)
        self.assertNotIn(sha256(receipt + b" ").hexdigest(), readme)

    def test_mutant_drops_refusals_or_relabels_them(self):
        data = deepcopy(self.large)
        data["cycles"] = [row for row in data["cycles"] if row["outcome"] != "capacity_refused"]
        with self.assertRaisesRegex(EvidenceError, "Missing or extra cycle"):
            score(data)
        data = deepcopy(self.large)
        row = next(row for row in data["cycles"] if row["outcome"] == "capacity_refused")
        row["outcome"] = "refused"
        with self.assertRaisesRegex(EvidenceError, "lost or relabeled"):
            score(data)
        data = deepcopy(self.large)
        data["admissions"] = [decision for decision in data["admissions"] if decision["admitted"]]
        with self.assertRaisesRegex(EvidenceError, "Missing or extra initial admission"):
            score(data)

    def test_mutant_raises_real_p6_ceiling(self):
        # Mutation is exercised by the real P6 path. Its frozen oracle rejects
        # the first over-capacity approval before any receipt can be emitted.
        with patch.object(p6, "MAX_SURFACES", 32):
            with self.assertRaises(ValidationError):
                fixture(32)
        with self.assertRaisesRegex(EvidenceError, "ceiling changed"):
            score(dict(self.large, surface_ceiling=32))

    def test_source_ceilings_and_resident_cohort(self):
        self.assertEqual(source_ceilings(DIRECT.parents[1]), {"MaxSurfaces": 16, "MAX_CAPSULES": 16})
        with self.assertRaisesRegex(EvidenceError, "ceiling evidence differs"):
            score(dict(self.small, source_ceilings={"MaxSurfaces": 16, "MAX_CAPSULES": 32}))
        data = deepcopy(self.small)
        data["cohort_end"][0]["incarnation"] = "replacement"
        with self.assertRaisesRegex(EvidenceError, "cohort identity changed"):
            score(data)

    def test_failures_and_refusals_remain_in_verified_denominator(self):
        data = deepcopy(self.small)
        for row in data["cycles"][:5]:
            fail_cycle(row, "refused")
        self.assertEqual(score(data)["result"], "PASS")
        fail_cycle(data["cycles"][5])
        result = score(data)
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(result["verified_percent"], 94)
        self.assertEqual(result["outcomes"]["refused"], 5)
        self.assertEqual(result["outcomes"]["failed"], 1)

    def test_scorer_mutant_excluding_refusals_is_rejected(self):
        source = getsource(score)
        boundary = "    performance = (\n"
        checks, _, scoring = source.partition(boundary)
        self.assertEqual(scoring.count("len(plan)"), 3)
        mutation = '    denominator = len(plan) - counts["refused"] - counts["capacity_refused"]\n'
        namespace = dict(vars(scorer))
        exec(compile(checks + mutation + boundary + scoring.replace("len(plan)", "denominator"),
                     "<T1 refusal denominator mutant>", "exec"), namespace)
        mutant = namespace["score"]

        direct = deepcopy(self.small)
        for row in direct["cycles"][:6]:
            fail_cycle(row, "refused")
        self.assertEqual(score(direct)["result"], "FAIL")
        self.assertEqual(mutant(direct)["result"], "PASS")

        for capture in (direct, self.large):
            with self.subTest(scenario=len(capture["roster"])):
                rows = capture["cycles"]
                verified = sum(row["outcome"] == "verified" for row in rows)
                expected = (len(rows), 100 * verified / len(rows))

                def check_denominator(result):
                    self.assertEqual((result["cycle_denominator"], result["verified_percent"]),
                                     expected, "every planned cycle must stay in the denominator")

                result = score(capture)
                check_denominator(result)
                mutant_result = mutant(capture)
                self.assertEqual(mutant_result["outcomes"], result["outcomes"])
                with self.assertRaisesRegex(AssertionError, "every planned cycle"):
                    check_denominator(mutant_result)

    def test_missing_or_misordered_clock_markers_and_short_run_fail(self):
        with self.assertRaisesRegex(EvidenceError, "shorter"):
            score(dict(self.small, end_mono_ns=self.small["end_mono_ns"] - 1))
        data = deepcopy(self.small)
        data["cycles"][0]["markers"]["t_first_byte"] = None
        with self.assertRaisesRegex(EvidenceError, "lacks clock markers"):
            score(data)
        data["cycles"][0]["markers"]["t_first_byte"] = 0
        with self.assertRaisesRegex(EvidenceError, "out of order"):
            score(data)

    def test_mutants_reuse_observation_drop_p7_ack_or_relabel_strata(self):
        data = deepcopy(self.small)
        for key in ("p4", "p7"):
            data["cycles"][1][key]["fresh_observation_id"] = data["cycles"][0][key]["fresh_observation_id"]
        with self.assertRaisesRegex(EvidenceError, "reused observation"):
            score(data)
        data = deepcopy(self.small)
        for key in ("p4", "p7"):
            data["cycles"][0][key]["acks"].remove("stream")
        with self.assertRaisesRegex(EvidenceError, "matching P4/P7"):
            score(data)
        data = deepcopy(self.small)
        data["cycles"][0]["broker_states"][0]["state"] = "OFF"
        with self.assertRaisesRegex(EvidenceError, "stratum differs"):
            score(data)

    def test_capacity_refusal_cannot_hide_memory_refusal(self):
        data = deepcopy(self.large)
        memory_refused = data["admissions"][-1]
        memory_refused["margin_percent"] = 10
        for row in data["cycles"]:
            if row["surface_id"] == memory_refused["surface_id"]:
                row["outcome"] = "refused"
        result = score(data)
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(result["admissions"]["other_refused"], 1)

    def test_latency_regressions_fail_current_stratum_or_queue(self):
        # Force the p95 (or p50) to cross its threshold by one nanosecond.
        for state, count, delay, boundary in (("ASLEEP", 3, 1000 * MS + 1, "wake"),
                                               ("OFF", 3, 3000 * MS + 1, "wake"),
                                               ("ASLEEP", 30, 250 * MS + 1, "wake"),
                                               ("OFF", 30, 1000 * MS + 1, "wake"),
                                               ("ASLEEP", 6, 500 * MS + 1, "queue")):
            with self.subTest(state=state, boundary=boundary, delay=delay):
                data = deepcopy(self.small)
                affected = [row for row in data["cycles"] if row["state"] == state][:count]
                for row in affected:
                    markers = row["markers"]
                    if boundary == "queue":
                        markers["t_admitted"] = markers["t_submit"] + delay
                        markers["t_first_byte"] = markers["t_admitted"] + 250 * MS
                    else:
                        markers["t_first_byte"] = markers["t_admitted"] + delay
                    markers["t_complete"] = markers["t_first_byte"] + MS
                    row["broker_states"][1]["since_mono_ns"] = markers["t_admitted"]
                    row["broker_states"][2]["since_mono_ns"] = markers["t_first_byte"]
                self.assertEqual(score(data)["result"], "FAIL")

    def test_live_script_compiles_without_running(self):
        path = T1 / "LIVE_t1.py"
        compile(path.read_bytes(), str(path), "exec")

    def test_live_cli_loads_public_lifecycle_before_opening_a_fixture(self):
        run = subprocess.run([sys.executable, "-I", "-B", str(T1 / "LIVE_t1.py"), "--help"],
                             capture_output=True, text=True, check=True)
        self.assertIn("manifest output", run.stdout)

    def test_replay_cli_reports_raw_coordinate_digest_and_invalid_evidence(self):
        with TemporaryDirectory(dir=os.environ["P7_TEST_RAM"]) as directory:
            capture = Path(directory) / "raw.json"
            invoke = [sys.executable, "-I", "-B", str(T1 / "score.py"), str(capture)]
            capture.write_text(json.dumps(self.large))
            run = subprocess.run(invoke, capture_output=True, text=True, check=True)
            report = json.loads(run.stdout)
            self.assertEqual(report["raw_capture"], str(capture))
            self.assertEqual(report["result"], "EXPECTED_CAPACITY_REFUSAL")
            self.assertEqual(len(report["raw_sha256"]), 64)
            capture.write_text(json.dumps(dict(self.large, cycles=[])))
            run = subprocess.run(invoke, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn("Missing or extra cycle receipt", run.stderr)

    def test_real_p4_p7_preserve_off_and_asleep_wake_boundaries(self):
        fixture_case = p7_fixtures.ReceiptTests()
        self.addCleanup(fixture_case.doCleanups)
        verifier = TransitionVerifier()
        with patch.object(fixture_case, "publish", verifier):
            fixture_case.setUp()
        owner = fixture_case.lifecycle
        observations = set()
        for state in ("OFF", "ASLEEP"):
            p4, p7, states, observation = verifier.call(owner, "wake")
            self.assertEqual(p4, p7)
            self.assertEqual([row["state"] for row in states], [state, "WAKING", "ACTIVE"])
            self.assertEqual(observation.observation_id, p4["fresh_observation_id"])
            self.assertNotIn(observation.observation_id, observations)
            observations.add(observation.observation_id)
            verify_frame(owner, observation)
            verifier.call(owner, "sleep" if state == "OFF" else "off")
            with self.assertRaisesRegex(LifecycleError, "^NO_LEASE$"):
                owner.read_frame(observation.observation_id)
        with self.assertRaisesRegex(EvidenceError, "P7 lacks independent verification"):
            verifier.call(owner, "off")  # An unchanged state produces no publication.

    def test_mutant_bypassing_p7_verification_fails_transition_hook_oracle(self):
        def awake_fixture():
            fixture_case = p7_fixtures.ReceiptTests()
            self.addCleanup(fixture_case.doCleanups)
            verifier = TransitionVerifier()
            with patch.object(fixture_case, "publish", verifier):
                fixture_case.setUp()
            receipt, verified, _, _ = verifier.call(fixture_case.lifecycle, "wake")
            self.assertEqual(verified, receipt)
            return verifier, fixture_case.lifecycle

        def require_p7_refusal(verifier, owner):
            with patch("benchmark.verify_transition", side_effect=EvidenceError("P7 contrast")):
                with self.assertRaisesRegex(LifecycleError, "^HOLD$") as raised:
                    verifier.call(owner, "sleep")
            self.assertIsInstance(raised.exception.__cause__, EvidenceError)
            self.assertEqual(str(raised.exception.__cause__), "P7 contrast")

        require_p7_refusal(*awake_fixture())
        verifier, owner = awake_fixture()

        def bypass_p7(self, receipt, proof):
            self._checked.append((receipt, proof))

        with patch.object(TransitionVerifier, "__call__", bypass_p7):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                require_p7_refusal(verifier, owner)
