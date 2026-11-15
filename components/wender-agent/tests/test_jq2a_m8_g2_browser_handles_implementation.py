"""Production unit tests for browser helper integration with explicit session handles.

Task ID: jq2a-m8-g2-browser-handles
Gate G M8 7: Two explicit handles isolate DOM, AX queries and finally cleanup;
reject shared cursor and perception focus claim; positive control legacy and sibling fake CDP calls.
INV-M8-1: Each helper binds explicit handle and resolved session through registry,
preserving per-tab AX cleanup.
"""

from dataclasses import fields
import hashlib
import json
import threading
import time
import unittest
from unittest import mock
import numpy as np

from wender.perceive import browser
from wender.perceive.browser import (
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
    SUPPORTED_TARGET_KINDS,
    HandleError,
    StaleGenerationError,
    EpochChangedError,
    SessionDisagreementError,
    AmbiguousDiscoveryError,
)


class FakeMultiSessionCdp:
    """Offline fake CDP transport tracking per-session method calls and preventing forbidden I/O."""

    FORBIDDEN_METHODS = frozenset({
        "Page.bringToFront",
        "Target.activateTarget",
        "DOM.focus",
        "Input.dispatchKeyEvent",
        "Input.dispatchMouseEvent",
        "Input.insertText",
    })

    def __init__(self, failure_session=None, failure_method=None, failure_error=None):
        self.sent_calls = []  # list of (method, session_id, params)
        self.failure_session = failure_session
        self.failure_method = failure_method
        self.failure_error = failure_error or RuntimeError("Simulated CDP error")
        self._lock = threading.Lock()

    def send(self, method, params=None, session_id=None):
        with self._lock:
            if method in self.FORBIDDEN_METHODS:
                raise AssertionError(f"Perception must never invoke focus or input method: {method}")
            self.sent_calls.append((method, session_id, params or {}))
            if session_id == self.failure_session and method == self.failure_method:
                raise self.failure_error

            if method == "Accessibility.enable":
                return {}
            elif method == "Accessibility.getFullAXTree":
                return {
                    "nodes": [
                        {
                            "nodeId": f"node-{session_id}-1",
                            "role": {"type": "role", "value": "RootWebArea"},
                            "name": {"type": "name", "value": f"Tab Title {session_id}"},
                            "backendDOMNodeId": 101,
                            "childIds": [],
                        }
                    ]
                }
            elif method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            elif method == "DOM.getBoxModel":
                return {"model": {"border": [10, 20, 110, 20, 110, 70, 10, 70]}}
            elif method == "Accessibility.disable":
                return {}
            return {}

    def evaluate(self, session_id, expression, timeout=None):
        with self._lock:
            self.sent_calls.append(("Runtime.evaluate", session_id, {"expr": expression}))
            if session_id == self.failure_session and self.failure_method == "Runtime.evaluate":
                raise self.failure_error

            if "location.href" in expression:
                return json.dumps([f"https://example.com/{session_id}", f"Title {session_id}"])
            elif "wenderViewport" in expression:
                return json.dumps({
                    "w": 1280,
                    "h": 800,
                    "scroll_x": 0,
                    "scroll_y": 0,
                    "doc_w": 1280,
                    "doc_h": 800,
                    "dpr_ppm": 1000000,
                })
            return json.dumps(None)


class FakeSessionRegistry:
    """Mock SessionRegistry resolving explicit Handle to TargetRecord."""

    def __init__(self, records=None, tab_manager=None, ambiguous=False):
        self.records = dict(records or {})
        self.tab_manager = tab_manager
        self.ambiguous = ambiguous

    def resolve(self, handle):
        if self.ambiguous:
            raise AmbiguousDiscoveryError("Registry reports ambiguous handle discovery")
        if handle in self.records:
            return self.records[handle]
        # Match by (kind, canonical_id) to detect stale generations
        for reg_handle, rec in self.records.items():
            if reg_handle.kind == handle.kind and reg_handle.canonical_id == handle.canonical_id:
                return rec
        return None


class FakeTabManager:
    """Mock TabManager mapping handles to session IDs."""

    def __init__(self, session_mapping=None):
        self.session_mapping = dict(session_mapping or {})

    def session_for_handle(self, handle):
        return self.session_mapping.get(handle)


