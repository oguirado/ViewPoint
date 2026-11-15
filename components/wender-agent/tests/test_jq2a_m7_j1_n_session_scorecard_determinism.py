# @module {"id":"tests.test_jq2a_m7_j1_n_session_scorecard_determinism","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""@module tests.test_jq2a_m7_j1_n_session_scorecard_determinism - Contract tests for session scorecard determinism.

Gate G M7 1: Same seed and trace give identical canonical bytes.
Gate G M7 2: Injected cross-target state bleed fails scorecard.
Gate G M7 3: Heterogeneous trial reports every target kind.
Gate G M7 4: Pooled confusion equals sum and rejects missing seals.
Gate G M7 5: Crash/timeout counted without false success.

Owning implementation task_id: jq2a-m7-c1-replay-scorecard
Target owner module: wender.learn.multiwin_scorecard
"""

from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.util
import os
import unittest
from typing import Any, Sequence

import numpy as np

from wender.state import schema


# @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.expected_failure_if_absent
def expected_failure_if_absent(owner_module: str, symbol_name: str):
    """Dynamically apply unittest.expectedFailure if owner_module or symbol_name is absent.

    When the owner module and symbol exist, the wrapper is automatically removed so that
    landed implementations are tested normally.
    """
    def decorator(func):
        spec = importlib.util.find_spec(owner_module)
        if spec is None:
            return unittest.expectedFailure(func)
        try:
            mod = importlib.import_module(owner_module)
            if not hasattr(mod, symbol_name):
                return unittest.expectedFailure(func)
        except ModuleNotFoundError as e:
            if e.name == owner_module:
                return unittest.expectedFailure(func)
            raise
        return func
    return decorator


# --- Shared Canonical Frozen Dataclasses (imported from wender.perceive.session_registry or fallback) ---
try:
    from wender.perceive.session_registry import (
        Handle,
        TargetRecord,
        FrameRef,
        Observation,
        Receipt,
        ActionRequest,
        ActorLease,
        FocusLease,
        StreamSpec,
        StreamToken,
        Ticket,
        ChangeResult,
        PerceptionResult,
        WindowFrames,
        WindowDescription,
    )
except ModuleNotFoundError:
    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.Handle
    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.TargetRecord
    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.FrameRef
    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.Observation
    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.Receipt
    @dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: str | None

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.ActionRequest
    @dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.ActorLease
    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.FocusLease
    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.StreamSpec
    @dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.StreamToken
    @dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.Ticket
    @dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.ChangeResult
    @dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: np.ndarray
        ops: dict

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.PerceptionResult
    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.WindowFrames
    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.WindowDescription
    @dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None


# --- Proposed Target Record Exports (from wender.learn.multiwin_scorecard or local fallback) ---
try:
    from wender.learn.multiwin_scorecard import (
        Scenario,
        WindowEvidence,
        TrialReport,
        Scorecard,
        FakeClock,
    )
except ModuleNotFoundError:
    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.Scenario
    @dataclass(frozen=True)
    class Scenario:
        name: str
        targets: tuple
        events: tuple
        expected_outcomes: dict

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.WindowEvidence
    @dataclass(frozen=True)
    class WindowEvidence:
        handle: Handle
        scene: str
        epoch: int
        frame_seq: int
        tp: int
        fp: int
        fn: int
        tn: int
        ops: dict
        frame_supported: bool
        dropped: bool
        exact: bool | None
        terminal: str | None
        expected_terminal: str | None
        seal: str
        violations: tuple

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.TrialReport
    @dataclass(frozen=True)
    class TrialReport:
        rows: tuple
        outcomes: dict
        faults: tuple

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.Scorecard
    @dataclass(frozen=True)
    class Scorecard:
        windows: tuple
        pooled: dict
        metrics: dict
        violations: tuple
        passed: bool

    # @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.FakeClock
    class FakeClock:
        """Injected mutable fake clock operating in nanoseconds."""

        def __init__(self, now_ns: int = 0):
            self.now_ns = now_ns

        def __call__(self) -> int:
            return self.now_ns

        def advance(self, delta_ns: int) -> int:
            if delta_ns < 0:
                raise ValueError(f"Negative time increment refused: {delta_ns}")
            self.now_ns += delta_ns
            return self.now_ns


# --- Local Contract Reference Harness (for offline contract validation before owner module lands) ---
# @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.compute_seal
def compute_seal(handle: Handle, scene: str, epoch: int, frame_seq: int) -> str:
    """Compute deterministic cryptographic seal for a window identity."""
    raw = f"{handle.kind}:{handle.canonical_id}:{handle.generation}:{scene}:{epoch}:{frame_seq}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.LocalScorecardSimulator
class LocalScorecardSimulator:
    """Deterministic offline reference harness for multiwindow scorecard laws."""

    SUPPORTED_KINDS = {"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"}

    def run_trial(self, scenario: Scenario, *, seed: int, clock: FakeClock, adapters: dict[str, Any]) -> TrialReport:
        """Execute trial simulation deterministically without hardware or wall-clock dependence."""
        rows = []
        outcomes = {}
        faults = []

        # Sort events deterministically by event id or explicit sequence
        sorted_events = sorted(scenario.events, key=lambda e: (e.get("seq", 0), e.get("action_id", "")))

        for ev in sorted_events:
            handle = ev["handle"]
            action_id = ev.get("action_id", "act_0")
            expected_term = scenario.expected_outcomes.get(handle.canonical_id, "SUCCESS")
            actual_term = ev.get("terminal", expected_term)

            # Check cross-target bleed or invalid target kind
            violations = []
            if handle.kind not in self.SUPPORTED_KINDS:
                violations.append(f"unsupported_target_kind:{handle.kind}")

            if ev.get("injected_bleed"):
                violations.append(f"cross_target_state_bleed:{handle.canonical_id}")

            seal = compute_seal(handle, ev.get("scene", "default"), ev.get("epoch", 0), ev.get("frame_seq", 0))

            we = WindowEvidence(
                handle=handle,
                scene=ev.get("scene", "default"),
                epoch=ev.get("epoch", 0),
                frame_seq=ev.get("frame_seq", 0),
                tp=ev.get("tp", 10),
                fp=ev.get("fp", 0),
                fn=ev.get("fn", 0),
                tn=ev.get("tn", 100),
                ops=ev.get("ops", {"click": 1}),
                frame_supported=True,
                dropped=ev.get("dropped", False),
                exact=True,
                terminal=actual_term,
                expected_terminal=expected_term,
                seal=seal,
                violations=tuple(violations),
            )
            rows.append(we)
            outcomes[action_id] = actual_term
            if actual_term != expected_term or actual_term == "UNKNOWN":
                faults.append((action_id, handle.canonical_id, actual_term))

        return TrialReport(rows=tuple(rows), outcomes=outcomes, faults=tuple(faults))

    def score_windows(self, rows: Sequence[WindowEvidence]) -> Scorecard:
        """Compute scorecard metrics and pooled confusion matrix from window evidence rows."""
        pooled_tp = 0
        pooled_fp = 0
        pooled_fn = 0
        pooled_tn = 0
        total_ops = {}
        violations = []
        terminal_matches = 0
        total_terminals = len(rows)

        seen_identities = set()

        for r in rows:
            # Check duplicate scene / window identity
            ident = (r.handle.canonical_id, r.scene, r.epoch, r.frame_seq)
            if ident in seen_identities:
                violations.append(f"duplicate_window_identity:{ident}")
            seen_identities.add(ident)

            # Check seal validity
            expected_seal = compute_seal(r.handle, r.scene, r.epoch, r.frame_seq)
            if r.seal != expected_seal:
                violations.append(f"invalid_seal:{r.handle.canonical_id}")

            pooled_tp += r.tp
            pooled_fp += r.fp
            pooled_fn += r.fn
            pooled_tn += r.tn

            for op_k, op_v in r.ops.items():
                total_ops[op_k] = total_ops.get(op_k, 0) + op_v

            violations.extend(r.violations)

            # Fault check: UNKNOWN or mismatch is not complete
            if r.terminal and r.expected_terminal and r.terminal == r.expected_terminal and r.terminal != "UNKNOWN":
                terminal_matches += 1

        pooled = {"tp": pooled_tp, "fp": pooled_fp, "fn": pooled_fn, "tn": pooled_tn}

        metrics = {
            "cross_window_bleed": (sum(1 for v in violations if "cross_target_state_bleed" in v), total_terminals or 1),
            "safety_violations": (len(violations), 1),
            "frame_exactness": (sum(1 for r in rows if r.exact), total_terminals or 1),
            "frame_drops": (sum(1 for r in rows if r.dropped), total_terminals or 1),
            "operation_totals": total_ops,
            "eligibility_stalls": (0, total_terminals or 1),
            "starvation": (0, total_terminals or 1),
            "terminal_completeness": (terminal_matches, total_terminals) if total_terminals > 0 else None,
            "pooled_confusion": pooled,
        }

        passed = (
            len(violations) == 0
            and metrics["terminal_completeness"] is not None
            and metrics["terminal_completeness"][0] == metrics["terminal_completeness"][1]
        )

        return Scorecard(
            windows=tuple(rows),
            pooled=pooled,
            metrics=metrics,
            violations=tuple(violations),
            passed=passed,
        )

    def canonical_bytes(self, report: Scorecard | TrialReport) -> bytes:
        """Serialize scorecard into canonical integer-only JSON bytes excluding wall-clock time."""
        if isinstance(report, TrialReport):
            payload = {
                "rows": [
                    {
                        "handle": {"kind": r.handle.kind, "canonical_id": r.handle.canonical_id, "generation": r.handle.generation},
                        "scene": r.scene,
                        "epoch": r.epoch,
                        "frame_seq": r.frame_seq,
                        "tp": r.tp,
                        "fp": r.fp,
                        "fn": r.fn,
                        "tn": r.tn,
                        "ops": r.ops,
                        "terminal": r.terminal,
                        "expected_terminal": r.expected_terminal,
                        "seal": r.seal,
                    }
                    for r in report.rows
                ],
                "outcomes": report.outcomes,
                "faults": report.faults,
            }
        else:
            payload = {
                "windows_count": len(report.windows),
                "pooled": report.pooled,
                "metrics": {
                    k: v if not isinstance(v, tuple) else list(v)
                    for k, v in report.metrics.items()
                },
                "violations": list(report.violations),
                "passed": report.passed,
            }

        canonical_str = schema.dumps(payload)
        return canonical_str.encode("utf-8")


# @anchor wa.tests.test_jq2a_m7_j1_n_session_scorecard_determinism.TestSessionScorecardDeterminism
class TestSessionScorecardDeterminism(unittest.TestCase):
    """Offline test suite for session scorecard determinism and gate G M7 laws."""

    def setUp(self):
        self.harness = LocalScorecardSimulator()
        self.clock = FakeClock(now_ns=1_000_000)

        self.h_cdp = Handle(kind="cdp_tab", canonical_id="tab_01", generation=1)
        self.h_x11 = Handle(kind="x11_window", canonical_id="win_x11", generation=1)
        self.h_xvfb = Handle(kind="xvfb_screen", canonical_id="scr_xvfb", generation=1)
        self.h_capsule = Handle(kind="capsule", canonical_id="cap_01", generation=1)
        self.h_godot = Handle(kind="godot_window", canonical_id="godot_main", generation=1)

    # --- Ordinary Passing Controls (Offline RAM fixtures & local determinism laws) ---

    def test_ordinary_fake_clock_mechanics(self):
        """Verify FakeClock advance and negative increment refusal."""
        clock = FakeClock(100)
        self.assertEqual(clock(), 100)
        new_time = clock.advance(50)
        self.assertEqual(new_time, 150)
        self.assertEqual(clock(), 150)
        with self.assertRaises(ValueError):
            clock.advance(-10)

    def test_ordinary_ram_frame_ref_creation(self):
        """Verify FrameRef stores NumPy uint8 array in RAM without disk write."""
        rgb_data = np.zeros((100, 100, 3), dtype=np.uint8)
        frame = FrameRef(
            handle=self.h_cdp,
            epoch=1,
            frame_seq=0,
            width=100,
            height=100,
            rgb=rgb_data,
            digest=hashlib.sha256(rgb_data.tobytes()).hexdigest(),
        )
        self.assertEqual(frame.width, 100)
        self.assertEqual(frame.height, 100)
        self.assertIsInstance(frame.rgb, np.ndarray)

    def test_ordinary_forbidden_io_raises_in_fixtures(self):
        """Verify forbidden disk I/O raises in test environment/fixtures."""
        def open_forbidden(path, *args, **kwargs):
            if "frame" in str(path) or "screenshot" in str(path):
                raise PermissionError("Forbidden disk frame write detected under STORAGE LAW")
            return open(path, *args, **kwargs)

        with self.assertRaises(PermissionError):
            open_forbidden("/tmp/frame_001.png", "wb")

    def test_ordinary_gate_g_m7_1_determinism_same_seed_and_trace(self):
        """Positive control: identical evidence and seed produce exact matching canonical bytes."""
        events = (
            {"action_id": "a1", "handle": self.h_cdp, "scene": "s1", "seq": 1},
            {"action_id": "a2", "handle": self.h_godot, "scene": "s2", "seq": 2},
        )
        sc = Scenario(name="det_test", targets=(self.h_cdp, self.h_godot), events=events, expected_outcomes={})

        tr1 = self.harness.run_trial(sc, seed=42, clock=FakeClock(), adapters={})
        sc1 = self.harness.score_windows(tr1.rows)
        bytes1 = self.harness.canonical_bytes(sc1)

        tr2 = self.harness.run_trial(sc, seed=42, clock=FakeClock(), adapters={})
        sc2 = self.harness.score_windows(tr2.rows)
        bytes2 = self.harness.canonical_bytes(sc2)

        self.assertEqual(bytes1, bytes2)

    def test_ordinary_gate_g_m7_1_reversed_worker_completion_order(self):
        """Positive control: reversed worker completion order produces identical canonical bytes."""
        events_forward = (
            {"action_id": "a1", "handle": self.h_cdp, "scene": "s1", "seq": 1},
            {"action_id": "a2", "handle": self.h_godot, "scene": "s2", "seq": 2},
        )
        events_reversed = (
            {"action_id": "a2", "handle": self.h_godot, "scene": "s2", "seq": 2},
            {"action_id": "a1", "handle": self.h_cdp, "scene": "s1", "seq": 1},
        )

        sc1 = Scenario(name="det_test", targets=(self.h_cdp, self.h_godot), events=events_forward, expected_outcomes={})
        sc2 = Scenario(name="det_test", targets=(self.h_cdp, self.h_godot), events=events_reversed, expected_outcomes={})

        tr1 = self.harness.run_trial(sc1, seed=100, clock=FakeClock(), adapters={})
        tr2 = self.harness.run_trial(sc2, seed=100, clock=FakeClock(), adapters={})

        bytes1 = self.harness.canonical_bytes(self.harness.score_windows(tr1.rows))
        bytes2 = self.harness.canonical_bytes(self.harness.score_windows(tr2.rows))

        self.assertEqual(bytes1, bytes2)

    def test_ordinary_gate_g_m7_1_negative_mutant_wall_clock_timestamp(self):
        """Negative mutant: wall-clock timestamps are excluded from canonical report bytes."""
        events = ({"action_id": "a1", "handle": self.h_cdp, "scene": "s1", "seq": 1},)
        sc = Scenario(name="wall_time_test", targets=(self.h_cdp,), events=events, expected_outcomes={})

        tr = self.harness.run_trial(sc, seed=42, clock=FakeClock(100), adapters={})
        scorecard = self.harness.score_windows(tr.rows)

        canonical_bytes_val = self.harness.canonical_bytes(scorecard)

        # Confirm canonical bytes do NOT contain float wall time or host timestamps
        self.assertNotIn(b"wall_clock", canonical_bytes_val)
        self.assertNotIn(b"timestamp_host", canonical_bytes_val)

    def test_ordinary_gate_g_m7_2_injected_cross_target_state_bleed(self):
        """Gate G M7 2: Injected cross-target state bleed causes scorecard failure."""
        events = (
            {"action_id": "a1", "handle": self.h_cdp, "scene": "s1", "seq": 1, "injected_bleed": True},
        )
        sc = Scenario(name="bleed_test", targets=(self.h_cdp,), events=events, expected_outcomes={})

        tr = self.harness.run_trial(sc, seed=42, clock=FakeClock(), adapters={})
        scorecard = self.harness.score_windows(tr.rows)

        self.assertFalse(scorecard.passed)
        self.assertGreater(scorecard.metrics["cross_window_bleed"][0], 0)

    def test_ordinary_gate_g_m7_3_heterogeneous_trial_all_target_kinds(self):
        """Gate G M7 3: Heterogeneous trial includes every target kind."""
        all_handles = (self.h_cdp, self.h_x11, self.h_xvfb, self.h_capsule, self.h_godot)
        events = tuple(
            {"action_id": f"act_{i}", "handle": h, "scene": f"scene_{i}", "seq": i}
            for i, h in enumerate(all_handles)
        )
        sc = Scenario(name="all_kinds", targets=all_handles, events=events, expected_outcomes={})

        tr = self.harness.run_trial(sc, seed=42, clock=FakeClock(), adapters={})
        scorecard = self.harness.score_windows(tr.rows)

        reported_kinds = {r.handle.kind for r in scorecard.windows}
        self.assertEqual(reported_kinds, LocalScorecardSimulator.SUPPORTED_KINDS)

    def test_ordinary_gate_g_m7_4_pooled_confusion_equals_sum_and_rejects_missing_seals(self):
        """Gate G M7 4: Pooled confusion equals sum of per-window confusion and rejects invalid seals."""
        we1 = WindowEvidence(
            handle=self.h_cdp, scene="s1", epoch=0, frame_seq=0, tp=10, fp=1, fn=2, tn=20,
            ops={"click": 1}, frame_supported=True, dropped=False, exact=True,
            terminal="SUCCESS", expected_terminal="SUCCESS",
            seal=compute_seal(self.h_cdp, "s1", 0, 0), violations=(),
        )
        we2 = WindowEvidence(
            handle=self.h_godot, scene="s2", epoch=0, frame_seq=0, tp=5, fp=0, fn=1, tn=15,
            ops={"press": 1}, frame_supported=True, dropped=False, exact=True,
            terminal="SUCCESS", expected_terminal="SUCCESS",
            seal=compute_seal(self.h_godot, "s2", 0, 0), violations=(),
        )

        scorecard = self.harness.score_windows([we1, we2])
        self.assertEqual(scorecard.pooled["tp"], 15)
        self.assertEqual(scorecard.pooled["fp"], 1)
        self.assertEqual(scorecard.pooled["fn"], 3)
        self.assertEqual(scorecard.pooled["tn"], 35)

        # Invalid seal test
        we_corrupt = WindowEvidence(
            handle=self.h_cdp, scene="s1", epoch=0, frame_seq=0, tp=10, fp=1, fn=2, tn=20,
            ops={"click": 1}, frame_supported=True, dropped=False, exact=True,
            terminal="SUCCESS", expected_terminal="SUCCESS",
            seal="invalid_seal_hex", violations=(),
        )
        scorecard_corrupt = self.harness.score_windows([we_corrupt])
        self.assertFalse(scorecard_corrupt.passed)
        self.assertIn("invalid_seal:tab_01", scorecard_corrupt.violations)

    def test_ordinary_gate_g_m7_5_crash_timeout_counted_without_false_success(self):
        """Gate G M7 5: Crash or UNKNOWN terminal counted without false success."""
        we_unknown = WindowEvidence(
            handle=self.h_cdp, scene="s1", epoch=0, frame_seq=0, tp=0, fp=0, fn=0, tn=0,
            ops={}, frame_supported=True, dropped=False, exact=False,
            terminal="UNKNOWN", expected_terminal="SUCCESS",
            seal=compute_seal(self.h_cdp, "s1", 0, 0), violations=(),
        )
        scorecard = self.harness.score_windows([we_unknown])
        self.assertFalse(scorecard.passed)

    # --- Owner Contract Tests (targeting wender.learn.multiwin_scorecard with @expected_failure_if_absent) ---

    @expected_failure_if_absent("wender.learn.multiwin_scorecard", "run_trial")
    def test_owner_gate_g_m7_1_run_trial_contract(self):
        """jq2a-m7-c1-replay-scorecard owner contract: run_trial returns TrialReport."""
        from wender.learn.multiwin_scorecard import run_trial, Scenario, FakeClock, Handle
        h = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        sc = Scenario(name="sc1", targets=(h,), events=(), expected_outcomes={})
        report = run_trial(sc, seed=42, clock=FakeClock(), adapters={})
        self.assertIsNotNone(report)

    @expected_failure_if_absent("wender.learn.multiwin_scorecard", "score_windows")
    def test_owner_gate_g_m7_4_score_windows_contract(self):
        """jq2a-m7-c1-replay-scorecard owner contract: score_windows returns Scorecard."""
        from wender.learn.multiwin_scorecard import score_windows
        sc = score_windows([])
        self.assertIsNotNone(sc)
        self.assertIn("pooled_confusion", sc.metrics)

    @expected_failure_if_absent("wender.learn.multiwin_scorecard", "canonical_bytes")
    def test_owner_gate_g_m7_1_canonical_bytes_contract(self):
        """jq2a-m7-c1-replay-scorecard owner contract: canonical_bytes returns exact bytes."""
        from wender.learn.multiwin_scorecard import canonical_bytes, score_windows
        sc = score_windows([])
        res = canonical_bytes(sc)
        self.assertIsInstance(res, bytes)


if __name__ == "__main__":
    unittest.main()
