"""Revision-bound P1 Area-B policy gate with real W8U custody/registry owners.

TARGET input and OS calibration vectors are independently constructed in RAM.
Only the outside transport/authority proof issuers are declared test evidence;
registry, frame/AX planner, budget state machine, lease, focus and fence are real.
No X server, disk frame fixtures, network endpoint, shell or sleeping.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import importlib
import json
import secrets
import struct
import threading
import unittest

import numpy as np

# Lazy imports fail loudly if merged production modules are absent.  They are
# never skipped, expected-failed, replaced or imported from another test suite.
def _owners():
    policy = importlib.import_module("wender.act.proto1_action_policy")
    ax = importlib.import_module("wender.perceive.ax")
    session = importlib.import_module("wender.perceive.session_registry")
    locks = importlib.import_module("wender.act.target_lock_registry")
    budget = importlib.import_module("wender.act.budget_governor")
    owner = importlib.import_module("wender.act.w8u_fence")
    scheduler = importlib.import_module("wender.act.window_scheduler")
    return policy, ax, session, locks, budget, owner, scheduler


class DeclaredClock:
    def __init__(self):
        self.value = 100_000_000

    def __call__(self):
        return self.value


class EvidenceAuthority:
    """Independent externally minted proof identity; not a lease/focus validator."""
    def __init__(self, sessions, owner):
        self.sessions, self.owner = sessions, owner
        self._issued_grants, self._issued_focus = {}, {}
        self.current_focus = None

    def issue(self, handle, *, editable=True):
        grant = self.owner.DisplayGrant(handle, self.sessions.resolve(handle).epoch,
                                        "seat-A", "display-A", "domain-A", object())
        focus = self.owner.FocusEvidence(handle, grant.epoch, grant.domain, editable, object())
        self._issued_grants[id(grant)] = grant
        self._issued_focus[id(focus)] = focus
        self.grant, self.current_focus = grant, focus

    def grant_for(self, handle):
        return self.grant if self.grant.handle == handle else None

    def verify_grant(self, grant, *, registry, now_ns):
        return (registry is self.sessions and type(now_ns) is int
                and self._issued_grants.get(id(grant)) is grant
                and grant == self.grant)

    def focus_for(self, handle):
        return self.current_focus if self.current_focus is not None and self.current_focus.handle == handle else None

    def verify_focus(self, evidence, *, registry, now_ns):
        return (registry is self.sessions and type(now_ns) is int
                and self._issued_focus.get(id(evidence)) is evidence
                and self.current_focus is evidence)


class FrameWitness:
    def __init__(self, observation):
        self.handle = observation.handle
        self.epoch = observation.epoch
        self.seq = observation.frame_seq
        self.digest = hashlib.sha256(memoryview(observation.frame.rgb).tobytes()).hexdigest()


class DeclaredFrames:
    """Independent positive and negative synthetic OS vectors; no fake frame validator."""
    contract_id = "w8u-os-contract"
    revision = "a" * 40  # TARGET revision, NOT a host attestation.

    def __init__(self):
        pixels = secrets.token_bytes(12)  # ephemeral; no on-disk/reconstructible frame
        self.accepted = struct.pack(">II", 2, 2) + hashlib.sha256(pixels).digest()
        self.rejected = self.accepted[:-1] + bytes([self.accepted[-1] ^ 1])
        self._issued = {}

    def verify_vector(self, vector, *, revision):
        return revision == self.revision and vector == self.accepted

    def issue(self, observation):
        witness = FrameWitness(observation)
        self._issued[id(witness)] = witness
        return witness

    def verify(self, observation, evidence, *, revision, now_ns):
        digest = hashlib.sha256(memoryview(observation.frame.rgb).tobytes()).hexdigest()
        return (revision == self.revision and type(now_ns) is int
                and self._issued.get(id(evidence)) is evidence
                and evidence.handle == observation.handle
                and evidence.epoch == observation.epoch
                and evidence.seq == observation.frame_seq
                and evidence.digest == digest and observation.frame.digest == digest)


class RealBudgetAdapter:
    """Required W8U reserve/validate/settle facade over real BudgetGovernor."""
    def __init__(self, governor, clock):
        self.governor = governor
        self.clock = clock
        self._issued = {}

    def reserve(self, handle, cost, *, action_id, deadline_ns):
        try:
            token = self.governor.reserve(handle, cost, action_id=action_id,
                                          deadline_ns=deadline_ns)
        except self._budget_errors():
            return None
        self._issued[id(token)] = (token, handle, action_id, deadline_ns)
        return token

    @staticmethod
    def _budget_errors():
        mod = importlib.import_module("wender.act.budget_governor")
        return (mod.BudgetExhausted, mod.DeadlineInfeasible)

    def validate(self, reservation, *, handle, action_id, now_ns):
        original = self._issued.get(id(reservation))
        return (original is not None and original[0] is reservation
                and original[1] == handle and original[2] == action_id
                and type(now_ns) is int and now_ns < original[3]
                and self.governor.pending_count > 0)

    def settle(self, reservation, *, consumed):
        self.governor.settle(reservation, consumed=consumed)
        self._issued.pop(id(reservation))


class CountingTransport:
    """Only faked boundary: typed external exec, returns real Completion."""
    def __init__(self, Completion):
        self.Completion = Completion
        self.sent = []
        self.opened = 0
        self.lost_ack = False

    def __call__(self, request, decision):
        self.opened += 1
        self.sent.append((request.action_id, decision.route, decision.operation,
                          copy.deepcopy(request.payload)))
        if self.lost_ack:
            return None
        return self.Completion(request.action_id, request.handle, "SUCCEEDED", True)


class ActionPolicyGate(unittest.TestCase):
    def setUp(self):
        p, ax, session, locks, budget, owner, scheduler = _owners()
        self.p, self.ax, self.session, self.locks_mod = p, ax, session, locks
        self.clock = DeclaredClock()
        self.sessions = session.SessionRegistry(clock=self.clock)
        self.handle = self.sessions.register("cdp_tab", "tab-A", object(), owned=False)
        self.authority = EvidenceAuthority(self.sessions, owner)
        self.authority.issue(self.handle)
        self.registry = owner.AuthenticatedRegistryAdapter(
            self.sessions, self.authority, clock=self.clock)
        self.raw = locks.TargetLockRegistry(self.registry, clock=self.clock)
        self.locks = owner.SchedulerLockAdapter(self.raw)
        self.focus = owner.SchedulerFocusAdapter(
            self.registry, self.locks, self.authority, clock=self.clock)
        self.writer = scheduler.WriterFence(clock=self.clock)
        self.frame_provider = DeclaredFrames()
        self.frames = owner.RevisionPinnedFrames(
            self.frame_provider, revision=DeclaredFrames.revision,
            accepted_vector=self.frame_provider.accepted,
            rejected_vector=self.frame_provider.rejected)
        self.gov = budget.BudgetGovernor(10, 10, service_cap_ns=1,
                                         clock=self.clock, telemetry=[])
        self.budget = RealBudgetAdapter(self.gov, self.clock)
        self.fence = owner.W8UFence(registry=self.registry, locks=self.locks,
                                    focus=self.focus, writer_fence=self.writer,
                                    budget=self.budget, frames=self.frames, clock=self.clock)
        self.policy = p.ActionPolicy(fence=self.fence, clock=self.clock)
        self.actuator = CountingTransport(scheduler.Completion)
        self.node = {
            "id": "text-1", "role": "textbox", "name": "Notes", "ordinal": 0,
            "cardinality": 1, "depth": 0, "ancestors": [], "attrs": [],
            "backend_id": 17, "box": [0, 0, 2, 2],
        }
        self.state = {"nodes": [self.node], "viewport": {
            "w": 3, "h": 2, "scroll_x": 0, "scroll_y": 0}}
        self.pixels = np.frombuffer(secrets.token_bytes(18), dtype=np.uint8).reshape((2, 3, 3))
        frame = self.sessions.admit_frame(self.handle, 1, self.pixels)
        self.observation = self.sessions.admit_observation(
            self.handle, frame, state=self.state, dpr_ppm=1_000_000)
        self.frame_evidence = self.frame_provider.issue(self.observation)
        self.binding = "a" * 64
        self.grounding = self.ax.describe(self.node)
        self.grounding["id"] = self.node["id"]
        self.routes = p.RouteEvidence(
            ax_ops=frozenset(("click", "insert_text", "press")),
            coordinates_ops=frozenset(("click",)),
            modifier_mask_supported=True,
            calibration=p.PixelCalibration(1, 3, 2, 1_000_000, self.binding),
            recorded_ax=self.grounding,
        )

    def adapter(self, **changes):
        f = self.observation.frame
        val = dict(zip(self.p.ADAPTER_FIELDS, (
            "run-A", dict(zip(self.p.HANDLE_FIELDS,
                               (self.handle.kind, self.handle.canonical_id, self.handle.generation))),
            0, self.binding,
            [dict(zip(self.p.FRAME_FIELDS, (f.frame_seq, f.width, f.height,
                                            hashlib.sha256(self.pixels.tobytes()).hexdigest(), self.binding)))],
            hashlib.sha256(json.dumps(self.observation.state, sort_keys=True,
                                      separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
            "act-A", "f" * 64, None, None,
        )))
        val.update(changes)
        return val

    def body(self, verb="click", args=None, **changes):
        if args is None:
            args = {"target": "text-1", "button": 1, "irreversible": False}
        val = dict(zip(self.p.ENVELOPE_FIELDS, (
            self.p.WIRE_SCHEMA, "run-A", "lease-A", 0, self.handle.generation,
            "act-A", 0, self.observation.frame_seq, self.observation.frame.digest,
            self.clock.value + 5000000000, verb, args, "b" * 64,
        )))
        val.update(changes)
        return json.dumps(val, ensure_ascii=False, separators=(",", ":")).encode()

    def admit(self, body=None, adapter=None, *, transport=None):
        return self.policy.admit(
            self.body() if body is None else body,
            self.adapter() if adapter is None else adapter,
            observation=self.observation,
            frame_evidence=self.frame_evidence,
            transport=self.routes if transport is None else transport,
        )

    def refused(self, code, body=None, adapter=None, *, transport=None, admission=False):
        before = self.policy.snapshot()
        budget_pending = self.gov.pending_count
        focus_before = (copy.deepcopy(self.focus.policy._focus),
                        dict(self.focus.policy._grants), set(self.focus.policy._spent))
        slots_before = dict(self.locks_mod._SLOTS)
        raw_body = self.body() if body is None else body
        raw_adapter = self.adapter() if adapter is None else adapter
        adapter_before = copy.deepcopy(raw_adapter)
        with self.assertRaises(self.p.PolicyRefused) as captured:
            self.admit(raw_body, raw_adapter, transport=transport)
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(raw_adapter, adapter_before)
        self.assertEqual((self.focus.policy._focus, self.focus.policy._grants,
                          self.focus.policy._spent), focus_before)
        self.assertEqual(self.locks_mod._SLOTS, slots_before)
        self.assertEqual(self.policy.snapshot(), before)
        self.assertEqual(self.gov.pending_count, budget_pending)
        self.assertEqual(self.actuator.opened, 0)
        self.assertEqual(self.actuator.sent, [])

    def test_admits_ax_click_then_exactly_one_owned_send_and_replay(self):
        body, adapter = self.body(), self.adapter()
        ticket = self.admit(body, adapter)
        self.assertEqual(ticket.decision.route, "ax")
        self.assertEqual(ticket.decision.operation, "click")
        self.assertEqual(self.actuator.opened, 0)
        self.assertIs(ticket, self.admit(body, adapter))
        output = self.policy.dispatch(ticket, self.actuator)
        self.assertEqual(output["code"], "OK_ADMITTED")
        self.assertEqual(output["status"], "admitted")
        self.assertTrue(output["emitted"])
        self.assertFalse(output["result_known"])
        self.assertEqual(self.actuator.opened, 1)
        self.assertEqual(self.policy.dispatch(ticket, self.actuator), output)
        self.assertEqual(self.actuator.opened, 1)

    def test_admits_insert_text_and_key_press_using_actual_cdp_vocabulary(self):
        for verb, args, operation in (
                ("type", {"target": "text-1", "text": "hi \u03b1"}, "insert_text"),
                ("key", {"target": "text-1", "operation": "press",
                         "key": "Enter", "modifiers": ["Shift", "Control"]}, "press")):
            with self.subTest(verb=verb):
                # Each subtest uses a fresh real lock and independently tracked id.
                self.setUp()
                ticket = self.admit(self.body(verb, args))
                self.assertEqual(ticket.decision.operation, operation)
                if verb == "key":
                    self.assertEqual(ticket.decision.params["modifiers"], 10)
                self.policy.dispatch(ticket, self.actuator)
                self.assertEqual(self.actuator.opened, 1)

    def test_structural_unavailable_reversible_fallback_and_missing_calibration(self):
        routes = replace(self.routes, ax_ops=frozenset())
        ticket = self.admit(transport=routes)
        self.assertEqual(ticket.decision.route, "coordinates")
        self.policy.dispatch(ticket, self.actuator)
        self.assertEqual(self.actuator.sent[0][1:3], ("coordinates", "click"))
        # Separate fresh test state so original real actor budget is irrelevant.
        self.setUp()
        routes = replace(self.routes, ax_ops=frozenset(), calibration=None)
        self.refused("E_UNCERTAIN", transport=routes)

    def test_read_only_local_routes_do_not_acquire_transport_or_budget(self):
        for verb, args in (("observe", {}), ("locate", {"target": "text-1"}),
                           ("wait", {"duration_ms": 0}),
                           ("assert", {"expected_state_sha256": self.adapter()["state_sha256"]})):
            with self.subTest(verb=verb):
                self.setUp()
                ticket = self.admit(self.body(verb, args))
                self.assertEqual(ticket.decision.route, "local")
                self.assertIsNone(ticket.prepared)
                out = self.policy.dispatch(ticket, self.actuator)
                self.assertFalse(out["emitted"])
                self.assertEqual(self.actuator.opened, 0)
                self.assertEqual(self.gov.pending_count, 0)

    def test_closed_verb_and_transport_unavailability(self):
        self.refused("E_VERB", self.body(verb="launch", args={}))
        self.refused("E_VERB", self.body("drag", dict(from_x=0, from_y=0,
                                                      to_x=2, to_y=1, button=3)))
        self.refused("E_VERB", self.body("scroll", dict(x=0, y=0, delta_x=-120, delta_y=120)))
        self.refused("E_VERB", self.body("key", dict(target="text-1", operation="down",
                                                     key="Enter", modifiers=[])))
        self.refused("E_VERB", self.body("key", dict(target="text-1", operation="press",
                                                     key="Home", modifiers=[])))

    def test_invalid_shape_and_limits_do_not_open_exec(self):
        scenarios = (
            ("click", dict(x=3, y=1, button=1, irreversible=False), "E_SCHEMA"),
            ("click", dict(x=True, y=1, button=1, irreversible=False), "E_SCHEMA"),
            ("click", dict(target="text-1", button=True, irreversible=False), "E_SCHEMA"),
            ("click", dict(target="text-1", button=4, irreversible=False), "E_SCHEMA"),
            ("type", dict(target="text-1", text="abc\x00def"), "E_SCHEMA"),
            ("type", dict(target="text-1", text="\u00e9" * 2049), "E_SCHEMA"),
            ("key", dict(target="text-1", operation="press", key="Enter",
                         modifiers=["Alt", "Shift"]), "E_SCHEMA"),
            ("key", dict(target="text-1", operation="press", key="Enter",
                         modifiers=["Shift", "Shift"]), "E_SCHEMA"),
            ("wait", {"duration_ms": 5001}, "E_SCHEMA"),
            ("scroll", {"x": 0, "y": 0, "delta_x": 0, "delta_y": 0}, "E_SCHEMA"),
            ("scroll", {"x": 0, "y": 0, "delta_x": 121, "delta_y": 0}, "E_SCHEMA"),
        )
        for verb, args, code in scenarios:
            with self.subTest(verb=verb, args=repr(args)[:40]):
                self.refused(code, self.body(verb, args))

    def test_irreversible_pixel_only_and_no_ax_proof(self):
        self.refused("E_UNCERTAIN", self.body(args=dict(x=1, y=1, button=1,
                                                           irreversible=True)))
        self.refused("E_UNCERTAIN", self.body(args=dict(target="text-1", button=1,
                        irreversible=True)), transport=replace(self.routes, recorded_ax=None))

    def test_stale_frame_expired_deadline_and_missing_focus(self):
        self.refused("E_DEADLINE", self.body(deadline_ns=self.clock.value))
        self.sessions.admit_frame(self.handle, 2, self.pixels)
        self.refused("E_STALE_FRAME")
        self.setUp()
        self.authority.current_focus = None
        self.refused("E_FOCUS", self.body("type", dict(target="text-1", text="A")))

    def test_wrong_epoch_and_registered_generation(self):
        self.sessions.advance_epoch(self.handle)
        self.refused("E_EPOCH")
        self.setUp()
        adapter = self.adapter()
        adapter["target_handle"]["generation"] += 1
        self.refused("E_LOCK", adapter=adapter)

    def test_ambiguity_and_removed_target(self):
        state = copy.deepcopy(self.state)
        state["nodes"].append(copy.deepcopy(state["nodes"][0]))
        self.observation = self.sessions.admit_observation(
            self.handle, self.observation.frame, state=state, dpr_ppm=1_000_000)
        self.refused("E_UNCERTAIN", adapter=self.adapter())
        self.setUp()
        state = copy.deepcopy(self.state)
        state["nodes"].clear()
        self.observation = self.sessions.admit_observation(
            self.handle, self.observation.frame, state=state, dpr_ppm=1_000_000)
        self.refused("E_UNCERTAIN", adapter=self.adapter())

    def test_exhausted_budget_keeps_sequence_focus_and_transport_intact(self):
        budget = importlib.import_module("wender.act.budget_governor")
        owner = importlib.import_module("wender.act.w8u_fence")
        empty = budget.BudgetGovernor(0, 0, service_cap_ns=1, clock=self.clock, telemetry=[])
        self.gov = empty
        self.fence = owner.W8UFence(registry=self.registry, locks=self.locks,
                                    focus=self.focus, writer_fence=self.writer,
                                    budget=RealBudgetAdapter(empty, self.clock),
                                    frames=self.frames, clock=self.clock)
        self.policy = self.p.ActionPolicy(fence=self.fence, clock=self.clock)
        self.refused("E_BUDGET")
        self.assertEqual(empty.spent_global, 0)

    def test_changed_replay_refuses_without_new_send_or_state_mutation(self):
        ticket = self.admit()
        out = self.policy.dispatch(ticket, self.actuator)
        self.assertEqual(self.actuator.opened, 1)
        with self.assertRaises(self.p.PolicyRefused) as captured:
            self.admit(self.body(args=dict(target="text-1", button=1, irreversible=True)))
        self.assertEqual(captured.exception.code, "E_SCHEMA")
        self.assertEqual(self.actuator.opened, 1)
        self.assertEqual(self.policy.dispatch(ticket, self.actuator), out)

    def test_lost_ack_never_retries_the_same_action(self):
        ticket = self.admit()
        self.actuator.lost_ack = True
        with self.assertRaises(self.p.PolicyRefused) as received:
            self.policy.dispatch(ticket, self.actuator)
        self.assertEqual(received.exception.code, "E_UNCERTAIN")
        self.assertEqual(self.actuator.opened, 1)
        self.assertEqual(self.policy.dispatch(ticket, self.actuator)["status"], "uncertain")
        self.assertEqual(self.actuator.opened, 1)

    def test_unknown_extension_receipt_and_replay_schema(self):
        data = self.adapter()
        data["owner_receipt"] = b"claimed-owner"
        self.refused("E_UNCERTAIN", adapter=data)
        data = self.adapter()
        data["extension_version"] = 2
        self.refused("E_SCHEMA", adapter=data)
        data = self.adapter()
        data["target_handle"]["generation"] = True
        self.refused("E_SCHEMA", adapter=data)
        self.refused("E_SCHEMA", self.body(schema="w8u.action/v2"))

    def test_duplicate_keys_bad_utf8_trailing_data_floats_oversize(self):
        original = self.body()
        self.refused("E_SCHEMA", original.replace(b'"verb":', b'"verb":"click","verb":'))
        self.refused("E_SCHEMA", original + b" {}")
        self.refused("E_SCHEMA", original + b"\xff")
        self.refused("E_SCHEMA", original.replace(b'"epoch":0', b'"epoch":0.0'))
        self.refused("E_SCHEMA", original.replace(b'"epoch":0', b'"epoch":true'))
        self.refused("E_SCHEMA", b" " * (65536 + 1))

    def test_both_argument_boundaries_and_canonical_response(self):
        max_text = self.body("type", {"target": "text-1", "text": "\u00e9" * 2048})
        self.assertEqual(len(self.p.parse_envelope(max_text)["args"]["text"].encode()), 4096)
        over_text = self.body("type", {"target": "text-1", "text": "\u00e9" * 2048 + "A"})
        with self.assertRaises(self.p.PolicyRefused) as refusal:
            self.p.parse_envelope(over_text)
        self.assertEqual(refusal.exception.code, "E_SCHEMA")
        zero_text = self.body("type", {"target": "text-1", "text": ""})
        self.assertEqual(self.p.parse_envelope(zero_text)["args"]["text"], "")
        for key in ("button",):
            for bound in (1, 3):
                args = {"target": "text-1", key: bound, "irreversible": False}
                self.assertEqual(self.p.parse_envelope(self.body(args=args))["args"][key], bound)
        for bound in (0, 5000):
            self.assertEqual(self.p.parse_envelope(self.body("wait", {"duration_ms": bound}))
                             ["args"]["duration_ms"], bound)
        out = self.p.make_response(self.p.parse_envelope(self.body()), "admitted", "OK_ADMITTED")
        self.assertEqual(tuple(out), self.p.RESPONSE_FIELDS)
        self.assertTrue(self.p.encode_response(out).startswith(b'{"schema":"w8u.action/v1"'))

    def test_concurrent_duplicate_arrivals_use_single_real_custody(self):
        bar = threading.Barrier(3)
        outcomes = []
        guard = threading.Lock()
        body, adapter = self.body(), self.adapter()

        def worker():
            bar.wait()
            try:
                result = self.admit(body, adapter)
                value = ("ok", result)
            except self.p.PolicyRefused as error:
                value = (error.code, None)
            with guard:
                outcomes.append(value)

        a = threading.Thread(target=worker)
        b = threading.Thread(target=worker)
        a.start(); b.start(); bar.wait(); a.join(); b.join()
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(v[0] == "ok" for v in outcomes), 1 if any(
            v[0] == "E_LOCK" for v in outcomes) else 2)
        self.assertTrue(all(v[0] in ("ok", "E_LOCK") for v in outcomes))
        self.assertEqual(self.gov.pending_count, 1)
        admitted = next(v[1] for v in outcomes if v[0] == "ok")
        self.policy.dispatch(admitted, self.actuator)
        self.assertEqual(self.actuator.opened, 1)
        self.assertEqual(self.gov.pending_count, 0)


if __name__ == "__main__":
    unittest.main()
