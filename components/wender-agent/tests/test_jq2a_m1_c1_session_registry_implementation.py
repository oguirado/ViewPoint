"""Offline, ordinary passing implementation tests for G M1 1 and value boundaries.

In-memory transports and NumPy buffers only. No browser, network, disk, or VM
speed claim. A mutant that ignores handle.generation after respawn fails these.
"""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields
from types import SimpleNamespace
from unittest import TestCase, mock
import hashlib
import socket
import subprocess
import urllib.request

import numpy as np

from wender.perceive.session_registry import (
    ActionRequest, ActorLease, AliasConflictError, ChangeResult, FocusLease,
    FrameRef, FrameValidationError, Handle, Observation, PerceptionResult,
    QuarantinedTargetError, Receipt, RegistryError, SessionRegistry,
    StaleHandleError, StreamSpec, StreamToken, TargetRecord, Ticket,
    WindowDescription, WindowFrames,
)


class FakeClock:
    def __init__(self):
        self.now = 4_200

    def __call__(self):
        self.now += 1
        return self.now


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.fail_detach = False
        self.fail_close = False
        self.registry = None

    def detach_session(self, sid):
        self.calls.append(("detach", sid))
        if self.fail_detach:
            raise OSError("detach offline failure")
        if self.registry is not None:
            # The registry metadata mutex must not span transport callbacks.
            if not self.registry._lock.acquire(blocking=False):
                raise RuntimeError("transport invoked under registry mutex")
            self.registry._lock.release()

    def close_target(self, target):
        self.calls.append(("close_target", target))
        if self.fail_close:
            raise OSError("close offline failure")
        if self.registry is not None:
            if not self.registry._lock.acquire(blocking=False):
                raise RuntimeError("transport invoked under registry mutex")
            self.registry._lock.release()


class FakeCdpPort:
    """Connection-level close is a forbidden API for target cleanup."""
    def __init__(self):
        self.calls = []

    def send(self, method, params, **kwargs):
        self.calls.append((method, params))
        return {"success": True}

    def detach_session(self, session_id):
        self.calls.append(("detach", session_id))

    def close(self):
        raise AssertionError("never call connection-wide CdpPort.close")


@contextmanager
def prohibit_external_io():
    def forbidden(*args, **kwargs):
        raise AssertionError("FORBIDDEN network/process/file I/O")
    with (mock.patch("socket.create_connection", side_effect=forbidden),
          mock.patch("socket.socket", side_effect=forbidden),
          mock.patch("subprocess.Popen", side_effect=forbidden),
          mock.patch("urllib.request.urlopen", side_effect=forbidden),
          mock.patch("builtins.open", side_effect=forbidden)):
        yield


class SessionRegistryTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.registry = SessionRegistry(clock=self.clock)
        self.transport = FakeTransport()
        self.transport.registry = self.registry

    def test_generation_stale_after_respawn_and_healthy_fresh_handle(self):
        first = self.registry.register("cdp_tab", "target-A", self.transport, owned=False)
        self.assertEqual(self.registry.resolve(first).handle, first)
        self.assertEqual(self.registry.close(first).status, "closed")
        replacement = self.registry.register("cdp_tab", "target-A", self.transport, owned=False)
        self.assertGreater(replacement.generation, first.generation)
        self.assertEqual(first.canonical_id, replacement.canonical_id)
        with self.assertRaises(StaleHandleError):
            self.registry.resolve(first)
        self.assertEqual(self.registry.resolve(replacement).handle, replacement)
        self.assertEqual(self.registry.close(first).status, "already_closed")
        self.assertEqual(self.registry.resolve(replacement).handle, replacement)

    def test_reject_generation_ignoring_mutant_and_duck_typed_handles(self):
        first = self.registry.register("x11_window", "screen-2", self.transport, owned=False)
        self.registry.close(first)
        fresh = self.registry.register("x11_window", "screen-2", self.transport, owned=False)
        forged = Handle(first.kind, first.canonical_id, first.generation)
        with self.assertRaises(StaleHandleError):
            self.registry.resolve(forged)
        duck = SimpleNamespace(kind=fresh.kind, canonical_id=fresh.canonical_id, generation=fresh.generation)
        self.assertEqual(self.registry.resolve(duck).handle, fresh)
        with self.assertRaises(StaleHandleError):
            self.registry.advance_epoch(duck.__class__(**{**vars(duck), "generation": first.generation}))
        self.assertEqual(self.registry.resolve(fresh).epoch, 0)

    def test_generations_unbounded_python_integers_no_wrap(self):
        self.registry._generation = 2 ** 64 - 2
        a = self.registry.register("cdp_tab", "A", self.transport, owned=False)
        b = self.registry.register("cdp_tab", "B", self.transport, owned=False)
        self.assertEqual((a.generation, b.generation), (2 ** 64 - 1, 2 ** 64))
        self.assertNotEqual(a, b)

    def test_epoch_changes_without_generation_or_identity_reuse(self):
        h = self.registry.register("cdp_tab", "tab-3", self.transport, owned=True)
        self.assertEqual(self.registry.advance_epoch(h), 1)
        self.assertEqual(self.registry.advance_epoch(h), 2)
        self.assertEqual(self.registry.resolve(h).handle, h)
        self.assertEqual(self.registry.resolve(h).epoch, 2)
        self.registry.close(h)
        with self.assertRaises(StaleHandleError):
            self.registry.advance_epoch(h)

    def test_aliases_same_capsule_screen_destination_and_explicit_other_ids(self):
        primary = self.registry.register("capsule", "destination-9", self.transport, owned=False)
        alias = self.registry.register("xvfb_screen", "destination-9", self.transport, owned=False)
        with self.assertRaises(AliasConflictError):
            self.registry.register("xvfb_screen", "destination-9", FakeTransport(), owned=False)
        self.assertEqual(primary, alias)
        self.assertEqual(self.registry.handle_for("xvfb_screen", "destination-9"), primary)
        self.registry.bind_alias("x11_window", "window-9", primary)
        self.assertEqual(self.registry.handle_for("x11_window", "window-9"), primary)
        self.registry.close(primary)
        with self.assertRaises(StaleHandleError):
            self.registry.handle_for("x11_window", "window-9")

    def test_distinct_kinds_do_not_alias_accidentally_and_conflicts_rejected(self):
        a = self.registry.register("cdp_tab", "same", self.transport, owned=False)
        b = self.registry.register("godot_window", "same", self.transport, owned=False)
        self.assertNotEqual(a, b)
        with self.assertRaises(AliasConflictError):
            self.registry.bind_alias("cdp_tab", "same", b)
        with self.assertRaises(AliasConflictError):
            self.registry.register("cdp_tab", "same", FakeTransport(), owned=False)
        with self.assertRaises(AliasConflictError):
            self.registry.register("cdp_tab", "same", self.transport, owned=True)
        self.assertEqual(self.registry.register("cdp_tab", "same", self.transport, owned=False), a)

    def test_caller_target_not_closed_even_when_attachment_owned(self):
        h = self.registry.register("cdp_tab", "caller-tab", self.transport, owned=False)
        self.registry.bind_attachment(h, "session-owned", owned=True)
        with prohibit_external_io():
            receipt = self.registry.close(h)
        self.assertEqual(receipt.status, "closed")
        self.assertEqual(self.transport.calls, [("detach", "session-owned")])
        self.assertEqual(self.registry.close(h).status, "already_closed")
        self.assertEqual(len(self.transport.calls), 1)

    def test_owned_target_and_attachment_cleanup_outside_lock_once(self):
        h = self.registry.register("cdp_tab", "broker-tab", self.transport, owned=True)
        self.registry.bind_attachment(h, "sid-broker", owned=True)
        with prohibit_external_io():
            receipt = self.registry.close(h)
        self.assertEqual(receipt.status, "closed")
        self.assertEqual(self.transport.calls, [("detach", "sid-broker"), ("close_target", "broker-tab")])
        rec, when, pending = self.registry.tombstone(h)
        self.assertTrue(rec.closed)
        self.assertEqual((when, pending), (4_201, False))
        self.assertEqual(self.registry.close(h).status, "already_closed")
        with self.assertRaises(StaleHandleError):
            self.registry.resolve(h)

    def test_cdp_port_like_transport_not_connection_closed(self):
        cdp = FakeCdpPort()
        h = self.registry.register("cdp_tab", "tab-1", cdp, owned=True)
        self.registry.bind_attachment(h, "s-1", owned=True)
        with prohibit_external_io():
            result = self.registry.close(h)
        self.assertEqual(result.status, "closed")
        self.assertEqual(cdp.calls, [("detach", "s-1"), ("Target.closeTarget", {"targetId": "tab-1"})])

    def test_bind_attachment_does_not_steal_other_session_or_escalate(self):
        first = self.registry.register("cdp_tab", "t1", self.transport, owned=False)
        second = self.registry.register("cdp_tab", "t2", self.transport, owned=False)
        self.registry.bind_attachment(first, "shared-sid", owned=False)
        with self.assertRaises(AliasConflictError):
            self.registry.bind_attachment(second, "shared-sid", owned=True)
        with self.assertRaises(AliasConflictError):
            self.registry.bind_attachment(first, "shared-sid", owned=True)
        self.assertFalse(self.registry.resolve(first).attachment_owned)
        self.assertIsNone(self.registry.resolve(second).session_id)

    def test_stop_is_metadata_only_and_quarantines_until_ack(self):
        h = self.registry.register("cdp_tab", "t-stop", self.transport, owned=True)
        self.registry.bind_attachment(h, "sid-stop", owned=True)
        with prohibit_external_io():
            res = self.registry.stop(h)
        self.assertEqual(res.status, "stopped")
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(StaleHandleError):
            self.registry.resolve(h)
        with self.assertRaises(QuarantinedTargetError):
            self.registry.register("cdp_tab", "t-stop", self.transport, owned=True)
        self.assertTrue(self.registry.tombstone(h)[2])
        self.registry.acknowledge_cleanup(h)
        fresh = self.registry.register("cdp_tab", "t-stop", self.transport, owned=True)
        self.assertGreater(fresh.generation, h.generation)
        self.assertFalse(self.registry.tombstone(h)[2])

    def test_cleanup_error_retains_pending_tombstone_without_claiming_success(self):
        self.transport.fail_close = True
        h = self.registry.register("cdp_tab", "flaky", self.transport, owned=True)
        result = self.registry.close(h)
        self.assertEqual(result.status, "cleanup_pending")
        self.assertIn("close offline failure", result.reason)
        self.assertTrue(self.registry.tombstone(h)[2])
        with self.assertRaises(QuarantinedTargetError):
            self.registry.register("cdp_tab", "flaky", self.transport, owned=True)
        self.registry.acknowledge_cleanup(h)
        self.assertEqual(self.registry.register("cdp_tab", "flaky", self.transport, owned=True).generation, h.generation + 1)

    def test_invalid_handles_and_kinds_rejected(self):
        for kind in ("", "frame", "browser"):
            with self.assertRaises(RegistryError):
                self.registry.register(kind, "id", self.transport, owned=False)
        with self.assertRaises(RegistryError):
            Handle("cdp_tab", "id", True)
        with self.assertRaises(RegistryError):
            Handle("cdp_tab", " id ", 1)
        with self.assertRaises(RegistryError):
            self.registry.register("cdp_tab", "id", self.transport, owned=1)


