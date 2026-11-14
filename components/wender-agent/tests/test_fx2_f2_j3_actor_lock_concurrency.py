"""Tests for ActorLock concurrency, metadata retention, and choose_path extensions.

Fix ID: fx2-f2-executor-actor-chromedriver
"""

import threading
import unittest

from wender.act import actor
from wender.act.actor import ActorBusy, ActorLock, choose_path


class CustomSentinelException(Exception):
    """Custom exception used for exception propagation tests."""

    pass


class CustomReleaseError(RuntimeError):
    """Custom exception raised by instrumented lock primitive on release."""

    pass


class InstrumentedPrimitive:
    """Wrapped lock primitive to hook or fail during release calls."""

    def __init__(self, release_hook=None, raise_on_release=False):
        self._lock = threading.Lock()
        self.release_hook = release_hook
        self.raise_on_release = raise_on_release
        self._in_hook = False

    def acquire(self, blocking=False):
        return self._lock.acquire(blocking=blocking)

    def release(self):
        if self.release_hook and not self._in_hook:
            self._in_hook = True
            try:
                self.release_hook()
            finally:
                self._in_hook = False
        if self.raise_on_release:
            raise CustomReleaseError("simulated primitive release failure")
        self._lock.release()

    @property
    def locked(self):
        return self._lock.locked()


class TestActorLockConcurrency(unittest.TestCase):
    """Tests multi-threaded contention, barriers, events, and release handover."""

    def test_contention_with_barriers_events_and_captured_exceptions(self):
        """Nominal control: Winner holds lock while contenders attempt acquisition and capture ActorBusy."""
        lock = ActorLock()
        lock.acquire("winner_actor")

        num_contenders = 3
        barrier = threading.Barrier(num_contenders + 1)
        all_attempted_event = threading.Event()
        attempt_counter_lock = threading.Lock()
        attempts = [0]
        captured_results = []

        def contender_worker(actor_name):
            barrier.wait(timeout=2.0)
            try:
                lock.acquire(actor_name)
                captured_results.append((actor_name, "acquired", None))
            except Exception as exc:
                captured_results.append((actor_name, "raised", exc))
            finally:
                with attempt_counter_lock:
                    attempts[0] += 1
                    if attempts[0] == num_contenders:
                        all_attempted_event.set()

        threads = [
            threading.Thread(target=contender_worker, args=(f"contender_{i}",))
            for i in range(num_contenders)
        ]
        for t in threads:
            t.start()

        # Release contenders simultaneously while main thread holds lock as winner
        barrier.wait(timeout=2.0)

        # Ensure winner holds lock until all contenders have attempted acquisition
        all_attempted_reached = all_attempted_event.wait(timeout=2.0)
        self.assertTrue(
            all_attempted_reached,
            "All contender threads must attempt acquisition before winner releases",
        )

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Worker thread must finish within bounded timeout")

        self.assertEqual(len(captured_results), num_contenders)
        for actor_name, status, exc in captured_results:
            self.assertEqual(status, "raised")
            self.assertIsInstance(exc, ActorBusy)
            self.assertIn("'winner_actor'", str(exc))

        # Winner releases lock
        lock.release()
        self.assertIsNone(lock.holder)

        # Subsequent acquisition succeeds
        lock.acquire("next_winner")
        self.assertEqual(lock.holder, "next_winner")
        lock.release()

    def test_release_time_handover(self):
        """Nominal control: Lock release enables a waiting contender thread to acquire ownership."""
        lock = ActorLock()
        lock.acquire("winner_1")

        handover_event = threading.Event()
        acquired_event = threading.Event()
        worker_outcome = []

        def waiting_contender():
            handover_event.wait(timeout=2.0)
            try:
                lock.acquire("winner_2")
                worker_outcome.append(lock.holder)
                acquired_event.set()
                lock.release()
            except Exception as exc:
                worker_outcome.append(exc)

        t = threading.Thread(target=waiting_contender)
        t.start()

        lock.release()
        handover_event.set()

        got_signal = acquired_event.wait(timeout=2.0)
        self.assertTrue(got_signal, "Contender thread must acquire lock after winner release")

        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(worker_outcome, ["winner_2"])
        self.assertIsNone(lock.holder)


