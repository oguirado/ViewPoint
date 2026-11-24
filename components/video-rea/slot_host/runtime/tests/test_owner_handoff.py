# SPDX-License-Identifier: AGPL-3.0-only
"""Offline handoff checks for U12 plus offline slices of U11/U13/U15/U16.

Covers the VERIFY now[3] gate: actual projected CLI input bounds, parked
or unavailable route behaviour, preview without executable evidence,
comparator input validation, fixture tape dependence, and published
shared-example offline checks. Live retention, authenticated readback,
and cross-face central claims stay explicit seams that deny (fail
closed); their owner-dependent positives are skipped with reason
"owner-pending: <T0x|MAIN>" per CHAIN_RULES.md.
"""

import base64
import contextlib
import copy
import hashlib
import io
from dataclasses import replace
from types import ModuleType, SimpleNamespace
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.compare import tools as compare_tools
from slot_host.compare._evaluate import evaluate
from slot_host.compare.tests.test_comparators import (
    Host, behaviour_comparison, event_comparison, event_trace, timeline_side,
)
from slot_host.projections import PublicationError, SlotProjector, availability_for, control_reply, json_invoke
from slot_host.runtime import receipts, tools
from slot_host.runtime.evidence_projection import V03_REF
from slot_host.runtime.runner import run_bound
from slot_host.runtime.tests.binding_fixtures import FixtureLaunch, fixture_policy, fixture_retention, verified_fixture_binding
from slot_host.runtime.tests.test_live_park import _fixture_capability, _manifest, _write_fixture
from slot_host.runtime.tests.test_replay_lineage import _manifest as replay_manifest, _write_marker
from slot_host.runtime.tests.test_result_custody import _valid_obj, _write_obj
from slot_host.runtime.tests.test_v03_projection import validate
from slot_host.projections.cli import MAX_ARGS_BYTES, main as cli_main
from slot_host.projections.schema import accepts, digest, standalone
from slot_host.projections.tests.test_projections import CONTROL_REPLY, CONTROL_REQUEST, Fixture
from video_rea.expose import wire

setUpModule = fixture_retention


# ViewPoint_Tools eeafb4b5b8af5c5d824098cfc89f025378407cde published these
# v0.2 examples and writer. The upstream writer is preserved byte for byte.
FIXTURES = Path(__file__).resolve().parent / "fixtures_v03"


def compare_fixture(comparison, *, candidate_ref="ep:candidate", request_id="req-handoff-compare",
                    episode_ids=("ep-reference-handoff", "ep-candidate-handoff")):
    """Resolve a controlled pair and pin the resolved policy in the retained artifact."""
    payload = {"reference_ref": "ep:reference", "candidate_ref": candidate_ref,
               "policy_ref": "policy:handoff"}
    episodes = {
        "ep:reference": SimpleNamespace(**(vars(comparison.reference) | {"episode_id": episode_ids[0]})),
        "ep:candidate": SimpleNamespace(**(vars(comparison.candidate) | {"episode_id": episode_ids[1]})),
        "ep:foreign": SimpleNamespace(**(vars(comparison.candidate) | {"episode_id": "ep-foreign-handoff"})),
    }
    policy = {"policy:handoff": comparison.policy}

    class FixtureHost(Host):
        comparison_witness = None

        def receive_comparison_witness(self, witness):
            self.comparison_witness = witness

        def resolve_comparison(self, tool_id, request_id, *, reference_ref, candidate_ref, policy_ref):
            return replace(comparison, reference=episodes[reference_ref],
                           candidate=episodes[candidate_ref],
                           policy=policy[policy_ref],
                           evidence_bound=comparison.evidence_bound and candidate_ref == "ep:candidate")

    host = FixtureHost(comparison)
    report = compare_tools.handle("behaviour.compare", payload, request_id=request_id, host=host)
    return host, report


