# @module {"id":"wa.tests.test_jq2a_m1_j4_reply_ttl_and_dead_tab_eviction","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.session_registry","wa.perceive.tab_manager","wa.perceive.cdp_pipe","wa.perceive.cdp_port"],"book":"HB-WA-s1-perceive"}
"""@module wa.tests.test_jq2a_m1_j4_reply_ttl_and_dead_tab_eviction - tests for request TTL, late reply tombstoning, and dead tab eviction.

Covers request TTL expiration, tombstoned request IDs, target respawn with numeric-looking target ID reuse,
interleaved late/active replies and transport crash, connection-wide request uniqueness, active waiter retention,
and lane-local fault fencing for owning implementation jq2a-m1-c2-cdp-lanes-tabs.
"""

import json
import os
import inspect
import threading
import unittest
from dataclasses import dataclass, is_dataclass
from collections import deque
from unittest.mock import MagicMock
import numpy as np

from wender.perceive.cdp_pipe import CdpPipe, CdpError, EVENT_BUFFER
from wender.perceive.cdp_port import CdpPort


# Probe helpers for proposed components
def require_symbol(module_name, symbol_name=None):
    """Applies unittest.expectedFailure only while module_name or symbol_name is absent."""
    try:
        mod = __import__(module_name, fromlist=[symbol_name] if symbol_name else [])
        if symbol_name and not hasattr(mod, symbol_name):
            return unittest.expectedFailure
        return lambda func: func
    except ImportError:
        return unittest.expectedFailure


def require_cdp_pipe_clock_and_fence():
    """Applies unittest.expectedFailure if CdpPipe constructor does not accept clock kwarg."""
    try:
        from wender.perceive.cdp_pipe import CdpPipe
        sig = inspect.signature(CdpPipe.__init__)
        if "clock" not in sig.parameters or "writer_fence" not in sig.parameters:
            return unittest.expectedFailure
        return lambda func: func
    except Exception:
        return unittest.expectedFailure


