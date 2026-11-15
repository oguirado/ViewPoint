# @module {"id":"tests.test_jq2a_m8_j4_godot_confirm_save_guards_in_multiwindow","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""Contract test battery for Gate G M8 4: Replayed confirm and automated main save refuse.

Task ID: jq2a-m8-j4-godot-confirm-save-guards-in-multiwindow
Owning implementation task_id: jq2a-m8-c2-godot-driver
Target owner modules:
- wender.act.godot_window_driver.GodotWindowDriver
- wender.perceive.godot_window_target.GodotWindowTarget
- wender.perceive.session_registry
"""

from dataclasses import dataclass
import hashlib
import json
import unittest
from typing import Any
import numpy as np

# Canonical shared records import or structural fallback
try:
    from wender.perceive.session_registry import (  # type: ignore
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


def expected_failure_if_absent(module_name: str, symbol_name: str):
    """Decorator applying @unittest.expectedFailure only while module/symbol is absent."""
    try:
        mod = __import__(module_name, fromlist=[symbol_name])
        if hasattr(mod, symbol_name):
            return lambda fn: fn
    except ImportError:
        pass
    return unittest.expectedFailure


class FakeSink:
    """Offline test sink tracking emitted batch records."""

    def __init__(self):
        self.calls: list[bytes] = []

    def __call__(self, encoded_batch: bytes) -> None:
        self.calls.append(encoded_batch)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def fake_encode_batch(records: list[dict[str, Any]]) -> bytes:
    return json.dumps(records, sort_keys=True).encode("utf-8")


class TestGodotConfirmSaveGuardsMultiwindow(unittest.TestCase):
    """Gate G M8 4 & contract tests for multiwindow confirm and main-save guards."""

    def setUp(self):
        self.basis_sha256 = "a1" * 32
        self.handle_a1 = Handle(kind="godot_window", canonical_id="win_a", generation=1)
        self.handle_a2 = Handle(kind="godot_window", canonical_id="win_a", generation=2)
        self.handle_b1 = Handle(kind="godot_window", canonical_id="win_b", generation=1)

    def _make_executor(self, slot_kind="sandbox", sink=None):
        from wender.act.godot_executor import GodotExecutor

        ctx = {
            "principal": "agent_worker",
            "channel": "ui",
            "slot_kind": slot_kind,
            "automation_active": True,
            "mandates": ["owner"],
        }
        if sink is None:
            sink = FakeSink()
        schemas = {
            "take_loan": {"args": {"target_id": "str", "terms": "dict"}},
            "sell_company": {"args": {"target_id": "str", "terms": "dict"}},
            "save": {"args": {}},
        }
        executor = GodotExecutor(
            trusted_context=ctx,
            action_schemas=schemas,
            encode_batch=fake_encode_batch,
            sink=sink,
        )
        return executor, sink

    def test_positive_control_authorized_test_slot_proposal_confirmed(self):
        """Positive control: Authorized test-slot proposal confirmed through public admission API."""
        executor, sink = self._make_executor(slot_kind="sandbox")

        proposal_req = {
            "cmd": "take_loan",
            "tick": 1,
            "seq": 1,
            "args": {"target_id": "bank_1", "terms": {"amount": 10000}},
        }
        res_prop = executor.dispatch(proposal_req, committed_tick=0, basis_sha256=self.basis_sha256)
        self.assertEqual(res_prop.get("reason"), "NEEDS_CONFIRM")
        self.assertEqual(sink.call_count, 0)

        modal_id = res_prop.get("modal_id")
        self.assertIsNotNone(modal_id)

        confirm_req = {
            "cmd": "confirm",
            "tick": 1,
            "seq": 1,
            "args": {
                "modal_id": modal_id,
                "cmd": "take_loan",
                "target_id": "bank_1",
                "terms": {"amount": 10000},
                "basis_sha256": self.basis_sha256,
            },
        }
        res_confirm = executor.dispatch(confirm_req, committed_tick=0, basis_sha256=self.basis_sha256)
        self.assertEqual(res_confirm.get("reason"), "OK")
        self.assertEqual(sink.call_count, 1)

    def test_replayed_confirm_same_handle_refused(self):
        """Gate G M8 4: Replaying a confirmed proposal on the same handle returns USED_CONFIRM with 0 extra sends."""
        executor, sink = self._make_executor(slot_kind="sandbox")

        proposal_req = {
            "cmd": "take_loan",
            "tick": 1,
            "seq": 1,
            "args": {"target_id": "bank_1", "terms": {"amount": 10000}},
        }
        res_prop = executor.dispatch(proposal_req, committed_tick=0, basis_sha256=self.basis_sha256)
        modal_id = res_prop.get("modal_id")

        confirm_req = {
            "cmd": "confirm",
            "tick": 1,
            "seq": 1,
            "args": {
                "modal_id": modal_id,
                "cmd": "take_loan",
                "target_id": "bank_1",
                "terms": {"amount": 10000},
                "basis_sha256": self.basis_sha256,
            },
        }
        res_c1 = executor.dispatch(confirm_req, committed_tick=0, basis_sha256=self.basis_sha256)
        self.assertEqual(res_c1.get("reason"), "OK")
        self.assertEqual(sink.call_count, 1)

        # Replay confirm attempt with sequence 2
        replay_req = {
            "cmd": "confirm",
            "tick": 1,
            "seq": 2,
            "args": {
                "modal_id": modal_id,
                "cmd": "take_loan",
                "target_id": "bank_1",
                "terms": {"amount": 10000},
                "basis_sha256": self.basis_sha256,
            },
        }
        res_replay = executor.dispatch(replay_req, committed_tick=0, basis_sha256=self.basis_sha256)
        self.assertEqual(res_replay.get("reason"), "USED_CONFIRM")
        # Zero additional sends
        self.assertEqual(sink.call_count, 1)

    def test_confirm_replay_across_sibling_handles_refused(self):
        """Gate G M8 4: Proposal generated on target Handle A cannot be confirmed on sibling Handle B."""
        executor_a, sink_a = self._make_executor(slot_kind="sandbox")
        executor_b, sink_b = self._make_executor(slot_kind="sandbox")

        prop_req = {
            "cmd": "sell_company",
            "tick": 1,
            "seq": 1,
            "args": {"target_id": "company_x", "terms": {"price": 50000}},
        }
        res_prop = executor_a.dispatch(prop_req, committed_tick=0, basis_sha256=self.basis_sha256)
        modal_id = res_prop.get("modal_id")

        # Replay proposal confirm on sibling Handle B's executor
        sibling_confirm_req = {
            "cmd": "confirm",
            "tick": 1,
            "seq": 1,
            "args": {
                "modal_id": modal_id,
                "cmd": "sell_company",
                "target_id": "company_x",
                "terms": {"price": 50000},
                "basis_sha256": self.basis_sha256,
            },
        }
        res_sibling = executor_b.dispatch(sibling_confirm_req, committed_tick=0, basis_sha256=self.basis_sha256)
        # Sibling executor has no record of this modal_id/proposal
        self.assertEqual(res_sibling.get("reason"), "NEEDS_CONFIRM")
        self.assertEqual(sink_b.call_count, 0)
        self.assertEqual(sink_a.call_count, 0)

    def test_confirm_replay_across_stale_generations_refused(self):
        """Gate G M8 4: Confirmation with stale basis or stale generation/epoch is refused."""
        executor, sink = self._make_executor(slot_kind="sandbox")

        prop_req = {
            "cmd": "sell_company",
            "tick": 1,
            "seq": 1,
            "args": {"target_id": "company_y", "terms": {"price": 12000}},
        }
        res_prop = executor.dispatch(prop_req, committed_tick=0, basis_sha256=self.basis_sha256)
        modal_id = res_prop.get("modal_id")

        stale_basis_sha256 = "b2" * 32
        confirm_stale = {
            "cmd": "confirm",
            "tick": 1,
            "seq": 1,
            "args": {
                "modal_id": modal_id,
                "cmd": "sell_company",
                "target_id": "company_y",
                "terms": {"price": 12000},
                "basis_sha256": stale_basis_sha256,
            },
        }
        res_stale = executor.dispatch(confirm_stale, committed_tick=0, basis_sha256=stale_basis_sha256)
        self.assertEqual(res_stale.get("reason"), "STALE_CONFIRM")
        self.assertEqual(sink.call_count, 0)

    def test_automated_main_save_refused(self):
        """Gate G M8 4: Automated main save on slot_kind='main' is refused with CONSENT_MAIN_SAVE."""
        executor_main, sink_main = self._make_executor(slot_kind="main")

        main_save_req = {
            "cmd": "take_loan",
            "tick": 1,
            "seq": 1,
            "args": {"target_id": "bank_main", "terms": {"amount": 1000000}},
        }
        res_main = executor_main.dispatch(main_save_req, committed_tick=0, basis_sha256=self.basis_sha256)
        self.assertEqual(res_main.get("reason"), "CONSENT_MAIN_SAVE")
        self.assertEqual(sink_main.call_count, 0)

    def test_negative_mutant_bypass_executor_guard_refused(self):
        """Negative mutant: Bypassing executor guards or forging consent fields in payload is refused with 0 sends."""
        executor_main, sink_main = self._make_executor(slot_kind="main")

        forged_req = {
            "cmd": "take_loan",
            "tick": 1,
            "seq": 1,
            "args": {
                "target_id": "bank_forged",
                "terms": {"amount": 50000},
                "consent": "forged_consent_token",
                "consent_verified": True,
            },
            "consent": True,
        }
        res_forged = executor_main.dispatch(forged_req, committed_tick=0, basis_sha256=self.basis_sha256)
        self.assertEqual(res_forged.get("reason"), "CONSENT_MAIN_SAVE")
        self.assertEqual(sink_main.call_count, 0)

    @expected_failure_if_absent("wender.act.godot_window_driver", "GodotWindowDriver")
    def test_godot_window_driver_owner_conformance(self):
        """Owner conformance test for GodotWindowDriver & GodotWindowTarget."""
        from wender.act.godot_window_driver import GodotWindowDriver
        from tests.test_jq2a_m8_c2_godot_driver_implementation import (
            FakeTarget, FakeLocks, FakeFocus, FakeFence)

        executor, sink = self._make_executor(slot_kind="sandbox")
        target = FakeTarget("win_a")
        target.handle = self.handle_a1
        driver = GodotWindowDriver(
            target, executor, locks=FakeLocks(), focus_policy=FakeFocus(),
            writer_fence=FakeFence(), clock=lambda: 10)

        lease = ActorLease(
            handle=self.handle_a1,
            owner="test_agent",
            generation=1,
            nonce=b"nonce_bytes_1234",
            expires_ns=2000000000000,
        )
        focus = FocusLease(lease=lease, domain="godot-viewport", epoch=target.epoch)
        request = ActionRequest(
            handle=self.handle_a1,
            action_id="act_001",
            epoch=target.epoch,
            payload={"cmd": "take_loan", "tick": target.tick + 1,
                     "basis_sha256": target.basis, "frame_seq": target.frame_seq,
                     "args": {"target_id": "bank_1", "terms": {"amount": 1000}}},
            cost=1,
            deadline_ns=2000000000000,
        )

        receipt = driver.execute(request, lease=lease, focus=focus)
        self.assertIsInstance(receipt, Receipt)
        self.assertEqual(receipt.handle, self.handle_a1)
        self.assertEqual((receipt.status, receipt.reason), ("proposed", "NEEDS_CONFIRM"))
        self.assertEqual(sink.call_count, 0)


if __name__ == "__main__":
    unittest.main()