class TestBrowserHandlesGateGM87(unittest.TestCase):
    """Gate G M8 7: Two explicit handles isolate DOM, AX queries and finally cleanup."""

    def setUp(self):
        self.cdp = FakeMultiSessionCdp()
        self.handle_a = Handle(kind="cdp_tab", canonical_id="tab-A", generation=1)
        self.handle_b = Handle(kind="cdp_tab", canonical_id="tab-B", generation=1)

        self.record_a = TargetRecord(
            handle=self.handle_a,
            transport=self.cdp,
            owned=True,
            epoch=10,
            session_id="session-tab-A",
            attachment_owned=True,
            closed=False,
        )
        self.record_b = TargetRecord(
            handle=self.handle_b,
            transport=self.cdp,
            owned=True,
            epoch=20,
            session_id="session-tab-B",
            attachment_owned=True,
            closed=False,
        )

        self.tab_mgr = FakeTabManager({
            self.handle_a: "session-tab-A",
            self.handle_b: "session-tab-B",
        })
        self.registry = FakeSessionRegistry(
            records={self.handle_a: self.record_a, self.handle_b: self.record_b},
            tab_manager=self.tab_mgr,
        )

    def test_two_explicit_handles_isolate_dom_ax_and_finally_cleanup(self):
        """Two explicit handles strictly isolate all DOM and AX operations and per-tab cleanup."""
        state_a = browser.page_state(self.cdp, handle=self.handle_a, registry=self.registry)
        self.assertEqual(state_a["source"]["kind"], "cdp")
        self.assertIn("https://example.com/session-tab-A", state_a["source"]["url"])

        state_b = browser.page_state(self.cdp, handle=self.handle_b, registry=self.registry)
        self.assertEqual(state_b["source"]["kind"], "cdp")
        self.assertIn("https://example.com/session-tab-B", state_b["source"]["url"])

        # Check all calls for tab A
        calls_a = [c for c in self.cdp.sent_calls if c[1] == "session-tab-A"]
        methods_a = [c[0] for c in calls_a]
        self.assertIn("Accessibility.enable", methods_a)
        self.assertIn("Accessibility.getFullAXTree", methods_a)
        self.assertIn("Runtime.evaluate", methods_a)
        self.assertIn("DOM.getDocument", methods_a)
        self.assertIn("DOM.getBoxModel", methods_a)
        self.assertIn("Accessibility.disable", methods_a)

        # Check all calls for tab B
        calls_b = [c for c in self.cdp.sent_calls if c[1] == "session-tab-B"]
        methods_b = [c[0] for c in calls_b]
        self.assertIn("Accessibility.enable", methods_b)
        self.assertIn("Accessibility.getFullAXTree", methods_b)
        self.assertIn("Runtime.evaluate", methods_b)
        self.assertIn("DOM.getDocument", methods_b)
        self.assertIn("DOM.getBoxModel", methods_b)
        self.assertIn("Accessibility.disable", methods_b)

        # Assert absolute isolation: no call used None or cross session
        all_sessions = {c[1] for c in self.cdp.sent_calls}
        self.assertEqual(all_sessions, {"session-tab-A", "session-tab-B"})

        # Each tab has its own AX release
        self.assertEqual(methods_a.count("Accessibility.disable"), 1)
        self.assertEqual(methods_b.count("Accessibility.disable"), 1)

    def test_finally_cleanup_on_construction_failure(self):
        """Per-tab AX release runs in finally even when perception construction fails."""
        cdp_failing = FakeMultiSessionCdp(
            failure_session="session-tab-A",
            failure_method="Runtime.evaluate",
            failure_error=RuntimeError("Evaluation failed on tab A"),
        )
        reg = FakeSessionRegistry({
            self.handle_a: TargetRecord(
                handle=self.handle_a,
                transport=cdp_failing,
                owned=True,
                epoch=10,
                session_id="session-tab-A",
                attachment_owned=True,
                closed=False,
            )
        })

        with self.assertRaises(RuntimeError) as ctx:
            browser.page_state(cdp_failing, handle=self.handle_a, registry=reg)
        self.assertIn("Evaluation failed on tab A", str(ctx.exception))

        # Despite the failure, Accessibility.disable was sent to session-tab-A
        cleanup_calls = [
            c for c in cdp_failing.sent_calls
            if c[0] == "Accessibility.disable" and c[1] == "session-tab-A"
        ]
        self.assertEqual(len(cleanup_calls), 1)

    def test_interleaved_sibling_isolation_concurrent(self):
        """Concurrent interleaved calls for sibling handles maintain complete isolation."""
        barrier = threading.Barrier(3)
        results = {}

        def runner(worker_id, handle):
            barrier.wait(timeout=3.0)
            res = browser.page_state(self.cdp, handle=handle, registry=self.registry)
            results[worker_id] = res

        t1 = threading.Thread(target=runner, args=("t_a", self.handle_a))
        t2 = threading.Thread(target=runner, args=("t_b", self.handle_b))
        t1.start()
        t2.start()
        barrier.wait(timeout=3.0)
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        self.assertIn("t_a", results)
        self.assertIn("t_b", results)
        self.assertIn("session-tab-A", results["t_a"]["source"]["url"])
        self.assertIn("session-tab-B", results["t_b"]["source"]["url"])

        # Sibling isolation: exactly one disable call per session
        disable_a = [c for c in self.cdp.sent_calls if c[0] == "Accessibility.disable" and c[1] == "session-tab-A"]
        disable_b = [c for c in self.cdp.sent_calls if c[0] == "Accessibility.disable" and c[1] == "session-tab-B"]
        self.assertEqual(len(disable_a), 1)
        self.assertEqual(len(disable_b), 1)

    def test_boxes_helper_explicit_handle_isolation(self):
        """boxes() helper with explicit handle resolves session and maintains isolation."""
        nodes_a = [{"id": 1, "backend_id": 101}]
        nodes_b = [{"id": 2, "backend_id": 102}]

        bx_a = browser.boxes(self.cdp, nodes=nodes_a, handle=self.handle_a, registry=self.registry)
        bx_b = browser.boxes(self.cdp, nodes=nodes_b, handle=self.handle_b, registry=self.registry)

        self.assertIn(1, bx_a)
        self.assertIn(2, bx_b)

        calls_a = [c for c in self.cdp.sent_calls if c[1] == "session-tab-A"]
        calls_b = [c for c in self.cdp.sent_calls if c[1] == "session-tab-B"]
        self.assertTrue(all(c[0] in ("DOM.getDocument", "DOM.getBoxModel") for c in calls_a))
        self.assertTrue(all(c[0] in ("DOM.getDocument", "DOM.getBoxModel") for c in calls_b))


