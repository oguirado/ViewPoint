"""Acceptance suite G-E2-1 for jq3-e-e2-g1-electron-cdp-input-dispatcher.

Covers:
- Closed brokered IPC T2 batch client (wender.act.jq3_t2_broker_client):
  INV-E2-1 (membership completion before admission),
  INV-E2-2 (no malformed-batch effects; per-producer sequence checked before deterministic sorting),
  INV-E2-3 (valid predecessors preserved on structurally valid domain rejection; trusted attempt bookkeeping advances),
  INV-E2-4 (stale/retired port_epoch fenced on submit_batch and reconcile),
  and uncertain commit + lost acknowledgment reconciliation without a new sequence.
- Named catalogue mutants killed through real production paths:
  M-wender-act-cdp-executor-1 and M-wender-act-cdp-executor-2.
- Seeded random.Random property loop with TARGET 64 cases and independent oracle.
- Concurrency suite with TARGET 32 recorded logical schedules, <= 4 threads,
  2s timeout per barrier and join, barrier cleanup, and zero leaked threads.
- Offline only (fake CDP, fake broker, fake clocks, numpy frames in RAM) within a 15s VM budget.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
import hashlib
import inspect
import random
import sys
import threading
import time
import types
from types import SimpleNamespace
import unittest

import numpy as np

import wender.act.cdp_executor as cdp_executor_mod
from wender.act.cdp_executor import ActionError, run_ax_action
from wender.act.focus_policy import (
    ActorLease,
    FocusPolicy,
    FocusRefused,
    Handle,
    Observation,
)
import wender.act.jq3_t2_broker_client as broker_client_mod
from wender.act.jq3_t2_broker_client import (
    BatchMembershipIncomplete,
    BrokerClientError,
    ClosedCommandRegistry,
    CommandSpec,
    FakeT2Broker,
    MalformedBatchError,
    PrerequisiteAbsent as BrokerPrerequisiteAbsent,
    RECORD_KEYS,
    StalePortEpochError,
    T2BrokerClient,
    bind_default_client,
    reconcile,
    submit_batch,
)
from wender.act.target_lock_registry import (
    PrerequisiteAbsent as LockPrerequisiteAbsent,
    TargetLockRegistry,
)

VM_BUDGET_SECONDS = 15.0
PROPERTY_TARGET_CASES = 64
PROPERTY_SEED = 0x50B9FBB4
CONCURRENCY_TARGET_SCHEDULES = 32
MAX_CONCURRENCY_THREADS = 4
SYNC_TIMEOUT_S = 2.0


class FakeClock:
    """Deterministic monotonic nanosecond clock in RAM."""

    def __init__(self, initial_ns: int = 1_000_000) -> None:
        self.now_ns = int(initial_ns)

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, delta_ns: int) -> int:
        self.now_ns += int(delta_ns)
        return self.now_ns


@dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    epoch: int = 2
    session_id: str = "sid-e2"
    closed: bool = False
    actuation_resource: str = "tab-main"


class FakeSessionRegistry:
    """Explicit authority provider supporting both get() and resolve()."""

    def __init__(self, records: list[TargetRecord]) -> None:
        self.records = {r.handle.canonical_id: r for r in records}
        self.current_obs: dict[str, Observation] = {}

    def get(self, handle: Handle) -> TargetRecord | None:
        return self.records.get(getattr(handle, "canonical_id", None))

    def resolve(self, handle: Handle) -> TargetRecord | None:
        return self.get(handle)

    def validate_observation(self, observation: Observation) -> bool:
        cid = getattr(getattr(observation, "handle", None), "canonical_id", None)
        return self.current_obs.get(cid) is observation


class FakeLockOwnershipAdapter:
    """Explicit ownership adapter bridging TargetLockRegistry to FocusPolicy.locks.owns."""

    def __init__(self, issued_leases: list[ActorLease]) -> None:
        self._issued = {lease.nonce: lease for lease in issued_leases}

    def register(self, lease: ActorLease) -> None:
        self._issued[lease.nonce] = lease

    def owns(self, lease: ActorLease, *, now_ns: int) -> bool:
        stored = self._issued.get(getattr(lease, "nonce", None))
        return stored is lease and now_ns < lease.expires_ns


class FakeWriterFence:
    """Explicit writer fence tracking acquisitions, releases, and quarantines."""

    def __init__(self) -> None:
        self.busy = False
        self.acquisitions = 0
        self.releases = 0
        self.quarantines = 0

    def acquire(self, lease: ActorLease) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.acquisitions += 1
        return True

    def release(self, lease: ActorLease) -> None:
        if not self.busy:
            raise AssertionError("writer fence released while not acquired")
        self.busy = False
        self.releases += 1

    def quarantine(self, lease: ActorLease) -> None:
        self.busy = True
        self.quarantines += 1


class FakeCdpTransport:
    """In-RAM CDP transport double recording calls without network or browser."""

    def __init__(self, scripted: dict[str, object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.scripted = dict(scripted or {})

    def send(self, method: str, params: dict[str, object] | None = None, session_id: str | None = None) -> dict[str, object]:
        payload = copy.deepcopy(dict(params or {}))
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        item = self.scripted.get(method)
        if isinstance(item, list) and item:
            item = item.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(payload, session_id)
        if isinstance(item, dict):
            return copy.deepcopy(item)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-%s" % payload.get("backendNodeId", 1)}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "undefined"}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 50, 20, 50, 60, 10, 60]}}
        return {}


def _make_ram_frame(handle: Handle, *, epoch: int = 2, frame_seq: int = 7, width: int = 64, height: int = 48) -> SimpleNamespace:
    """Create an in-RAM numpy RGB frame and digest without touching disk."""
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[0, 0, :] = (17, 34, 51)
    digest = hashlib.sha256(pixels.tobytes()).hexdigest()
    return SimpleNamespace(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        width=width,
        height=height,
        digest=digest,
        rgb=pixels,
    )


def _make_ax_node(
    *,
    node_id: str = "0123456789abcdef",
    role: str = "button",
    name: str = "Submit",
    attrs: tuple[tuple[str, str], ...] = (("pressed", "false"),),
    backend_id: int = 101,
    box: list[int] | None = None,
) -> dict[str, object]:
    return {
        "id": node_id,
        "role": role,
        "name": name,
        "depth": 1,
        "attrs": attrs,
        "ancestors": (),
        "ordinal": 0,
        "cardinality": 1,
        "backend_id": backend_id,
        "box": [10, 20, 40, 20] if box is None else list(box),
    }


def _make_closed_registry(*, max_delta: int = 50) -> ClosedCommandRegistry:
    """Create an injected closed command registry for T2 broker testing."""

    def _validate_impulse_domain(args: dict[str, object], prior_effects: list[dict[str, object]]) -> bool:
        delta = args.get("delta", 0)
        if not isinstance(delta, int) or isinstance(delta, bool):
            return False
        if abs(delta) > max_delta:
            return False
        if args.get("forbid_when_prior") and len(prior_effects) > 0:
            return False
        return True

    return ClosedCommandRegistry(
        {
            "pointer_move": CommandSpec(
                name="pointer_move",
                required_args=("x", "y"),
                optional_args=("producer",),
                arg_types={"x": int, "y": int, "producer": str},
                domain_validator=lambda args, _prior: 0 <= int(args["x"]) < 1920 and 0 <= int(args["y"]) < 1080,
            ),
            "key_tap": CommandSpec(
                name="key_tap",
                required_args=("code",),
                optional_args=("producer", "modifiers"),
                arg_types={"code": str, "modifiers": int, "producer": str},
                domain_validator=lambda args, _prior: args["code"] in ("Enter", "Escape", "Tab", "Space"),
            ),
            "impulse": CommandSpec(
                name="impulse",
                required_args=("delta",),
                optional_args=("producer", "forbid_when_prior"),
                arg_types={"delta": int, "forbid_when_prior": bool, "producer": str},
                domain_validator=_validate_impulse_domain,
            ),
        }
    )


class _RoleDriftNodeDict(dict):
    """Dict subclass simulating observed role drift at _authorized_input_events check."""

    def __init__(self, base: dict[str, object], *, drifted_role: str) -> None:
        super().__init__(base)
        self._drifted_role = drifted_role
        self._role_reads = 0

    def get(self, key: str, default: object = None) -> object:
        if key == "role":
            self._role_reads += 1
            if self._role_reads >= 2:
                return self._drifted_role
        return super().get(key, default)


class _ScopedAstMutantRunner:
    """In-memory AST mutant harness with unique qualified anchor, positive control, and cleanup."""

    @staticmethod
    def run_mutant(
        *,
        module: types.ModuleType,
        qualified_anchor: str,
        mutator,
        probe,
    ) -> str:
        # 1. Positive control on real unmutated module must pass cleanly.
        probe(module)

        source = inspect.getsource(module)
        tree = ast.parse(source, filename=module.__file__ or "<memory>")

        target_parts = qualified_anchor.split(".")
        module_parts = module.__name__.split(".")
        if target_parts[: len(module_parts)] != module_parts:
            raise RuntimeError(
                "qualified anchor %r does not belong to module %r"
                % (qualified_anchor, module.__name__)
            )
        rel_qualname = target_parts[len(module_parts) :]
        if not rel_qualname:
            raise RuntimeError("qualified anchor must name a function or method")

        matched_scopes: list[ast.AST] = []

        def _find_scope(node: ast.AST, path: tuple[str, ...]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    next_path = path + (child.name,)
                    if next_path == tuple(rel_qualname):
                        matched_scopes.append(child)
                    else:
                        _find_scope(child, next_path)

        _find_scope(tree, ())
        if len(matched_scopes) != 1:
            raise RuntimeError(
                "expected exactly 1 AST scope for %r, found %d"
                % (qualified_anchor, len(matched_scopes))
            )

        mutation_count = mutator(matched_scopes[0])
        if mutation_count != 1:
            raise RuntimeError(
                "expected exactly 1 AST mutation in %r, applied %d"
                % (qualified_anchor, mutation_count)
            )
        ast.fix_missing_locations(tree)
        code_obj = compile(tree, filename=module.__file__ or "<ast-mutant>", mode="exec")

        mutated_mod = types.ModuleType(module.__name__)
        mutated_mod.__file__ = module.__file__
        mutated_mod.__package__ = module.__package__
        mutated_mod.__loader__ = getattr(module, "__loader__", None)
        mutated_mod.__spec__ = getattr(module, "__spec__", None)

        original_sys_mod = sys.modules.get(module.__name__)
        parent_pkg = sys.modules.get(module.__package__) if module.__package__ else None
        attr_name = module.__name__.rsplit(".", 1)[-1]
        original_pkg_attr = getattr(parent_pkg, attr_name, None) if parent_pkg is not None else None

        try:
            exec(code_obj, mutated_mod.__dict__)
            sys.modules[module.__name__] = mutated_mod
            if parent_pkg is not None:
                setattr(parent_pkg, attr_name, mutated_mod)
            try:
                probe(mutated_mod)
            except AssertionError:
                return "KILLED"
            except Exception as exc:
                raise RuntimeError(
                    "mutant %r raised non-AssertionError %s: %s"
                    % (qualified_anchor, type(exc).__name__, exc)
                ) from exc
            raise AssertionError("mutant at %r SURVIVED" % (qualified_anchor,))
        finally:
            if original_sys_mod is not None:
                sys.modules[module.__name__] = original_sys_mod
            if parent_pkg is not None and original_pkg_attr is not None:
                setattr(parent_pkg, attr_name, original_pkg_attr)


class TestJq3E2G1ElectronCdpInputDispatcher(unittest.TestCase):
    """Acceptance gate G-E2-1 test suite."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._vm_suite_start = time.monotonic()

    @classmethod
    def tearDownClass(cls) -> None:
        elapsed_vm = time.monotonic() - cls._vm_suite_start
        if elapsed_vm > VM_BUDGET_SECONDS:
            raise AssertionError(
                "VM timing budget exceeded: %.3fs VM > %.1fs VM"
                % (elapsed_vm, VM_BUDGET_SECONDS)
            )

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.registry_cmd = _make_closed_registry()
        self.broker = FakeT2Broker(port_epoch=3)
        self.client = T2BrokerClient(
            self.broker, self.registry_cmd, port_epoch=3, clock=self.clock
        )
        bind_default_client(self.client)

    def tearDown(self) -> None:
        bind_default_client(None)

    def test_positive_control_completed_valid_current_port_batch_and_sorting(self) -> None:
        t0 = time.monotonic()
        records = [
            {
                "tick": 5,
                "seq": 1,
                "cmd": "pointer_move",
                "args": {"x": 200, "y": 150, "producer": "p_b"},
            },
            {
                "tick": 2,
                "seq": 1,
                "cmd": "key_tap",
                "args": {"code": "Enter", "modifiers": 0, "producer": "p_a"},
            },
            {
                "tick": 5,
                "seq": 2,
                "cmd": "impulse",
                "args": {"delta": 12, "producer": "p_b"},
            },
        ]
        self.assertEqual(tuple(RECORD_KEYS), ("tick", "seq", "cmd", "args"))
        receipt = submit_batch(records, port_epoch=3, producer_done=True)
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(receipt["attempt_seq"], 1)
        self.assertEqual(receipt["port_epoch"], 3)
        self.assertEqual(receipt["accepted_count"], 3)
        self.assertIsNone(receipt["rejected_index"])
        self.assertEqual(receipt["acknowledged"], 1)
        self.assertEqual(receipt["reconciled"], 0)
        self.assertIsNone(receipt["error"])

        # Deterministic sorting places tick=2 before tick=5.
        effect_ticks_cmds = [(e["tick"], e["seq"], e["cmd"]) for e in self.broker.effect_log]
        self.assertEqual(
            effect_ticks_cmds,
            [
                (2, 1, "key_tap"),
                (5, 1, "pointer_move"),
                (5, 2, "impulse"),
            ],
        )
        self.assertEqual(self.client.trusted_attempt_seq, 1)
        self.assertEqual(self.client.producer_sequences, {"p_a": 1, "p_b": 2})
        self.assertLess(time.monotonic() - t0, VM_BUDGET_SECONDS, "VM positive control timing")

    def test_inv_e2_1_closes_batch_membership_before_admission(self) -> None:
        valid_record = {
            "tick": 1,
            "seq": 1,
            "cmd": "pointer_move",
            "args": {"x": 10, "y": 20, "producer": "p1"},
        }
        for bad_done in (False, None, 1, "true", {}, {"p1": False}, {"other": True}):
            with self.subTest(producer_done=bad_done):
                with self.assertRaises(BatchMembershipIncomplete):
                    self.client.submit_batch(
                        [valid_record], port_epoch=3, producer_done=bad_done
                    )
                self.assertEqual(self.broker.effect_log, [])
                self.assertEqual(self.client.trusted_attempt_seq, 0)

    def test_inv_e2_2_malformed_final_record_and_pre_sort_seq_leave_no_effects(self) -> None:
        good_first = {
            "tick": 1,
            "seq": 1,
            "cmd": "pointer_move",
            "args": {"x": 10, "y": 20, "producer": "p1"},
        }
        malformed_last_cases = [
            # Missing 'args' key
            {"tick": 2, "seq": 2, "cmd": "pointer_move"},
            # Extra key
            {"tick": 2, "seq": 2, "cmd": "pointer_move", "args": {"x": 1, "y": 2, "producer": "p1"}, "extra": 1},
            # Unregistered command in closed registry
            {"tick": 2, "seq": 2, "cmd": "invented_macro", "args": {"producer": "p1"}},
            # Float in args violates canonical STATE/Receipt integer-only rule
            {"tick": 2, "seq": 2, "cmd": "pointer_move", "args": {"x": 1.5, "y": 2, "producer": "p1"}},
            # Bool tick
            {"tick": True, "seq": 2, "cmd": "pointer_move", "args": {"x": 1, "y": 2, "producer": "p1"}},
            # Missing required command arg
            {"tick": 2, "seq": 2, "cmd": "pointer_move", "args": {"x": 1, "producer": "p1"}},
        ]
        for idx, bad_last in enumerate(malformed_last_cases):
            with self.subTest(case=idx):
                with self.assertRaises(MalformedBatchError):
                    self.client.submit_batch(
                        [good_first, bad_last], port_epoch=3, producer_done=True
                    )
                self.assertEqual(self.broker.effect_log, [])
                self.assertEqual(self.client.trusted_attempt_seq, 0)
                self.assertEqual(self.client.producer_sequences, {})

        # Per-producer sequence must be checked BEFORE deterministic sorting:
        # Input order has seq=2 then seq=1 at ticks (2, 1); sorting by (tick, seq)
        # would reorder (tick=1, seq=1) before (tick=2, seq=2) if not checked first.
        unsorted_seq_violation = [
            {"tick": 2, "seq": 2, "cmd": "pointer_move", "args": {"x": 10, "y": 10, "producer": "p1"}},
            {"tick": 1, "seq": 1, "cmd": "pointer_move", "args": {"x": 20, "y": 20, "producer": "p1"}},
        ]
        with self.assertRaises(MalformedBatchError):
            self.client.submit_batch(
                unsorted_seq_violation, port_epoch=3, producer_done=True
            )
        self.assertEqual(self.broker.effect_log, [])
        self.assertEqual(self.client.trusted_attempt_seq, 0)

    def test_inv_e2_3_domain_rejection_preserves_valid_predecessors_and_advances_trusted_attempt(self) -> None:
        batch = [
            {"tick": 1, "seq": 1, "cmd": "pointer_move", "args": {"x": 100, "y": 200, "producer": "p1"}},
            {"tick": 2, "seq": 2, "cmd": "impulse", "args": {"delta": 25, "producer": "p1"}},
            # Structurally valid int delta, but exceeds domain limit max_delta=50
            {"tick": 3, "seq": 3, "cmd": "impulse", "args": {"delta": 999, "producer": "p1"}},
            {"tick": 4, "seq": 4, "cmd": "key_tap", "args": {"code": "Enter", "producer": "p1"}},
        ]
        receipt = self.client.submit_batch(batch, port_epoch=3, producer_done=True)
        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["status"], "domain_rejected")
        self.assertEqual(receipt["attempt_seq"], 1)
        self.assertEqual(receipt["accepted_count"], 2)
        self.assertEqual(receipt["rejected_index"], 2)
        self.assertEqual(receipt["acknowledged"], 1)
        self.assertEqual(len(self.broker.effect_log), 2)
        self.assertEqual([e["seq"] for e in self.broker.effect_log], [1, 2])
        self.assertEqual(self.client.trusted_attempt_seq, 1)
        # Only accepted predecessor sequences are committed.
        self.assertEqual(self.client.producer_sequences, {"p1": 2})

    def test_inv_e2_4_fences_stale_and_retired_port_epochs(self) -> None:
        self.client.advance_port_epoch(5)
        batch = [
            {"tick": 1, "seq": 1, "cmd": "key_tap", "args": {"code": "Tab", "producer": "p1"}}
        ]
        # Retired epoch 3 and future epoch 6 must fail closed with empty effect log.
        for bad_epoch in (3, 4, 6, -1, True):
            with self.subTest(bad_epoch=bad_epoch):
                with self.assertRaises(StalePortEpochError):
                    self.client.submit_batch(batch, port_epoch=bad_epoch, producer_done=True)
                self.assertEqual(self.broker.effect_log, [])
                self.assertEqual(self.client.trusted_attempt_seq, 0)

        with self.assertRaises(StalePortEpochError):
            self.client.reconcile(1, port_epoch=3)

    def test_commit_followed_by_lost_ack_is_uncertain_and_reconciles_original_attempt(self) -> None:
        self.broker.lose_next_ack = True
        batch = [
            {"tick": 1, "seq": 1, "cmd": "pointer_move", "args": {"x": 50, "y": 60, "producer": "p1"}},
            {"tick": 2, "seq": 2, "cmd": "key_tap", "args": {"code": "Enter", "producer": "p1"}},
        ]
        uncertain = self.client.submit_batch(batch, port_epoch=3, producer_done=True)
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertEqual(uncertain["ok"], 0)
        self.assertEqual(uncertain["acknowledged"], 0)
        self.assertEqual(uncertain["attempt_seq"], 1)
        self.assertEqual(self.client.trusted_attempt_seq, 1)
        self.assertEqual(self.client.uncertain_attempt_seq, 1)
        self.assertEqual(len(self.broker.effect_log), 2)

        # New batch while uncertain is fenced until reconcile completes.
        next_batch = [
            {"tick": 3, "seq": 3, "cmd": "key_tap", "args": {"code": "Escape", "producer": "p1"}}
        ]
        with self.assertRaises(BrokerClientError):
            self.client.submit_batch(next_batch, port_epoch=3, producer_done=True)
        self.assertEqual(self.client.trusted_attempt_seq, 1)

        # Reconcile with stale epoch is fenced.
        with self.assertRaises(StalePortEpochError):
            self.client.reconcile(1, port_epoch=2)

        # Reconcile original attempt at current port_epoch without minting a new sequence.
        resolved = reconcile(1, port_epoch=3)
        self.assertEqual(resolved["ok"], 1)
        self.assertEqual(resolved["status"], "committed")
        self.assertEqual(resolved["attempt_seq"], 1)
        self.assertEqual(resolved["acknowledged"], 1)
        self.assertEqual(resolved["reconciled"], 1)
        self.assertEqual(resolved["accepted_count"], 2)
        self.assertEqual(self.client.trusted_attempt_seq, 1)
        self.assertIsNone(self.client.uncertain_attempt_seq)
        # Effects were not duplicated on reconcile.
        self.assertEqual(len(self.broker.effect_log), 2)

        # Subsequent batch now advances attempt_seq to 2 cleanly.
        follow_up = self.client.submit_batch(next_batch, port_epoch=3, producer_done=True)
        self.assertEqual(follow_up["ok"], 1)
        self.assertEqual(follow_up["attempt_seq"], 2)
        self.assertEqual(self.client.trusted_attempt_seq, 2)
        self.assertEqual(len(self.broker.effect_log), 3)

    def test_snapshot_boundary_and_authority_prerequisites_fail_closed(self) -> None:
        with self.assertRaises(BrokerPrerequisiteAbsent):
            T2BrokerClient(None, self.registry_cmd)
        with self.assertRaises(BrokerPrerequisiteAbsent):
            T2BrokerClient(self.broker, None)
        with self.assertRaises(LockPrerequisiteAbsent) as ctx_lock:
            TargetLockRegistry(None, clock=self.clock)
        self.assertIn("session registry required", str(ctx_lock.exception))

        handle = Handle("cdp_tab", "tab-main", 1)
        rec = TargetRecord(handle=handle, epoch=2, session_id="sid-e2")
        sess_reg = FakeSessionRegistry([rec])
        lock_reg = TargetLockRegistry(sess_reg, clock=self.clock)
        lease = lock_reg.acquire(handle, "worker-1", expires_ns=self.clock() + 50_000)
        lock_reg.mark_inflight(lease)
        with self.assertRaises(TypeError) as ctx_bool:
            lock_reg.reconcile_quarantine(handle, acknowledged=1)  # type: ignore[arg-type]
        self.assertIn("acknowledged must be bool", str(ctx_bool.exception))
        lock_reg.reconcile_quarantine(handle, acknowledged=True)

        # The concrete registry now authenticates minted leases through owns.
        policy = FocusPolicy(sess_reg, lock_reg, clock=self.clock)
        live_lease = lock_reg.acquire(handle, "worker-1", expires_ns=self.clock() + 50_000)
        policy._check_lease(live_lease, self.clock())
        from dataclasses import replace
        forged_lease = replace(live_lease, nonce=b"not-minted")
        with self.assertRaisesRegex(FocusRefused, "actor lease nonce is not current/owned"):
            policy._check_lease(forged_lease, self.clock())

        with self.assertRaises(FocusRefused) as ctx_get:
            FocusPolicy(object(), FakeLockOwnershipAdapter([]), clock=self.clock)
        self.assertIn("session registry.get prerequisite AUSENTE", str(ctx_get.exception))

    def _build_authorized_cdp_fixture(self, *, mod: types.ModuleType = cdp_executor_mod):
        clock = FakeClock(1_000)
        handle = Handle("cdp_tab", "tab-auth", 2)
        record = TargetRecord(handle=handle, epoch=4, session_id="sid-auth")
        registry = FakeSessionRegistry([record])
        lease = ActorLease(handle, "actor-auth", 2, b"nonce-auth-01", 90_000)
        locks = FakeLockOwnershipAdapter([lease])
        focus_policy = FocusPolicy(registry, locks, clock=clock)
        focus_policy.set_focus(handle, domain="cdp:sid-auth", epoch=4)
        focus_grant = focus_policy.grant(lease, domain="cdp:sid-auth")
        fence = FakeWriterFence()
        cdp = FakeCdpTransport()
        btn = _make_ax_node(
            node_id="0123456789abcdef",
            role="button",
            name="Confirm",
            attrs=(("pressed", "false"),),
            backend_id=77,
            box=[20, 30, 40, 20],
        )
        frame = _make_ram_frame(handle, epoch=4, frame_seq=11, width=120, height=80)
        state = {
            "handle": handle,
            "epoch": 4,
            "frame_seq": 11,
            "viewport": {"w": 120, "h": 80, "scroll_x": 0, "scroll_y": 0},
            "nodes": [copy.deepcopy(btn)],
            "focused_node_id": btn["id"],
        }
        obs = Observation(
            handle=handle,
            epoch=4,
            frame_seq=11,
            dpr_ppm=1_000_000,
            state=state,
            frame=frame,
        )
        registry.current_obs[handle.canonical_id] = obs
        return SimpleNamespace(
            mod=mod,
            clock=clock,
            handle=handle,
            record=record,
            registry=registry,
            lease=lease,
            locks=locks,
            focus_policy=focus_policy,
            focus=focus_grant,
            fence=fence,
            cdp=cdp,
            btn=btn,
            obs=obs,
        )

    def test_kill_mutant_m_wender_act_cdp_executor_1(self) -> None:
        """Kill M-wender-act-cdp-executor-1 via _run_authorized_ax_action real path."""

        def _mutator_m1(func_node: ast.AST) -> int:
            count = 0
            for node in ast.walk(func_node):
                if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
                    if (
                        len(node.values) == 2
                        and isinstance(node.values[0], ast.Call)
                        and isinstance(node.values[0].func, ast.Name)
                        and node.values[0].func.id == "_verify_expect"
                    ):
                        node.op = ast.Or()
                        count += 1
            return count

        def _probe_m1(mod: types.ModuleType) -> None:
            fx = self._build_authorized_cdp_fixture(mod=mod)
            # Post-action snapshot changes digest (busy='true' added on target),
            # but the expected postcondition pressed=='true' is NOT met (pressed remains 'false').
            changed_unverified = _make_ax_node(
                node_id=fx.btn["id"],
                role="button",
                name="Confirm",
                attrs=(("pressed", "false"), ("busy", "true")),
                backend_id=77,
                box=[20, 30, 40, 20],
            )
            snapshots = iter(([fx.btn], [changed_unverified]))
            action = {
                "op": "click",
                "target": fx.btn["id"],
                "expect": {"target_states": {"pressed": "true"}},
                "irreversible": False,
            }
            receipt = mod.run_ax_action(
                fx.cdp,
                "sid-auth",
                [fx.btn],
                action,
                lambda: next(snapshots),
                retries=1,
                wait_ms=0,
                sleep=lambda _: None,
                lease=fx.lease,
                focus=fx.focus,
                observation=fx.obs,
                registry=fx.registry,
                focus_policy=fx.focus_policy,
                writer_fence=fx.fence,
                clock=fx.clock,
            )
            self.assertNotEqual(receipt["pre_sha"], receipt["post_sha"])
            self.assertEqual(receipt["ok"], 0)
            self.assertEqual(receipt["verified"], 0)
            self.assertEqual(receipt["error"], "postcondition not established")

        verdict = _ScopedAstMutantRunner.run_mutant(
            module=cdp_executor_mod,
            qualified_anchor="wender.act.cdp_executor._run_authorized_ax_action",
            mutator=_mutator_m1,
            probe=_probe_m1,
        )
        self.assertEqual(verdict, "KILLED")

    def test_kill_mutant_m_wender_act_cdp_executor_2(self) -> None:
        """Kill M-wender-act-cdp-executor-2 via _authorized_input_events real path."""

        def _mutator_m2(func_node: ast.AST) -> int:
            count = 0
            for node in ast.walk(func_node):
                if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.NotEq):
                    if (
                        isinstance(node.left, ast.Call)
                        and isinstance(node.left.func, ast.Attribute)
                        and isinstance(node.left.func.value, ast.Name)
                        and node.left.func.value.id == "obs_node"
                        and node.left.func.attr == "get"
                        and node.left.args
                        and isinstance(node.left.args[0], ast.Constant)
                        and node.left.args[0].value == "role"
                    ):
                        node.ops[0] = ast.Eq()
                        count += 1
            return count

        def _probe_m2(mod: types.ModuleType) -> None:
            # 1. Matching roles must pass and dispatch pointer events.
            fx_pass = self._build_authorized_cdp_fixture(mod=mod)
            after_hover = _make_ax_node(
                node_id=fx_pass.btn["id"],
                role="button",
                name="Confirm",
                attrs=(("pressed", "true"),),
                backend_id=77,
                box=[20, 30, 40, 20],
            )
            snapshots_pass = iter(([fx_pass.btn], [after_hover]))
            hover_action = {
                "op": "hover",
                "target": fx_pass.btn["id"],
                "expect": {"target_states": {"pressed": "true"}},
                "irreversible": False,
            }
            try:
                receipt = mod.run_ax_action(
                    fx_pass.cdp,
                    "sid-auth",
                    [fx_pass.btn],
                    hover_action,
                    lambda: next(snapshots_pass),
                    retries=1,
                    wait_ms=0,
                    sleep=lambda _: None,
                    lease=fx_pass.lease,
                    focus=fx_pass.focus,
                    observation=fx_pass.obs,
                    registry=fx_pass.registry,
                    focus_policy=fx_pass.focus_policy,
                    writer_fence=fx_pass.fence,
                    clock=fx_pass.clock,
                )
            except mod.ActionError as exc:
                self.fail("matching roles in _authorized_input_events unexpectedly refused: %s" % exc)
            self.assertEqual(receipt["ok"], 1)
            self.assertEqual(len(fx_pass.cdp.calls), 1)
            self.assertEqual(fx_pass.cdp.calls[0]["method"], "Input.dispatchMouseEvent")

            # 2. Mismatching observed role at _authorized_input_events must refuse before dispatch.
            fx_fail = self._build_authorized_cdp_fixture(mod=mod)
            fx_fail.obs.state["nodes"] = [
                _RoleDriftNodeDict(fx_fail.btn, drifted_role="textbox")
            ]
            snapshots_fail = iter(([fx_fail.btn], [after_hover]))
            with self.assertRaises(mod.ActionError):
                mod.run_ax_action(
                    fx_fail.cdp,
                    "sid-auth",
                    [fx_fail.btn],
                    hover_action,
                    lambda: next(snapshots_fail),
                    retries=1,
                    wait_ms=0,
                    sleep=lambda _: None,
                    lease=fx_fail.lease,
                    focus=fx_fail.focus,
                    observation=fx_fail.obs,
                    registry=fx_fail.registry,
                    focus_policy=fx_fail.focus_policy,
                    writer_fence=fx_fail.fence,
                    clock=fx_fail.clock,
                )
            self.assertEqual(fx_fail.cdp.calls, [])

        verdict = _ScopedAstMutantRunner.run_mutant(
            module=cdp_executor_mod,
            qualified_anchor="wender.act.cdp_executor._authorized_input_events",
            mutator=_mutator_m2,
            probe=_probe_m2,
        )
        self.assertEqual(verdict, "KILLED")

    def test_seeded_random_property_loops_target_64_cases_with_oracle(self) -> None:
        rng = random.Random(PROPERTY_SEED)
        completed_cases = 0

        for case_idx in range(PROPERTY_TARGET_CASES):
            epoch = rng.randint(1, 9)
            broker = FakeT2Broker(port_epoch=epoch)
            registry = _make_closed_registry(max_delta=50)
            client = T2BrokerClient(broker, registry, port_epoch=epoch)

            oracle_effects: list[dict[str, object]] = []
            oracle_attempt_seq = 0
            oracle_prod_seq: dict[str, int] = {}

            scenario = rng.choice(
                (
                    "valid_batch",
                    "incomplete_membership",
                    "malformed_final",
                    "seq_violation_pre_sort",
                    "domain_rejection_middle",
                    "stale_epoch",
                    "lost_ack_reconcile",
                )
            )
            diag = "seed=0x%x case=%d scenario=%s epoch=%d" % (
                PROPERTY_SEED,
                case_idx,
                scenario,
                epoch,
            )

            n_records = rng.randint(2, 5)
            batch: list[dict[str, object]] = []
            local_seq = 0
            cur_tick = rng.randint(1, 5)
            for _ in range(n_records):
                local_seq += 1
                cur_tick += rng.randint(0, 3)
                batch.append(
                    {
                        "tick": cur_tick,
                        "seq": local_seq,
                        "cmd": "impulse",
                        "args": {"delta": rng.randint(-40, 40), "producer": "p_prop"},
                    }
                )

            if scenario == "valid_batch":
                receipt = client.submit_batch(batch, port_epoch=epoch, producer_done=True)
                oracle_attempt_seq += 1
                oracle_prod_seq["p_prop"] = n_records
                expected_sorted = sorted(batch, key=lambda r: (int(r["tick"]), int(r["seq"])))
                for r in expected_sorted:
                    oracle_effects.append(
                        {
                            "attempt_seq": oracle_attempt_seq,
                            "port_epoch": epoch,
                            "tick": r["tick"],
                            "seq": r["seq"],
                            "cmd": r["cmd"],
                            "args": copy.deepcopy(r["args"]),
                        }
                    )
                self.assertEqual(receipt["ok"], 1, msg=diag)
                self.assertEqual(receipt["accepted_count"], n_records, msg=diag)

            elif scenario == "incomplete_membership":
                with self.assertRaises(BatchMembershipIncomplete, msg=diag):
                    client.submit_batch(batch, port_epoch=epoch, producer_done=False)

            elif scenario == "malformed_final":
                bad_batch = copy.deepcopy(batch)
                bad_batch[-1] = {"tick": cur_tick + 1, "seq": local_seq, "cmd": "impulse"}
                with self.assertRaises(MalformedBatchError, msg=diag):
                    client.submit_batch(bad_batch, port_epoch=epoch, producer_done=True)

            elif scenario == "seq_violation_pre_sort":
                bad_batch = [
                    {"tick": 10, "seq": 2, "cmd": "impulse", "args": {"delta": 5, "producer": "p_prop"}},
                    {"tick": 5, "seq": 1, "cmd": "impulse", "args": {"delta": 5, "producer": "p_prop"}},
                ]
                with self.assertRaises(MalformedBatchError, msg=diag):
                    client.submit_batch(bad_batch, port_epoch=epoch, producer_done=True)

            elif scenario == "domain_rejection_middle":
                reject_idx = rng.randint(1, n_records - 1)
                dom_batch = copy.deepcopy(batch)
                dom_batch[reject_idx]["args"]["delta"] = 500  # exceeds max_delta=50
                receipt = client.submit_batch(dom_batch, port_epoch=epoch, producer_done=True)
                oracle_attempt_seq += 1
                oracle_prod_seq["p_prop"] = reject_idx
                for r in dom_batch[:reject_idx]:
                    oracle_effects.append(
                        {
                            "attempt_seq": oracle_attempt_seq,
                            "port_epoch": epoch,
                            "tick": r["tick"],
                            "seq": r["seq"],
                            "cmd": r["cmd"],
                            "args": copy.deepcopy(r["args"]),
                        }
                    )
                self.assertEqual(receipt["ok"], 0, msg=diag)
                self.assertEqual(receipt["status"], "domain_rejected", msg=diag)
                self.assertEqual(receipt["accepted_count"], reject_idx, msg=diag)
                self.assertEqual(receipt["rejected_index"], reject_idx, msg=diag)

            elif scenario == "stale_epoch":
                client.advance_port_epoch(epoch + 1)
                with self.assertRaises(StalePortEpochError, msg=diag):
                    client.submit_batch(batch, port_epoch=epoch, producer_done=True)

            elif scenario == "lost_ack_reconcile":
                broker.lose_next_ack = True
                uncertain = client.submit_batch(batch, port_epoch=epoch, producer_done=True)
                oracle_attempt_seq += 1
                oracle_prod_seq["p_prop"] = n_records
                for r in batch:
                    oracle_effects.append(
                        {
                            "attempt_seq": oracle_attempt_seq,
                            "port_epoch": epoch,
                            "tick": r["tick"],
                            "seq": r["seq"],
                            "cmd": r["cmd"],
                            "args": copy.deepcopy(r["args"]),
                        }
                    )
                self.assertEqual(uncertain["status"], "uncertain", msg=diag)
                self.assertEqual(uncertain["attempt_seq"], oracle_attempt_seq, msg=diag)
                reconciled_rec = client.reconcile(oracle_attempt_seq, port_epoch=epoch)
                self.assertEqual(reconciled_rec["ok"], 1, msg=diag)
                self.assertEqual(reconciled_rec["attempt_seq"], oracle_attempt_seq, msg=diag)
                self.assertEqual(reconciled_rec["reconciled"], 1, msg=diag)

            self.assertEqual(client.trusted_attempt_seq, oracle_attempt_seq, msg=diag)
            self.assertEqual(client.producer_sequences, oracle_prod_seq, msg=diag)
            self.assertEqual(broker.effect_log, oracle_effects, msg=diag)
            completed_cases += 1

        self.assertEqual(completed_cases, PROPERTY_TARGET_CASES)

    def test_concurrency_32_recorded_schedules_bounded_barriers_and_joins(self) -> None:
        baseline_threads = {t.ident for t in threading.enumerate()}
        completed_schedules: list[tuple[int, tuple[str, ...]]] = []

        for sched_idx in range(CONCURRENCY_TARGET_SCHEDULES):
            epoch = 10 + sched_idx
            broker = FakeT2Broker(port_epoch=epoch)
            registry = _make_closed_registry(max_delta=50)
            client = T2BrokerClient(broker, registry, port_epoch=epoch)

            mode = sched_idx % 4
            num_threads = 3 if mode in (0, 1) else MAX_CONCURRENCY_THREADS
            self.assertLessEqual(num_threads, MAX_CONCURRENCY_THREADS)

            start_barrier = threading.Barrier(num_threads, timeout=SYNC_TIMEOUT_S)
            phase_event = threading.Event()
            done_event = threading.Event()
            schedule_log: list[str] = []
            log_lock = threading.Lock()
            errors: list[BaseException] = []
            threads: list[threading.Thread] = []

            def _record(step: str) -> None:
                with log_lock:
                    schedule_log.append(step)

            def _worker(tid: int, *, _mode: int = mode, _epoch: int = epoch) -> None:
                try:
                    start_barrier.wait(timeout=SYNC_TIMEOUT_S)
                    if _mode == 0:
                        if tid == 0:
                            _record("w0:submit_initial")
                            rec = client.submit_batch(
                                [
                                    {
                                        "tick": 1,
                                        "seq": 1,
                                        "cmd": "impulse",
                                        "args": {"delta": 10, "producer": "p0"},
                                    }
                                ],
                                port_epoch=_epoch,
                                producer_done=True,
                            )
                            assert rec["ok"] == 1
                            phase_event.set()
                        elif tid == 1:
                            assert phase_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record("w1:stale_epoch_refused")
                            try:
                                client.submit_batch(
                                    [
                                        {
                                            "tick": 2,
                                            "seq": 1,
                                            "cmd": "impulse",
                                            "args": {"delta": 5, "producer": "p1"},
                                        }
                                    ],
                                    port_epoch=_epoch - 1,
                                    producer_done=True,
                                )
                            except StalePortEpochError:
                                pass
                            else:
                                raise AssertionError("expected StalePortEpochError")
                            done_event.set()
                        else:
                            assert done_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record("w2:submit_second")
                            rec2 = client.submit_batch(
                                [
                                    {
                                        "tick": 2,
                                        "seq": 2,
                                        "cmd": "impulse",
                                        "args": {"delta": 15, "producer": "p0"},
                                    }
                                ],
                                port_epoch=_epoch,
                                producer_done=True,
                            )
                            assert rec2["ok"] == 1
                    elif _mode == 1:
                        if tid == 0:
                            broker.lose_next_ack = True
                            _record("w0:uncertain_commit")
                            r0 = client.submit_batch(
                                [
                                    {
                                        "tick": 1,
                                        "seq": 1,
                                        "cmd": "key_tap",
                                        "args": {"code": "Enter", "producer": "p_u"},
                                    }
                                ],
                                port_epoch=_epoch,
                                producer_done=True,
                            )
                            assert r0["status"] == "uncertain"
                            phase_event.set()
                        elif tid == 1:
                            assert phase_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record("w1:blocked_by_uncertain")
                            try:
                                client.submit_batch(
                                    [
                                        {
                                            "tick": 2,
                                            "seq": 2,
                                            "cmd": "key_tap",
                                            "args": {"code": "Tab", "producer": "p_u"},
                                        }
                                    ],
                                    port_epoch=_epoch,
                                    producer_done=True,
                                )
                            except BrokerClientError:
                                pass
                            else:
                                raise AssertionError("expected uncertain fence")
                            done_event.set()
                        else:
                            assert done_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record("w2:reconcile_original")
                            r_rec = client.reconcile(1, port_epoch=_epoch)
                            assert r_rec["ok"] == 1 and r_rec["attempt_seq"] == 1
                    else:
                        prod_name = "prod_%d" % tid
                        if tid == 0:
                            _record("w0:malformed_rejected")
                            try:
                                client.submit_batch(
                                    [{"tick": 1, "seq": 1, "cmd": "unknown_cmd", "args": {"producer": prod_name}}],
                                    port_epoch=_epoch,
                                    producer_done=True,
                                )
                            except MalformedBatchError:
                                pass
                            else:
                                raise AssertionError("expected MalformedBatchError")
                            phase_event.set()
                        else:
                            assert phase_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record("w%d:valid_submit" % tid)
                            r_ok = client.submit_batch(
                                [
                                    {
                                        "tick": tid,
                                        "seq": 1,
                                        "cmd": "pointer_move",
                                        "args": {"x": tid * 10, "y": tid * 10, "producer": prod_name},
                                    }
                                ],
                                port_epoch=_epoch,
                                producer_done=True,
                            )
                            assert r_ok["ok"] == 1
                            done_event.set()
                except BaseException as exc:  # noqa: BLE001
                    with log_lock:
                        errors.append(exc)
                    phase_event.set()
                    done_event.set()

            try:
                for tid in range(num_threads):
                    t = threading.Thread(
                        target=_worker,
                        args=(tid,),
                        name="jq3-e2-sched-%d-t-%d" % (sched_idx, tid),
                        daemon=False,
                    )
                    threads.append(t)
                    t.start()
                for t in threads:
                    t.join(timeout=SYNC_TIMEOUT_S)
            finally:
                phase_event.set()
                done_event.set()
                try:
                    start_barrier.abort()
                except Exception:
                    pass
                for t in threads:
                    if t.is_alive():
                        t.join(timeout=SYNC_TIMEOUT_S)
                    self.assertFalse(
                        t.is_alive(),
                        "leaked thread %s in schedule %d" % (t.name, sched_idx),
                    )

            if errors:
                raise AssertionError(
                    "schedule %d failed with errors: %r" % (sched_idx, errors)
                )
            self.assertEqual(len(schedule_log), num_threads)
            completed_schedules.append((sched_idx, tuple(schedule_log)))

        self.assertEqual(len(completed_schedules), CONCURRENCY_TARGET_SCHEDULES)
        after_threads = {t.ident for t in threading.enumerate()}
        self.assertTrue(after_threads.issubset(baseline_threads))


if __name__ == "__main__":
    unittest.main()
