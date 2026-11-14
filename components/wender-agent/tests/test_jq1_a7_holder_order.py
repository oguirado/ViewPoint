"""Unit tests reproducing F-ACT-04 holder order defect and pinning metadata lifecycle policy."""

import threading
import unittest
from wender.act.actor import ActorBusy, ActorLock


class InstrumentedPrimitive:
    """Deterministic primitive wrapping threading.Lock to hook lifecycle boundaries."""

    def __init__(self, release_hook=None):
        self._lock = threading.Lock()
        self.release_hook = release_hook
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
        self._lock.release()

    @property
    def locked(self):
        return self._lock.locked()


class TestHolderOrder(unittest.TestCase):
    def test_nominal_actor_lock_lifecycle(self):
        """Nominal control: ActorLock acquire, contention, and release without instrumentation."""
        lock = ActorLock()
        try:
            lock.acquire("actor_a")
            self.assertEqual(lock.holder, "actor_a")

            with self.assertRaises(ActorBusy) as ctx:
                lock.acquire("actor_b")
            self.assertIn("'actor_a'", str(ctx.exception))

            lock.release()
            self.assertIsNone(lock.holder)

            # Re-acquire after release should succeed
            lock.acquire("actor_c")
            self.assertEqual(lock.holder, "actor_c")
        finally:
            if lock.holder is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass

    def test_holder_retained_until_primitive_release_completes(self):
        # F-ACT-04: Holder metadata is cleared before primitive release completes.
        # Desired corrected behavior: lock.holder remains "actor_a" while the primitive is locked,
        # and competing acquire attempts record ActorBusy identifying "actor_a".
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
                lock.acquire("actor_b")
            except ActorBusy as exc:
                state["busy_exc"] = exc

        primitive = InstrumentedPrimitive(release_hook=release_hook)
        lock._lock = primitive

        try:
            lock.acquire("actor_a")
            self.assertEqual(lock.holder, "actor_a")

            lock.release()

            self.assertTrue(state["hook_executed"])
            # Under corrected policy, holder during release while primitive is still locked must be "actor_a"
            self.assertEqual(
                state["holder_during_release"],
                "actor_a",
                "F-ACT-04: holder was cleared before primitive release completed",
            )
            self.assertIsNotNone(state["busy_exc"])
            self.assertIn(
                "'actor_a'",
                str(state["busy_exc"]),
                "F-ACT-04: ActorBusy reported None instead of current holder 'actor_a'",
            )
            # After release finishes, holder must be None
            self.assertIsNone(lock.holder)
        finally:
            if lock.holder is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass

    def test_acquire_none_raises_value_error(self):
        # F-ACT-04: Proposed identity policy requires lock.acquire(None) to raise ValueError
        # before primitive acquisition rather than storing None as holder.
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


    def test_release_wrong_owner_refused_and_preserves_holder(self):
        lock = ActorLock()
        try:
            lock.acquire("actor_a")
            with self.assertRaises(RuntimeError):
                lock.release("actor_b")
            self.assertEqual(lock.holder, "actor_a")
            with self.assertRaises(ActorBusy):
                lock.acquire("actor_b")
            lock.release("actor_a")
            self.assertIsNone(lock.holder)
        finally:
            if lock.holder is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass


if __name__ == "__main__":
    unittest.main()
