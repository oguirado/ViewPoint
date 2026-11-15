"""Offline target-frame identity and actor-fence controls.

This suite uses real registry, client, validator, and executor producers. Only
external T2 codecs/sinks and transport endpoints are RAM-only doubles.
Fixtures/axes are TARGET, never host-performance measurements.
"""
import hashlib
import unittest
from types import SimpleNamespace


class RamSink:
    def __init__(self):
        self.payloads = []
        self.bytes_sent = 0
        self.command_batches = 0

    def __call__(self, payload):
        if type(payload) is not bytes:
            raise AssertionError("RAM sink requires exact bytes")
        self.payloads.append(payload)
        self.bytes_sent += len(payload)
        self.command_batches += 1


class RamTransport:
    def __init__(self):
        self.commands = 0
        self.bytes_sent = 0
        self.cleanup_calls = 0

    def send(self, method, args):
        self.commands += 1
        self.bytes_sent += len(repr((method, args)).encode("utf-8"))

    def close_target(self, canonical_id):
        self.cleanup_calls += 1


class TestGodotTargetFrames(unittest.TestCase):
    def _case(self, name="offline-window"):
        # Producers are imported lazily; absent imports or changed constructors
        # are hard test failures, never implicit compatibility shims.
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.godot import GodotT2Client
        from wender.act.godot_executor import GodotExecutor
        from wender.perceive.godot_window_target import GodotWindowTarget
        from wender.state import schema
        from tests.test_godot_t2_adapter import (
            _fixture_encode_batch, _fixture_decode_batches,
            _make_action_schemas, _make_valid_base_and_sidecar,
        )

        registry = SessionRegistry(clock=lambda: 123456789)
        transport = RamTransport()
        handle = registry.register("godot_window", name, transport, owned=False)
        base, sidecar = _make_valid_base_and_sidecar()
        base["nodes"][0]["states"]["tick"] = "12"
        sidecar["base_sha256"] = hashlib.sha256(
            schema.dumps(base).encode("utf-8")).hexdigest()
        self.assertEqual(schema.validate_state(base), [])
        sink = RamSink()
        client = GodotT2Client(
            "ram_inbox_" + name, "ram_outbox_" + name, "offline-publisher",
            decode_batches=_fixture_decode_batches,
            encode_batch=_fixture_encode_batch, sink=sink)
        executor = GodotExecutor(
            {"principal": "offline", "channel": "t2", "slot_kind": "sandbox",
             "automation_active": False},
            _make_action_schemas(), _fixture_encode_batch, sink,
            t2_inbox="ram_inbox_" + name, t2_outbox="ram_outbox_" + name,
            publisher="offline-publisher")
        registries = {
            "session_registry": registry, "epoch": 0,
            "screens": {"portfolio_overview", "Holdings"},
            "cadences": {"tick", "quarterly"},
            "frame_evidence": lambda f: (12, sidecar["base_sha256"]),
            "prerequisite_gate": lambda h, e: True,
        }
        target = GodotWindowTarget.bind(
            handle, client, executor, registries=registries)
        pair = _fixture_encode_batch([base, sidecar])
        return SimpleNamespace(
            handle=handle, registry=registry, transport=transport,
            client=client, executor=executor, registries=registries,
            target=target, base=base, sidecar=sidecar, sink=sink,
            pair=pair, encode=_fixture_encode_batch)

    def _pixels(self, case, seq=1, shape=(6, 11, 3)):
        import numpy as np
        from wender.perceive.session_registry import FrameRef
        rgb = np.random.default_rng(47000 + seq).integers(
            0, 256, size=shape, dtype=np.uint8)
        digest = hashlib.sha256(rgb.tobytes(order="C")).hexdigest()
        ref = FrameRef(case.handle, 0, seq, shape[1], shape[0], rgb, digest)
        return rgb, ref

    def _command(self, seq=1):
        return {"tick": 12, "seq": seq, "cmd": "new_game", "args": {"seed": 7}}

    def _make_outcome(self, case, *, seq=1, status="accepted", basis=None):
        return {"seq": seq, "requested_tick": 12, "applied_tick": 12,
                "order_seq_id": seq, "status": status, "reason": None,
                "state_sha256": case.sidecar["base_sha256"] if basis is None else basis}

    def _assert_no_actor_write(self, case):
        self.assertEqual(case.sink.command_batches, 0)
        self.assertEqual(case.sink.bytes_sent, 0)
        self.assertEqual(case.executor.next_seq, 1)
        self.assertEqual(case.target.command_seq, 1)
        self.assertEqual(case.transport.commands, 0)
        self.assertEqual(case.transport.bytes_sent, 0)
        self.assertEqual(case.transport.cleanup_calls, 0)

    def test_four_actual_rgb_windows_dispatch_only_after_provenance(self):
        from wender.perceive.session_registry import Observation
        cases = [self._case("window-" + str(i)) for i in range(4)]
        for i, c in enumerate(cases):
            # Four 640 x 360 TARGET fixture windows; no VM time is a host SLO.
            source, frame = self._pixels(c, shape=(360, 640, 3))
            independent_sha = hashlib.sha256(source.tobytes()).hexdigest()
            observed = c.target.perceive(c.pair, frame=frame)
            self.assertIsInstance(observed, Observation)
            self.assertEqual(observed.frame.digest, independent_sha)
            self.assertEqual(observed.frame.rgb.tobytes(), source.tobytes())
            self.assertEqual((observed.handle, observed.epoch, observed.frame_seq),
                             (c.handle, 0, 1))
            self.assertFalse(observed.frame.rgb.flags.writeable)
            with self.assertRaises(ValueError):
                observed.frame.rgb.setflags(write=True)
            self.assertIsInstance(c.target.snapshot(require_pixels=True), Observation)
            self.assertTrue(c.target.visual_corroborated)
            source[:] = 0
            self.assertEqual(observed.frame.digest, independent_sha)
            self.assertEqual(hashlib.sha256(observed.frame.rgb.tobytes()).hexdigest(),
                             independent_sha)
            accepted = c.target.dispatch(self._command(), action_id="actual-" + str(i))
            self.assertEqual(accepted["status"], "admitted")
            self.assertEqual(c.sink.command_batches, 1)
            self.assertGreater(c.sink.bytes_sent, 0)
            self.assertEqual(c.executor.next_seq, 2)
            self.assertEqual(c.transport.commands, 0)
            self.assertEqual(c.target.receipts, ())

    def test_missing_frame_is_metadata_only_and_cannot_unlock_pixel_dispatch(self):
        from wender.perceive.session_registry import Observation
        from wender.perceive.godot_window_target import (
            GodotMetadataObservation, GodotPrerequisiteError,
        )
        c = self._case()
        status = c.target.perceive(c.pair)
        self.assertIsInstance(status, GodotMetadataObservation)
        self.assertNotIsInstance(status, Observation)
        self.assertIsNone(status.frame)
        self.assertEqual(status.frame_seq, 0)
        self.assertEqual(c.target.snapshot().state, c.base)
        with self.assertRaises(GodotPrerequisiteError):
            c.target.snapshot(require_pixels=True)
        with self.assertRaises(GodotPrerequisiteError):
            c.target.dispatch(self._command(), action_id="status-only")
        self.assertEqual(c.registry._last_frame_seq, {})
        self._assert_no_actor_write(c)

    def test_digest_mismatch_fails_before_codec_state_or_registry_admission(self):
        from wender.perceive.godot_window_target import GodotProtocolError
        c = self._case()
        rgb, good = self._pixels(c)
        bad = SimpleNamespace(
            handle=good.handle, epoch=good.epoch, frame_seq=good.frame_seq,
            width=good.width, height=good.height, rgb=rgb, digest="0" * 64)
        with self.assertRaisesRegex(GodotProtocolError, "digest mismatch"):
            c.target.perceive(c.pair, frame=bad)
        self.assertIsNone(c.target._last_observation)
        self.assertEqual(c.target.tick_trail, ())
        self.assertEqual(c.target.pending_bytes, b"")
        self.assertEqual(c.target._frame_seq, 0)
        self.assertEqual(c.registry._last_frame_seq, {})
        self._assert_no_actor_write(c)

    def test_stale_capture_handle_or_epoch_refuses_without_publication(self):
        from wender.perceive.session_registry import Handle
        from wender.perceive.godot_window_target import GodotPrerequisiteError
        c = self._case()
        rgb, good = self._pixels(c)
        for changes in (
            {"handle": Handle("godot_window", c.handle.canonical_id, c.handle.generation + 1)},
            {"epoch": 1},
        ):
            fields = {"handle": good.handle, "epoch": good.epoch,
                      "frame_seq": good.frame_seq, "width": good.width,
                      "height": good.height, "rgb": rgb, "digest": good.digest}
            fields.update(changes)
            with self.subTest(changes=changes), self.assertRaises(
                    GodotPrerequisiteError):
                c.target.perceive(c.pair, frame=SimpleNamespace(**fields))
            self.assertIsNone(c.target._last_observation)
            self.assertEqual(c.target._frame_seq, 0)
            self.assertEqual(c.registry._last_frame_seq, {})
        self._assert_no_actor_write(c)

    def test_sequence_regression_keeps_previous_observation_and_no_new_state(self):
        from wender.perceive.godot_window_target import GodotProtocolError
        c = self._case()
        _, frame = self._pixels(c)
        first = c.target.perceive(c.pair, frame=frame)
        original_trail = c.target.tick_trail
        with self.assertRaisesRegex(GodotProtocolError, "sequence"):
            c.target.perceive(c.encode([{"tick": 12}]), frame=frame)
        self.assertIs(c.target._last_observation, first)
        self.assertEqual(c.target.tick_trail, original_trail)
        self.assertEqual(c.target._frame_seq, 1)
        self.assertEqual(c.registry._last_frame_seq[(c.handle, 0)], 1)
        self._assert_no_actor_write(c)

    def test_epoch_change_rejects_late_outcome_before_settlement(self):
        from wender.perceive.godot_window_target import GodotPrerequisiteError
        c = self._case()
        _, frame = self._pixels(c)
        c.target.perceive(c.pair, frame=frame)
        self.assertEqual(c.target.dispatch(self._command(), action_id="old")["status"],
                         "admitted")
        # Freeze actual late-outcome bytes before the registry changes epoch.
        late_outcome_bytes = c.encode([self._make_outcome(c)])
        self.assertIs(type(late_outcome_bytes), bytes)
        self.assertEqual(c.registry.advance_epoch(c.handle), 1)
        before = c.target._last_observation
        with self.assertRaises(GodotPrerequisiteError):
            c.target.perceive(late_outcome_bytes)
        self.assertIs(c.target._last_observation, before)
        self.assertEqual(c.target.receipts, ())
        self.assertEqual(c.sink.command_batches, 1)
        self.assertEqual(c.executor.next_seq, 2)

    def test_stop_quarantines_old_handle_and_rebind_does_not_settle_old_seq(self):
        from wender.perceive.session_registry import QuarantinedTargetError
        from wender.perceive.godot_window_target import GodotPrerequisiteError
        c = self._case()
        _, frame = self._pixels(c)
        c.target.perceive(c.pair, frame=frame)
        self.assertEqual(c.target.dispatch(self._command(), action_id="old")["status"],
                         "admitted")
        retirement = c.target.stop()
        self.assertEqual(retirement.status, "stopped")
        self.assertEqual(c.transport.cleanup_calls, 0)
        with self.assertRaises(QuarantinedTargetError):
            c.registry.register("godot_window", c.handle.canonical_id,
                                RamTransport(), owned=False)
        c.registry.acknowledge_cleanup(c.handle)
        new_transport = RamTransport()
        fresh = c.registry.register("godot_window", c.handle.canonical_id,
                                    new_transport, owned=False)
        self.assertGreater(fresh.generation, c.handle.generation)
        c.registries["epoch"] = 0
        new_target = c.target.rebind(
            fresh, c.client, c.executor, registries=c.registries)
        with self.assertRaises(GodotPrerequisiteError):
            c.target.perceive(c.pair, frame=frame)
        with self.assertRaises(GodotPrerequisiteError):
            c.target.dispatch(self._command(seq=2), action_id="old-target")
        c.handle, c.target = fresh, new_target
        _, new_frame = self._pixels(c)
        c.target.perceive(c.pair, frame=new_frame)
        self.assertEqual(c.target.dispatch(self._command(seq=2), action_id="new")["status"],
                         "admitted")
        self.assertEqual(c.sink.command_batches, 2)
        c.target.perceive(c.encode([self._make_outcome(c, seq=1)]))
        self.assertEqual(c.target.receipts, ())
        self.assertEqual(c.executor.next_seq, 3)

    def test_close_inside_gate_refuses_before_any_executor_send(self):
        from wender.perceive.godot_window_target import GodotPrerequisiteError
        c = self._case()
        _, frame = self._pixels(c)
        c.target.perceive(c.pair, frame=frame)

        def gate_closes_target(handle, epoch):
            self.assertEqual(handle, c.handle)
            c.target.close()
            return True

        with self.assertRaises(GodotPrerequisiteError):
            c.target.dispatch(self._command(), action_id="closed",
                              prerequisite_gate=gate_closes_target)
        self.assertEqual(c.registry.tombstone(c.handle)[0].closed, True)
        self.assertEqual(c.target.receipts, ())
        self.assertEqual(c.registry._last_frame_seq[(c.handle, 0)], 1)
        self._assert_no_actor_write(c)

    def test_admitted_is_not_applied_wrong_proposed_and_true_outcome_correlation(self):
        c = self._case()
        _, frame = self._pixels(c)
        c.target.perceive(c.pair, frame=frame)
        self.assertEqual(c.target.dispatch(self._command(), action_id="one")["status"],
                         "admitted")
        self.assertEqual(c.target.receipts, ())
        c.target.perceive(c.encode([self._make_outcome(c, basis="f" * 64)]))
        self.assertEqual(c.target.receipts, ())
        c.target.perceive(c.encode([self._make_outcome(c, status="proposed")]))
        self.assertEqual(c.target.receipts, ())
        c.target.perceive(c.encode([self._make_outcome(c)]))
        self.assertEqual(len(c.target.receipts), 1)
        self.assertEqual(c.target.receipts[0].action_id, "one")
        self.assertEqual(c.target.receipts[0].status, "accepted")
        self.assertEqual(c.sink.command_batches, 1)
        self.assertEqual(c.executor.next_seq, 2)


if __name__ == "__main__":
    unittest.main()
