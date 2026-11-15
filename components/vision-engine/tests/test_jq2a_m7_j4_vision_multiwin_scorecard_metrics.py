"""Contract and conformance test suite for vision multi-window scorecard metrics.

Gate G M7 4: Pooled confusion equals sum and rejects missing seals.
Owning implementation task_id: jq2a-m7-g1-vision-scorecard
Owner module: eval.v2eval.multiwin_scorecard
"""
import importlib.util
import unittest
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple
import numpy as np

OWNER_MODULE = "eval.v2eval.multiwin_scorecard"


def probe_owner_symbol(symbol_name: str = "score_windows"):
    """Probe the owner module and symbol explicitly.

    Applies individually scoped unittest.expectedFailure only while that exact owner
    module or symbol is absent; removes the wrapper automatically when it exists.
    """
    def decorator(func):
        spec = importlib.util.find_spec(OWNER_MODULE)
        if spec is None:
            return unittest.expectedFailure(func)
        try:
            mod = importlib.import_module(OWNER_MODULE)
            if not hasattr(mod, symbol_name):
                return unittest.expectedFailure(func)
        except ImportError:
            return unittest.expectedFailure(func)
        return func
    return decorator


# Local definitions of proposed records matching exact required field order
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
    claims: np.ndarray
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


# Proposed Scorecard records
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
    exact: Optional[bool]
    terminal: Optional[str]
    expected_terminal: Optional[str]
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


@dataclass
class FakeClock:
    now_ns: int = 0

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, delta_ns: int) -> int:
        if delta_ns < 0:
            raise ValueError("Negative time increment refused")
        self.now_ns += delta_ns
        return self.now_ns


# Contract harness reference functions for Gate G M7 4 and mutant validation
def contract_pool_confusion_and_validate(rows: Sequence[WindowEvidence]) -> Tuple[Dict[str, int], bool, Tuple[str, ...]]:
    """Contract reference predicate for Gate G M7 4."""
    if not rows:
        return {"tp": 0, "fp": 0, "fn": 0, "tn": 0}, False, ("EMPTY_ROWS",)

    violations = []
    # Reject missing or mixed seals
    seals = {r.seal for r in rows}
    if any(s == "" or s is None for s in seals):
        violations.append("MISSING_SEAL")
    if len(seals) > 1:
        violations.append("MIXED_SEAL")

    # Reject duplicate or foreign identities
    seen_ids = set()
    for r in rows:
        ident = (r.handle.canonical_id, r.scene, r.epoch, r.frame_seq)
        if ident in seen_ids:
            violations.append("DUPLICATE_IDENTITY")
        seen_ids.add(ident)
        if not r.handle.kind or not r.handle.canonical_id:
            violations.append("FOREIGN_IDENTITY")

    # Reject absent frame support
    for r in rows:
        if not r.frame_supported:
            violations.append("ABSENT_FRAME_SUPPORT")

    # Elementwise sum pooling of confusion integer counts
    pooled_tp = sum(r.tp for r in rows)
    pooled_fp = sum(r.fp for r in rows)
    pooled_fn = sum(r.fn for r in rows)
    pooled_tn = sum(r.tn for r in rows)

    pooled = {"tp": pooled_tp, "fp": pooled_fp, "fn": pooled_fn, "tn": pooled_tn}
    passed = len(violations) == 0
    return pooled, passed, tuple(violations)


def mutant_average_ratios_confusion(rows: Sequence[WindowEvidence]) -> Tuple[Dict[str, float], bool, Tuple[str, ...]]:
    """Negative mutant: averages ratios instead of elementwise integer sum."""
    pooled, passed, violations = contract_pool_confusion_and_validate(rows)
    n = len(rows) if rows else 1
    return {
        "tp": pooled["tp"] / n,
        "fp": pooled["fp"] / n,
        "fn": pooled["fn"] / n,
        "tn": pooled["tn"] / n,
    }, passed, violations


def mutant_replace_null_with_zero(metrics: dict) -> dict:
    """Negative mutant: replaces missing denominator/null with 0."""
    mutated = {}
    for k, v in metrics.items():
        if v is None:
            mutated[k] = 0
        else:
            mutated[k] = v
    return mutated


