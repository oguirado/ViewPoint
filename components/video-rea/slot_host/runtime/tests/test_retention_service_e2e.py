# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise REA readiness and admission with the pinned Tools server."""

from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from slot_host.compare import tools as comparison_tools
from slot_host.compare.tests.test_comparators import event_comparison, event_trace, timeline_side
from slot_host.runtime import binding, evidence_projection as projection, tools
from slot_host.runtime.tests.retention_flow_fixtures import FixtureObserver, install_capture
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.process_lifecycle import proc_starttime, terminate_only
from slot_host.runtime.tests.test_retention_integration import (
    RetentionClients, RetentionIdentity, _RequestFixture, _hash, _replace_clients, _supports_flow,
)


@unittest.skipUnless("REA_RETENTION_SRC" in os.environ, "owner-pending: T01 (REA_RETENTION_SRC is unset)")
class RetentionServiceTests(_RequestFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(os.environ["REA_RETENTION_SRC"]).resolve(strict=True)
        if not (cls.source / "retention_service" / "server" / "__main__.py").is_file():
            raise ValueError("REA_RETENTION_SRC must identify the pinned Tools export")
        cls.enterClassContext(patch.object(sys, "path", [str(cls.source), *sys.path]))
        with patch.object(sys, "dont_write_bytecode", True):
            cls.service = importlib.import_module("retention_service.server.test_e2e")
            if not Path(cls.service.__file__).resolve().is_relative_to(cls.source):
                raise ValueError("Tools fixtures must be loaded from REA_RETENTION_SRC")
            cls.contract = importlib.import_module("retention_service.contracts.validation")
            cls.wire = importlib.import_module("retention_service.contracts.wire")
            cls.client_module = importlib.import_module("retention_service.transport.client")

        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        pinned = Path(__file__).with_name("fixtures_v03")
        frozen = root / "frozen"
        frozen.mkdir()
        lines = []
        for _, filename, digest in cls.contract.FROZEN_PINS:
            shutil.copyfile(pinned / filename, frozen / filename)
            lines.append(f"{digest}  {filename}\n")
        sums = "".join(lines).encode("ascii")
        if _hash(sums) != cls.contract.FREEZE_LIST_SHA256:
            raise ValueError("test schema manifest differs from the pinned Tools manifest")
        (frozen / "FROZEN_SHA256SUMS").write_bytes(sums)
        cls.enterClassContext(patch.dict(os.environ, {"RETENTION_FROZEN_DIR": str(frozen)}))
        cls.service.SyntheticREA.setUpClass()
        cls.addClassCleanup(cls.service.SyntheticREA.doClassCleanups)

    def setUp(self):
        self.fixture(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(patch.object(tools, "_REQUEST_STORE", {}))
        self.server_fixture = self.service.SyntheticREA()
        self.addCleanup(self.server_fixture.doCleanups)
        self.server_fixture.setUp()

    @contextmanager
    def server(self, *, witness_env=None):
        fixture = self.server_fixture
        child_env = {
            "PATH": os.defpath, "PYTHONPATH": str(self.source),
            "RETENTION_FROZEN_DIR": fixture.frozen_dir,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if witness_env is not None:
            child_env.update(witness_env)
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "retention_service.server", "--config",
             fixture.config_file, "--approval", fixture.approval_file],
            cwd=fixture.root, env=child_env, stdout=subprocess.DEVNULL,
        )
        identity = proc_starttime(process.pid)
        try:
            deadline = time.monotonic() + 10
            while True:
                self.assertIsNone(process.poll(), "Tools server exited before readiness")
                self.assertLess(time.monotonic(), deadline, "Tools server did not become ready")
                if fixture.unix.exists():
                    try:
                        fixture.ready("unix", "authority")
                    except OSError:
                        pass
                    else:
                        break
                time.sleep(0.02)
            yield
        finally:
            self.assertTrue(terminate_only(process.pid, process, 5, identity),
                            "Tools test server was not reaped")

    def exercise(self, transport):
        fixture = self.server_fixture
        identities = {
            role: RetentionIdentity(client=fixture.client(transport, role), principal=self.service.ROLES[role][0])
            for role in ("authority", "runner", "inspector")
        }
        clients = RetentionClients(
            tenant=self.service.TENANT, policy_revision=fixture.config["policy_revision"],
            acl_revision=fixture.config["acl_revision"], profile_schema="fixture.profile/v1",
            profile_sha256=self.service.Fixture().admission("1" * 32)[0]["profile_schema_sha256"],
            association_path=self.path, admission_bytes=self.wire.admission_bytes, **identities,
        )
        with self.server():
            self.assertEqual(self.inspect_slot(clients)["availability"], "admitted")
            self.assertTrue(self.invoke(clients, dry_run=True, request_id="req-real-retention-preview")["ok"])
            self.assertFalse(self.path.exists())
            with self.assertRaises(tools._ControlError) as parked:
                self.invoke(clients, request_id="req-real-retention")
            self.assertEqual(parked.exception.code, LIVE_EXECUTION_PARKED)
            self.assertFalse(self.marker.exists())

            # Resolve the committed admission from the durable REA mapping.
            with closing(sqlite3.connect(self.path)) as db:
                raw, confirmed = db.execute("SELECT admission, confirmed FROM retention_requests").fetchone()
            self.assertEqual(confirmed, 1)
            admission = json.loads(raw)
            with clients.authority.client.session(clients.tenant) as session:
                self.assertEqual(session.admit(admission), admission)
                with self.assertRaises(self.client_module.RemoteError) as denied:
                    changed = admission | {"source_url": "https://fixture.example/changed"}
                    changed["admission_sha256"] = _hash(
                        self.wire.admission_bytes(changed, fixture.validator),
                    )
                    session.admit(changed)
                self.assertEqual(denied.exception.status, 409)

            # Mutate the actual server reply before the real Session.ready
            # validates its locally generated challenge.
            exchange = self.client_module.Session._json

            def stale(session, method, path, form, **kwargs):
                reply = exchange(session, method, path, form, **kwargs)
                if form == "Ready" and reply["verb"] == "claim":
                    reply["challenge"] = "0" * 64
                return reply

            with patch.object(self.client_module.Session, "_json", stale):
                with self.assertRaises(binding.BindingRejected) as unavailable:
                    self.bind(clients)
            self.assertEqual(unavailable.exception.reason, "profile_not_admitted")

    def test_tools_server_unix_readiness_and_admission(self):
        self.exercise("unix")

    def test_tools_server_tcp_tls_readiness_and_admission(self):
        self.exercise("tcp_tls")

    def exercise_retained_flow(self, transport):
        fixture = self.server_fixture
        raw_fixture = install_capture(self, "offline capture \u03c0")
        witness_dir = self.root / "witness"
        witness_dir.mkdir()
        profile_path = Path(projection.__file__).resolve().parents[2] / "contracts" / "evidence" / "rea.slot_evidence_profile_v1.schema.json"
        profile_raw = profile_path.read_bytes()
        pin = _hash(profile_raw)
        recording = b"\x00\xffret2 test recording\x01"
        events = event_trace()
        event_octets = projection.jcs_bytes(events)
        observer = FixtureObserver(
            raw_fixture, recordings={"reference": recording},
            media={"reference": {"recording_sha256": _hash(recording), "container": "mkv", "codec": "ffv1",
                                 "clock_source": "pts", "frames": [],
                                 "frames_unavailable_reason": "disposable octet fixture"}},
            timeline={"reference": {"clock_source": "monotonic", "origin": "episode_start",
                                    "canonicalization": "rea.timeline_digest/v1", "events": events,
                                    "time_base": {"num": 1, "den": 2000},
                                    "timeline_sha256": _hash(event_octets)}},
        )
        options = {}
        if _supports_flow(RetentionClients):
            from slot_host.runtime.retention_witness import pinned_profiles
            options["validate_profile"] = pinned_profiles({(projection.PROFILE_SCHEMA_V1, pin): profile_raw})
            options["observe_terminal"] = observer
        clients = RetentionClients(
            tenant=self.service.TENANT, policy_revision=fixture.config["policy_revision"],
            acl_revision=fixture.config["acl_revision"], profile_schema=projection.PROFILE_SCHEMA_V1,
            profile_sha256=pin, association_path=self.path, admission_bytes=self.wire.admission_bytes,
            **{role: RetentionIdentity(client=fixture.client(transport, role), principal=self.service.ROLES[role][0])
               for role in ("authority", "runner", "inspector")}, **options,
        )
        other = _replace_clients(
            clients, tenant=self.service.OTHER_TENANT, association_path=self.root / "other.sqlite3",
            authority=RetentionIdentity(client=fixture.client(transport, "other-authority"),
                                        principal=self.service.OTHER_AUTHORITY),
            inspector=RetentionIdentity(client=fixture.client(transport, "denial"), principal=self.service.DENIAL),
        )
        approval = fixture.approval
        if options:
            approval = dict(approval, witness_module="slot_host.runtime.retention_witness")
        fixture.write(fixture.approval_file, approval)
        witness_env = {
            "PYTHONPATH": os.pathsep.join((str(self.source), str(profile_path.parents[2]))),
            "REA_RETENTION_SRC": str(self.source),
            "REA_RETENTION_OFFLINE_WITNESS_DIR": str(witness_dir),
            "REA_RETENTION_OFFLINE_PROFILE_SCHEMA": str(profile_path),
            "REA_RETENTION_OFFLINE_PROFILE_SHA256": pin,
        }
        session = self.client_module.Session
        original_start, original_claim, original_finish = session.start, session.claim, session.finish
        admissions, starts, finishes, claims = {}, {}, {}, []
        faults = {"claim": False, "finish": True}

        def write_proof(kind, tenant, attempt, digest):
            admission = admissions[tenant, attempt]
            witness_binding = {
                "kind": kind, "tenant": tenant, "principal": admission["executor_id"],
                "admission_sha256": admission["admission_sha256"], "record_sha256": digest,
            }
            proof = {"binding": witness_binding, "evidence": {"offline_fixture": True, "event": kind}}
            target = witness_dir / (projection.record_digest(witness_binding) + ".json")
            if target.exists():
                self.assertEqual(target.read_bytes(), projection.record_bytes(proof))
            else:
                target.write_bytes(projection.record_bytes(proof))

        original_admit = session.admit

        def admit(client, admission):
            result = original_admit(client, admission)
            admissions[client.tenant, admission["attempt_id"]] = result
            return result

        def start(client, attempt, raw, *, request_sha256):
            command = json.loads(raw).get("command")
            if command is not None:
                self.assertTrue(Path(command["arguments"][0]).exists())
                self.assertTrue(Path(command["cwd"]).is_dir())
            write_proof("start", client.tenant, attempt, _hash(raw))
            result = original_start(client, attempt, raw, request_sha256=request_sha256)
            starts[client.tenant, attempt] = raw
            return result

        def claim(client, ack):
            self.assertIn((client.tenant, ack["attempt_id"]), starts)
            write_proof("claim", client.tenant, ack["attempt_id"], ack["start_sha256"])
            result = original_claim(client, ack)
            claims.append((client.tenant, ack["attempt_id"]))
            if faults["claim"]:
                raise OSError("lost test claim acknowledgement after durable commit")
            return result

        def finish(client, attempt, terminal, artifact, *, start_sha256):
            if admissions[client.tenant, attempt]["operation"] != "compare":
                self.assertIn((client.tenant, attempt), claims)
            write_proof("finish", client.tenant, attempt, _hash(terminal))
            result = original_finish(client, attempt, terminal, artifact, start_sha256=start_sha256)
            finishes[client.tenant, attempt] = (terminal, artifact, start_sha256)
            if faults["finish"]:
                faults["finish"] = False
                raise OSError("lost test finish acknowledgement after durable commit")
            return result

        def before_spawn(argv, kwargs):
            tenant, attempt = claims[-1]
            command = json.loads(starts[tenant, attempt])["command"]
            self.assertEqual(command["arguments"], argv)
            self.assertEqual(command["cwd"], kwargs["cwd"])
            self.assertEqual(command["env_sha256"], projection.env_sha256(kwargs["env"]))

        observer.start_check = before_spawn
        with self.server(witness_env=witness_env), observer.track(), patch.multiple(
            session, admit=admit, start=start, claim=claim, finish=finish,
        ):
            try:
                preview = self.invoke(clients, dry_run=True, request_id="req-ret2-preview")
            except tools._ControlError as exc:
                self.fail(f"retained-flow preview was refused: {exc.code}")
            self.assertTrue(preview["ok"])
            self.assertFalse(self.path.exists())
            try:
                result = self.invoke(clients, request_id="req-ret2-positive")["result"]
            except tools._ControlError as exc:
                self.fail(f"configured offline retention refused the run: {exc.code}")
            self.assertEqual(self.marker.read_bytes(), b"1")
            self.assertTrue(result["evidence_accepted"], result["custody"])
            attempt = result["receipt"]["attempt"]["attempt_id"]
            ref = result["receipt"]["shared_receipt_adapter"]["shared_receipt_ref"]
            terminal, artifact, start_sha = finishes[clients.tenant, attempt]
            with clients.runner.client.session(clients.tenant) as writer:
                self.assertEqual(
                    writer.finish(attempt, terminal, artifact, start_sha256=start_sha),
                    {"attempt_id": attempt, "start_sha256": start_sha, "terminal_sha256": _hash(terminal)},
                )
                changed = json.loads(terminal)
                changed["artifact"]["observation_id"] = "fixture/conflicting-observation"
                with self.assertRaises(self.client_module.RemoteError) as conflict:
                    writer.finish(attempt, projection.record_bytes(changed), artifact, start_sha256=start_sha)
                self.assertEqual(conflict.exception.status, 409)
            with clients.inspector.client.session(clients.tenant) as inspector:
                inspected = inspector.inspect(attempt)
                self.assertEqual(inspected, inspector.receipt(ref))
                index = json.loads(artifact)
                observation = next(item for item in index["objects"] if item["role"] == "observation")
                self.assertEqual(inspector.object_page(attempt, observation["object_ref"]).data, raw_fixture)
                for role, raw in (("recording", recording), ("timeline_events", event_octets)):
                    entry = next(item for item in index["objects"] if item["role"] == role)
                    self.assertEqual(inspector.object_page(attempt, entry["object_ref"]).data, raw)
                self.assertEqual(inspected["start_sha256"], result["custody"]["start_sha256"])

            candidate_events = [event | {"t_ms": event["t_ms"] + 1, "t_ticks": event["t_ms"] + 1}
                                for event in events]
            observer.timeline = {"reference": observer.timeline["reference"] | {
                "events": candidate_events, "time_base": {"num": 1, "den": 1000},
                "timeline_sha256": _hash(projection.jcs_bytes(candidate_events)),
            }}
            candidate = self.invoke(clients, request_id="req-ret2-candidate")["result"]
            self.assertTrue(candidate["evidence_accepted"], candidate["custody"])
            comparison = self.exercise_comparison(clients, result, candidate, observer, starts)
            self.assertNotIn((clients.tenant, comparison), claims)
            self.assertIn((clients.tenant, comparison), finishes)

            own = self.invoke(other, request_id="req-ret2-own-tenant")["result"]["receipt"]
            self.assertEqual(self.marker.read_bytes(), b"111")
            with other.inspector.client.session(other.tenant) as denial:
                self.assertEqual(denial.receipt(own["shared_receipt_adapter"]["shared_receipt_ref"])["state"], "committed")
                for read in (lambda: denial.inspect(attempt), lambda: denial.receipt(ref)):
                    with self.assertRaises(self.client_module.RemoteError) as blocked:
                        read()
                    self.assertEqual(blocked.exception.status, 404)

            faults["claim"] = True
            lost = self.invoke(clients, request_id="req-ret2-lost-claim")["result"]
            lost_attempt = lost["receipt"]["attempt"]["attempt_id"]
            self.assertEqual(lost["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
            self.assertEqual(lost["custody"]["retirement"], "unknown")
            self.assertFalse(lost["evidence_accepted"])
            self.assertNotIn((clients.tenant, lost_attempt), finishes)
            with clients.inspector.client.session(clients.tenant) as inspector:
                inspected = inspector.inspect(lost_attempt)
            self.assertEqual(inspected["state"], "unknown")
            self.assertTrue(inspected["launch_claimed"])
            self.assertEqual(inspected["start_sha256"], lost["custody"]["start_sha256"])
            with fixture.client(transport, "recovery").session(clients.tenant) as recovery:
                page = recovery.recovery({"limit": 10})
                manifest_raw = recovery.recovery_item(page["manifest_item"]).data
                self.assertEqual(_hash(manifest_raw), page["manifest_sha256"])
                manifest = json.loads(manifest_raw)
                pending = next(item for item in manifest["attempts"] if item["attempt_id"] == lost_attempt)
                self.assertEqual(pending["disposition"], "unknown")
                start_item = next(item for item in manifest["items"]
                                  if item["attempt_id"] == lost_attempt and item["kind"] == "start")
                self.assertEqual(recovery.recovery_item(start_item["item"]).data,
                                 starts[clients.tenant, lost_attempt])
            with self.assertRaises(tools._ControlError) as retry:
                self.invoke(clients, request_id="req-ret2-lost-claim")
            self.assertEqual(retry.exception.code, "UNCERTAIN_OUTCOME")
            self.assertEqual(self.marker.read_bytes(), b"111")
            self.assertEqual(claims.count((clients.tenant, lost_attempt)), 1)

    def exercise_comparison(self, clients, reference, candidate, observer, starts):
        fixture = self.server_fixture
        receipts = (reference["receipt"], candidate["receipt"])
        attempts = tuple(record["attempt"]["attempt_id"] for record in receipts)
        refs = tuple(record["shared_receipt_adapter"]["shared_receipt_ref"] for record in receipts)
        destination = "cd" * 16
        acl = json.loads(fixture.acl.read_bytes())
        acl["comparisons"].append({
            "writer": clients.runner.principal, "inspector": clients.inspector.principal,
            "tenant": clients.tenant, "destination": {"attempt_id": destination},
            "reference": {"attempt_id": attempts[0]}, "candidate": {"attempt_id": attempts[1]},
        })
        replacement = fixture.acl.with_suffix(".ret2")
        fixture.write(replacement, acl)
        replacement.replace(fixture.acl)

        payload = {"reference_ref": refs[0], "candidate_ref": refs[1], "policy_ref": "policy:offline"}
        request = "req-ret2-compare"
        tapes = [
            item["shared_records"]["terminal"]["timeline"]["reference"]
            for item in (reference, candidate)
        ]
        timeline_records = []
        with clients.inspector.client.session(clients.tenant) as inspector:
            for attempt, timeline in zip(attempts, tapes, strict=True):
                record = dict(timeline)
                events_ref = record.pop("events_object_ref")
                record["events"] = json.loads(inspector.object_page(attempt, events_ref).data)
                timeline_records.append(replace(timeline_side(record["events"]), record=record))
        resolved = event_comparison(*timeline_records)
        resolved = replace(
            resolved, attempt_id=destination, reference_attempt_id=attempts[0], candidate_attempt_id=attempts[1],
            reference=replace(resolved.reference, episode_id=receipts[0]["episode"]["episode_id"]),
            candidate=replace(resolved.candidate, episode_id=receipts[1]["episode"]["episode_id"]),
            request_sha256=_hash(tools.jcs_bytes({"request_id": request, "tool_id": "timeline.compare",
                                                  "payload": payload})),
        )
        test = self

        class Host:
            def resolve_comparison(self, *_args, **_kwargs):
                return resolved

            def verify_comparison_sources(self, _tool, _request, _resolved, sources):
                return (tuple(source.inspection["receipt_ref"] for source in sources.values()) == refs
                        and all(source.profile["timeline"]["reference"] == getattr(resolved, side).timeline.record
                                for side, source in sources.items()))

            def retain_comparison(self, *_args):
                test.fail("comparison used the legacy writer")

        evaluate = comparison_tools.evaluate
        result_fields = comparison_tools._result_fields

        def evaluate_after_start(*args, **kwargs):
            test.assertIn((clients.tenant, destination), starts)
            return evaluate(*args, **kwargs)

        def capture_report(tool, report):
            values = result_fields(tool, report)
            observer.report_digest = comparison_tools._comparison_digest(report | {"result_fields": values})
            return values

        with patch.object(comparison_tools, "evaluate", evaluate_after_start), patch.object(
            comparison_tools, "_result_fields", capture_report,
        ):
            compared = comparison_tools.handle(
                "timeline.compare", payload, request_id=request, host=Host(), retention=clients,
            )
        self.assertEqual(compared["verdict"]["result"], "match", compared["verdict"])
        with clients.inspector.client.session(clients.tenant) as inspector:
            readback = inspector.receipt(compared["receipt_ref"])
        self.assertEqual(readback["artifact_ref"], compared["artifact_ref"])
        self.assertEqual(readback["start"]["operation"], "compare")
        self.assertEqual(readback["terminal"]["operation"], "compare")
        self.assertEqual([readback["terminal"]["timeline"][side]["time_base"]
                          for side in ("reference", "candidate")],
                         [timeline["time_base"] for timeline in tapes])
        self.assertEqual(tuple(readback["start"][f"{side}_attempt_id"] for side in ("reference", "candidate")), attempts)
        return destination

    def test_retained_flow_over_unix_socket(self):
        self.exercise_retained_flow("unix")

    def test_retained_flow_over_tcp_tls(self):
        self.exercise_retained_flow("tcp_tls")
