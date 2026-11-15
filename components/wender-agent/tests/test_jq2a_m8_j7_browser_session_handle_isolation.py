# @module {"id":"tests.test_jq2a_m8_j7_browser_session_handle_isolation","layer":3,"owner":"TESTS","deps":["wa.perceive.browser","wa.state.schema"]}
"""@module tests.test_jq2a_m8_j7_browser_session_handle_isolation - Gate G M8 7 browser session handle isolation contracts.

Gate G M8 7: Use two explicit tab handles on one fake CDP connection. Isolate DOM and AX
requests and ax.release in finally, refuse stale generations and changed epochs, and assert
no actor or focus lease during perception. Negative mutants share a session_id cursor or claim
focus; positive controls preserve concurrent sibling isolation and legacy cdp, session_id calls.
"""

import json
import hashlib
import unittest
from dataclasses import dataclass
from typing import Any
import numpy as np

from wender.perceive import browser, ax
from wender.state import schema


# -----------------------------------------------------------------------------
# Record Definitions & Canonical Fallbacks
# -----------------------------------------------------------------------------

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
    # Canonical frozen dataclasses fallback matching exact required field order
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


# Probe owner implementation availability (task jq2a-m8-g2-browser-handles)
def _is_owner_present() -> bool:
    import inspect
    try:
        from wender.perceive import session_registry
    except ImportError:
        return False
    sig = inspect.signature(browser.page_state)
    return "handle" in sig.parameters and "registry" in sig.parameters


OWNER_PRESENT = _is_owner_present()


def expected_if_owner_absent(func):
    """Apply @unittest.expectedFailure conditionally if the owner implementation is absent."""
    if not OWNER_PRESENT:
        return unittest.expectedFailure(func)
    return func


# -----------------------------------------------------------------------------
# Fake CDP & Fixtures
# -----------------------------------------------------------------------------

class FakeCdp:
    """Fake CDP transport tracking sessions and AX/DOM commands."""

    def __init__(self, fail_at_method=None):
        self.sent_calls = []  # list of (method, params, session_id)
        self.fail_at_method = fail_at_method
        self.released_sessions = []
        self.eval_counts = {}

    def send(self, method, params=None, session_id=None):
        self.sent_calls.append((method, params, session_id))
        if method == self.fail_at_method:
            raise RuntimeError(f"Simulated failure at {method}")

        if method == "Accessibility.enable":
            return {}
        elif method == "Accessibility.getFullAXTree":
            return {
                "nodes": [
                    {
                        "nodeId": f"root-{session_id}",
                        "role": {"type": "role", "value": "RootWebArea"},
                        "name": {"type": "name", "value": f"Title for {session_id}"},
                        "backendDOMNodeId": 100,
                        "childIds": [],
                    }
                ]
            }
        elif method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        elif method == "DOM.getBoxModel":
            return {"model": {"border": [0, 0, 100, 0, 100, 100, 0, 100]}}
        elif method == "Accessibility.disable":
            self.released_sessions.append(session_id)
            return {}
        return {}

    def evaluate(self, session_id, expression, timeout=None):
        self.sent_calls.append(("Runtime.evaluate", expression, session_id))
        if "location.href" in expression:
            return json.dumps([f"https://example.com/{session_id}", f"Title for {session_id}"])
        elif "wenderViewport" in expression:
            return json.dumps({"w": 1280, "h": 800, "dpr_ppm": 100})
        return json.dumps(None)


class FakeSessionRegistry:
    """Fake session registry tracking handles, records, actor leases and focus leases."""

    def __init__(self, target_records=None):
        self.records = target_records or {}
        self.active_actor_leases = []
        self.active_focus_leases = []

    def resolve(self, handle: Handle) -> TargetRecord | None:
        record = self.get_target(handle)
        if record is not None and record.handle.generation != handle.generation:
            raise ValueError("stale handle generation")
        return record

    def lookup(self, handle: Handle) -> TargetRecord | None:
        return self.records.get(handle.canonical_id)

    def get_target(self, handle: Handle) -> TargetRecord | None:
        rec = self.records.get(handle.canonical_id)
        if rec is None:
            return None
        if rec.handle.generation != handle.generation or rec.epoch != rec.handle.generation:
            # generation or epoch mismatch
            pass
        return rec


class SharedCursorMutantCdp(FakeCdp):
    """Mutant CDP transport that bleeds session_id cursor across calls."""

    def __init__(self, shared_session_id="SHARED_CURSOR"):
        super().__init__()
        self.shared_session_id = shared_session_id

    def send(self, method, params=None, session_id=None):
        # Bleed shared session_id cursor
        return super().send(method, params=params, session_id=self.shared_session_id)


