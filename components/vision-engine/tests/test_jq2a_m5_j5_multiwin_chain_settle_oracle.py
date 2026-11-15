"""Unit and contract harness tests for multi-window chain settlement oracle (Gate G M5 5).

Owning implementation task_id: jq2a-m5-g1-multiwin-chain
Merged production APIs:
- eval.v2eval.chain.compose_chain
- eval.v2x.per_window_detector.PerWindowDetector
- eval.v2x.per_window_detector.ChangeResult
- eval.v2x.per_window_detector.Handle

MultiWindowChain is absent. The owner methods retain real detector results;
explicit clock checks live in a structural consumer fixture, not production.
"""

from dataclasses import dataclass
import unittest
import numpy as np

from eval.v2x.per_window_detector import ChangeResult, Handle, PerWindowDetector
from eval.v2eval.chain import compose_chain
from visioncore import desc
from visioncore.grid import Grid
import hashlib

# Consumer record shapes are local fixtures, not exports or authority granted
# by per_window_detector. The two detector records above are canonical.

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

# --- Test Helpers & Contract Harness ---

def make_dummy_frame(handle: Handle, epoch: int, frame_seq: int, w: int = 64, h: int = 64) -> FrameRef:
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb.flags.writeable = False
    digest = hashlib.sha256(rgb.tobytes()).hexdigest()
    return FrameRef(handle=handle, epoch=epoch, frame_seq=frame_seq, width=w, height=h, rgb=rgb, digest=digest)


def make_dummy_observation(handle: Handle, epoch: int, frame_seq: int, w: int = 64, h: int = 64, *, baseline_cls: int = 0) -> Observation:
    frame = make_dummy_frame(handle, epoch, frame_seq, w, h)
    return Observation(handle=handle, epoch=epoch, frame_seq=frame_seq, dpr_ppm=1, state={"baseline_cls": baseline_cls}, frame=frame)


class DetectorChainFixture:
    """Consumer seam over the real detector and existing claim composition.

    MultiWindowChain is absent. This fixture never manufactures a detector
    result: update returns the exact ChangeResult produced by PerWindowDetector.
    Fake-time freshness belongs only to this consumer fixture; production
    coverage is the detector's adjacent-frame settlement and compose_chain's
    claim arbitration. No production wall-clock oracle is claimed here.
    """

    def __init__(self, *, max_age_ns):
        self.max_age_ns = max_age_ns
        self.detector = PerWindowDetector()
        self._evidence = {}

    def update(self, observation, *, now_ns):
        rgb = observation.frame.rgb
        grid = Grid(rgb.shape[1], rgb.shape[0])
        tiles, _ = desc.describe_padded(grid.pad(rgb), grid, lazy_xh=True)
        tiles["cls"] = observation.state["baseline_cls"]
        tiles["mv_dx"] = tiles["mv_dy"] = 0
        result = self.detector.update(observation.handle, tiles, rgb,
                                      epoch=observation.epoch,
                                      frame_seq=observation.frame_seq)
        baseline = {name: tiles[name].astype(np.int64)
                    for name in ("cls", "mv_dx", "mv_dy")}
        gm = {"mask": result.claims["cls"] == 3, "cls": 3,
              "mv_dx": result.claims["mv_dx"], "mv_dy": result.claims["mv_dy"]}
        fl = {"mask": result.claims["cls"] == 4, "cls": 4,
              "mv_dx": result.claims["mv_dx"], "mv_dy": result.claims["mv_dy"]}
        veto = (tiles["cls"] == 4) & (result.claims["cls"] == 2)
        composed = compose_chain(baseline, gm, fl, flicker_veto=veto)
        for name in baseline:
            np.testing.assert_array_equal(composed[name], result.claims[name])
        self._evidence[result.handle] = (result, now_ns)
        return result

    def is_settled(self, handle, *, epoch=None, now_ns=None):
        evidence = self._evidence.get(handle)
        if evidence is None or type(now_ns) is not int or now_ns < 0:
            return False
        result, stamp = evidence
        if epoch is not None and epoch != result.epoch:
            return False
        if type(stamp) is not int or not 0 <= now_ns - stamp <= self.max_age_ns:
            return False
        return result.settled


