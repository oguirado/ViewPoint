"""Offline production-path tests: G M2 1 / G M2 5, plus ActorLock entry.

No test-local lock implementation or live transport is used. Fakes establish
registry records, alias evidence, and monotonic nanoseconds only.
"""
from dataclasses import dataclass, replace
from types import SimpleNamespace
import threading
import unittest

from wender.act.actor import ActorBusy, ActorLock, choose_path
from wender.act.target_lock_registry import (
    ActorLease, Handle, PrerequisiteAbsent, TargetBusy,
    TargetLockRefused, TargetLockRegistry,
)


@dataclass(frozen=True)
class _Record:
    handle: object
    closed: bool = False


class _Clock:
    def __init__(self):
        self.ns = 100

    def __call__(self):
        return self.ns


class _Registry:
    def __init__(self):
        self.targets = {}
        self.aliases = {}

    def register(self, handle, resource=None):
        key = (handle.kind, handle.canonical_id)
        self.targets[key] = _Record(handle)
        self.aliases[key] = resource if resource is not None else handle.canonical_id

    def get(self, handle):
        return self.targets.get((handle.kind, handle.canonical_id))

    def canonical_resource(self, handle):
        return self.aliases.get((handle.kind, handle.canonical_id))


class TestTargetExclusion(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.registry = _Registry()
        self.a = Handle("cdp_tab", "tab-A", 11)
        self.b = Handle("x11_window", "window-B", 3)
        self.godot = Handle("godot_window", "godot-A", 4)
        self.capsule = Handle("capsule", "capsule-A", 2)
        self.screen = Handle("xvfb_screen", "screen-A", 1)
        self.registry.register(self.a, "destination-A")
        self.registry.register(self.godot, "destination-A")
        self.registry.register(self.capsule, "destination-A")
        self.registry.register(self.screen, "destination-A")
        self.registry.register(self.b, "destination-B")
        self.one = TargetLockRegistry(self.registry, clock=self.clock)
        self.two = TargetLockRegistry(self.registry, clock=self.clock)

    def _take(self, handle=None, owner="owner", **kw):
        return self.one.acquire(handle or self.a, owner,
                                expires_ns=kw.get("expires_ns", 400))

    def test_same_canonical_resource_excludes_across_instances_and_paths(self):
        lease = self._take()
        # Mutant: per-instance primitive locks would allow second acquisition.
        for handle in (self.a, self.godot, self.capsule, self.screen):
            with self.subTest(handle=handle), self.assertRaises(TargetBusy):
                self.two.acquire(handle, "other-path", expires_ns=400)
        self.one.release(lease)
        second = self.two.acquire(self.screen, "godot-bridge", expires_ns=400)
        self.two.release(second)

    def test_different_resources_remain_independent(self):
        # Positive control: target-scoped rather than accidental global exclusion.
        lease_a = self._take()
        lease_b = self.two.acquire(self.b, "parallel", expires_ns=400)
        self.assertEqual(lease_a.handle, self.a)
        self.assertEqual(lease_b.handle, self.b)
        self.two.release(lease_b)
        self.one.release(lease_a)

    def test_one_winner_under_simultaneous_submission(self):
        bar = threading.Barrier(3)
        results = []
        gate = threading.Lock()
        leases = []

        def submit(manager, owner):
            bar.wait()
            try:
                lease = manager.acquire(self.a, owner, expires_ns=400)
                with gate:
                    results.append("accepted")
                    leases.append((manager, lease))
            except TargetBusy:
                with gate:
                    results.append("busy")

        threads = [threading.Thread(target=submit, args=(self.one, "first")),
                   threading.Thread(target=submit, args=(self.two, "second"))]
        for t in threads:
            t.start()
        bar.wait()
        for t in threads:
            t.join(timeout=2)
            self.assertFalse(t.is_alive())
        self.assertCountEqual(results, ("accepted", "busy"))
        for manager, lease in leases:
            manager.release(lease)

    def test_nonce_generation_and_replay_cannot_release_active_lease(self):
        old = self._take(owner="same-owner")
        self.one.release(old)
        current = self.two.acquire(self.a, "same-owner", expires_ns=400)
        for bad in (
            old,
            replace(current, nonce=b"not-issued"),
            replace(current, owner="other"),
            replace(current),  # even a byte-for-byte copy is not an issued token
        ):
            with self.subTest(bad=bad), self.assertRaises(TargetLockRefused):
                self.one.release(bad)
            with self.assertRaises(TargetBusy):
                self.one.acquire(self.a, "contender", expires_ns=400)
        # Corrupt the issued token so identity/type checks pass and the saved
        # claim's generation check must reject it. Restore it for healthy release.
        original_generation = current.generation
        object.__setattr__(current, "generation", original_generation + 1)
        try:
            with self.assertRaises(TargetLockRefused):
                self.one.release(current)
            with self.assertRaises(TargetBusy):
                self.one.acquire(self.a, "contender", expires_ns=400)
        finally:
            object.__setattr__(current, "generation", original_generation)
        self.two.release(current)
        nxt = self._take(owner="healthy")
        self.one.release(nxt)

    def test_sent_action_is_not_reaped_by_expiry_or_release(self):
        # Mutant: expiry frees a lease even if an irreversible action was sent.
        sent = self._take()
        self.one.mark_inflight(sent)
        self.clock.ns = 1000
        with self.assertRaises(TargetLockRefused):
            self.one.release(sent)
        with self.assertRaises(TargetLockRefused):
            self.one.mark_inflight(sent)
        with self.assertRaises(TargetBusy):
            self.two.acquire(self.a, "contender", expires_ns=2000)
        self.one.reconcile_quarantine(self.a, acknowledged=False)
        with self.assertRaises(TargetBusy):
            self.two.acquire(self.a, "still-busy", expires_ns=2000)
        self.two.reconcile_quarantine(self.a, acknowledged=True)
        new = self.one.acquire(self.a, "quiescent", expires_ns=2000)
        self.one.release(new)

    def test_live_sent_action_requires_ack_even_without_expiry(self):
        lease = self._take()
        self.one.mark_inflight(lease)
        with self.assertRaises(TargetLockRefused):
            self.one.release(lease)
        with self.assertRaises(TargetLockRefused):
            self.one.reconcile_quarantine(replace(self.a, generation=12), acknowledged=True)
        with self.assertRaises(TypeError):
            self.one.reconcile_quarantine(self.a, acknowledged=1)
        with self.assertRaises(TargetBusy):
            self.two.acquire(self.a, "unacknowledged", expires_ns=400)
        self.one.reconcile_quarantine(self.a, acknowledged=True)
        with self.assertRaises(TargetLockRefused):
            self.one.release(lease)
        new = self._take()
        self.one.release(new)

    def test_stale_invalid_and_missing_registry_inputs_refuse(self):
        with self.assertRaises(TargetLockRefused):
            self._take(replace(self.a, generation=9))
        with self.assertRaises(TargetLockRefused):
            self._take(SimpleNamespace(**{**vars(self.a), "kind": "not-a-kind"}))
        with self.assertRaises(TargetLockRefused):
            self._take(expires_ns=100)
        with self.assertRaises(TargetLockRefused):
            self._take(owner="   ")
        with self.assertRaises(PrerequisiteAbsent):
            TargetLockRegistry(object(), clock=self.clock).acquire(
                self.a, "actor", expires_ns=400)
        with self.assertRaises(PrerequisiteAbsent):
            self.one.acquire(Handle("cdp_tab", "missing", 1), "actor", expires_ns=400)
        good = self._take()
        self.one.release(good)

    def test_unacknowledged_quarantine_fences_other_targets_only_locally(self):
        sent = self._take()
        self.one.mark_inflight(sent)
        self.one.reconcile_quarantine(self.a, acknowledged=False)
        independent = self.two.acquire(self.b, "independent", expires_ns=400)
        self.two.release(independent)
        with self.assertRaises(TargetBusy):
            self.two.acquire(self.godot, "alias", expires_ns=400)
        self.one.reconcile_quarantine(self.a, acknowledged=True)


class TestLegacyContracts(unittest.TestCase):
    def test_bare_context_is_type_error_and_body_never_runs(self):
        lock = ActorLock()
        entered = []
        with self.assertRaises(TypeError):
            with lock:
                entered.append("unsafe")
        self.assertFalse(entered)
        self.assertIsNone(lock.holder)

    def test_context_acquire_and_owner_release(self):
        lock = ActorLock()
        with self.assertRaisesRegex(ValueError, "actor identity"):
            lock.acquire(None)
        with self.assertRaises(ZeroDivisionError):
            with lock.acquire("owner"):
                with self.assertRaises(ActorBusy):
                    lock.acquire("other")
                1 / 0
        self.assertIsNone(lock.holder)
        with lock.acquire("other"):
            self.assertEqual(lock.holder, "other")

    def test_actor_primitive_release_failure_keeps_ownership(self):
        class _ReleaseFails:
            def __init__(self):
                self.held = False

            def acquire(self, blocking=False):
                if self.held:
                    return False
                self.held = True
                return True

            def release(self):
                raise RuntimeError("primitive did not release")

        lock = ActorLock()
        lock._lock = _ReleaseFails()
        lock.acquire("writer")
        with self.assertRaisesRegex(RuntimeError, "primitive did not release"):
            lock.release("writer")
        self.assertEqual(lock.holder, "writer")
        with self.assertRaises(ActorBusy):
            lock.acquire("other")

    def test_path_shape_and_preference(self):
        for value in ("ax", b"api", bytearray(b"api")):
            with self.subTest(value=value), self.assertRaises(TypeError):
                choose_path(value)
        self.assertEqual(choose_path(iter(["coordinates", "ax", "api"])), "api")
        self.assertEqual(choose_path(["ax"]), "ax")
        self.assertIsNone(choose_path([]))


if __name__ == "__main__":
    unittest.main()
