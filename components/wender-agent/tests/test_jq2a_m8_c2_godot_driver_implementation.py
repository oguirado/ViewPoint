"""Offline production-path checks for GodotWindowDriver and the real GodotExecutor.

Only explicit RAM fakes for the unmerged target/lock/scheduler/T2 dependencies;
no test-local driver or executor implementation and no native process/network/files.
"""
import builtins
import copy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

import numpy as np

from wender.act.godot_executor import GodotExecutor
from wender.act.godot_window_driver import (
    ActionRequest, ActorLease, FocusLease, FrameRef, GodotDriverError,
    GodotProtocolError, GodotWindowDriver, Handle, Observation,
    PrerequisiteUnavailable,
)
from wender.perceive.godot import node_id_for_control_path, validate_uitree
from wender.state.schema import build_state, dumps


END = b"\n---OFFLINE-FAKE-T2-END---\n"


def fixture_encode(records):
    return (json.dumps({"fixture_records": records}, sort_keys=True,
                       separators=(",", ":")).encode("utf-8") + END)


def fixture_decode(data):
    batches = []
    pending = bytes(data)
    while END in pending:
        head, pending = pending.split(END, 1)
        obj = json.loads(head.decode("utf-8"))
        if set(obj) != {"fixture_records"} or not isinstance(obj["fixture_records"], list):
            raise ValueError("malformed fake T2 envelope")
        batches.append(obj["fixture_records"])
    return batches, pending


def fixture_state(label):
    path = "root/next"
    node_id = node_id_for_control_path(path)
    base = build_state(
        [{"id": node_id, "role": "button", "name": "Next", "depth": 0,
          "ordinal": 0, "attrs": []}], kind="godot", url=None, title=label,
        ax_sha256="a" * 64, ax_stable_sha256="b" * 64,
        prune={"name": "fixture", "version": 1}, raw_count=1,
        actionable_roles={"button"},
    )
    basis = hashlib.sha256(dumps(base).encode("utf-8")).hexdigest()
    sidecar = {"schema": "holdings.godot.uitree/v1", "base_sha256": basis,
               "nodes": {node_id: {"control_path": path, "screen_id": "root"}}}
    assert not validate_uitree(base, sidecar), "offline fixture must validate"
    return base, sidecar, basis


class FakeTarget:
    offline_fixture = True

    def __init__(self, canonical_id, resource=None, tick=4):
        self.handle = Handle("godot_window", canonical_id, 2)
        self.epoch = 7
        self.frame_seq = 13
        self.resource = resource or ("godot:" + canonical_id)
        self.tick = tick
        self.set_state("state-initial", tick)

    def set_state(self, label, tick):
        self.base, self.sidecar, self.basis = fixture_state(label)
        self.tick = tick

    def snapshot(self):
        return {"handle": self.handle, "epoch": self.epoch,
                "committed_tick": self.tick, "basis_sha256": self.basis,
                "base_state": copy.deepcopy(self.base),
                "sidecar": copy.deepcopy(self.sidecar),
                "frame_seq": self.frame_seq,
                "actuation_resource": self.resource}


class FakeLocks:
    """RAM-only shared registry: resource IDs alias capsule and child screens."""
    def __init__(self):
        self.owners = {}
        self.claims = []

    def try_acquire(self, resource, lease):
        if resource in self.owners:
            return None
        token = object()
        self.owners[resource] = token
        self.claims.append((resource, lease.handle.kind))
        return token

    def release(self, resource, claim):
        if self.owners[resource] is not claim:
            raise RuntimeError("invalid actor lock owner")
        del self.owners[resource]


class FakeFence:
    def __init__(self):
        self.issued = set()
        self.disabled = False

    def try_reserve(self, handle, lease, request, now_ns):
        if self.disabled or request.cost > 20 or request.deadline_ns <= now_ns:
            return None
        token = object()
        self.issued.add(token)
        return token

    def release(self, claim):
        self.issued.remove(claim)


class FakeFocus:
    def authorize(self, handle, lease, focus):
        return (focus is not None and focus.lease.nonce == lease.nonce
                and focus.epoch == 7 and focus.domain == "godot-viewport")


class FakeT2:
    """Fake session-owned provenance and bounded fixture-only wire codec."""
    def __init__(self, request, records, *, data=None):
        self.handle = request.handle
        self.epoch = request.epoch
        self.basis_sha256 = request.payload["basis_sha256"]
        self.frame_seq = request.payload["frame_seq"]
        self.data = fixture_encode(records) if data is None else data
        self.decode_batches = fixture_decode


