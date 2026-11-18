import inspect
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
import sys
from threading import Barrier, Event
from textwrap import dedent
import unittest
from unittest.mock import Mock, patch

from jsonschema import ValidationError

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from perception.demand import DemandCoordinator, DemandError
from recorder.timeline import TimelinePosition, TimelineRecorder


def demand(name, interest="VERIFIED", *, credits=2, **changes):
    return dict({"schema": "vp.demand/v1", "surface_id": "surface", "demand_id": name,
                 "consumer": name, "interest": interest, "purpose": "recording" if name == "recorder" else name,
                 "lease": name + "-lease", "credits": credits}, **changes)


def service(owner, interest="VERIFIED", budget=1, timeout=0):
    return owner.claim(interest, budget=budget, timeout=timeout)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.drain = Mock(return_value=True)
        self.owner = DemandCoordinator("surface", interests=("VERIFIED", "DISPLAY"),
                                       authorize=lambda r: r["lease"] == r["consumer"] + "-lease",
                                       drain=self.drain)

    def test_schema_surface_lease_and_compatibility(self):
        for row in (demand("x", credits=-1), demand("x", extra=True), demand("x", "unknown")):
            with self.assertRaises(ValidationError), self.owner.register(row):
                self.fail("invalid schema admitted")
        for row in (demand("x", surface_id="other"), demand("x", lease="other-lease")):
            with self.assertRaises(DemandError), self.owner.register(row):
                self.fail("foreign subscription admitted")
        display = DemandCoordinator("surface", interests=["DISPLAY"], authorize=lambda r: True,
                                    drain=self.drain)
        with self.assertRaisesRegex(DemandError, "incompatible"), display.register(demand("agent")):
            self.fail("display-only producer admitted verified work")
        self.assertEqual(self.owner.snapshot()["state"], "idle")
        self.drain.assert_not_called()

    def test_per_interest_credits_and_opaque_leases(self):
        row = demand("agent")
        with self.owner.register(row) as agent, self.owner.register(demand("tile", "DISPLAY")) as tile:
            row["credits"] = 90
            self.owner.publish(agent, 1, "a")
            self.owner.publish(tile, 1, "b")
            snapshot = self.owner.snapshot()
            self.assertEqual(snapshot["available"], {"DISPLAY": 1, "VERIFIED": 1})
            self.assertEqual([r["lease"] for r in snapshot["demands"]], ["agent-lease", "tile-lease"])
            snapshot["demands"][0]["credits"] = 0
            for interest in ("VERIFIED", "DISPLAY"):
                work, = service(self.owner, interest)
                self.assertEqual(work.reference, "a" if interest == "VERIFIED" else "b")
                self.owner.complete(work)
            self.assertEqual(self.owner.snapshot()["available"]["VERIFIED"], 2)
        self.drain.assert_called_once_with()

    def test_duplicate_zero_credit_and_stale_handle_refuse(self):
        with self.owner.register(demand("agent", credits=0)) as old:
            with self.assertRaisesRegex(DemandError, "credits exhausted"):
                self.owner.publish(old, 1, "zero")
            with self.assertRaises(DemandError), self.owner.register(demand("agent")):
                self.fail("duplicate subscription admitted")
        with self.owner.register(demand("agent", credits=1)) as new:
            with self.assertRaisesRegex(DemandError, "NO_LEASE"):
                self.owner.publish(old, 1, "reused")
            self.owner.publish(new, 1, "one")
            with self.assertRaisesRegex(DemandError, "sequence"):
                self.owner.publish(new, 1, "duplicate")
            with self.assertRaisesRegex(DemandError, "credits exhausted"):
                self.owner.publish(new, 2, "no capacity")
            work, = service(self.owner)
            self.owner.complete(work)
            self.owner.publish(new, 2, "credit restored")
            self.owner.complete(service(self.owner)[0])
            with self.assertRaises(DemandError):
                self.owner.complete(work)

    def recording_oracle(self):
        recorder = TimelineRecorder(surface_id="surface", capsule_id="capsule", incarnation="inc",
                                    register=self.owner.register, verify_ram_frame=lambda *_: False)
        with recorder.recording(demand("recorder")):
            with self.owner.register(demand("agent")):
                self.assertTrue(recorder.add_cdp(TimelinePosition(1, "capsule", "inc"), text="before"))
            self.assertEqual(self.drain.call_count, 0, "agent cancellation stopped the recording producer")
            with self.assertRaisesRegex(DemandError, "outstanding obligations"):
                self.owner.release()
            self.assertTrue(recorder.add_cdp(TimelinePosition(2, "capsule", "inc"), text="after"))
            self.assertEqual([r["demand_id"] for r in self.owner.snapshot()["demands"]], ["recorder"])
            self.assertEqual([r["mono_ns"] for r in recorder.snapshot()["events"]], [1, 2])
        self.drain.assert_called_once_with()

    def test_recording_continues_after_agent_cancellation(self):
        self.recording_oracle()

    def test_display_remains_after_verified_consumer_leaves(self):
        with self.owner.register(demand("tile", "DISPLAY")) as tile:
            with self.owner.register(demand("agent")):
                pass
            self.drain.assert_not_called()
            self.owner.publish(tile, 1, "tile-frame")
            self.assertEqual(service(self.owner), ())
            self.owner.complete(service(self.owner, "DISPLAY")[0])
        self.drain.assert_called_once_with()

    def test_cancel_retains_queue_delivery_and_borrow_charge(self):
        with self.owner.register(demand("agent")) as handle:
            self.owner.publish(handle, 1, "frame")
        retained = self.owner.snapshot()["demands"][0]
        self.assertFalse(retained["active"])
        self.assertEqual(retained["outstanding"], 1)
        with self.assertRaisesRegex(DemandError, "outstanding obligations"):
            self.owner.release()
        work, = service(self.owner)
        with self.owner.borrow(work, 4096):
            with self.assertRaises(DemandError):
                self.owner.complete(work)
            with self.assertRaises(DemandError):
                self.owner.release()
            self.assertEqual(self.owner.snapshot()["borrowed_bytes"], 4096)
            self.drain.assert_not_called()
        self.assertEqual(self.owner.snapshot()["borrowed_bytes"], 0)
        self.drain.assert_not_called()
        self.owner.complete(work)
        self.drain.assert_called_once_with()
        self.assertEqual(self.owner.snapshot()["state"], "idle")

    def test_recording_end_with_borrow_pinned_requires_delivery_ack(self):
        registration = self.owner.register(demand("recorder", credits=1))
        handle = registration.__enter__()
        self.owner.publish(handle, 1, "recorded-frame")
        work, = service(self.owner)
        with self.owner.borrow(work, 512):
            registration.__exit__(None, None, None)
            snapshot = self.owner.snapshot()
            self.assertFalse(snapshot["demands"][0]["active"])
            self.assertEqual(snapshot["borrowed_bytes"], 512)
            with self.assertRaisesRegex(DemandError, "outstanding obligations"):
                self.owner.release()
            self.drain.assert_not_called()
        self.drain.assert_not_called()
        self.owner.complete(work)
        self.drain.assert_called_once_with()

    def test_foreign_handles_and_claims_cannot_spend_or_ack(self):
        other = DemandCoordinator("other", interests=["VERIFIED"],
                                  authorize=lambda _: True, drain=lambda: True)
        with self.owner.register(demand("recorder")) as ours:
            with other.register(demand("recorder", surface_id="other")) as theirs:
                with self.assertRaisesRegex(DemandError, "NO_LEASE"):
                    self.owner.publish(theirs, 1, "foreign")
                self.assertIsNone(self.owner.publish(ours, 1, "delivery"))
                other.publish(theirs, 1, "delivery")
                local, = service(self.owner)
                foreign, = service(other)
                self.assertEqual((local.consumer, local.purpose, local.lease),
                                 ("recorder", "recording", "recorder-lease"))
                with self.assertRaises(DemandError), self.owner.borrow(foreign, 64):
                    self.fail("foreign claim borrowed")
                with self.assertRaises(DemandError):
                    self.owner.complete(foreign)
                self.owner.complete(local)
                other.complete(foreign)

    def publication_oracle(self):
        with self.owner.register(demand("agent")) as handle:
            observed = []
            def observe_notification():
                state = self.owner.snapshot()
                observed.append((state["queued"]["VERIFIED"], state["available"]["VERIFIED"]))
            with patch.object(self.owner._ready["VERIFIED"], "notify_all", observe_notification):
                self.owner.publish(handle, 1, "frame")
            self.assertEqual(observed, [(1, 1)], "notification preceded publication or credit reservation")
            self.owner.complete(service(self.owner)[0])

    def test_credit_and_work_are_published_before_notification(self):
        self.publication_oracle()

    def test_spurious_notification_grants_no_work_or_credit(self):
        with self.owner.register(demand("agent", credits=0)):
            with self.owner._lock:
                self.owner._ready["VERIFIED"].notify_all()
            self.assertEqual(service(self.owner), ())
            self.assertEqual(self.owner.snapshot()["available"]["VERIFIED"], 0)
            with self.assertRaisesRegex(DemandError, "outstanding"):
                self.owner.release()

    def continuation_oracle(self):
        with self.owner.register(demand("agent")) as handle:
            ready = self.owner._ready["VERIFIED"]
            self.owner.publish(handle, 1, "A")
            # Coalesced second notification: there is no edge for B.
            with patch.object(ready, "notify_all", return_value=None):
                self.owner.publish(handle, 2, "B")
            a, = service(self.owner, budget=1)
            self.owner.complete(a)
            with patch.object(ready, "wait", side_effect=AssertionError("parked with admitted work")):
                b, = service(self.owner, budget=1)
            self.assertEqual([a.reference, b.reference], ["A", "B"])
            self.owner.complete(b)
            self.assertEqual(service(self.owner), ())

    def test_budget_exhaustion_and_coalesced_wake_continue_once(self):
        self.continuation_oracle()

    def test_reader_final_recheck_and_lost_notification(self):
        ready = self.owner._ready["VERIFIED"]
        with self.owner.register(demand("agent")) as handle:
            for sequence, drop in enumerate((False, True), start=1):
                at_park = Barrier(2, timeout=3)
                published = Event()
                original_wait = ready.wait
                def park(timeout):
                    at_park.wait()
                    if not drop:
                        return original_wait(timeout)
                    # Model expiry after admission with the hint suppressed. The
                    # barrier controls ordering independently of scheduler speed.
                    self.owner._lock.release()
                    try:
                        self.assertTrue(published.wait(3))
                        return False
                    finally:
                        self.owner._lock.acquire()
                with patch.object(ready, "wait", park), ThreadPoolExecutor(1) as pool:
                    future = pool.submit(service, self.owner, timeout=3)
                    at_park.wait()
                    hint = patch.object(ready, "notify_all") if drop else nullcontext()
                    with hint:
                        self.owner.publish(handle, sequence, "frame")
                    published.set()
                    work, = future.result(timeout=4)
                    self.assertEqual((work.sequence, work.reference), (sequence, "frame"))
                    self.owner.complete(work)

    def test_competing_claims_deliver_once(self):
        with self.owner.register(demand("agent", credits=12)) as handle:
            published = [(n, str(n)) for n in range(1, 13)]
            for sequence, reference in published:
                self.owner.publish(handle, sequence, reference)
            start = Barrier(3, timeout=3)
            def consume():
                start.wait()
                return service(self.owner, budget=8)
            with ThreadPoolExecutor(2) as pool:
                readers = [pool.submit(consume) for _ in range(2)]
                start.wait()
                seen = [w for f in readers for w in f.result()]
            self.assertCountEqual([(w.sequence, w.reference) for w in seen], published)
            for w in seen:
                self.owner.complete(w)

    def test_uncertain_drain_fences_admission_until_retry(self):
        started, finish = Event(), Event()
        def drain():
            started.set()
            self.assertTrue(finish.wait(3))
            return False
        self.drain.side_effect = drain
        scope = self.owner.register(demand("agent"))
        scope.__enter__()
        with ThreadPoolExecutor(1) as pool:
            closing = pool.submit(scope.__exit__, None, None, None)
            try:
                self.assertTrue(started.wait(3))
                with self.assertRaisesRegex(DemandError, "unsettled"), self.owner.register(demand("tile")):
                    self.fail("admitted during drain")
                with self.assertRaisesRegex(DemandError, "unsettled"):
                    self.owner.release()
            finally:
                finish.set()
            with self.assertRaisesRegex(DemandError, "unacknowledged"):
                closing.result()
        self.assertEqual(self.owner.snapshot()["state"], "drain_failed")
        with self.assertRaises(DemandError), self.owner.register(demand("other")):
            self.fail("admitted after failed drain")
        self.drain.side_effect = None
        self.owner.release()
        self.assertEqual(self.owner.snapshot()["state"], "idle")
        self.owner.release()
        self.assertEqual(self.drain.call_count, 2)

    @contextmanager
    def mutation(self, method_name, before, after):
        method = getattr(DemandCoordinator, method_name)
        source = dedent(inspect.getsource(method))
        self.assertEqual(source.count(before), 1)
        compiled = {}
        exec(compile(source.replace(before, after), "<P2 mutant>", "exec"), method.__globals__, compiled)
        reached = Mock(wraps=compiled[method_name])
        def run(instance, *args, **kwargs):
            return reached(instance, *args, **kwargs)
        with patch.object(DemandCoordinator, method_name, run):
            yield
        reached.assert_called()

    def test_mutant_notify_before_publish(self):
        append = "self._queues[interest].append(work)"
        notify = "self._ready[interest].notify_all()"
        with self.mutation("publish", append + "\n        " + notify, notify + "\n        " + append):
            with self.assertRaisesRegex(AssertionError, "notification preceded publication"):
                self.publication_oracle()

    def test_mutant_park_without_recheck(self):
        with self.mutation("claim", "self._ready[interest].wait_for(lambda: bool(queue), timeout)",
                           "self._ready[interest].wait(timeout)"):
            with self.assertRaisesRegex(AssertionError, "parked with admitted work"):
                self.continuation_oracle()

    def test_mutant_stop_on_agent_silence(self):
        with self.mutation("_retire_if_settled", "if self._subscriptions:",
                           'if self._subscriptions and sub.record["consumer"] != "agent":'):
            with self.assertRaisesRegex(AssertionError, "agent cancellation stopped"):
                self.recording_oracle()