class TestActorLockMetadataAndFailures(unittest.TestCase):
    """Tests ownership metadata retention, primitive failures, context managers and parameter validation."""

    def test_retained_ownership_metadata_during_release_hook(self):
        """Fix ID: fx2-f2-executor-actor-chromedriver

        Holder metadata must remain set until primitive release completes.
        """
        lock = ActorLock()
        state = {
            "hook_executed": False,
            "holder_during_release": None,
            "busy_exc": None,
        }

        def release_hook():
            state["hook_executed"] = True
            state["holder_during_release"] = lock.holder
            try:
                lock.acquire("contender_b")
            except ActorBusy as exc:
                state["busy_exc"] = exc

        primitive = InstrumentedPrimitive(release_hook=release_hook)
        lock._lock = primitive

        lock.acquire("actor_a")
        self.assertEqual(lock.holder, "actor_a")

        lock.release()

        self.assertTrue(state["hook_executed"])
        self.assertEqual(
            state["holder_during_release"],
            "actor_a",
            "Holder metadata cleared before primitive release completed",
        )
        self.assertIsNotNone(state["busy_exc"])
        self.assertIn("'actor_a'", str(state["busy_exc"]))
        self.assertIsNone(lock.holder)

    def test_primitive_release_failure_retains_holder_metadata(self):
        """Fix ID: fx2-f2-executor-actor-chromedriver

        Primitive release failure must retain holder metadata rather than clearing it.
        """
        lock = ActorLock()
        primitive = InstrumentedPrimitive(raise_on_release=True)
        lock._lock = primitive

        lock.acquire("actor_a")
        self.assertEqual(lock.holder, "actor_a")

        with self.assertRaises(CustomReleaseError):
            lock.release()

        self.assertEqual(
            lock.holder,
            "actor_a",
            "Holder metadata was cleared despite primitive release failure",
        )

    def test_pre_acquired_sentinel_exception_propagation(self):
        """Nominal control: Pre-acquired context propagates body exceptions and clears holder on exit."""
        lock = ActorLock()
        acquired_lock = lock.acquire("actor_a")

        with self.assertRaises(CustomSentinelException):
            with acquired_lock:
                raise CustomSentinelException("sentinel body error")

        self.assertIsNone(lock.holder, "holder must be cleared after pre-acquired context exit")

        lock.acquire("actor_b")
        self.assertEqual(lock.holder, "actor_b")
        lock.release()
        self.assertIsNone(lock.holder)

    def test_bare_context_refuses_unowned_entry(self):
        """Fix ID: fx2-f2-executor-actor-chromedriver

        Bare context entry (`with lock:`) must refuse unowned entry before executing context body.
        """
        lock = ActorLock()
        body_entered = False
        try:
            with self.assertRaises(ActorBusy):
                with lock:
                    body_entered = True
        finally:
            self.assertFalse(body_entered, "Body must not be entered on bare unowned context")
            if lock.holder is not None:
                lock.release()

    def test_acquire_none_raises_value_error_before_primitive_access(self):
        """Fix ID: fx2-f2-executor-actor-chromedriver

        `lock.acquire(None)` must raise ValueError before attempting primitive acquisition.
        """
        lock = ActorLock()
        try:
            with self.assertRaises(ValueError):
                lock.acquire(None)
            self.assertIsNone(lock.holder)
        finally:
            if lock.holder is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass


class TestChoosePathShapeExtension(unittest.TestCase):
    """Tests shape validation, bytes inputs, unhashable values, order permutations and unknown labels."""

    def test_choose_path_bare_bytes_raises(self):
        """Fix ID: fx2-f2-executor-actor-chromedriver

        Bare bytes input must raise TypeError or ValueError instead of returning None.
        """
        with self.assertRaises((TypeError, ValueError)):
            choose_path(b"api")

        with self.assertRaises((TypeError, ValueError)):
            choose_path(b"ax")

    def test_choose_path_collection_of_bytes_returns_none(self):
        """Nominal control: Collection containing bytes elements yields None as string labels do not match."""
        self.assertIsNone(choose_path([b"api", b"ax"]))
        self.assertIsNone(choose_path((b"coordinates",)))

    def test_choose_path_unhashable_elements_raises_typeerror(self):
        """Nominal control: Collection with unhashable elements raises TypeError during set conversion."""
        with self.assertRaises(TypeError):
            choose_path([[1]])

        with self.assertRaises(TypeError):
            choose_path([{"path": "api"}])

    def test_choose_path_preference_order_permutations(self):
        """Nominal control: Documented preference (api > ax > coordinates) is preserved regardless of iterable order."""
        self.assertEqual(choose_path(["coordinates", "ax", "api"]), "api")
        self.assertEqual(choose_path(["coordinates", "api"]), "api")
        self.assertEqual(choose_path(["ax", "coordinates"]), "ax")
        self.assertEqual(choose_path(["coordinates", "ax"]), "ax")
        self.assertEqual(choose_path(["coordinates"]), "coordinates")

    def test_choose_path_duplicates_and_unknown_labels(self):
        """Nominal control: Duplicate labels and unknown path labels do not alter selection preference or fail."""
        self.assertEqual(choose_path(["coordinates", "coordinates", "ax", "ax"]), "ax")
        self.assertEqual(choose_path(("api", "api", "coordinates")), "api")
        self.assertIsNone(choose_path(["unknown_1", "unknown_2", "invalid_path"]))
        self.assertEqual(choose_path(["unknown_label", "coordinates"]), "coordinates")
        self.assertIsNone(choose_path([]))
        self.assertIsNone(choose_path(set()))


if __name__ == "__main__":
    unittest.main()