class ImmutableContractsTests(TestCase):
    def setUp(self):
        self.r = SessionRegistry(clock=FakeClock())
        self.a = self.r.register("xvfb_screen", "screen-A", object(), owned=False)
        self.b = self.r.register("xvfb_screen", "screen-B", object(), owned=False)
        self.arr = np.arange(3 * 5 * 3, dtype=np.uint8).reshape(3, 5, 3)

    def test_canonical_record_field_orders_and_frozen_shells(self):
        definitions = {
            Handle: ("kind", "canonical_id", "generation"),
            TargetRecord: ("handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed"),
            FrameRef: ("handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"),
            Observation: ("handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"),
            Receipt: ("action_id", "handle", "status", "reason"),
            ActionRequest: ("handle", "action_id", "epoch", "payload", "cost", "deadline_ns"),
            ActorLease: ("handle", "owner", "generation", "nonce", "expires_ns"),
            FocusLease: ("lease", "domain", "epoch"),
            StreamSpec: ("width", "height", "slots", "quota_bytes"),
            StreamToken: ("handle", "epoch"),
            Ticket: ("action_id", "handle"),
            ChangeResult: ("handle", "epoch", "settled", "claims", "ops"),
            PerceptionResult: ("handle", "observation", "error"),
            WindowFrames: ("handle", "epoch", "frames", "frame_seqs", "dpr_ppm"),
            WindowDescription: ("handle", "epoch", "frame_seqs", "descriptors", "regions", "error"),
        }
        for cls, expected in definitions.items():
            self.assertEqual(tuple(f.name for f in fields(cls)), expected)
        with self.assertRaises(FrozenInstanceError):
            self.a.generation = 999

    def test_frame_is_owned_readonly_contiguous_and_content_digested(self):
        with prohibit_external_io():
            f = self.r.admit_frame(self.a, 0, self.arr)
        before = f.rgb.tobytes()
        self.arr.fill(255)
        self.assertEqual(f.rgb.tobytes(), before)
        self.assertFalse(f.rgb.flags.writeable)
        self.assertTrue(f.rgb.flags.c_contiguous)
        self.assertEqual(f.digest, hashlib.sha256(before).hexdigest())
        with self.assertRaises(ValueError):
            f.rgb.setflags(write=True)
        with self.assertRaises(FrozenInstanceError):
            f.epoch = 99

    def test_reject_malformed_rgb_dimensions_dtype_digest(self):
        with self.assertRaises(FrameValidationError):
            FrameRef.from_rgb(self.a, 0, 0, np.ones((3, 4, 3), dtype=np.float32))
        with self.assertRaises(FrameValidationError):
            FrameRef.from_rgb(self.a, 0, 0, np.ones((3, 4), dtype=np.uint8))
        digest = hashlib.sha256(self.arr.tobytes()).hexdigest()
        with self.assertRaises(FrameValidationError):
            FrameRef(self.a, 0, 0, 10, 3, self.arr, digest)
        with self.assertRaises(FrameValidationError):
            FrameRef(self.a, 0, 0, 5, 3, self.arr, "0" * 64)

    def test_admission_strict_frame_sequence_and_epoch(self):
        f0 = self.r.admit_frame(self.a, 0, self.arr)
        with self.assertRaises(FrameValidationError):
            self.r.admit_frame(self.a, 0, self.arr)
        f1 = self.r.admit_frame(self.a, 1, self.arr[::-1].copy())
        with self.assertRaises(FrameValidationError):
            self.r.admit_observation(self.a, f0, state={}, dpr_ppm=1_000_000)
        with self.assertRaises(FrameValidationError):
            self.r.admit_observation(self.a, FrameRef.from_rgb(self.a, 0, 1, self.arr), state={}, dpr_ppm=1_000_000)
        self.assertEqual(self.r.admit_observation(self.a, f1, state={}, dpr_ppm=1_000_000).frame_seq, 1)
        with self.assertRaises(FrameValidationError):
            self.r.admit_frame(self.a, 0, self.arr)
        self.r.advance_epoch(self.a)
        with self.assertRaises(FrameValidationError):
            self.r.admit_frame(self.a, 2, self.arr, epoch=0)
        with self.assertRaises(FrameValidationError):
            self.r.admit_observation(self.a, f0, state={}, dpr_ppm=1_000_000)
        f1 = self.r.admit_frame(self.a, 0, self.arr, epoch=1)
        self.assertEqual(f1.frame_seq, 0)

    def test_observation_identity_dpr_and_state_snapshot(self):
        f = self.r.admit_frame(self.a, 1, self.arr)
        state = {"ax": {"role": "button"}}
        obs = self.r.admit_observation(self.a, f, state=state, dpr_ppm=1_250_000)
        state["ax"]["role"] = "invalid mutation"
        self.assertEqual(obs.state["ax"]["role"], "button")
        self.assertEqual(obs.handle, self.a)
        with self.assertRaises(RegistryError):
            Observation(self.a, 0, 1, 0, {}, f)
        with self.assertRaises(FrameValidationError):
            Observation(self.b, 0, 1, 1_000_000, {}, f)
        with self.assertRaises(FrameValidationError):
            self.r.admit_observation(self.b, f, state={}, dpr_ppm=1_000_000)

    def test_window_frames_owned_and_sequences_strict(self):
        snap = WindowFrames(self.a, 0, (self.arr, self.arr[::-1]), (7, 8), 1_000_000)
        self.arr.fill(0)
        self.assertFalse(snap.frames[0].flags.writeable)
        self.assertTrue(snap.frames[1].flags.c_contiguous)
        with self.assertRaises(FrameValidationError):
            WindowFrames(self.a, 0, (self.arr,), (0, 1), 1_000_000)
        with self.assertRaises(FrameValidationError):
            WindowFrames(self.a, 0, (self.arr, self.arr), (1, 1), 1_000_000)
        with self.assertRaises(FrameValidationError):
            WindowFrames(self.a, 0, (FrameRef.from_rgb(self.b, 0, 0, self.arr),), (0,), 1_000_000)
        desc = WindowDescription(self.a, 0, (7, 8), ("sha-A", "sha-B"), ((), ()), None)
        self.assertEqual(desc.frame_seqs, snap.frame_seqs)
        with self.assertRaises(FrameValidationError):
            WindowDescription(self.a, 0, (9, 8), (), (), None)

    def test_remaining_records_bind_handle_epoch_and_copy_mutables(self):
        payload = {"op": ["click"]}
        req = ActionRequest(self.a, "act-1", 0, payload, 2, 9_000)
        payload["op"].append("bad")
        self.assertEqual(req.payload, {"op": ["click"]})
        lease = ActorLease(self.a, "worker", self.a.generation, b"random-token", 10_000)
        self.assertEqual(FocusLease(lease, "domain-A", 0).lease, lease)
        self.assertEqual(StreamSpec(5, 3, 2, 90).slots, 2)
        self.assertEqual(StreamToken(self.a, 0).handle, self.a)
        self.assertEqual(Ticket("act-1", self.a).action_id, "act-1")
        claims = np.ones((2, 3), dtype=np.uint8)
        change = ChangeResult(self.a, 0, True, claims, {"ops": 1})
        claims[:] = 0
        self.assertEqual(int(change.claims.sum()), 6)
        self.assertFalse(change.claims.flags.writeable)
        self.assertIsNone(PerceptionResult(self.a, None, "no frame").observation)
        with self.assertRaises(RegistryError):
            ActorLease(self.a, "worker", self.a.generation + 1, b"nonce", 0)
        with self.assertRaises(RegistryError):
            StreamSpec(5, 3, 2, 89)
        with self.assertRaises(RegistryError):
            ActionRequest(self.a, "act-2", 0, {}, -1, 0)
        with self.assertRaises(FrameValidationError):
            PerceptionResult(self.b, Observation(self.a, 0, 0, 1_000_000, {}, FrameRef.from_rgb(self.a, 0, 0, self.arr)), None)


if __name__ == "__main__":
    import unittest
    unittest.main()
