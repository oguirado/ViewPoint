"""Offline G6 checks backed by the G4 managed custody fixture."""
from dataclasses import replace
import importlib.util
import inspect
from pathlib import Path
import sys
from textwrap import dedent
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
DIRECT = HERE.parents[2]
sys.path.insert(0, str(DIRECT))
from guest import demand
from perception.demand import DemandError


spec = importlib.util.spec_from_file_location(
    "g4_fixture", DIRECT / "guest/custody/tests/test_custody.py",
)
g4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g4)
g4.HERE = HERE  # Keep the fixture's temporary writes inside the G6 surface.


def mutated(method, guard, replacement):
    source = dedent(inspect.getsource(method))
    if source.count(guard) != 1:
        raise AssertionError("mutation must replace exactly one production guard")
    namespace = dict(vars(demand))
    exec(compile(source.replace(guard, replacement), "<G6 mutant>", "exec"), namespace)
    return patch.object(demand.GuestDemand, method.__name__, namespace[method.__name__])


class GuestDemandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = g4.CustodyTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.record = self.fixture.guest
        self.reservation = self.fixture.reserve()
        self.marker = self.fixture.s.marker.read_bytes()
        self.now = 1_000_000_000
        self.states = demand.ParticipantStates("running", "attached", "visible")
        self.agent = self.demand_record("agent", "VERIFIED", "observation")
        self.recording = self.demand_record("recorder", "VERIFIED", "recording")
        self.display = self.demand_record("presenter", "DISPLAY", "presentation")
        self.privileged = True
        self.stops = 0
        self.controller = self.guest_demand(self.record)

    def demand_record(self, consumer, interest, purpose):
        return dict(schema="vp.demand/v1", demand_id=f"demand-{consumer}",
                    surface_id=self.record["surface_id"], consumer=consumer,
                    interest=interest, purpose=purpose, lease=f"lease-{consumer}", credits=1)

    def guest_demand(self, record):
        return demand.GuestDemand(
            record, custody=self.fixture.book, reservation_id=self.reservation.reservation_id,
            interests=("VERIFIED", "DISPLAY"), authorize=lambda row: row in (
                self.agent, self.recording, self.display,
            ), agent_consumer="agent", authorize_agent=lambda: self.privileged,
            stop_producer=self.stop, read_states=lambda: self.states, clock=lambda: self.now,
        )

    def stop(self):
        self.stops += 1
        return True

    def acks(self, ticket):
        return [demand.ParticipantAck(ticket, name) for name in ("guest", "devices", "surface")]

    def acknowledge(self):
        ticket = self.controller.begin_drain()
        return ticket, self.controller.acknowledge(ticket, self.acks(ticket))

    def test_guest_device_surface_states_stay_independent_and_custody_stays_held(self):
        controller = self.controller
        self.assertEqual(controller.status(),
                         dict(guest="running", devices="attached", surface="visible", agent="ACTIVE"))
        self.states = replace(self.states, surface="hidden")
        ticket = controller.begin_drain()
        self.assertEqual(controller.status(),
                         dict(guest="running", devices="attached", surface="hidden", agent="DRAINING"))
        self.states = replace(self.states, guest="paused")
        controller.acknowledge(ticket, self.acks(ticket))
        self.assertEqual(controller.status(),
                         dict(guest="paused", devices="attached", surface="hidden", agent="DRAINED"))
        self.states = replace(self.states, devices="detached")
        self.assertEqual(controller.status(),
                         dict(guest="paused", devices="detached", surface="hidden", agent="DRAINED"))
        self.assertEqual(self.fixture.s.marker.read_bytes(), self.marker)
        self.assertEqual(self.fixture.book.status(self.reservation.reservation_id, self.record),
                         self.reservation)
        self.assertTrue(self.reservation.held)

    def test_p2_preserves_recording_display_and_borrows_until_last_delivery(self):
        controller = self.controller
        with controller.register(self.recording) as recorder:
            with controller.register(self.display) as display:
                with controller.register(self.agent):
                    ticket = controller.begin_drain()
                    with self.assertRaisesRegex(DemandError, "agent work unsettled"):
                        controller.acknowledge(ticket, self.acks(ticket))
                controller.acknowledge(ticket, self.acks(ticket))
                controller.publish(recorder, 1, "guest-frame")
                frame, = controller.claim("VERIFIED", budget=1, timeout=0)
                self.assertEqual(frame.reference, "guest-frame")
                controller.complete(frame)
                controller.publish(display, 1, "guest-presentation")
                presentation, = controller.claim("DISPLAY", budget=1, timeout=0)
                self.assertEqual(presentation.reference, "guest-presentation")
            with controller.borrow(presentation, 64):
                with self.assertRaisesRegex(DemandError, "borrowed"):
                    controller.complete(presentation)
                self.assertEqual(controller.snapshot()["borrowed_bytes"], 64)
                self.assertEqual(self.stops, 0)
        self.assertEqual(self.stops, 0)
        controller.complete(presentation)
        controller.release()
        self.assertEqual(self.stops, 1)
        self.assertEqual(controller.status()["guest"], "running")

    def test_pending_agent_borrow_and_inflight_request_hold_ack(self):
        controller = self.controller
        with controller.agent_request():
            with controller.register(self.agent) as agent:
                controller.publish(agent, 1, "agent-frame")
                work, = controller.claim("VERIFIED", budget=1, timeout=0)
                ticket = controller.begin_drain()
                with self.assertRaisesRegex(DemandError, "NO_LEASE"):
                    controller.publish(agent, 2, "new-agent-frame")
            with controller.borrow(work, 64):
                with self.assertRaisesRegex(DemandError, "agent work unsettled"):
                    controller.acknowledge(ticket, self.acks(ticket))
            controller.complete(work)
            with self.assertRaisesRegex(DemandError, "agent work unsettled"):
                controller.acknowledge(ticket, self.acks(ticket))
        self.assertEqual(controller.acknowledge(ticket, self.acks(ticket)), self.now)
        self.assertEqual(self.stops, 1)

    def test_ack_requires_complete_unique_current_participants(self):
        controller = self.controller
        ticket = controller.begin_drain()
        guest, devices, surface = self.acks(ticket)
        replaced_ticket = replace(ticket)
        for acks in (None, [], [guest, surface], [guest, devices, guest],
                     [guest, devices, replace(surface, ticket=replaced_ticket)]):
            with self.subTest(acks=acks):
                with self.assertRaisesRegex(DemandError, "participant drain unacknowledged"):
                    controller.acknowledge(ticket, acks)
                self.assertEqual(controller.status()["agent"], "DRAINING")
        self.assertEqual(controller.acknowledge(ticket, [surface, guest, devices]), self.now)
        self.now += 1
        self.assertIs(controller.begin_drain(), ticket)
        self.assertLess(controller.acknowledge(ticket, self.acks(ticket)), self.now)

    def test_hide_vcpu_pause_and_ring_capture_refuse_drain(self):
        controller = self.controller
        ticket = controller.begin_drain()
        guest, devices, surface = self.acks(ticket)
        for effect, state, peer_acks in (
            ("hide", replace(self.states, surface="hidden"), [guest, devices]),
            ("vCPU pause", replace(self.states, guest="paused"), [devices, surface]),
        ):
            with self.subTest(effect=effect):
                self.states = state
                with self.assertRaisesRegex(DemandError, "participant drain unacknowledged"):
                    controller.acknowledge(ticket, peer_acks)
                self.assertEqual(controller.status()["agent"], "DRAINING")
        with controller.register(self.recording) as recorder:
            # A synthetic RingSource result still travels through real P2 delivery.
            controller.publish(recorder, 1, "RingSource.Capture:fixture-frame")
            observation, = controller.claim("VERIFIED", budget=1, timeout=0)
            controller.complete(observation)
            self.assertEqual(observation.reference, "RingSource.Capture:fixture-frame")
            with self.assertRaisesRegex(DemandError, "participant drain unacknowledged"):
                controller.acknowledge(ticket, [guest, devices, observation])
            self.assertEqual(controller.status()["agent"], "DRAINING")

    def test_full_60_seconds_of_observed_zero_agent_requests(self):
        ticket, ack_ns = self.acknowledge()
        self.now += 60_000_000_000
        evidence = demand.RestEvidence(ticket, ack_ns, self.now, 0)
        self.assertIs(self.controller.verify_rest(evidence), evidence)

    def test_missing_short_future_shifted_or_nonzero_activity_refuses(self):
        controller = self.controller
        ticket, ack_ns = self.acknowledge()
        self.now += 60_000_000_000
        evidence = demand.RestEvidence(ticket, ack_ns, self.now, 0)
        with self.assertRaisesRegex(DemandError, "evidence unavailable"):
            controller.verify_rest(None)
        for candidate in (replace(evidence, through_mono_ns=self.now - 1),
                          replace(evidence, through_mono_ns=self.now + 1),
                          replace(evidence, from_mono_ns=ack_ns + 1)):
            with self.assertRaisesRegex(DemandError, "insufficient post-ack coverage"):
                controller.verify_rest(candidate)
        for count in (None, False, 1):
            with self.subTest(count=count):
                with self.assertRaisesRegex(DemandError, "absence unproven"):
                    controller.verify_rest(replace(evidence, agent_requests=count))

    def test_rest_requires_current_ack_and_explicit_resume_authority(self):
        controller = self.controller
        ticket = controller.begin_drain()
        sample = demand.RestEvidence(ticket, self.now, self.now + 60_000_000_000, 0)
        with self.assertRaisesRegex(DemandError, "drain unacknowledged"):
            controller.verify_rest(sample)
        with self.assertRaisesRegex(DemandError, "NO_LEASE"):
            controller.resume_admission(ticket)
        controller.acknowledge(ticket, self.acks(ticket))
        self.privileged = False
        with self.assertRaisesRegex(DemandError, "NO_LEASE"):
            controller.resume_admission(ticket)
        self.privileged = True
        controller.resume_admission(ticket)
        with controller.agent_request():
            self.assertEqual(controller.status()["agent"], "ACTIVE")
        new_ticket = controller.begin_drain()
        self.assertIsNot(ticket, new_ticket)
        with self.assertRaisesRegex(DemandError, "stale or foreign"):
            controller.verify_rest(sample)

    def test_custody_rejects_foreign_lifetimes_and_settled_reservation(self):
        for binding in (dict(surface_id="foreign-surface"), dict(workload_id="foreign-work"),
                        dict(authority_epoch=2), dict(incarnation="retired")):
            with self.subTest(binding=binding):
                with self.assertRaisesRegex(DemandError, "OWNER"):
                    self.guest_demand(dict(self.record, **binding))
        ticket, _ = self.acknowledge()
        self.fixture.book.settle(self.reservation.reservation_id, self.record, self.fixture.archive())
        with self.assertRaisesRegex(DemandError, "OWNER"):
            self.controller.acknowledge(ticket, self.acks(ticket))

    def assert_admission_closed(self):
        with self.assertRaisesRegex(DemandError, "NO_LEASE"):
            with self.controller.agent_request():
                pass
        with self.assertRaisesRegex(DemandError, "NO_LEASE"):
            with self.controller.register(self.agent):
                pass

    def test_drain_fences_new_agent_admission(self):
        ticket = self.controller.begin_drain()
        self.assert_admission_closed()
        self.controller.acknowledge(ticket, self.acks(ticket))
        self.assert_admission_closed()

    def test_mutant_ignoring_drain_fence_fails_admission_oracle(self):
        self.controller.begin_drain()
        self.assert_admission_closed()
        with mutated(demand.GuestDemand._require_agent,
                     'self._agent_state != "ACTIVE" or self._authorize_agent() is not True',
                     'self._authorize_agent() is not True'):
            with self.assertRaisesRegex(AssertionError, "DemandError not raised"):
                self.assert_admission_closed()

    def test_mutant_accepting_partial_acks_fails_peer_oracle(self):
        controller = self.controller
        ticket = controller.begin_drain()
        partial = self.acks(ticket)[:-1]

        def refuse_partial():
            with self.assertRaisesRegex(DemandError, "participant drain unacknowledged"):
                controller.acknowledge(ticket, partial)

        refuse_partial()
        with mutated(demand.GuestDemand.acknowledge,
                     "or len(acks) != len(PARTICIPANTS)\n"
                     "                or {ack.participant for ack in acks} != PARTICIPANTS",
                     "or not acks"):
            with self.assertRaisesRegex(AssertionError, "DemandError not raised"):
                refuse_partial()

    def test_mutants_accepting_elapsed_time_or_requests_fail_coverage_oracle(self):
        controller = self.controller
        ticket, ack_ns = self.acknowledge()
        self.now += 60_000_000_000
        cases = (
            (demand.RestEvidence(ticket, ack_ns, ack_ns + 1, 0),
             "self._ack_ns + MIN_REST_NS <= evidence.through_mono_ns",
             "self._ack_ns <= evidence.through_mono_ns", "insufficient post-ack coverage"),
            (demand.RestEvidence(ticket, ack_ns, self.now, 1),
             "evidence.agent_requests != 0", "evidence.agent_requests < 0", "absence unproven"),
        )
        for evidence, guard, replacement, error in cases:
            with self.subTest(guard=guard):
                def refuse_evidence():
                    with self.assertRaisesRegex(DemandError, error):
                        controller.verify_rest(evidence)

                refuse_evidence()
                with mutated(demand.GuestDemand.verify_rest, guard, replacement):
                    with self.assertRaisesRegex(AssertionError, "DemandError not raised"):
                        refuse_evidence()