SCHEMAS = {
    "open_screen": {"args": {"screen_id": "nonempty_str"}, "boundary": "current"},
    "take_loan": {"args": {"target_id": "nonempty_str", "amount": "pos_int"},
                  "economic": True, "irreversible": True},
}


def executor_for(sink, context=None):
    return GodotExecutor(context or {"slot_kind": "sandbox", "principal": "offline"},
                         SCHEMAS, fixture_encode, sink.append)


def action(target, action_id, cmd="open_screen", args=None, tick=None, *, observation=None):
    if args is None:
        args = {"screen_id": "dashboard"} if cmd == "open_screen" else {}
    payload = {"cmd": cmd, "args": copy.deepcopy(args),
               "tick": target.tick if tick is None else tick,
               "basis_sha256": target.basis, "frame_seq": target.frame_seq}
    if observation is not None:
        payload["observation"] = observation
    return ActionRequest(target.handle, action_id, target.epoch, payload, 1, 1_000)


def leases(target):
    lease = ActorLease(target.handle, "worker-1", target.handle.generation, b"nonce-1", 1_000)
    return lease, FocusLease(lease, "godot-viewport", target.epoch)


def driver_for(target, *, locks=None, fence=None, ctx=None, sink=None, executor=None):
    sink = [] if sink is None else sink
    executor = executor or executor_for(sink, ctx)
    locks = locks or FakeLocks()
    fence = fence or FakeFence()
    driver = GodotWindowDriver(target, executor, locks=locks, focus_policy=FakeFocus(),
                               writer_fence=fence, clock=lambda: 10)
    return driver, executor, sink, locks, fence


def engine_outcome(seq, requested_tick, applied_tick, state_sha256, *,
                   status="accepted", reason=None, order_seq_id=1):
    return {"seq": seq, "requested_tick": requested_tick,
            "applied_tick": applied_tick, "order_seq_id": order_seq_id,
            "status": status, "reason": reason, "state_sha256": state_sha256}