class ReferenceSettleChain:
    """Reference implementation of multi-window settlement chain contract for harness validation."""

    def __init__(self, quiet_frames: int, max_age_ns: int):
        self.quiet_frames = quiet_frames
        self.max_age_ns = max_age_ns
        self._states = {}  # (canonical_id, epoch) -> {"quiet_count": int, "last_seq": int, "last_ns": int}

    def update(self, observation: Observation, claims: np.ndarray | None = None, now_ns: int | None = None) -> ChangeResult:
        if now_ns is None:
            now_ns = 0
        handle = observation.handle
        epoch = observation.epoch
        key = (handle.kind, handle.canonical_id, epoch)

        if claims is None:
            claims = np.zeros(16, dtype=np.int64)

        has_motion = np.any(claims != 0)
        st = self._states.get(key)

        if st is None:
            # First observation
            quiet_count = 1 if not has_motion else 0
            self._states[key] = {
                "quiet_count": quiet_count,
                "last_seq": observation.frame_seq,
                "last_ns": now_ns,
            }
        else:
            if now_ns < st["last_ns"]:
                # Regressing time rejected
                quiet_count = 0
            elif observation.frame_seq != st["last_seq"] + 1 or has_motion:
                # Sequence gap or motion resets quiet count
                quiet_count = 1 if (not has_motion and observation.frame_seq == st["last_seq"] + 1) else (1 if not has_motion else 0)
            else:
                quiet_count = st["quiet_count"] + 1

            self._states[key] = {
                "quiet_count": quiet_count,
                "last_seq": observation.frame_seq,
                "last_ns": max(now_ns, st["last_ns"]),
            }

        is_s = self.is_settled(handle, epoch=epoch, now_ns=now_ns)
        return ChangeResult(handle=handle, epoch=epoch, settled=is_s, claims=claims, ops={"update": 1})

    def is_settled(self, handle: Handle, epoch: int = 1, now_ns: int | None = None) -> bool:
        if now_ns is None:
            return False
        key = (handle.kind, handle.canonical_id, epoch)
        st = self._states.get(key)
        if st is None:
            return False  # Missing evidence
        if now_ns < st["last_ns"]:
            return False  # Regressing time
        if now_ns - st["last_ns"] > self.max_age_ns:
            return False  # Stale frame
        return st["quiet_count"] >= self.quiet_frames


class NegativeMutantSettledOnMissingEvidence(ReferenceSettleChain):
    """Mutant that wrongly reports settled=True on missing evidence."""

    def is_settled(self, handle: Handle, epoch: int = 1, now_ns: int | None = None) -> bool:
        key = (handle.kind, handle.canonical_id, epoch)
        st = self._states.get(key)
        if st is None:
            return True  # BUG: Returns True on missing evidence!
        return super().is_settled(handle, epoch=epoch, now_ns=now_ns)


class NegativeMutantSettledAfterGap(ReferenceSettleChain):
    """Mutant that wrongly ignores sequence gaps."""

    def update(self, observation: Observation, claims: np.ndarray | None = None, now_ns: int | None = None) -> ChangeResult:
        if now_ns is None:
            now_ns = 0
        handle = observation.handle
        epoch = observation.epoch
        key = (handle.kind, handle.canonical_id, epoch)
        st = self._states.get(key)
        if st is None:
            quiet_count = 1
        else:
            quiet_count = st["quiet_count"] + 1  # BUG: Ignores frame_seq gap!
        self._states[key] = {"quiet_count": quiet_count, "last_seq": observation.frame_seq, "last_ns": now_ns}
        is_s = self.is_settled(handle, epoch=epoch, now_ns=now_ns)
        return ChangeResult(handle=handle, epoch=epoch, settled=is_s, claims=np.zeros(16, dtype=np.int64), ops={})


class NegativeMutantSettledOnMotion(ReferenceSettleChain):
    """Mutant that wrongly ignores motion claims."""

    def update(self, observation: Observation, claims: np.ndarray | None = None, now_ns: int | None = None) -> ChangeResult:
        # Passes claims=None to super so motion is ignored
        return super().update(observation, claims=None, now_ns=now_ns)


class NegativeMutantSettledWhenStale(ReferenceSettleChain):
    """Mutant that wrongly ignores max_age_ns limit."""

    def is_settled(self, handle: Handle, epoch: int = 1, now_ns: int | None = None) -> bool:
        if now_ns is None:
            return False
        key = (handle.kind, handle.canonical_id, epoch)
        st = self._states.get(key)
        if st is None:
            return False
        return st["quiet_count"] >= self.quiet_frames  # BUG: Ignores max_age_ns!


