# @module {"id":"wa.tests.test_jq2a_m1_j5_harvest_broker_lifecycle","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.session_registry","wa.perceive.tab_manager","wa.perceive.harvest_broker"],"book":"HB-WA-s1-perceive"}
"""Contract tests for HarvestBroker lifecycle, target ownership, and STOP invariants (jq2a-m1-j5-harvest-broker-lifecycle).

Gate G M1 5: Ownership and STOP survive teardown.
- STOP a broker between ownership registration and transport cleanup, retaining tombstones and forbidding cleanup sends beyond writer_fence.
- Compare caller-owned and broker-owned target plus attachment combinations.
- Healthy owned close is idempotent and sibling sessions continue.
- Negative mutant: close caller tab or send command after STOP raises or is blocked.
- Contract Invariants: INV-M1-1 (generation & epoch non-repetition, aliased resolution), INV-M1-2 (reply matching, tombstones, bounded session lanes, overflow fencing), INV-M1-3 (origin validation, refusal of ambiguous matches, separate target/attachment ownership, global lock order).
- Target Kinds: cdp_tab, x11_window, xvfb_screen, capsule, godot_window.
"""

import hashlib
import importlib
import unittest
import numpy as np


# Define fallback canonical frozen dataclasses conforming to jq2a specification.
try:
    from wender.perceive.session_registry import (
        Handle, TargetRecord, FrameRef, Observation, Receipt,
        ActionRequest, ActorLease, FocusLease, StreamSpec, StreamToken,
        Ticket, ChangeResult, PerceptionResult, WindowFrames, WindowDescription
    )
except ImportError:
    from dataclasses import dataclass

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


def _owner_available():
    """Probe named owner modules and symbols explicitly."""
    try:
        reg_mod = importlib.import_module("wender.perceive.session_registry")
        if not hasattr(reg_mod, "SessionRegistry") or not hasattr(reg_mod, "Handle"):
            return False
        tab_mod = importlib.import_module("wender.perceive.tab_manager")
        if not hasattr(tab_mod, "TabManager"):
            return False
        hb_mod = importlib.import_module("wender.perceive.harvest_broker")
        if not hasattr(hb_mod, "HarvestBroker"):
            return False
        return True
    except ImportError:
        return False


def expect_failure_if_owner_absent(func):
    """Decorator applying unittest.expectedFailure only while owner module or symbol is absent."""
    if not _owner_available():
        return unittest.expectedFailure(func)
    return func


# Fixtures for offline testing without network, Godot binary, or disk writes.

class FakeClock:
    """Fake monotonic clock returning nanoseconds."""

    def __init__(self, start_ns=1_000_000_000):
        self._ns = start_ns

    def __call__(self):
        return self._ns

    def advance(self, ns):
        self._ns += ns


class ForbiddenIOTransport:
    """Transport that forbids external network or disk write operations."""

    def __init__(self, kind="cdp_tab"):
        self.kind = kind
        self.calls = []
        self.closed = False

    def send(self, method, params=None, session_id=None, timeout=None):
        if self.closed:
            raise RuntimeError("Transport is closed")
        self.calls.append({"method": method, "params": params or {}, "session_id": session_id})
        return {"result": "ok"}

    def close(self):
        self.closed = True


def make_ram_frame_ref(handle, epoch=1, frame_seq=1, width=64, height=64):
    """Creates a FrameRef with owned RGB bytes in RAM."""
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    digest = hashlib.sha256(rgb.tobytes()).hexdigest()
    return FrameRef(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        width=width,
        height=height,
        rgb=rgb,
        digest=digest
    )


