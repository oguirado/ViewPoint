"""Tests for ActorLock context behavior and bare context entry policies."""
import unittest

from wender.act import actor


class CustomSentinelException(Exception):
    """Custom sentinel exception for exception propagation testing."""
    pass


class TestBareActorLock(unittest.TestCase):
    """Test ActorLock pre-acquired and bare context entry behaviors."""

    def test_pre_acquired_context_exception_propagation_and_release(self):
        """Nominal control: pre-acquired context propagates body exception and clears holder."""
        lock = actor.ActorLock()
        with self.assertRaises(CustomSentinelException):
            with lock.acquire("actor_a"):
                raise CustomSentinelException("sentinel body error")

        self.assertIsNone(lock.holder, "holder must be cleared after context exit")

        # Verify another actor can acquire afterward
        acquired_lock = lock.acquire("actor_b")
        self.assertEqual(lock.holder, "actor_b")
        acquired_lock.release()
        self.assertIsNone(lock.holder)

    # Defect F-ACT-01: Bare context entry does not establish exclusion / reject unowned entry.
    def test_bare_context_entry_rejects_unowned_entry_before_body(self):
        """F-ACT-01: Bare context entry must reject unowned entry with ActorBusy before body."""
        lock = actor.ActorLock()
        body_entered = False
        try:
            with self.assertRaises(actor.ActorBusy):
                with lock:
                    body_entered = True
        finally:
            self.assertFalse(body_entered, "mutation body must never be entered on bare context")
            if lock.holder is not None:
                lock.release()

    # Defect F-ACT-01: Bare context entry replaces body exception with RuntimeError on unlocked release.
    def test_bare_context_does_not_replace_body_exception(self):
        """F-ACT-01: Bare context must reject on entry or preserve body exception without unlocked release error."""
        lock = actor.ActorLock()
        try:
            with self.assertRaises((actor.ActorBusy, CustomSentinelException)):
                with lock:
                    raise CustomSentinelException("body error")
        finally:
            if lock.holder is not None:
                lock.release()


if __name__ == "__main__":
    unittest.main()