class U12Handoff(Fixture):
    def projector(self, dispatch, **extra):
        kwargs = self.sources(**extra)
        kwargs.update({"dispatch": dispatch, "context": {}})
        return SlotProjector(self.manifest, self.bundle, **kwargs)

    def cli_run(self, argv, projector, stdin=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        extra = patch.object(sys, "stdin", io.StringIO(stdin)) if stdin is not None else contextlib.nullcontext()
        with extra, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(argv, projector=projector)
        return code, stdout.getvalue(), stderr.getvalue()

    def record_dispatch(self, calls):
        def dispatch(envelope):
            calls.append(json_invoke(self.manifest, self.bundle, envelope, **self.host()))
            return control_reply(envelope["request_id"], "AVAILABILITY_PARKED", ok=False)
        return dispatch

    def test_projected_cli_preserves_parked_refusal_and_exit_16(self):
        code, stdout, _ = self.cli_run(
            ["slot", "call", "rea", "record", "--args", json.dumps(self.args(params={"seed": 7}))],
            self.projector(None),
        )
        reply = json.loads(stdout)
        self.assertEqual(reply["schema"], "vp.control.reply/v1")
        self.assertEqual(reply["op"], "rea.slot.project")
        self.assertEqual(reply["request_id"], self.args()["request_id"])
        self.assertIs(reply["ok"], False)
        self.assertEqual(reply["code"], "AVAILABILITY_PARKED")
        self.assertEqual(reply["result"], {})
        self.assertEqual(code, 16)

    def test_projected_cli_parked_publication_error_exits_16(self):
        projector = self.projector(None)
        with patch.object(projector, "invoke", side_effect=PublicationError("LIVE_EXECUTION_PARKED", "parked")):
            code, stdout, _ = self.cli_run(
                ["slot", "call", "rea", "record", "--args", json.dumps(self.args(params={"seed": 7}))],
                projector,
            )
        self.assertEqual(json.loads(stdout)["result"], {})
        self.assertEqual(code, 16)

    def test_projected_cli_preserves_dispatched_live_execution_parked(self):
        schemas = Path(__file__).resolve().parents[4] / "vp-direct" / "schemas"
        pins_file = Path(__file__).resolve().parents[2] / "projections/tests/MAIN_CONTROL_SHA256SUMS"
        pins = {name: sha for sha, name in (
            row.split() for row in pins_file.read_text(encoding="ascii").splitlines()
            if row and not row.startswith("#")
        )}
        self.assertEqual(set(pins), {"vp.control.req_v1.schema.json", "vp.control.reply_v1.schema.json"})
        registry = dict(self.registry)
        for kind, ref in (("req", CONTROL_REQUEST), ("reply", CONTROL_REPLY)):
            name = f"vp.control.{kind}_v1.schema.json"
            raw = (schemas / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), pins[name], name)
            registry[ref] = json.loads(raw)
        installation = "installation:rea@handoff-park"
        self.binding["installations"] = [installation]
        bundle = self.generate(registry=registry)
        context = {"caller": "agent", "agent_instance": "park-handoff-fixture"}
        request_id = "req-handoff-live-parked"
        calls = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, executable = root / "launched", root / "fixture"
            manifest = _manifest(_write_fixture(executable, str(marker)))
            manifest["slot_id"] = manifest["provider_ref"]["provider_id"] = "rea"

            def dispatch(envelope):
                calls.append(envelope)
                inner = envelope["args"]["arguments"]
                with self.assertRaises(tools._ControlError) as raised:
                    tools.dispatch(
                        "behaviour.record",
                        {"request_id": envelope["request_id"], "dry_run": envelope["dry_run"],
                         "payload": {"installation_ref": inner["installation_ref"],
                                     "operation_id": "record", "argv": []}},
                        host={installation: (manifest, executable)}, host_policy=fixture_policy(manifest),
                    )
                self.assertEqual(raised.exception.code, "LIVE_EXECUTION_PARKED")
                return control_reply(envelope["request_id"], raised.exception.code, ok=False,
                                     effective=digest(envelope))

            projector = SlotProjector(
                self.manifest, bundle, **self.sources(registry=registry), dispatch=dispatch,
                context=context, result_verifier=lambda *_: self.fail("a refusal has no result to verify"),
            )
            code, stdout, _ = self.cli_run(
                ["slot", "call", "rea", "record", "--args",
                 json.dumps(self.args(request_id=request_id, installation_ref=installation))],
                projector,
            )
            self.assertFalse(marker.exists())

        self.assertEqual(len(calls), 1, stdout)
        self.assertEqual(calls[0]["args"]["route_id"], "ext.rea.record")
        self.assertIs(calls[0]["dry_run"], False)
        parked_reply = json.loads(stdout)
        self.assertEqual((code, parked_reply), (16, {
            "schema": "vp.control.reply/v1", "request_id": request_id, "op": "rea.slot.project",
            "ok": False, "code": "LIVE_EXECUTION_PARKED", "result": {},
            "effective_request_sha256": digest(calls[0]), "evidence": [], "not_verified": [],
        }))

        # Specializing either carrier prunes unrelated branches and restricts
        # the result to the selected operation.
        for ref, value in ((CONTROL_REQUEST, calls[0]), (CONTROL_REPLY, parked_reply)):
            selected = standalone(registry[ref], registry, control_op="rea.slot.project")
            self.assertTrue(accepts(value, selected))
            self.assertFalse(accepts(dict(value, op="vp_tool_route"), selected))
        reply_schema = standalone(registry[CONTROL_REPLY], registry, control_op="rea.slot.project")
        self.assertFalse(accepts(dict(parked_reply, code="LIVE_EXECUTION_PARKD"), reply_schema))
        self.assertFalse(accepts(dict(parked_reply, result={"unexpected": True}), reply_schema))
        request = standalone(registry[CONTROL_REQUEST], registry, control_op="rea.slot.project")
        self.assertFalse(accepts(dict(calls[0], context={}), request))
        args = calls[0]["args"]
        unaccepted = dict(args, arguments=dict(args["arguments"], params={"frames": [1]}))
        self.assertFalse(accepts(dict(calls[0], args=unaccepted), request))

    def assert_schema_refusal(self, run, request_id):
        code, stdout, _ = run
        self.assertEqual(code, 14)
        reply = json.loads(stdout)
        self.assertEqual(reply["request_id"], request_id)
        self.assertEqual(reply["code"], "SCHEMA_INVALID")
        self.assertIs(reply["ok"], False)
        self.assertEqual(reply["result"], {})

    def test_cli_args_file_at_limit_dispatches_and_beyond_limit_refuses(self):
        calls = []
        projector = self.projector(self.record_dispatch(calls))
        request_id = self.args()["request_id"]
        carrier = ["slot", "call", "rea", "record", "--dry-run", "--request-id", request_id]
        raw = json.dumps({"params": {"seed": 7}}).encode()
        exact = raw.ljust(MAX_ARGS_BYTES, b" ")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "args.json"
            path.write_bytes(exact)
            argv = carrier + ["--args-file", str(path)]
            code, stdout, _ = self.cli_run(argv, projector)
            self.assertEqual(code, 16)
            self.assertEqual(json.loads(stdout)["code"], "AVAILABILITY_PARKED")
            self.assertEqual([call["params"] for call in calls], [{"seed": 7}])
            path.write_bytes(exact + b" ")
            self.assert_schema_refusal(self.cli_run(argv, projector), request_id)
            self.assertEqual(len(calls), 1)

    def test_cli_stdin_dash_dispatches_valid_and_refuses_oversize(self):
        calls = []
        projector = self.projector(self.record_dispatch(calls))
        request_id = self.args()["request_id"]
        argv = ["slot", "call", "rea", "record", "--dry-run", "--args-file", "-",
                "--request-id", request_id]
        exact = json.dumps({"params": {"seed": 7}}).ljust(MAX_ARGS_BYTES)
        code, stdout, _ = self.cli_run(argv, projector, stdin=exact)
        self.assertEqual(code, 16)
        self.assertEqual(json.loads(stdout)["code"], "AVAILABILITY_PARKED")
        self.assertEqual([call["params"] for call in calls], [{"seed": 7}])
        self.assert_schema_refusal(self.cli_run(argv, projector, stdin=exact + " "), request_id)
        self.assertEqual(len(calls), 1)

    def test_wire_loads_enforces_byte_cap_before_decode(self):
        # Gate: unified bounded decoding. A direct wire.loads call
        # beyond MAX_LINE_BYTES refuses before JSON decoding; at-limit
        # parses. Pre-U12 this call returned {} and the check would
        # have failed on the old behaviour.
        at_limit = " " * (wire.MAX_LINE_BYTES - 2) + "{}"
        self.assertEqual(len(at_limit.encode("utf-8")), wire.MAX_LINE_BYTES)
        self.assertEqual(wire.loads(at_limit.encode("utf-8")), {})
        with self.assertRaises(ValueError):
            wire.loads(b" " * wire.MAX_LINE_BYTES + b"{}")
        with self.assertRaises(ValueError):
            wire.loads(" " * wire.MAX_LINE_BYTES + "{}")

    def test_parked_route_is_refusal_not_executed_ok(self):
        # Gate: unavailable publication / dispatcher never yields an
        # executed OK (C03). Both the no-manifest seam and the
        # no-dispatcher projector refuse with AVAILABILITY_PARKED.
        valid_doc = json.dumps(self.args(params={"seed": 7}))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(["slot", "call", "rea", "record", "--args", valid_doc],
                            manifest=None, bundle=None)
        self.assertEqual(code, 16)
        reply = json.loads(stdout.getvalue())
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "AVAILABILITY_PARKED")
        self.assertEqual(reply["result"], {})
        projector = self.projector(dispatch=None)
        code, stdout, _ = self.cli_run(
            ["slot", "call", "rea", "record", "--args", valid_doc], projector)
        self.assertEqual(code, 16)
        reply = json.loads(stdout)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "AVAILABILITY_PARKED")
        self.assertEqual(reply["result"], {})

        projected = copy.deepcopy(self.manifest)
        self.assertEqual(availability_for(projected, "record", binding=self.binding, policy=self.policy),
                         "admitted")
        projected["operations"][0]["binding"] = {"kind": "mcp_tool", "tool": "record"}
        self.assertEqual(availability_for(projected, "record", binding=self.binding, policy=self.policy),
                         "parked")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, executable = root / "launched", root / "fixture"
            manifest = _manifest(_write_fixture(executable, str(marker)))
            manifest["transport"]["alternates"] = [
                {"kind": "mcp_stdio", "argv_prefix": [], "framing": "jsonrpc_stdio"},
            ]
            ref = "installation:fixture-slot@handoff-alternate"
            host = {ref: (manifest, executable)}
            payload = {"installation_ref": ref, "operation_id": "record"}
            request = {"request_id": "req-handoff-primary", "dry_run": True, "payload": payload}
            with patch.object(tools, "run_bound") as runner:
                with self.assertRaises(tools._ControlError) as primary:
                    tools.dispatch("behaviour.record", request, host=host,
                                   host_policy=fixture_policy(manifest))
                self.assertEqual(primary.exception.code, "LIVE_EXECUTION_PARKED")
                self.assertTrue(tools.dispatch("behaviour.record", request, host=host,
                                               host_policy=fixture_policy(manifest),
                                               live_authority=FixtureLaunch())["result"]["planned"])
                manifest["operations"][0]["binding"] = {"kind": "mcp_tool", "tool": "ext.fixture.op"}
                inspected = tools.handle_slot_inspect(payload, host=host)
                self.assertEqual(inspected["readiness"]["availability"], "parked")
                self.assertEqual(inspected["plan"]["transport_kind"], "mcp_stdio")
                with self.assertRaises(tools._ControlError) as alternate:
                    tools.dispatch("behaviour.record", dict(request, request_id="req-handoff-alternate"),
                                   host=host, host_policy=fixture_policy(manifest),
                                   live_authority=FixtureLaunch())
                self.assertEqual(alternate.exception.code, "AVAILABILITY_PARKED")
                runner.assert_not_called()
            self.assertFalse(marker.exists())

    def test_preview_carries_no_execution_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, exe = root / "launch", root / "fixture"
            manifest = _manifest(_write_fixture(exe, str(marker)))
            ref = "installation:fixture-slot@handoff"
            request_id = "req-handoff-preview"
            args = {"request_id": request_id, "dry_run": True,
                    "payload": {"installation_ref": ref}}
            host = {ref: (manifest, exe)}
            policy = fixture_policy(manifest)
            with patch.object(tools, "run_bound") as runner:
                with self.assertRaises(tools._ControlError) as unauthorized:
                    tools.dispatch("behaviour.record", args, host=host, host_policy=policy)
                self.assertEqual(unauthorized.exception.code, "LIVE_EXECUTION_PARKED")
                no_ceiling = {key: value for key, value in policy.items() if key != "perception_ceiling"}
                with self.assertRaises(tools._ControlError) as missing:
                    tools.dispatch("behaviour.record", args, host=host, host_policy=no_ceiling,
                                   live_authority=FixtureLaunch())
                self.assertEqual(missing.exception.code, "PERCEPTION_DENIED")
                raw = dict(args, payload=dict(args["payload"], argv=["unexpected"]))
                with self.assertRaises(tools._ControlError) as invalid:
                    tools.dispatch("behaviour.record", raw, host=host, host_policy=policy,
                                   live_authority=FixtureLaunch())
                self.assertEqual(invalid.exception.code, "SCHEMA_INVALID")
                reply = tools.dispatch("behaviour.record", args, host=host,
                                       host_policy=policy, live_authority=FixtureLaunch())
                self.assertEqual(tools.dispatch("behaviour.record", args, host=host,
                                                host_policy=policy, live_authority=FixtureLaunch()), reply)
                with self.assertRaises(tools._ControlError) as changed:
                    tools.dispatch("behaviour.record", dict(args, dry_run=False), host=host,
                                   host_policy=policy, live_authority=FixtureLaunch())
                self.assertEqual(changed.exception.code, "REQUEST_ID_CONFLICT")
            self.assertEqual(reply, {"ok": True, "dry_run": True, "request_id": request_id,
                                    "result": {"planned": True}})
            runner.assert_not_called()
            self.assertFalse(marker.exists())

    def test_comparator_verdict_rejects_forged_qualification(self):
        for actual, outcome in (("on", "match"), ("off", "mismatch")):
            with self.subTest(outcome=outcome):
                comparison = behaviour_comparison(actual)
                host, report = compare_fixture(comparison)
                verdict = report["verdict"]
                self.assertEqual(evaluate("behaviour.compare", comparison)["verdict"], verdict)
                self.assertEqual(verdict["policy"], comparison.policy.pin())
                self.assertEqual(verdict["result"], outcome)
                check, = verdict["checks"]
                self.assertEqual((check["kind"], check["outcome"]), ("structured_value", outcome))
                retained = host.inspect(report["receipt_ref"], authorized=True)
                self.assertEqual(retained.attempt_id, comparison.attempt_id)
                self.assertEqual(retained.request_id, "req-handoff-compare")
                self.assertEqual(json.loads(host.artifacts[retained.artifact_ref])["verdict"], verdict)
                if outcome == "mismatch":
                    self.assertEqual(check["first_divergence"]["case_id"], "press-1")
                    self.assertIs(verdict["independent_qualification"], False)
                else:
                    self.assertIs(verdict["independent_qualification"], True)

        match = behaviour_comparison("on")
        # A one-tick delay crosses the inclusive policy bound while the
        # structured observation remains equal.
        for tick, outcome in ((146, "match"), (147, "mismatch")):
            with self.subTest(tick=tick):
                timing = event_comparison(candidate=timeline_side(event_trace((20, tick, 500), prefix="c")))
                combined = replace(match, policy=replace(match.policy, checks=("timeline", "structured")),
                                   reference=replace(match.reference, timeline=timing.reference.timeline),
                                   candidate=replace(match.candidate, timeline=timing.candidate.timeline))
                host, report = compare_fixture(combined)
                verdict = report["verdict"]
                self.assertEqual(verdict["result"], outcome)
                self.assertEqual(verdict["timeline_policy"], combined.policy.timeline.receipt_fields())
                self.assertEqual([(c["kind"], c["outcome"]) for c in verdict["checks"]],
                                 [("event_timeline", outcome), ("structured_value", "match")])
                if outcome == "mismatch":
                    self.assertEqual(verdict["checks"][0]["first_divergence"]["event_id"], "e2")
                    self.assertEqual(host.report["diagnostics"]["checks"][0]["diagnostics"]["events"][1]
                                     ["delta_ms_exact"], [67, 2])

        control, = match.negative_controls
        provider_ok = {
            "schema": "rea.slot_result/v1", "status": "OK", "rc": 0,
            "sha256": hashlib.sha256(b"provider status only").hexdigest(),
        }
        for status, rc, reason in (
            ("OK", 0, "slot_claim_only"),
            ("ERROR", 2, "slot_error"),
            ("INCOMPLETE", 3, "slot_incomplete"),
            ("STALE", 4, "slot_stale"),
            ("FAIL", 5, "slot_claim_only"),
            ("BUDGET_EXCEEDED", 6, "slot_budget_exceeded"),
        ):
            with self.subTest(status=status):
                _, report = compare_fixture(replace(
                    match, slot_result=dict(provider_ok, status=status, rc=rc),
                    policy=replace(match.policy, checks=()),
                ))
                self.assertEqual(report["verdict"]["result"], "abstain")
                self.assertEqual(report["verdict"]["abstain_reason"], reason)
                self.assertEqual(report["verdict"]["checks"], [])

        for reason, changed in (
            ("negative_control_missing", replace(match, negative_controls=())),
            ("negative_control_unactivated", replace(match, negative_controls=(
                dict(control, activated=False, outcome="invalid"),))),
            ("negative_control_survived", replace(match, negative_controls=(
                dict(control, outcome="survived"),))),
        ):
            with self.subTest(reason=reason):
                _, report = compare_fixture(changed)
                self.assertEqual(report["verdict"]["result"], "abstain")
                self.assertEqual(report["verdict"]["abstain_reason"], reason)
                self.assertIs(report["verdict"]["independent_qualification"], False)

        _, foreign = compare_fixture(match, candidate_ref="ep:foreign")
        self.assertEqual(foreign["verdict"]["result"], "abstain")
        self.assertEqual(foreign["verdict"]["abstain_reason"], "evidence_binding_rejected")
        self.assertEqual(foreign["verdict"]["checks"], [])

        # Removing origin on an otherwise valid result cannot leave a provider
        # status in the runtime receipt, even if it was already process-bound.
        opened = receipts.build_attempt_receipt(
            attempt_id="att-handoff-compare", request_id="req-handoff-compare",
            operation="compare", reference_episode_id="ep-reference-handoff",
            candidate_episode_id="ep-candidate-handoff", process_bound=True,
        )
        _, match_report = compare_fixture(match)
        for submitted in (match_report["verdict"],
                          dict(match_report["verdict"], independent_qualification="true"),
                          provider_ok):
            final = receipts.finalize_receipt(opened, provider_ok, verdict=submitted)
            self.assertNotIn("slot_result", final["attempt"])
            self.assertIs(final["observation"]["producer"]["process_bound"], False)
            self.assertEqual(final["verdict"]["result"], "abstain")
            self.assertFalse(receipts.evidence_accepted({"receipt": final, "custody": {}}))

    def test_fixture_tape_dependence_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, exe = root / "launch", root / "fixture"
            manifest = replay_manifest(_write_marker(exe, marker))
            ref, parent = "installation:fixture-slot@handoff-replay", "ep-handoff-original"
            tapes = {hashlib.sha256(tape).hexdigest() for tape in (b"seed:7", b"seed:8")}

            def resolve(episode, digest):
                if episode == parent and digest in tapes:
                    return {"parent_episode_id": parent, "input_tape_sha256": digest}
                return None

            expected = []
            for index, digest in enumerate(sorted(tapes)):
                replay = {"input_tape_sha256": digest, "determinism_mode": "uncontrolled"}
                reply = tools.dispatch(
                    "behaviour.replay", {"request_id": f"req-handoff-tape-{index}", "payload": {
                        "installation_ref": ref, "parent_episode_id": parent, "replay": replay,
                    }},
                    host={ref: (manifest, exe)}, host_policy=fixture_policy(manifest, "replay"),
                    tape_resolver=resolve, live_authority=FixtureLaunch(),
                )
                self.assertTrue(reply["ok"])
                self.assertEqual(reply["result"]["receipt"]["replay"], replay)
                expected.append({"argv": ["replay"], "replay": {"parent_episode_id": parent, **replay}})
            self.assertEqual([json.loads(row) for row in marker.read_text().splitlines()], expected)

    def test_shared_example_offline_shape_and_tamper_rejection(self):
        from .fixtures_v03 import tools as fixture_tools
        from .fixtures_v03.tools import arm64_capsules, check_contracts, maintenance_sample

        raw = (FIXTURES / "attempt_evidence_record.v0.2.schema.json").read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        self.assertEqual(sha, receipts._PUBLISHED_SHA)
        canonical = json.loads(raw)
        path = FIXTURES / "tools/attempt_evidence.py.txt"
        self.assertTrue(path.is_file(), "the exact upstream writer must be packaged locally")
        source = path.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "bc7cecfefc522c55911124ff1ac3d7be44ce2c5a45a0522b6629619a96d2022f")
        writer = ModuleType("tools.attempt_evidence")
        writer.__file__ = str(path)
        # Upstream imports use their original package names and the unversioned
        # schema filename. Resolve both against the verified local fixtures.
        schema_alias = FIXTURES / "attempt_evidence_record.schema.json"
        with patch.dict(sys.modules, {
            "tools": fixture_tools, "tools.arm64_capsules": arm64_capsules,
            "tools.check_contracts": check_contracts, "tools.maintenance_sample": maintenance_sample,
        }), patch.dict(maintenance_sample.CONTRACTS.cache, {schema_alias: canonical}):
            exec(compile(source, str(path), "exec"), writer.__dict__)
        self.assertEqual(writer.RECORD, canonical)

        examples = FIXTURES / "examples"
        prefix = "attempt_evidence_record"
        start = json.loads((examples / f"{prefix}.valid.json").read_bytes())
        retired = json.loads((examples / f"{prefix}.valid.confirmed-retired.json").read_bytes())
        for name, accepted in (("valid", True), ("invalid", False)):
            paths = sorted(examples.glob(f"{prefix}.{name}.*.json"))
            self.assertTrue(paths)
            for example in paths:
                with self.subTest(example=example.name):
                    record = json.loads(example.read_bytes())
                    if not accepted:
                        with self.assertRaises(ValueError):
                            writer.CONTRACTS.validate(record, canonical)
                    elif record["state"] == "started":
                        writer.check_start(record)
                    else:
                        writer.check_terminal(record, start, writer.record_digest(start))
        writer.check_start(start)

        with tempfile.TemporaryDirectory() as directory:
            store = writer.AttemptStore(directory)
            attempt_id = start["attempt_id"]
            held = Path(directory) / attempt_id

            def after_start(digest):
                self.assertEqual(digest, retired["start_sha256"])
                self.assertEqual(hashlib.sha256((held / writer.START).read_bytes()).hexdigest(), digest)
                self.assertEqual(store.verify(attempt_id)["state"], "unknown")

            store.dispatch(start, after_start)
            start_bytes = (held / writer.START).read_bytes()
            with self.assertRaises(FileExistsError):
                store.dispatch(dict(start, executor_id="substitute"), lambda _: self.fail("second dispatch"))
            self.assertEqual((held / writer.START).read_bytes(), start_bytes)

            execute, artifact, cleanup = (retired[key] for key in ("execution", "artifact", "cleanup"))
            with self.assertRaisesRegex(writer.EvidenceError, "artifact digest mismatch"):
                store.finish(attempt_id, execute, artifact, b"wrong", cleanup)
            self.assertEqual(store.verify(attempt_id)["state"], "unknown")
            store.finish(attempt_id, execute, artifact, b"hello", cleanup)
            terminal_bytes = (held / writer.END).read_bytes()
            store.finish(attempt_id, execute, artifact, b"hello", cleanup)
            with self.assertRaisesRegex(writer.EvidenceError, "conflicting terminal"):
                store.finish(attempt_id, execute, artifact, b"hello", dict(cleanup, observation_id="other"))
            self.assertEqual((held / writer.END).read_bytes(), terminal_bytes)
            self.assertEqual((held / writer.START).read_bytes(), start_bytes)
            report = store.verify(attempt_id)
            self.assertEqual(report["retirement"], "confirmed_retired")
            self.assertEqual(report["terminal"], retired)
            self.assertEqual(json.loads(store.export(attempt_id, "json")), report)
            self.assertIn(attempt_id, store.export(attempt_id, "markdown"))

            payload_path = held / writer.PAYLOAD
            original_payload = payload_path.read_bytes()
            payload_path.write_text(json.dumps({"base64": base64.b64encode(b"wrong").decode("ascii")}))
            with self.assertRaisesRegex(writer.EvidenceError, "artifact digest mismatch"):
                store.verify(attempt_id)
            payload_path.write_bytes(original_payload)

            (held / writer.END).unlink()
            report = store.verify(attempt_id)
            self.assertEqual(report["state"], "unknown")
            self.assertEqual(report["orphan_payloads"], [writer.PAYLOAD])
            (held / writer.START).unlink()
            with self.assertRaisesRegex(writer.EvidenceError, "orphan payload"):
                store.verify(attempt_id)
            end = held / writer.END
            end.write_bytes(terminal_bytes)
            end.chmod(0o600)
            with self.assertRaisesRegex(writer.EvidenceError, "orphan terminal"):
                store.export(attempt_id, "json")

        for ref in ("shared:fix", "shared:fix-attempt", "artifact:fixture/1", "receipt:fixture/1"):
            with self.subTest(unresolved_ref=ref):
                receipt = receipts.build_attempt_receipt(
                    attempt_id="att-handoff-retention", request_id="req-handoff-retention", operation="record",
                    shared_schema_ref=canonical["$id"], shared_schema_sha256=sha,
                    shared_receipt_ref=ref,
                )
                adapter = receipt["shared_receipt_adapter"]
                self.assertEqual(adapter["binding_state"], "bound")
                self.assertIs(adapter["live_use_permitted"], False)
                self.assertNotIn("shared_receipt_ref", adapter)
                # Also exercise the terminal boundary with an unverified prior
                # claim of readback, rather than just testing the constructor.
                adapter.update(shared_receipt_ref=ref, live_use_permitted=True)
                final = receipts.finalize_receipt(receipt, None)
                self.assertNotIn("shared_receipt_ref", final["shared_receipt_adapter"])
                self.assertIs(final["shared_receipt_adapter"]["live_use_permitted"], False)
                self.assertEqual(final["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
                self.assertFalse(receipts.evidence_accepted({"receipt": final, "custody": {}}))

    def test_offline_external_attempt_mapping_enforces_artifact_limit(self):
        record_schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        index_schema = json.loads((FIXTURES / "rea_retention_index.v1.schema.json").read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            for size in (65536, 65537):
                with self.subTest(size=size):
                    path = Path(directory) / f"tool-{size}.py"
                    payload = "x" * size
                    digest = hashlib.sha256(payload.encode()).hexdigest()
                    implementation = _write_obj(path, _valid_obj(result_payload=payload, sha256=digest))
                    manifest = _manifest(implementation)
                    manifest["evidence"]["shared_schema_ref"] = V03_REF
                    manifest["operations"][0]["limits"]["max_output_bytes"] = 70_000
                    bound = verified_fixture_binding(manifest, str(path), operation_id="record",
                                                     host_grants={"grant.exec.external_process"})
                    request_id, attempt_id = f"req-v03-{size}", f"att-v03-{size}"
                    wrapper = run_bound(bound, [], cwd=directory, request_id=request_id, attempt_id=attempt_id,
                                        live_authority=_fixture_capability(bound, request_id, attempt_id))
                    self.assertEqual(wrapper["receipt"]["attempt"]["slot_result"]["sha256"], digest)
                    self.assertTrue(wrapper["custody"]["producer_binding_verified"])
                    self.assertFalse(wrapper["custody"]["overflow"])
                    self.assertFalse(wrapper["receipt"]["shared_receipt_adapter"]["live_use_permitted"])
                    if size == 65537:
                        self.assertIsNone(wrapper["shared_records"])
                        self.assertFalse(wrapper["evidence_accepted"])
                        self.assertFalse(receipts.evidence_accepted(wrapper))
                    else:
                        self.assertTrue(wrapper["evidence_accepted"])
                        bundle = wrapper["shared_records"]
                        self.assertTrue(validate(bundle["started"], record_schema))
                        self.assertTrue(validate(bundle["terminal"], record_schema))
                        self.assertTrue(validate(bundle["retention_index"], index_schema))
                        self.assertEqual(bundle["terminal"]["artifact"]["bytes"], size)
                        self.assertEqual(bundle["terminal"]["artifact"]["sha256"], digest)
                        self.assertEqual(bundle["retention_index"]["attempt_id"], bundle["started"]["attempt_id"])

    @unittest.skip("owner-pending: T04")
    def test_live_comparator_readback_owner_pending(self):
        self.fail("Enrolled host.behaviour.compare needs authenticated Tools attachment readback")

    @unittest.skip("owner-pending: T05")
    def test_live_attempt_recovery_owner_pending(self):
        self.fail("Live recovery requires authenticated Tools readback and cleanup")

    @unittest.skip("owner-pending: T01")
    def test_live_retention_readback_owner_pending(self):
        # Authenticated durable start + evidence.inspect readback need
        # the ViewPoint_Tools qualification tenant.
        self.fail("live retention is owner-pending")

    @unittest.skip("owner-pending: MAIN")
    def test_central_claim_cross_face_owner_pending(self):
        # Shared RequestStore across ROUTER/CLI/MCP/JSON is MAIN-owned.
        self.fail("central claim is owner-pending")

    @unittest.skip("owner-pending: T03")
    def test_authenticated_producer_witness_owner_pending(self):
        # Owner-qualified result-byte attribution is T03-owned.
        self.fail("producer witness is owner-pending")


if __name__ == "__main__":
    unittest.main()
