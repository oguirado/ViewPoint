"""C-ports and C-lanes: strict, offline SessionRegistry scheduler views.

The real producer is imported inside undecorated tests, never replaced by a
fallback Handle. Bytes, digests and registry snapshots use independent oracles.
"""
from __future__ import annotations

from dataclasses import fields
import hashlib
import threading
from types import SimpleNamespace
import unittest

import numpy as np


KINDS = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")


class FakeTargetTransport:
    """Only the *external* I/O boundary is fake; state stays in the real registry."""

    def __init__(self):
        self.calls = []

    def send(self, method, params):
        self.calls.append(("send", method, params))
        return {"success": True}

    def close_target(self, target):
        self.calls.append(("close_target", target))

    def detach_session(self, session):
        self.calls.append(("detach_session", session))


def contents(registry):
    """Complete metadata snapshot, independent of producer getters/views."""
    with registry._lock:
        return (registry._generation, dict(registry._active), dict(registry._aliases),
                dict(registry._tombstones), dict(registry._closed_ns),
                set(registry._quarantine), dict(registry._pending),
                dict(registry._last_frame_seq), dict(registry._latest_frame_digest))


class RegistryBoundaryTests(unittest.TestCase):
    def setUp(self):
        from wender.perceive.session_registry import SessionRegistry
        self.reg = SessionRegistry(clock=lambda: 17000)
        self.a = FakeTargetTransport()
        self.b = FakeTargetTransport()

    def refuses(self, error, action, *transports):
        """Rejected calls must not touch any registry metadata or fake I/O."""
        baseline = contents(self.reg)
        calls = tuple(tuple(t.calls) for t in transports)
        with self.assertRaises(error):
            action()
        self.assertEqual(contents(self.reg), baseline)
        self.assertEqual(tuple(tuple(t.calls) for t in transports), calls)

    def test_c_ports_strict_field_order_and_authoritative_views(self):
        from wender.perceive.session_registry import Handle, TargetRecord
        self.assertEqual(tuple(f.name for f in fields(Handle)),
                         ("kind", "canonical_id", "generation"))
        self.assertEqual(tuple(f.name for f in fields(TargetRecord)),
                         ("handle", "transport", "owned", "epoch", "session_id",
                          "attachment_owned", "closed"))
        handles = {}
        for i, kind in enumerate(KINDS):
            h = self.reg.register(kind, "independent-" + str(i), self.a, owned=False)
            handles[kind] = h
            self.assertEqual(h.generation, i + 1)
            record = self.reg.resolve(h)
            self.assertIs(self.reg.get(h), record)
            self.assertIs(record.transport, self.a)
            self.assertFalse(record.closed)
            self.assertEqual(record.epoch, 0)  # zero is legitimate
            self.assertEqual(self.reg.handle_for(kind, h.canonical_id), h)
            self.assertEqual(self.reg.canonical_resource(h), kind + ":" + h.canonical_id
                             if kind not in ("capsule", "xvfb_screen")
                             else "capsule_screen:" + h.canonical_id)
        self.assertEqual(len(set(handles.values())), len(KINDS))
        self.assertEqual(self.a.calls, [])

    def test_authenticated_alias_and_duck_typed_cross_repository_handle(self):
        from wender.perceive.session_registry import AliasConflictError
        cap = self.reg.register("capsule", "virtual-42", self.a, owned=False)
        sibling = self.reg.register("x11_window", "other-42", self.b, owned=False)
        self.assertEqual(self.reg.bind_alias("x11_window", "screen-alias-42", cap), cap)
        self.assertEqual(self.reg.handle_for("x11_window", "screen-alias-42"), cap)
        structural = SimpleNamespace(kind=cap.kind, canonical_id=cap.canonical_id,
                                     generation=cap.generation)
        self.assertIs(self.reg.get(structural), self.reg.resolve(cap))
        self.assertEqual(self.reg.canonical_resource(structural), "capsule_screen:virtual-42")
        self.assertEqual(self.reg.canonical_resource(cap),
                         self.reg.canonical_resource(self.reg.handle_for("x11_window", "screen-alias-42")))
        # A caller-supplied alias string cannot silently claim another target.
        self.refuses(AliasConflictError,
                     lambda: self.reg.bind_alias("x11_window", "other-42", cap), self.a, self.b)
        self.assertEqual(self.reg.resolve(sibling).transport, self.b)

    def test_c_lanes_changed_transport_and_ownership_refuse_without_mutation(self):
        from wender.perceive.session_registry import AliasConflictError
        h = self.reg.register("capsule", "shared-33", self.a, owned=False)
        self.assertEqual(self.reg.register("xvfb_screen", "shared-33", self.a, owned=False), h)
        self.refuses(AliasConflictError,
                     lambda: self.reg.register("capsule", "shared-33", self.b, owned=False), self.a, self.b)
        self.refuses(AliasConflictError,
                     lambda: self.reg.register("xvfb_screen", "shared-33", self.b, owned=False), self.a, self.b)
        self.refuses(AliasConflictError,
                     lambda: self.reg.register("capsule", "shared-33", self.a, owned=True), self.a)
        self.assertIs(self.reg.get(h).transport, self.a)

    def test_same_text_id_across_kinds_never_grants_authority(self):
        from wender.perceive.session_registry import Handle, StaleHandleError, AliasConflictError
        a = self.reg.register("cdp_tab", "same", self.a, owned=False)
        b = self.reg.register("godot_window", "same", self.b, owned=False)
        self.assertNotEqual(a, b)
        self.assertNotEqual(self.reg.canonical_resource(a), self.reg.canonical_resource(b))
        self.refuses(AliasConflictError,
                     lambda: self.reg.bind_alias("godot_window", "same", a), self.a, self.b)
        wrong_kind = Handle("x11_window", "same", a.generation)
        self.assertIsNone(self.reg.get(wrong_kind))
        self.refuses(StaleHandleError, lambda: self.reg.resolve(wrong_kind), self.a, self.b)
        self.refuses(StaleHandleError, lambda: self.reg.canonical_resource(wrong_kind), self.a, self.b)
        self.assertEqual(self.reg.get(b).handle, b)

    def test_malformed_foreign_sentinel_generation_and_lookup_are_pure(self):
        from wender.perceive.session_registry import RegistryError, StaleHandleError, Handle
        h = self.reg.register("cdp_tab", "genuine", self.a, owned=False)
        for gen in (0, False, True, -1, 1.0, "1", None):
            invalid = SimpleNamespace(kind=h.kind, canonical_id=h.canonical_id,
                                      generation=gen)
            self.refuses(RegistryError, lambda bad=invalid: self.reg.get(bad), self.a)
            self.refuses(RegistryError, lambda bad=invalid: self.reg.canonical_resource(bad), self.a)
            self.refuses(RegistryError, lambda bad=invalid: self.reg.resolve(bad), self.a)
            self.refuses(RegistryError,
                         lambda bad=invalid: Handle(bad.kind, bad.canonical_id, bad.generation), self.a)
        for bogus in (
            SimpleNamespace(kind="foreign", canonical_id="genuine", generation=h.generation),
            SimpleNamespace(kind="cdp_tab", canonical_id="0xFFFFFFFF", generation=h.generation),
            SimpleNamespace(kind="cdp_tab", canonical_id="genuine", generation=0xFFFFFFFF),
            object(),
        ):
            if getattr(bogus, "kind", None) == "cdp_tab":
                self.assertIsNone(self.reg.get(bogus))
                self.refuses(StaleHandleError,
                             lambda bad=bogus: self.reg.canonical_resource(bad), self.a)
            else:
                self.refuses(RegistryError, lambda bad=bogus: self.reg.get(bad), self.a)
                self.refuses(RegistryError, lambda bad=bogus: self.reg.canonical_resource(bad), self.a)
        self.assertEqual(self.reg.get(h).handle, h)

    def test_stale_retirement_quarantine_and_sibling_live(self):
        from wender.perceive.session_registry import StaleHandleError, QuarantinedTargetError
        old = self.reg.register("cdp_tab", "tab-A", self.a, owned=False)
        sibling = self.reg.register("cdp_tab", "tab-B", self.b, owned=False)
        self.reg.bind_alias("x11_window", "alias-A", old)
        self.assertEqual(self.reg.stop(old).status, "stopped")
        self.assertEqual(self.a.calls, [])  # STOP sends nothing
        self.assertIsNone(self.reg.get(old))
        self.refuses(StaleHandleError, lambda: self.reg.resolve(old), self.a, self.b)
        self.refuses(StaleHandleError, lambda: self.reg.canonical_resource(old), self.a, self.b)
        self.refuses(QuarantinedTargetError,
                     lambda: self.reg.register("cdp_tab", "tab-A", self.a, owned=False), self.a)
        self.refuses(QuarantinedTargetError,
                     lambda: self.reg.register("x11_window", "alias-A", self.a, owned=False), self.a)
        self.assertIs(self.reg.get(sibling), self.reg.resolve(sibling))
        self.assertEqual(self.reg.canonical_resource(sibling), "cdp_tab:tab-B")
        self.reg.acknowledge_cleanup(old)  # operator action, no transport I/O
        fresh = self.reg.register("cdp_tab", "tab-A", self.a, owned=False)
        self.assertGreater(fresh.generation, old.generation)
        self.assertIsNone(self.reg.get(old))
        self.assertEqual(self.reg.get(fresh).epoch, 0)
        self.refuses(StaleHandleError, lambda: self.reg.canonical_resource(old), self.a)
        self.assertIs(self.reg.get(sibling), self.reg.resolve(sibling))

    def test_owned_cleanup_sends_only_on_explicit_close(self):
        from wender.perceive.session_registry import StaleHandleError
        h = self.reg.register("cdp_tab", "owned-A", self.a, owned=True)
        self.reg.bind_attachment(h, "session-7", owned=True)
        self.assertIsNotNone(self.reg.get(h))
        self.assertEqual(self.a.calls, [])
        self.reg.close(h)
        self.assertEqual(self.a.calls, [("detach_session", "session-7"),
                                       ("close_target", "owned-A")])
        self.assertIsNone(self.reg.get(h))
        self.refuses(StaleHandleError, lambda: self.reg.canonical_resource(h), self.a)
        self.assertEqual(self.reg.close(h).status, "already_closed")
        self.assertEqual(len(self.a.calls), 2)

    def test_unbounded_generation_never_wraps_or_reattaches(self):
        from wender.perceive.session_registry import StaleHandleError
        self.reg._generation = (2 ** 64) - 2
        h1 = self.reg.register("x11_window", "huge", self.a, owned=False)
        self.assertEqual(h1.generation, 2 ** 64 - 1)
        self.assertEqual(self.reg.close(h1).status, "closed")
        h2 = self.reg.register("x11_window", "huge", self.a, owned=False)
        self.assertEqual(h2.generation, 2 ** 64)
        self.assertIsNone(self.reg.get(h1))
        self.refuses(StaleHandleError, lambda: self.reg.canonical_resource(h1), self.a)
        self.assertIsNotNone(self.reg.get(h2))
        self.assertEqual(self.reg.canonical_resource(h2), "x11_window:huge")

    def test_frame_rgb24_ownership_hash_epoch_and_sequence_controls(self):
        from wender.perceive.session_registry import FrameValidationError, Observation
        handles = [self.reg.register("x11_window", str(i), self.a, owned=False)
                   for i in range(4)]
        for i, h in enumerate(handles):
            # 640x360 is a deterministic TARGET fixture, not a host speed metric.
            arr = (np.arange(640 * 360 * 3, dtype=np.uint32).reshape(360, 640, 3) + i) % 256
            rgb = arr.astype(np.uint8)
            expected_bytes = bytes(rgb.tobytes(order="C"))
            expected_digest = hashlib.sha256(expected_bytes).hexdigest()
            fr = self.reg.admit_frame(h, 0, rgb, epoch=0)
            self.assertEqual((fr.width, fr.height, fr.epoch, fr.frame_seq), (640, 360, 0, 0))
            self.assertEqual(fr.rgb.tobytes(), expected_bytes)
            self.assertEqual(fr.digest, expected_digest)
            rgb.fill(99)
            self.assertEqual(fr.rgb.tobytes(), expected_bytes)
            self.assertFalse(fr.rgb.flags.writeable)
            with self.assertRaises(ValueError):
                fr.rgb.setflags(write=True)
            obs = self.reg.admit_observation(h, fr, state={"window": i}, dpr_ppm=1000000)
            self.assertIsInstance(obs, Observation)
            self.assertEqual((obs.handle, obs.epoch, obs.frame_seq), (h, 0, 0))
            self.refuses(FrameValidationError,
                         lambda h=h, rgb=rgb: self.reg.admit_frame(h, 0, rgb, epoch=0), self.a)
        self.assertEqual(self.a.calls, [])

    def test_concurrent_duplicate_register_is_single_incarnation(self):
        barrier = threading.Barrier(5)
        found = []
        guard = threading.Lock()

        def worker():
            barrier.wait()
            h = self.reg.register("x11_window", "parallel", self.a, owned=False)
            with guard:
                found.append(h)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(found), 4)
        self.assertEqual(len(set(found)), 1)
        self.assertEqual(contents(self.reg)[0], 1)
        self.assertEqual(len(self.reg._active), 1)
        self.assertEqual(self.a.calls, [])

    def test_semantic_mutant_controls_four_valid_four_killed(self):
        """Count behavior failures, not import errors or constructor crashes."""
        from wender.perceive.session_registry import StaleHandleError
        h = self.reg.register("cdp_tab", "mutant", self.a, owned=False)
        stale = SimpleNamespace(kind=h.kind, canonical_id=h.canonical_id,
                                generation=h.generation + 1)
        live = self.reg.resolve(h)
        # Baseline positive and negative controls: all must be green first.
        self.assertIs(self.reg.get(h), live)
        self.assertIsNone(self.reg.get(stale))
        expected_resource = "cdp_tab:mutant"  # independent literal identity oracle
        self.assertEqual(self.reg.canonical_resource(h), expected_resource)
        with self.assertRaises(StaleHandleError):
            self.reg.canonical_resource(stale)
        cases = (
            ("get_always_none", lambda handle: None,
             lambda fn: self.assertIs(fn(h), live)),
            ("get_ignores_generation", lambda handle: live,
             lambda fn: self.assertIsNone(fn(stale))),
            ("resource_from_untrusted_string", lambda handle: "cdp_tab:forged",
             lambda fn: self.assertEqual(fn(h), expected_resource)),
            ("resource_ignores_retirement", lambda handle: expected_resource,
             lambda fn: self.assertRaises(StaleHandleError, fn, stale)),
        )
        kills = []
        for name, mutant, assertion in cases:
            with self.subTest(mutant=name):
                try:
                    assertion(mutant)
                except AssertionError:
                    kills.append(name)
        self.assertEqual(len(kills), len(cases),
                         f"semantic kills {len(kills)}/{len(cases)}; survivors="
                         f"{[name for name, _, _ in cases if name not in kills]}")


if __name__ == "__main__":
    unittest.main()
