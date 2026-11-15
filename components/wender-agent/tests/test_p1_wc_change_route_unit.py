"""Offline C-lane identity, refusal, boundedness and dispatch regression tests.

All RGB arrays are synthesized independently in RAM from deterministic seeds.
Production classes are imported during undecorated tests (never replaced or
silently imported through a fallback). There are no sleeps, UI dependencies,
network connections, external commands, disk frame fixtures or skipped gates.
"""
from dataclasses import fields
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
from types import SimpleNamespace
import unittest

import numpy as np


class SpyTransport:
    """Fake external transport, with visible sends and bytes (no network)."""

    def __init__(self):
        self.sends = []
        self.bytes_sent = 0
        self.reservations = 0
        self.cleanup = 0

    def send(self, method, payload):
        self.sends.append((method, dict(payload)))
        self.bytes_sent += len(repr(payload).encode("utf-8"))

    def close_target(self, _canonical_id):
        self.cleanup += 1


class DispatchSpy:
    def __init__(self):
        self.events = []
        self.transports = {}
        self.raise_after_send = False

    def __call__(self, event):
        key = (event.handle.kind, event.handle.canonical_id, event.handle.generation)
        self.transports[key].send(
            "Change.route", {"epoch": event.epoch, "frame_seq": event.frame_seq,
                             "sha256": event.source_sha256, "status": event.status}
        )
        self.events.append(event)
        if self.raise_after_send:
            raise OSError("injected post-send uncertainty")


class ReverseCompletionRunner:
    """Maps whole real jobs, then delivers completed lane groups in reverse."""

    def map(self, function, jobs):
        completed = [function(job) for job in jobs]
        return reversed(completed)