class TestNamedMutantDistinctions(unittest.TestCase):
    """Mutant distinctions: reject shared session cursor and claimed focus mutants."""

    def test_reject_shared_session_cursor_mutant(self):
        """Negative mutant test: reject shared session cursor across distinct handles."""
        cdp = FakeMultiSessionCdp()
        handle_1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        handle_2 = Handle(kind="cdp_tab", canonical_id="tab-2", generation=1)

        # Flawed registry or cursor that reuses session-tab-1 for handle_2
        # (simulating shared cursor mutant across targets)
        shared_cursor_registry = FakeSessionRegistry({
            handle_1: TargetRecord(handle_1, cdp, True, 1, "session-tab-1", True, False),
            handle_2: TargetRecord(handle_2, cdp, True, 1, "session-tab-1", True, False),
        })

        # When caller specifies the distinct expected session, disagreement is rejected
        with self.assertRaises(SessionDisagreementError):
            browser.page_state(cdp, session_id="session-tab-2", handle=handle_2, registry=shared_cursor_registry)

    def test_reject_claimed_focus_mutant(self):
        """Negative mutant test: perception claims focus or requests FocusLease."""
        # 1. Perception helper must NEVER issue focus commands to CDP transport
        cdp = FakeMultiSessionCdp()
        handle = Handle(kind="cdp_tab", canonical_id="tab-focus-check", generation=1)
        registry = FakeSessionRegistry({
            handle: TargetRecord(handle, cdp, True, 1, "session-f", True, False)
        })

        browser.page_state(cdp, handle=handle, registry=registry)

        # Verify no focus or input methods were called
        for method, session_id, _ in cdp.sent_calls:
            self.assertNotIn(method, FakeMultiSessionCdp.FORBIDDEN_METHODS)

        # 2. Mutant simulation: if an agent attempted to call a focus command during perception,
        # transport forbids it
        with self.assertRaises(AssertionError):
            cdp.send("Page.bringToFront", session_id="session-f")
        with self.assertRaises(AssertionError):
            cdp.send("DOM.focus", {"nodeId": 1}, session_id="session-f")


