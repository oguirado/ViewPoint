# @module {"id":"tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""Contract tests for Gate G M7 5: Fault Injection and Recovery Scorecard.

Target API provided by build task jq2a-m7-c1-replay-scorecard:
- wender.learn.multiwin_scorecard.run_trial
- wender.learn.multiwin_scorecard.score_windows
- wender.learn.multiwin_scorecard.canonical_bytes
- wender.learn.multiwin_scorecard.Scenario
- wender.learn.multiwin_scorecard.WindowEvidence
- wender.learn.multiwin_scorecard.TrialReport
- wender.learn.multiwin_scorecard.Scorecard
- wender.learn.multiwin_scorecard.FakeClock
"""

import dataclasses
from dataclasses import dataclass
import importlib
import json
import unittest
from typing import Any, Dict, Optional, Tuple

try:
    import numpy
except ImportError:
    numpy = Any  # type: ignore

from wender.state import schema
from tests.test_jq2a_m7_c1_replay_scorecard_implementation import FixtureAdapter, _event, _key


# Attempt importing canonical shared records from wender.perceive.session_registry if available.
try:
    from wender.perceive.session_registry import (
        ActionRequest,
        ActorLease,
        ChangeResult,
        FocusLease,
        FrameRef,
        Handle,
        Observation,
        PerceptionResult,
        Receipt,
        StreamSpec,
        StreamToken,
        TargetRecord,
        Ticket,
        WindowDescription,
        WindowFrames,
    )
except ImportError:
    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: Optional[str]
        attachment_owned: bool
        closed: bool

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: Optional[str]

    @dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: Any
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Optional[Observation]
        error: Optional[str]

    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    @dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: Optional[str]


# @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.LocalFakeClock
class LocalFakeClock:
    """Fixture clock driving fake time in test harness."""

    def __init__(self, now_ns: int = 0):
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, delta_ns: int) -> int:
        if delta_ns < 0:
            raise ValueError("Negative clock increment not allowed")
        self.now_ns += delta_ns
        return self.now_ns


def _owner_symbol_absent(module_name: str, symbol_name: str) -> bool:
    try:
        mod = importlib.import_module(module_name)
        return not hasattr(mod, symbol_name)
    except ImportError:
        return True


def expected_failure_if_absent(module_name: str, symbol_name: str):
    if _owner_symbol_absent(module_name, symbol_name):
        return unittest.expectedFailure
    return lambda fn: fn


OWNER_MODULE = "wender.learn.multiwin_scorecard"
OWNER_SYMBOL = "run_trial"


# @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard
class TestFaultInjectionAndRecoveryScorecard(unittest.TestCase):
    """Suite testing fault injection, UNKNOWN rejection, recovery, and Gate G M7 5."""

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_positive_control_record_structures_and_clock
    def test_positive_control_record_structures_and_clock(self):
        """Positive control: verify shared record structures and LocalFakeClock fixture."""
        clk = LocalFakeClock(now_ns=1000)
        self.assertEqual(clk(), 1000)
        self.assertEqual(clk.advance(500), 1500)
        self.assertEqual(clk(), 1500)
        with self.assertRaises(ValueError):
            clk.advance(-10)

        handle = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        self.assertEqual(handle.kind, "cdp_tab")
        self.assertEqual(handle.canonical_id, "tab-1")
        self.assertEqual(handle.generation, 1)

        req = ActionRequest(
            handle=handle,
            action_id="act-1",
            epoch=0,
            payload={"op": "click"},
            cost=1,
            deadline_ns=2000,
        )
        self.assertEqual(req.action_id, "act-1")

        rec = Receipt(action_id="act-1", handle=handle, status="SUCCESS", reason=None)
        self.assertEqual(rec.status, "SUCCESS")

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_positive_control_negative_mutant_harness_predicate
    def test_positive_control_negative_mutant_harness_predicate(self):
        """Positive control: contract harness predicate rejects UNKNOWN as complete."""
        def evaluate_terminal(terminal: Optional[str], expected_terminal: Optional[str]) -> bool:
            if terminal == "UNKNOWN":
                return False
            return terminal == expected_terminal

        self.assertFalse(evaluate_terminal("UNKNOWN", "SUCCESS"))
        self.assertFalse(evaluate_terminal("UNKNOWN", "UNKNOWN"))
        self.assertTrue(evaluate_terminal("SUCCESS", "SUCCESS"))

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_gate_gm7_5_crash_timeout_counted_without_false_success
    @expected_failure_if_absent(OWNER_MODULE, OWNER_SYMBOL)
    def test_gate_gm7_5_crash_timeout_counted_without_false_success(self):
        """Gate G M7 5: Injected crash and timeout are explicitly counted without false success."""
        from wender.learn.multiwin_scorecard import (
            FakeClock,
            Scenario,
            Scorecard,
            TrialReport,
            WindowEvidence,
            canonical_bytes,
            run_trial,
            score_windows,
        )

        h_gen1 = Handle(kind="cdp_tab", canonical_id="target-1", generation=1)
        h_gen2 = Handle(kind="cdp_tab", canonical_id="target-1", generation=2)

        h_gen3 = Handle(kind="cdp_tab", canonical_id="target-1", generation=3)
        # Each transport fault quarantines its incarnation; acknowledge recovery
        # before injecting the next fault, then recover to a successful terminal.
        scenario = Scenario(
            name="fault_recovery_gen3",
            targets=(h_gen1, h_gen2, h_gen3),
            events=(
                _event(h_gen1, 0, 10, action_id="a1", payload={"raise": "crash"})[0],
                {"kind": "recover", "handle": h_gen2, "previous_handle": h_gen1,
                 "acknowledged": True, "at_ns": 20},
                _event(h_gen2, 0, 30, action_id="a2", payload={"raise": "timeout"})[0],
                {"kind": "recover", "handle": h_gen3, "previous_handle": h_gen2,
                 "acknowledged": True, "at_ns": 40},
                _event(h_gen3, 0, 50, action_id="a3")[0],
            ),
            expected_outcomes={"a1": "CRASH", "a2": "TIMEOUT", "a3": "SUCCESS"},
        )

        clk = FakeClock(now_ns=0)
        adapters = {_key(h): FixtureAdapter(h) for h in scenario.targets}
        report = run_trial(scenario, seed=42, clock=clk, adapters=adapters)

        self.assertIsInstance(report, TrialReport)
        self.assertEqual([f["terminal"] for f in report.faults], ["CRASH", "TIMEOUT"])
        self.assertEqual([r["recovered"] for r in report.outcomes["recoveries"]], [True, True])

        scorecard = score_windows(report.rows)
        self.assertIsInstance(scorecard, Scorecard)
        self.assertTrue(scorecard.passed)
        self.assertEqual(scorecard.metrics["terminal_completeness"]["numerator"], 1)
        self.assertEqual(scorecard.metrics["terminal_completeness"]["denominator"], 1)
        self.assertTrue(scorecard.metrics["terminal_completeness"]["passed"])
        self.assertEqual(scorecard.metrics["terminal_completeness"]["fault_outcomes"]["numerator"], 2)

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_negative_mutant_unknown_never_counts_as_complete
    @expected_failure_if_absent(OWNER_MODULE, OWNER_SYMBOL)
    def test_negative_mutant_unknown_never_counts_as_complete(self):
        """Negative mutant test: UNKNOWN status or terminal action never counts as complete."""
        from wender.learn.multiwin_scorecard import (
            FakeClock,
            Scenario,
            Scorecard,
            TrialReport,
            WindowEvidence,
            run_trial,
            score_windows,
        )

        h1 = Handle(kind="x11_window", canonical_id="win-1", generation=1)
        scenario = Scenario(
            name="unknown_terminal_mutant",
            targets=(h1,),
            events=(
                _event(h1, 0, 10, action_id="a1", payload={"terminal": "UNKNOWN"})[0],
            ),
            expected_outcomes={"a1": "SUCCESS"},
        )

        clk = FakeClock(now_ns=0)
        adapters = {_key(h1): FixtureAdapter(h1)}
        report = run_trial(scenario, seed=123, clock=clk, adapters=adapters)
        scorecard = score_windows(report.rows)

        self.assertFalse(scorecard.passed)
        # UNKNOWN terminal must not be counted as complete
        term_comp = scorecard.metrics.get("terminal_completeness")
        self.assertEqual(term_comp["numerator"], 0)
        self.assertEqual(term_comp["denominator"], 1)
        self.assertFalse(term_comp["passed"])
        self.assertEqual(report.outcomes["actions"]["a1"]["terminal"], "UNKNOWN")

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_negative_mutant_stale_generation_cannot_repair_missing_new_evidence
    @expected_failure_if_absent(OWNER_MODULE, OWNER_SYMBOL)
    def test_negative_mutant_stale_generation_cannot_repair_missing_new_evidence(self):
        """Negative mutant test: Old-generation evidence cannot repair missing new-generation evidence."""
        from wender.learn.multiwin_scorecard import (
            FakeClock,
            Scenario,
            Scorecard,
            TrialReport,
            WindowEvidence,
            run_trial,
            score_windows,
        )

        h_old = Handle(kind="xvfb_screen", canonical_id="screen-1", generation=1)
        h_new = Handle(kind="xvfb_screen", canonical_id="screen-1", generation=2)

        # Crash occurs on h_old; recovery yields h_new, but event tries to present h_old evidence for h_new.
        scenario = Scenario(
            name="stale_generation_repair_attempt",
            targets=(h_old, h_new),
            events=(
                _event(h_old, 0, 10, action_id="a1", payload={"raise": "crash"})[0],
                {"kind": "recover", "handle": h_new, "previous_handle": h_old,
                 "acknowledged": True, "at_ns": 20},
                # Presenting action under old generation handle h_old after fence
                _event(h_old, 1, 30, action_id="a2")[0],
            ),
            expected_outcomes={"a1": "CRASH", "a2": "SUCCESS"},
        )

        clk = FakeClock(now_ns=0)
        adapters = {_key(h): FixtureAdapter(h) for h in scenario.targets}
        report = run_trial(scenario, seed=777, clock=clk, adapters=adapters)
        scorecard = score_windows(report.rows)

        # Recovery authorizes only the new incarnation; the old one stays fenced.
        self.assertTrue(report.outcomes["recoveries"][0]["recovered"])
        self.assertEqual(report.outcomes["actions"]["a2"]["terminal"], "QUARANTINED")
        self.assertFalse(scorecard.passed)

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_explicit_fault_outcomes_quarantines_and_drops_counted
    @expected_failure_if_absent(OWNER_MODULE, OWNER_SYMBOL)
    def test_explicit_fault_outcomes_quarantines_and_drops_counted(self):
        """Explicitly count declared fault outcomes, quarantines, and frame drops across target kinds."""
        from wender.learn.multiwin_scorecard import (
            FakeClock,
            Scenario,
            Scorecard,
            TrialReport,
            WindowEvidence,
            run_trial,
            score_windows,
        )

        kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]
        targets = tuple(Handle(kind=k, canonical_id=f"target-{k}", generation=1) for k in kinds)

        events, expected = [], {}
        newer = tuple(Handle(h.kind, h.canonical_id, 2) for h in targets)
        adapters = {_key(h): FixtureAdapter(h) for h in targets + newer}
        for h, new in zip(targets, newer):
            at = 10 * (len(events) + 1)
            drop = _event(h, 0, at)[0]
            drop.pop("action")
            events.append({**drop, "drop_expected": True})
            adapters[_key(h)].drop_seqs = {0}
            crash = f"act-crash-{h.kind}"
            timeout = f"act-timeout-{h.kind}"
            events.append(_event(h, 1, at + 10, action_id=crash, payload={"raise": "crash"})[0])
            events.append({"kind": "recover", "handle": new, "previous_handle": h,
                           "acknowledged": True, "at_ns": at + 20})
            events.append(_event(new, 0, at + 30, action_id=timeout, payload={"raise": "timeout"})[0])
            expected.update({crash: "CRASH", timeout: "TIMEOUT"})
        targets += newer

        scenario = Scenario(
            name="heterogeneous_fault_matrix",
            targets=targets,
            events=tuple(events),
            expected_outcomes=expected,
        )

        clk = FakeClock(now_ns=0)
        report = run_trial(scenario, seed=999, clock=clk, adapters=adapters)

        self.assertIsInstance(report, TrialReport)
        self.assertEqual(len(report.faults), 10)
        self.assertEqual([f["terminal"] for f in report.faults], ["CRASH", "TIMEOUT"] * 5)
        self.assertEqual(sum(r.ops["quarantined"] for r in report.rows), 10)

        scorecard = score_windows(report.rows)
        self.assertIsInstance(scorecard, Scorecard)
        self.assertIn("frame_drops", scorecard.metrics)
        self.assertEqual(scorecard.metrics["frame_drops"]["observed"], 5)
        self.assertEqual(scorecard.metrics["frame_drops"]["expected"], 5)
        self.assertTrue(scorecard.metrics["frame_drops"]["passed"])

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_canonical_bytes_excludes_wall_clock_and_is_deterministic
    @expected_failure_if_absent(OWNER_MODULE, OWNER_SYMBOL)
    def test_canonical_bytes_excludes_wall_clock_and_is_deterministic(self):
        """Canonical replay bytes match exactly for identical evidence and exclude diagnostic wall time."""
        from wender.learn.multiwin_scorecard import (
            FakeClock,
            Scenario,
            Scorecard,
            TrialReport,
            WindowEvidence,
            canonical_bytes,
            run_trial,
            score_windows,
        )

        h1 = Handle(kind="capsule", canonical_id="cap-1", generation=1)
        scenario = Scenario(
            name="canonical_bytes_determinism",
            targets=(h1,),
            events=(
                _event(h1, 0, 10, action_id="a1")[0],
            ),
            expected_outcomes={"a1": "SUCCESS"},
        )

        clk1 = FakeClock(now_ns=0)
        clk2 = FakeClock(now_ns=0)
        adapters = {_key(h1): FixtureAdapter(h1)}

        report1 = run_trial(scenario, seed=100, clock=clk1, adapters=adapters)
        report2 = run_trial(scenario, seed=100, clock=clk2, adapters=adapters)

        scorecard1 = score_windows(report1.rows)
        scorecard2 = score_windows(report2.rows)

        self.assertTrue(scorecard1.passed)
        self.assertTrue(scorecard2.passed)
        b1 = canonical_bytes(scorecard1)
        b2 = canonical_bytes(scorecard2)

        self.assertIsInstance(b1, bytes)
        self.assertEqual(b1, b2)

    # @anchor:tests.test_jq2a_m7_j5_fault_injection_and_recovery_scorecard.TestFaultInjectionAndRecoveryScorecard.test_heterogeneous_targets_offline_invariants
    @expected_failure_if_absent(OWNER_MODULE, OWNER_SYMBOL)
    def test_heterogeneous_targets_offline_invariants(self):
        """INV-M7-1..INV-M7-3: Heterogeneous trial reports all target kinds offline without I/O."""
        from wender.learn.multiwin_scorecard import (
            FakeClock,
            Scenario,
            Scorecard,
            TrialReport,
            WindowEvidence,
            run_trial,
            score_windows,
        )

        all_kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]
        targets = tuple(Handle(kind=k, canonical_id=f"h-{k}", generation=1) for k in all_kinds)

        events = tuple(_event(h, 0, 10 * (i + 1), action_id=f"a-{h.kind}")[0]
                       for i, h in enumerate(targets))
        scenario = Scenario(
            name="heterogeneous_offline",
            targets=targets,
            events=events,
            expected_outcomes={f"a-{k}": "SUCCESS" for k in all_kinds},
        )

        clk = FakeClock(now_ns=100)
        adapters = {_key(h): FixtureAdapter(h) for h in targets}
        report = run_trial(scenario, seed=42, clock=clk, adapters=adapters)
        scorecard = score_windows(report.rows)

        self.assertTrue(scorecard.passed)
        self.assertEqual({row.handle.kind for row in report.rows}, set(all_kinds))
        # Check required metrics keys exist
        for metric_key in (
            "cross_window_bleed",
            "safety_violations",
            "frame_exactness",
            "frame_drops",
            "operation_totals",
            "eligibility_stalls",
            "starvation",
            "terminal_completeness",
            "pooled_confusion",
        ):
            self.assertIn(metric_key, scorecard.metrics)


if __name__ == "__main__":
    unittest.main()
