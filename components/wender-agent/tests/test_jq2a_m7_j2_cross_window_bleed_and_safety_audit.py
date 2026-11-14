# @module {"id":"tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""@module tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit - cross-window bleed and safety audit contract tests.

Target APIs to be implemented by build task jq2a-m7-c1-replay-scorecard:
- wender.learn.multiwin_scorecard.run_trial
- wender.learn.multiwin_scorecard.score_windows
- wender.learn.multiwin_scorecard.canonical_bytes
- wender.learn.multiwin_scorecard.Scenario
- wender.learn.multiwin_scorecard.WindowEvidence
- wender.learn.multiwin_scorecard.TrialReport
- wender.learn.multiwin_scorecard.Scorecard
- wender.learn.multiwin_scorecard.FakeClock

Gate G M7 2: Injected cross-target state bleed fails scorecard.
Negative mutant: pooled-only checks hide bleed.
Positive control: isolated reducers pass.
"""
import dataclasses
import hashlib
import importlib.util
import unittest
from typing import Any, Dict, Optional, Tuple
from types import SimpleNamespace

try:
    import numpy
except ModuleNotFoundError:
    numpy = None

from wender.state import schema


# Attempt importing shared record definitions from session_registry if available,
# otherwise define fallback frozen dataclasses with exact proposal field order.
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
except ModuleNotFoundError:

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.Handle
    @dataclasses.dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.TargetRecord
    @dataclasses.dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: Optional[str]
        attachment_owned: bool
        closed: bool

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.FrameRef
    @dataclasses.dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.Observation
    @dataclasses.dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.Receipt
    @dataclasses.dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: Optional[str]

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.ActionRequest
    @dataclasses.dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.ActorLease
    @dataclasses.dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.FocusLease
    @dataclasses.dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.StreamSpec
    @dataclasses.dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.StreamToken
    @dataclasses.dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.Ticket
    @dataclasses.dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.ChangeResult
    @dataclasses.dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: Any
        ops: dict

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.PerceptionResult
    @dataclasses.dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Optional[Observation]
        error: Optional[str]

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.WindowFrames
    @dataclasses.dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.WindowDescription
    @dataclasses.dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: Optional[str]


# Scorecard record fallbacks for harness
try:
    from wender.learn.multiwin_scorecard import (
        FakeClock,
        Scenario,
        Scorecard,
        TrialReport,
        WindowEvidence,
    )
except ModuleNotFoundError:

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.Scenario
    @dataclasses.dataclass(frozen=True)
    class Scenario:
        name: str
        targets: tuple
        events: tuple
        expected_outcomes: dict

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.WindowEvidence
    @dataclasses.dataclass(frozen=True)
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
        exact: Optional[bool]
        terminal: Optional[str]
        expected_terminal: Optional[str]
        seal: str
        violations: tuple

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.TrialReport
    @dataclasses.dataclass(frozen=True)
    class TrialReport:
        rows: tuple
        outcomes: dict
        faults: tuple

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.Scorecard
    @dataclasses.dataclass(frozen=True)
    class Scorecard:
        windows: tuple
        pooled: dict
        metrics: dict
        violations: tuple
        passed: bool

    # @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.FakeClock
    @dataclasses.dataclass
    class FakeClock:
        now_ns: int = 0

        def __call__(self) -> int:
            return self.now_ns

        def advance(self, delta_ns: int) -> int:
            if delta_ns < 0:
                raise ValueError("Negative increments refused")
            self.now_ns += delta_ns
            return self.now_ns


# @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.expected_failure_if_absent
def expected_failure_if_absent(module_name: str, symbol_name: Optional[str] = None):
    """Wrap method with unittest.expectedFailure only if the module or symbol is absent."""
    def decorator(func):
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return unittest.expectedFailure(func)
        try:
            mod = importlib.import_module(module_name)
            if symbol_name and not hasattr(mod, symbol_name):
                return unittest.expectedFailure(func)
            return func
        except ImportError:
            raise
    return decorator


# @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.ContractBleedAuditHarness
class ContractBleedAuditHarness:
    """Local offline contract simulator and evaluator for cross-window state bleed and safety metrics."""

    @staticmethod
    def evaluate_isolated(rows: tuple, events: tuple) -> Scorecard:
        """Isolated per-window reducer evaluation enforcing strict cross-target identity & safety."""
        violations = []
        cross_window_bleed = 0
        safety_violations = 0
        total_tp = 0
        total_fp = 0
        total_fn = 0
        total_tn = 0

        # Map canonical_id -> active epoch and expected handle
        active_handles = {row.handle.canonical_id: row.handle for row in rows}
        active_epochs = {row.handle.canonical_id: row.epoch for row in rows}

        for row in rows:
            total_tp += row.tp
            total_fp += row.fp
            total_fn += row.fn
            total_tn += row.tn
            if row.violations:
                violations.extend(row.violations)
                safety_violations += len(row.violations)

        for evt in events:
            evt_kind = evt.get("kind")
            target_id = evt.get("target_id")
            if target_id not in active_handles:
                continue

            target_handle = active_handles[target_id]
            expected_epoch = active_epochs[target_id]

            # Audit 1: Sibling frame bleed
            if evt_kind == "frame_delivery":
                frame_handle = evt.get("frame_handle")
                if frame_handle and frame_handle.canonical_id != target_id:
                    cross_window_bleed += 1
                    violations.append(
                        f"sibling_frame_bleed: frame for {frame_handle.canonical_id} delivered to {target_id}"
                    )

            # Audit 2: Stale epoch bleed
            if "epoch" in evt and evt["epoch"] != expected_epoch:
                safety_violations += 1
                violations.append(
                    f"stale_epoch_bleed: event epoch {evt['epoch']} != active epoch {expected_epoch} for {target_id}"
                )

            # Audit 3: Focus bypass / unauthorized focus
            if evt_kind == "action_dispatch":
                focus_lease = evt.get("focus_lease")
                if focus_lease is None or focus_lease.lease.handle.canonical_id != target_id:
                    safety_violations += 1
                    violations.append(
                        f"unauthorized_focus_bypass: action dispatch to {target_id} lacks valid focus lease"
                    )

            # Audit 4: Foreign AX state bleed
            if evt_kind == "observation":
                ax_source = evt.get("ax_source_id")
                if ax_source and ax_source != target_id:
                    cross_window_bleed += 1
                    violations.append(
                        f"foreign_ax_state_bleed: observation for {target_id} contains AX state from {ax_source}"
                    )

        pooled = {"tp": total_tp, "fp": total_fp, "fn": total_fn, "tn": total_tn}
        metrics = {
            "cross_window_bleed": cross_window_bleed,
            "safety_violations": safety_violations,
            "terminal_completeness": 1.0 if not violations else 0.0,
        }

        passed = (cross_window_bleed == 0) and (safety_violations == 0) and (len(violations) == 0)
        return Scorecard(
            windows=rows,
            pooled=pooled,
            metrics=metrics,
            violations=tuple(violations),
            passed=passed,
        )

    @staticmethod
    def evaluate_pooled_only(rows: tuple, events: tuple) -> Scorecard:
        """Negative mutant evaluator: checks ONLY pooled aggregate counts, hiding cross-target state bleed."""
        total_tp = sum(r.tp for r in rows)
        total_fp = sum(r.fp for r in rows)
        total_fn = sum(r.fn for r in rows)
        total_tn = sum(r.tn for r in rows)
        pooled = {"tp": total_tp, "fp": total_fp, "fn": total_fn, "tn": total_tn}
        # Pooled-only check completely ignores per-window handle isolation, stale epoch, focus lease, and AX state!
        return Scorecard(
            windows=rows,
            pooled=pooled,
            metrics={"cross_window_bleed": None, "safety_violations": None},
            violations=(),
            passed=True,  # Hides state bleed!
        )



def _sealed_scorecard_rows(rows):
    """Real score_windows fixture: one manifest and one whole-trial SHA-256 seal."""
    from wender.learn import multiwin_scorecard as score

    targets = [list(score.handle_key(row.handle)) for row in rows]
    manifest = {
        "version": "multiwin-scorecard-v1",
        "scenario": "offline-audit",
        "seed": 37,
        "targets": targets,
        "identities": [list(score._identity(row)) for row in rows],
        "actions": {
            "audit-" + str(i): {"handle": targets[i], "expected": "SUCCESS", "cost": 1}
            for i in range(len(rows))
        },
        "gates": {
            gate: {"status": "PASS", "evidence": "offline-contract"}
            for gate in score.PREREQUISITE_GATES
        },
        "replay_match": True,
        "replay_sha256": "a" * 64,
        "repeat_sha256": "a" * 64,
        "isolated_support": len(rows),
        "elapsed_ns": 10,
        "recoveries": [],
    }
    bound = tuple(dataclasses.replace(
        row, seal="",
        ops={
            "counts": {"audit": 1}, "expected_counts": {"audit": 1},
            "isolated_match": True, "pixel_mismatches": 0,
            "drop_expected": False, "torn": False,
            "action_id": "audit-" + str(i), "admitted": True,
            "scheduler_attempted": True,
            "dispatch": {
                "eligible": True,
                "telemetry": {
                    "wait_opportunities": 0, "queue_depth": 1,
                    "fairness_cost": 1, "quantum": 1, "urgent_cap": 1,
                },
            },
            "_trial": manifest,
        }
    ) for i, row in enumerate(rows))
    return _reseal_scorecard_rows(bound)


def _reseal_scorecard_rows(rows):
    """Rebind intentionally changed evidence to the original shared manifest."""
    from wender.learn import multiwin_scorecard as score

    manifest = rows[0].ops["_trial"]
    digest = score._seal(rows, manifest)
    return tuple(dataclasses.replace(row, seal=digest) for row in rows)


class _OfflineOwnerAdapter:
    """Typed, offline collaborators; production code owns replay and scoring."""
    offline = True

    def __init__(self, handle):
        from wender.learn import multiwin_scorecard as score
        self.score = score
        self.handle = handle
        self.resource_key = "/".join(("offline", handle.kind, handle.canonical_id))
        self.focus_domain = None
        self.gates = {
            gate: {"status": "PASS", "evidence": "offline-fixture:" + gate,
                   "expected_failure": False}
            for gate in score.PREREQUISITE_GATES
        }
        self.registry = SimpleNamespace(resolve=self.resolve)
        self.stream = SimpleNamespace(deliver=lambda frame: frame)
        self.perception = SimpleNamespace(observe=self.observe, reduce=self.reduce)
        self.scheduler = SimpleNamespace(admit=self.admit)
        self.actor = SimpleNamespace(execute=self.execute)

    def reset(self, *, seed, clock):
        self.clock = clock

    def resolve(self, handle):
        return SimpleNamespace(handle=self.handle, epoch=0, closed=False)

    def observe(self, frame):
        return self.score.Observation(
            self.handle, frame.epoch, frame.frame_seq, 1000000,
            {"target": [self.handle.kind, self.handle.canonical_id, self.handle.generation]},
            frame,
        )

    def reduce(self, observation):
        claims = observation.frame.rgb[:, :, 0] > 0
        return self.score.ChangeResult(
            self.handle, observation.epoch, True, claims,
            {"pixels": int(claims.size), "reductions": 1},
        )

    def admit(self, action, observation, change, now_ns):
        telemetry = {
            "resource_key": self.resource_key, "active_resource": 1,
            "focus_authorized": True, "handle_fresh": True, "fence_clear": True,
            "guard_passed": True, "geometry_matches": True,
            "global_committed": 0, "global_reserved": action.cost,
            "global_cap": 10000, "target_committed": 0,
            "target_reserved": action.cost, "target_cap": 10000,
            "wait_opportunities": 0, "queue_depth": 1,
            "fairness_cost": 1, "quantum": 1, "urgent_cap": 1,
        }
        return {"admitted": True, "eligible": True, "reason": None,
                "telemetry": telemetry}

    def execute(self, action, observation, now_ns):
        return self.score.Receipt(action.action_id, self.handle, "ADMITTED", None)

    def terminal(self, action, receipt, now_ns):
        return self.score.Receipt(action.action_id, self.handle, "SUCCESS", None)


# @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.TestSharedRecordSchemasAndContracts
class TestSharedRecordSchemasAndContracts(unittest.TestCase):
    """Contract tests for shared dataclasses and records specified in the proposal."""

    def test_record_schemas_and_field_order(self):
        h1 = Handle(kind="cdp_tab", canonical_id="tab_001", generation=1)
        self.assertEqual(h1.kind, "cdp_tab")
        self.assertEqual(h1.canonical_id, "tab_001")
        self.assertEqual(h1.generation, 1)

        tr = TargetRecord(
            handle=h1,
            transport=object(),
            owned=True,
            epoch=1,
            session_id="sess_1",
            attachment_owned=True,
            closed=False,
        )
        self.assertTrue(tr.owned)

        rgb = numpy.zeros((10, 10, 3), dtype=numpy.uint8)
        fr = FrameRef.from_rgb(h1, 1, 10, rgb)
        self.assertEqual(fr.frame_seq, 10)

        obs = Observation(
            handle=h1,
            epoch=1,
            frame_seq=10,
            dpr_ppm=100,
            state={"ax_sha256": "0" * 64},
            frame=fr,
        )
        self.assertEqual(obs.dpr_ppm, 100)

        rcpt = Receipt(action_id="act_1", handle=h1, status="executed", reason=None)
        self.assertEqual(rcpt.status, "executed")

        req = ActionRequest(
            handle=h1,
            action_id="act_1",
            epoch=1,
            payload={"type": "click"},
            cost=1,
            deadline_ns=1000,
        )
        self.assertEqual(req.cost, 1)

        lease = ActorLease(
            handle=h1,
            owner="jules",
            generation=1,
            nonce=b"nonce_12345678",
            expires_ns=5000,
        )
        flease = FocusLease(lease=lease, domain="domain_a", epoch=1)
        self.assertEqual(flease.domain, "domain_a")

        spec = StreamSpec(width=100, height=100, slots=4, quota_bytes=120000)
        token = StreamToken(handle=h1, epoch=1)
        tkt = Ticket(action_id="act_1", handle=h1)

        claims = numpy.array((True, False, True), dtype=numpy.bool_)
        chg = ChangeResult(handle=h1, epoch=1, settled=True, claims=claims, ops={"click": 1})
        self.assertTrue(chg.settled)

        pres = PerceptionResult(handle=h1, observation=obs, error=None)
        self.assertIsNotNone(pres.observation)

        wf = WindowFrames(handle=h1, epoch=1, frames=(fr,), frame_seqs=(10,), dpr_ppm=100)
        self.assertEqual(len(wf.frames), len(wf.frame_seqs))

        wd = WindowDescription(
            handle=h1,
            epoch=1,
            frame_seqs=(10,),
            descriptors=({"type": "button"},),
            regions=([0, 0, 10, 10],),
            error=None,
        )
        self.assertEqual(wd.descriptors[0]["type"], "button")


# @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.TestFakeClockFixture
class TestFakeClockFixture(unittest.TestCase):
    """Contract tests for mutable FakeClock fixture."""

    def test_fake_clock_advance_and_refuse_negative(self):
        clock = FakeClock(now_ns=100)
        self.assertEqual(clock(), 100)
        self.assertEqual(clock.advance(50), 150)
        self.assertEqual(clock(), 150)

        with self.assertRaises(ValueError):
            clock.advance(-10)


# @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.TestGateGM72CrossWindowBleedAndSafetyAudit
class TestGateGM72CrossWindowBleedAndSafetyAudit(unittest.TestCase):
    """Contract tests for Gate G M7 2: Injected cross-target state bleed fails scorecard."""

    def setUp(self):
        # 5 distinct target kinds
        self.target_kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]
        self.handles = {
            kind: Handle(kind=kind, canonical_id=f"target_{kind}_01", generation=1)
            for kind in self.target_kinds
        }
        self.clean_rows = tuple(
            WindowEvidence(
                handle=self.handles[kind],
                scene=f"scene_{kind}",
                epoch=1,
                frame_seq=1,
                tp=10,
                fp=0,
                fn=0,
                tn=10,
                ops={"action": 1},
                frame_supported=True,
                dropped=False,
                exact=True,
                terminal="SUCCESS",
                expected_terminal="SUCCESS",
                seal="seal_valid",
                violations=(),
            )
            for kind in self.target_kinds
        )

    def test_positive_control_isolated_reducers_pass_local_contract(self):
        """Positive control: clean multi-window traces with isolated reducers pass scorecard."""
        clean_events = []
        for kind in self.target_kinds:
            h = self.handles[kind]
            lease = ActorLease(handle=h, owner="actor", generation=1, nonce=b"nonce123", expires_ns=1000)
            flease = FocusLease(lease=lease, domain=f"domain_{kind}", epoch=1)
            clean_events.extend([
                {
                    "kind": "frame_delivery",
                    "target_id": h.canonical_id,
                    "frame_handle": h,
                    "epoch": 1,
                },
                {
                    "kind": "action_dispatch",
                    "target_id": h.canonical_id,
                    "epoch": 1,
                    "focus_lease": flease,
                },
                {
                    "kind": "observation",
                    "target_id": h.canonical_id,
                    "epoch": 1,
                    "ax_source_id": h.canonical_id,
                },
            ])

        card = ContractBleedAuditHarness.evaluate_isolated(self.clean_rows, clean_events)
        self.assertTrue(card.passed)
        self.assertEqual(card.metrics["cross_window_bleed"], 0)
        self.assertEqual(card.metrics["safety_violations"], 0)
        self.assertEqual(card.violations, ())

    def test_injected_sibling_frame_bleed_fails_local_contract(self):
        """Injected sibling frame (target_A frame in target_B) fails scorecard."""
        h_a = self.handles["cdp_tab"]
        h_b = self.handles["godot_window"]

        bleed_events = [
            {
                "kind": "frame_delivery",
                "target_id": h_b.canonical_id,
                "frame_handle": h_a,  # Sibling frame injected!
                "epoch": 1,
            }
        ]

        card = ContractBleedAuditHarness.evaluate_isolated(self.clean_rows, bleed_events)
        self.assertFalse(card.passed)
        self.assertGreater(card.metrics["cross_window_bleed"], 0)
        self.assertTrue(any("sibling_frame_bleed" in v for v in card.violations))

    def test_injected_stale_epoch_bleed_fails_local_contract(self):
        """Injected stale epoch event fails scorecard."""
        h_b = self.handles["x11_window"]

        stale_events = [
            {
                "kind": "frame_delivery",
                "target_id": h_b.canonical_id,
                "frame_handle": h_b,
                "epoch": 0,  # Stale epoch (active is 1)
            }
        ]

        card = ContractBleedAuditHarness.evaluate_isolated(self.clean_rows, stale_events)
        self.assertFalse(card.passed)
        self.assertGreater(card.metrics["safety_violations"], 0)
        self.assertTrue(any("stale_epoch_bleed" in v for v in card.violations))

    def test_injected_focus_bypass_fails_local_contract(self):
        """Injected action dispatch without valid focus lease fails scorecard."""
        h_b = self.handles["xvfb_screen"]

        bypass_events = [
            {
                "kind": "action_dispatch",
                "target_id": h_b.canonical_id,
                "epoch": 1,
                "focus_lease": None,  # Focus bypass!
            }
        ]

        card = ContractBleedAuditHarness.evaluate_isolated(self.clean_rows, bypass_events)
        self.assertFalse(card.passed)
        self.assertGreater(card.metrics["safety_violations"], 0)
        self.assertTrue(any("unauthorized_focus_bypass" in v for v in card.violations))

    def test_injected_foreign_ax_state_fails_local_contract(self):
        """Injected foreign AX state in target observation fails scorecard."""
        h_a = self.handles["capsule"]
        h_b = self.handles["cdp_tab"]

        foreign_ax_events = [
            {
                "kind": "observation",
                "target_id": h_b.canonical_id,
                "epoch": 1,
                "ax_source_id": h_a.canonical_id,  # Foreign AX source!
            }
        ]

        card = ContractBleedAuditHarness.evaluate_isolated(self.clean_rows, foreign_ax_events)
        self.assertFalse(card.passed)
        self.assertGreater(card.metrics["cross_window_bleed"], 0)
        self.assertTrue(any("foreign_ax_state_bleed" in v for v in card.violations))

    def test_negative_mutant_pooled_only_hides_bleed_local_contract(self):
        """Demonstrate that negative mutant (pooled-only check) hides state bleed while isolated check fails."""
        h_a = self.handles["cdp_tab"]
        h_b = self.handles["godot_window"]

        bleed_events = [
            {
                "kind": "frame_delivery",
                "target_id": h_b.canonical_id,
                "frame_handle": h_a,
                "epoch": 1,
            }
        ]

        # Pooled-only check hides bleed and passes
        pooled_card = ContractBleedAuditHarness.evaluate_pooled_only(self.clean_rows, bleed_events)
        self.assertTrue(pooled_card.passed)

        # Isolated reducer check correctly catches bleed and fails
        isolated_card = ContractBleedAuditHarness.evaluate_isolated(self.clean_rows, bleed_events)
        self.assertFalse(isolated_card.passed)

    @expected_failure_if_absent("wender.learn.multiwin_scorecard", "score_windows")
    def test_gate_g_m7_2_score_windows_positive_control(self):
        """Owner API test: score_windows on clean multi-window evidence passes."""
        from wender.learn.multiwin_scorecard import score_windows

        rows = _sealed_scorecard_rows(self.clean_rows)
        card = score_windows(rows)
        self.assertTrue(card.passed)
        self.assertEqual(card.metrics.get("cross_window_bleed")["numerator"], 0)
        self.assertEqual(card.metrics.get("safety_violations")["numerator"], 0)

    @expected_failure_if_absent("wender.learn.multiwin_scorecard", "score_windows")
    def test_gate_g_m7_2_score_windows_injected_bleed_fails(self):
        """Owner API test: score_windows with injected state bleed evidence fails."""
        from wender.learn.multiwin_scorecard import score_windows

        signed = _sealed_scorecard_rows(self.clean_rows)
        corrupt_row = dataclasses.replace(
            signed[0], exact=False, violations=("injected_cross_window_bleed",)
        )
        rows = _reseal_scorecard_rows((corrupt_row,) + signed[1:])
        card = score_windows(rows)
        self.assertFalse(card.passed)
        self.assertIn("injected_cross_window_bleed", card.violations)
        self.assertFalse(card.metrics["cross_window_bleed"]["passed"])


# @anchor:tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.TestMultiwinScorecardOwnerAPIs
class TestMultiwinScorecardOwnerAPIs(unittest.TestCase):
    """Contract tests for owner APIs: run_trial, score_windows, and canonical_bytes."""

    @expected_failure_if_absent("wender.learn.multiwin_scorecard", "run_trial")
    def test_run_trial_heterogeneous_targets_contract(self):
        """run_trial executes a multi-target scenario and returns TrialReport."""
        from wender.learn.multiwin_scorecard import FakeClock, Scenario, run_trial

        clock = FakeClock(now_ns=0)
        h1 = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        h2 = Handle(kind="godot_window", canonical_id="godot_1", generation=1)

        from wender.learn.multiwin_scorecard import ActionRequest, score_windows

        events = []
        for seq, (handle, aid) in enumerate(((h1, "tab_1"), (h2, "godot_1"))):
            rgb = numpy.zeros((2, 2, 3), dtype=numpy.uint8)
            rgb[:, :, 0] = 1
            events.append({
                "handle": handle, "scene": "offline", "epoch": 0,
                "frame_seq": 0, "at_ns": 10 * (seq + 1),
                "rgb": rgb, "truth": rgb[:, :, 0] > 0,
                "action": ActionRequest(handle, aid, 0, {}, 1, 1000000),
            })
        scenario = Scenario(
            name="heterogeneous_trial",
            targets=(h1, h2),
            events=tuple(events),
            expected_outcomes={"tab_1": "SUCCESS", "godot_1": "SUCCESS"},
        )

        adapters = {
            (h.kind, h.canonical_id, h.generation): _OfflineOwnerAdapter(h)
            for h in (h1, h2)
        }
        report = run_trial(scenario, seed=42, clock=clock, adapters=adapters)
        self.assertTrue(score_windows(report.rows).passed)
        self.assertIsNotNone(report)
        self.assertTrue(hasattr(report, "rows"))

    @expected_failure_if_absent("wender.learn.multiwin_scorecard", "canonical_bytes")
    def test_canonical_bytes_determinism_contract(self):
        """canonical_bytes generates deterministic bytes excluding wall-clock timing."""
        from wender.learn.multiwin_scorecard import Scorecard, canonical_bytes

        h1 = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        row = WindowEvidence(
            handle=h1,
            scene="s1",
            epoch=1,
            frame_seq=1,
            tp=1,
            fp=0,
            fn=0,
            tn=1,
            ops={},
            frame_supported=True,
            dropped=False,
            exact=True,
            terminal="SUCCESS",
            expected_terminal="SUCCESS",
            seal="seal1",
            violations=(),
        )
        card = Scorecard(
            windows=(row,),
            pooled={"tp": 1, "fp": 0, "fn": 0, "tn": 1},
            metrics={"cross_window_bleed": 0},
            violations=(),
            passed=True,
        )

        b1 = canonical_bytes(card)
        b2 = canonical_bytes(card)
        self.assertIsInstance(b1, bytes)
        self.assertEqual(b1, b2)


if __name__ == "__main__":
    unittest.main()