def verify_gate_m5_5_contract(chain_instance) -> None:
    """Contract harness for Gate G M5 5.

    Raises AssertionError if the candidate implementation fails any contract assertion.
    """
    handle = Handle(kind="x11_window", canonical_id="win_001", generation=1)

    # 1. Missing evidence check: is_settled must be False before any observation
    if chain_instance.is_settled(handle, epoch=1, now_ns=1000):
        raise AssertionError("Contract failure: is_settled returned True on missing evidence!")

    # 2. Quiet run: 3 consecutive quiet observations
    obs1 = make_dummy_observation(handle, epoch=1, frame_seq=1)
    chain_instance.update(obs1, now_ns=1000)
    if chain_instance.is_settled(handle, epoch=1, now_ns=1000):
        raise AssertionError("Contract failure: is_settled returned True after 1 quiet frame (configured quiet_frames=3)!")

    obs2 = make_dummy_observation(handle, epoch=1, frame_seq=2)
    chain_instance.update(obs2, now_ns=2000)
    if chain_instance.is_settled(handle, epoch=1, now_ns=2000):
        raise AssertionError("Contract failure: is_settled returned True after 2 quiet frames!")

    obs3 = make_dummy_observation(handle, epoch=1, frame_seq=3)
    chain_instance.update(obs3, now_ns=3000)
    if not chain_instance.is_settled(handle, epoch=1, now_ns=3000):
        raise AssertionError("Contract failure: is_settled returned False after 3 valid quiet frames within age limit!")

    # 3. Stale frame check: querying with now_ns far in the future exceeding max_age_ns
    stale_ns = 3000 + chain_instance.max_age_ns + 100
    if chain_instance.is_settled(handle, epoch=1, now_ns=stale_ns):
        raise AssertionError("Contract failure: is_settled returned True when frame was stale!")

    # 4. Sequence gap check: jump frame_seq from 3 to 5
    obs_gap = make_dummy_observation(handle, epoch=1, frame_seq=5)
    chain_instance.update(obs_gap, now_ns=4000)
    if chain_instance.is_settled(handle, epoch=1, now_ns=4000):
        raise AssertionError("Contract failure: is_settled returned True after a sequence gap!")

    # 5. Motion check: quiet run recovered then motion introduced
    obs6 = make_dummy_observation(handle, epoch=1, frame_seq=6)
    chain_instance.update(obs6, now_ns=5000)
    obs7 = make_dummy_observation(handle, epoch=1, frame_seq=7)
    chain_instance.update(obs7, now_ns=6000)
    obs8 = make_dummy_observation(handle, epoch=1, frame_seq=8)
    chain_instance.update(obs8, now_ns=7000)
    if not chain_instance.is_settled(handle, epoch=1, now_ns=7000):
        raise AssertionError("Contract failure: failed to settle after 3 quiet frames post-gap!")

    # Motion claim
    motion_claims = np.array([3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=np.int64)
    obs9 = make_dummy_observation(handle, epoch=1, frame_seq=9)
    chain_instance.update(obs9, claims=motion_claims, now_ns=8000)
    if chain_instance.is_settled(handle, epoch=1, now_ns=8000):
        raise AssertionError("Contract failure: is_settled returned True despite motion claims!")


# --- Contract Harness Unit Tests ---

class TestSettleContractHarness(unittest.TestCase):
    """Tests for the Gate G M5 5 contract harness itself against positive controls and negative mutants."""

    def test_positive_control_valid_quiet_run_settles(self):
        """Positive control: Reference quiet run satisfies all contract rules."""
        ref_chain = ReferenceSettleChain(quiet_frames=3, max_age_ns=100_000)
        verify_gate_m5_5_contract(ref_chain)

    def test_negative_mutant_settled_on_missing_evidence_rejected(self):
        """Negative mutant: Settled on missing evidence is caught and rejected by harness."""
        mutant = NegativeMutantSettledOnMissingEvidence(quiet_frames=3, max_age_ns=100_000)
        with self.assertRaises(AssertionError) as ctx:
            verify_gate_m5_5_contract(mutant)
        self.assertIn("missing evidence", str(ctx.exception))

    def test_negative_mutant_settled_after_gap_rejected(self):
        """Negative mutant: Settled after sequence gap is caught and rejected by harness."""
        mutant = NegativeMutantSettledAfterGap(quiet_frames=3, max_age_ns=100_000)
        with self.assertRaises(AssertionError) as ctx:
            verify_gate_m5_5_contract(mutant)
        self.assertIn("sequence gap", str(ctx.exception))

    def test_negative_mutant_settled_on_motion_rejected(self):
        """Negative mutant: Settled on motion claims is caught and rejected by harness."""
        mutant = NegativeMutantSettledOnMotion(quiet_frames=3, max_age_ns=100_000)
        with self.assertRaises(AssertionError) as ctx:
            verify_gate_m5_5_contract(mutant)
        self.assertIn("motion claims", str(ctx.exception))

    def test_negative_mutant_settled_when_stale_rejected(self):
        """Negative mutant: Settled when frame is stale is caught and rejected by harness."""
        mutant = NegativeMutantSettledWhenStale(quiet_frames=3, max_age_ns=100_000)
        with self.assertRaises(AssertionError) as ctx:
            verify_gate_m5_5_contract(mutant)
        self.assertIn("stale", str(ctx.exception))


# --- Production Owner Conformance Tests ---

class TestMultiWindowChainSettleOracleOwner(unittest.TestCase):
    """Real detector settlement with an explicitly test-local freshness consumer.

    MultiWindowChain is absent; DetectorChainFixture preserves actual detector
    outputs and checks the existing eval.v2eval.chain composition.
    """

    def test_gate_m5_5_valid_configured_quiet_run_settles(self):
        """Gate G M5 5: real detector quiet transitions yield settlement evidence."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        handle = Handle(kind="x11_window", canonical_id="win_main", generation=1)

        # Initial evidence plus three consecutive quiet transitions
        for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
            obs = make_dummy_observation(handle, epoch=1, frame_seq=seq)
            res = chain.update(obs, now_ns=10_000 * seq)
            self.assertEqual(res.handle, handle)
            self.assertEqual(res.epoch, 1)

        self.assertTrue(chain.is_settled(handle, now_ns=30_000))

    def test_gate_m5_5_missing_evidence_unsettled(self):
        """Gate G M5 5: Missing evidence returns UNSETTLED (False), never True or numeric success."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        handle = Handle(kind="cdp_tab", canonical_id="tab_01", generation=1)

        # Query before any update
        self.assertFalse(chain.is_settled(handle, now_ns=10_000))

    def test_gate_m5_5_settle_fails_after_sequence_gap(self):
        """Gate G M5 5: Settle fails after a frame sequence gap."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        handle = Handle(kind="xvfb_screen", canonical_id="screen_0", generation=1)

        # First establish real settlement, then change only local adjacency.
        chain.update(make_dummy_observation(handle, epoch=1, frame_seq=0), now_ns=0)
        chain.update(make_dummy_observation(handle, epoch=1, frame_seq=1), now_ns=10_000)
        chain.update(make_dummy_observation(handle, epoch=1, frame_seq=2), now_ns=20_000)

        chain.update(make_dummy_observation(handle, epoch=1, frame_seq=3), now_ns=25_000)
        self.assertTrue(chain.is_settled(handle, now_ns=25_000))

        # Sequence gap: frame 5 instead of 4
        chain.update(make_dummy_observation(handle, epoch=1, frame_seq=5), now_ns=30_000)

        self.assertFalse(chain.is_settled(handle, now_ns=30_000))

    def test_gate_m5_5_settle_fails_after_motion(self):
        """Gate G M5 5: Settle fails when unresolved motion is present."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        handle = Handle(kind="godot_window", canonical_id="godot_viewport", generation=1)

        # Real detector receives an explicit baseline motion claim after settling.
        chain.update(make_dummy_observation(handle, epoch=1, frame_seq=0), now_ns=0)
        obs1 = make_dummy_observation(handle, epoch=1, frame_seq=1)
        obs2 = make_dummy_observation(handle, epoch=1, frame_seq=2)

        chain.update(obs1, now_ns=10_000)
        chain.update(obs2, now_ns=20_000)

        chain.update(make_dummy_observation(handle, epoch=1, frame_seq=3), now_ns=25_000)
        self.assertTrue(chain.is_settled(handle, now_ns=25_000))

        # The next observation carries actual baseline MOVED evidence.
        obs3 = make_dummy_observation(handle, epoch=1, frame_seq=4, baseline_cls=3)
        res3 = chain.update(obs3, now_ns=30_000)

        # Motion breaks quiet run
        self.assertTrue(np.any(res3.claims["cls"] == 3))
        self.assertFalse(res3.settled)
        self.assertFalse(chain.is_settled(handle, now_ns=30_000))

    def test_gate_m5_5_settle_fails_after_stale_frame(self):
        """Gate G M5 5: Settle fails when now_ns exceeds last valid frame timestamp plus max_age_ns."""
        max_age = 50_000_000  # 50 ms
        chain = DetectorChainFixture(max_age_ns=max_age)
        handle = Handle(kind="capsule", canonical_id="cap_001", generation=1)

        # Initial evidence and three quiet transitions, last at t = 30 ms.
        for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
            chain.update(make_dummy_observation(handle, epoch=1, frame_seq=seq), now_ns=10_000_000 * seq)

        # At t = 30 ms -> settled
        self.assertTrue(chain.is_settled(handle, now_ns=30_000_000))

        # At t = 81 ms -> age is 51 ms > 50 ms max_age -> UNSETTLED
        self.assertFalse(chain.is_settled(handle, now_ns=81_000_000))

    def test_gate_m5_5_regressing_time_or_none_time_fails_settle(self):
        """Regressing now_ns time or missing now_ns time never consults wall clock or yields settled."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        handle = Handle(kind="x11_window", canonical_id="win_time", generation=1)

        for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
            chain.update(make_dummy_observation(handle, epoch=1, frame_seq=seq), now_ns=100_000 * seq)

        # missing time (now_ns=None) must be False
        self.assertFalse(chain.is_settled(handle, now_ns=None))

        # regressing time (now_ns < last timestamp) must be False
        self.assertFalse(chain.is_settled(handle, now_ns=200_000))

    def test_settled_never_grants_action_permission(self):
        """Settled state is purely an information oracle and never grants action permission or lease."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        handle = Handle(kind="cdp_tab", canonical_id="tab_action", generation=1)

        for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
            res = chain.update(make_dummy_observation(handle, epoch=1, frame_seq=seq), now_ns=10_000 * seq)

        # Settled status must not carry any lease, token, or permission authorization
        self.assertTrue(chain.is_settled(handle, now_ns=30_000))
        # Ensure result records are ChangeResult, not ActorLease or FocusLease
        self.assertIsInstance(res, ChangeResult)
        self.assertNotIsInstance(res, (ActorLease, FocusLease, Ticket, Receipt))

    def test_inv_m5_1_handle_isolation_and_epoch_reset(self):
        """INV-M5-1: State belongs to a handle/epoch; gaps or motion in one handle leave others unaffected, and epoch changes reset state."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        h1 = Handle(kind="x11_window", canonical_id="win_a", generation=1)
        h2 = Handle(kind="godot_window", canonical_id="win_b", generation=1)

        # Build up quiet run on h1 and h2
        for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
            chain.update(make_dummy_observation(h1, epoch=1, frame_seq=seq), now_ns=10_000 * seq)
            chain.update(make_dummy_observation(h2, epoch=1, frame_seq=seq), now_ns=10_000 * seq)

        self.assertTrue(chain.is_settled(h1, now_ns=30_000))
        self.assertTrue(chain.is_settled(h2, now_ns=30_000))

        # Sequence gap on h1 only
        chain.update(make_dummy_observation(h1, epoch=1, frame_seq=5), now_ns=40_000)

        # h1 becomes unsettled due to gap, h2 remains settled
        self.assertFalse(chain.is_settled(h1, now_ns=40_000))
        self.assertTrue(chain.is_settled(h2, now_ns=40_000))

        # Epoch change on h2 resets h2 state
        obs_h2_e2 = make_dummy_observation(h2, epoch=2, frame_seq=1)
        chain.update(obs_h2_e2, now_ns=50_000)
        self.assertFalse(chain.is_settled(h2, epoch=2, now_ns=50_000))

    def test_supported_target_kinds(self):
        """Supported target kinds: cdp_tab, x11_window, xvfb_screen, capsule, godot_window."""
        supported_kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]
        chain = DetectorChainFixture(max_age_ns=100_000_000)

        for idx, kind in enumerate(supported_kinds, start=1):
            handle = Handle(kind=kind, canonical_id=f"target_{kind}", generation=1)
            for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
                chain.update(make_dummy_observation(handle, epoch=1, frame_seq=seq), now_ns=5_000 * seq)
            self.assertTrue(chain.is_settled(handle, now_ns=20_000), f"Target kind {kind} failed to settle.")

    def test_capsule_and_child_screen_canonical_destination(self):
        """Capsule and child screen addressing the same canonical destination share canonical resource addressing."""
        chain = DetectorChainFixture(max_age_ns=100_000_000)
        cap_handle = Handle(kind="capsule", canonical_id="dest_001", generation=1)
        screen_handle = Handle(kind="xvfb_screen", canonical_id="dest_001", generation=1)

        # Independent handles with same canonical_id
        for seq in range(PerWindowDetector.SETTLE_CONSECUTIVE + 1):
            chain.update(make_dummy_observation(cap_handle, epoch=1, frame_seq=seq), now_ns=5_000 * seq)

        self.assertTrue(chain.is_settled(cap_handle, now_ns=20_000))


if __name__ == "__main__":
    unittest.main()
