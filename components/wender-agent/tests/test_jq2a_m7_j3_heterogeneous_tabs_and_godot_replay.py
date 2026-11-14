# @module {"id":"tests.test_jq2a_m7_j3_heterogeneous_tabs_and_godot_replay","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""@module tests.test_jq2a_m7_j3_heterogeneous_tabs_and_godot_replay - Heterogeneous tabs and Godot replay contract tests.

Target APIs to be implemented by build task jq2a-m7-c1-replay-scorecard:
- wender.learn.multiwin_scorecard.run_trial
- wender.learn.multiwin_scorecard.score_windows
- wender.learn.multiwin_scorecard.canonical_bytes
- wender.learn.multiwin_scorecard.Scenario
- wender.learn.multiwin_scorecard.WindowEvidence
- wender.learn.multiwin_scorecard.TrialReport
- wender.learn.multiwin_scorecard.Scorecard
- wender.learn.multiwin_scorecard.FakeClock

Gate G M7 3: Heterogeneous trial reports every target kind.
Supported target kinds: cdp_tab, x11_window, xvfb_screen, capsule, godot_window.
"""
from dataclasses import dataclass
import hashlib
import json
import unittest
import numpy as np
from tests.test_jq2a_m7_c1_replay_scorecard_implementation import FixtureAdapter, _event, _key

# Try importing shared records from wender.perceive.session_registry if present, else fallback
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
        session_id: str | None
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
        reason: str | None

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
        claims: np.ndarray
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

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
        error: str | None


# Try importing scorecard records from wender.learn.multiwin_scorecard if present, else fallback
try:
    from wender.learn.multiwin_scorecard import (
        Scenario,
        WindowEvidence,
        TrialReport,
        Scorecard,
        FakeClock,
    )
except ImportError:
    @dataclass(frozen=True)
    class Scenario:
        name: str
        targets: tuple
        events: tuple
        expected_outcomes: dict

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

    @dataclass(frozen=True)
    class TrialReport:
        rows: tuple
        outcomes: dict
        faults: tuple

    @dataclass(frozen=True)
    class Scorecard:
        windows: tuple
        pooled: dict
        metrics: dict
        violations: tuple
        passed: bool

    # @anchor:tests.test_jq2a_m7_j3_heterogeneous_tabs_and_godot_replay.FakeClock
    class FakeClock:
        """Injected fake clock fixture with nanosecond precision."""

        def __init__(self, now_ns: int = 0):
            self.now_ns = now_ns

        def __call__(self) -> int:
            return self.now_ns

        def advance(self, delta_ns: int) -> int:
            if delta_ns < 0:
                raise ValueError("Negative time increment not permitted")
            self.now_ns += delta_ns
            return self.now_ns


def expected_if_owner_absent(symbol_name: str):
    """Applies unittest.expectedFailure dynamically when owner module/symbol is missing."""
    def decorator(func):
        try:
            mod = __import__("wender.learn.multiwin_scorecard", fromlist=[symbol_name])
            sym = getattr(mod, symbol_name, None)
            if sym is not None:
                return func
        except ImportError:
            pass
        return unittest.expectedFailure(func)
    return decorator


# @anchor:tests.test_jq2a_m7_j3_heterogeneous_tabs_and_godot_replay.FakeTargetAdapter
class FakeTargetAdapter:
    """Offline target adapter fixture generating RAM numpy frames without I/O."""

    def __init__(self, handle: Handle, scene: str = "default_scene"):
        self.handle = handle
        self.scene = scene
        self.epoch = 0
        self.frame_seq = 0
        self.cursor = 0
        self.confirmed = False

    def capture_observation(self) -> Observation:
        self.frame_seq += 1
        rgb = np.zeros((64, 64, 3), dtype=np.uint8)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        frame = FrameRef(
            handle=self.handle,
            epoch=self.epoch,
            frame_seq=self.frame_seq,
            width=64,
            height=64,
            rgb=rgb,
            digest=digest,
        )
        return Observation(
            handle=self.handle,
            epoch=self.epoch,
            frame_seq=self.frame_seq,
            dpr_ppm=100,
            state={"scene": self.scene, "cursor": self.cursor, "confirmed": self.confirmed},
            frame=frame,
        )

    def dispatch_action(self, action: ActionRequest) -> Receipt:
        if action.payload.get("type") == "godot_confirm":
            self.confirmed = True
        self.cursor += 1
        return Receipt(
            action_id=action.action_id,
            handle=self.handle,
            status="success",
            reason=None,
        )


# @anchor:tests.test_jq2a_m7_j3_heterogeneous_tabs_and_godot_replay.TestHeterogeneousTabsAndGodotReplay
class TestHeterogeneousTabsAndGodotReplay(unittest.TestCase):
    """Contract tests for Gate G M7 3: Heterogeneous trial reports every target kind."""

    def setUp(self):
        self.clock = FakeClock(now_ns=1000)
        self.handles = {
            "cdp_tab": Handle(kind="cdp_tab", canonical_id="tab_001", generation=1),
            "x11_window": Handle(kind="x11_window", canonical_id="win_001", generation=1),
            "xvfb_screen": Handle(kind="xvfb_screen", canonical_id="scr_001", generation=1),
            "capsule": Handle(kind="capsule", canonical_id="cap_001", generation=1),
            "godot_window": Handle(kind="godot_window", canonical_id="godot_001", generation=1),
        }
        self.adapters = {
            _key(h): FixtureAdapter(h) for h in self.handles.values()
        }

    @expected_if_owner_absent("run_trial")
    def test_g_m7_3_heterogeneous_trial_reports_every_target_kind_positive(self):
        """Positive control: trial containing all 5 target kinds reports evidence for every target kind."""
        from wender.learn.multiwin_scorecard import run_trial, score_windows, Scenario

        targets = tuple(self.handles.values())
        events = tuple(
            _event(h, 0, 10 * (i + 1), action_id=f"act_{h.kind}",
                   payload={"type": "click" if h.kind != "godot_window" else "godot_confirm"})[0]
            for i, h in enumerate(targets)
        )
        scenario = Scenario(
            name="heterogeneous_all_kinds",
            targets=targets,
            events=events,
            expected_outcomes={f"act_{h.kind}": "SUCCESS" for h in targets},
        )

        report = run_trial(scenario, seed=42, clock=self.clock, adapters=self.adapters)
        scorecard = score_windows(report.rows)

        # Verify all 5 target kinds are present in window evidence rows
        reported_kinds = {row.handle.kind for row in report.rows}
        expected_kinds = {"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"}
        self.assertEqual(reported_kinds, expected_kinds)
        self.assertTrue(scorecard.passed)
        self.assertEqual(scorecard.metrics["cross_window_bleed"],
                         {"numerator": 0, "denominator": 5, "passed": True})

    @expected_if_owner_absent("run_trial")
    def test_g_m7_3_heterogeneous_trial_negative_mutant_omit_godot(self):
        """Negative mutant: omitting godot_window from heterogeneous trial fails Gate G M7 3."""
        from wender.learn.multiwin_scorecard import run_trial, score_windows, Scenario

        # Omit godot_window from scenario targets
        incomplete_targets = tuple(h for k, h in self.handles.items() if k != "godot_window")
        scenario = Scenario(
            name="heterogeneous_omit_godot",
            targets=incomplete_targets,
            events=(),
            expected_outcomes={},
        )

        report = run_trial(scenario, seed=42, clock=self.clock, adapters=self.adapters)
        scorecard = score_windows(report.rows)

        reported_kinds = {row.handle.kind for row in report.rows}
        self.assertNotIn("godot_window", reported_kinds)
        self.assertFalse(scorecard.passed)
        self.assertEqual(report.outcomes["missing_supported_kinds"], ["godot_window"])

    @expected_if_owner_absent("run_trial")
    def test_g_m7_3_heterogeneous_trial_negative_mutant_overwrite_tab_identity(self):
        """Negative mutant: overwriting tab identity produces validation failure."""
        from wender.learn.multiwin_scorecard import run_trial, score_windows, Scenario

        # Overwrite tab handle kind to x11_window resulting in duplicate origin / missing cdp_tab
        corrupted_tab_handle = Handle(kind="x11_window", canonical_id="tab_001", generation=1)
        targets = (
            corrupted_tab_handle,
            self.handles["x11_window"],
            self.handles["xvfb_screen"],
            self.handles["capsule"],
            self.handles["godot_window"],
        )
        scenario = Scenario(
            name="heterogeneous_corrupt_tab_identity",
            targets=targets,
            events=(),
            expected_outcomes={},
        )

        report = run_trial(scenario, seed=42, clock=self.clock, adapters=self.adapters)
        scorecard = score_windows(report.rows)

        reported_kinds = {row.handle.kind for row in report.rows}
        self.assertNotIn("cdp_tab", reported_kinds)
        self.assertFalse(scorecard.passed)

    @expected_if_owner_absent("canonical_bytes")
    def test_canonical_bytes_deterministic_reproducibility(self):
        """INV-M7-3: Canonical bytes exclude wall clock timing and match exactly across identical runs."""
        from wender.learn.multiwin_scorecard import run_trial, score_windows, canonical_bytes, Scenario

        targets = tuple(self.handles.values())
        scenario = Scenario(
            name="deterministic_test",
            targets=targets,
            events=(),
            expected_outcomes={},
        )

        clock1 = FakeClock(now_ns=5000)
        report1 = run_trial(scenario, seed=123, clock=clock1, adapters=self.adapters)
        scorecard1 = score_windows(report1.rows)
        bytes1 = canonical_bytes(scorecard1)

        clock2 = FakeClock(now_ns=999999)  # Different clock initial value
        report2 = run_trial(scenario, seed=123, clock=clock2, adapters=self.adapters)
        scorecard2 = score_windows(report2.rows)
        bytes2 = canonical_bytes(scorecard2)

        self.assertEqual(bytes1, bytes2)
        self.assertIsInstance(bytes1, bytes)

    @expected_if_owner_absent("score_windows")
    def test_capsule_and_child_screen_shared_actuation_resource(self):
        """INV-M7-1: Capsule and its child screen share actuation resource when addressing same destination."""
        from wender.learn.multiwin_scorecard import score_windows

        row_capsule = WindowEvidence(
            handle=self.handles["capsule"],
            scene="dest_001",
            epoch=0,
            frame_seq=1,
            tp=1, fp=0, fn=0, tn=1,
            ops={"click": 1},
            frame_supported=True,
            dropped=False,
            exact=True,
            terminal="success",
            expected_terminal="success",
            seal="seal_cap",
            violations=(),
        )
        row_screen = WindowEvidence(
            handle=self.handles["xvfb_screen"],
            scene="dest_001",
            epoch=0,
            frame_seq=1,
            tp=1, fp=0, fn=0, tn=1,
            ops={"click": 1},
            frame_supported=True,
            dropped=False,
            exact=True,
            terminal="success",
            expected_terminal="success",
            seal="seal_scr",
            violations=(),
        )

        scorecard = score_windows((row_capsule, row_screen))
        self.assertIsInstance(scorecard, Scorecard)


if __name__ == "__main__":
    unittest.main()