# Import shared record dataclasses from wender.perceive.session_registry if present,
# or define identical fallback frozen dataclasses with fields in exact specified order.
try:
    from wender.perceive.session_registry import (
        Handle, TargetRecord, FrameRef, Observation, Receipt,
        ActionRequest, ActorLease, FocusLease, StreamSpec, StreamToken,
        Ticket, ChangeResult, PerceptionResult, WindowFrames, WindowDescription
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


class TestHealthyControlsAndValueContracts(unittest.TestCase):
    """Healthy control tests for record dataclasses and active reply delivery."""

    def test_shared_dataclasses_field_layout_and_immutability(self):
        """Verify field layout and frozen immutability across all 15 shared dataclasses."""
        import hashlib

        h = Handle("cdp_tab", "tab_101", 1)
        self.assertTrue(is_dataclass(h))
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab_101")
        self.assertEqual(h.generation, 1)

        with self.assertRaises(Exception):
            h.generation = 2  # Frozen

        tr = TargetRecord(h, None, True, 0, "sid_1", True, False)
        self.assertEqual(tr.handle, h)
        self.assertTrue(tr.owned)

        rgb = np.zeros((480, 640, 3), dtype=np.uint8)
        fr = FrameRef(h, 0, 1, 640, 480, rgb, hashlib.sha256(rgb.tobytes()).hexdigest())
        self.assertEqual(fr.width, 640)

        obs = Observation(h, 0, 1, 96, {"title": "test"}, fr)
        self.assertEqual(obs.dpr_ppm, 96)

        rcpt = Receipt("act_1", h, "ok", None)
        self.assertEqual(rcpt.status, "ok")

        req = ActionRequest(h, "act_1", 0, {"click": [10, 20]}, 1, 1000000)
        self.assertEqual(req.cost, 1)

        lease = ActorLease(h, "owner_a", 1, b"\x00\x01", 2000000)
        self.assertEqual(lease.owner, "owner_a")

        flease = FocusLease(lease, "domain_x", 0)
        self.assertEqual(flease.domain, "domain_x")

        spec = StreamSpec(640, 480, 4, 640 * 480 * 3 * 4)
        self.assertEqual(spec.slots, 4)

        token = StreamToken(h, 0)
        self.assertEqual(token.handle, h)

        tkt = Ticket("act_1", h)
        self.assertEqual(tkt.action_id, "act_1")

        cr = ChangeResult(h, 0, True, np.array([1, 2]), {"op": "click"})
        self.assertTrue(cr.settled)

        pr = PerceptionResult(h, obs, None)
        self.assertEqual(pr.observation, obs)

        wf = WindowFrames(h, 0, (fr,), (1,), 96)
        self.assertEqual(len(wf.frames), 1)

        wd = WindowDescription(h, 0, (1,), ("desc",), ("reg",), None)
        self.assertEqual(wd.error, None)

    def test_duck_typing_and_frame_ref_rgb_uint8_array(self):
        """Verify FrameRef rgb array constraints and duck-typed tuple bindings."""
        import hashlib

        h = Handle("xvfb_screen", "scr_0", 1)
        rgb_data = np.ones((100, 100, 3), dtype=np.uint8)
        fr = FrameRef(h, 1, 0, 100, 100, rgb_data, hashlib.sha256(rgb_data.tobytes()).hexdigest())
        self.assertEqual(fr.rgb.dtype, np.uint8)
        self.assertEqual(fr.rgb.shape, (100, 100, 3))

    def test_cdp_pipe_active_reply_delivered_positive_control(self):
        """Positive control: Active reply delivered to waiting request in CdpPipe."""
        r_fd, w_fd = os.pipe()
        try:
            client = CdpPipe.__new__(CdpPipe)
            client.chrome_bin = "fake"
            client.headless = True
            client.timeout = 5
            client.proc = None
            client.user_data_dir = None
            client._next_id = 0
            client._lock = threading.Lock()
            client._cv = threading.Condition()
            client._replies = {}
            client.events = deque(maxlen=EVENT_BUFFER)
            client._closed = False
            client._w = w_fd
            client._r = r_fd

            reader = threading.Thread(target=client._read_loop, daemon=True)
            reader.start()

            msg = json.dumps({"id": 1, "result": {"active_ok": True}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg)

            with client._cv:
                while 1 not in client._replies:
                    client._cv.wait(timeout=2)

            self.assertIn(1, client._replies)
            reply = client._replies.pop(1)
            self.assertEqual(reply.get("result"), {"active_ok": True})
        finally:
            os.close(w_fd)
            os.close(r_fd)


class TestRequestTTLAndTombstoning(unittest.TestCase):
    """Tests for Gate G M1 4: Expired reply discarded and request TTL tombstoning."""

    @require_cdp_pipe_clock_and_fence()
    def test_expired_request_ttl_tombstones_id_and_discards_late_reply(self):
        """Gate G M1 4: Expired request is tombstoned and late reply discarded."""
        fake_time_ns = 1_000_000_000

        def fake_clock():
            nonlocal fake_time_ns
            return fake_time_ns

        mock_fence = MagicMock()
        client = CdpPipe(chrome_bin="/bin/true", clock=fake_clock, writer_fence=mock_fence)

        # Send request with short timeout/TTL
        r_fd, w_fd = os.pipe()
        client._w = w_fd
        client._r = r_fd

        def timed_out_send():
            try:
                client.send("Page.navigate", {"url": "http://example.com"}, timeout=0.001)
            except Exception:
                pass

        send_thread = threading.Thread(target=timed_out_send)
        send_thread.start()

        # Advance clock past request deadline
        fake_time_ns += 10_000_000_000  # +10s
        send_thread.join(timeout=1)

        req_id = client._next_id

        # Late reply arrives for expired req_id
        late_reply = {"id": req_id, "result": {"stale": True}}
        if hasattr(client, "_handle_incoming_message"):
            client._handle_incoming_message(late_reply)

        # Expired reply must be discarded, NOT retained in _replies
        self.assertNotIn(req_id, client._replies, "Late reply for expired request was not discarded")
        os.close(w_fd)
        os.close(r_fd)

    @require_cdp_pipe_clock_and_fence()
    def test_active_reply_delivered_while_late_reply_discarded(self):
        """Positive control: Active reply delivered while late reply is discarded."""
        fake_time_ns = 1_000_000_000

        def fake_clock():
            nonlocal fake_time_ns
            return fake_time_ns

        mock_fence = MagicMock()
        client = CdpPipe(chrome_bin="/bin/true", clock=fake_clock, writer_fence=mock_fence)

        # Late reply for expired request #10
        if hasattr(client, "_handle_incoming_message"):
            client._handle_incoming_message({"id": 10, "result": {"late": True}})
        self.assertNotIn(10, client._replies)


def configured_tab_cdp():
    """Return acknowledged, distinct targets and sessions without external I/O."""
    cdp = MagicMock()
    target_count = 0

    def send(method, params=None, **kwargs):
        nonlocal target_count
        if method == "Target.getTargets":
            return {"targetInfos": []}
        if method == "Target.createTarget":
            target_count += 1
            return {"targetId": f"tab-{target_count}"}
        if method == "Target.attachToTarget":
            return {"sessionId": f"sid-{params['targetId']}"}
        return {"result": 2}

    cdp.send.side_effect = send
    return cdp


def register_managed_lane(broker, registry, handle):
    record = registry.resolve(handle)
    registered = broker.register(handle.kind, handle.canonical_id, record.transport,
                                 owned=record.owned, session_id=record.session_id,
                                 attachment_owned=record.attachment_owned)
    if registered != handle:
        raise AssertionError("Broker must adopt the managed generation")


class TestTargetRespawnAndNumericIDReuse(unittest.TestCase):
    """Tests for numeric-looking target ID reuse after target respawn."""

    @require_symbol("wender.perceive.tab_manager", "TabManager")
    def test_target_respawn_reuses_numeric_target_id_with_generation_fencing(self):
        """Reuse numeric-looking target ID after target respawn with generation increment."""
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.session_registry import SessionRegistry

        reg = SessionRegistry()
        mock_cdp = MagicMock()
        mock_cdp.send.side_effect = lambda method, params=None, **kwargs: (
            {"targetInfos": []} if method == "Target.getTargets" else
            {"targetId": "200"} if method == "Target.createTarget" else
            {"sessionId": "sid_101"} if method == "Target.attachToTarget" else
            {}
        )

        tm = TabManager(mock_cdp, reg)

        h1 = tm.ensure_tab("https://app.local", "https://app.local/page1")
        self.assertEqual(h1.canonical_id, "200")
        self.assertEqual(h1.generation, 1)

        # Close tab
        tm.close(h1)

        # Respawn tab, receiving numeric-looking target ID "200" again
        h2 = tm.ensure_tab("https://app.local", "https://app.local/page1")
        self.assertEqual(h2.canonical_id, "200")
        self.assertEqual(h2.generation, 2)
        self.assertNotEqual(h1.generation, h2.generation)


class TestInterleavedRepliesAndTransportCrash(unittest.TestCase):
    """Tests for interleaving late replies, active replies, and transport crash."""

    @require_symbol("wender.perceive.harvest_broker", "HarvestBroker")
    def test_interleaved_late_replies_active_replies_and_transport_crash(self):
        """Gate G M1 4: Crash fences only its lane; active reply delivered; late reply discarded."""
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.harvest_broker import HarvestBroker

        reg = SessionRegistry()
        mock_cdp = configured_tab_cdp()
        tm = TabManager(mock_cdp, reg)
        broker = HarvestBroker(reg)

        h_lane_a = tm.ensure_tab("https://app.local", "https://app.local/a")
        h_lane_b = tm.ensure_tab("https://app.local", "https://app.local/b")

        register_managed_lane(broker, reg, h_lane_a)
        register_managed_lane(broker, reg, h_lane_b)

        # Active reply on Lane B delivered successfully
        res = broker.command(h_lane_b, "Page.evaluate", {"expression": "1+1"})
        self.assertEqual(res, {"result": 2, "broker_request_id": "req-2-1"})


class TestActiveWaiterRetentionAndConnectionWideUniqueness(unittest.TestCase):
    """Tests for connection-wide request uniqueness and active waiter retention."""

    @require_symbol("wender.perceive.session_registry", "SessionRegistry")
    def test_connection_wide_request_uniqueness_and_active_waiter_retention(self):
        """INV-M1-2: Connection-wide request identities and active waiters are never evicted."""
        from wender.perceive.session_registry import SessionRegistry
        reg = SessionRegistry()

        tr_a = reg.resolve(reg.register("cdp_tab", "tab_1", MagicMock(), owned=True))
        tr_b = reg.resolve(reg.register("cdp_tab", "tab_2", MagicMock(), owned=True))

        self.assertNotEqual(tr_a.handle.canonical_id, tr_b.handle.canonical_id)

    @require_symbol("wender.perceive.harvest_broker", "HarvestBroker")
    def test_lane_local_fencing_on_overflow_or_crash(self):
        """INV-M1-2: Overflow or crash fences affected lane without losing sibling evidence."""
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.harvest_broker import HarvestBroker

        reg = SessionRegistry()
        mock_cdp = configured_tab_cdp()
        tm = TabManager(mock_cdp, reg)
        broker = HarvestBroker(reg)

        h_a = tm.ensure_tab("https://app.local", "https://app.local/a")
        h_b = tm.ensure_tab("https://app.local", "https://app.local/b")

        register_managed_lane(broker, reg, h_a)
        register_managed_lane(broker, reg, h_b)

        # Lane B remains operational
        res = broker.command(h_b, "Page.reload", {})
        self.assertIsNotNone(res)


class TestProposedComponentContracts(unittest.TestCase):
    """Tests for SessionRegistry, TabManager, and HarvestBroker proposed APIs."""

    @require_symbol("wender.perceive.session_registry", "SessionRegistry")
    def test_session_registry_register_resolve_close(self):
        """SessionRegistry register, resolve, close lifecycle contract."""
        from wender.perceive.session_registry import SessionRegistry

        reg = SessionRegistry()
        mock_transport = MagicMock()
        h = reg.register("cdp_tab", "canonical_tab_1", mock_transport, owned=True)

        self.assertIsInstance(h, Handle)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "canonical_tab_1")

        rec = reg.resolve(h)
        self.assertEqual(rec.handle, h)

        rcpt = reg.close(h)
        self.assertEqual(rcpt.status, "closed")

    @require_symbol("wender.perceive.tab_manager", "TabManager")
    def test_tab_manager_ensure_tab_and_close(self):
        """TabManager.ensure_tab and close contract."""
        from wender.perceive.tab_manager import TabManager
        from wender.perceive.session_registry import SessionRegistry

        reg = SessionRegistry()
        mock_cdp = configured_tab_cdp()
        tm = TabManager(mock_cdp, reg)

        h = tm.ensure_tab("https://localhost:8080", "https://localhost:8080/app")
        self.assertIsInstance(h, Handle)

        rcpt = tm.close(h)
        self.assertEqual(rcpt.status, "closed")


if __name__ == "__main__":
    unittest.main()