class TestHandleValidationAndContractEdgeCases(unittest.TestCase):
    """Validation of generations, epochs, disagreements, and target kinds."""

    def setUp(self):
        self.cdp = FakeMultiSessionCdp()
        self.handle = Handle(kind="cdp_tab", canonical_id="tab-valid", generation=2)
        self.record = TargetRecord(
            handle=self.handle,
            transport=self.cdp,
            owned=True,
            epoch=5,
            session_id="sess-valid",
            attachment_owned=True,
            closed=False,
        )
        self.registry = FakeSessionRegistry({self.handle: self.record})

    def test_refuse_stale_generation(self):
        """Reject stale handle generation before and after perception."""
        # Handle with generation 1 when registry has generation 2
        stale_handle = Handle(kind="cdp_tab", canonical_id="tab-valid", generation=1)
        with self.assertRaises(StaleGenerationError):
            browser.page_state(self.cdp, handle=stale_handle, registry=self.registry)
        with self.assertRaises(StaleGenerationError):
            browser.boxes(self.cdp, nodes=(), handle=stale_handle, registry=self.registry)

    def test_refuse_changed_epoch_before_returning(self):
        """Reject changed epoch occurring before helper returns."""
        # Registry that mutates epoch during execution
        class MutatingEpochRegistry:
            def __init__(self, record):
                self.record = record
                self.call_count = 0

            def resolve(self, handle):
                self.call_count += 1
                # On initial resolve epoch is 5; on subsequent check epoch is 6
                new_epoch = 5 if self.call_count == 1 else 6
                return TargetRecord(
                    handle=self.record.handle,
                    transport=self.record.transport,
                    owned=self.record.owned,
                    epoch=new_epoch,
                    session_id=self.record.session_id,
                    attachment_owned=self.record.attachment_owned,
                    closed=self.record.closed,
                )

        mutating_reg = MutatingEpochRegistry(self.record)
        with self.assertRaises(EpochChangedError):
            browser.page_state(self.cdp, handle=self.handle, registry=mutating_reg)

        mutating_reg_boxes = MutatingEpochRegistry(self.record)
        with self.assertRaises(EpochChangedError):
            browser.boxes(self.cdp, nodes=(), handle=self.handle, registry=mutating_reg_boxes)

    def test_refuse_disagreement_with_explicit_session_id(self):
        """Reject disagreement when both explicit session_id and handle are provided."""
        with self.assertRaises(SessionDisagreementError):
            browser.page_state(
                self.cdp,
                session_id="sess-different",
                handle=self.handle,
                registry=self.registry,
            )
        with self.assertRaises(SessionDisagreementError):
            browser.boxes(
                self.cdp,
                session_id="sess-different",
                nodes=(),
                handle=self.handle,
                registry=self.registry,
            )

    def test_agreement_with_explicit_session_id_succeeds(self):
        """Agreeing session_id and handle succeed without conflict."""
        state = browser.page_state(
            self.cdp,
            session_id="sess-valid",
            handle=self.handle,
            registry=self.registry,
        )
        self.assertEqual(state["source"]["kind"], "cdp")

        bx = browser.boxes(
            self.cdp,
            session_id="sess-valid",
            nodes=(),
            handle=self.handle,
            registry=self.registry,
        )
        self.assertEqual(bx, {})

    def test_refuse_unsupported_handle_kind(self):
        """Reject non-cdp_tab target kinds in browser perception."""
        for kind in ("godot_window", "x11_window", "xvfb_screen", "capsule"):
            h = Handle(kind=kind, canonical_id="non-browser", generation=1)
            with self.assertRaises(ValueError):
                browser.page_state(self.cdp, handle=h, registry=self.registry)
            with self.assertRaises(ValueError):
                browser.boxes(self.cdp, nodes=(), handle=h, registry=self.registry)

    def test_refuse_closed_target_record(self):
        """Reject perception on closed target records."""
        closed_rec = TargetRecord(
            handle=self.handle,
            transport=self.cdp,
            owned=True,
            epoch=5,
            session_id="sess-valid",
            attachment_owned=True,
            closed=True,
        )
        closed_reg = FakeSessionRegistry({self.handle: closed_rec})
        with self.assertRaises(ValueError):
            browser.page_state(self.cdp, handle=self.handle, registry=closed_reg)

    def test_refuse_ambiguous_discovery(self):
        """Reject ambiguous discovery from registry."""
        ambiguous_reg = FakeSessionRegistry(ambiguous=True)
        with self.assertRaises(AmbiguousDiscoveryError):
            browser.page_state(self.cdp, handle=self.handle, registry=ambiguous_reg)


class TestPositiveControlsLegacyCallers(unittest.TestCase):
    """Positive controls: existing legacy single-target callers remain intact."""

    def test_legacy_page_state_without_handle(self):
        """Legacy callers passing (cdp, session_id) execute normally."""
        cdp = FakeMultiSessionCdp()
        state = browser.page_state(cdp, "legacy-s1")
        self.assertEqual(state["source"]["kind"], "cdp")
        self.assertIn("https://example.com/legacy-s1", state["source"]["url"])
        self.assertIn("Accessibility.disable", [c[0] for c in cdp.sent_calls])

    def test_legacy_boxes_without_handle(self):
        """Legacy callers passing (cdp, session_id, nodes) execute normally."""
        cdp = FakeMultiSessionCdp()
        nodes = [{"id": "n1", "backend_id": 101}]
        bx = browser.boxes(cdp, "legacy-s1", nodes)
        self.assertIn("n1", bx)
        self.assertEqual(bx["n1"], [10, 20, 100, 50])