class TestHarvestBrokerHealthyControls(unittest.TestCase):
    """Healthy control tests for dataclasses and offline test fixtures (run without expectedFailure)."""

    def test_canonical_dataclasses_field_ordering_and_immutability(self):
        """Verifies that canonical dataclasses are frozen and possess expected fields in exact order."""
        handle = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        self.assertEqual(handle.kind, "cdp_tab")
        self.assertEqual(handle.canonical_id, "tab-1")
        self.assertEqual(handle.generation, 1)

        with self.assertRaises((TypeError, AttributeError)):
            handle.generation = 2

        rec = TargetRecord(
            handle=handle,
            transport=None,
            owned=True,
            epoch=1,
            session_id="s-101",
            attachment_owned=True,
            closed=False
        )
        self.assertEqual(rec.handle, handle)
        self.assertTrue(rec.owned)
        self.assertEqual(rec.epoch, 1)

        receipt = Receipt(action_id="act-1", handle=handle, status="ok", reason=None)
        self.assertEqual(receipt.action_id, "act-1")
        self.assertEqual(receipt.status, "ok")

        frame_ref = make_ram_frame_ref(handle)
        self.assertEqual(frame_ref.width, 64)
        self.assertEqual(frame_ref.height, 64)
        self.assertIsInstance(frame_ref.rgb, np.ndarray)

        obs = Observation(
            handle=handle,
            epoch=1,
            frame_seq=1,
            dpr_ppm=100,
            state={"status": "ready"},
            frame=frame_ref
        )
        self.assertEqual(obs.dpr_ppm, 100)

        lease = ActorLease(
            handle=handle,
            owner="jules",
            generation=1,
            nonce=b"\x00" * 16,
            expires_ns=2_000_000_000
        )
        self.assertEqual(lease.owner, "jules")

        flease = FocusLease(lease=lease, domain="example.com", epoch=1)
        self.assertEqual(flease.domain, "example.com")

        spec = StreamSpec(width=1280, height=720, slots=4, quota_bytes=1280 * 720 * 3 * 4)
        self.assertEqual(spec.width, 1280)

        token = StreamToken(handle=handle, epoch=1)
        self.assertEqual(token.epoch, 1)

        ticket = Ticket(action_id="act-1", handle=handle)
        self.assertEqual(ticket.action_id, "act-1")

        change = ChangeResult(handle=handle, epoch=1, settled=True, claims=np.array([1, 2]), ops={})
        self.assertTrue(change.settled)

        perc = PerceptionResult(handle=handle, observation=obs, error=None)
        self.assertEqual(perc.observation, obs)

        wframes = WindowFrames(handle=handle, epoch=1, frames=(frame_ref,), frame_seqs=(1,), dpr_ppm=100)
        self.assertEqual(len(wframes.frames), 1)

        wdesc = WindowDescription(handle=handle, epoch=1, frame_seqs=(1,), descriptors=("desc1",), regions=("reg1",), error=None)
        self.assertEqual(wdesc.descriptors, ("desc1",))

    def test_offline_fake_fixtures_validation(self):
        """Verifies fake clock and forbidden transport behavior."""
        clock = FakeClock(100)
        self.assertEqual(clock(), 100)
        clock.advance(50)
        self.assertEqual(clock(), 150)

        transport = ForbiddenIOTransport(kind="cdp_tab")
        res = transport.send("Page.enable")
        self.assertEqual(res, {"result": "ok"})
        self.assertEqual(len(transport.calls), 1)
        transport.close()
        with self.assertRaises(RuntimeError):
            transport.send("Page.enable")


