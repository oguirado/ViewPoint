"""Offline real-module controls for the owned PSLOT action-gate contract.

Only the declared authority, perception, clock, writer fence and CDP transport
are fake.  Production imports are deliberately lazy: an absent producer is a
test error, never a skipped test or a permissive replacement implementation.
Synthetic frames remain in RAM; failures print metadata-only counterexamples.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import importlib
import random
import threading
import unittest
from unittest.mock import patch

import numpy as np


PROPERTY_SEED = 0x513345
PROPERTY_CASES = 24


class FakeAuthority:
    """Exactly the get/validate_observation structural authority protocol.

    The fixture, not a claimed SessionRegistry API, records issued objects and
    their admitted contents.  Merely constructing an Observation is insufficient.
    """

    def __init__(self, record):
        self.record = record
        self.current = None
        self.proof = None
        self.validation_answer = True

    def get(self, handle):
        return self.record if handle == self.record.handle else None

    def validate_observation(self, observation):
        if observation is not self.current or self.proof is None:
            return False
        handle, epoch, seq, digest, dpr, state = self.proof
        if (observation.handle != handle or observation.epoch != epoch
                or observation.frame_seq != seq or observation.frame.digest != digest
                or observation.dpr_ppm != dpr or observation.state != state
                or self.record.handle != handle or self.record.epoch != epoch
                or self.record.closed):
            return False
        return self.validation_answer


class FakeLocks:
    """Only owns(), with identity, owner, nonce, expiry and revocation checks."""

    def __init__(self, lease):
        self.lease = lease
        self.enabled = True
        self.answer = True

    def owns(self, lease, *, now_ns):
        if (not self.enabled or lease is not self.lease
                or lease.owner != self.lease.owner
                or lease.nonce != self.lease.nonce or now_ns >= lease.expires_ns):
            return False
        return self.answer


class FakeWriterFence:
    """Nonblocking structural writer fence; quarantines are never released."""

    def __init__(self):
        self.mutex = threading.Lock()
        self.active = None
        self.busy = False
        self.acquired = []
        self.released = []
        self.quarantined = []

    def acquire(self, lease):
        with self.mutex:
            if self.busy or self.active is not None or self.quarantined:
                return False
            self.active = lease
            self.acquired.append(lease)
            return True

    def release(self, lease):
        with self.mutex:
            if self.active is not lease or self.quarantined:
                raise RuntimeError("release without live writer")
            self.active = None
            self.released.append(lease)

    def quarantine(self, lease):
        with self.mutex:
            self.active = lease
            if lease not in self.quarantined:
                self.quarantined.append(lease)


class FakeTransport:
    """Deterministic acknowledgement only; never opens a connection."""

    def __init__(self):
        self.calls = []
        self.fail_at = None

    def send(self, method, params, *, session_id):
        if method != "Input.dispatchMouseEvent":
            raise AssertionError("vision route attempted a non-coordinate command")
        self.calls.append((method, deepcopy(params), session_id))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("deterministic transport failure")
        return {"acknowledged": True}


class FakePerceiver:
    """Explicit deterministic observation source, with no model or network."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.reads = 0
        self.before = None
        self.after = None

    def __call__(self):
        self.reads += 1
        if not self.fixture.cdp.calls:
            if self.before is not None:
                return self.before()
            return self.fixture.observation
        if self.after is not None:
            return self.after()
        return self.fixture.post_observation()