class TestCanonicalRecordsContract(unittest.TestCase):
    """Verify exact canonical records contract, field order, and immutability."""

    def test_canonical_record_types_and_fields(self):
        """All 15 shared records exist with exact field order and immutability."""
        handle = Handle(kind="cdp_tab", canonical_id="tab-canon", generation=1)
        self.assertEqual(
            [f.name for f in fields(Handle)],
            ["kind", "canonical_id", "generation"]
        )

        target_rec = TargetRecord(
            handle=handle,
            transport=None,
            owned=True,
            epoch=1,
            session_id="s1",
            attachment_owned=True,
            closed=False
        )
        self.assertEqual(
            [f.name for f in fields(TargetRecord)],
            ["handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed"]
        )

        rgb = np.zeros((48, 64, 3), dtype=np.uint8)
        frame_ref = FrameRef(
            handle=handle,
            epoch=1,
            frame_seq=1,
            width=64,
            height=48,
            rgb=rgb,
            digest=hashlib.sha256(rgb.tobytes()).hexdigest(),
        )
        self.assertEqual(
            [f.name for f in fields(FrameRef)],
            ["handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"]
        )

        obs = Observation(
            handle=handle,
            epoch=1,
            frame_seq=1,
            dpr_ppm=1000000,
            state={"ok": True},
            frame=frame_ref,
        )
        self.assertEqual(
            [f.name for f in fields(Observation)],
            ["handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"]
        )

        receipt = Receipt(action_id="act-1", handle=handle, status="ok", reason=None)
        self.assertEqual(
            [f.name for f in fields(Receipt)],
            ["action_id", "handle", "status", "reason"]
        )

        req = ActionRequest(
            handle=handle, action_id="a1", epoch=1, payload={}, cost=10, deadline_ns=2000000000
        )
        self.assertEqual(
            [f.name for f in fields(ActionRequest)],
            ["handle", "action_id", "epoch", "payload", "cost", "deadline_ns"]
        )

        lease = ActorLease(
            handle=handle, owner="worker-1", generation=1, nonce=b"nonce", expires_ns=1000
        )
        self.assertEqual(
            [f.name for f in fields(ActorLease)],
            ["handle", "owner", "generation", "nonce", "expires_ns"]
        )

        flease = FocusLease(lease=lease, domain="example.com", epoch=1)
        self.assertEqual(
            [f.name for f in fields(FocusLease)],
            ["lease", "domain", "epoch"]
        )

        spec = StreamSpec(width=1280, height=720, slots=2, quota_bytes=5529600)
        self.assertEqual(
            [f.name for f in fields(StreamSpec)],
            ["width", "height", "slots", "quota_bytes"]
        )

        token = StreamToken(handle=handle, epoch=1)
        self.assertEqual(
            [f.name for f in fields(StreamToken)],
            ["handle", "epoch"]
        )

        ticket = Ticket(action_id="t1", handle=handle)
        self.assertEqual(
            [f.name for f in fields(Ticket)],
            ["action_id", "handle"]
        )

        change = ChangeResult(handle=handle, epoch=1, settled=True, claims=np.array([1]), ops={})
        self.assertEqual(
            [f.name for f in fields(ChangeResult)],
            ["handle", "epoch", "settled", "claims", "ops"]
        )

        perc = PerceptionResult(handle=handle, observation=obs, error=None)
        self.assertEqual(
            [f.name for f in fields(PerceptionResult)],
            ["handle", "observation", "error"]
        )

        wframes = WindowFrames(
            handle=handle, epoch=1, frames=(frame_ref,), frame_seqs=(1,), dpr_ppm=1000000
        )
        self.assertEqual(
            [f.name for f in fields(WindowFrames)],
            ["handle", "epoch", "frames", "frame_seqs", "dpr_ppm"]
        )

        wdesc = WindowDescription(
            handle=handle,
            epoch=1,
            frame_seqs=(1,),
            descriptors=("d1",),
            regions=("r1",),
            error=None,
        )
        self.assertEqual(
            [f.name for f in fields(WindowDescription)],
            ["handle", "epoch", "frame_seqs", "descriptors", "regions", "error"]
        )

        # Immutability verification
        with self.assertRaises((AttributeError, TypeError)):
            handle.generation = 99


if __name__ == "__main__":
    unittest.main()
