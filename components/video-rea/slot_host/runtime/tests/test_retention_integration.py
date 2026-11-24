# SPDX-License-Identifier: AGPL-3.0-only
"""ret1 regressions at the readiness, coordinator and composition boundaries."""

import contextlib
import copy
import hashlib
import inspect
import io
import json
from pathlib import Path
import secrets
import sqlite3
from dataclasses import fields, is_dataclass, replace
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from slot_host.compare import tools as comparison
from slot_host.projections.mcp_lane import SlotProjector, control_reply
from slot_host.projections.project import control_envelope
from slot_host.projections.tests import test_projections
from slot_host.runtime import binding, tools
from slot_host.runtime.evidence_projection import INDEX_SHA
from slot_host.runtime.live_park import authorize_live, make_conformance_authority
from slot_host.runtime.conformance_authority import create_fixture_receipt_for_binding
from slot_host.runtime.tests.binding_fixtures import FixtureLaunch, fixture_policy
from slot_host.runtime.tests.test_conformance_authority import (
    _KIT, _NOW, _REGISTRY_GEN, _commit_policy, _enrolled, _manifest, _policy, _write_fixture,
)

# Keep the same tests runnable against the parent: exercise the old
# entry-point defaults when it lacks the injection parameter.
try:
    from slot_host.runtime.retention import RetentionClients, RetentionIdentity
except ModuleNotFoundError as exc:
    if exc.name != "slot_host.runtime.retention":
        raise
    RetentionClients = RetentionIdentity = SimpleNamespace


def _supports_flow(clients_type):
    return is_dataclass(clients_type) and any(field.name == "validate_profile" for field in fields(clients_type))


def _replace_clients(clients, **changes):
    if isinstance(clients, SimpleNamespace):
        return SimpleNamespace(**(vars(clients) | changes))
    return replace(clients, **changes)


_REQUIRED = {
    "admit": "fixture/authority",
    "start": "fixture/runner",
    "claim": "fixture/runner",
    "upload": "fixture/runner",
    "finish": "fixture/runner",
    "inspect": "fixture/inspector",
}
_TENANT = "fixture/tenant"
_POLICY = "fixture/policy"
_ACL = "fixture/acl"
_UNCHANGED = object()


def _injection(call, clients):
    return {"retention": clients} if "retention" in inspect.signature(call).parameters else {}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _admission_bytes(admission, _validator):
    fields = {key: value for key, value in admission.items() if key != "admission_sha256"}
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class _FakeClient:
    def __init__(self, principal, backend):
        self.principal = principal
        self.backend = backend
        self.validator = object()

    def session(self, tenant):
        return _FakeSession(self, tenant)


class _FakeSession:
    def __init__(self, client, tenant):
        self.client = client
        self.tenant = tenant

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def ready(self, verb, *, principal, policy_revision, acl_revision):
        backend = self.client.backend
        challenge = secrets.token_hex(32)
        backend.probes.append((self.client.principal, verb, challenge))
        response = {
            "challenge": challenge, "verb": verb, "tenant": self.tenant,
            "principal": self.client.principal, "record_pin": binding._V03_SHA,
            "index_pin": INDEX_SHA, "policy_revision": _POLICY, "acl_revision": _ACL,
        }
        if (principal, policy_revision, acl_revision) != (self.client.principal, _POLICY, _ACL):
            raise ValueError("Tools rejected the expected identity or revisions")
        if verb == backend.mutate_verb:
            response.update(backend.mutation)
        if response["challenge"] != challenge:
            raise ValueError("Tools rejected a stale challenge")
        return response

    def admit(self, admission):
        backend = self.client.backend
        if self.client.principal != "fixture/authority":
            raise AssertionError("only authority may admit")
        if admission["admission_sha256"] != _hash(_admission_bytes(admission, self.client.validator)):
            raise AssertionError("invalid admission digest")
        backend.admissions.append(copy.deepcopy(admission))
        if backend.lose_reply:
            backend.lose_reply = False
            raise OSError("reply lost after commit")
        return copy.deepcopy(admission)