class TestGodotWindowDriverProductionPaths(unittest.TestCase):
    def test_real_base_sidecar_validation_and_no_base_metadata_pollution(self):
        target = FakeTarget("a")
        driver, executor, sink, _, _ = driver_for(target)
        lease, focus = leases(target)
        request = action(target, "x")
        original_keys = set(target.base)
        self.assertEqual(driver.execute(request, lease=lease, focus=focus).status, "pending")
        self.assertEqual(set(target.base), original_keys)
        self.assertNotIn("handle", target.base)
        bad = FakeTarget("bad")
        bad.sidecar["base_sha256"] = "0" * 64
        d2, _, s2, _, _ = driver_for(bad)
        with self.assertRaisesRegex(GodotDriverError, "sidecar"):
            d2.execute(action(bad, "bad"), lease=leases(bad)[0], focus=leases(bad)[1])
        self.assertEqual(s2, [])

    def test_g_m8_2_independent_godot_cursors_and_cdp_claim_co_schedule(self):
        locks = FakeLocks()
        cdp_handle = Handle("cdp_tab", "tab-21", 5)
        cdp_lease = ActorLease(cdp_handle, "cdp-worker", 5, b"cdp-nonce", 1_000)
        cdp_claim = locks.try_acquire("cdp:tab-21", cdp_lease)
        self.assertIsNotNone(cdp_claim)
        a, b = FakeTarget("a"), FakeTarget("b")
        da, ea, sa, _, _ = driver_for(a, locks=locks)
        db, eb, sb, _, _ = driver_for(b, locks=locks)
        ra, rb = action(a, "A"), action(b, "B")
        self.assertEqual(da.execute(ra, lease=leases(a)[0], focus=leases(a)[1]).status, "pending")
        self.assertEqual(db.execute(rb, lease=leases(b)[0], focus=leases(b)[1]).status, "pending")
        self.assertEqual((da.next_seq, db.next_seq, ea.next_seq, eb.next_seq), (2, 2, 2, 2))
        self.assertEqual(json.loads(sa[0].split(END)[0])["fixture_records"][0]["seq"], 1)
        self.assertEqual(json.loads(sb[0].split(END)[0])["fixture_records"][0]["seq"], 1)
        self.assertEqual(set(locks.owners), {"cdp:tab-21", "godot:a", "godot:b"})
        locks.release("cdp:tab-21", cdp_claim)

    def test_g_m8_2_shared_executor_mutant_refused_and_seq_injection_rejected(self):
        target = FakeTarget("a")
        sink = []
        executor = executor_for(sink)
        first_driver, _, _, _, _ = driver_for(target, executor=executor, sink=sink)
        self.assertIsNotNone(first_driver)
        with self.assertRaisesRegex(GodotDriverError, "already bound"):
            driver_for(FakeTarget("b"), executor=executor, sink=sink)
        d, _, _, _, _ = driver_for(FakeTarget("c"))
        target = d.target
        req = action(target, "forge")
        req.payload["seq"] = 999
        self.assertEqual(d.execute(req, lease=leases(target)[0], focus=leases(target)[1]).reason,
                         "COMMAND_METADATA")
        self.assertEqual(d.next_seq, 1)

    def test_admission_pending_then_authoritative_matching_engine_outcome(self):
        target = FakeTarget("a")
        driver, _, sink, locks, fence = driver_for(target)
        req = action(target, "open")
        lease, focus = leases(target)
        self.assertEqual(driver.execute(req, lease=lease, focus=focus).status, "pending")
        self.assertEqual(driver.committed_tick, target.tick)
        self.assertTrue(locks.owners)
        self.assertEqual(driver.execute(action(target, "other"), lease=lease, focus=focus).reason,
                         "ACK_PENDING")
        _, _, new_digest = fixture_state("after-ack")
        outcome = engine_outcome(1, 4, 4, new_digest)
        receipt = driver.confirm_completion(req, FakeT2(req, [outcome]))
        self.assertEqual((receipt.status, receipt.reason), ("applied", None))
        self.assertEqual((driver.committed_tick, driver.basis_sha256, driver.next_seq),
                         (4, new_digest, 2))
        self.assertEqual((locks.owners, fence.issued), ({}, set()))
        self.assertEqual(len(driver.tick_trail), 1)
        self.assertEqual(driver.tick_trail[0]["applied_tick"], 4)
        self.assertEqual(len(driver.validated_batches), 1)
        self.assertEqual(len(sink), 1)

    def test_t2_basis_epoch_frame_and_tick_mismatch_never_complete(self):
        for mutant, expected in (("epoch", "T2_IDENTITY_OR_FRAME_MISMATCH"),
                                 ("basis_sha256", "T2_IDENTITY_OR_FRAME_MISMATCH"),
                                 ("frame_seq", "T2_IDENTITY_OR_FRAME_MISMATCH"),
                                 ("outcome_tick", "T2_SEQUENCE_OR_TICK_MISMATCH")):
            with self.subTest(mutant=mutant):
                target = FakeTarget(mutant)
                d, _, _, locks, _ = driver_for(target)
                req = action(target, "r")
                d.execute(req, lease=leases(target)[0], focus=leases(target)[1])
                outcome = engine_outcome(1, 4, 4, target.basis)
                t2 = FakeT2(req, [outcome])
                if mutant == "outcome_tick":
                    t2.data = fixture_encode([engine_outcome(1, 3, 4, target.basis)])
                else:
                    setattr(t2, mutant, "f" * 64 if mutant == "basis_sha256"
                            else getattr(t2, mutant) + 1)
                receipt = d.confirm_completion(req, t2)
                self.assertEqual(receipt.reason, expected)
                self.assertEqual(receipt.status, "quarantined")
                self.assertTrue(locks.owners)
                self.assertEqual(d.execute(action(target, "retry"), lease=leases(target)[0],
                                           focus=leases(target)[1]).reason, "QUARANTINED")

    def test_g_m8_4_confirm_proposal_single_use_and_main_save_guard(self):
        target = FakeTarget("slot")
        d, ex, sink, _, _ = driver_for(target)
        args = {"target_id": "company-5", "amount": 100}
        lease, focus = leases(target)
        proposed = action(target, "proposal", cmd="take_loan", args=args, tick=5)
        r = d.execute(proposed, lease=lease, focus=focus)
        self.assertEqual((r.status, r.reason), ("proposed", "NEEDS_CONFIRM"))
        self.assertEqual((sink, d.next_seq), ([], 1))
        proposal = ex.proposals["modal_take_loan_1"]
        binding = {k: proposal[k] for k in ("modal_id", "cmd", "target_id", "terms", "basis_sha256")}
        confirm = action(target, "confirmed", cmd="confirm", args=binding, tick=5)
        self.assertEqual(d.execute(confirm, lease=lease, focus=focus).status, "pending")
        self.assertEqual(len(sink), 1)
        _, _, digest = fixture_state("engine-after-confirm")
        t2 = FakeT2(confirm, [engine_outcome(1, 5, 5, digest)])
        self.assertEqual(d.confirm_completion(confirm, t2).status, "applied")
        target.set_state("engine-after-confirm", tick=5)
        target.frame_seq += 1
        replay = action(target, "replay", cmd="confirm", args=binding, tick=6)
        rr = d.execute(replay, lease=lease, focus=focus)
        self.assertEqual((rr.status, rr.reason), ("rejected", "USED_CONFIRM"))
        self.assertEqual(len(sink), 1)
        protected = FakeTarget("main")
        guarded, _, writes, _, _ = driver_for(
            protected, ctx={"slot_kind": "main", "automation_active": True,
                            "principal": "main-user", "role": "owner"})
        refusal = guarded.execute(action(protected, "unsafe", cmd="take_loan", args=args, tick=5),
                                 lease=leases(protected)[0], focus=leases(protected)[1])
        self.assertEqual((refusal.status, refusal.reason), ("rejected", "CONSENT_MAIN_SAVE"))
        self.assertEqual(writes, [])

    def test_unknown_command_and_authority_payload_never_enter_sink(self):
        target = FakeTarget("unknown")
        d, ex, sink, _, _ = driver_for(target)
        invalid = action(target, "unknown", cmd="secret_action", args={}, tick=4)
        self.assertEqual(d.execute(invalid, lease=leases(target)[0], focus=leases(target)[1]).reason,
                         "ACTION_UNAVAILABLE")
        forged = action(target, "forge", cmd="take_loan",
                        args={"target_id": "co", "amount": 5, "consent_token": "fake"}, tick=5)
        self.assertEqual(d.execute(forged, lease=leases(target)[0], focus=leases(target)[1]).status,
                         "rejected")
        self.assertEqual((sink, ex.next_seq, d.next_seq), ([], 1, 1))

    def test_frame_observation_owns_readonly_rgb_and_stale_frame_refuses(self):
        target = FakeTarget("frames")
        arr = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
        arr.setflags(write=False)
        frame = FrameRef(target.handle, 7, 13, 2, 2, arr,
                         hashlib.sha256(arr.tobytes()).hexdigest())
        obs = Observation(target.handle, 7, 13, 1_000_000, copy.deepcopy(target.base), frame)
        d, _, sink, _, _ = driver_for(target)
        req = action(target, "obs", observation=obs)
        self.assertEqual(d.execute(req, lease=leases(target)[0], focus=leases(target)[1]).status,
                         "pending")
        self.assertEqual(len(sink), 1)
        bad_target = FakeTarget("frames-invalid")
        d2, _, bad_sink, _, _ = driver_for(bad_target)
        writable = np.zeros((2, 2, 3), dtype=np.uint8)
        bad_frame = FrameRef(bad_target.handle, 7, 13, 2, 2, writable,
                             hashlib.sha256(writable.tobytes()).hexdigest())
        # Bypass FrameRef's defensive snapshot to exercise the driver's refusal.
        object.__setattr__(bad_frame, "rgb", writable)
        bad_obs = Observation(bad_target.handle, 7, 13, 1_000_000,
                              copy.deepcopy(bad_target.base), bad_frame)
        self.assertEqual(d2.execute(action(bad_target, "bad", observation=bad_obs),
                                    lease=leases(bad_target)[0], focus=leases(bad_target)[1]).reason,
                         "FRAME_BINDING")
        self.assertEqual(bad_sink, [])

    def test_split_t2_chunk_retains_pending_bytes_and_exact_tick_stamps(self):
        target = FakeTarget("split")
        d, _, _, _, _ = driver_for(target)
        req = action(target, "split-request")
        d.execute(req, lease=leases(target)[0], focus=leases(target)[1])
        raw = fixture_encode([engine_outcome(1, 4, 4, target.basis)])
        first = FakeT2(req, [], data=raw[:17])
        self.assertEqual(d.confirm_completion(req, first).status, "pending")
        self.assertEqual(d.pending_bytes, raw[:17])
        self.assertEqual(len(d.validated_batches), 0)
        second = FakeT2(req, [], data=raw[17:])
        self.assertEqual(d.confirm_completion(req, second).status, "applied")
        self.assertEqual((d.pending_bytes, d.tick_trail[0]["requested_tick"]), (b"", 4))

    def test_oversized_complete_t2_batch_is_protocol_error_not_truncation(self):
        target = FakeTarget("oversize")
        d, _, _, locks, _ = driver_for(target)
        req = action(target, "oversized")
        d.execute(req, lease=leases(target)[0], focus=leases(target)[1])
        records = [engine_outcome(1, 4, 4, target.basis, order_seq_id=i + 1)
                   for i in range(25)]
        with self.assertRaisesRegex(GodotProtocolError, "oversized"):
            d.confirm_completion(req, FakeT2(req, records))
        self.assertEqual(len(d.tick_trail), 0)
        self.assertTrue(locks.owners)

    def test_tick_trail_backpressure_never_drops_old_timestamps(self):
        target = FakeTarget("backpressure")
        d, _, _, _, _ = driver_for(target)
        lease, focus = leases(target)
        for n in range(24):
            req = action(target, f"a-{n}")
            self.assertEqual(d.execute(req, lease=lease, focus=focus).status, "pending")
            label = f"state-{n}"
            _, _, new_basis = fixture_state(label)
            ack = FakeT2(req, [engine_outcome(n + 1, 4, 4, new_basis,
                                              order_seq_id=n + 1)])
            self.assertEqual(d.confirm_completion(req, ack).status, "applied")
            target.set_state(label, tick=4)
            target.frame_seq += 1
        self.assertEqual(len(d.tick_trail), 24)
        blocked = d.execute(action(target, "blocked"), lease=lease, focus=focus)
        self.assertEqual(blocked.reason, "TICK_TRAIL_BACKPRESSURE")
        trail = d.drain_tick_trail()
        self.assertEqual([r["order_seq_id"] for r in trail], list(range(1, 25)))
        self.assertEqual(len(d.tick_trail), 0)
        self.assertEqual(d.execute(action(target, "next"), lease=lease, focus=focus).status,
                         "pending")

    def test_actor_focus_scheduler_resource_alias_and_expired_lease(self):
        shared = FakeLocks()
        cdp_handle = Handle("capsule", "cap-4", 1)
        cdp_lease = ActorLease(cdp_handle, "cap-worker", 1, b"cap", 1_000)
        claim = shared.try_acquire("display:primary", cdp_lease)
        target = FakeTarget("aliased-screen", resource="display:primary")
        d, _, sink, _, _ = driver_for(target, locks=shared)
        req = action(target, "alias")
        self.assertEqual(d.execute(req, lease=leases(target)[0], focus=leases(target)[1]).reason,
                         "ACTOR_BUSY")
        shared.release("display:primary", claim)
        expired = ActorLease(target.handle, "worker", 2, b"expire", 5)
        self.assertEqual(d.execute(req, lease=expired, focus=None).reason, "ACTOR_LEASE_INVALID")
        self.assertEqual(d.execute(req, lease=leases(target)[0], focus=None).reason, "FOCUS_DENIED")
        self.assertEqual(sink, [])

    def test_offline_no_disk_network_or_process_on_public_execution_paths(self):
        target = FakeTarget("no-io")
        d, _, _, _, _ = driver_for(target)
        req = action(target, "pure")
        _, _, digest = fixture_state("pure-after")
        outcome = engine_outcome(1, 4, 4, digest)
        def forbid(*a, **kw):
            raise AssertionError("forbidden file, socket or subprocess in offline test")
        lease, focus = leases(target)
        with patch.object(builtins, "open", forbid), patch.object(Path, "open", forbid), \
             patch.object(socket, "socket", forbid), patch.object(subprocess, "Popen", forbid):
            self.assertEqual(d.execute(req, lease=lease, focus=focus).status, "pending")
            self.assertEqual(d.confirm_completion(req, FakeT2(req, [outcome])).status, "applied")

    def test_explicit_structural_dependencies_work_without_offline_flag(self):
        target = FakeTarget("injected-structural-contract")
        target.offline_fixture = False
        d, _, sink, _, _ = driver_for(target)
        req = action(target, "structural")
        self.assertEqual(d.execute(req, lease=leases(target)[0],
                                   focus=leases(target)[1]).status, "pending")
        self.assertEqual(len(sink), 1)
        _, _, next_basis = fixture_state("structural-complete")
        self.assertEqual(d.confirm_completion(req, FakeT2(req, [
            engine_outcome(1, 4, 4, next_basis)])).status, "applied")

    def test_incomplete_injected_contract_refuses_before_dispatch(self):
        target = FakeTarget("missing-required-method")
        target.offline_fixture = False
        with self.assertRaises(PrerequisiteUnavailable):
            GodotWindowDriver(target, executor_for([]), locks=object(),
                              focus_policy=FakeFocus(), writer_fence=FakeFence(),
                              clock=lambda: 10)


if __name__ == "__main__":
    unittest.main()
