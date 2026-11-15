"""PROTO1 Area B controls. No synthetic registry, fence, owner validator or leases."""
from __future__ import annotations

import hashlib
import threading
import unittest

from wender.act.proto1_settle_clock import (
    AdapterInput, BackendProfile, FrameIdentity, RuntimeHandle, SettleConfig,
    SettleCoordinator, SettleRefused, SignalEvidence, SIGNALS,
)


def digest(value):
    # Expected values are built independently of production digest logic.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class VirtualClock:
    def __init__(self):
        self.value = 100
        self.delays = []
        self.mutex = threading.Lock()

    def now(self):
        with self.mutex:
            return self.value

    def advance(self, ns):
        if type(ns) is not int or ns < 0:
            raise AssertionError("invalid scheduling delay")
        with self.mutex:
            self.delays.append(ns)
            self.value += ns

    def set(self, ns):
        with self.mutex:
            self.value = ns


class AreaBControls(unittest.TestCase):
    def setUp(self):
        # Actual producer imports. Missing provider is a hard test error.
        from wender.perceive.session_registry import SessionRegistry
        from wender.act.window_scheduler import WriterFence
        self.clock = VirtualClock()
        self.registry = SessionRegistry(clock=self.clock.now)
        self.transport = object()  # Only a network transport is inert in memory.
        self.handle = self.registry.register("cdp_tab", "tab-one", self.transport, owned=False)
        self.fence = WriterFence(clock=self.clock.now)
        self.fence.bind(self.handle, "cdp:tab-one", epoch=0)
        self.coordinator = SettleCoordinator(
            registry=self.registry, writer_fence=self.fence,
            clock=self.clock.now, pace=self.clock.advance,
        )
        self.frame = FrameIdentity(7, 3, 2, digest("six RGB bytes per pixel"), digest("binding"))
        self.adapter = AdapterInput(
            "run-one", self.handle, 0, digest("binding"), (self.frame,),
            digest("state"), "act-one", digest("trace"), "APPLIED", b"unverified-owner-bytes",
        )
        self.profile = BackendProfile("cdp-full", 1, {name: True for name in SIGNALS})
        self.config = SettleConfig(200, 4, 8, 8, 2, 40)

    def evidence(self, signal, *, quiet=True, count=0, action_id="act-one", frame=None,
                 state=None, handle=None, epoch=0, observed=None, outcome="APPLIED"):
        return SignalEvidence(
            signal, self.handle if handle is None else handle, epoch, action_id,
            digest("binding"), self.frame if frame is None else frame,
            digest("state") if state is None else state, digest("trace"), outcome,
            self.clock.now() if observed is None else observed,
            None if signal == "inflight_work" else quiet,
            count if signal == "inflight_work" else None,
        )

    def snapshot(self, *, quiet=None, inflight=0):
        return {n: self.evidence(n, quiet=(quiet or {}).get(n, True), count=inflight)
                for n in SIGNALS}

    def assert_code(self, code, operation):
        with self.assertRaises(SettleRefused) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)
        self.assertFalse(self.coordinator.is_settled(self.handle, 0))

    def test_positive_independent_signals_real_quiet_window(self):
        # Four independent fixed deadlines, not a precomputed all-quiet oracle.
        due = {"visual": 112, "dom_layout": 120, "scene_tick": 128, "inflight_work": 136}
        snapshots = []

        def read():
            now = self.clock.now()
            values = {n: self.evidence(n, quiet=now >= due[n],
                                       count=0 if now >= due[n] else 1)
                      for n in SIGNALS}
            snapshots.append({n: (s.quiet if n != "inflight_work" else s.inflight_count == 0)
                              for n, s in values.items()})
            return values

        result = self.coordinator.settle(self.adapter, self.profile, self.config, read)
        self.assertEqual((result.action_id, result.source_frame, result.state_sha256),
                         ("act-one", self.frame, digest("state")))
        self.assertEqual(result.outcome, "APPLIED")
        self.assertEqual(result.deadline_ns, 200)
        self.assertGreaterEqual(result.settled_at_ns, 144)
        self.assertTrue(any(not all(x.values()) for x in snapshots))
        self.assertTrue(all(snapshots[-1].values()))
        self.assertTrue(self.coordinator.is_settled(self.handle, 0))
        self.assertTrue(all(type(d) is int and d >= 0 for d in self.clock.delays))
        self.fence.stop(self.handle)
        self.assertFalse(self.coordinator.is_settled(self.handle, 0))

    def test_missing_each_pillar_must_fail(self):
        for missing in sorted(SIGNALS):
            with self.subTest(missing=missing):
                self.assert_code("MISSING_SIGNAL", lambda: self.coordinator.settle(
                    self.adapter, self.profile, self.config,
                    lambda: {n: e for n, e in self.snapshot().items() if n != missing},
                ))

    def test_inapplicable_pillar_must_be_declared_and_not_claimed(self):
        flags = {n: n == "visual" for n in SIGNALS}
        profile = BackendProfile("one-channel", 1, flags)
        self.assert_code("INAPPLICABLE_SIGNAL", lambda: self.coordinator.settle(
            self.adapter, profile, self.config, self.snapshot,
        ))
        self.assert_code("PROFILE_INCOMPLETE", lambda: BackendProfile("x", 1, {"visual": True}))
        self.assert_code("UNKNOWN_EXTENSION_VERSION", lambda: BackendProfile("x", 2, flags))

    def test_missing_or_null_evidence_is_never_quiet(self):
        self.assert_code("EVIDENCE_UNKNOWN", lambda: self.coordinator.settle(
            self.adapter, self.profile, self.config,
            lambda: dict(self.snapshot(), visual=None),
        ))
        self.assert_code("EVIDENCE_UNKNOWN", lambda: self.coordinator.settle(
            self.adapter, self.profile, self.config,
            lambda: dict(self.snapshot(), inflight_work=self.evidence("inflight_work", count=None)),
        ))

    def test_perpetual_inflight_work_and_retries_keep_same_deadline(self):
        cfg = SettleConfig(113, 4, 5, 8, 1, 3)
        self.assert_code("POLL_LIMIT", lambda: self.coordinator.settle(
            self.adapter, self.profile, cfg, lambda: self.snapshot(inflight=1)))
        self.assertLess(self.clock.now(), 113)
        self.clock.set(113)
        self.assert_code("ABSOLUTE_DEADLINE", lambda: self.coordinator.settle(
            self.adapter, self.profile, cfg, self.snapshot))
        self.assertEqual(cfg.deadline_ns, 113)

    def test_stale_generation_action_frame_state_and_epoch(self):
        stale = RuntimeHandle("cdp_tab", "tab-one", self.handle.generation + 1)
        other = AdapterInput("run-one", stale, 0, digest("binding"), (self.frame,),
                             digest("state"), "act-one", digest("trace"), "APPLIED", None)
        self.assert_code("STALE_GENERATION", lambda: self.coordinator.settle(
            other, self.profile, self.config, self.snapshot))
        later_epoch = AdapterInput("run-one", self.handle, 1, digest("binding"), (self.frame,),
                                   digest("state"), "act-one", digest("trace"), "APPLIED", None)
        self.assert_code("STALE_EPOCH", lambda: self.coordinator.settle(
            later_epoch, self.profile, self.config, self.snapshot))
        for field, evidence in (
            ("action", self.evidence("visual", action_id="older-action")),
            ("frame", self.evidence("visual", frame=FrameIdentity(6, 3, 2, digest("pixels"), digest("binding")))),
            ("state", self.evidence("visual", state=digest("old-state"))),
        ):
            with self.subTest(field=field):
                self.assert_code("EVIDENCE_IDENTITY_MISMATCH", lambda: self.coordinator.settle(
                    self.adapter, self.profile, self.config,
                    lambda: dict(self.snapshot(), visual=evidence),
                ))

    def test_only_admitted_and_no_owner_validator(self):
        admitted = AdapterInput("run-one", self.handle, 0, digest("binding"),
                                (self.frame,), digest("state"), "act-one", digest("trace"),
                                "ADMITTED_ONLY", b"no-validation")
        seen = []
        self.assert_code("APPLICATION_NOT_CONFIRMED", lambda: self.coordinator.settle(
            admitted, self.profile, self.config, lambda: seen.append(1) or self.snapshot()))
        self.assertEqual([], seen)
        self.assert_code("OWNER_VALIDATOR_AUSENTE", lambda: self.coordinator.accept_owner_receipt(self.adapter))
        self.assert_code("RECEIPT_UNKNOWN", lambda: self.coordinator.accept_owner_receipt(
            AdapterInput("run-one", self.handle, 0, digest("binding"), (self.frame,),
                         digest("state"), "act-one", digest("trace"), "APPLIED", None)))
        self.assertEqual(self.adapter.owner_receipt, b"unverified-owner-bytes")

    def test_clock_regression_and_fixed_absolute_timeout(self):
        def backward():
            self.clock.set(99)
            return self.snapshot()
        self.assert_code("CLOCK_REGRESSION", lambda: self.coordinator.settle(
            self.adapter, self.profile, self.config, backward))
        self.clock.set(100)
        self.coordinator = SettleCoordinator(registry=self.registry, writer_fence=self.fence,
                                             clock=self.clock.now, pace=self.clock.advance)
        cfg = SettleConfig(104, 4, 8, 8, 0, 5)
        self.assert_code("ABSOLUTE_DEADLINE", lambda: self.coordinator.settle(
            self.adapter, self.profile, cfg, self.snapshot))

    def test_bad_integer_and_digest_types_rejected(self):
        for bad in (True, 1.0, -1):
            with self.subTest(bad=bad):
                self.assert_code("INVALID_INTEGER", lambda: RuntimeHandle("cdp_tab", "x", bad))
                self.assert_code("INVALID_INTEGER", lambda: FrameIdentity(0, bad, 2, digest("a"), digest("b")))
                self.assert_code("INVALID_INTEGER", lambda: SettleConfig(200, bad, 3, 3, 0, 4))
        self.assert_code("INVALID_DIGEST", lambda: FrameIdentity(0, 2, 3, "G" * 64, digest("binding")))
        self.assert_code("INVALID_FRAMES", lambda: AdapterInput(
            "run", self.handle, 0, digest("binding"), (self.frame, self.frame),
            digest("state"), "act", digest("trace"), "APPLIED", None))
        self.assert_code("INVALID_INTEGER", lambda: AdapterInput(
            "run", self.handle, True, digest("binding"), (self.frame,),
            digest("state"), "act", digest("trace"), "APPLIED", None))

    def test_barrier_late_signal_after_stop_no_promotion(self):
        arrived, released = threading.Barrier(2), threading.Barrier(2)
        results, emitted = [], []
        original_deadline = self.config.deadline_ns

        def read():
            arrived.wait(timeout=3)
            released.wait(timeout=3)
            return self.snapshot()

        def worker():
            try:
                results.append(self.coordinator.settle(self.adapter, self.profile, self.config, read))
            except SettleRefused as exc:
                emitted.append(exc.code)

        thread = threading.Thread(target=worker)
        thread.start()
        arrived.wait(timeout=3)
        self.fence.stop(self.handle)   # Real shared WriterFence, not a mocked one.
        released.wait(timeout=3)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["STOPPED"], emitted)
        self.assertEqual([], results)
        self.assertFalse(self.coordinator.is_settled(self.handle, 0))
        self.assertEqual(original_deadline, self.config.deadline_ns)
        self.assert_code("OWNER_VALIDATOR_AUSENTE", lambda: self.coordinator.accept_owner_receipt(self.adapter))

    def test_barrier_late_signal_after_deadline(self):
        arrived, released = threading.Barrier(2), threading.Barrier(2)
        emitted = []
        def read():
            arrived.wait(timeout=3)
            released.wait(timeout=3)
            return self.snapshot()
        def worker():
            try:
                self.coordinator.settle(self.adapter, self.profile, self.config, read)
            except SettleRefused as exc:
                emitted.append(exc.code)
        thread = threading.Thread(target=worker)
        thread.start()
        arrived.wait(timeout=3)
        self.clock.set(self.config.deadline_ns)
        released.wait(timeout=3)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["ABSOLUTE_DEADLINE"], emitted)
        self.assertFalse(self.coordinator.is_settled(self.handle, 0))
        self.assertEqual(self.config.deadline_ns, 200)


if __name__ == "__main__":
    unittest.main()