class _Backend:
    def __init__(self):
        self.probes = []
        self.admissions = []
        self.mutate_verb = None
        self.mutation = {}
        self.lose_reply = False

    def clients(self, path):
        return RetentionClients(
            tenant=_TENANT, policy_revision=_POLICY, acl_revision=_ACL,
            profile_schema="fixture.profile/v1", profile_sha256="4" * 64,
            authority=RetentionIdentity(client=_FakeClient("fixture/authority", self), principal="fixture/authority"),
            runner=RetentionIdentity(client=_FakeClient("fixture/runner", self), principal="fixture/runner"),
            inspector=RetentionIdentity(client=_FakeClient("fixture/inspector", self), principal="fixture/inspector"),
            association_path=path, admission_bytes=_admission_bytes,
        )


class _RequestFixture:
    def fixture(self, root):
        self.root = root
        self.marker = root / "launched"
        self.exe = root / "tool"
        self.manifest = _manifest(_write_fixture(self.exe, str(self.marker)))
        self.manifest["evidence"]["shared_schema_ref"] = binding._V03_REF
        self.manifest["upstream"]["source_url"] = "https://fixture.example/tool"
        self.ref = "installation:fixture-slot@retention"
        self.policy = fixture_policy(self.manifest) | {"perception_ceiling": "none"}
        self.host = {self.ref: (self.manifest, self.exe)}
        self.payload = {"installation_ref": self.ref, "operation_id": "record"}
        self.path = root / "retention.sqlite3"
        self.context = {"principal": "fixture/operator"}

    def bind(self, clients):
        return binding.verify_binding(
            self.manifest, self.exe, operation_id="record", requested_profile="external_process",
            installation_ref=self.ref, capsule_perception_ceiling="none",
            **tools._auth_kwargs_from_policy(self.policy), **_injection(binding.verify_binding, clients),
        )

    def require_binding(self, clients):
        try:
            return self.bind(clients)
        except binding.BindingRejected as exc:
            self.fail(f"healthy readiness refused the binding: {exc.detail}")

    def invoke(self, clients, *, payload=_UNCHANGED, dry_run=False, request_id="req-retention-fixture",
               authority=None):
        return tools.dispatch(
            "behaviour.record",
            {"request_id": request_id, "payload": self.payload if payload is _UNCHANGED else payload,
             "dry_run": dry_run},
            host=self.host, host_policy=self.policy, context=self.context,
            live_authority=FixtureLaunch() if authority is None else authority,
            **_injection(tools.dispatch, clients),
        )

    def inspect_slot(self, clients, *, request_id="req-retention-inspect"):
        return tools.dispatch(
            "slot.inspect", {"request_id": request_id, "payload": self.payload},
            host=self.host, host_policy=self.policy, context=self.context,
            **_injection(tools.dispatch, clients),
        )["result"]["readiness"]


