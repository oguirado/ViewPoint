"""Deterministic offline W8U step-client gate; no external processes or displays."""
import hashlib
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from wender.act.w8u_client import SeatEvidence, StepError, W8UStepClient
from wender.act.window_scheduler import DispatchContext, WriterFence
from wender.perceive.session_registry import ActorLease, FocusLease, Handle, SessionRegistry, Ticket

NOW = 7_000_000_000
DIGEST = "a" * 64
GRANT = "b" * 64
SCHEMA = "fixture.résultat/v1"  # injected TARGET test magic, never a production choice


def action(verb="pointer", args=None, ident="a0", seq=0, **changes):
    data = dict(schema="w8u.action/v1", run_id="run-0", lease_id="lease-0",
                epoch=2, generation=1, action_id=ident, action_seq=seq,
                observed_frame_seq=0, observed_frame_sha256=DIGEST,
                deadline_ns=NOW + 2_000_000_000, verb=verb,
                args={"operation": "click", "x": 2, "y": 3, "button": 1} if args is None else args,
                grant_sha256=GRANT)
    data.update(changes)
    return data


def trace(events):
    parts = ["/".join(map(str, event)).encode("ascii") for event in events]
    return hashlib.sha256(b"fixture-trace\0" + b"|".join(parts)).hexdigest()