class TestProposedRecordTypes(unittest.TestCase):
    """Verify exact field ordering and immutability of proposed canonical dataclasses."""

    def test_handle_record_fields(self):
        h = Handle(kind="cdp_tab", canonical_id="tab-123", generation=1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab-123")
        self.assertEqual(h.generation, 1)
        with self.assertRaises(Exception):
            h.generation = 2  # Frozen

    def test_fake_clock_contract(self):
        clock = FakeClock(now_ns=1000)
        self.assertEqual(clock(), 1000)
        self.assertEqual(clock.advance(500), 1500)
        self.assertEqual(clock(), 1500)
        with self.assertRaises(ValueError):
            clock.advance(-100)

    def test_window_evidence_fields(self):
        h = Handle(kind="x11_window", canonical_id="win-1", generation=1)
        ev = WindowEvidence(
            handle=h,
            scene="scene_a",
            epoch=0,
            frame_seq=10,
            tp=12,
            fp=1,
            fn=0,
            tn=100,
            ops={"click": 2},
            frame_supported=True,
            dropped=False,
            exact=True,
            terminal="COMPLETE",
            expected_terminal="COMPLETE",
            seal="seal_v1",
            violations=(),
        )
        self.assertEqual(ev.handle, h)
        self.assertEqual(ev.seal, "seal_v1")
        self.assertEqual(ev.tp, 12)

    def test_scorecard_and_trial_report_fields(self):
        h = Handle(kind="capsule", canonical_id="cap-1", generation=0)
        ev = WindowEvidence(
            handle=h,
            scene="s1",
            epoch=0,
            frame_seq=1,
            tp=5,
            fp=0,
            fn=0,
            tn=20,
            ops={},
            frame_supported=True,
            dropped=False,
            exact=True,
            terminal="OK",
            expected_terminal="OK",
            seal="s",
            violations=(),
        )
        sc = Scorecard(
            windows=(ev,),
            pooled={"tp": 5, "fp": 0, "fn": 0, "tn": 20},
            metrics={"pooled_confusion": {"tp": 5, "fp": 0, "fn": 0, "tn": 20}},
            violations=(),
            passed=True,
        )
        self.assertTrue(sc.passed)
        self.assertEqual(sc.pooled["tp"], 5)

    def test_all_canonical_dataclasses_instantiation(self):
        h = Handle("xvfb_screen", "scr-0", 1)
        tr = TargetRecord(h, object(), True, 1, "sess-1", True, False)
        fr = FrameRef(h, 1, 1, 640, 480, np.zeros((480, 640, 3), dtype=np.uint8), "dig1")
        obs = Observation(h, 1, 1, 100, {}, fr)
        rc = Receipt("act-1", h, "OK", None)
        ar = ActionRequest(h, "act-1", 1, {}, 1, 10000)
        al = ActorLease(h, "owner1", 1, b"nonce", 20000)
        fl = FocusLease(al, "domain1", 1)
        ss = StreamSpec(640, 480, 4, 1024)
        st = StreamToken(h, 1)
        tk = Ticket("act-1", h)
        cr = ChangeResult(h, 1, True, np.zeros(4), {})
        pr = PerceptionResult(h, obs, None)
        wf = WindowFrames(h, 1, (), (), 100)
        wd = WindowDescription(h, 1, (), (), (), None)

        self.assertEqual(tr.handle, h)
        self.assertEqual(fr.width, 640)
        self.assertEqual(obs.dpr_ppm, 100)
        self.assertEqual(rc.status, "OK")
        self.assertEqual(ar.cost, 1)
        self.assertEqual(al.nonce, b"nonce")
        self.assertEqual(fl.domain, "domain1")
        self.assertEqual(ss.slots, 4)
        self.assertEqual(st.epoch, 1)
        self.assertEqual(tk.action_id, "act-1")
        self.assertTrue(cr.settled)
        self.assertIsNone(pr.error)
        self.assertEqual(wf.epoch, 1)
        self.assertIsNone(wd.error)


class TestContractHarness(unittest.TestCase):
    """Contract harness for Gate G M7 4 testing positive controls and negative mutants."""

    def test_positive_control_same_seal_known_count_fixture(self):
        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        h2 = Handle(kind="x11_window", canonical_id="win-2", generation=1)

        w1 = WindowEvidence(
            handle=h1, scene="scene_1", epoch=0, frame_seq=1,
            tp=10, fp=2, fn=1, tn=100, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_2026_valid", violations=(),
        )
        w2 = WindowEvidence(
            handle=h2, scene="scene_2", epoch=0, frame_seq=1,
            tp=25, fp=5, fn=3, tn=250, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_2026_valid", violations=(),
        )

        pooled, passed, violations = contract_pool_confusion_and_validate((w1, w2))
        self.assertTrue(passed)
        self.assertEqual(violations, ())
        self.assertEqual(pooled, {"tp": 35, "fp": 7, "fn": 4, "tn": 350})

    def test_negative_mutant_average_ratios_rejected(self):
        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        h2 = Handle(kind="x11_window", canonical_id="win-2", generation=1)

        w1 = WindowEvidence(
            handle=h1, scene="scene_1", epoch=0, frame_seq=1,
            tp=10, fp=2, fn=1, tn=100, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_2026_valid", violations=(),
        )
        w2 = WindowEvidence(
            handle=h2, scene="scene_2", epoch=0, frame_seq=1,
            tp=20, fp=4, fn=2, tn=200, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_2026_valid", violations=(),
        )

        pooled_true, _, _ = contract_pool_confusion_and_validate((w1, w2))
        mutated_pooled, _, _ = mutant_average_ratios_confusion((w1, w2))

        # True integer sum: tp=30, fp=6, fn=3, tn=300
        # Average mutant: tp=15.0, fp=3.0, fn=1.5, tn=150.0
        self.assertEqual(pooled_true, {"tp": 30, "fp": 6, "fn": 3, "tn": 300})
        self.assertNotEqual(pooled_true, mutated_pooled)
        self.assertEqual(mutated_pooled["tp"], 15.0)

    def test_negative_mutant_replace_null_with_zero_rejected(self):
        metrics_with_null = {
            "cross_window_bleed": 0,
            "safety_violations": 0,
            "frame_exactness": None,  # Missing denominator / support
            "pooled_confusion": {"tp": 10, "fp": 0, "fn": 0, "tn": 100},
        }

        mutated = mutant_replace_null_with_zero(metrics_with_null)
        self.assertIsNone(metrics_with_null["frame_exactness"])
        self.assertEqual(mutated["frame_exactness"], 0)
        # Null metric must NOT become 0 or passing evidence
        self.assertNotEqual(metrics_with_null["frame_exactness"], mutated["frame_exactness"])

    def test_missing_seal_rejected(self):
        h = Handle(kind="godot_window", canonical_id="godot-1", generation=1)
        w = WindowEvidence(
            handle=h, scene="scene_1", epoch=0, frame_seq=1,
            tp=5, fp=0, fn=0, tn=50, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="", violations=(),
        )
        _, passed, violations = contract_pool_confusion_and_validate((w,))
        self.assertFalse(passed)
        self.assertIn("MISSING_SEAL", violations)

    def test_mixed_seals_rejected(self):
        h1 = Handle(kind="godot_window", canonical_id="godot-1", generation=1)
        h2 = Handle(kind="cdp_tab", canonical_id="cdp-1", generation=1)
        w1 = WindowEvidence(
            handle=h1, scene="scene_1", epoch=0, frame_seq=1,
            tp=5, fp=0, fn=0, tn=50, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_alpha", violations=(),
        )
        w2 = WindowEvidence(
            handle=h2, scene="scene_2", epoch=0, frame_seq=1,
            tp=5, fp=0, fn=0, tn=50, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_beta", violations=(),
        )
        _, passed, violations = contract_pool_confusion_and_validate((w1, w2))
        self.assertFalse(passed)
        self.assertIn("MIXED_SEAL", violations)

    def test_duplicate_identity_rejected(self):
        h = Handle(kind="x11_window", canonical_id="win-1", generation=1)
        w1 = WindowEvidence(
            handle=h, scene="scene_dup", epoch=1, frame_seq=5,
            tp=1, fp=0, fn=0, tn=10, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_valid", violations=(),
        )
        w2 = WindowEvidence(
            handle=h, scene="scene_dup", epoch=1, frame_seq=5,
            tp=1, fp=0, fn=0, tn=10, ops={}, frame_supported=True,
            dropped=False, exact=True, terminal="OK", expected_terminal="OK",
            seal="seal_valid", violations=(),
        )
        _, passed, violations = contract_pool_confusion_and_validate((w1, w2))
        self.assertFalse(passed)
        self.assertIn("DUPLICATE_IDENTITY", violations)

    def test_absent_frame_support_rejected(self):
        h = Handle(kind="xvfb_screen", canonical_id="scr-1", generation=1)
        w = WindowEvidence(
            handle=h, scene="scene_nosup", epoch=0, frame_seq=1,
            tp=0, fp=0, fn=0, tn=0, ops={}, frame_supported=False,
            dropped=False, exact=None, terminal="OK", expected_terminal="OK",
            seal="seal_valid", violations=(),
        )
        _, passed, violations = contract_pool_confusion_and_validate((w,))
        self.assertFalse(passed)
        self.assertIn("ABSENT_FRAME_SUPPORT", violations)


def _sealed_rows(rows):
    from eval.v2eval.multiwin_scorecard import TrialReport, seal_trial
    return seal_trial(TrialReport(rows=tuple(rows), outcomes={}, faults=())).rows


class TestVisionMultiwinScorecardOwner(unittest.TestCase):
    """Owner module conformance tests probing eval.v2eval.multiwin_scorecard."""

    @probe_owner_symbol("score_windows")
    def test_owner_pooled_confusion_equals_sum_and_unequal_denominators(self):
        from eval.v2eval.multiwin_scorecard import score_windows, WindowEvidence, Handle

        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        h2 = Handle(kind="x11_window", canonical_id="win-2", generation=1)
        h3 = Handle(kind="godot_window", canonical_id="godot-3", generation=1)

        w1 = WindowEvidence(
            handle=h1, scene="scene_a", epoch=0, frame_seq=1,
            tp=10, fp=1, fn=0, tn=100, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )
        w2 = WindowEvidence(
            handle=h2, scene="scene_b", epoch=0, frame_seq=1,
            tp=5, fp=2, fn=3, tn=50, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )
        w3 = WindowEvidence(
            handle=h3, scene="scene_c", epoch=0, frame_seq=1,
            tp=20, fp=0, fn=1, tn=200, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )

        scorecard = score_windows(_sealed_rows((w1, w2, w3)))
        self.assertTrue(scorecard.passed)
        self.assertEqual(scorecard.pooled["tp"], 35)
        self.assertEqual(scorecard.pooled["fp"], 3)
        self.assertEqual(scorecard.pooled["fn"], 4)
        self.assertEqual(scorecard.pooled["tn"], 350)

    @probe_owner_symbol("score_windows")
    def test_owner_rejects_missing_and_mixed_seals(self):
        from eval.v2eval.multiwin_scorecard import score_windows, WindowEvidence, Handle

        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        h2 = Handle(kind="x11_window", canonical_id="win-2", generation=1)

        # Missing seal
        w_missing = WindowEvidence(
            handle=h1, scene="scene_a", epoch=0, frame_seq=1,
            tp=10, fp=1, fn=0, tn=100, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )
        from eval.v2eval.multiwin_scorecard import SealMismatchError
        with self.assertRaises(SealMismatchError):
            score_windows((w_missing,))

        # Mixed seals
        w_seal1 = WindowEvidence(
            handle=h1, scene="scene_a", epoch=0, frame_seq=1,
            tp=10, fp=1, fn=0, tn=100, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="seal_A", violations=(),
        )
        w_seal2 = WindowEvidence(
            handle=h2, scene="scene_b", epoch=0, frame_seq=1,
            tp=5, fp=2, fn=3, tn=50, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="seal_B", violations=(),
        )
        with self.assertRaises(SealMismatchError):
            score_windows((w_seal1, w_seal2))

    @probe_owner_symbol("score_windows")
    def test_owner_rejects_duplicate_and_foreign_identities(self):
        from eval.v2eval.multiwin_scorecard import score_windows, WindowEvidence, Handle

        h = Handle(kind="xvfb_screen", canonical_id="scr-1", generation=1)
        w1 = WindowEvidence(
            handle=h, scene="scene_dup", epoch=0, frame_seq=1,
            tp=1, fp=0, fn=0, tn=10, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )
        w2 = WindowEvidence(
            handle=h, scene="scene_dup", epoch=0, frame_seq=1,
            tp=1, fp=0, fn=0, tn=10, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )

        from eval.v2eval.multiwin_scorecard import WindowIdentityError
        with self.assertRaises(WindowIdentityError):
            score_windows(_sealed_rows((w1, w2)))

    @probe_owner_symbol("score_windows")
    def test_owner_absent_frame_support_yields_null_metric(self):
        from eval.v2eval.multiwin_scorecard import score_windows, WindowEvidence, Handle

        h = Handle(kind="capsule", canonical_id="cap-1", generation=1)
        w = WindowEvidence(
            handle=h, scene="scene_nosup", epoch=0, frame_seq=1,
            tp=0, fp=0, fn=0, tn=0, ops={"isolated_ops": {}}, frame_supported=False,
            dropped=False, exact=None, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )

        scorecard = score_windows(_sealed_rows((w,)))
        self.assertFalse(scorecard.passed)
        # Null metric must NOT become 0 or passing
        self.assertIsNone(scorecard.metrics["frame_exactness"]["denominator"])
        self.assertIsNone(scorecard.metrics["frame_exactness"]["value"])
        self.assertFalse(scorecard.metrics["frame_exactness"]["defined"])
        self.assertFalse(scorecard.metrics["frame_exactness"]["passed"])

    @probe_owner_symbol("canonical_bytes")
    def test_owner_canonical_bytes_reproducibility(self):
        from eval.v2eval.multiwin_scorecard import score_windows, canonical_bytes, WindowEvidence, Handle

        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        w = WindowEvidence(
            handle=h, scene="scene_a", epoch=0, frame_seq=1,
            tp=10, fp=1, fn=0, tn=100, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )

        scorecard = score_windows(_sealed_rows((w,)))
        bytes1 = canonical_bytes(scorecard)
        bytes2 = canonical_bytes(scorecard)

        self.assertIsInstance(bytes1, bytes)
        self.assertEqual(bytes1, bytes2)

    @probe_owner_symbol("score_windows")
    def test_owner_all_supported_target_kinds(self):
        from eval.v2eval.multiwin_scorecard import score_windows, WindowEvidence, Handle

        target_kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]
        rows = []
        for idx, kind in enumerate(target_kinds):
            h = Handle(kind=kind, canonical_id=f"id-{idx}", generation=1)
            w = WindowEvidence(
                handle=h, scene=f"scene_{kind}", epoch=0, frame_seq=1,
                tp=2, fp=0, fn=0, tn=20, ops={"isolated_ops": {}}, frame_supported=True,
                dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
                seal="", violations=(),
            )
            rows.append(w)

        scorecard = score_windows(_sealed_rows(tuple(rows)))
        self.assertTrue(scorecard.passed)
        self.assertEqual(scorecard.pooled["tp"], 10)

    @probe_owner_symbol("score_windows")
    def test_owner_required_metrics_keys_and_null_handling(self):
        from eval.v2eval.multiwin_scorecard import score_windows, WindowEvidence, Handle

        required_keys = {
            "cross_window_bleed",
            "safety_violations",
            "frame_exactness",
            "frame_drops",
            "operation_totals",
            "eligibility_stalls",
            "starvation",
            "terminal_completeness",
            "pooled_confusion",
        }

        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        w = WindowEvidence(
            handle=h, scene="scene_a", epoch=0, frame_seq=1,
            tp=10, fp=1, fn=0, tn=100, ops={"isolated_ops": {}}, frame_supported=True,
            dropped=False, exact=True, terminal="COMPLETE", expected_terminal="COMPLETE",
            seal="", violations=(),
        )

        scorecard = score_windows(_sealed_rows((w,)))
        for k in required_keys:
            self.assertIn(k, scorecard.metrics)


if __name__ == "__main__":
    unittest.main()
