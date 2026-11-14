"""J7 offline gates: real guarded API, aliases, STOP, recovery, lane isolation."""
import builtins
from dataclasses import replace
import hashlib
import importlib
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np

from wender.act.target_lock_registry import Handle, TargetBusy, TargetLockRegistry
from wender.state.schema import ActionRequest, FrameRef, Observation, Receipt
from wender.act.holdings_dispatch import (
    ObservedTarget, LaneAction, dispatch_guarded, dispatch_batch,
    acknowledge_outcome, match_holdings_outcome, DispatchRefused,
)


def _session_registry_module():
    return importlib.import_module("wender.perceive.session_registry")


class FakeClock:
    def __init__(self):
        self.ns = 100

    def __call__(self):
        return self.ns


class FakeRegistry:
    """Thin alias-aware adapter around the real SessionRegistry."""

    def __init__(self, clock):
        sr = _session_registry_module()
        self.inner = sr.SessionRegistry(clock=clock)
        self.aliases = {}

    def register(self, kind, canonical_id, transport, alias, *, epoch=7):
        handle = self.inner.register(kind, canonical_id, transport, owned=False)
        for _ in range(epoch):
            self.inner.advance_epoch(handle)
        self.aliases[(handle.kind, handle.canonical_id)] = alias
        return handle

    def get(self, handle):
        try:
            return self.inner.resolve(handle)
        except Exception:
            return None

    def resolve(self, handle):
        return self.inner.resolve(handle)

    def canonical_resource(self, handle):
        return self.aliases.get((handle.kind, handle.canonical_id))

    def admit_observation(self, handle, seq, pixels, *, state, dpr_ppm=1000000):
        frame = self.inner.admit_frame(handle, seq, pixels)
        return self.inner.admit_observation(handle, frame, state=state, dpr_ppm=dpr_ppm)


class FakeCDP:
    def __init__(self):
        self.calls = []

    def send(self, request, record, observation):
        self.calls.append((request.action_id, observation.frame_seq))
        return Receipt(request.action_id, request.handle, "accepted", None)


class FakeBrokeredIPC:
    def __init__(self):
        self.calls = []

    def send(self, request, record, observation):
        self.calls.append((request.action_id, observation.frame_seq))
        return Receipt(request.action_id, request.handle, "queued", None)


class ReverseRunner:
    def map(self, fn, jobs):
        return iter(reversed([fn(job) for job in jobs]))


class HalfCrashRunner:
    def map(self, fn, jobs):
        jobs = list(jobs)
        yield fn(jobs[0])
        raise RuntimeError("lane runner exited")