class Channel:
    def __init__(self, service):
        self.service, self.offset, self.reply = service, 0, b""

    def send(self, body):
        o = self.service
        o.sends += 1
        o.bodies.append(body)
        req = json.loads(body.decode("utf-8"))
        x = req["args"]
        if o.emit:
            if req["verb"] == "pointer" and x["operation"] == "click":
                o.events.extend([("down", x["x"], x["y"], x["button"]),
                                 ("up", x["x"], x["y"], x["button"])])
            elif req["verb"] == "pointer":
                o.events.append((x["operation"], x["x"], x["y"]))
            elif req["verb"] == "key" and x["operation"] == "press":
                o.events.extend([("key-down", x["key"]), ("key-up", x["key"])])
            elif req["verb"] == "key":
                o.events.append(("key-" + x["operation"], x["key"]))
            elif req["verb"] == "text":
                o.events.append(("text", len(x["text"].encode("utf-8"))))
        result = dict(schema=SCHEMA, action_id=req["action_id"],
                      action_seq=req["action_seq"], lease_id=req["lease_id"],
                      epoch=req["epoch"], status="applied", code="OK_ADMITTED",
                      emitted=True, result_known=True, applied_tick=3,
                      outcome_sha256=trace(o.events))
        result.update(o.override)
        self.reply = o.raw if o.raw is not None else json.dumps(
            result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def read(self, max_bytes):
        if self.service.ack_lost:
            raise OSError("injected step ACK absent after possible effect")
        if self.offset == len(self.reply):
            self.service.eofs += 1
            return b""
        result = self.reply[self.offset:self.offset + 1]
        self.offset += 1
        return result  # Fragment non-ASCII UTF-8 across the read boundary.

    def wait(self):
        return self.service.rc

    def close(self):
        self.service.closes += 1


class StepTransport:
    def __init__(self):
        self.opens = self.sends = self.closes = self.eofs = 0
        self.events, self.bodies, self.override = [], [], {}
        self.raw, self.rc, self.ack_lost, self.emit = None, 0, False, True

    def open(self):
        self.opens += 1
        return Channel(self)


class ClientGate(unittest.TestCase):
    def setUp(self):
        self.transport = StepTransport()
        self.frame_opens = 1  # Separate resident channel; client cannot touch it.
        self.seat = SeatEvidence("run-0", "lease-0", 2, 1, 0, DIGEST, GRANT, 10, 8, True)
        self.handle = Handle("xvfb_screen", "screen-0", 1)
        self.fence = WriterFence(clock=lambda: NOW)
        self.fence.bind(self.handle, "seat-0", epoch=2)
        self.client = W8UStepClient(channel_factory=self.transport, seat=self.seat,
                                    clock=lambda: NOW, result_schema=SCHEMA,
                                    verify_applied=lambda response, request, seat:
                                        response["outcome_sha256"] == trace(self.transport.events)
                                        and response["applied_tick"] == 3
                                        and bool(self.transport.events))

    def dispatch(self, request):
        actor = ActorLease(self.handle, "unit", 1, b"nonce", NOW + 4_000_000_000)
        focus = FocusLease(actor, "focus", 2)
        ticket = Ticket(request["action_id"], self.handle)
        permit = self.fence.authorize(ticket, "seat-0", NOW + 3_000_000_000, 2)
        return DispatchContext(ticket, actor, focus, self.fence,
                               NOW + 3_000_000_000, lambda: NOW, permit)

    def step(self, request):
        context = self.dispatch(request if type(request) is dict else action())
        try:
            return self.client.execute(request, dispatch=context)
        finally:
            self.fence.retire(context._permit)

    def assert_refused_before_open(self, request, code):
        with self.assertRaises(StepError) as caught:
            self.step(request)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.transport.opens, self.transport.sends,
                          len(self.transport.events)), (0, 0, 0))

    def assert_counts(self, opens, effects):
        self.assertEqual((self.transport.opens, self.transport.sends,
                          self.transport.closes, len(self.transport.events),
                          self.frame_opens), (opens, opens, opens, effects, 1))

    def test_balanced_pointer_click_correlated_and_idempotent(self):
        a = action()
        result = self.step(a)
        self.assertTrue(result.applied_known)
        self.assertEqual((result.status, result.code), ("applied", "OK_ADMITTED"))
        self.assertEqual(self.transport.events, [("down", 2, 3, 1), ("up", 2, 3, 1)])
        self.assertEqual(self.transport.eofs, 1)
        self.assertEqual(self.step(json.dumps(a, indent=2).encode()), result)
        self.assert_counts(1, 2)

    def test_closed_input_matrix(self):
        cases = [
            ({"verb": "unknown"}, "E_VERB"),
            ({"lease_id": "not-held"}, "E_LOCK"),
            ({"epoch": 3}, "E_EPOCH"),
            ({"generation": 9}, "E_LOCK"),
            ({"observed_frame_seq": 9}, "E_STALE_FRAME"),
            ({"observed_frame_sha256": "f" * 64}, "E_STALE_FRAME"),
            ({"grant_sha256": "f" * 64}, "E_LOCK"),
            ({"args": {"operation": "click", "x": 10, "y": 3, "button": 1}}, "E_SCHEMA"),
            ({"args": {"operation": "click", "x": True, "y": 3, "button": 1}}, "E_SCHEMA"),
            ({"args": {"operation": "scroll", "x": 0, "y": 0, "dx": 0, "dy": 0}}, "E_SCHEMA"),
            ({"args": {"operation": "scroll", "x": 0, "y": 0, "dx": 121, "dy": 1}}, "E_SCHEMA"),
            ({"verb": "text", "args": {"mode": "type", "text": "é" * 2049}}, "E_SCHEMA"),
            ({"verb": "text", "args": {"mode": "type", "text": "hi\0"}}, "E_SCHEMA"),
            ({"verb": "key", "args": {"operation": "up", "key": "Tab", "modifiers": []}}, "E_LOCK"),
            ({"verb": "key", "args": {"operation": "press", "key": "Z", "modifiers": []}}, "E_VERB"),
            ({"verb": "key", "args": {"operation": "press", "key": "Tab", "modifiers": ["Alt", "Shift"]}}, "E_SCHEMA"),
            ({"verb": "key", "args": {"operation": "press", "key": "Tab", "modifiers": ["Shift", "Shift"]}}, "E_SCHEMA"),
            ({"verb": "wait", "args": {"duration_ms": 5001}}, "E_SCHEMA"),
            ({"verb": "wait", "args": {"duration_ms": 3000}}, "E_DEADLINE"),
            ({"deadline_ns": NOW}, "E_DEADLINE"),
            ({"schema": "w8u.action/v0"}, "E_SCHEMA"),
            ({"unexpected": "closed-set"}, "E_SCHEMA")
        ]
        for change, code in cases:
            with self.subTest(change=change):
                self.assert_refused_before_open(action(**change), code)

    def test_bytes_reject_duplicates_floats_invalid_utf8_and_trailing_data(self):
        body = json.dumps(action(), separators=(",", ":")).encode()
        cases = [body[:-1] + b',"action_id":"again"}', body + b' {}',
                 body.replace(b'"x":2', b'"x":2.0'),
                 body.replace(b'"x":2', b'"x":NaN'),
                 body + b'\xff', b' ' * 65537]
        for wire in cases:
            with self.subTest(size=len(wire)):
                self.assert_refused_before_open(wire, "E_SCHEMA")

    def test_changed_replay_payload_refuses_without_new_event(self):
        self.step(action())
        with self.assertRaises(StepError) as caught:
            self.step(action(deadline_ns=NOW + 2_000_000_001))
        self.assertEqual(caught.exception.code, "E_SCHEMA")
        self.assert_counts(1, 2)

    def test_rc0_malformed_body_does_not_claim_applied(self):
        self.transport.raw = b'{"schema":'
        with self.assertRaises(StepError) as caught:
            self.step(action())
        self.assertEqual(caught.exception.code, "E_SCHEMA")
        self.assertTrue(self.client.quarantined)
        self.assertEqual(self.fence.reason(self.handle), "QUARANTINED")
        self.assert_counts(1, 2)

    def test_all_response_identity_fields_are_correlated(self):
        for values in ({"action_id": "wrong"}, {"action_seq": 2},
                       {"lease_id": "wrong"}, {"epoch": 3},
                       {"outcome_sha256": "WRONG"}, {"result_known": False},
                       {"unknown": True}):
            with self.subTest(values=values):
                self.setUp()
                self.transport.override = values
                with self.assertRaises(StepError) as caught:
                    self.step(action())
                self.assertEqual(caught.exception.code, "E_SCHEMA")
                self.assertTrue(self.client.quarantined)
                self.assert_counts(1, 2)

    def test_ack_lost_after_effect_blocks_new_id_and_replay(self):
        self.transport.ack_lost = True
        with self.assertRaises(StepError) as caught:
            self.step(action())
        self.assertEqual(caught.exception.code, "E_UNCERTAIN")
        for a in (action(), action(ident="different", seq=1)):
            with self.assertRaises(StepError) as rejected:
                self.step(a)
            self.assertEqual(rejected.exception.code, "E_UNCERTAIN")
        self.assertEqual(self.fence.reason(self.handle), "QUARANTINED")
        self.assert_counts(1, 2)

    def test_outcome_claim_requires_independent_verification(self):
        self.client.verify_applied = lambda *_: False
        with self.assertRaises(StepError) as rejected:
            self.step(action())
        self.assertEqual(rejected.exception.code, "E_UNCERTAIN")
        self.assertTrue(self.client.quarantined)
        self.assert_counts(1, 2)

    def test_admitted_effect_not_applied_known(self):
        self.transport.override = dict(status="admitted", code="OK_ADMITTED",
                                       emitted=True, result_known=False,
                                       applied_tick=None, outcome_sha256=None)
        with self.assertRaises(StepError) as rejected:
            self.step(action())
        self.assertEqual(rejected.exception.code, "E_UNCERTAIN")
        self.assert_counts(1, 2)

    def test_refusal_preserves_known_zero_effect(self):
        self.transport.emit = False
        self.transport.rc = 2
        self.transport.override = dict(status="refused", code="E_SCHEMA",
                                       emitted=False, result_known=True,
                                       applied_tick=None, outcome_sha256=None)
        result = self.step(action())
        self.assertFalse(result.applied_known)
        self.assertFalse(self.client.quarantined)
        self.assertEqual(self.transport.eofs, 1)
        self.assert_counts(1, 0)

    def test_host_stop_refuses_without_open(self):
        self.fence.stop(self.handle)
        with self.assertRaises(StepError) as rejected:
            self.step(action())
        self.assertEqual(rejected.exception.code, "E_LOCK")
        self.assert_counts(0, 0)

    def test_host_dispatch_identity_mismatch_fails_before_channel_open(self):
        request = action()
        for changed in (
            {"ticket": Ticket("another-id", self.handle)},
            {"actor_lease": ActorLease(Handle("xvfb_screen", "other", 1),
                                       "unit", 1, b"nonce", NOW + 4_000_000_000)},
            {"actor_lease": ActorLease(self.handle, "unit", 1, b"nonce", NOW)},
        ):
            with self.subTest(changed=tuple(changed)):
                context = replace(self.dispatch(request), **changed)
                with self.assertRaises(StepError) as rejected:
                    self.client.execute(request, dispatch=context)
                self.assertEqual(rejected.exception.code, "E_LOCK")
                self.fence.retire(context._permit)
                self.assert_counts(0, 0)

    def test_real_registry_epoch_and_stale_handle(self):
        registry = SessionRegistry(clock=lambda: NOW)
        h = registry.register("xvfb_screen", "test-target", object(), owned=False)
        self.assertEqual(registry.resolve(h).epoch, 0)
        self.assertEqual(registry.advance_epoch(h), 1)
        registry.stop(h)
        with self.assertRaises(ValueError):
            registry.resolve(h)

    def test_focus_is_required_for_text(self):
        self.client.seat = SeatEvidence("run-0", "lease-0", 2, 1, 0, DIGEST, GRANT, 10, 8, False)
        self.assert_refused_before_open(action(verb="text", args={"mode": "type", "text": "ok"}), "E_FOCUS")

    def test_pointer_key_text_variants(self):
        for verb, args, count in (
            ("pointer", {"operation": "move", "x": 9, "y": 7}, 1),
            ("pointer", {"operation": "scroll", "x": 0, "y": 0, "dx": -120, "dy": 120}, 1),
            ("key", {"operation": "press", "key": "Enter", "modifiers": ["Shift", "Alt"]}, 2),
            ("text", {"mode": "type", "text": "olé"}, 1)):
            with self.subTest(verb=verb):
                self.setUp()
                self.assertTrue(self.step(action(verb=verb, args=args)).applied_known)
                self.assert_counts(1, count)

    def test_key_down_and_up_are_correlated_across_two_steps(self):
        down = action("key", {"operation": "down", "key": "Tab", "modifiers": []})
        up = action("key", {"operation": "up", "key": "Tab", "modifiers": []}, ident="a1", seq=1)
        self.assertTrue(self.step(down).applied_known)
        self.assertTrue(self.step(up).applied_known)
        self.assertEqual(self.transport.events, [("key-down", "Tab"), ("key-up", "Tab")])
        self.assert_counts(2, 2)

    def test_rc6_uncertain_response_remains_quarantined(self):
        self.transport.rc = 6
        self.transport.override = dict(status="uncertain", code="E_UNCERTAIN",
                                       emitted=True, result_known=False,
                                       applied_tick=None, outcome_sha256=None)
        with self.assertRaises(StepError) as rejected:
            self.step(action())
        self.assertEqual(rejected.exception.code, "E_UNCERTAIN")
        self.assertTrue(self.client.quarantined)
        self.assert_counts(1, 2)

    def test_exact_eof_and_utf8_fragmented_response(self):
        self.assertTrue(self.step(action()).applied_known)
        self.assertEqual(self.transport.eofs, 1)
        self.assertEqual(self.transport.closes, 1)
        self.assert_counts(1, 2)

    def test_scoped_disk_sink_spy(self):
        with patch("builtins.open", side_effect=AssertionError("disk writes forbidden")):
            self.assertTrue(self.step(action()).applied_known)
        self.assert_counts(1, 2)


if __name__ == "__main__":
    unittest.main()
