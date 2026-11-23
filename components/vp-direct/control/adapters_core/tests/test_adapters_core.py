"""C2 admission and readback exercised through the C1 dispatcher."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT.parents[1] / "agents")]

from control import adapters_core as adapters
from control.core import control
from metaviewer_agents.envelope import make_envelope


EXAMPLE_PATH = ROOT / "schemas/tests/examples/adapter_pairs.json"
EXAMPLES = json.loads(EXAMPLE_PATH.read_bytes())
CATALOG = json.loads((ROOT / "schemas/adapter_ops.json").read_bytes())
IDENTITY = control.LaneIdentity("agent", "fixture-agent")
OPERATOR = control.LaneIdentity("operator", "fixture-operator")
CAPSULE = "vp_capsule_call"
CAPTURE = "vp_capture_call"
SAVED = "vp_saved_data_call"
GUEST = "vp_guest_command_call"
HEADLESS = "vp_headless_call"
TOOLS = (CAPSULE, CAPTURE, SAVED, GUEST, HEADLESS)


def synthetic_catalog():
    catalog = deepcopy(CATALOG)
    fixture_source = {
        "path": str(EXAMPLE_PATH.relative_to(ROOT.parents[1])),
        "line_start": 1,
        "line_end": len(EXAMPLE_PATH.read_bytes().splitlines()),
        "sha256": hashlib.sha256(EXAMPLE_PATH.read_bytes()).hexdigest(),
    }
    for tool in TOOLS:
        for row in catalog[tool]["ops"].values():
            if row.get("refusal_only"):
                continue
            row.update(supported=True, binding={
                "kind": "core_route", "name": f"fixture-{tool}", "source": fixture_source,
            })
    return catalog


def request(tool, selector, *, request_id=None, dry_run=False, identity=IDENTITY):
    return {
        "schema": control.REQUEST,
        "request_id": request_id or f"case-{tool}-{selector}-{dry_run}",
        "op": tool,
        "context": {"caller": identity.caller, "agent_instance": identity.agent_instance},
        "args": deepcopy(EXAMPLES[tool][selector]["args"]),
        "dry_run": dry_run,
    }


def allow(_request, _risk):
    return True


class FixtureCore:
    def __init__(self, *, component="agent", alter_reply=None):
        self.component = component
        self.alter_reply = alter_reply
        self.calls = []

    def execute(self, route, envelope):
        return self._exchange("execute", route, envelope)

    def preview(self, route, envelope):
        return self._exchange("preview", route, envelope)

    def _exchange(self, method, route, envelope):
        self.calls.append((method, self.component, route, deepcopy(envelope)))
        args = envelope["args"]
        result = EXAMPLES[envelope["op"]][args["op"]]["result"]
        reply = {
            "schema": control.REPLY,
            "request_id": envelope["request_id"],
            "op": envelope["op"],
            "effective_request_sha256": control.digest(envelope),
            "ok": True,
            "code": "OK",
            "result": deepcopy(result),
            "evidence": [],
            "not_verified": [],
        }
        return self.alter_reply(reply) if self.alter_reply else reply


class FixtureNativeClient:
    local = {"endpoint": {"component": "agent", "instance": "fixture-agent"}}
    owner = "actor-" + hashlib.sha256(b"agent:fixture-agent").hexdigest()

    def __init__(self, alter_event=None):
        self.calls = []
        self.alter_event = alter_event
        self.guest_commands = self

    def request(self, body, expect, *, timeout_ms):
        self.calls.append((deepcopy(body), expect, timeout_ms))
        # Use the native envelope builder, which validates both body contracts.
        make_envelope(msg_id=body["request_id"], from_component="agent",
                      from_instance="fixture-agent", to_component="core", to_instance="core",
                      session_id="fixture", seq=1, body=body)
        op, params = body["op"], body["params"]
        kind, phase, ownership, job = "state_changed", "ready", "held", None
        if op == "run":
            kind, phase = "completed", "completed"
            job = {"job_id": params["job_id"], "operation": "sumsq", "n": params["n"],
                   "status": "completed", "value": 5}
        if op == "stop":
            phase, ownership = "stopped", "released"
        event = {
            "schema": "headless_event/v0.2", "event_id": "event-fixture",
            "request_id": body["request_id"], "event": kind,
            "data": {"session": {"schema": "headless_session/v0.2", "workload_id": "workload-fixture",
                                 "generation": 1, "owner_id": self.owner, "profile_id": "headless-sumsq-v1",
                                 "backend": "firecracker", "isa": "aarch64", "job": job,
                                 "phase": phase, "ownership": ownership}},
        }
        if self.alter_event is not None:
            self.alter_event(event, op)
        make_envelope(msg_id=event["event_id"], from_component="core",
                      from_instance="core", to_component="agent", to_instance="fixture-agent",
                      session_id="fixture", seq=2, body=event)
        return event

    def create(self, *, timeout_ms):
        self.calls.append(("guest-create", timeout_ms))
        return {"workload_id": "workload-fixture", "generation": 1, "owner_id": self.owner,
                "profile_id": "guest-command-arm64-v1", "phase": "ready", "ownership": "held"}


class CoreAdapterTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory(dir=TESTS, prefix=".core-test-"))
        self.directory = Path(directory)
        self.index = 0

    def dispatcher(self, peer, *, pace=allow, lease=allow, store_path=None):
        self.index += 1
        registry = control.CapabilityRegistry()
        bindings = adapters.build_bindings(registry, peer)
        if store_path is None:
            store_path = self.directory / f"requests-{self.index}.sqlite"
        return control.Dispatcher(
            registry,
            control.RequestStore(store_path),
            pace_risk=pace,
            lease_epoch=lease,
            bindings=bindings,
        )

    def qualify(self, catalog=None):
        return patch.object(
            control._validation, "_adapter_rows",
            return_value=synthetic_catalog() if catalog is None else catalog,
        )

    def assert_success(self, response, envelope):
        tool, selector = envelope["op"], envelope["args"]["op"]
        self.assertTrue(response["ok"])
        self.assertEqual(response["code"], "OK")
        self.assertEqual(response["request_id"], envelope["request_id"])
        self.assertEqual(response["op"], tool)
        self.assertEqual(response["effective_request_sha256"], control.digest(envelope))
        self.assertEqual(response["result"], EXAMPLES[tool][selector]["result"])

    def assert_refused(self, reply, code):
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], code)
        self.assertEqual(reply["result"], {})

    def test_registered_operations_dispatch_and_validate_readback(self):
        peer = FixtureCore()
        with self.qualify():
            dispatcher = self.dispatcher(peer)
            for tool in TOOLS:
                for selector, row in CATALOG[tool]["ops"].items():
                    if row.get("refusal_only"):
                        continue
                    with self.subTest(tool=tool, selector=selector):
                        identity = OPERATOR if row["operator_only"] else IDENTITY
                        envelope = request(tool, selector, identity=identity)
                        reply = dispatcher.dispatch_mailbox(envelope, identity)
                        self.assert_success(reply, envelope)
                        self.assertEqual(peer.calls[-1], (
                            "execute", "agent", f"fixture-{tool}", envelope,
                        ))
            self.assertEqual(len(peer.calls), sum(
                not row.get("refusal_only")
                for tool in TOOLS for row in CATALOG[tool]["ops"].values()
            ))

    def test_preview_never_executes_and_retry_returns_same_reply(self):
        peer = FixtureCore()
        with self.qualify():
            dispatcher = self.dispatcher(peer)
            envelope = request(CAPTURE, "screenshot", dry_run=True)
            reply = dispatcher.dispatch_mailbox(envelope, IDENTITY)
            repeated = dispatcher.dispatch_cli("capture", envelope, IDENTITY)
        self.assert_success(reply, envelope)
        self.assertEqual(repeated, reply)
        self.assertEqual(peer.calls, [("preview", "agent", f"fixture-{CAPTURE}", envelope)])

    def test_no_production_bindings_and_pin_layout_remain_parked(self):
        peer = FixtureCore()
        dispatcher = self.dispatcher(peer)
        for tool in TOOLS:
            for selector in CATALOG[tool]["ops"]:
                with self.subTest(tool=tool, selector=selector):
                    envelope = request(tool, selector, identity=OPERATOR)
                    self.assert_refused(dispatcher.dispatch_mailbox(envelope, OPERATOR), "HOLD")
        with self.qualify():
            dispatcher = self.dispatcher(peer)
            for selector in ("pin", "layout_save", "layout_load"):
                envelope = request(CAPSULE, selector)
                self.assert_refused(dispatcher.dispatch_mailbox(envelope, IDENTITY), "HOLD")
        self.assertEqual(peer.calls, [])

    def test_missing_row_or_pinned_schema_refuses_before_transport(self):
        peer = FixtureCore()
        for missing in ("row", "args_schema_sha256", "reply_schema_sha256"):
            with self.subTest(missing=missing):
                catalog = synthetic_catalog()
                rows = catalog[CAPSULE]["ops"]
                if missing == "row":
                    del rows["pause"]
                else:
                    rows["pause"][missing] = "0" * 64
                with self.qualify(catalog):
                    dispatcher = self.dispatcher(peer)
                    envelope = request(CAPSULE, "pause")
                    reply = dispatcher.dispatch_mailbox(envelope, IDENTITY)
                self.assert_refused(reply, "ARGS_SCHEMA_ABSENT")
        self.assertEqual(peer.calls, [])

    def test_foreign_binding_kind_does_not_install_a_core_handler(self):
        peer = FixtureCore()
        catalog = synthetic_catalog()
        catalog[CAPSULE]["ops"]["pause"]["binding"]["kind"] = "host_ipc"
        with self.qualify(catalog):
            dispatcher = self.dispatcher(peer)
            reply = dispatcher.dispatch_mailbox(request(CAPSULE, "pause"), IDENTITY)
        self.assert_refused(reply, "HOLD")
        self.assertEqual(peer.calls, [])

    def test_c1_keeps_operator_pace_and_lease_gates(self):
        peer = FixtureCore()
        with self.qualify():
            dispatcher = self.dispatcher(peer)
            reply = dispatcher.dispatch_mailbox(request(CAPSULE, "role_switch"), IDENTITY)
            self.assert_refused(reply, "OWNER")
            for gate, code in (("pace", "PACE_LIMITED"), ("lease", "NO_LEASE")):
                with self.subTest(gate=gate):
                    dispatcher = self.dispatcher(peer, **{gate: lambda *_: code})
                    response = dispatcher.dispatch_mailbox(request(CAPSULE, "pause"), IDENTITY)
                    self.assert_refused(response, code)
        self.assertEqual(peer.calls, [])

    def test_core_readback_must_identify_request_and_validate_exact_result(self):
        def select_other_operation_result(reply):
            return dict(reply, result=EXAMPLES[CAPSULE]["stop"]["result"])

        cases = (
            lambda reply: dict(reply, request_id="different"),
            lambda reply: dict(reply, op=HEADLESS),
            lambda reply: dict(reply, effective_request_sha256="0" * 64),
            select_other_operation_result,
        )
        with self.qualify():
            for alter in cases:
                with self.subTest(alter=alter):
                    peer = FixtureCore(alter_reply=alter)
                    dispatcher = self.dispatcher(peer)
                    envelope = request(CAPSULE, "pause")
                    reply = dispatcher.dispatch_mailbox(envelope, IDENTITY)
                    repeated = dispatcher.dispatch_mailbox(envelope, IDENTITY)
                    self.assert_refused(reply, "UNCERTAIN_OUTCOME")
                    self.assertEqual(repeated, reply)
                    self.assertEqual(len(peer.calls), 1)

    def test_saved_data_accepted_is_not_terminal_readback(self):
        def accept_without_completion(reply):
            return dict(reply, result=dict(reply["result"], event="accepted"))

        peer = FixtureCore(alter_reply=accept_without_completion)
        with self.qualify():
            dispatcher = self.dispatcher(peer)
            reply = dispatcher.dispatch_mailbox(request(SAVED, "list"), IDENTITY)
        self.assert_refused(reply, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(peer.calls), 1)

    def assert_uncertain_retry_is_fenced(self, *, forget_claim=False):
        def fail_after_send(reply):
            if len(first_peer.calls) == 1:
                raise TimeoutError("transport sent the request but no terminal readback arrived")
            return reply

        first_peer = FixtureCore(alter_reply=fail_after_send)
        replacement = FixtureCore()
        store_path = self.directory / "durable.sqlite"
        envelope = request(HEADLESS, "create")
        with self.qualify():
            first = self.dispatcher(first_peer, store_path=store_path)
            pending = first.dispatch_mailbox(envelope, IDENTITY)
            self.assert_refused(pending, "UNCERTAIN_OUTCOME")
            self.assertEqual(len(first_peer.calls), 1)
            if forget_claim:
                with closing(sqlite3.connect(store_path)) as db, db:
                    db.execute("DELETE FROM requests WHERE id = ?", (envelope["request_id"],))
            restarted = self.dispatcher(replacement, store_path=store_path)
            repeated = restarted.dispatch_cli("headless", envelope, IDENTITY)
            self.assertEqual(replacement.calls, [], "retry reached Core transport")
            self.assertEqual(repeated, pending)
            altered = deepcopy(envelope)
            altered["dry_run"] = True
            self.assert_refused(restarted.dispatch_mailbox(altered, IDENTITY), "REQUEST_ID_CONFLICT")

    def test_retry_after_uncertain_outcome_never_redispatches_transport(self):
        self.assert_uncertain_retry_is_fenced()

    def test_mutant_forgetting_uncertain_claim_is_killed(self):
        with self.assertRaisesRegex(AssertionError, "retry reached Core transport"):
            self.assert_uncertain_retry_is_fenced(forget_claim=True)

    def test_native_headless_exchange_matches_wire_and_returns_terminal_identity(self):
        client = FixtureNativeClient()
        with self.qualify():
            dispatcher = self.dispatcher(adapters.NativeCoreTransport(client, timeout_ms=1000))
            for selector in ("create", "run", "stop"):
                with self.subTest(selector=selector):
                    envelope = request(HEADLESS, selector)
                    if selector == "run":
                        envelope["args"]["n"] = 2
                    self.assert_success(dispatcher.dispatch_mailbox(envelope, IDENTITY), envelope)
                    body, expect, timeout_ms = client.calls[-1]
                    self.assertEqual(body, {
                        "schema": "headless_request/v0.2", "op": selector,
                        "request_id": "request-" + hashlib.sha256(envelope["request_id"].encode()).hexdigest(),
                        "params": {k: v for k, v in envelope["args"].items() if k != "op"},
                    })
                    self.assertEqual((expect, timeout_ms), ("headless_event/v0.2", 1000))
        self.assertEqual(len(client.calls), 3)

    def test_native_guest_create_uses_the_owned_guest_session(self):
        client = FixtureNativeClient()
        with self.qualify():
            dispatcher = self.dispatcher(adapters.NativeCoreTransport(client, timeout_ms=1000))
            envelope = request(GUEST, "create")
            self.assert_success(dispatcher.dispatch_mailbox(envelope, IDENTITY), envelope)
            self.assertEqual(client.calls, [("guest-create", 1000)])

    def test_native_guest_starting_session_is_not_ready_readback(self):
        client = FixtureNativeClient()
        create = client.create

        def starting_session(**kwargs):
            return dict(create(**kwargs), phase="starting")

        with self.qualify(), patch.object(client, "create", side_effect=starting_session):
            dispatcher = self.dispatcher(adapters.NativeCoreTransport(client, timeout_ms=1000))
            reply = dispatcher.dispatch_mailbox(request(GUEST, "create"), IDENTITY)
        self.assert_refused(reply, "UNCERTAIN_OUTCOME")
        self.assertEqual(client.calls, [("guest-create", 1000)])

    def test_native_ui_only_routes_and_unmapped_selectors_do_not_send(self):
        client = FixtureNativeClient()
        with self.qualify():
            dispatcher = self.dispatcher(adapters.NativeCoreTransport(client, timeout_ms=1000))
            for tool, op, expected in ((CAPTURE, "screenshot", "policy_denied"),
                                       (CAPTURE, "ram_export", "policy_denied"),
                                       (CAPTURE, "frame_subscribe", "HOLD"),
                                       (SAVED, "list", "policy_denied"),
                                       (GUEST, "run", "HOLD"),
                                       (CAPSULE, "add", "HOLD")):
                self.assert_refused(dispatcher.dispatch_mailbox(request(tool, op), IDENTITY), expected)
            preview = request(HEADLESS, "create", dry_run=True)
            self.assert_refused(dispatcher.dispatch_mailbox(preview, IDENTITY), "HOLD")
        self.assertEqual(client.calls, [])

    def test_native_headless_owner_refusal_and_unknown_outcome_propagate(self):
        def replace_event(event, op):
            if op == "create":
                event.update(event="refused", data={"workload_id": None, "generation": None,
                                                    "effect": "not_dispatched", "code": "OWNER_MISMATCH"})
            else:
                event.update(event="outcome_unknown")
                event["data"].update(op=op, code="OUTCOME_UNKNOWN")
                event["data"]["session"].update(phase="uncertain", ownership="held")

        client = FixtureNativeClient(replace_event)
        with self.qualify():
            dispatcher = self.dispatcher(adapters.NativeCoreTransport(client, timeout_ms=1000))
            self.assert_refused(dispatcher.dispatch_mailbox(request(HEADLESS, "create"), IDENTITY), "OWNER")
            self.assert_refused(dispatcher.dispatch_mailbox(request(HEADLESS, "stop"), IDENTITY),
                                "UNCERTAIN_OUTCOME")

    def test_native_headless_refuses_provisional_or_incorrect_job_readback(self):
        def accepted(event, op):
            event["event"] = "accepted"
            event["data"]["op"] = op

        def wrong_job(event, _):
            event["data"]["session"]["job"]["value"] = 6

        def wrong_workload(event, _):
            event["data"]["session"]["workload_id"] = "workload-foreign"

        with self.qualify():
            for mutation in (accepted, wrong_job, wrong_workload):
                with self.subTest(event=mutation.__name__):
                    client = FixtureNativeClient(mutation)
                    dispatcher = self.dispatcher(adapters.NativeCoreTransport(client, timeout_ms=1000))
                    envelope = request(HEADLESS, "run")
                    envelope["args"]["n"] = 2
                    self.assert_refused(dispatcher.dispatch_mailbox(envelope, IDENTITY), "UNCERTAIN_OUTCOME")
                    self.assertEqual(len(client.calls), 1)

    def assert_foreign_native_owner_refused(self):
        def replace_owner(event, _):
            event["data"]["session"]["owner_id"] = foreign_owner

        foreign_owner = "actor-" + "f" * 64
        client = FixtureNativeClient(replace_owner)
        with self.qualify():
            dispatcher = self.dispatcher(adapters.NativeCoreTransport(client, timeout_ms=1000))
            reply = dispatcher.dispatch_mailbox(request(HEADLESS, "create"), IDENTITY)
        self.assertEqual(len(client.calls), 1)
        self.assert_refused(reply, "UNCERTAIN_OUTCOME")

    def test_native_headless_foreign_owner_is_uncertain(self):
        self.assert_foreign_native_owner_refused()

    def test_mutant_accepting_foreign_native_owner_is_killed(self):
        with patch.object(adapters.NativeCoreTransport, "_owner_id", return_value="actor-" + "f" * 64):
            with self.assertRaisesRegex(AssertionError, "True is not false"):
                self.assert_foreign_native_owner_refused()

    def test_unknown_or_ui_core_component_is_refused(self):
        with self.qualify():
            for component in ("ui", None, "absent"):
                for dry_run in (False, True):
                    with self.subTest(component=component, dry_run=dry_run):
                        peer = FixtureCore(component=component)
                        if component == "absent":
                            del peer.component
                        dispatcher = self.dispatcher(peer)
                        envelope = request(CAPTURE, "screenshot", dry_run=dry_run)
                        reply = dispatcher.dispatch_mailbox(envelope, IDENTITY)
                        self.assert_refused(reply, "policy_denied")
                        self.assertEqual(peer.calls, [])

    @staticmethod
    def core_owner_refusal(reply):
        return dict(reply, ok=False, code="OWNER", result={})

    def test_core_owner_refusal_propagates(self):
        peer = FixtureCore(alter_reply=self.core_owner_refusal)
        with self.qualify():
            dispatcher = self.dispatcher(peer)
            reply = dispatcher.dispatch_mailbox(request(CAPSULE, "pause"), IDENTITY)
        self.assert_refused(reply, "OWNER")
        self.assertEqual(len(peer.calls), 1)

    def test_mutant_impersonating_component_ui_is_killed(self):
        peer = FixtureCore(component="ui")
        with self.qualify(), patch.object(adapters, "_CORE_COMPONENT", "ui"):
            dispatcher = self.dispatcher(peer)
            reply = dispatcher.dispatch_mailbox(request(CAPTURE, "screenshot"), IDENTITY)
        self.assertEqual([call[1] for call in peer.calls], ["ui"])
        self.assertTrue(reply["ok"])
        with self.assertRaises(AssertionError):
            self.assert_refused(reply, "policy_denied")

    def test_mutant_swallowing_core_owner_refusal_is_killed(self):
        def swallow_owner(envelope, reply):
            seen.append(reply["code"])
            result = deepcopy(EXAMPLES[CAPSULE]["pause"]["result"])
            return original_readback(envelope, dict(reply, ok=True, code="OK", result=result))

        peer = FixtureCore(alter_reply=self.core_owner_refusal)
        seen = []
        original_readback = adapters._readback
        with self.qualify(), patch.object(adapters, "_readback", swallow_owner):
            dispatcher = self.dispatcher(peer)
            reply = dispatcher.dispatch_mailbox(request(CAPSULE, "pause"), IDENTITY)
        self.assertEqual(seen, ["OWNER"])
        self.assertTrue(reply["ok"])
        with self.assertRaises(AssertionError):
            self.assert_refused(reply, "OWNER")