class TestHoldingsDispatch(unittest.TestCase):
    def setUp(self):
        sr = _session_registry_module()
        self.clock = FakeClock()
        self.cdp = FakeCDP()
        self.ipc = FakeBrokeredIPC()
        self.registry = FakeRegistry(self.clock)
        self.tab = self.registry.register("cdp_tab", "tab-A", self.cdp, "cdp-A")
        self.sibling = self.registry.register("cdp_tab", "tab-B", self.cdp, "cdp-B")
        self.window = self.registry.register("godot_window", "game-A", self.ipc, "game-window-A")
        self.capsule = self.registry.register("capsule", "capsule-A", self.ipc, "game-window-A")
        self.stream_spec = sr.StreamSpec(width=3, height=2, slots=2, quota_bytes=64)
        self.stream_token = sr.StreamToken(handle=self.window, epoch=7)
        self.ticket = sr.Ticket(action_id="pause-1", handle=self.tab)
        self.locks = TargetLockRegistry(self.registry, clock=self.clock)
        self.other_locks = TargetLockRegistry(self.registry, clock=self.clock)
        self.observation_seq = 12
        self.stopped = False
        self.scheduled = True
        self.admitted = True  # synthetic prior-wave receipt validation, VM only
        self.focused = True
        self.ipc_background_safe = True
        self.authorized = True
        self.sent = []
        self.seen = []
        self.procfs = "Name:\tfixture\nState:\tS (sleeping)\n"
        self.cgroup = "0::/vm/offline-fixture\n"

    def request(self, handle=None, *, action_id="pause-1", seq=3, tick=15):
        return ActionRequest(handle or self.tab, action_id, 7,
                             {"tick": tick, "seq": seq, "cmd": "pause", "args": {}},
                             1, 4000)

    def observe(self, handle):
        kind = "cdp" if handle.kind == "cdp_tab" else "godot"
        self.observation_seq += 1
        pixels = np.full((2, 3, 3), 7, dtype=np.uint8)
        pixels.flags.writeable = False
        obs = self.registry.admit_observation(
            handle,
            self.observation_seq,
            pixels,
            state={"source": {"kind": kind}, "nodes": [],
                   "viewport": {"dpr_ppm": 1000000}},
            dpr_ppm=1000000,
        )
        self.seen.append((kind, handle.canonical_id))
        return ObservedTarget(obs, self.clock.ns)

    def send(self, request, record, observation):
        self.sent.append(request.action_id)
        if request.handle.kind == "cdp_tab":
            return self.cdp.send(request, record, observation)
        return self.ipc.send(request, record, observation)

    def bindings(self):
        return dict(locks=self.locks, resolve=self.registry.get, observe=self.observe,
                    authorize=lambda *args: self.authorized,
                    schedule=lambda *args: self.scheduled,
                    admit=lambda *args: self.admitted,
                    send=self.send, clock=self.clock,
                    stop=lambda: self.stopped,
                    focus=lambda *args: self.focused,
                    background_safe=lambda *args: self.ipc_background_safe,
                    lease_ttl_ns=400, max_observation_age_ns=50, owner="fixture-actor")

    def dispatch(self, request=None, *, source_policy="cdp", **overrides):
        args = self.bindings()
        args.update(overrides)
        return dispatch_guarded(request or self.request(), source_policy=source_policy,
                                submission_seq=8, **args)

    def outcome(self, request=None, *, order="episode-1", **updates):
        request = request or self.request()
        row = {"seq": request.payload["seq"], "requested_tick": request.payload["tick"],
               "applied_tick": request.payload["tick"] + 1, "order_seq_id": order,
               "status": "applied", "reason": "ok", "state_sha256": "a" * 64}
        row.update(updates)
        return row

    def ack(self, result, outcome=None, *, quiescent=True):
        return acknowledge_outcome(result, outcome or self.outcome(result.request),
                                   locks=self.locks, expected_order_seq_id="episode-1",
                                   quiescent=lambda *args: quiescent)

    def test_healthy_cdp_and_brokered_ipc_with_separate_source_provenance(self):
        cdp = self.dispatch()
        ipc = self.dispatch(self.request(self.window, action_id="game-pause"),
                            source_policy="brokered_ipc")
        self.assertEqual(self.sent, ["pause-1", "game-pause"])
        self.assertEqual(self.seen, [("cdp", "tab-A"), ("cdp", "tab-A"),
                                     ("godot", "game-A"), ("godot", "game-A")])
        self.assertEqual([x.event for x in cdp.events], ["OBSERVED", "ELIGIBLE", "DISPATCHED"])
        self.assertTrue(cdp.pending)
        self.assertFalse(cdp.acknowledged)
        self.assertEqual(cdp.events[-1].receipt_ref, "pause-1")
        self.assertEqual([x.event for x in ipc.events][-1], "DISPATCHED")
        self.assertTrue(self.ack(cdp).acknowledged)
        self.assertTrue(self.ack(ipc).acknowledged)

    def test_alias_collision_across_window_and_capsule_and_positive_sibling(self):
        first = self.dispatch(self.request(self.window, action_id="window"),
                              source_policy="brokered_ipc")
        collision = self.dispatch(self.request(self.capsule, action_id="capsule"),
                                  source_policy="brokered_ipc")
        sibling = self.dispatch(self.request(self.sibling, action_id="sibling"))
        self.assertEqual(self.sent, ["window", "sibling"])
        self.assertEqual(collision.events[-1].event, "DROPPED")
        self.assertEqual(collision.events[-1].reason, "TARGET_BUSY")
        self.assertTrue(sibling.pending)
        self.ack(first)
        self.ack(sibling)
        after = self.dispatch(self.request(self.capsule, action_id="after"),
                              source_policy="brokered_ipc")
        self.assertTrue(after.sent)
        self.ack(after)

    def test_focus_scheduler_and_background_capabilities_are_required(self):
        cases = (("focus", "cdp", self.request()),
                 ("schedule", "cdp", self.request()),
                 ("background_safe", "brokered_ipc", self.request(self.window)))
        for missing, source, req in cases:
            with self.subTest(missing=missing):
                outcome = self.dispatch(req, source_policy=source, **{missing: None})
                self.assertFalse(outcome.sent)
                self.assertEqual(outcome.events[-1].reason, "AUSENTE:" + missing)
        self.assertEqual(self.sent, [])

    def test_conductor_admission_missing_or_unverified_blocks_send(self):
        absent = self.dispatch(admit=None)
        self.assertEqual(absent.events[-1].reason, "AUSENTE:admit")
        self.admitted = False
        denied = self.dispatch()
        self.assertEqual(denied.events[-1].reason, "REFUSED:conductor_admission")
        self.assertEqual(self.sent, [])

    def test_explicit_scheduler_denial_and_missing_focus_do_not_default_to_send(self):
        self.scheduled = False
        rejected = self.dispatch()
        self.assertEqual(rejected.events[-1].reason, "REFUSED:scheduler")
        self.scheduled = True
        self.focused = False
        rejected = self.dispatch()
        self.assertEqual(rejected.events[-1].reason, "REFUSED:focus")
        self.assertEqual(self.sent, [])

    def test_stop_before_acquire_and_during_final_fence_prevents_send(self):
        self.stopped = True
        early = self.dispatch()
        self.assertEqual(early.events[-1].event, "STOPPED")
        self.assertEqual(self.sent, [])
        self.stopped = False
        calls = []

        def stopping_schedule(*args):
            calls.append(True)
            if len(calls) == 2:
                self.stopped = True
            return True

        fenced = self.dispatch(schedule=stopping_schedule)
        self.assertEqual(fenced.events[-1].event, "QUARANTINED")
        self.assertEqual(fenced.events[-1].reason, "STOP")
        self.assertEqual(self.sent, [])
        with self.assertRaises(TargetBusy):
            self.other_locks.acquire(self.tab, "other", expires_ns=900)
        self.locks.reconcile_quarantine(self.tab, acknowledged=True)

    def test_stop_just_before_mark_releases_live_unsent_lease(self):
        state = {"n": 0}
        def stopping():
            state["n"] += 1
            # two initial fence calls, then pre-mark check
            return state["n"] == 3
        result = self.dispatch(stop=stopping)
        self.assertEqual(result.events[-1].event, "STOPPED")
        self.assertFalse(result.pending)
        self.assertFalse(self.sent)
        lease = self.other_locks.acquire(self.tab, "later", expires_ns=900)
        self.other_locks.release(lease)

    def test_sent_lease_is_not_reaped_by_clock_expiry_without_ack(self):
        result = self.dispatch()
        self.clock.ns = 3000
        with self.assertRaises(TargetBusy):
            self.other_locks.acquire(self.tab, "late", expires_ns=3500)
        self.locks.reconcile_quarantine(self.tab, acknowledged=False)
        with self.assertRaises(TargetBusy):
            self.other_locks.acquire(self.tab, "still-late", expires_ns=3500)
        self.ack(result)
        lease = self.other_locks.acquire(self.tab, "after-ack", expires_ns=3500)
        self.other_locks.release(lease)

    def test_stale_generation_and_epoch_refuse_during_final_fence(self):
        for mutation in ("generation", "epoch"):
            with self.subTest(mutation=mutation):
                self.setUp()
                calls = {"n": 0}
                def mutating_resolve(handle):
                    calls["n"] += 1
                    rec = self.registry.get(handle)
                    if calls["n"] == 2:
                        if mutation == "generation":
                            return SimpleNamespace(handle=replace(handle, generation=20),
                                                   epoch=7, closed=False)
                        return SimpleNamespace(handle=handle, epoch=8, closed=False)
                    return rec
                result = self.dispatch(resolve=mutating_resolve)
                self.assertEqual(result.events[-1].event, "QUARANTINED")
                self.assertEqual(self.sent, [])
                self.locks.reconcile_quarantine(self.tab, acknowledged=True)

    def test_explicit_fresh_observation_and_truthful_provenance(self):
        def stale(handle):
            base = self.observe(handle)
            return ObservedTarget(base.observation, self.clock.ns - 100)
        self.clock.ns = 300
        result = self.dispatch(observe=stale)
        self.assertEqual(result.events[-1].reason, "STALE:observation_age")
        self.assertFalse(result.pending)
        def relabeled(handle):
            obs = self.observe(handle)
            return ObservedTarget(replace(obs.observation,
                state={"source": {"kind": "godot"}, "nodes": []}), obs.captured_ns)
        result = self.dispatch(observe=relabeled)
        self.assertEqual(result.events[-1].reason, "INVALID:source_provenance")
        self.assertFalse(self.sent)

    def test_invalid_frame_binding_and_authorization_fence(self):
        def broken(handle):
            pair = self.observe(handle)
            obs = pair.observation
            frame = replace(obs.frame, frame_seq=obs.frame_seq + 1)
            return ObservedTarget(replace(obs, frame=frame), pair.captured_ns)
        result = self.dispatch(observe=broken)
        self.assertEqual(result.events[-1].reason, "STALE:frame_binding")

        def broken_structural(handle):
            pair = self.observe(handle)
            obs = pair.observation
            frame = replace(obs.frame, frame_seq=obs.frame_seq + 1)
            object.__setattr__(obs, "frame", frame)
            return ObservedTarget(obs, pair.captured_ns)
        result_struct = self.dispatch(observe=broken_structural)
        self.assertEqual(result_struct.events[-1].reason, "STALE:frame_binding")

        self.authorized = False
        result = self.dispatch()
        self.assertEqual(result.events[-1].reason, "REFUSED:authorization")
        self.assertFalse(self.sent)

    def test_callback_denial_just_before_send_quarantines(self):
        calls = {"n": 0}
        def authorize(*args):
            calls["n"] += 1
            return calls["n"] == 1
        result = self.dispatch(authorize=authorize)
        self.assertEqual(result.events[-1].event, "QUARANTINED")
        self.assertEqual(result.events[-1].reason, "REFUSED:authorization")
        self.assertEqual(self.sent, [])
        self.locks.reconcile_quarantine(self.tab, acknowledged=True)

    def test_throwing_transport_is_quarantined_without_leaking_exception(self):
        def throws(*args):
            raise RuntimeError("RAW_RECEIPT_PRIVATE_PIXEL_BYTES")
        result = self.dispatch(send=throws)
        self.assertEqual(result.events[-1].event, "QUARANTINED")
        self.assertEqual(result.events[-1].reason, "CALLBACK_ERROR:RuntimeError")
        self.assertNotIn("RAW_RECEIPT", str(result.events))
        with self.assertRaises(TargetBusy):
            self.other_locks.acquire(self.tab, "dup", expires_ns=900)
        self.locks.reconcile_quarantine(self.tab, acknowledged=True)

    def test_matched_outcomes_and_positive_quiescence_are_both_mandatory(self):
        result = self.dispatch()
        self.assertTrue(match_holdings_outcome(result.request, self.outcome(),
                                               expected_order_seq_id="episode-1"))
        cases = (dict(seq=4), dict(requested_tick=18), dict(order_seq_id="wrong"),
                 dict(status="rejected"), dict(state_sha256="NOT-A-DIGEST"),
                 dict(applied_tick=0))
        for mutation in cases:
            with self.subTest(mutation=mutation):
                not_ack = self.ack(result, self.outcome(**mutation))
                self.assertTrue(not_ack.pending)
                self.assertFalse(not_ack.acknowledged)
                self.assertEqual(not_ack.events[-1].event, "QUARANTINED")
        not_quiet = self.ack(result, quiescent=False)
        self.assertTrue(not_quiet.pending)
        self.assertEqual(not_quiet.events[-1].reason, "NOT_QUIESCENT")
        acknowledged = self.ack(result)
        self.assertTrue(acknowledged.acknowledged)
        self.assertFalse(acknowledged.pending)
        self.assertEqual(acknowledged.events[-1].event, "ACKNOWLEDGED")
        with self.assertRaises(DispatchRefused):
            self.ack(acknowledged)

    def test_batch_reverse_compute_completion_preserves_canonical_order(self):
        actions = [LaneAction(self.request(self.sibling, action_id="B"), "cdp", 2),
                   LaneAction(self.request(self.window, action_id="G"), "brokered_ipc", 1),
                   LaneAction(self.request(self.tab, action_id="A"), "cdp", 2)]
        results = dispatch_batch(actions, readiness=lambda item: True,
                                 runner=ReverseRunner(), **self.bindings())
        self.assertEqual([r.request.action_id for r in results], ["G", "A", "B"])
        self.assertEqual(self.sent, ["G", "A", "B"])
        for result in results:
            self.ack(result)

    def test_batch_backpressure_and_runner_crash_isolate_only_affected_lane(self):
        actions = [LaneAction(self.request(self.window, action_id="G"), "brokered_ipc", 0),
                   LaneAction(self.request(self.tab, action_id="A"), "cdp", 1),
                   LaneAction(self.request(self.sibling, action_id="B"), "cdp", 2)]
        def check(item):
            if item.request.action_id == "A":
                raise BufferError("backpressure")
            return True
        results = dispatch_batch(actions, readiness=check, runner=HalfCrashRunner(),
                                 **self.bindings())
        self.assertEqual([r.events[-1].event for r in results],
                         ["DISPATCHED", "DROPPED", "DISPATCHED"])
        self.assertEqual(self.sent, ["G", "B"])
        self.assertEqual(results[1].events[-1].reason, "CALLBACK_ERROR:BufferError")
        self.ack(results[0])
        self.ack(results[2])

    def test_no_disk_io_or_pixel_receipt_leakage(self):
        with patch.object(builtins, "open", side_effect=AssertionError("disk write")):
            result = self.dispatch()
            outcome = self.ack(result)
        text = repr(outcome.events)
        self.assertNotIn("uint8", text)
        self.assertNotIn("rgb", text)
        self.assertNotIn("state_sha256", text)
        self.assertNotIn("nonce", text)
        self.assertEqual(set(vars(outcome.events[-1])),
            {"action_id", "event", "submission_seq", "reason", "receipt_ref",
             "generation", "epoch", "frame_seq"})

    def test_offline_fixture_metadata_not_host_measurement(self):
        sr = _session_registry_module()
        self.assertEqual(sr.SUPPORTED_KINDS,
                         frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"}))
        self.assertGreaterEqual(self.tab.generation, 1)
        self.assertEqual(self.stream_spec.slots, 2)
        self.assertEqual(self.stream_token.handle, self.window)
        self.assertEqual(self.ticket.handle, self.tab)
        self.assertFalse(match_holdings_outcome(self.request(), {"ticket": self.ticket.action_id},
                                                expected_order_seq_id="episode-1"))
        zero_gen_req = SimpleNamespace(
            handle=SimpleNamespace(kind="cdp_tab", canonical_id="tab-A", generation=0),
            action_id="bad-gen", epoch=7,
            payload={"tick": 1, "seq": 1, "cmd": "pause", "args": {}},
            cost=1, deadline_ns=4000,
        )
        rejected = self.dispatch(zero_gen_req)
        self.assertEqual(rejected.events[-1].event, "DROPPED")
        self.assertEqual(rejected.events[-1].reason, "INVALID:generation")
        self.assertIn("fixture", self.procfs)
        self.assertIn("/vm/offline-fixture", self.cgroup)
        self.assertGreater(self.clock.ns, 0)
        result = self.dispatch()
        self.assertTrue(result.sent)
        self.ack(result)


if __name__ == "__main__":
    unittest.main()
