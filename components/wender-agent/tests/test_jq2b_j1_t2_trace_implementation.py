"""VM-only semantic and adversarial controls for the real T2 trace recorder.

All limits and fixture numbers are authored test targets, NOT host measurements.
No display, transport process, Godot binary or live CDP connection is used.
"""

import dataclasses
import hashlib
import json
import unittest
from types import SimpleNamespace

import numpy as np

from wender.act.t2_trace import (
    AttemptDraft, CommitKey, TraceConflictError, TraceError,
    TraceRecorder, TraceUnavailableError,
)


BASE = "a" * 64
POST = "b" * 64
CMD_KEYS = {"tick", "seq", "cmd", "args"}
OUTCOME_KEYS = {"seq", "requested_tick", "applied_tick", "order_seq_id",
                "status", "reason", "state_sha256"}


class FakeClock:
    def __init__(self, initial=1000):
        self.now = initial

    def __call__(self):
        return self.now

    def advance(self, amount):
        self.now += amount


class FakeBroker:
    """Test-only broker attests outbox bytes and *separate* post-commit snapshots."""

    def __init__(self):
        self.inbox = []
        self.commits = {}
        self.proof_queries = []
        self.raw_tree = {"secret": "FAKE_RAW_TREE_MUST_REMAIN_VOLATILE"}
        self.raw_receipt = b"RAW_HOOK_RECEIPT_MUST_REMAIN_VOLATILE"
        self.checkpoint = b"OPAQUE_CHECKPOINT_MUST_REMAIN_VOLATILE"
        self.ram_frame = np.arange(3 * 4 * 3, dtype=np.uint8).reshape(3, 4, 3)
        self.procfs = "VmRSS:\t2345 kB\nVmSwap:\t0 kB\n"  # fixture only; no host claim
        self.cgroup = "65536\n"                       # authored fixture bytes

    def sink(self, payload):
        self.inbox.append(payload)

    def adapter(self, command, handle, context, observed_sink):
        # All authority remains in the trusted context, never in command.
        assert set(command) == CMD_KEYS
        assert context["ticket"] and context["session"]
        observed_sink(json.dumps(command, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return SimpleNamespace(action_id=context["action_id"], handle=handle,
                               status="admitted", reason="OK")

    def install_commit(self, attempt, outcome, auth):
        key = CommitKey(auth["source_policy"], auth["stream_ref"], auth["source_ticket"],
                        auth["session"], outcome["seq"], outcome["requested_tick"],
                        outcome["order_seq_id"])
        self.commits[key] = {
            "stream_ref": auth["stream_ref"], "source_ticket": auth["source_ticket"],
            "session": auth["session"], "seq": outcome["seq"],
            "requested_tick": outcome["requested_tick"], "applied_tick": outcome["applied_tick"],
            "order_seq_id": outcome["order_seq_id"], "state_sha256": outcome["state_sha256"],
        }

    def post_commit(self, key):
        self.proof_queries.append(key)
        return self.commits.get(key)


class FakeCDP:
    """Source-specific structural adapter; no actual CDP import or transport."""

    def __init__(self):
        self.commands = []
        self.ax_tree = {"AX": "PRIVATE_AX_DUMP"}

    def send(self, method, params, session_id):
        self.commands.append((method, params, session_id))
        return {"ok": True}

    def adapter(self, command, handle, context, observed_sink):
        self.send("Input.dispatchKeyEvent", {"key": command["cmd"]}, context["session"])
        observed_sink(json.dumps(command, sort_keys=True).encode("utf-8"))
        return {"action_id": context["action_id"], "handle": handle,
                "status": "admitted", "reason": "OK"}


class WriteSpy:
    """Simulate metadata persistence; reject unapproved volatile data."""

    def __init__(self):
        self.saved = []

    def persist(self, audit):
        raw = json.dumps(audit, sort_keys=True).encode("utf-8")
        for forbidden in (b"RAW_TREE_MUST_REMAIN_VOLATILE", b"RAW_HOOK_RECEIPT",
                          b"OPAQUE_CHECKPOINT", b"PRIVATE_AX_DUMP", b"base64,",
                          b"SECRET_PIXEL_DATA", b"VmRSS", b"VmSwap"):
            if forbidden in raw:
                raise AssertionError("unapproved raw data escaped the metadata audit")
        self.saved.append(raw)


def setup(policy="godot", *, seq=1, ticket="ticket-01", stream="stream-01", action="act-01"):
    ctx = {"principal": "p-01", "company": "holdings-01", "role": "owner",
           "mandate": "owner-mandate", "session": "session-01", "ticket": ticket,
           "stream_ref": stream, "action_id": action, "source_policy": policy,
           "basis_sha256": BASE, "next_seq": seq}
    handle = SimpleNamespace(kind="godot_window" if policy == "godot" else "cdp_tab",
                             canonical_id="target-01", generation=3)
    return ctx, handle


def recorder():
    return TraceRecorder(max_attempts=64, max_command_bytes=1024,
                         max_metadata_bytes=128, max_emitted_bytes=4096,
                         timeout_ns=200, approved_arg_keys=("target_id",))


def pause(seq=1, tick=7, args=None):
    return {"tick": tick, "seq": seq, "cmd": "pause", "args": {} if args is None else args}


def outcome(status="accepted", seq=1, tick=7, *, order=11, state=POST):
    return {"seq": seq, "requested_tick": tick, "applied_tick": tick,
            "order_seq_id": order, "status": status,
            "reason": None if status == "accepted" else "RULE_REJECTED",
            "state_sha256": state}


def auth(ctx):
    return {"source_policy": ctx["source_policy"], "stream_ref": ctx["stream_ref"],
            "source_ticket": ctx["ticket"], "session": ctx["session"]}


def local_receipt(ctx, handle, status="admitted", reason="OK"):
    return SimpleNamespace(action_id=ctx["action_id"], handle=handle,
                           status=status, reason=reason)


class TraceSemanticContract(unittest.TestCase):
    def test_full_four_phase_positive_matrix_and_audit_rejection(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()

        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        accepted = r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        self.assertEqual(accepted.phase, "PENDING")
        self.assertIsNone(accepted.engine_outcome)  # gate G J1 1 negative contrast
        self.assertEqual(r.cursor(a.attempt_id), 2)
        e = outcome()
        broker.install_commit(accepted, e, auth(ctx))
        accepted = r.record_outcome(attempt_id=a.attempt_id, outcome=e,
                                    authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit)
        self.assertEqual(accepted.phase, "ACCEPTED")
        self.assertEqual(accepted.committed_state_sha256, POST)

        ctx2, _ = setup(seq=2, action="act-02")
        b = r.begin(command=pause(seq=2), handle=handle, trusted_context=ctx2, clock=clock)
        rejected = r.dispatch(attempt_id=b.attempt_id, adapter=broker.adapter, sink=broker.sink)
        e2 = outcome("rejected", seq=2, order=12, state=BASE)
        broker.install_commit(rejected, e2, auth(ctx2))
        rejected = r.record_outcome(attempt_id=b.attempt_id, outcome=e2,
                                    authenticated_source=auth(ctx2), lookup_post_commit=broker.post_commit)
        self.assertEqual(rejected.phase, "REJECTED")
        self.assertEqual(rejected.committed_state_sha256, BASE)
        self.assertEqual(r.cursor(b.attempt_id), 3)  # engine reject burns emitted seq

        ctx3, _ = setup(seq=3, action="act-03")
        c = r.begin(command=pause(seq=3), handle=handle, trusted_context=ctx3, clock=clock)
        refused = r.admit(attempt_id=c.attempt_id,
                          receipt=local_receipt(ctx3, handle, "rejected", "SCHEMA"), emitted_bytes=None)
        self.assertEqual(refused.phase, "REFUSED")
        self.assertIsNone(refused.emitted_sha256)
        self.assertEqual(r.cursor(c.attempt_id), 3)  # local refusal never burns sequence
        cursor_after_refusal = r.cursor(c.attempt_id)

        ctx4, _ = setup(seq=3, action="act-04")
        d = r.begin(command=pause(seq=3), handle=handle, trusted_context=ctx4, clock=clock)
        pending = r.dispatch(attempt_id=d.attempt_id, adapter=broker.adapter, sink=broker.sink)
        self.assertEqual(pending.phase, "PENDING")
        self.assertEqual([x["phase"] for x in r.audit()],
                         ["ACCEPTED", "REJECTED", "REFUSED", "PENDING"])
        self.assertIn("REJECTED", [x[1] for x in r.events()])
        self.assertNotEqual([x["phase"] for x in r.audit()],
                            ["ACCEPTED"] * 4)  # mutation: falsely auto-accepted
        drop_rejection = [x for x in r.audit() if x["phase"] != "REJECTED"]
        self.assertNotEqual(drop_rejection, r.audit())  # mutation: dropped rejected row
        self.assertNotEqual(cursor_after_refusal, 4)  # mutation: local refusal advanced

    def test_both_source_policies_without_cross_labeling(self):
        for policy in ("godot", "cdp"):
            with self.subTest(policy=policy):
                r = recorder()
                ctx, handle = setup(policy=policy)
                clock = FakeClock()
                if policy == "godot":
                    broker = FakeBroker()
                    row = r.dispatch(attempt_id=r.begin(command=pause(), handle=handle,
                                  trusted_context=ctx, clock=clock).attempt_id,
                                  adapter=broker.adapter, sink=broker.sink)
                    self.assertEqual(len(broker.inbox), 1)
                else:
                    cdp, sink_payloads = FakeCDP(), []
                    row = r.dispatch(attempt_id=r.begin(command=pause(), handle=handle,
                                  trusted_context=ctx, clock=clock).attempt_id,
                                  adapter=cdp.adapter, sink=sink_payloads.append)
                    self.assertEqual(cdp.commands[0][2], ctx["session"])
                self.assertEqual(row.phase, "PENDING")
                self.assertEqual(set(r.audit()[0]["command"]), CMD_KEYS)
                self.assertEqual(set(row.command), CMD_KEYS)

    def test_proposed_confirmation_does_not_execute_economic_command(self):
        r, clock = recorder(), FakeClock()
        ctx, handle = setup()
        draft = r.begin(command={"tick": 8, "seq": 1, "cmd": "ipo", "args": {}},
                        handle=handle, trusted_context=ctx, clock=clock)
        confirmation = {"modal_id": "modal-ipo-01", "cmd": "ipo", "target_id": "entity-01",
                        "terms": {"price": 50}, "basis_sha256": BASE}
        proposed = r.admit(attempt_id=draft.attempt_id,
                           receipt=local_receipt(ctx, handle, "rejected", "NEEDS_CONFIRM"),
                           emitted_bytes=None, proposal=confirmation)
        self.assertEqual(proposed.phase, "PROPOSED")
        self.assertIsNone(proposed.engine_outcome)
        self.assertIsNone(proposed.committed_state_sha256)
        self.assertEqual(r.cursor(proposed.attempt_id), 1)
        self.assertNotIn("terms", r.proposal_metadata(proposed.attempt_id))
        self.assertEqual(r.proposal_metadata(proposed.attempt_id)["basis_sha256"], BASE)
        self.assertNotIn("modal-ipo-01", json.dumps(r.audit()))

    def test_timeout_unknown_but_not_premature(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        clock.advance(199)
        self.assertEqual(r.timeout(a.attempt_id).phase, "PENDING")
        clock.advance(1)
        self.assertEqual(r.timeout(a.attempt_id).phase, "UNKNOWN")
        self.assertIsNone(r.attempt(a.attempt_id).committed_state_sha256)
        self.assertIn((a.attempt_id, "UNKNOWN", "TIMEOUT"), r.events())

    def test_late_authenticated_engine_outcome_can_reconcile_unknown(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        clock.advance(201)
        r.timeout(a.attempt_id)
        e = outcome()
        broker.install_commit(a, e, auth(ctx))
        self.assertEqual(r.record_outcome(attempt_id=a.attempt_id, outcome=e,
                         authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit).phase,
                         "ACCEPTED")
        self.assertIn((a.attempt_id, "UNKNOWN", "TIMEOUT"), r.events())

    def test_identical_authenticated_duplicate_is_idempotent(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        e = outcome()
        broker.install_commit(a, e, auth(ctx))
        kw = dict(attempt_id=a.attempt_id, outcome=e, authenticated_source=auth(ctx),
                  lookup_post_commit=broker.post_commit)
        first = r.record_outcome(**kw)
        event_count = len(r.events())
        self.assertIs(first, r.record_outcome(**kw))
        self.assertEqual(event_count, len(r.events()))
        with self.assertRaises(TraceConflictError):
            r.record_outcome(**{**kw, "outcome": {**e, "status": "rejected"}})
        self.assertEqual(r.attempt(a.attempt_id), first)

    def test_wrong_identity_seq_tick_and_source_fail_not_arrival_order(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        e = outcome()
        broker.install_commit(a, e, auth(ctx))
        for field in ("source_ticket", "stream_ref", "session", "source_policy"):
            with self.subTest(field=field), self.assertRaises(TraceConflictError):
                r.record_outcome(attempt_id=a.attempt_id, outcome=e,
                                 authenticated_source={**auth(ctx), field: "wrong"},
                                 lookup_post_commit=broker.post_commit)
        for field, value in (("seq", 2), ("requested_tick", 8)):
            with self.subTest(field=field), self.assertRaises(TraceConflictError):
                r.record_outcome(attempt_id=a.attempt_id, outcome={**e, field: value},
                                 authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit)
        self.assertEqual(r.attempt(a.attempt_id).phase, "PENDING")

    def test_reject_preaction_hash_for_accepted_postcommit(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        e = outcome()
        broker.install_commit(a, e, auth(ctx))
        key = next(iter(broker.commits))
        broker.commits[key]["state_sha256"] = BASE  # mutation: basis substituted for post hash
        with self.assertRaises(TraceConflictError):
            r.record_outcome(attempt_id=a.attempt_id, outcome=e,
                             authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit)
        self.assertIsNone(r.attempt(a.attempt_id).engine_outcome)

    def test_rejection_requires_unchanged_independently_verified_state(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        e = outcome("rejected", state=POST)
        broker.install_commit(a, e, auth(ctx))
        with self.assertRaises(TraceConflictError):
            r.record_outcome(attempt_id=a.attempt_id, outcome=e,
                             authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit)
        self.assertEqual(r.cursor(a.attempt_id), 2)

    def test_post_commit_proof_missing_is_ausente_not_zero_or_acceptance(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        with self.assertRaisesRegex(TraceUnavailableError, "AUSENTE"):
            r.record_outcome(attempt_id=a.attempt_id, outcome=outcome(),
                             authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit)
        self.assertEqual(r.attempt(a.attempt_id).phase, "PENDING")

    def test_local_refusal_rejects_fabricated_emission(self):
        r, clock = recorder(), FakeClock()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        with self.assertRaises(TraceConflictError):
            r.admit(attempt_id=a.attempt_id, receipt=local_receipt(ctx, handle, "rejected", "SCHEMA"),
                    emitted_bytes=b"not really admitted")
        self.assertEqual(r.cursor(a.attempt_id), 1)

    def test_sink_failure_is_incomplete_halts_dispatch(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        def fails_after_call(payload):
            raise IOError("fake brokered IPC failure")
        with self.assertRaisesRegex(IOError, "brokered"):
            r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=fails_after_call)
        self.assertTrue(r.halted)
        self.assertEqual(r.attempt(a.attempt_id).local_status, "INCOMPLETE")
        self.assertEqual(r.attempt(a.attempt_id).phase, "UNKNOWN")
        self.assertIsNone(r.attempt(a.attempt_id).emitted_sha256)
        with self.assertRaises(TraceConflictError):
            r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)

    def test_sink_success_but_adapter_error_retains_observed_hash_and_burn(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        def raises_after_emission(command, handle, context, sink):
            sink(b"sent-and-then-failed")
            raise RuntimeError("fixture subsequent adapter failure")
        with self.assertRaisesRegex(RuntimeError, "subsequent"):
            r.dispatch(attempt_id=a.attempt_id, adapter=raises_after_emission, sink=broker.sink)
        self.assertTrue(r.halted)
        self.assertEqual(r.attempt(a.attempt_id).emitted_sha256,
                         hashlib.sha256(b"sent-and-then-failed").hexdigest())
        self.assertEqual(r.cursor(a.attempt_id), 2)

    def test_admitted_without_witness_is_unknown_not_accepted(self):
        r, clock = recorder(), FakeClock()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        row = r.admit(attempt_id=a.attempt_id, receipt=local_receipt(ctx, handle), emitted_bytes=None)
        self.assertEqual((row.phase, row.local_status), ("UNKNOWN", "INCOMPLETE"))
        self.assertTrue(r.halted)
        self.assertEqual(r.cursor(a.attempt_id), 1)

    def test_receipt_binding_and_no_receipt_persistence(self):
        r, clock = recorder(), FakeClock()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        with self.assertRaises(TraceConflictError):
            r.admit(attempt_id=a.attempt_id,
                    receipt=local_receipt({**ctx, "action_id": "impostor"}, handle), emitted_bytes=None)
        with self.assertRaises(TraceConflictError):
            r.admit(attempt_id=a.attempt_id,
                    receipt=local_receipt(ctx, SimpleNamespace(kind="godot_window",
                            canonical_id="not-this-target", generation=3)), emitted_bytes=None)
        self.assertIsInstance(r.attempt(a.attempt_id), AttemptDraft)

    def test_draft_and_attempt_are_immutable_snapshots(self):
        r, clock = recorder(), FakeClock()
        ctx, handle = setup()
        command = pause(args={"target_id": "persist-allowed", "opaque_note": "secret"})
        draft = r.begin(command=command, handle=handle, trusted_context=ctx, clock=clock)
        command["args"]["target_id"] = "changed"
        handle.generation = 55
        with self.assertRaises((TypeError, AttributeError)):
            draft.command["args"]["target_id"] = "changed"
        self.assertEqual(draft.command["args"]["target_id"], "persist-allowed")
        row = r.admit(attempt_id=draft.attempt_id,
                      receipt=local_receipt(ctx, SimpleNamespace(kind="godot_window",
                                canonical_id="target-01", generation=3), "rejected", "SCHEMA"),
                      emitted_bytes=None)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.phase = "ACCEPTED"
        self.assertEqual(r.audit()[0]["command"]["args"], {"target_id": "persist-allowed"})

    def test_metadata_write_spy_prohibits_raw_receipts_frames_and_tree(self):
        r, clock, broker, spy = recorder(), FakeClock(), FakeBroker(), WriteSpy()
        ctx, handle = setup()
        command = pause(args={"target_id": "company-01",
                              "unapproved_note": "SECRET_PIXEL_DATA"})
        a = r.begin(command=command, handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        spy.persist(r.audit())
        self.assertEqual(len(spy.saved), 1)
        self.assertTrue(broker.ram_frame.flags["C_CONTIGUOUS"])
        self.assertIn("VmRSS", broker.procfs)  # authored procfs/cgroup text; no host access
        self.assertTrue(broker.cgroup.strip().isdigit())
        with self.assertRaises(TraceError):
            r.begin(command=pause(seq=2, args={"pixels": broker.ram_frame}),
                    handle=handle, trusted_context={**ctx, "next_seq": 2, "action_id": "act-02"},
                    clock=clock)

    def test_no_authority_in_external_command(self):
        for bad in ({"session": "stolen"}, {"nested": {"ticket": "forged"}},
                    {"consent_token": "self-authored"}, {"frame": b"raw"}):
            r, clock = recorder(), FakeClock()
            ctx, handle = setup()
            with self.subTest(bad=bad), self.assertRaises(TraceError):
                r.begin(command=pause(args=bad), handle=handle, trusted_context=ctx, clock=clock)

    def test_sequence_cursor_requires_trusted_bookkeeping(self):
        r, clock = recorder(), FakeClock()
        ctx, handle = setup()
        first = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        with self.assertRaises(TraceConflictError):
            r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.admit(attempt_id=first.attempt_id,
                receipt=local_receipt(ctx, handle, "rejected", "SCHEMA"), emitted_bytes=None)
        again = r.begin(command=pause(), handle=handle,
                        trusted_context={**ctx, "action_id": "new-action"}, clock=clock)
        self.assertNotEqual(first.attempt_id, again.attempt_id)
        self.assertEqual(r.cursor(again.attempt_id), 1)

    def test_missing_context_and_inconsistent_handle_fail_loudly(self):
        r, clock = recorder(), FakeClock()
        ctx, handle = setup()
        for changed_ctx in ({}, {**ctx, "source_policy": "cdp"},
                            {**ctx, "next_seq": False}):
            with self.subTest(ctx=changed_ctx), self.assertRaises(TraceError):
                r.begin(command=pause(), handle=handle, trusted_context=changed_ctx, clock=clock)

    def test_rejection_cannot_be_joined_to_local_refusal(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.admit(attempt_id=a.attempt_id,
                receipt=local_receipt(ctx, handle, "rejected", "SCHEMA"), emitted_bytes=None)
        with self.assertRaises(TraceConflictError):
            r.record_outcome(attempt_id=a.attempt_id, outcome=outcome("rejected", state=BASE),
                             authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit)

    def test_status_and_outcome_exact_schema_validator(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        r.dispatch(attempt_id=a.attempt_id, adapter=broker.adapter, sink=broker.sink)
        malformed = ({**outcome(), "secret": "forged"}, {**outcome(), "order_seq_id": True},
                     {**outcome(), "state_sha256": BASE.upper()},
                     {**outcome(), "status": "ADMITTED"})
        for record in malformed:
            with self.subTest(record=record), self.assertRaises(TraceError):
                r.record_outcome(attempt_id=a.attempt_id, outcome=record,
                                 authenticated_source=auth(ctx), lookup_post_commit=broker.post_commit)

    def test_sink_multiple_emissions_halting(self):
        r, clock, broker = recorder(), FakeClock(), FakeBroker()
        ctx, handle = setup()
        a = r.begin(command=pause(), handle=handle, trusted_context=ctx, clock=clock)
        def broken(cmd, handle, context, sink):
            sink(b"one")
            sink(b"two")
        with self.assertRaises(TraceConflictError):
            r.dispatch(attempt_id=a.attempt_id, adapter=broken, sink=broker.sink)
        self.assertEqual(len(broker.inbox), 1)
        self.assertTrue(r.halted)
        self.assertEqual(r.cursor(a.attempt_id), 2)

    def test_limits_are_required_and_not_inferred(self):
        with self.assertRaises(TypeError):
            TraceRecorder()
        with self.assertRaises(TraceError):
            TraceRecorder(max_attempts=0, max_command_bytes=1024,
                          max_metadata_bytes=128, max_emitted_bytes=4096,
                          timeout_ns=200, approved_arg_keys=())


if __name__ == "__main__":
    unittest.main()