class TestChangeRoute(unittest.TestCase):
    def setUp(self):
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.vision_adapter import FakeVisionEngine
        from wender.perceive.proto1_change_route import ChangeEvidenceRouter

        self.registry = SessionRegistry(clock=lambda: 1000000)
        self.publisher = DispatchSpy()
        self.engine = FakeVisionEngine(tile=1, threshold=1)
        self.router = ChangeEvidenceRouter(
            self.registry, adapter=self.engine, publisher=self.publisher,
            max_lanes=12, max_history=4, max_work_units=5000,
            max_rgb_bytes=1200, max_batch_items=16,
        )

    def handle(self, kind="cdp_tab", name="display", owned=True):
        transport = SpyTransport()
        handle = self.registry.register(kind, name, transport, owned=owned)
        self.publisher.transports[(handle.kind, handle.canonical_id, handle.generation)] = transport
        return handle

    @staticmethod
    def pixels(seed, *, height=3, width=4):
        rng = np.random.default_rng(seed)
        return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)

    def observation(self, handle, seq, rgb=None, *, epoch=None):
        if rgb is None:
            rgb = self.pixels(100 + seq)
        if epoch is None:
            epoch = self.registry.resolve(handle).epoch
        frame = self.registry.admit_frame(handle, seq, rgb, epoch=epoch)
        state = {"nodes": [], "viewport": {"dpr_ppm": 1000000},
                 "handle": {"kind": handle.kind, "canonical_id": handle.canonical_id,
                            "generation": handle.generation}, "epoch": epoch}
        return self.registry.admit_observation(
            handle, frame, state=state, dpr_ppm=1000000,
        )

    @staticmethod
    def struct(value, **overrides):
        """Cross-constructor structural record without a permissive class fallback."""
        attrs = {name: getattr(value, name) for name in value.__dataclass_fields__}
        attrs.update(overrides)
        return SimpleNamespace(**attrs)

    @staticmethod
    def digest_history(router, handle, epoch=0):
        return tuple((ev.frame_seq, ev.source_sha256) for ev in router.lane_history(handle, epoch))

    def assert_refusal_unchanged_sibling(self, target, candidate, sibling, code):
        from wender.perceive.proto1_change_route import ChangeRouteError
        history_before = self.digest_history(self.router, sibling)
        sends = {k: (len(t.sends), t.bytes_sent, t.reservations, t.cleanup)
                 for k, t in self.publisher.transports.items()}
        route_count = self.router.publication_count
        with self.assertRaises(ChangeRouteError) as cm:
            self.router.route(candidate)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.router.publication_count, route_count)
        self.assertEqual(self.digest_history(self.router, sibling), history_before)
        for key, transport in self.publisher.transports.items():
            self.assertEqual((len(transport.sends), transport.bytes_sent,
                              transport.reservations, transport.cleanup), sends[key])
        self.assertTrue(target is not None)

    def test_real_reference_fields_and_independent_digest_ownership(self):
        from wender.perceive.session_registry import FrameRef, Handle, Observation
        self.assertEqual([f.name for f in fields(Handle)],
                         ["kind", "canonical_id", "generation"])
        self.assertEqual([f.name for f in fields(FrameRef)],
                         ["handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"])
        self.assertEqual([f.name for f in fields(Observation)],
                         ["handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"])
        a = self.handle("x11_window", "own")
        source = self.pixels(1501)
        pre_sha = hashlib.sha256(source.tobytes(order="C")).hexdigest()
        obs = self.observation(a, 0, source)
        source[:, :, :] = 0  # mutable source is detached from real registry's bytes
        self.assertEqual(obs.frame.digest, pre_sha)
        self.assertEqual(hashlib.sha256(obs.frame.rgb.tobytes()).hexdigest(), pre_sha)
        self.assertFalse(obs.frame.rgb.flags.writeable)
        self.assertEqual(self.router.route(obs).status, "BASELINE")
        self.assertEqual(len(self.publisher.events), 1)

    def test_two_kinds_shared_id_reverse_completion_preserves_submission(self):
        first = self.handle("cdp_tab", "same-id")
        second = self.handle("x11_window", "same-id")
        self.assertNotEqual(first.kind, second.kind)
        self.router.route(self.observation(first, 0, self.pixels(41)))
        self.router.route(self.observation(second, 0, self.pixels(47)))
        changes = [self.observation(first, 1, self.pixels(43)),
                   self.observation(second, 1, self.pixels(49))]
        results = self.router.route_many(changes, runner=ReverseCompletionRunner())
        self.assertEqual([item.index for item in results], [0, 1])
        self.assertEqual([item.evidence.handle for item in results], [first, second])
        self.assertEqual([item.evidence.status for item in results], ["CHANGED", "CHANGED"])
        self.assertTrue(all(item.error is None for item in results))
        self.assertEqual(self.router.publication_count, 4)
        self.assertEqual([len(self.digest_history(self.router, h)) for h in (first, second)], [2, 2])
        self.assertEqual([len(t.sends) for t in self.publisher.transports.values()], [2, 2])

    def test_serialized_stale_gap_cannot_absorb_sibling_lane(self):
        one = self.handle("cdp_tab", "gap")
        two = self.handle("x11_window", "gap")
        self.router.route(self.observation(one, 0))
        self.router.route(self.observation(two, 0))
        bad = self.observation(one, 2)
        self.assert_refusal_unchanged_sibling(one, bad, two, "FRAME_GAP")
        good = self.router.route(self.observation(two, 1))
        self.assertEqual(good.status, "CHANGED")
        self.assertEqual(self.digest_history(self.router, one)[-1][0], 0)
        self.assertEqual(self.digest_history(self.router, two)[-1][0], 1)
        # The only recovery after an unfillable registry seq gap is a trusted
        # new epoch, not silently comparing nonconsecutive frames.
        next_epoch = self.registry.advance_epoch(one)
        self.assertEqual(next_epoch, 1)
        self.assertEqual(self.router.route(self.observation(one, 0, epoch=1)).status, "BASELINE")
        self.assertEqual(self.digest_history(self.router, one, 0)[-1][0], 0)
        self.assertEqual(self.digest_history(self.router, one, 1)[-1][0], 0)

    def test_geometry_resize_same_epoch_requires_explicit_bump(self):
        one = self.handle("cdp_tab", "resized")
        two = self.handle("x11_window", "resized")
        self.router.route(self.observation(one, 0))
        self.router.route(self.observation(two, 0))
        changed_geom = self.observation(one, 1, self.pixels(5, height=4, width=4))
        self.assert_refusal_unchanged_sibling(one, changed_geom, two, "SAME_EPOCH_RESIZE")
        self.registry.advance_epoch(one)
        self.assertEqual(self.router.route(self.observation(one, 0, self.pixels(6, height=4), epoch=1)).status,
                         "BASELINE")

    def test_old_epoch_stop_and_retired_generation_are_fenced(self):
        old = self.handle("cdp_tab", "retired")
        sibling = self.handle("x11_window", "retired")
        obs = self.observation(old, 0)
        self.router.route(obs)
        self.router.route(self.observation(sibling, 0))
        self.registry.advance_epoch(old)
        self.assert_refusal_unchanged_sibling(old, obs, sibling, "STALE_EPOCH")
        # Stop performs no cleanup, quarantines the alias and tombstones generation.
        transport = self.publisher.transports[(old.kind, old.canonical_id, old.generation)]
        before = len(transport.sends)
        receipt = self.registry.stop(old)
        self.assertEqual(receipt.status, "stopped")
        self.assertEqual(len(transport.sends), before)
        self.assertEqual(transport.cleanup, 0)
        self.assert_refusal_unchanged_sibling(old, obs, sibling, "STALE_GENERATION")
        self.registry.acknowledge_cleanup(old)
        current = self.handle("cdp_tab", "retired")
        self.assertGreater(current.generation, old.generation)
        self.assert_refusal_unchanged_sibling(old, obs, sibling, "STALE_GENERATION")
        self.router.route(self.observation(current, 0, self.pixels(200)))
        self.assertEqual(self.digest_history(self.router, sibling)[-1][0], 0)

    def test_bool_epoch_sequence_generation_refused(self):
        from wender.perceive.proto1_change_route import ChangeRouteError
        one = self.handle("cdp_tab", "bool")
        two = self.handle("x11_window", "bool")
        self.router.route(self.observation(two, 0))
        original = self.observation(one, 0)
        for update in (dict(frame_seq=True), dict(epoch=True)):
            self.assert_refusal_unchanged_sibling(one, self.struct(original, **update), two,
                                                  "INVALID_EVIDENCE")
        bogus = SimpleNamespace(kind=one.kind, canonical_id=one.canonical_id, generation=True)
        # Positive generation is checked before any routing operation.
        with self.assertRaises(ChangeRouteError) as cm:
            self.router.route(self.struct(original, handle=bogus))
        self.assertIn(cm.exception.code, ("INVALID_EVIDENCE", "INVALID_HANDLE"))

    def test_digest_tampering_and_mutable_root_fail_closed(self):
        one = self.handle("cdp_tab", "pixels")
        two = self.handle("x11_window", "pixels")
        self.router.route(self.observation(two, 0))
        original = self.observation(one, 0)
        fake_ref = self.struct(original.frame, digest="0" * 64)
        self.assert_refusal_unchanged_sibling(one, self.struct(original, frame=fake_ref), two, "BAD_DIGEST")
        # A writable copy or readonly view whose ultimate owner is writable
        # cannot claim the FrameRef's immutable, owned storage contract.
        mutable = np.array(original.frame.rgb, copy=True)
        fake_ref = self.struct(original.frame, rgb=mutable)
        self.assert_refusal_unchanged_sibling(one, self.struct(original, frame=fake_ref), two, "BAD_PIXELS")
        mutable.setflags(write=False)
        fake_ref = self.struct(original.frame, rgb=mutable)
        self.assert_refusal_unchanged_sibling(one, self.struct(original, frame=fake_ref), two, "BAD_PIXELS")

    def test_detector_exhaustion_never_publishes_unchanged_or_settled(self):
        from wender.perceive.proto1_change_route import ChangeEvidenceRouter, ChangeRouteError, DetectorExhausted
        from wender.perceive.vision_adapter import FakeVisionEngine
        one = self.handle("cdp_tab", "operation-limit")
        low = ChangeEvidenceRouter(self.registry, adapter=self.engine,
                                   publisher=self.publisher, max_work_units=1)
        with self.assertRaises(ChangeRouteError) as cm:
            low.route(self.observation(one, 0))
        self.assertEqual(cm.exception.code, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(self.publisher.events, [])
        self.assertEqual(low.publication_count, 0)

        class ExhaustingDetector(FakeVisionEngine):
            def changed_regions(self, prev, cur):
                raise DetectorExhausted("exhausted operation allowance")

        normal = ChangeEvidenceRouter(self.registry, adapter=ExhaustingDetector(tile=1, threshold=1),
                                      publisher=self.publisher, max_work_units=5000)
        baseline = normal.route(self.observation(one, 1))
        self.assertIsNone(baseline.settled)
        with self.assertRaises(ChangeRouteError) as cm:
            normal.route(self.observation(one, 2))
        self.assertEqual(cm.exception.code, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(len(self.publisher.events), 1)
        self.assertEqual(normal.publication_count, 1)
        self.assertEqual(len(normal.lane_history(one, 0)), 1)

    def test_idempotent_claim_and_conflicting_content(self):
        one = self.handle("cdp_tab", "replay")
        original = self.observation(one, 0)
        self.assertEqual(self.router.route(original).status, "BASELINE")
        self.assertIsNone(self.router.route(original))
        self.assertEqual(len(self.publisher.events), 1)
        # Two independently hashed buffers at the same identity/seq MUST NOT
        # allow a second publication or overwrite original history.
        alternative = self.pixels(803)
        replacement_sha = hashlib.sha256(alternative.tobytes()).hexdigest()
        from wender.perceive.session_registry import FrameRef
        new_ref = FrameRef(one, 0, 0, 4, 3, alternative, replacement_sha)
        contradictory = self.struct(original, frame=new_ref)
        from wender.perceive.proto1_change_route import ChangeRouteError
        with self.assertRaises(ChangeRouteError) as cm:
            self.router.route(contradictory)
        self.assertEqual(cm.exception.code, "REPLAY_CONFLICT")
        self.assertEqual(self.router.publication_count, 1)
        self.assertEqual(self.digest_history(self.router, one), ((0, original.frame.digest),))

    def test_c_score_identity_requirements_and_absent_score(self):
        from wender.perceive.proto1_change_route import (
            ChangeRouteError, c_score_identity_preconditions,
        )
        one = self.handle("cdp_tab", "score")
        sibling = self.handle("x11_window", "score")
        first = self.router.route(self.observation(one, 0, self.pixels(61)))
        second = self.router.route(self.observation(one, 1, self.pixels(62)))
        foreign = self.router.route(self.observation(sibling, 0, self.pixels(65)))
        eligibility = c_score_identity_preconditions((first, second))
        self.assertEqual(eligibility.frame_seqs, (0, 1))
        self.assertEqual(eligibility.source_digests,
                         (hashlib.sha256(first.frame.rgb.tobytes()).hexdigest(),
                          hashlib.sha256(second.frame.rgb.tobytes()).hexdigest()))
        self.assertIsNone(eligibility.score)
        self.assertIsNone(eligibility.settled)
        self.assertEqual(eligibility.missing_producer, "AUSENTE")
        for bad in ((first, foreign), (first,), (second, first)):
            with self.assertRaises(ChangeRouteError):
                c_score_identity_preconditions(bad)
        with self.assertRaises(ChangeRouteError) as cm:
            c_score_identity_preconditions((first, self.struct(second, source_sha256="0" * 64)))
        self.assertEqual(cm.exception.code, "BAD_DIGEST")

    def test_threaded_duplicate_claims_only_publish_once(self):
        one = self.handle("cdp_tab", "parallel-duplicate")
        obs = self.observation(one, 0)
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            return self.router.route(obs)

        with ThreadPoolExecutor(max_workers=6) as pool:
            completed = list(pool.map(lambda _: worker(), range(6)))
        self.assertEqual(sum(x is not None for x in completed), 1)
        self.assertEqual(sum(x is None for x in completed), 5)
        self.assertEqual(self.router.publication_count, 1)
        self.assertEqual(len(self.publisher.events), 1)
        self.assertEqual(len(next(iter(self.publisher.transports.values())).sends), 1)

    def test_failed_publisher_consumes_uncertain_claim(self):
        from wender.perceive.proto1_change_route import ChangeRouteError
        one = self.handle("cdp_tab", "send-uncertain")
        self.router.route(self.observation(one, 0))
        change = self.observation(one, 1)
        self.publisher.raise_after_send = True
        with self.assertRaises(ChangeRouteError) as cm:
            self.router.route(change)
        self.assertEqual(cm.exception.code, "PUBLICATION_UNCERTAIN")
        self.publisher.raise_after_send = False
        self.assertIsNone(self.router.route(change))
        tx = self.publisher.transports[(one.kind, one.canonical_id, one.generation)]
        self.assertEqual(len(tx.sends), 2)
        self.assertEqual(self.router.publication_count, 1)  # only confirmed sends

    def test_batch_invalid_item_cannot_poison_valid_sibling(self):
        first = self.handle("cdp_tab", "batch")
        second = self.handle("x11_window", "batch")
        invalid = self.struct(self.observation(first, 0), frame_seq=True)
        valid = self.observation(second, 0)
        outcomes = self.router.route_many((invalid, valid), runner=ReverseCompletionRunner())
        self.assertEqual([outcome.index for outcome in outcomes], [0, 1])
        self.assertIsNone(outcomes[0].evidence)
        self.assertIn("INVALID_EVIDENCE", outcomes[0].error)
        self.assertEqual(outcomes[1].evidence.handle, second)
        self.assertEqual(self.router.publication_count, 1)
        self.assertEqual(self.digest_history(self.router, first), ())
        self.assertEqual(len(self.digest_history(self.router, second)), 1)

    def test_real_parallel_perception_grouping_matches_full_identity(self):
        from wender.perceive.parallel_perceive import perceive_many
        first = self.handle("cdp_tab", "perception")
        sibling = self.handle("x11_window", "perception")
        one = self.observation(first, 0, self.pixels(1001))
        two = self.observation(sibling, 0, self.pixels(1002))
        results = perceive_many([one, two], adapter=self.engine, runner=ReverseCompletionRunner())
        self.assertEqual([r.handle for r in results], [first, sibling])
        self.assertTrue(all(r.error is None for r in results))
        for result in results:
            self.assertEqual(result.observation.state["_perception"]["handle"]["kind"],
                             result.handle.kind)
            self.assertEqual(result.observation.state["_perception"]["frame_seq"], 0)

    def test_valid_semantic_mutants_all_refused_with_independent_sibling(self):
        """Count explicit *input* semantic mutants, not import crashes as kills.

        Valid original controls are routed first in each isolated fixture.
        Each case reaches the real router and expects a typed refusal. No
        skips, expected failures, early constructor errors or equivalents.
        """
        from wender.perceive.proto1_change_route import ChangeRouteError
        mutations = (
            "shared_canonical_id_wrong_kind", "retired_generation", "old_epoch",
            "frame_gap", "changed_geometry", "unsupported_kind", "bool_sequence",
            "source_buffer_mutation", "wrong_sha256",
        )
        killed = 0
        for index, mutation in enumerate(mutations):
            with self.subTest(mutant=mutation):
                # The fixture is genuinely independent; a failed one cannot
                # contaminate another's registry or evidence population.
                self.setUp()
                first = self.handle("cdp_tab", "same")
                sibling = self.handle("x11_window", "same")
                positive_first = self.router.route(self.observation(first, 0, self.pixels(300 + index)))
                positive_sibling = self.router.route(self.observation(sibling, 0, self.pixels(400 + index)))
                self.assertEqual((positive_first.status, positive_sibling.status),
                                 ("BASELINE", "BASELINE"))
                if mutation == "frame_gap":
                    bad = self.observation(first, 2)
                    expected = "FRAME_GAP"
                elif mutation == "changed_geometry":
                    bad = self.observation(first, 1, self.pixels(501, height=4))
                    expected = "SAME_EPOCH_RESIZE"
                else:
                    candidate = self.observation(first, 1, self.pixels(510 + index))
                    if mutation == "shared_canonical_id_wrong_kind":
                        bad = self.struct(candidate, handle=sibling)
                        expected = "SIBLING_EVIDENCE"
                    elif mutation == "retired_generation":
                        self.registry.stop(first)
                        bad = candidate
                        expected = "STALE_GENERATION"
                    elif mutation == "old_epoch":
                        self.registry.advance_epoch(first)
                        bad = candidate
                        expected = "STALE_EPOCH"
                    elif mutation == "unsupported_kind":
                        bad_handle = SimpleNamespace(kind="alien_target",
                                                     canonical_id=first.canonical_id,
                                                     generation=first.generation)
                        bad = self.struct(candidate, handle=bad_handle)
                        expected = "INVALID_HANDLE"
                    elif mutation == "bool_sequence":
                        bad = self.struct(candidate, frame_seq=True)
                        expected = "INVALID_EVIDENCE"
                    elif mutation == "source_buffer_mutation":
                        fake_ref = self.struct(candidate.frame, rgb=np.array(candidate.frame.rgb, copy=True))
                        bad = self.struct(candidate, frame=fake_ref)
                        expected = "BAD_PIXELS"
                    elif mutation == "wrong_sha256":
                        bad = self.struct(candidate,
                                          frame=self.struct(candidate.frame, digest="0" * 64))
                        expected = "BAD_DIGEST"
                    else:
                        self.fail("invalid control mutation")
                history = self.digest_history(self.router, sibling)
                sends_before = {k: len(t.sends) for k, t in self.publisher.transports.items()}
                publications_before = self.router.publication_count
                with self.assertRaises(ChangeRouteError) as cm:
                    self.router.route(bad)
                self.assertEqual(cm.exception.code, expected)
                self.assertEqual(self.digest_history(self.router, sibling), history)
                self.assertEqual(self.router.publication_count, publications_before)
                for key, transport in self.publisher.transports.items():
                    self.assertEqual(len(transport.sends), sends_before[key])
                killed += 1
        self.assertEqual(killed, len(mutations))  # 9 / 9 valid input mutants


if __name__ == "__main__":
    unittest.main()
