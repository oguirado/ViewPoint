# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise REA's local-open adapter and qualification controls."""

from base64 import b64decode
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from hashlib import sha256
from importlib import import_module
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from slot_host.runtime import evidence_projection as projection, retention_witness


class LocalOpenQualificationTests(unittest.TestCase):
    def module(self, name):
        try:
            return import_module(name)
        except ModuleNotFoundError as error:
            if not name.startswith(error.name):
                raise
            self.fail(f"the local-open qualification entry point is missing ({error.name})")

    def set_up_local(self):
        deployment = self.module("slot_host.runtime.retention_deployment")
        if not os.environ.get("REA_RETENTION_SRC"):
            self.skipTest("owner-pending: T01 (REA_RETENTION_SRC is unset)")
        fixtures = deployment.import_tools("retention_service.server.test_local")
        synthetic = deployment.import_tools("retention_service.server.test_e2e")
        harness = fixtures.LocalREA()
        self.addCleanup(harness.doCleanups)
        harness.setUp()
        # Fixture evidence exercises the service APIs and qualifies no REA installation.
        self.harness = harness
        self.synthetic = synthetic
        self.transport = fixtures.TRANSPORT
        self.server = deployment.import_tools("retention_service.server.__main__")
        return deployment.load_local_retention(harness.root / "association.db",
                                               observe_terminal=lambda *_: None)

    def attempt(self, tenant=None):
        return self.harness.attempt(self.transport, tenant=tenant)

    def test_denial_readiness_rejects_missing_and_malformed_challenges(self):
        from slot_host.runtime.retention_deployment import LocalRetention

        reader = Mock()
        client = Mock()
        client.session.return_value = nullcontext(reader)
        local = LocalRetention(SimpleNamespace(policy_revision="policy", acl_revision="acl"),
                               SimpleNamespace(client=client, principal="denial"), "control")
        proof = {"verb": "inspect", "tenant": "control", "principal": "denial",
                 "policy_revision": "policy", "acl_revision": "acl", "record_pin": projection.V03_SHA,
                 "index_pin": projection.INDEX_SHA}
        reader.ready.return_value = proof | {"challenge": "a" * 64}
        self.assertTrue(local.check_denial_endpoint())
        for response in (proof, *(proof | {"challenge": value} for value in (None, 17, "x", "G" * 64))):
            with self.subTest(response=response):
                reader.ready.return_value = response
                self.assertFalse(local.check_denial_endpoint())

    def test_generated_roles_share_the_writer_principal_with_distinct_clients(self):
        local = self.set_up_local()
        clients = local.clients
        with self.server.open_local_server(witness_module=self.synthetic.__name__):
            self.assertTrue(clients.ready(record_pin=projection.V03_SHA, operation_id="record"))
            self.assertTrue(clients.ready(record_pin=projection.V03_SHA, operation_id="compare"))
            self.assertFalse(clients.ready(record_pin="0" * 64, operation_id="record"))
            self.assertTrue(local.check_denial_endpoint())
        self.assertEqual(clients.authority.principal, clients.runner.principal)
        self.assertIsNot(clients.authority.client, clients.runner.client)
        self.assertEqual(clients.authority.client.endpoint.unix, clients.runner.client.endpoint.unix)
        self.assertEqual(clients.inspector.client.endpoint.unix, self.harness.endpoints["inspector"].unix)
        self.assertIsNone(clients.runner.client.tls)
        with self.assertRaisesRegex(ValueError, "role identities"):
            replace(clients, local_open=False)

    def test_local_role_rejects_an_unpinned_successor_route_before_readiness(self):
        local = self.set_up_local()
        from slot_host.runtime.conformance_authority import tools_retention_available

        binding = SimpleNamespace(operation_id="record", shared_schema_ref=projection.V03_REF,
                                  shared_schema_sha256=projection.V03_SHA, provider_schema="rea.provider/v2",
                                  request_schema_ref="urn:rea.slot_request:v2",
                                  result_schema_ref="urn:rea.slot_result:v2")
        with self.server.open_local_server(witness_module=self.synthetic.__name__):
            self.assertTrue(tools_retention_available(binding=binding, operation_id="record", retention=local.clients))
            binding.provider_schema = "rea.provider/previous"
            self.assertFalse(tools_retention_available(binding=binding, operation_id="record", retention=local.clients))
            binding.provider_schema = "rea.provider/v2"
            binding.result_schema_ref = "urn:rea.slot_result:previous"
            self.assertFalse(tools_retention_available(binding=binding, operation_id="record", retention=local.clients))

    def test_command_starts_isolated_service_and_refuses_missing_live_witness(self):
        self.set_up_local()
        command = self.module("slot_host.qualify.__main__")
        output = self.harness.root / "qualification.json"
        diagnostic = StringIO()
        original = self.server.open_local_server
        missing = []

        @contextmanager
        def service_without_witness(**options):
            missing.append(options.pop("witness_module", None))
            with original(**options) as service:
                yield service

        with patch.object(command, "scope_properties", return_value={}), redirect_stderr(diagnostic), \
                patch.object(self.server, "open_local_server", service_without_witness), \
                patch.object(command, "measure_revisions", return_value={}, create=True):
            self.assertEqual(command.main(["--out", str(output)]), 1)
        self.assertEqual(missing, ["slot_host.runtime.retention_witness"])
        self.assertIn("record: PROFILE_NOT_ADMITTED", diagnostic.getvalue())
        self.assertIn("owner-pending: T03", diagnostic.getvalue())
        self.assertFalse(output.exists())

    def test_local_actor_cannot_reuse_an_offline_witness(self):
        actor = SimpleNamespace(tenant=f"local/uid/{os.geteuid()}", principal=f"local/uid/{os.geteuid()}/writer")
        admission = {"operation": "record", "executor_id": actor.principal,
                     "attempt_id": "a" * 32, "request_sha256": "b" * 64, "admission_sha256": "c" * 64}
        ack = {key: admission[key] for key in ("attempt_id", "request_sha256")} | {"start_sha256": "d" * 64}
        with patch.object(retention_witness, "_offline") as offline:
            with self.assertRaisesRegex(OSError, "local-open live witness source is absent"):
                retention_witness.witness("claim", actor, admission, ack)
            offline.assert_not_called()

    def test_denial_control_reads_its_own_committed_attempt_and_refuses_primary(self):
        local = self.set_up_local()
        controls = self.module("slot_host.qualify.controls")
        control = local.control_clients(self.harness.root / "control.db")
        self.assertIs(control.inspector, local.denial)
        with self.server.open_local_server(witness_module=self.synthetic.__name__):
            primary = self.attempt()[0]["attempt_id"]
            own = self.attempt(tenant=control.tenant)[0]["attempt_id"]
            with control.inspector.client.session(control.tenant) as reader:
                view = reader.inspect(own)
                self.assertEqual(reader.receipt(view["receipt_ref"]), view)
                self.assertEqual(view["state"], "committed")
                controls.status_is(404, lambda: reader.inspect(primary), "cross_tenant_denial")
                with self.assertRaisesRegex(ValueError, "unexpectedly succeeded"):
                    controls.status_is(404, lambda: reader.inspect(own), "cross_tenant_denial")

    def test_exact_and_conflicting_finish_hold_deletion_and_recovery(self):
        clients = self.set_up_local().clients
        controls = self.module("slot_host.qualify.controls")
        with self.server.open_local_server(witness_module=self.synthetic.__name__) as server:
            admission, _, _, _, artifact, _ = self.attempt()
            with clients.inspector.client.session(clients.tenant) as reader:
                view = reader.inspect(admission["attempt_id"])
                retained = SimpleNamespace(inspection=view, artifact=artifact)
            controls.finish_retries(clients, retained)
            controls.deletion_control(server.backend.store, clients, (), retained)
            with clients.runner.client.session(clients.tenant) as writer:
                snapshot = controls.snapshot(writer)
                self.assertTrue(any(item["kind"] == "tombstone" for item in snapshot["items"]))
            controls.status_is(404, lambda: controls.finish_retries(clients, retained), "deleted_finish")

    def test_lost_claim_is_durable_unknown_and_retry_cannot_claim_again(self):
        clients = self.set_up_local().clients
        controls = self.module("slot_host.qualify.controls")
        fault, arm_loss = controls.claim_reply_loss()
        with self.server.open_local_server(witness_module=self.synthetic.__name__, fault=fault):
            admission, raw = self.harness.fixture.admission(
                "e" * 32, request_id="fixture/loss/claim", executor=clients.runner.principal,
            )
            with clients.runner.client.session(clients.tenant) as writer:
                writer.admit(admission)
                ack = writer.start(admission["attempt_id"], raw, request_sha256=admission["request_sha256"])
                with arm_loss() as triggered:
                    controls.status_is(503, lambda: writer.claim(ack), "lost_claim")
                    self.assertTrue(triggered())
            with clients.runner.client.session(clients.tenant) as writer:
                controls.status_is(409, lambda: writer.claim(ack), "lost_claim_retry")
            with clients.runner.client.session(clients.tenant) as writer:
                manifest = controls.snapshot(writer)
                self.assertEqual(manifest["attempts"][0]["disposition"], "unknown")
            with clients.inspector.client.session(clients.tenant) as reader:
                view = reader.inspect(admission["attempt_id"])
                self.assertEqual((view["state"], view["launch_claimed"], view["terminal"]),
                                 ("unknown", True, None))

    def test_qualification_rejects_a_non_conformance_authority_before_dispatch(self):
        controls = self.module("slot_host.qualify.controls")
        authority = SimpleNamespace(verify=lambda **_: True)
        call = controls.LiveCall(SimpleNamespace(), authority, "host.behaviour.record", {"request_id": "req-qual"}, None)
        with self.assertRaisesRegex(ValueError, "authenticated default-on composition"):
            call.invoke("cli", None)

    def test_qualification_refuses_both_faces_if_dispatch_does_not_use_its_authority(self):
        controls = self.module("slot_host.qualify.controls")
        from slot_host.projections.mcp_lane import SlotProjector, control_reply
        from slot_host.projections.tests.test_projections import Fixture
        from slot_host.runtime.conformance_authority import ConformanceLiveAuthority
        from slot_host.runtime.tests.test_conformance_authority import _bind, _manifest, _write_fixture
        from slot_host.runtime import binding as binding_module

        command = self.module("slot_host.qualify.__main__")
        fixture = Fixture()
        fixture.setUp()
        clients = object()
        seen = []

        def dispatch(envelope, *, retention):
            self.assertIs(retention, clients)
            seen.append(envelope)
            return control_reply(envelope["request_id"], "AVAILABILITY_PARKED", ok=False)

        projector = SlotProjector(fixture.manifest, fixture.bundle, dispatch=dispatch, retention=clients,
                                  authenticated_context={}, **fixture.sources())
        authority = ConformanceLiveAuthority(retention=clients)
        with command.isolated_deployment() as root:
            executable = root / "record"
            _write_fixture(executable, str(root / "spawn"))
            with patch.object(binding_module, "_retention_available", return_value=True):
                binding = _bind(_manifest(binding_module.sha256_of_file(executable)), executable)
            args = fixture.args(payload={"tape": "fixture"})
            call = controls.LiveCall(projector, authority, "host.behaviour.record", args, binding)
            with patch.object(authority, "verify", return_value=True):
                for face in ("cli", "mcp"):
                    with self.subTest(face=face), self.assertRaisesRegex(
                        controls.QualificationRejected, "authenticated default-on composition",
                    ):
                        call.invoke(face, clients)
            self.assertEqual(seen, [])

    def test_success_receipt_publication_is_exact_and_leaves_one_file(self):
        command = self.module("slot_host.qualify.__main__")
        with command.isolated_deployment() as root:
            target = Path(root) / "receipt.json"
            expected = {"schema": "rea.retention.qualification/v1", "controls": {"recovery": "pass"}}
            command.write_receipt(target, expected)
            self.assertEqual(json.loads(target.read_bytes()), expected)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["receipt.json", "run", "state"])


class QualificationGateTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("REA_RETENTION_SRC"):
            self.skipTest("owner-pending: T01 (REA_RETENTION_SRC is unset)")
        from slot_host.qualify import __main__, controls

        self.command, self.controls = __main__, controls

    def test_cli_and_mcp_default_to_live_local_retention(self):
        from slot_host.projections import cli
        from slot_host.projections.mcp_lane import SlotProjector
        from slot_host.qualify.workload import QualificationWorkload
        from slot_host.runtime.retention_deployment import load_local_retention

        pins, _ = self.command.local_open_profile()
        server = self.command.import_tools("retention_service.server.__main__")
        with self.command.isolated_deployment() as root:
            association = root / "state" / "viewpoint-retention" / "rea-associations.sqlite3"
            local = load_local_retention(association)
            with server.open_local_server(witness_module=pins["witness_module"]):
                plan = QualificationWorkload(root, local.clients, local.control_clients(root / "control.sqlite3"))
                original = plan.projectors[plan.primary]

                def dispatch(envelope, *, retention=None):
                    self.assertIsNotNone(retention, "the projected entry point must default to local retention")
                    self.assertEqual(retention.claim_pins(), local.clients.claim_pins())
                    self.assertEqual(retention.association_path, association)
                    self.assertTrue(retention.local_open)
                    self.assertIsNone(retention.runner.client.tls)
                    with patch.object(plan.primary, "ports", retention):
                        return plan.primary(envelope, retention=retention)

                host = {"dispatch": dispatch, "context": original.context,
                        "authenticated_context": original.context, "binding": original.binding,
                        "policy": original.policy, "registry": original.registry}
                row = next(row for row in original.bundle["router_rows"] if row["id"] == plan.record.route_id)
                argv = [json.dumps(plan.record.arguments) if token == "<json>" else token
                        for token in row["cli_argv_shape"]]
                out = StringIO()
                with redirect_stdout(out):
                    status = cli.main(argv, manifest=original.manifest, bundle=original.bundle, **host)
                self.assertEqual(status, 0, out.getvalue())
                reference = self.controls.inspect_reply(local.clients, json.loads(out.getvalue()), "record", "cli")
                candidate = plan.replay(reference.inspection["receipt_ref"])
                mcp = SlotProjector(original.manifest, original.bundle, **host)
                answer = mcp.call_tool("rea_behaviour_replay", candidate.arguments)
                self.assertFalse(answer["isError"])
                self.controls.inspect_reply(local.clients, answer["structuredContent"], "replay", "mcp")
                self.assertEqual(plan.launch_count(), 2)

    def test_cli_and_mcp_park_when_local_retention_is_unavailable(self):
        from slot_host.projections import cli
        from slot_host.projections.mcp_lane import SlotProjector, control_reply
        from slot_host.projections.tests.test_projections import Fixture

        fixture = Fixture()
        fixture.setUp()
        dispatches = []

        def dispatch(envelope, *, retention=None):
            dispatches.append(retention)
            return control_reply(envelope["request_id"], "AVAILABILITY_PARKED", ok=False)

        host = {"dispatch": dispatch, "authenticated_context": {}, **fixture.sources()}
        args = fixture.args(payload={"tape": "fixture"})
        row = fixture.route("host.behaviour.record")
        argv = [json.dumps(args) if token == "<json>" else token for token in row["cli_argv_shape"]]
        stdout, stderr = StringIO(), StringIO()
        with self.command.isolated_deployment():
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = cli.main(argv, manifest=fixture.manifest, bundle=fixture.bundle, **host)
            projector = SlotProjector(fixture.manifest, fixture.bundle, **host)
            mcp = projector.call_tool(row["mcp_name"], args)
            unauthenticated = SlotProjector(fixture.manifest, fixture.bundle, **(host | {"authenticated_context": None}))
            no_lease = unauthenticated.call_tool(row["mcp_name"], args)
        self.assertEqual(status, 16)
        self.assertTrue(mcp["isError"])
        for reply in (json.loads(stdout.getvalue()), mcp["structuredContent"]):
            with self.subTest(reply=reply):
                self.assertEqual(reply["code"], "LIVE_EXECUTION_PARKED")
                self.assertIn("Local-open retention is unavailable", reply["not_verified"][0])
        self.assertIn("Local-open retention is unavailable", stderr.getvalue())
        self.assertEqual(no_lease["structuredContent"]["code"], "NO_LEASE")
        self.assertEqual(dispatches, [])

    def test_nominal_module_main_verifies_real_cli_mcp_and_reopened_receipts(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "QUALIFICATION_RECEIPT.json"
            diagnostic = StringIO()
            with patch.object(self.command, "scope_properties", return_value={
                "memory.high": 6 << 30, "memory.max": 8 << 30, "memory.swap.max": 0,
            }), redirect_stderr(diagnostic):
                self.assertEqual(self.command.main(["--out", str(target)]), 0, diagnostic.getvalue())
            receipt = json.loads(target.read_bytes())
            self.assertEqual(list(Path(directory).iterdir()), [target])

        self.assertEqual(receipt["schema"], "rea.retention.qualification/v1")
        self.assertRegex(receipt["revisions"]["rea"], r"^[0-9a-f]{40}$")
        self.assertEqual(receipt["revisions"]["tools_frozen_sha256"][
            "attempt_evidence_record.v0.3.schema.json"], projection.V03_SHA)
        source = self.command.tools_source()
        expected = {path.relative_to(source).as_posix(): sha256(path.read_bytes()).hexdigest()
                    for path in (source / "retention_service").rglob("*.py")}
        self.assertIn("retention_service/transport/client.py", expected)
        self.assertIn("tools_source_sha256", receipt["revisions"])
        self.assertEqual(receipt["revisions"]["tools_source_sha256"], projection.record_digest(expected))
        controls = {name: check["evidence"] for name, check in receipt["controls"].items()}
        self.assertTrue(all(check["verdict"] == "pass" for check in receipt["controls"].values()))
        reference, candidate, comparison, denial = receipt["live_receipts"]
        refs = [row["receipt_ref"] for row in (reference, candidate, comparison, denial)]
        self.assertEqual(controls["recovery"]["inspector_receipts"], refs)
        self.assertNotEqual(controls["recovery"]["service_process"]["pid"], os.getpid())
        self.assertEqual(controls["recovery"]["start_retry"], controls["exact_start_retry"]["ack"])
        self.assertEqual(controls["recovery"]["finish_retries"], {
            "exact": controls["exact_finish_retry"]["ack"], "conflict_http": 409,
        })
        self.assertEqual(projection.record_digest(controls["recovery"]["manifest"]),
                         controls["recovery"]["manifest_sha256"])
        self.assertEqual(controls["reference_cli"]["face"], "cli")
        self.assertEqual(controls["candidate_mcp"]["face"], "mcp")
        self.assertEqual(controls["comparison_mcp"]["verdict"], "match")
        self.assertEqual(controls["conflicting_finish_retry"]["http"], 409)
        self.assertEqual(controls["conflicting_finish_retry"]["inspector_receipts"], refs)
        self.assertEqual(controls["deletion"]["inspector_receipts"], refs)
        self.assertEqual(controls["deletion"]["inspect_http"], 404)
        self.assertEqual(controls["deletion"]["receipt_http"], 404)
        self.assertEqual(controls["deletion"]["tombstone"]["record"]["attempt_id"],
                         controls["deletion"]["tombstone"]["attempt_id"])
        self.assertEqual(set(controls["deletion"]["surviving_releases"]),
                         {row["attempt_id"] for row in (reference, candidate, comparison)})
        self.assertEqual({name for name in controls["cross_tenant_denial"]}, {
            "primary_to_control", "primary_on_control_tenant", "denial_to_primary", "denial_on_primary_tenant",
        })
        for denial_check in controls["cross_tenant_denial"].values():
            self.assertEqual(denial_check, dict.fromkeys(("receipt_http", "inspect_http", "artifact_http",
                                                          "object_http"), 404))
        pending = receipt["unresolved_claim"]
        self.assertEqual(pending["state"], "unknown")
        self.assertTrue(pending["launch_claimed"])
        self.assertEqual(projection.record_digest(pending["start"]), pending["start_sha256"])
        self.assertNotIn(pending["attempt_id"], [row["attempt_id"] for row in receipt["live_receipts"]])
        self.assertEqual(controls["lost_claim_no_spawn"]["reply"], "UNCERTAIN_OUTCOME")
        self.assertEqual(controls["lost_claim_retry"]["reply"], "UNCERTAIN_OUTCOME")
        self.assertEqual(controls["lost_claim_retry"]["launch_delta"], 0)

        for row in receipt["live_receipts"]:
            self.assertRegex(row["receipt_ref"], r"^rcp_[0-9a-f]{32}$")
            self.assertRegex(row["artifact_ref"], r"^art_[0-9a-f]{32}$")
            self.assertEqual(projection.record_digest(row["start"]), row["start_sha256"])
            self.assertEqual(projection.record_digest(row["terminal"]), row["terminal_sha256"])
            self.assertEqual(row["terminal"]["start_sha256"], row["start_sha256"])
            self.assertEqual(projection.record_digest(row["index"]), row["artifact_sha256"])
            self.assertEqual(row["terminal"]["artifact"]["sha256"], row["artifact_sha256"])
            self.assertEqual(projection.record_digest(row["lineage"]), row["lineage_sha256"])
            self.assertEqual(row["outer_receipt"]["shared_receipt_adapter"], {
                "profile": projection.PROFILE_SCHEMA_V1, "binding_state": "bound",
                "shared_schema_id": projection.V03_ID, "shared_schema_sha256": projection.V03_SHA,
                "live_use_permitted": True, "shared_receipt_ref": row["receipt_ref"],
            })
            for entry in row["objects"]:
                ref = entry["object_ref"]
                self.assertRegex(ref, r"^obj_[0-9a-f]{32}$")
                body = b64decode(row["object_bytes_b64"][ref], validate=True)
                self.assertEqual((len(body), sha256(body).hexdigest()), (entry["bytes"], entry["sha256"]))
                if entry["role"] == "rea_profile":
                    inner = json.loads(body)
                    self.assertEqual(inner["shared_receipt_adapter"]["live_use_permitted"], False)
                    self.assertEqual({k: v for k, v in inner.items() if k != "shared_receipt_adapter"},
                                     {k: v for k, v in row["outer_receipt"].items()
                                      if k != "shared_receipt_adapter"})
        lineage = {item["side"]: item for item in comparison["lineage"]}
        for side, source in (("reference", reference), ("candidate", candidate)):
            self.assertEqual(lineage[side]["source_attempt_id"], source["attempt_id"])
            self.assertEqual(lineage[side]["source_terminal_sha256"], source["terminal_sha256"])
            self.assertEqual(lineage[side]["source_sha256"], source["artifact_sha256"])
        self.assertEqual(candidate["outer_receipt"]["episode"]["parent_episode_id"],
                         reference["outer_receipt"]["episode"]["episode_id"])
        self.assertNotEqual(reference["tenant"], denial["tenant"])

    def test_scope_requires_user_service_and_explicit_memory_limits(self):
        uid = os.geteuid()
        expected = {"memory.high": str(6 << 30), "memory.max": str(8 << 30), "memory.swap.max": "0"}
        for membership, memory, reason in (
            ("/system.slice/run-test.scope", expected, "dedicated systemd user scope"),
            (f"/user.slice/user-{uid}.slice/user@{uid}.service/run-test.scope",
             expected | {"memory.max": "max"}, "prescribed memory properties"),
        ):
            def read(path):
                return "0::" + membership if path.name == "cgroup" else memory[path.name]

            with self.subTest(reason=reason), patch.object(Path, "read_text", read), self.assertRaisesRegex(
                self.controls.QualificationRejected, reason,
            ):
                self.command.scope_properties()

    def test_readiness_rejects_identity_pin_and_denial_mismatches(self):
        inspector = SimpleNamespace(principal="local/writer/inspector", client=Mock())
        read = Mock()
        inspector.client.session.return_value = nullcontext(read)
        clients = SimpleNamespace(inspector=inspector, tenant="local/writer", policy_revision="policy/v1",
                                  acl_revision="acl/v1", ready=Mock(return_value=True))
        local = SimpleNamespace(clients=clients, check_denial_endpoint=Mock(return_value=True))
        reply = {"challenge": "a" * 64, "verb": "inspect", "principal": inspector.principal,
                 "tenant": clients.tenant, "policy_revision": clients.policy_revision,
                 "acl_revision": clients.acl_revision, "record_pin": projection.V03_SHA,
                 "index_pin": projection.INDEX_SHA}
        for mutation, denial, reason in (
            ({"principal": "wrong"}, True, "authenticated readiness bindings differ"),
            ({"index_pin": "0" * 64}, True, "frozen record and index pins differ"),
            ({}, False, "denial_endpoint"),
        ):
            read.ready.return_value = reply | mutation
            local.check_denial_endpoint.return_value = denial
            with self.subTest(reason=reason), self.assertRaisesRegex(self.controls.QualificationRejected, reason):
                self.command.verify_readiness(local)
        clients.ready.assert_not_called()

    def test_nested_runtime_receipt_refusals_are_specific(self):
        request = "req-qualify-control"
        reference = "rcp_" + "a" * 32
        profile = {"shared_receipt_adapter": {
            "profile": projection.PROFILE_SCHEMA_V1, "binding_state": "bound",
            "shared_schema_id": projection.V03_ID, "shared_schema_sha256": projection.V03_SHA,
            "live_use_permitted": False,
        }}
        from slot_host.runtime.receipts import RetainedReadback

        clients = SimpleNamespace(profile_schema=projection.PROFILE_SCHEMA_V1, profile_sha256="b" * 64)
        admitted = {"operation": "replay", "request_id": request, "profile_schema": clients.profile_schema,
                    "profile_schema_sha256": clients.profile_sha256}
        retained = RetainedReadback({"admission": admitted, "terminal": {"state": "observed"},
                                    "receipt_ref": reference}, b"", {}, profile)
        clients.read_source = Mock(return_value=retained)
        adapter = profile["shared_receipt_adapter"] | {"live_use_permitted": True, "shared_receipt_ref": reference}
        wrapper = {"evidence_accepted": True, "receipt": {"shared_receipt_adapter": adapter}}
        payload = {"ok": True, "request_id": request, "result": wrapper}
        reply = {"ok": True, "code": "OK", "request_id": request,
                 "result": {"kind": "host_result", "payload": payload, "outside_capsule": True}}
        self.assertIs(self.controls.inspect_reply(clients, reply, "replay", "candidate"), retained)
        for mutation, reason in (
            ({"live_use_permitted": False}, "did not authorize the published receipt"),
            ({"shared_receipt_ref": "art_" + "a" * 32}, "not a Tools receipt"),
            ({"binding_state": "pending"}, "exact REA profile"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(self.controls.QualificationRejected, reason):
                wrapper["receipt"] = {"shared_receipt_adapter": adapter | mutation}
                self.controls.inspect_reply(clients, reply, "replay", "candidate")
        with self.assertRaisesRegex(self.controls.QualificationRejected, "live operation was refused"):
            self.controls.inspect_reply(clients, reply | {"ok": False}, "replay", "candidate")

    def test_exact_finish_and_inspector_readback_reject_independent_mutations(self):
        view = {"attempt_id": "a", "start_sha256": "b", "terminal_sha256": "c", "receipt_ref": "rcp_a",
                "terminal": {"artifact": {"observation_id": "original"}}}
        retained = SimpleNamespace(inspection=view, artifact=b"artifact")
        writer = Mock()
        clients = SimpleNamespace(runner=SimpleNamespace(client=Mock()), tenant="rea-test",
                                  read_source=Mock(return_value=retained))
        clients.runner.client.session.return_value = nullcontext(writer)
        ack = {key: view[key] for key in ("attempt_id", "start_sha256", "terminal_sha256")}
        remote = self.command.import_tools("retention_service.transport.client").RemoteError
        writer.finish.side_effect = [ack, remote(409)]
        self.assertEqual(self.controls.finish_retries(clients, retained), {"exact": ack, "conflict_http": 409})
        writer.finish.side_effect = [ack | {"terminal_sha256": "wrong"}]
        with self.assertRaisesRegex(self.controls.QualificationRejected, "exact_finish_retry"):
            self.controls.finish_retries(clients, retained)
        writer.finish.side_effect = [ack, ack]
        with self.assertRaisesRegex(self.controls.QualificationRejected, "conflicting_finish_retry"):
            self.controls.finish_retries(clients, retained)
        clients.read_source.return_value = SimpleNamespace(inspection=view, artifact=b"changed")
        with self.assertRaisesRegex(self.controls.QualificationRejected, "inspector byte graph changed"):
            self.controls.unchanged(clients, retained)

    def test_sweep_requires_inspect_denial_and_releases_survivors_on_real_clock(self):
        fixed = datetime(2026, 11, 24, tzinfo=timezone.utc)
        expiry = fixed + timedelta(days=30)
        store = SimpleNamespace(clock=lambda: fixed, sweep=Mock(),
                                policy=SimpleNamespace(purge_window=timedelta(days=1)))
        target = SimpleNamespace(inspection={"attempt_id": "target", "receipt_ref": "ref"})
        survivor = SimpleNamespace(inspection={"attempt_id": "survivor"})
        writer, reader = Mock(), Mock()
        release_times = {}
        writer.hold.side_effect = lambda a, _name, release=False: release_times.update(
            {a: store.clock()} if release else {},
        )
        ports = SimpleNamespace(tenant="test", runner=SimpleNamespace(client=Mock()),
                                inspector=SimpleNamespace(client=Mock()))
        ports.runner.client.session.return_value = nullcontext(writer)
        ports.inspector.client.session.return_value = nullcontext(reader)
        remote = self.command.import_tools("retention_service.transport.client").RemoteError
        reader.receipt.side_effect = [target.inspection, remote(404)]
        reader.inspect.return_value = target.inspection
        with patch.object(self.controls, "snapshot", side_effect=[
            {"attempts": [{"attempt_id": "target", "holds": ["qualification"], "expires_at": expiry.isoformat()}]},
            {"items": [{"attempt_id": "target", "kind": "tombstone"}]},
        ]), self.assertRaisesRegex(self.controls.QualificationRejected, "deletion: request unexpectedly succeeded"):
            self.controls.deletion_control(store, ports, (survivor,), target)
        self.assertEqual(release_times, {"target": expiry + timedelta(days=1), "survivor": fixed})

    def test_hold_rejects_absent_attempt_and_survival_failure(self):
        target = SimpleNamespace(inspection={"attempt_id": "target", "receipt_ref": "rcp_target"})
        store = SimpleNamespace(clock=lambda: datetime.now(timezone.utc), sweep=Mock(),
                                policy=SimpleNamespace(purge_window=timedelta(days=1)))
        writer, reader = Mock(), Mock()
        clients = SimpleNamespace(tenant="test", runner=SimpleNamespace(client=Mock()),
                                  inspector=SimpleNamespace(client=Mock()))
        clients.runner.client.session.return_value = nullcontext(writer)
        clients.inspector.client.session.return_value = nullcontext(reader)
        for manifest, message in (
            ({"attempts": []}, "hold: writer hold is absent from recovery"),
            ({"attempts": [{"attempt_id": "target", "holds": ["qualification"],
                            "expires_at": store.clock().isoformat()}]}, "hold: held evidence expired"),
        ):
            with self.subTest(message=message), patch.object(self.controls, "snapshot", return_value=manifest), \
                    self.assertRaisesRegex(self.controls.QualificationRejected, message):
                self.controls.deletion_control(store, clients, (), target)

    def test_lost_claim_rejects_spawn_and_unresolved_artifact_disclosure(self):
        start = {"attempt_id": "a", "external_process": {"request_id": "r"}}
        data = projection.record_bytes(start)
        writer, reader = Mock(), Mock()
        clients = SimpleNamespace(tenant="test", runner=SimpleNamespace(client=Mock()),
                                  inspector=SimpleNamespace(client=Mock()))
        clients.runner.client.session.return_value = nullcontext(writer)
        clients.inspector.client.session.return_value = nullcontext(reader)
        writer.recovery_item.return_value = SimpleNamespace(data=data)
        reader.inspect.return_value = {
            "state": "unknown", "launch_claimed": True, "terminal": None, "receipt_ref": None,
            "start": start, "artifact_ref": "art_leaked",
        }
        call = SimpleNamespace(arguments={"request_id": "r"},
                               invoke=Mock(return_value={"ok": False, "code": "UNCERTAIN_OUTCOME"}))

        def armed():
            return nullcontext(lambda: True)

        inventory = {"items": [{"kind": "start", "item": "a", "sha256": sha256(data).hexdigest(),
                               "bytes": len(data)}]}
        with patch.object(self.controls, "snapshot", return_value=inventory):
            with self.assertRaisesRegex(self.controls.QualificationRejected, "did not suppress launch"):
                self.controls.verify_lost_claim(call, clients, armed, iter((0, 1)).__next__)
            with self.assertRaisesRegex(self.controls.QualificationRejected, "lost claim created a terminal"):
                self.controls.verify_lost_claim(call, clients, armed, lambda: 0)

    def test_comparison_rejects_swapped_retained_start_and_unexpected_mismatch(self):
        reference, candidate = (SimpleNamespace(inspection={"attempt_id": i}) for i in ("a", "b"))
        pair = {"reference_attempt_id": "a", "candidate_attempt_id": "b"}
        for start, verdict in (
            ({"reference_attempt_id": "b", "candidate_attempt_id": "a"}, "match"),
            (pair, "mismatch"),
        ):
            result = SimpleNamespace(inspection={"admission": pair, "start": start | {"operation": "compare"},
                                                 "terminal": {"verdict": {"result": verdict}}})
            with self.subTest(verdict=verdict, start=start), self.assertRaisesRegex(
                self.controls.QualificationRejected, "ordered sources and the expected match",
            ):
                self.controls.check_comparison(reference, candidate, result)

    def test_recovery_rejects_digest_mismatch_on_a_later_inventory_page(self):
        good = b"expected"
        item = {"item": "obj_item", "bytes": len(good), "sha256": sha256(good).hexdigest()}
        manifest = projection.record_bytes({"tenant": "test", "items": [item]})
        manifest_digest = sha256(manifest).hexdigest()
        root = {"item": "obj_manifest", "bytes": len(manifest), "sha256": manifest_digest}
        ident = {"snapshot": "snap", "manifest_item": "obj_manifest", "manifest_sha256": manifest_digest}
        writer = Mock(tenant="test")
        writer.recovery.side_effect = [ident | {"items": [root], "next": "cursor"},
                                      ident | {"items": [item], "next": None}]
        writer.recovery_item.side_effect = lambda ref, **_: SimpleNamespace(
            offset=0, total_bytes=root["bytes"] if ref == root["item"] else item["bytes"],
            data=manifest if ref == root["item"] else b"tampered",
        )
        with self.assertRaisesRegex(self.controls.QualificationRejected, "item bytes differ from the inventory"):
            self.controls.snapshot(writer)
        self.assertEqual(writer.recovery.call_args.kwargs, {})
        self.assertEqual(writer.recovery.call_args.args, ({"limit": 100, "cursor": "cursor"},))

    def test_live_witness_rejects_foreign_output_even_with_matching_bytes(self):
        capture = retention_witness.CredentialCapture()
        try:
            capture.pid = os.getpid() + 1
            capture.writer.sendall(b"same bytes")
            with self.assertRaisesRegex(OSError, "stdout credentials differ"):
                capture.read_chunk(64)
            self.assertEqual(capture.digest.hexdigest(), sha256(b"").hexdigest())
        finally:
            capture.close()


    def test_cross_tenant_control_rejects_an_unexpected_reverse_read(self):
        remote = self.command.import_tools("retention_service.transport.client").RemoteError
        for method in ("inspect", "artifact", "object_page"):
            with self.subTest(method=method):
                readers = [Mock(**{f"{verb}.side_effect": remote(404)
                                   for verb in ("inspect", "receipt", "artifact", "object_page")})
                           for _ in range(2)]
                leaked = getattr(readers[0], method)
                leaked.side_effect = None
                leaked.return_value = b"private evidence"
                primary, control = (
                    SimpleNamespace(tenant=tenant, inspector=SimpleNamespace(client=Mock(
                        **{"session.return_value": nullcontext(reader)},
                    ))) for tenant, reader in zip(("primary", "control"), readers, strict=True)
                )
                ref, own = (SimpleNamespace(inspection={
                    "attempt_id": name, "receipt_ref": "rcp_" + name, "artifact_ref": "art_" + name,
                    "terminal": {"artifact": {"bytes": 5, "sha256": "a" * 64}},
                }, index={"objects": [{"object_ref": "obj_" + name}]}) for name in ("primary", "control"))
                with self.assertRaisesRegex(self.controls.QualificationRejected, "request unexpectedly succeeded"):
                    self.controls.cross_tenant_denials(primary, control, ref, own)
                if method == "inspect":
                    leaked.assert_called_once_with("control")
                elif method == "artifact":
                    leaked.assert_called_once_with("control", "art_control", length=5, digest="a" * 64)
                else:
                    leaked.assert_called_once_with("control", "obj_control")

    def test_reopen_rejects_missing_historical_proof_instead_of_publishing(self):
        restart = getattr(self.command, "reopen_service", None)

        @contextmanager
        def remove_proof(root, module, local):
            proof = next(path for path in local.clients.custody.directory.glob("*.finish.json")
                         if json.loads(path.read_bytes())["binding"]["tenant"] == local.denial_tenant)
            proof.unlink()
            with restart(root, module, local) as child:
                yield child

        with TemporaryDirectory() as directory:
            target = Path(directory) / "QUALIFICATION_RECEIPT.json"
            diagnostic = StringIO()
            with patch.object(self.command, "scope_properties", return_value={}), redirect_stderr(diagnostic), \
                    patch.object(self.command, "reopen_service", remove_proof, create=True):
                self.assertEqual(self.command.main(["--out", str(target)]), 1)
            self.assertIn("readback: published inspector byte graph is unavailable", diagnostic.getvalue())
            self.assertFalse(target.exists())


class QualificationSkipTests(unittest.TestCase):
    def test_missing_tools_export_names_pending_owner(self):
        with patch.dict(os.environ, {"REA_RETENTION_SRC": ""}), self.assertRaisesRegex(
            unittest.SkipTest, r"^owner-pending: T01 \(REA_RETENTION_SRC is unset\)$",
        ):
            QualificationGateTests().setUp()