class RetentionTests(_RequestFixture, unittest.TestCase):
    def setUp(self):
        self.fixture(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.backend = _Backend()
        self.clients = self.backend.clients(self.path)
        self.enterContext(patch.object(tools, "_REQUEST_STORE", {}))

    def test_each_required_readiness_probe_rejects_stale_or_mismatched_reply(self):
        bound = self.require_binding(self.clients)
        self.assertEqual(bound.shared_schema_sha256, binding._V03_SHA)
        self.assertEqual({verb: principal for principal, verb, _ in self.backend.probes}, _REQUIRED)
        self.assertEqual(len({challenge for _, _, challenge in self.backend.probes}), len(_REQUIRED))
        for verb in _REQUIRED:
            for field, value in {
                "challenge": "0" * 64, "tenant": "fixture/other", "principal": "fixture/other",
                "record_pin": "0" * 64, "index_pin": "0" * 64, "policy_revision": "fixture/previous",
                "acl_revision": "fixture/previous", "verb": "recover",
            }.items():
                with self.subTest(verb=verb, field=field):
                    self.backend.mutate_verb, self.backend.mutation = verb, {field: value}
                    with self.assertRaises(binding.BindingRejected) as denied:
                        self.bind(self.clients)
                    self.assertEqual(denied.exception.reason, "profile_not_admitted")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.backend.admissions, [])

    def test_inspection_and_preview_are_readiness_only(self):
        self.assertEqual(self.inspect_slot(self.clients)["availability"], "admitted")
        preview = self.invoke(self.clients, dry_run=True)
        self.assertEqual(preview["result"], {"planned": True})
        self.assertFalse(self.path.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.backend.admissions, [])
        self.backend.mutate_verb, self.backend.mutation = "inspect", {"tenant": "fixture/other"}
        self.assertEqual(self.inspect_slot(self.clients)["availability"], "blocked")
        with self.assertRaises(tools._ControlError) as denied:
            self.invoke(self.clients, dry_run=True)
        self.assertEqual(denied.exception.code, "PROFILE_NOT_ADMITTED")

    def test_conformance_rechecks_readiness_and_execution_gate_stays_parked(self):
        bound = self.require_binding(self.clients)
        policy = _policy()
        policy["baseline"].update(shared_evidence_ref=binding._V03_REF, shared_evidence_sha256=binding._V03_SHA)
        signed = _commit_policy(self.root, policy)
        receipt = create_fixture_receipt_for_binding(
            bound, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN,
            now=_NOW, valid_for_seconds=3600,
        )
        authority = make_conformance_authority(
            policy=signed, receipts=[receipt], enrolment_check=_enrolled, now=_NOW,
            **_injection(make_conformance_authority, self.clients),
        )
        scope = dict(operation_id="record", request_id="req-retention-conformance", attempt_id="1" * 32)
        self.assertTrue(authority.verify(binding=bound, **scope))
        self.assertFalse(authorize_live(bound, self.exe, live_authority=authority, **scope))
        self.assertFalse(authorize_live(
            replace(bound, retention_required=False), self.exe, live_authority=authority, **scope,
        ))
        self.backend.mutate_verb, self.backend.mutation = "claim", {"acl_revision": "fixture/old"}
        self.assertFalse(authority.verify(binding=bound, **scope))
        self.assertIn("tools_retention_unavailable", authority.last_reason)
        self.assertFalse(self.path.exists())

    def test_winner_binds_jcs_digest_and_hands_off_separate_credentials(self):
        # Unicode key order and negative zero distinguish JCS from sort_keys.
        self.context = {"\uffff": -0.0, "\U00010000": 1.0, "principal": "fixture/operator"}
        self.policy["grants"] = tuple(self.policy["grants"])
        request_id = "req-retention-digest"
        with patch.object(tools, "run_bound", return_value={}) as runner:
            try:
                result = self.invoke(self.clients, request_id=request_id)
            except tools._ControlError as exc:
                self.fail(f"the ready coordinator refused admission: {exc.code}")
            self.assertTrue(result["ok"])
            before_join = len(self.backend.probes)
            self.assertEqual(self.invoke(self.clients, request_id=request_id), result)
        self.assertGreater(len(self.backend.probes), before_join)
        runner.assert_called_once()
        admission, = self.backend.admissions
        expected = (
            '{"context":{"principal":"fixture/operator","\U00010000":1,"\uffff":0},"dry_run":false,'
            '"payload":{"installation_ref":"installation:fixture-slot@retention","operation_id":"record"},'
            '"policy":' + json.dumps(self.policy, separators=(",", ":"), sort_keys=True) +
            ',"tool_id":"behaviour.record"}'
        )
        self.assertEqual(admission["request_sha256"], _hash(expected.encode()))
        self.assertEqual(admission["source_url"], self.manifest["upstream"]["source_url"])
        self.assertEqual(admission["implementation_sha256"], self.manifest["provider_ref"]["implementation_sha256"])
        self.assertEqual(admission["manifest_sha256"], self.policy["manifest_digest"])
        self.assertEqual(admission["installation_ref"], self.ref)
        self.assertEqual(admission["executor_id"], "fixture/runner")
        self.assertRegex(admission["attempt_id"], r"^[a-f0-9]{32}$")
        handoff = runner.call_args.kwargs["attempt_store"]
        self.assertEqual(runner.call_args.kwargs["attempt_id"], admission["attempt_id"])
        self.assertEqual(handoff.admission, admission)
        self.assertIs(handoff.runner_client, self.clients.runner.client)
        self.assertIs(handoff.inspector_client, self.clients.inspector.client)
        self.assertFalse(self.marker.exists())
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            durable_digest, claim_digest = db.execute(
                "SELECT intent_sha256, claim_sha256 FROM retention_requests"
            ).fetchone()
        self.assertEqual(durable_digest, admission["request_sha256"])
        self.assertNotEqual(claim_digest, durable_digest)

    def test_reply_loss_and_reopened_state_reuse_admission_and_refuse_changed_intent(self):
        self.backend.lose_reply = True
        with patch.object(tools, "run_bound", return_value={}) as runner:
            with self.assertRaises(tools._ControlError) as lost:
                self.invoke(self.clients)
            self.assertEqual(lost.exception.code, "UNCERTAIN_OUTCOME")
            runner.assert_not_called()
            with patch.object(tools, "_REQUEST_STORE", {}):
                restarted = self.backend.clients(self.path)
                with self.assertRaises(tools._ControlError) as changed:
                    self.invoke(restarted, payload=self.payload | {"timeout_s": 1})
                self.assertEqual(changed.exception.code, "REQUEST_ID_CONFLICT")
                self.assertTrue(self.invoke(restarted)["ok"])
            with patch.object(tools, "_REQUEST_STORE", {}):
                with self.assertRaises(tools._ControlError) as completed:
                    self.invoke(self.backend.clients(self.path))
                self.assertEqual(completed.exception.code, "UNCERTAIN_OUTCOME")
        runner.assert_called_once()
        self.assertEqual(len(self.backend.admissions), 2)
        self.assertEqual(*self.backend.admissions)
        self.assertFalse(self.marker.exists())

    def test_live_caller_cannot_supply_attempt_identity(self):
        with self.assertRaises(tools._ControlError) as denied:
            self.invoke(self.clients, payload=self.payload | {"attempt_id": "1" * 32})
        self.assertEqual(denied.exception.code, "SCHEMA_INVALID")
        self.assertEqual(self.backend.probes, [])
        self.assertFalse(self.path.exists())

    def test_runtime_cli_injects_clients_outside_the_request(self):
        args = {"request_id": "req-retention-cli", "payload": self.payload}
        actual = {}

        def dispatch(tool, arguments, **injected):
            actual.update(injected)
            self.assertEqual(arguments, args)
            return {"ok": True}

        with contextlib.redirect_stdout(io.StringIO()):
            code = tools.main(
                ["mini", "behaviour.record", "--args", json.dumps(args)], dispatch_fn=dispatch,
                **_injection(tools.main, self.clients),
            )
        self.assertEqual(code, 0)
        self.assertIs(actual.get("retention"), self.clients)

    def test_authenticated_mcp_injects_clients_outside_the_carrier(self):
        fixture = test_projections.Fixture()
        fixture.setUp()
        calls = []

        def dispatch(envelope, **injected):
            calls.append((envelope, injected))
            return control_reply(envelope["request_id"], "AVAILABILITY_PARKED", ok=False)

        projector = SlotProjector(
            fixture.manifest, fixture.bundle, dispatch=dispatch,
            authenticated_context={}, **fixture.sources(), **_injection(SlotProjector, self.clients),
        )
        name = fixture.route("ext.rea.record")["mcp_name"]
        projector.call_tool(name, fixture.args(params={"seed": 7}, dry_run=True))
        envelope, injected = calls[0]
        self.assertEqual(envelope["schema"], "vp.control.req/v1")
        self.assertNotIn("retention", envelope)
        self.assertIs(injected.get("retention"), self.clients)

    def test_per_call_mcp_retention_overrides_constructor_and_requires_authentication(self):
        fixture = test_projections.Fixture()
        fixture.setUp()
        calls = []

        def dispatch(envelope, **injected):
            calls.append(injected)
            return control_reply(envelope["request_id"], "AVAILABILITY_PARKED", ok=False)

        projector = SlotProjector(
            fixture.manifest, fixture.bundle, dispatch=dispatch,
            retention=object(), **fixture.sources(),
        )
        arguments = fixture.args(payload={"tape": "fixture"})
        envelope = control_envelope(fixture.manifest, "host.behaviour.record", arguments, context=None)
        missing_auth = SlotProjector(fixture.manifest, fixture.bundle, dispatch=dispatch, **fixture.sources())
        denied = missing_auth.handle_envelope(envelope, retention=self.clients)
        self.assertEqual(denied["code"], "NO_LEASE")
        self.assertEqual(calls, [])

        projector.handle_envelope(envelope, authenticated_context={}, retention=self.clients)
        projector.invoke("host.behaviour.record", arguments, authenticated_context={}, retention=self.clients)
        projector.call_tool("rea_behaviour_record", arguments, authenticated_context={}, retention=self.clients)
        self.assertEqual(calls, [{"retention": self.clients}] * 3)

    def test_comparison_cli_injects_separate_runner_and_inspector(self):
        payload = {"reference_ref": "ep:ref", "candidate_ref": "ep:other", "policy_ref": "policy:test"}
        for configured in (False, True):
            with self.subTest(configured=configured):
                clients = self.clients
                if configured:
                    clients = _replace_clients(clients, validate_profile=lambda *_: True)
                calls = []

                def dispatch(envelope, **injected):
                    calls.append((envelope, injected))
                    return {"ok": False, "code": "AVAILABILITY_PARKED"}

                args = {"request_id": "req-retention-compare", "payload": payload}
                with contextlib.redirect_stdout(io.StringIO()):
                    code = comparison.main(
                        ["mini", "behaviour.compare", "--args", json.dumps(args)], dispatch=dispatch,
                        **_injection(comparison.main, clients),
                    )
                self.assertEqual(code, 16)
                envelope, injected = calls[0]
                self.assertEqual(envelope["args"]["arguments"]["payload"], payload)
                self.assertEqual(set(injected), {"retention", "runner_client", "inspector_client"})
                handoff = injected["retention"]
                self.assertFalse(hasattr(handoff, "authority"))
                self.assertIs(handoff.runner.client, clients.runner.client)
                self.assertIs(handoff.inspector.client, clients.inspector.client)
                self.assertIs(injected["runner_client"], clients.runner.client)
                self.assertIs(injected["inspector_client"], clients.inspector.client)

    def test_caller_attempt_identity_rejected_with_no_retention(self):
        for tool in ("behaviour.record", "behaviour.replay"):
            with self.subTest(tool=tool):
                payload = self.payload | {"operation_id": tool.split(".")[1], "attempt_id": "1" * 32}
                with patch.object(tools, "_resolve_adapter", wraps=tools._resolve_adapter) as prepare:
                    with self.assertRaises(tools._ControlError) as denied:
                        tools.dispatch(tool, {"request_id": "req-retfix-caller-attempt", "payload": payload},
                                       host=self.host, host_policy=self.policy, retention=None)
                self.assertEqual(denied.exception.code, "SCHEMA_INVALID")
                prepare.assert_not_called()

    def test_no_retention_requires_exact_fixture_attempt_capability(self):
        from slot_host.runtime.live_park import _FixtureLiveAuthority

        bound = replace(self.require_binding(self.clients), retention_required=False)
        request = "req-retfix-fixture-attempt"
        arguments = {"request_id": request, "payload": self.payload}
        prepared = bound, "none", None, None, None
        with patch.object(tools, "_resolve_adapter", return_value=prepared), patch.object(
            tools, "run_bound", return_value={},
        ) as run:
            with self.assertRaises(tools._ControlError) as denied:
                tools.dispatch("behaviour.record", arguments, live_authority=FixtureLaunch())
            self.assertEqual(denied.exception.code, "LIVE_EXECUTION_PARKED")
            run.assert_not_called()
            fixture = _FixtureLiveAuthority(bound, request_id=request, attempt_id="fixture-host-allocated")
            reply = tools.dispatch("behaviour.record", arguments, live_authority=fixture)
            self.assertTrue(reply["ok"])
            self.assertEqual(run.call_args.kwargs["attempt_id"], "fixture-host-allocated")
            other = _FixtureLiveAuthority(bound, request_id=request, attempt_id="fixture-other")
            with self.assertRaises(tools._ControlError) as conflict:
                tools.dispatch("behaviour.record", arguments, live_authority=other)
            self.assertEqual(conflict.exception.code, "REQUEST_ID_CONFLICT")
            run.assert_called_once()