class ClaimedFocusMutantRegistry(FakeSessionRegistry):
    """Mutant registry that claims a focus lease during perception."""

    def get_target(self, handle: Handle) -> TargetRecord | None:
        rec = super().get_target(handle)
        if rec is not None:
            # Claim an unauthorized focus lease
            lease = ActorLease(handle=handle, owner="mutant_actor", generation=handle.generation, nonce=b"1234", expires_ns=999999)
            self.active_focus_leases.append(FocusLease(lease=lease, domain="web", epoch=rec.epoch))
        return rec


# -----------------------------------------------------------------------------
# Test Suite
# -----------------------------------------------------------------------------

class TestBrowserSessionHandleIsolation(unittest.TestCase):
    """Gate G M8 7: Browser session handle isolation and clean cleanup contracts."""

    def setUp(self):
        self.cdp = FakeCdp()
        self.handle_a = Handle(kind="cdp_tab", canonical_id="tab_A", generation=1)
        self.handle_b = Handle(kind="cdp_tab", canonical_id="tab_B", generation=1)

        self.rec_a = TargetRecord(
            handle=self.handle_a,
            transport=self.cdp,
            owned=True,
            epoch=1,
            session_id="sess_A",
            attachment_owned=True,
            closed=False,
        )
        self.rec_b = TargetRecord(
            handle=self.handle_b,
            transport=self.cdp,
            owned=True,
            epoch=1,
            session_id="sess_B",
            attachment_owned=True,
            closed=False,
        )

        self.registry = FakeSessionRegistry({
            "tab_A": self.rec_a,
            "tab_B": self.rec_b,
        })

    # --- Positive Controls (Legacy & Dataclasses) ---

    def test_legacy_page_state_preserves_positional_session_id(self):
        """Positive control: legacy page_state(cdp, session_id) returns valid state and releases AX."""
        st = browser.page_state(self.cdp, "sess_A")
        self.assertEqual(st["source"]["kind"], "cdp")
        self.assertIn("sess_A", self.cdp.released_sessions)
        self.assertEqual(schema.validate_state(st), [])

    def test_legacy_boxes_preserves_positional_session_id(self):
        """Positive control: legacy boxes(cdp, session_id, nodes) functions correctly."""
        nodes = [{"id": "n1", "backend_id": 100}]
        bx = browser.boxes(self.cdp, "sess_A", nodes)
        self.assertIn("n1", bx)
        self.assertEqual(bx["n1"], [0, 0, 100, 100])

    def test_record_dataclass_field_order_and_immutability(self):
        """Positive control: verify canonical dataclass field orders and immutability."""
        h = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        self.assertEqual((h.kind, h.canonical_id, h.generation), ("cdp_tab", "tab_1", 1))
        with self.assertRaises((AttributeError, TypeError)):
            h.generation = 2  # frozen

        fr = FrameRef.from_rgb(h, 1, 0, np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertEqual(fr.digest, hashlib.sha256(bytes(640 * 480 * 3)).hexdigest())

        obs = Observation(handle=h, epoch=1, frame_seq=0, dpr_ppm=100, state={}, frame=fr)
        self.assertEqual(obs.dpr_ppm, 100)

    # --- Landed Build Task Contracts (@expected_if_owner_absent) ---

    @expected_if_owner_absent
    def test_explicit_handles_isolate_sibling_tab_dom_and_ax_requests(self):
        """Explicit handles on one CDP connection isolate requests between tab A and tab B."""
        st_a = browser.page_state(self.cdp, None, handle=self.handle_a, registry=self.registry)
        st_b = browser.page_state(self.cdp, None, handle=self.handle_b, registry=self.registry)

        self.assertEqual(st_a["source"]["url"], "https://example.com/sess_A")
        self.assertEqual(st_b["source"]["url"], "https://example.com/sess_B")

        # Confirm all CDP calls for handle A used sess_A and handle B used sess_B
        calls_a = [c for c in self.cdp.sent_calls if c[2] == "sess_A"]
        calls_b = [c for c in self.cdp.sent_calls if c[2] == "sess_B"]

        self.assertTrue(len(calls_a) > 0)
        self.assertTrue(len(calls_b) > 0)
        self.assertEqual(len(calls_a) + len(calls_b), len(self.cdp.sent_calls))

    @expected_if_owner_absent
    def test_per_tab_ax_release_in_finally_on_success_and_failure(self):
        """Per-tab ax.release runs in finally block even if DOM or evaluate raises during perception."""
        fail_cdp = FakeCdp(fail_at_method="DOM.getDocument")
        fail_rec = TargetRecord(
            handle=self.handle_a,
            transport=fail_cdp,
            owned=True,
            epoch=1,
            session_id="sess_A",
            attachment_owned=True,
            closed=False,
        )
        reg = FakeSessionRegistry({"tab_A": fail_rec})

        with self.assertRaises(RuntimeError):
            browser.page_state(fail_cdp, None, handle=self.handle_a, registry=reg)

        # ax.release (Accessibility.disable) must still have been called for sess_A
        self.assertIn("sess_A", fail_cdp.released_sessions)

    @expected_if_owner_absent
    def test_refuse_stale_generations_and_changed_epochs(self):
        """Refuse perception when handle generation is stale or target epoch has changed."""
        stale_handle = Handle(kind="cdp_tab", canonical_id="tab_A", generation=1)
        current_handle = Handle(kind="cdp_tab", canonical_id="tab_A", generation=2)
        current_record = TargetRecord(
            handle=current_handle, transport=self.cdp, owned=True, epoch=2,
            session_id="sess_A", attachment_owned=True, closed=False,
        )
        stale_registry = FakeSessionRegistry({"tab_A": current_record})
        with self.assertRaises(ValueError):
            browser.page_state(self.cdp, None, handle=stale_handle, registry=stale_registry)

        # Target record changes epoch while perception is in flight
        rec_epoch_changed = TargetRecord(
            handle=self.handle_a,
            transport=self.cdp,
            owned=True,
            epoch=2,  # handle generation is 1
            session_id="sess_A",
            attachment_owned=True,
            closed=False,
        )
        class ChangingEpochRegistry(FakeSessionRegistry):
            def resolve(inner, handle):
                record = super().resolve(handle)
                inner.records["tab_A"] = rec_epoch_changed
                return record

        reg = ChangingEpochRegistry({"tab_A": self.rec_a})
        with self.assertRaises(ValueError):
            browser.page_state(self.cdp, None, handle=self.handle_a, registry=reg)

    @expected_if_owner_absent
    def test_reject_disagreement_between_explicit_session_id_and_handle(self):
        """Reject explicit session_id if it disagrees with resolved target handle mapping."""
        with self.assertRaises(ValueError):
            # Explicit session_id="sess_B" disagrees with tab_A's mapped "sess_A"
            browser.page_state(self.cdp, "sess_B", handle=self.handle_a, registry=self.registry)

    @expected_if_owner_absent
    def test_perception_holds_no_actor_or_focus_lease(self):
        """Perception operations must take no actor lease and no focus lease."""
        browser.page_state(self.cdp, None, handle=self.handle_a, registry=self.registry)
        self.assertEqual(len(self.registry.active_actor_leases), 0)
        self.assertEqual(len(self.registry.active_focus_leases), 0)

    # --- Negative Mutant Rejection Controls ---

    @expected_if_owner_absent
    def test_negative_mutant_shared_session_cursor_rejection(self):
        """Negative mutant sharing a session_id cursor across sibling handles must raise ValueError or RuntimeError."""
        rec_shared_a = TargetRecord(handle=self.handle_a, transport=self.cdp, owned=True, epoch=1, session_id="sess_shared", attachment_owned=True, closed=False)
        rec_shared_b = TargetRecord(handle=self.handle_b, transport=self.cdp, owned=True, epoch=1, session_id="sess_shared", attachment_owned=True, closed=False)
        reg = FakeSessionRegistry({"tab_A": rec_shared_a, "tab_B": rec_shared_b})

        browser.page_state(self.cdp, None, handle=self.handle_a, registry=reg)
        with self.assertRaises((ValueError, RuntimeError)):
            browser.page_state(self.cdp, None, handle=self.handle_b, registry=reg)

    @expected_if_owner_absent
    def test_negative_mutant_claimed_focus_lease_rejection(self):
        """Negative mutant claiming a focus lease during perception must raise ValueError or RuntimeError."""
        mutant_reg = ClaimedFocusMutantRegistry({
            "tab_A": self.rec_a,
        })
        with self.assertRaises((ValueError, RuntimeError)):
            browser.page_state(self.cdp, None, handle=self.handle_a, registry=mutant_reg)


if __name__ == "__main__":
    unittest.main()