class Fixture:
    def __init__(self, api, *, seq=7, epoch=2, generation=1,
                 width=32, height=24, box=None, name="confirm", dpr=1000000):
        self.api = api
        self.now = 100
        self.cdp = FakeTransport()
        self.session_id = "session-a"
        self.handle = api.sr.Handle("cdp_tab", "target-a", generation)
        self.record = api.sr.TargetRecord(
            self.handle, self.cdp, False, epoch, self.session_id, True, False)
        self.registry = FakeAuthority(self.record)
        self.lease = api.sr.ActorLease(
            self.handle, "worker-a", generation, b"deterministic-test-nonce", 10000)
        self.locks = FakeLocks(self.lease)
        self.policy = api.focus.FocusPolicy(self.registry, self.locks, clock=self.clock)
        self.policy.set_focus(self.handle, domain="cdp:" + self.session_id, epoch=epoch)
        self.focus = self.policy.grant(self.lease, domain="cdp:" + self.session_id)
        self.fence = FakeWriterFence()
        self.width, self.height = width, height
        self.dpr = dpr
        self.seq, self.epoch = seq, epoch
        self.node = {
            "depth": 1, "role": "button", "name": name, "ancestors": ["application"],
            "ordinal": 0, "cardinality": 1, "attrs": [["checked", "false"]],
            "backend_id": 11, "box": box or [8, 6, 10, 10], "actionable": True,
        }
        self.node["id"] = api.ax.stable_id(self.node)
        self.state = {
            "source": {"kind": "vision"},
            "nodes": [deepcopy(self.node)],
            "viewport": {"w": width * 1000000 // dpr,
                         "h": height * 1000000 // dpr, "scroll_x": 0, "scroll_y": 0},
        }
        self.observation = self.make_observation(seq=seq, state=self.state, pixel=0)
        self.admit(self.observation)
        self.target = api.gate.VisionTarget(
            self.handle, epoch, seq, self.observation.frame.digest, self.node["id"])
        self.action = {
            "op": "click", "path": "coordinates", "target": self.node["id"],
            "expect": {"target_states": {"checked": "true"}},
        }
        self.perceiver = FakePerceiver(self)
        self.sleeps = []

    def clock(self):
        return self.now

    def make_observation(self, *, seq=None, state=None, pixel=1,
                         handle=None, epoch=None, dpr=None):
        handle = self.handle if handle is None else handle
        epoch = self.epoch if epoch is None else epoch
        seq = self.seq + 1 if seq is None else seq
        dpr = self.dpr if dpr is None else dpr
        rgb = np.full((self.height, self.width, 3), pixel, dtype=np.uint8)
        frame = self.api.sr.FrameRef.from_rgb(handle, epoch, seq, rgb)
        return self.api.sr.Observation(
            handle, epoch, seq, dpr, deepcopy(self.state if state is None else state), frame)

    def admit(self, observation):
        self.registry.current = observation
        self.registry.proof = (
            observation.handle, observation.epoch, observation.frame_seq,
            observation.frame.digest, observation.dpr_ppm, deepcopy(observation.state))
        return observation

    def refresh_pre(self):
        self.observation = self.make_observation(seq=self.seq, state=self.state, pixel=0)
        self.admit(self.observation)

    def post_observation(self, *, seq=None, state=None, handle=None,
                         epoch=None, dpr=None, admit=True):
        if state is None:
            state = deepcopy(self.state)
            state["nodes"][0]["attrs"] = [["checked", "true"]]
        result = self.make_observation(
            seq=seq, state=state, handle=handle, epoch=epoch, dpr=dpr)
        return self.admit(result) if admit else result

    def kwargs(self):
        return {
            "target": self.target, "observation": self.observation,
            "read_observation": self.perceiver, "registry": self.registry,
            "lease": self.lease, "focus": self.focus, "focus_policy": self.policy,
            "writer_fence": self.fence, "clock": self.clock, "wait_ms": 0,
            "sleep": self.sleeps.append,
        }

    def run(self, **overrides):
        kw = self.kwargs()
        kw.update(overrides)
        return self.api.gate.run_vision_action(self.cdp, self.session_id, self.action, **kw)


class VisionActionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import actual exports after merge; never patch sys.modules or skip absence.
        class API:
            pass
        cls.api = API()
        cls.api.gate = importlib.import_module("wender.act.pslot_action_gate")
        cls.api.executor = importlib.import_module("wender.act.cdp_executor")
        cls.api.focus = importlib.import_module("wender.act.focus_policy")
        cls.api.sr = importlib.import_module("wender.perceive.session_registry")
        cls.api.ax = importlib.import_module("wender.perceive.ax")
        cls.api.locks = importlib.import_module("wender.act.target_lock_registry")

    def control(self, fixture=None):
        f = fixture or Fixture(self.api)
        result = f.run()
        self.assertEqual((result.ok, result.verified, result.attempts), (1, 1, 1))
        self.assertEqual(result.path, "coordinates")
        self.assertEqual(result.handle, f.handle)
        self.assertEqual(result.target_id, f.target.node_id)
        self.assertEqual(result.pre_frame_seq, f.seq)
        self.assertGreater(result.post_frame_seq, result.pre_frame_seq)
        self.assertEqual(f.perceiver.reads, 2)
        self.assertTrue(f.cdp.calls)
        self.assertTrue(all(call[2] == f.session_id for call in f.cdp.calls))
        self.assertEqual(f.fence.released, [f.lease])
        self.assertFalse(f.fence.quarantined)
        self.assertIsNone(f.fence.active)
        return f, result

    def refused(self, fixture, expected, *, dispatched=False, **overrides):
        with self.assertRaises(self.api.gate.GateRefused) as caught:
            fixture.run(**overrides)
        self.assertEqual(caught.exception.reason, getattr(self.api.gate.Refusal, expected))
        self.assertEqual(caught.exception.dispatched, dispatched)
        if dispatched:
            self.assertTrue(fixture.cdp.calls)
            self.assertIn(fixture.lease, fixture.fence.quarantined)
            self.assertFalse(fixture.fence.released)
        else:
            self.assertFalse(fixture.cdp.calls)
        return caught.exception

    def test_authorized_control_uses_real_executor_and_policy(self):
        real_executor = self.api.executor.run_ax_action
        self.assertEqual(real_executor.__module__, "wender.act.cdp_executor")
        f = Fixture(self.api)
        with patch.object(self.api.executor, "run_ax_action", wraps=real_executor) as executor:
            with patch.object(f.policy, "validate", wraps=f.policy.validate) as validate:
                self.control(f)
        executor.assert_called_once()
        self.assertGreaterEqual(validate.call_count, 2)

    def test_each_supported_existing_coordinate_operation(self):
        for op, expected_calls in (("click", 3), ("hover", 1), ("lpath_click", 4)):
            with self.subTest(op=op):
                f = Fixture(self.api)
                f.action["op"] = op
                if op == "lpath_click":
                    f.action.update(origin=[1, 1], waypoint_policy="horizontal_first")
                self.control(f)
                self.assertEqual(len(f.cdp.calls), expected_calls)

    def test_required_authority_collaborators_are_not_optional(self):
        for name in ("registry", "lease", "focus", "focus_policy", "writer_fence"):
            with self.subTest(absent=name):
                self.control()
                self.refused(Fixture(self.api), "AUTHORITY_ABSENT", **{name: None})

    def test_required_structural_methods_are_not_invented(self):
        for owner, method in (("registry", "get"), ("registry", "validate_observation"),
                              ("fence", "acquire"), ("fence", "release"),
                              ("fence", "quarantine"), ("locks", "owns")):
            with self.subTest(owner=owner, method=method):
                self.control()
                f = Fixture(self.api)
                setattr(getattr(f, owner), method, None)
                self.refused(f, "AUTHORITY_ABSENT")

    def test_concrete_registry_is_not_silently_adapted(self):
        self.control()
        f = Fixture(self.api)
        actual = self.api.sr.SessionRegistry(clock=f.clock)
        self.assertTrue(callable(getattr(actual, "get", None)))
        self.assertFalse(callable(getattr(actual, "validate_observation", None)))
        self.refused(f, "AUTHORITY_ABSENT", registry=actual)

    def test_concrete_lock_registry_refuses_unminted_lease(self):
        self.control()
        f = Fixture(self.api)
        actual = self.api.locks.TargetLockRegistry(f.registry, clock=f.clock)
        self.assertTrue(callable(getattr(actual, "owns", None)))
        f.policy.locks = actual
        self.refused(f, "AUTHORITY_REFUSED")

    def test_registry_authentication_requires_literal_true(self):
        for answer in (False, 1, "yes"):
            with self.subTest(answer=answer):
                self.control()
                f = Fixture(self.api)
                f.registry.validation_answer = answer
                self.refused(f, "AUTHORITY_REFUSED")

    def test_lock_ownership_requires_literal_true(self):
        for answer in (False, 1):
            with self.subTest(answer=answer):
                self.control()
                f = Fixture(self.api)
                f.locks.answer = answer
                self.refused(f, "AUTHORITY_REFUSED")

    def test_expired_actor_lease_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.now = f.lease.expires_ns
        self.refused(f, "AUTHORITY_REFUSED")

    def test_forged_equal_focus_object_refuses(self):
        self.control()
        f = Fixture(self.api)
        self.refused(f, "AUTHORITY_REFUSED", focus=replace(f.focus))

    def test_focus_revoked_during_pre_read_refuses_without_transport(self):
        self.control()
        f = Fixture(self.api)
        def revoke():
            f.policy.set_focus(f.handle, domain="cdp:" + f.session_id, epoch=f.epoch)
            return f.observation
        f.perceiver.before = revoke
        self.refused(f, "AUTHORITY_REFUSED")

    def test_actor_revoked_after_real_focus_consumption_keeps_refusal_reason(self):
        self.control()
        f = Fixture(self.api)
        consume = f.policy.consume
        def consume_then_revoke(token):
            consume(token)
            f.locks.enabled = False
        with patch.object(f.policy, "consume", side_effect=consume_then_revoke):
            self.refused(f, "AUTHORITY_REFUSED")

    def test_actor_revoked_between_events_stops_without_retry(self):
        self.control()
        f = Fixture(self.api)
        send = f.cdp.send
        def send_then_revoke(method, params, *, session_id):
            acknowledgement = send(method, params, session_id=session_id)
            f.locks.enabled = False
            return acknowledgement
        f.cdp.send = send_then_revoke
        self.refused(f, "AUTHORITY_REFUSED", dispatched=True)
        self.assertEqual(len(f.cdp.calls), 1)

    def test_busy_writer_refuses_without_transport(self):
        self.control()
        f = Fixture(self.api)
        f.fence.busy = True
        self.refused(f, "EXECUTOR_REFUSED")

    def test_writer_remains_held_after_real_executor_returns(self):
        f = Fixture(self.api)
        real_executor = self.api.executor.run_ax_action
        def execute_and_inspect(*args, **kwargs):
            outcome = real_executor(*args, **kwargs)
            self.assertEqual(outcome["ok"], 1)
            self.assertIs(f.fence.active, f.lease)
            self.assertFalse(f.fence.released)
            return outcome
        with patch.object(self.api.executor, "run_ax_action", side_effect=execute_and_inspect):
            self.control(f)

    def test_release_failure_overrides_verified_success_and_quarantines(self):
        self.control()
        f = Fixture(self.api)
        def unavailable(lease):
            self.assertIs(lease, f.lease)
            raise RuntimeError("deterministic writer release failure")
        f.fence.release = unavailable
        self.refused(f, "AUTHORITY_REFUSED", dispatched=True)
        self.assertEqual(f.perceiver.reads, 2)
        self.assertEqual(len(f.cdp.calls), 3)

    def test_consumed_focus_cannot_dispatch_again(self):
        f, _ = self.control()
        sent = len(f.cdp.calls)
        f.admit(f.observation)
        with self.assertRaises(self.api.gate.GateRefused):
            f.run()
        self.assertEqual(len(f.cdp.calls), sent)

    def test_sibling_target_binding_refuses(self):
        self.control()
        f = Fixture(self.api)
        sibling = self.api.sr.Handle("cdp_tab", "target-b", f.handle.generation)
        self.refused(f, "TARGET_MISMATCH", target=replace(f.target, handle=sibling))

    def test_replaced_generation_refuses(self):
        self.control()
        f = Fixture(self.api)
        stale = replace(f.handle, generation=f.handle.generation + 1)
        self.refused(f, "TARGET_MISMATCH", target=replace(f.target, handle=stale))

    def test_stale_selection_epoch_refuses(self):
        self.control()
        f = Fixture(self.api)
        self.refused(f, "STALE_FRAME",
                     target=replace(f.target, epoch=f.epoch - 1))

    def test_stale_selection_frame_refuses(self):
        self.control()
        f = Fixture(self.api)
        self.refused(f, "STALE_FRAME", target=replace(f.target, frame_seq=f.seq - 1))

    def test_digest_binding_refuses(self):
        self.control()
        f = Fixture(self.api)
        self.refused(f, "DIGEST_MISMATCH", target=replace(f.target, digest="0" * 64))

    def test_rgb_digest_is_recomputed_despite_a_claimed_issued_digest(self):
        self.control()
        f = Fixture(self.api)
        # Simulate corrupted producer memory, retaining every claimed metadata
        # field. Authority issuance alone must not suppress the gate's rehash.
        object.__setattr__(f.observation.frame, "rgb", np.ones(
            (f.height, f.width, 3), dtype=np.uint8))
        self.refused(f, "DIGEST_MISMATCH")

    def test_closed_target_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.registry.record = replace(f.record, closed=True)
        self.refused(f, "TARGET_MISMATCH")

    def test_wrong_cdp_attachment_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.registry.record = replace(f.record, session_id="session-b")
        self.refused(f, "TARGET_MISMATCH")

    def test_unbound_cdp_attachment_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.registry.record = replace(f.record, session_id=None, attachment_owned=False)
        self.refused(f, "TARGET_MISMATCH")

    def test_foreign_transport_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.registry.record = replace(f.record, transport=FakeTransport())
        self.refused(f, "TARGET_MISMATCH")

    def test_duplicate_target_id_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.state["nodes"].append(deepcopy(f.node))
        f.refresh_pre()
        self.refused(f, "AMBIGUOUS_TARGET")

    def test_distinct_ids_with_same_fingerprint_refuse(self):
        control = Fixture(self.api)
        other = deepcopy(control.node)
        other.update(id="separate-detector-id", backend_id=12, name="another")
        control.state["nodes"].append(other)
        control.refresh_pre()
        self.control(control)
        f = Fixture(self.api)
        duplicate = deepcopy(f.node)
        duplicate["id"] = "separate-detector-id"
        duplicate["backend_id"] = 12
        f.state["nodes"].append(duplicate)
        f.refresh_pre()
        self.refused(f, "AMBIGUOUS_TARGET")

    def test_absent_target_refuses(self):
        self.control()
        f = Fixture(self.api)
        other = deepcopy(f.node)
        other.update(id="different-target", name="another", backend_id=12)
        f.state["nodes"] = [other]
        f.refresh_pre()
        self.refused(f, "TARGET_ABSENT")

    def test_empty_node_evidence_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.state["nodes"] = []
        f.refresh_pre()
        self.refused(f, "EVIDENCE_ABSENT")

    def test_two_visual_nodes_cannot_claim_one_backend(self):
        self.control()
        f = Fixture(self.api)
        other = deepcopy(f.node)
        other.update(id="different-target", name="another")
        f.state["nodes"].append(other)
        f.refresh_pre()
        self.refused(f, "AMBIGUOUS_TARGET")

    def test_missing_backend_evidence_is_not_fabricated(self):
        self.control()
        f = Fixture(self.api)
        del f.state["nodes"][0]["backend_id"]
        f.refresh_pre()
        self.refused(f, "EVIDENCE_ABSENT")

    def test_nonvision_source_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.state["source"]["kind"] = "cdp"
        f.refresh_pre()
        self.refused(f, "EVIDENCE_ABSENT")

    def test_entire_box_must_fit_observed_viewport_even_when_center_fits(self):
        self.control()
        f = Fixture(self.api)
        f.state["nodes"][0]["box"] = [f.width - 3, 2, 4, 4]
        f.refresh_pre()
        self.refused(f, "EVIDENCE_ABSENT")

    def test_viewport_must_match_actual_frame_dimensions(self):
        self.control()
        f = Fixture(self.api)
        f.state["viewport"]["w"] += 1
        f.refresh_pre()
        self.refused(f, "EVIDENCE_ABSENT")

    def test_dpr_must_map_css_viewport_to_actual_frame_dimensions(self):
        self.control()
        f = Fixture(self.api)
        f.observation = replace(f.observation, dpr_ppm=2000000)
        f.admit(f.observation)
        self.refused(f, "EVIDENCE_ABSENT")

    def test_unverified_scroll_transform_refuses(self):
        for axis in ("scroll_x", "scroll_y"):
            with self.subTest(axis=axis):
                self.control()
                f = Fixture(self.api)
                f.state["viewport"][axis] = 1
                f.refresh_pre()
                self.refused(f, "EVIDENCE_ABSENT")

    def test_valid_dpr_two_uses_css_coordinates_with_exact_pixel_mapping(self):
        f, _ = self.control(Fixture(self.api, width=64, height=48, dpr=2000000))
        self.assertEqual(f.observation.frame.width, 64)
        self.assertEqual(f.observation.frame.height, 48)
        self.assertEqual(f.state["viewport"]["w"], 32)
        self.assertEqual(f.state["viewport"]["h"], 24)
        self.assertTrue(all((params["x"], params["y"]) == (13, 11)
                            for _, params, _ in f.cdp.calls))

    def test_closed_payload_rejects_each_extra_override(self):
        for key, value in (("box", [0, 0, 1, 1]), ("viewport", [1, 1]),
                           ("allow_coordinates", True), ("actor_lease", "forged"),
                           ("handle", "sibling"), ("flow_enabled", True)):
            with self.subTest(key=key):
                self.control()
                f = Fixture(self.api)
                f.action[key] = value
                self.refused(f, "INVALID_REQUEST")

    def test_unsupported_route_or_operation_refuses(self):
        for key, value in (("path", "ax"), ("op", "type"), ("irreversible", True)):
            with self.subTest(key=key, value=value):
                self.control()
                f = Fixture(self.api)
                f.action[key] = value
                self.refused(f, "INVALID_REQUEST")

    def test_expectation_must_be_explicit_and_nonvacuous(self):
        for expect in (None, {}, {"target_states": {}}, {"unknown": "true"}):
            with self.subTest(expect=expect):
                self.control()
                f = Fixture(self.api)
                f.action["expect"] = expect
                self.refused(f, "INVALID_REQUEST")

    def test_already_satisfied_effect_refuses(self):
        self.control()
        f = Fixture(self.api)
        f.action["expect"] = {"target_states": {"checked": "false"}}
        self.refused(f, "POSTCONDITION_FAILED")

    def test_missing_observation_reader_blocks_dispatch(self):
        self.control()
        self.refused(Fixture(self.api), "EVIDENCE_ABSENT",
                     read_observation=None)

    def test_unreadable_pre_observation_blocks_dispatch(self):
        self.control()
        f = Fixture(self.api)
        def unavailable():
            raise RuntimeError("deterministic pre-read failure")
        f.perceiver.before = unavailable
        self.refused(f, "EVIDENCE_ABSENT")

    def test_state_mutated_after_issuance_is_not_accepted(self):
        self.control()
        f = Fixture(self.api)
        f.observation.state["nodes"][0]["box"] = [0, 0, 1, 1]
        self.refused(f, "AUTHORITY_REFUSED")

    def test_new_frame_during_pre_read_invalidates_selected_frame(self):
        self.control()
        f = Fixture(self.api)
        f.perceiver.before = lambda: f.admit(f.make_observation(state=f.state))
        self.refused(f, "STALE_FRAME")

    def test_acknowledgement_with_unchanged_effect_fails_and_quarantines(self):
        self.control()
        f = Fixture(self.api)
        f.perceiver.after = lambda: f.post_observation(state=f.state)
        self.refused(f, "POSTCONDITION_FAILED", dispatched=True)
        self.assertEqual(len(f.cdp.calls), 3)

    def test_same_rgb_cannot_prove_a_new_visible_effect(self):
        self.control()
        f = Fixture(self.api)
        state = deepcopy(f.state)
        state["nodes"][0]["attrs"] = [["checked", "true"]]
        f.perceiver.after = lambda: f.admit(f.make_observation(state=state, pixel=0))
        self.refused(f, "POSTCONDITION_FAILED", dispatched=True)

    def test_name_derived_post_id_uses_a_unique_observed_backend(self):
        f = Fixture(self.api)
        f.action["expect"] = {"target_name": "confirmed"}
        state = deepcopy(f.state)
        state["nodes"][0]["name"] = "confirmed"
        state["nodes"][0]["id"] = self.api.ax.stable_id(state["nodes"][0])
        self.assertNotEqual(state["nodes"][0]["id"], f.target.node_id)
        f.perceiver.after = lambda: f.post_observation(state=state)
        self.control(f)

    def test_name_change_without_expected_name_is_not_success(self):
        self.control()
        f = Fixture(self.api)
        f.action["expect"] = {"target_name": "confirmed"}
        f.perceiver.after = lambda: f.post_observation(state=f.state)
        self.refused(f, "POSTCONDITION_FAILED", dispatched=True)

    def test_unrelated_node_change_does_not_prove_target_effect(self):
        self.control()
        f = Fixture(self.api)
        state = deepcopy(f.state)
        other = deepcopy(f.node)
        other.update(id="other-node", name="elsewhere", backend_id=12)
        other["attrs"] = [["checked", "true"]]
        state["nodes"].append(other)
        f.perceiver.after = lambda: f.post_observation(state=state)
        self.refused(f, "POSTCONDITION_FAILED", dispatched=True)

    def test_replayed_pre_observation_is_not_post_evidence(self):
        self.control()
        f = Fixture(self.api)
        f.perceiver.after = lambda: f.observation
        self.refused(f, "STALE_FRAME", dispatched=True)

    def test_current_post_observation_must_advance_sequence(self):
        self.control()
        f = Fixture(self.api)
        f.perceiver.after = lambda: f.post_observation(seq=f.seq)
        self.refused(f, "STALE_FRAME", dispatched=True)

    def test_unissued_fresh_post_observation_is_not_authority(self):
        self.control()
        f = Fixture(self.api)
        f.perceiver.after = lambda: f.post_observation(admit=False)
        self.refused(f, "AUTHORITY_REFUSED", dispatched=True)

    def test_foreign_post_target_fails(self):
        self.control()
        f = Fixture(self.api)
        sibling = self.api.sr.Handle("cdp_tab", "target-b", 1)
        f.perceiver.after = lambda: f.post_observation(handle=sibling)
        self.refused(f, "TARGET_MISMATCH", dispatched=True)

    def test_post_epoch_change_fails(self):
        self.control()
        f = Fixture(self.api)
        f.perceiver.after = lambda: f.post_observation(epoch=f.epoch + 1)
        self.refused(f, "STALE_FRAME", dispatched=True)

    def test_post_dpr_change_fails(self):
        self.control()
        f = Fixture(self.api)
        f.perceiver.after = lambda: f.post_observation(dpr=2000000)
        self.refused(f, "STALE_FRAME", dispatched=True)

    def test_post_viewport_change_fails(self):
        self.control()
        f = Fixture(self.api)
        state = deepcopy(f.state)
        state["nodes"][0]["attrs"] = [["checked", "true"]]
        state["viewport"]["w"] += 1
        f.perceiver.after = lambda: f.post_observation(state=state)
        self.refused(f, "STALE_FRAME", dispatched=True)

    def test_post_digest_is_recomputed(self):
        self.control()
        f = Fixture(self.api)
        def corrupt():
            obs = f.post_observation()
            object.__setattr__(obs.frame, "rgb", np.zeros(
                (f.height, f.width, 3), dtype=np.uint8))
            return obs
        f.perceiver.after = corrupt
        self.refused(f, "DIGEST_MISMATCH", dispatched=True)

    def test_post_target_cannot_move_to_another_backend(self):
        self.control()
        f = Fixture(self.api)
        state = deepcopy(f.state)
        state["nodes"][0]["attrs"] = [["checked", "true"]]
        state["nodes"][0]["backend_id"] += 1
        f.perceiver.after = lambda: f.post_observation(state=state)
        self.refused(f, "TARGET_MISMATCH", dispatched=True)

    def test_ambiguous_post_target_fails(self):
        self.control()
        f = Fixture(self.api)
        state = deepcopy(f.state)
        state["nodes"][0]["attrs"] = [["checked", "true"]]
        state["nodes"].append(deepcopy(state["nodes"][0]))
        f.perceiver.after = lambda: f.post_observation(state=state)
        self.refused(f, "AMBIGUOUS_TARGET", dispatched=True)

    def test_absent_post_target_fails(self):
        self.control()
        f = Fixture(self.api)
        state = deepcopy(f.state)
        other = deepcopy(f.node)
        other.update(id="different-target", name="another", backend_id=12)
        state["nodes"] = [other]
        f.perceiver.after = lambda: f.post_observation(state=state)
        self.refused(f, "TARGET_ABSENT", dispatched=True)

    def test_empty_post_evidence_fails(self):
        self.control()
        f = Fixture(self.api)
        state = deepcopy(f.state)
        state["nodes"] = []
        f.perceiver.after = lambda: f.post_observation(state=state)
        self.refused(f, "EVIDENCE_ABSENT", dispatched=True)

    def test_unreadable_post_observation_fails(self):
        self.control()
        f = Fixture(self.api)
        def unavailable():
            raise RuntimeError("deterministic post-read failure")
        f.perceiver.after = unavailable
        self.refused(f, "EVIDENCE_ABSENT", dispatched=True)

    def test_partial_transport_failure_is_not_retried(self):
        self.control()
        f = Fixture(self.api)
        f.cdp.fail_at = 2
        self.refused(f, "TRANSPORT_FAILED", dispatched=True)
        self.assertEqual(len(f.cdp.calls), 2)

    def test_receipt_contains_metadata_only(self):
        _, result = self.control()
        receipt = asdict(result)
        self.assertEqual(set(receipt), {
            "ok", "verified", "attempts", "path", "handle", "epoch", "target_id",
            "pre_frame_seq", "post_frame_seq", "pre_digest", "post_digest",
        })
        def no_media(value):
            self.assertNotIsInstance(value, (bytes, bytearray, np.ndarray))
            if isinstance(value, dict):
                for nested in value.values():
                    no_media(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    no_media(nested)
        no_media(receipt)

    def test_seeded_controls_and_single_condition_binding_counterexamples(self):
        rng = random.Random(PROPERTY_SEED)
        for index in range(PROPERTY_CASES):
            width, height = rng.randint(24, 60), rng.randint(24, 60)
            box = [rng.randint(0, width - 8), rng.randint(0, height - 8), 6, 6]
            data = {
                "seq": rng.randint(1, 100000), "epoch": rng.randint(0, 9),
                "generation": rng.randint(1, 10), "width": width, "height": height,
                "box": box, "name": "target-%d" % index,
            }
            # Minimal metadata-only counterexample: one selected frame decrement.
            case = {"seed": PROPERTY_SEED, "index": index, **data, "delta_seq": -1}
            with self.subTest(minimal_counterexample=case):
                self.control(Fixture(self.api, **data))
                f = Fixture(self.api, **data)
                self.refused(f, "STALE_FRAME", target=replace(f.target, frame_seq=f.seq - 1))


if __name__ == "__main__":
    unittest.main()