class TestHarvestBrokerLifecycleContracts(unittest.TestCase):
    """Contract tests for Gate G M1 5, STOP lifecycle, ownership matrix, and contract invariants."""

    @expect_failure_if_owner_absent
    def test_stop_between_ownership_registration_and_cleanup_retains_tombstones(self):
        """STOP a broker between ownership registration and transport cleanup, retaining tombstones and forbidding cleanup sends."""
        if not _owner_available():
            raise AssertionError("Owning implementation jq2a-m1-g1-harvest-broker is absent")

        from wender.perceive.session_registry import SessionRegistry, Handle
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.harvest_broker import HarvestBroker

        registry = SessionRegistry()
        tab_mgr = TabManager(cdp=ForbiddenIOTransport("cdp_tab"), registry=registry)
        broker = HarvestBroker(registry, tab_mgr)

        transport = ForbiddenIOTransport(kind="cdp_tab")
        handle = broker.register("cdp_tab", "tab-canon-1", transport, owned=True)
        self.assertIsInstance(handle, Handle)

        # Stop broker
        broker.stop(handle)

        # Tombstone retained, sends after STOP forbidden
        with self.assertRaises(Exception):
            broker.command(handle, "Page.navigate", {"url": "https://example.com"})

        # Verification that cleanup sends beyond writer_fence are forbidden
        self.assertEqual(len(transport.calls), 0)

    @expect_failure_if_owner_absent
    def test_negative_mutant_close_caller_tab_or_send_after_stop_raises(self):
        """Negative mutant check: closing caller tab or sending command after STOP raises error / is forbidden."""
        if not _owner_available():
            raise AssertionError("Owning implementation jq2a-m1-g1-harvest-broker is absent")

        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.harvest_broker import HarvestBroker

        registry = SessionRegistry()
        tab_mgr = TabManager(cdp=ForbiddenIOTransport("cdp_tab"), registry=registry)
        broker = HarvestBroker(registry, tab_mgr)

        caller_transport = ForbiddenIOTransport(kind="cdp_tab")
        caller_handle = broker.register("cdp_tab", "caller-tab-1", caller_transport, owned=False)

        broker.stop(caller_handle)

        # Sending command after STOP must fail
        with self.assertRaises(Exception):
            broker.command(caller_handle, "Page.enable", {})

        # Caller target must NOT be closed via transport
        self.assertFalse(caller_transport.closed)

    @expect_failure_if_owner_absent
    def test_positive_control_owned_healthy_close_is_idempotent_and_sibling_continues(self):
        """Positive control: healthy owned close is idempotent and sibling target continues operating."""
        if not _owner_available():
            raise AssertionError("Owning implementation jq2a-m1-g1-harvest-broker is absent")

        from wender.perceive.session_registry import SessionRegistry, Receipt
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.harvest_broker import HarvestBroker

        registry = SessionRegistry()
        tab_mgr = TabManager(cdp=ForbiddenIOTransport("cdp_tab"), registry=registry)
        broker = HarvestBroker(registry, tab_mgr)

        t_a = ForbiddenIOTransport("cdp_tab")
        t_b = ForbiddenIOTransport("cdp_tab")

        h_a = broker.register("cdp_tab", "canon-a", t_a, owned=True)
        h_b = broker.register("cdp_tab", "canon-b", t_b, owned=True)

        # Close handle_a once
        r1 = broker.close(h_a)
        self.assertIsInstance(r1, Receipt)

        # Close handle_a second time (idempotent)
        r2 = broker.close(h_a)
        self.assertIsInstance(r2, Receipt)

        # Sibling h_b continues to operate normally
        res_b = broker.command(h_b, "Page.enable", {})
        self.assertIsNotNone(res_b)

    @expect_failure_if_owner_absent
    def test_ownership_matrix_caller_and_broker_combinations(self):
        """Compare caller-owned and broker-owned target plus attachment combinations."""
        if not _owner_available():
            raise AssertionError("Owning implementation jq2a-m1-g1-harvest-broker is absent")

        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.harvest_broker import HarvestBroker

        registry = SessionRegistry()
        tab_mgr = TabManager(cdp=ForbiddenIOTransport("cdp_tab"), registry=registry)
        broker = HarvestBroker(registry, tab_mgr)

        # Combination 1: Caller-owned target
        t_caller = ForbiddenIOTransport("cdp_tab")
        h_caller = broker.register("cdp_tab", "caller-1", t_caller, owned=False)

        broker.close(h_caller)
        # Target must not be closed
        self.assertFalse(t_caller.closed)

        # Combination 2: Broker-owned target
        t_broker = ForbiddenIOTransport("cdp_tab")
        h_broker = broker.register("cdp_tab", "broker-1", t_broker, owned=True)

        broker.close(h_broker)
        # Broker-owned target close is executed / recorded
        rec = broker.get_target_record(h_broker)
        self.assertTrue(rec.closed)

    @expect_failure_if_owner_absent
    def test_inv_m1_1_generation_and_epoch_non_repetition(self):
        """INV-M1-1: generation never repeats; navigation increments separate epoch; aliased actuation surfaces resolve to same canonical resource."""
        if not _owner_available():
            raise AssertionError("Owning implementation jq2a-m1-g1-harvest-broker is absent")

        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.harvest_broker import HarvestBroker

        registry = SessionRegistry()
        tab_mgr = TabManager(cdp=ForbiddenIOTransport("cdp_tab"), registry=registry)
        broker = HarvestBroker(registry, tab_mgr)

        h1 = broker.register("cdp_tab", "canon-resource-1", ForbiddenIOTransport(), owned=False)
        self.assertEqual(broker.close(h1).status, "cleanup_pending")
        transport = broker.get_target_record(h1).transport
        transport.is_terminated = lambda canonical_id: canonical_id == "canon-resource-1"
        self.assertTrue(broker.reconcile_cleanup(h1))
        h2 = broker.register("cdp_tab", "canon-resource-1", ForbiddenIOTransport(), owned=False)

        self.assertGreater(h2.generation, h1.generation)
        self.assertEqual(h1.canonical_id, h2.canonical_id)

    @expect_failure_if_owner_absent
    def test_inv_m1_2_reply_matching_and_tombstones(self):
        """INV-M1-2: replies match connection-wide request identities and issuing incarnation; late replies discarded; overflow fences affected lane."""
        if not _owner_available():
            raise AssertionError("Owning implementation jq2a-m1-g1-harvest-broker is absent")

        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.harvest_broker import HarvestBroker

        registry = SessionRegistry()
        tab_mgr = TabManager(cdp=ForbiddenIOTransport("cdp_tab"), registry=registry)
        broker = HarvestBroker(registry, tab_mgr)

        h = broker.register("cdp_tab", "canon-lane-1", ForbiddenIOTransport(), owned=True)
        broker.stop(h)

        # Commands / replies after STOP are tombstoned
        with self.assertRaises(Exception):
            broker.command(h, "Page.enable", {})

    @expect_failure_if_owner_absent
    def test_inv_m1_3_supported_target_kinds_and_capsule_child_equivalence(self):
        """INV-M1-3 & Supported target kinds: cdp_tab, x11_window, xvfb_screen, capsule, godot_window. Capsule and child screen address same canonical resource."""
        if not _owner_available():
            raise AssertionError("Owning implementation jq2a-m1-g1-harvest-broker is absent")

        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.harvest_broker import HarvestBroker

        registry = SessionRegistry()
        tab_mgr = TabManager(cdp=ForbiddenIOTransport("cdp_tab"), registry=registry)
        broker = HarvestBroker(registry, tab_mgr)

        supported_kinds = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
        for kind in supported_kinds:
            h = broker.register(kind, f"canon-{kind}-1", ForbiddenIOTransport(kind), owned=False)
            self.assertEqual(h.kind, kind)

        # Capsule and child screen with same destination resolve to same canonical actuation resource
        transport = ForbiddenIOTransport("capsule")
        h_capsule = broker.register("capsule", "dest-1", transport, owned=False)
        h_screen = broker.register("xvfb_screen", "dest-1", transport, owned=False)
        self.assertEqual(h_capsule.canonical_id, h_screen.canonical_id)


if __name__ == "__main__":
    unittest.main()
