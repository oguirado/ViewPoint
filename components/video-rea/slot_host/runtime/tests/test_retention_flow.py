# SPDX-License-Identifier: AGPL-3.0-only
"""ret2 regressions using separate Clients with the pinned Session interface."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from slot_host.compare import tools as comparison_tools
from slot_host.compare.tests.test_comparators import behaviour_comparison
from slot_host.projections.mcp_lane import SlotProjector, control_reply
from slot_host.projections.schema import digest as request_digest
from slot_host.projections.tests.test_projections import Fixture as ProjectionFixture
from slot_host.runtime import evidence_projection as projection, runner, tools
from slot_host.runtime.tests import test_retention_integration as base
from slot_host.runtime.tests.retention_flow_fixtures import FixtureObserver, install_capture

def _digest(data):
    return hashlib.sha256(data).hexdigest()


_SUPPORTS_FLOW = base._supports_flow(base.RetentionClients)


class Client(base._FakeClient):
    def __init__(self, principal, backend):
        super().__init__(principal, backend)
        self.validator = backend

    def session(self, tenant):
        return Session(self, tenant)


class Session(base._FakeSession):
    def admit(self, admission):
        self.client.backend.events.append(("admit", admission["attempt_id"]))
        return super().admit(admission)

    def start(self, attempt, raw, *, request_sha256):
        backend = self.client.backend
        backend.assert_writer(self)
        backend.events.append(("start", attempt))
        backend.starts[attempt] = json.loads(raw)
        ack = {"attempt_id": attempt, "request_sha256": request_sha256, "start_sha256": _digest(raw)}
        return ack if not backend.bad_start else ack | {"request_sha256": "f" * 64}

    def claim(self, acknowledgement):
        backend = self.client.backend
        backend.assert_writer(self)
        attempt = acknowledgement["attempt_id"]
        if attempt in backend.claimed or attempt not in backend.starts:
            raise AssertionError("claim must be issued once after start")
        backend.events.append(("claim", attempt))
        backend.claimed.add(attempt)
        if backend.lose_claim:
            raise OSError("claim acknowledgement lost")
        return acknowledgement

    def upload(self, attempt, upload, length, digest, chunks):
        backend = self.client.backend
        backend.assert_writer(self)
        if (length, digest) != (len(chunks), _digest(chunks)):
            raise AssertionError("upload must bind the exact supplied octets")
        if attempt not in backend.starts:
            raise AssertionError("upload must follow start")
        backend.events.append(("upload", attempt))
        reference = "obj_" + _digest((attempt + upload).encode())[:32]
        previous = backend.objects.setdefault((attempt, reference), chunks)
        if previous != chunks:
            raise AssertionError("changed idempotent upload")
        return {"object_ref": reference, "sha256": digest, "bytes": length}

    def finish(self, attempt, terminal, artifact, *, start_sha256):
        backend = self.client.backend
        backend.assert_writer(self)
        admitted = backend.admission(attempt)
        if ((admitted["operation"] != "compare" and json.loads(terminal)["state"] == "observed"
             and attempt not in backend.claimed)
                or projection.record_digest(backend.starts[attempt]) != start_sha256):
            raise AssertionError("finish must bind the original start and applicable claim")
        backend.events.append(("finish", attempt))
        if backend.interrupt_finish:
            backend.interrupt_finish = False
            backend.interrupted[attempt] = (terminal, artifact, start_sha256)
            raise OSError("test interruption after durable finish intent")
        if attempt in backend.interrupted and backend.interrupted[attempt] != (terminal, artifact, start_sha256):
            raise AssertionError("finish retry changed the original bytes")
        backend.finished[attempt] = (json.loads(terminal), artifact)
        return {"attempt_id": attempt, "start_sha256": start_sha256, "terminal_sha256": _digest(terminal)}

    def inspect(self, attempt):
        backend = self.client.backend
        backend.assert_inspector(self)
        backend.events.append(("inspect", attempt))
        start = backend.starts[attempt]
        common = {"tenant": self.tenant, "attempt_id": attempt, "admission": backend.admission(attempt),
                  "start": start, "start_sha256": projection.record_digest(start)}
        if attempt not in backend.finished:
            return common | {"state": "unknown", "terminal": None, "retirement": "unknown",
                             "launch_claimed": attempt in backend.claimed}
        terminal = backend.finished[attempt][0]
        return common | {"state": "committed", "terminal": terminal,
                         "terminal_sha256": projection.record_digest(terminal),
                         "artifact_ref": "art_" + attempt,
                         "receipt_ref": (backend.bad_receipt or "rcp_" + attempt)}

    def receipt(self, reference):
        self.client.backend.assert_inspector(self)
        self.client.backend.events.append(("receipt", reference[4:]))
        return self.inspect(reference[4:])

    def artifact(self, attempt, reference, *, length, digest):
        self.client.backend.assert_inspector(self)
        if reference != "art_" + attempt:
            raise AssertionError("artifact must be read from the terminal attempt")
        return self.client.backend.finished[attempt][1]

    def object_page(self, attempt, reference, *, offset=0, limit=1 << 20):
        backend = self.client.backend
        backend.assert_inspector(self)
        backend.events.append(("page", attempt))
        raw = backend.objects[attempt, reference]
        # Force small pages to exercise reassembly even on a short fixture.
        data = raw[offset: offset + min(limit, 31)]
        if backend.corrupt_object and data:
            data = bytes((data[0] ^ 1,)) + data[1:]
        return SimpleNamespace(reference=reference, total_bytes=len(raw), sha256=_digest(raw), offset=offset, data=data)


class Backend(base._Backend):
    def __init__(self):
        super().__init__()
        self.events = []
        self.starts = {}
        self.claimed = set()
        self.objects = {}
        self.finished = {}
        self.interrupted = {}
        self.lose_claim = self.bad_start = self.interrupt_finish = self.corrupt_object = False
        self.bad_receipt = None

    def admission(self, attempt):
        return next(item for item in self.admissions if item["attempt_id"] == attempt)

    def assert_writer(self, session):
        if session.client.principal != "fixture/runner" or session.tenant != base._TENANT:
            raise AssertionError("retention write crossed identity or tenant")

    def assert_inspector(self, session):
        if session.client.principal != "fixture/inspector" or session.tenant != base._TENANT:
            raise AssertionError("retention read crossed identity or tenant")

    def validate(self, name, value):
        if type(value) is not dict:
            raise ValueError(f"{name} must be a document")

    def validate_index(self, value):
        self.validate("Index", value)
        if value["schema"] != projection.INDEX_SCHEMA:
            raise ValueError("bad index schema")

    def validate_live_receipt_binding(self, schema_ref, schema_sha256, receipt_ref):
        if ((schema_ref, schema_sha256) != (projection.V03_REF, projection.V03_SHA)
                or not receipt_ref.startswith("rcp_")):
            raise ValueError("bad Tools receipt binding")

    def live_clients(self, path, observer):
        clients = self.clients(path)
        def identity(role):
            principal = "fixture/" + role
            return base.RetentionIdentity(client=Client(principal, self), principal=principal)

        clients = base._replace_clients(
            clients, authority=identity("authority"), runner=identity("runner"),
            inspector=identity("inspector"), profile_schema=projection.PROFILE_SCHEMA_V1,
        )
        if _SUPPORTS_FLOW:
            from slot_host.runtime.retention_witness import verify_profile_binding
            clients = replace(clients, validate_profile=verify_profile_binding, observe_terminal=observer)
        return clients


class RetentionFlowTests(base._RequestFixture, unittest.TestCase):
    def setUp(self):
        self.fixture(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.raw = install_capture(self)
        self.backend = Backend()
        self.observer = FixtureObserver(self.raw, self.before_spawn)
        self.clients = self.backend.live_clients(self.path, self.observer)
        self.enterContext(patch.object(tools, "_REQUEST_STORE", {}))
        self.enterContext(self.observer.track())

    def require_retention_type(self, name):
        try:
            from slot_host.runtime import retention
        except ImportError:
            self.fail("the retained launch runtime is unavailable")
        self.assertTrue(hasattr(retention, name), f"retained launch runtime has no {name}")
        return getattr(retention, name)

    def before_spawn(self, argv, kwargs):
        verb, attempt = self.backend.events[-1]
        self.assertEqual(verb, "claim")
        document = self.backend.starts[attempt]
        self.assertEqual(document["command"]["arguments"], argv)
        self.assertEqual(document["command"]["cwd"], kwargs["cwd"])
        self.assertEqual(document["command"]["env_sha256"], projection.env_sha256(kwargs["env"]))
        self.assertIn(attempt, self.backend.claimed)
        self.backend.events.append(("spawn", attempt))

    def run_request(self, request_id="req-ret2-offline"):
        try:
            return self.invoke(self.clients, request_id=request_id)["result"]
        except tools._ControlError as exc:
            self.fail(f"configured retention refused the test invocation: {exc.code}")

    def test_verified_start_then_claim_then_spawn_then_finish_then_readback(self):
        result = self.run_request()
        attempt = result["receipt"]["attempt"]["attempt_id"]
        self.assertEqual(self.marker.read_bytes(), b"1")
        self.assertTrue(result["evidence_accepted"])
        verbs = [verb for verb, identity in self.backend.events if identity == attempt]
        self.assertEqual(verbs[:4], ["admit", "start", "claim", "spawn"])
        self.assertLess(verbs.index("finish"), verbs.index("inspect"))
        self.assertEqual(result["shared_records"]["started"]["external_process"]["request_sha256"],
                         self.backend.admission(attempt)["request_sha256"])
        self.assertEqual(result["shared_records"]["retention_index"]["attempt_id"], attempt)
        self.assertEqual(result["receipt"]["shared_receipt_adapter"]["shared_receipt_ref"], "rcp_" + attempt)

    def test_pre_stage_callback_must_return_literal_true_before_allocating_scratch(self):
        RetentionAttempt = self.require_retention_type("RetentionAttempt")

        with patch.object(RetentionAttempt, "__call__", return_value=1), patch.object(
            runner.process_lifecycle, "create_run_scratch", side_effect=AssertionError("staged on truthy preflight"),
        ) as staged:
            result = self.run_request()
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        staged.assert_not_called()
        self.assertEqual([verb for verb, _ in self.backend.events], ["admit"])
        self.assert_private_terminal(result)
        # A fresh attempt can pass preflight and retain a witnessed abort.
        # Neither refusal can publish an accepted observation.
        with patch.object(runner, "live_admission", return_value=nullcontext(False)):
            self.assert_aborted_terminal_is_private("LIVE_EXECUTION_PARKED")

    def test_post_start_refusal_only_finishes_with_independent_no_spawn_proof(self):
        request = "req-ret2-post-start-refusal"
        with patch.object(runner, "live_admission", return_value=nullcontext(False)):
            result = self.run_request(request)
        attempt = result["receipt"]["attempt"]["attempt_id"]
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertEqual(result["custody"]["retirement"], "unknown")
        self.assertIn(attempt, self.backend.starts)
        self.assertNotIn(attempt, self.backend.finished)
        self.assertNotIn(attempt, self.backend.claimed)
        self.assertFalse(self.marker.exists())

        with patch.object(runner, "live_admission", return_value=nullcontext(False)):
            self.assert_aborted_terminal_is_private("LIVE_EXECUTION_PARKED")

    def assert_aborted_terminal_is_private(self, outcome):
        TerminalEvidence = self.require_retention_type("TerminalEvidence")

        def observe(admission, started, facts):
            self.assertIsNone(facts["process_identity"])
            self.assertFalse(self.marker.exists())
            return TerminalEvidence(
                projection.record_digest(started), "fixture/no-spawn", "fixture/absence",
                "fixture/retirement", "fixture/reap",
                cleanup_retirement_sha256=_digest(b"observed fixture scope removal"), no_spawn=True,
            )

        self.clients = replace(self.clients, observe_terminal=observe)
        finish = runner._finish_retained
        wrapped = []

        def capture(wrapper, *args, **kwargs):
            result = finish(wrapper, *args, **kwargs)
            wrapped.append(wrapper)
            return result

        with patch.object(runner, "_finish_retained", side_effect=capture):
            if outcome == "LIVE_EXECUTION_PARKED":
                with self.assertRaises(tools._ControlError) as parked:
                    self.invoke(self.clients, request_id="req-retfix-witnessed-refusal")
                self.assertEqual(parked.exception.code, outcome)
            else:
                self.run_request("req-retfix-witnessed-substitution")
        self.assertEqual(len(wrapped), 1)
        result = wrapped[0]
        attempt = result["receipt"]["attempt"]
        self.assertEqual(attempt["control_outcome"], outcome)
        end, _ = self.backend.finished[attempt["attempt_id"]]
        self.assertEqual(end["state"], "aborted_before_spawn")
        self.assertEqual(end["cleanup"]["state"], "confirmed_retired")
        self.assertFalse(self.marker.exists())
        self.assertNotIn(attempt["attempt_id"], self.backend.claimed)
        self.assert_private_terminal(result)

    def assert_private_terminal(self, result):
        adapter = result["receipt"]["shared_receipt_adapter"]
        self.assertFalse(result["evidence_accepted"])
        self.assertIs(adapter["live_use_permitted"], False)
        self.assertNotIn("shared_receipt_ref", adapter)
        self.assertIsNone(result["shared_records"])

    def test_aborted_stage_mismatch_does_not_publish_committed_receipt(self):
        identity = runner._path_identity
        changed = False

        def stage_identity(path):
            nonlocal changed
            observed = identity(path)
            if changed:
                return observed[0], observed[1] + 1, observed[2]
            return observed

        def started(*args, **kwargs):
            nonlocal changed
            ack = original_start(*args, **kwargs)
            changed = True
            return ack

        original_start = Session.start
        with patch.object(Session, "start", started), patch.object(runner, "_path_identity", stage_identity):
            self.assert_aborted_terminal_is_private("evidence_binding_rejected")

    def test_spawned_failure_does_not_publish_committed_receipt(self):
        program = self.exe.read_text()
        original = self.raw
        for custody_failure in (True, False):
            with self.subTest(custody_failure=custody_failure):
                captured = json.loads(original)
                if custody_failure:
                    captured["sha256"] = "f" * 64
                else:
                    captured.update(status="FAIL", rc=5)
                raw = (json.dumps(captured) + "\n").encode()
                fixture = program.replace(repr(original), repr(raw)) + f"sys.exit({captured['rc']})\n"
                self.exe.write_text(fixture)
                self.manifest["provider_ref"]["implementation_sha256"] = _digest(fixture.encode())
                self.policy = base.fixture_policy(self.manifest) | {"perception_ceiling": "none"}
                self.observer.raw = raw
                result = self.run_request(f"req-retfix-failed-capture-{custody_failure}")
                self.assertIsNotNone(result["pid"])
                self.assertIs(result["custody"]["producer_binding_verified"], not custody_failure)
                attempt = result["receipt"]["attempt"]
                if not custody_failure:
                    self.assertEqual(attempt["slot_result"]["status"], "FAIL")
                terminal, _ = self.backend.finished[attempt["attempt_id"]]
                self.assertEqual(terminal["state"], "observed")
                self.assertEqual(terminal["cleanup"]["state"], "confirmed_retired")
                self.assert_private_terminal(result)

    def test_post_start_stage_substitution_never_reaches_claim(self):
        start = Session.start

        def replace_snapshot(session, attempt, raw, *, request_sha256):
            result = start(session, attempt, raw, request_sha256=request_sha256)
            staged = Path(json.loads(raw)["command"]["arguments"][0])
            replacement = staged.with_name("replacement")
            replacement.write_bytes(b"not the verified executable")
            replacement.chmod(0o500)
            replacement.replace(staged)
            return result

        with patch.object(Session, "start", replace_snapshot):
            result = self.run_request()
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertEqual([verb for verb, _ in self.backend.events], ["admit", "start"])
        self.assertFalse(self.marker.exists())

    def test_bad_start_ack_never_claims_spawns_or_finishes(self):
        self.backend.bad_start = True
        result = self.run_request()
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertEqual([verb for verb, _ in self.backend.events], ["admit", "start"])
        self.assertFalse(self.marker.exists())

    def test_lost_claim_inspects_once_and_preserves_unknown_retirement_without_spawn(self):
        self.backend.lose_claim = True
        result = self.run_request()
        attempt = result["receipt"]["attempt"]["attempt_id"]
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertEqual(result["custody"]["start_sha256"], projection.record_digest(self.backend.starts[attempt]))
        self.assertEqual(result["custody"]["retirement"], "unknown")
        self.assertFalse(self.marker.exists())
        self.assertEqual([verb for verb, _ in self.backend.events], ["admit", "start", "claim", "inspect"])
        with self.assertRaises(tools._ControlError) as retry:
            self.invoke(self.clients, request_id="req-ret2-offline")
        self.assertEqual(retry.exception.code, "UNCERTAIN_OUTCOME")

    def test_interrupted_finish_retries_exact_terminal_and_artifact_without_redispatch(self):
        self.backend.interrupt_finish = True
        result = self.run_request()
        attempt = result["receipt"]["attempt"]["attempt_id"]
        terminal, artifact = self.backend.finished[attempt]
        self.assertEqual(
            self.backend.interrupted[attempt],
            (projection.record_bytes(terminal), artifact, result["custody"]["start_sha256"]),
        )
        verbs = [verb for verb, _ in self.backend.events]
        self.assertEqual(verbs.count("finish"), 2)
        self.assertEqual(verbs.count("spawn"), 1)
        self.assertTrue(result["evidence_accepted"])

    def test_missing_independent_terminal_evidence_preserves_unresolved_start(self):
        if _SUPPORTS_FLOW:
            self.clients = replace(self.clients, observe_terminal=lambda *_: None)
        result = self.run_request()
        attempt = result["receipt"]["attempt"]["attempt_id"]
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertIn(attempt, self.backend.claimed)
        self.assertNotIn("finish", [verb for verb, _ in self.backend.events])
        self.assertEqual(result["custody"]["retirement"], "unknown")
        self.assertFalse(result["evidence_accepted"])

    def test_unknown_retirement_commits_truthful_terminal_without_publishing_receipt(self):
        if _SUPPORTS_FLOW:
            def observe(*args):
                return replace(self.observer(*args), cleanup_retirement_sha256=None)
            self.clients = replace(self.clients, observe_terminal=observe)
        result = self.run_request()
        attempt = result["receipt"]["attempt"]["attempt_id"]
        terminal, _ = self.backend.finished[attempt]
        self.assertEqual(terminal["cleanup"]["state"], "unknown")
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertIsNone(result["shared_records"])
        self.assertNotIn("shared_receipt_ref", result["receipt"]["shared_receipt_adapter"])
        self.assertFalse(result["evidence_accepted"])

    def test_perception_refusal_has_no_start_terminal_or_synthetic_live_bundle(self):
        bound = self.require_binding(self.clients)
        result = runner.run_bound(
            bound, [], request_id="req-ret2-refused", attempt_id="a" * 32,
            perception_level="vision", capsule_perception_ceiling="none",
            attempt_store=lambda value: self.fail("retained refusal called a terminal writer"),
        )
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "PERCEPTION_DENIED")
        self.assertIsNone(result["shared_records"])
        self.assertFalse(self.backend.events)

    def test_readback_rejects_wrong_kind_and_corrupt_pages_after_finish(self):
        for i, (reference, corrupt) in enumerate((("art_" + "e" * 32, False), ("shared:fix", False), (None, True))):
            with self.subTest(reference=reference, corrupt=corrupt):
                self.backend.bad_receipt, self.backend.corrupt_object = reference, corrupt
                result = self.run_request(f"req-ret2-invalid-readback-{i}")
                attempt = result["receipt"]["attempt"]["attempt_id"]
                self.assertIn(attempt, self.backend.finished)
                self.assertEqual(result["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
                self.assertFalse(result["receipt"]["shared_receipt_adapter"]["live_use_permitted"])
                self.assertNotIn("shared_receipt_ref", result["receipt"]["shared_receipt_adapter"])
                self.assertFalse(result["evidence_accepted"])

    def test_recording_index_uses_server_ref_for_octets_and_canonical_event_bytes(self):
        raw = b"\x00\xffrecording\x02"
        events = [{"event_id": "event-1", "kind": "press-\u03c0", "t_ms": 0, "t_ticks": 1,
                   "input_digest": _digest(b"test input")}]
        canonical_events = projection.jcs_bytes(events)
        media = {"reference": {"recording_sha256": _digest(raw), "container": "mkv",
                               "codec": "ffv1", "clock_source": "pts", "frames": [],
                               "frames_unavailable_reason": "disposable recording"}}
        side = {"clock_source": "monotonic", "origin": "episode_start", "time_base": {"num": 1, "den": 2000},
                "canonicalization": "rea.timeline_digest/v1", "events": events,
                "timeline_sha256": _digest(canonical_events)}
        if _SUPPORTS_FLOW:
            def observe(*args):
                return replace(self.observer(*args), recordings={"reference": raw},
                               media=media, timeline={"reference": side})
            self.clients = replace(self.clients, observe_terminal=observe)
        result = self.run_request()
        attempt = result["receipt"]["attempt"]["attempt_id"]
        index = result["shared_records"]["retention_index"]
        objects = {item["role"]: item for item in index["objects"]}
        for role, raw_bytes in (("recording", raw), ("timeline_events", canonical_events)):
            entry = objects[role]
            self.assertEqual(entry["side"], "reference")
            self.assertRegex(entry["object_ref"], r"^obj_[0-9a-f]{32}$")
            self.assertEqual(self.backend.objects[attempt, entry["object_ref"]], raw_bytes)
        held = json.loads(self.backend.objects[attempt, objects["rea_profile"]["object_ref"]])
        self.assertEqual(held["timeline"]["reference"], side)
        self.assertNotIn("shared_receipt_ref", held["shared_receipt_adapter"])
        terminal_side = result["shared_records"]["terminal"]["timeline"]["reference"]
        self.assertEqual(terminal_side["events_object_ref"], objects["timeline_events"]["object_ref"])
        self.assertEqual(terminal_side["time_base"], side["time_base"])
        self.assertNotIn("events", terminal_side)

    def test_compare_starts_before_host_evaluation_reads_three_receipts_and_never_claims(self):
        self.exercise_retained_comparison("handler")

    def test_cli_reaches_registered_retained_comparison(self):
        self.exercise_retained_comparison("cli")

    def test_mcp_reaches_registered_retained_comparison(self):
        self.exercise_retained_comparison("mcp")

    def exercise_retained_comparison(self, face):
        sources = [self.run_request("req-ret2-reference"), self.run_request("req-ret2-candidate")]
        records = [source["receipt"] for source in sources]
        attempts = tuple(record["attempt"]["attempt_id"] for record in records)
        refs = tuple(record["shared_receipt_adapter"]["shared_receipt_ref"] for record in records)
        comp = behaviour_comparison()
        destination = "af" * 16
        supplied = {}
        if _SUPPORTS_FLOW:
            supplied = {"request_sha256": _digest(b"normalized comparison"), "reference_attempt_id": attempts[0],
                        "candidate_attempt_id": attempts[1]}
        comp = replace(comp, attempt_id=destination,
                       reference=replace(comp.reference, episode_id=records[0]["episode"]["episode_id"]),
                       candidate=replace(comp.candidate, episode_id=records[1]["episode"]["episode_id"]), **supplied)
        test = self

        class Host:
            def resolve_comparison(self, *args, **kwargs):
                test.assertIs(kwargs["runner_client"], test.clients.runner.client)
                test.assertIs(kwargs["inspector_client"], test.clients.inspector.client)
                test.assertNotIn("authority_client", kwargs)
                return comp

            def verify_comparison_sources(self, tool_id, request_id, resolved, verified):
                test.assertEqual(tuple(source.inspection["attempt_id"] for source in verified.values()), attempts)
                test.assertEqual(tuple(source.inspection["receipt_ref"] for source in verified.values()), refs)
                return True

            def retain_comparison(self, *args):
                test.fail("legacy comparison writer cannot publish retained receipts")

            def receive_comparison_witness(self, witness):
                test.assertEqual(witness.receipt.receipt_ref, "rcp_" + destination)

        evaluate = comparison_tools.evaluate
        digest = comparison_tools._comparison_digest

        def record_evaluation(*args, **kwargs):
            self.assertEqual(self.backend.events[-1], ("start", destination))
            report = evaluate(*args, **kwargs)
            self.backend.events.append(("evaluate", destination))
            return report

        def observe_report(*args):
            # Match the exact report bytes recorded by the test at the retention boundary.
            self.observer.report_digest = stored_digest[0]
            return self.observer(*args)

        stored_digest = []

        def capture(fields_tool, report):
            value = result_fields(fields_tool, report)
            stored_digest.append(digest({**report, "result_fields": value, "binding": {
                "attempt_id": destination, "request_id": "req-ret2-compare",
                "reference_episode_id": comp.reference.episode_id, "candidate_episode_id": comp.candidate.episode_id,
                "policy_sha256": comp.policy.sha256, "slot_result_digest": digest(comp.slot_result),
            }}))
            return value

        result_fields = comparison_tools._result_fields
        clients = replace(self.clients, observe_terminal=observe_report)
        self.backend.events.clear()
        with patch.object(comparison_tools, "evaluate", record_evaluation), patch.object(
            comparison_tools, "_result_fields", capture,
        ):
            tool = "behaviour.compare"
            payload = {"reference_ref": refs[0], "candidate_ref": refs[1], "policy_ref": "policy:fixture"}
            request_id = "req-ret2-compare"
            handler = comparison_tools.MINI_TOOLS[tool]["handler"]
            args = {"request_id": request_id, "payload": payload}
            envelope = comparison_tools.control_request(tool, args, context=self.context)

            def dispatch(envelope, **options):
                handoff = options.get("retention")
                self.assertIsNotNone(handoff, "comparison coordinator dropped the retention handoff")
                self.assertFalse(hasattr(handoff, "authority"), "the adapter received the authority credential")
                self.assertEqual(envelope["args"]["route_id"], "host." + tool)
                value = handler(envelope["args"]["arguments"]["payload"],
                                request_id=envelope["request_id"], host=Host(), **options)
                return control_reply(envelope["request_id"], "OK", ok=True, effective=request_digest(envelope),
                                     result={"kind": "host_result", "outside_capsule": True, "payload": value})

            if face == "handler":
                handle = comparison_tools.handle

                def inspect_handler(*args, **options):
                    self.assertFalse(hasattr(options["retention"], "authority"))
                    return handle(*args, **options)

                with patch.object(comparison_tools, "handle", side_effect=inspect_handler):
                    try:
                        result = handler(payload, request_id=request_id, host=Host(), retention=clients)
                    except TypeError as exc:
                        self.fail(f"registered comparison handler did not accept retention: {exc}")
            elif face == "cli":
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = comparison_tools.main(
                        ["mini", tool, "--args", json.dumps(args)], dispatch=dispatch,
                        context=self.context, retention=clients,
                    )
                self.assertEqual(code, 0)
                result = json.loads(stdout.getvalue())["result"]["payload"]
            else:
                fixture = ProjectionFixture()
                fixture.setUp()
                fixture.registry.update(comparison_tools.ARGUMENT_SCHEMAS)
                fixture.registry.update(comparison_tools.RESULT_SCHEMAS)
                profile = json.loads(
                    (Path(projection.__file__).resolve().parents[2] / "contracts/evidence"
                     / "rea.slot_evidence_profile_v1.schema.json").read_bytes()
                )
                fixture.registry[profile["$id"]] = profile
                fixture.minis = {tool: comparison_tools.MINI_TOOLS[tool]}
                projector = SlotProjector(fixture.manifest, fixture.generate(), dispatch=dispatch,
                                          authenticated_context=self.context, **fixture.sources())
                reply = projector.handle_envelope(envelope, retention=clients)
                self.assertTrue(reply["ok"], reply)
                result = reply["result"]["payload"]
        self.assertEqual(result["verdict"]["result"], "match")
        self.assertEqual((result["receipt_ref"], result["artifact_ref"]), ("rcp_" + destination, "art_" + destination))
        self.assertTrue(all(("receipt", attempt) in self.backend.events for attempt in (*attempts, destination)))
        verbs = [verb for verb, identity in self.backend.events if identity == destination]
        self.assertNotIn("claim", verbs)
        self.assertLess(verbs.index("admit"), verbs.index("start"))
        self.assertLess(verbs.index("start"), verbs.index("evaluate"))
        self.assertLess(verbs.index("evaluate"), verbs.index("finish"))
        stale = replace(comp, attempt_id="bc" * 16, reference_attempt_id=attempts[1],
                        candidate_attempt_id=attempts[0])
        with patch.object(Host, "resolve_comparison", return_value=stale), patch.object(
            comparison_tools, "evaluate", side_effect=AssertionError("swapped source reached evaluation"),
        ) as evaluator:
            with self.assertRaisesRegex(ValueError, "differs from the admitted"):
                comparison_tools.handle(
                    "behaviour.compare", {"reference_ref": refs[0], "candidate_ref": refs[1],
                                          "policy_ref": "policy:fixture"},
                    request_id="req-ret2-swapped", host=Host(), retention=clients,
                )
        evaluator.assert_not_called()
        self.assertNotIn(stale.attempt_id, self.backend.starts)


class OfflineWitnessTests(unittest.TestCase):
    def test_witness_replays_exact_evidence_and_rejects_missing_or_foreign_proof(self):
        try:
            from slot_host.runtime.retention_witness import OfflineWitness
        except ModuleNotFoundError:
            self.fail("offline witness callbacks are absent")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            actor = SimpleNamespace(tenant="fixture/tenant", principal="fixture/runner")
            admission = {"attempt_id": "a" * 32, "request_id": "req-fixture", "request_sha256": "b" * 64,
                         "operation": "record", "admission_sha256": "c" * 64, "executor_id": actor.principal}
            start = {"attempt_id": admission["attempt_id"], "executor_id": actor.principal,
                     "external_process": {key: admission[key] for key in ("request_id", "request_sha256")}}
            artifact = b"{}\n"
            terminal = {"attempt_id": admission["attempt_id"],
                        "artifact": {"bytes": len(artifact), "sha256": _digest(artifact)}}
            ack = {"attempt_id": admission["attempt_id"], "request_sha256": admission["request_sha256"],
                   "start_sha256": projection.record_digest(start)}
            witness = OfflineWitness(source, lambda *_: True)
            with self.assertRaises(OSError):
                witness.witness("start", actor, admission, projection.record_bytes(start))
            for kind, record, value in (
                ("start", start, projection.record_bytes(start)), ("claim", start, ack),
                ("finish", terminal, (projection.record_bytes(terminal), artifact)),
            ):
                binding = {"kind": kind, "tenant": actor.tenant, "principal": actor.principal,
                           "admission_sha256": admission["admission_sha256"],
                           "record_sha256": projection.record_digest(record)}
                evidence = {"binding": binding, "evidence": {"fixture_observed": kind}}
                (source / (projection.record_digest(binding) + ".json")).write_bytes(projection.record_bytes(evidence))
                proof = witness.witness(kind, actor, admission, value)
                reopened = OfflineWitness(source, lambda *_: True)
                self.assertEqual(projection.record_bytes(proof),
                                 projection.record_bytes(reopened.witness(kind, actor, admission, value)))
                self.assertIs(reopened.verify_witness(kind, actor, admission, record, proof), True)
                foreign = SimpleNamespace(tenant="fixture/foreign", principal=actor.principal)
                self.assertIs(reopened.verify_witness(kind, foreign, admission, record, proof), False)
            with self.assertRaises(ValueError):
                witness.witness("finish", actor, admission, (projection.record_bytes(terminal), b"[]\n"))
            with self.assertRaises(ValueError):
                witness.witness("claim", actor, admission, ack | {"request_sha256": "a" * 64})
