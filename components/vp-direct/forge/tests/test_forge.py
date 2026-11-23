from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

VP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VP))

from bus.journal import Journal
from control.core.control import CapabilityRegistry, Dispatcher, Handler, LaneIdentity, Outcome, RequestStore, digest
from forge import FixtureExecutor, Forge, ForgeError, verify_receipt
from forge.fixture import file_sha256, read_spool, verify_attempts
from forge.forge import _predicate, _verify_material
from schemas.validation import canonical_bytes, validate_contract


AGENT = LaneIdentity("agent", "fixture-agent")
OPERATOR = LaneIdentity("operator", "fixture-operator")
TOOL = "vp_replay_workflow"
WINDOW = "vp_window_summon"
ARGS = {"session_id": "fixture-session"}
SOURCE = "fixtures/site/index.html"
FIXTURE = "schemas/tests/examples/vp.capability_v1.valid.json"
ROUTE = {
    "verdict": "ABSTAIN", "code": "ROUTE_ABSTAIN", "reason": "absent_capability",
    "index_sha256": "a" * 64, "candidates": [],
}
PROOF = {"op": "field_eq", "field": "moved", "value": True}
CONTRAST = dict(PROOF, value=False)


def allow(_request, _risk):
    return True


def candidate(*, name=TOOL):
    return {
        "schema": "vp.forge_tool/v1", "tool": name, "state": "proposed", "args_schema": {},
        "steps": [{"op": WINDOW, "args": ARGS.copy(), "assert": PROOF.copy()}],
        "oracle": PROOF.copy(), "negative_control": CONTRAST.copy(), "risk_class": "leer",
        "obligations": ["preserve fixture authorization"], "proposed_by": AGENT.agent_instance, "parents": [],
    }


class ForgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir=Path(__file__).parent)))
        self.calls = []
        self.available = True
        self.window = json.loads((VP / FIXTURE).read_text())
        self.backend = self.execute
        self.forge = self.make_forge()

    def execute(self, request):
        self.calls.append(request)
        if not self.available:
            return Outcome(False, "NO_LEASE")
        return Outcome(True, "OK", {"moved": True, "from_ws": 0, "to_ws": 1,
                                    "backend": "wmctrl", "witness_ok": True})

    def unavailable(self, request):
        self.calls.append(request)
        return Outcome(False, "NO_LEASE")

    def make_forge(self, *, site_pins=None, executor_pins=None, gate=allow, generation=6, extra_entries=()):
        dispatcher = Dispatcher(CapabilityRegistry([self.window, *extra_entries]), RequestStore(self.root / "c1.sqlite"),
                                pace_risk=gate, lease_epoch=allow, tools={WINDOW: Handler(self.backend)})
        files = ("control/core/control.py", "forge/tests/test_forge.py")
        executor = FixtureExecutor(
            dispatcher, AGENT, context=ARGS, allowed=[{"op": WINDOW, "args": ARGS}], source_root=VP,
            site_pins={SOURCE: file_sha256(VP / SOURCE)} if site_pins is None else site_pins,
            executor_pins={name: file_sha256(VP / name) for name in files} if executor_pins is None else executor_pins,
        )
        return Forge(self.root / "forge", run_id="fixture-c6", fixture=executor,
                     current_manifest_generation=generation)

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(ForgeError) as error:
            call(*args, **kwargs)
        self.assertEqual(error.exception.code, code)

    def propose(self, tool=None, *, request_id="forge-propose-fixture", route_reply=ROUTE):
        return self.forge.propose(candidate() if tool is None else tool, actor=AGENT,
                                  request_id=request_id, route_reply=route_reply)

    def validate(self, tool=None, *, request_id="forge-validate-fixture"):
        tool = candidate() if tool is None else tool
        return self.forge.validate(tool["tool"], digest(tool), actor=AGENT, request_id=request_id)

    def capability(self, tool=None):
        tool = candidate() if tool is None else tool
        return dict(self.window, tool=tool["tool"], json_op=tool["tool"], origin="forged",
                    cli=tool["tool"].removeprefix("vp_").replace("_", "-"),
                    args_schema_sha256=digest(tool["args_schema"]),
                    source=[{"path": SOURCE, "line_start": 1, "line_end": 1, "sha256": file_sha256(VP / SOURCE)}])

    def approve(self, tool=None, *, actor=OPERATOR):
        tool = candidate() if tool is None else tool
        return self.forge.approve(tool["tool"], digest(tool), actor=actor, request_id="forge-approve-fixture")

    def admit(self, tool=None, *, actor=OPERATOR, capability=None, request_id="forge-admit-fixture",
              generation=7):
        tool = candidate() if tool is None else tool
        capability = self.capability(tool) if capability is None else capability
        return self.forge.admit(tool["tool"], digest(tool), actor=actor, request_id=request_id,
                                capability=capability, manifest_generation=generation)

    def assert_lineage(self, receipt):
        lines = (self.forge.root / "lineage.jsonl").read_bytes().splitlines(keepends=True)
        index = receipt["lineage"]["lamport"] - 1
        line = lines[index]
        event = json.loads(line)
        self.assertTrue(line.endswith(b"\n"))
        self.assertNotIn("lineage", event["payload"])
        self.assertEqual(receipt["lineage"]["event_hash"], sha256(line).hexdigest())
        previous = sha256(lines[index - 1]).hexdigest() if index else "0" * 64
        self.assertEqual(receipt["lineage"]["prev_hash"], previous)
        self.assertEqual({key: value for key, value in receipt.items() if key != "lineage"}, event["payload"])
        for pin in event["evidence"]:
            self.assertEqual(pin["sha256"], file_sha256(self.forge.root / pin["path"]))

    def assert_quarantine(self):
        self.assertIsNone(self.forge.publication(TOOL, manifest_generation=99))
        self.assertIsNone(self.forge.registry.get(TOOL))
        self.assertEqual(self.calls, [])

    def test_lifecycle_gates_lineage_replay_and_generation(self):
        proposal = self.propose()
        self.assertEqual(proposal["tool_sha256"], digest(candidate()))
        self.assertEqual(proposal["transition"], {"from": "proposed", "to": "quarantined"})
        self.assert_quarantine()
        validation = self.validate()
        self.assertEqual(validation["gates"], {
            "schema": "pass", "oracle": "pass", "negative_control": "failed_as_required",
        })
        self.assertEqual(validation["transition"]["to"], "validated")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["context"]["agent_instance"], AGENT.agent_instance)
        self.assertFalse(self.calls[0]["dry_run"])
        self.forge = self.make_forge()
        self.assertEqual(self.validate(), validation)
        self.assertEqual(self.forge.status(TOOL), validation)
        self.assertEqual(len(self.calls), 1)
        approval = self.approve()
        admitted = self.admit()
        for receipt in (proposal, validation, approval, admitted):
            validate_contract("vp.forge_receipt/v1", receipt)
            self.assert_lineage(receipt)
            verify_receipt(self.forge.root, receipt)
        self.assertEqual(admitted["transition"], {"from": "approved", "to": "admitted"})
        self.assertEqual(admitted["capability_sha256"], digest(self.capability()))
        self.assertIsNone(self.forge.publication(TOOL, manifest_generation=6))
        self.assertEqual(self.forge.publication(TOOL, manifest_generation=7), admitted)
        self.assertEqual(self.admit(), admitted)

    def test_shell_python_and_unknown_assertion_refused(self):
        for body in ("exec shell", "def run(): pass"):
            self.refused("FORGE_SCHEMA_INVALID", self.propose, dict(candidate(), body=body))
        self.refused("FORGE_SCHEMA_INVALID", self.propose, dict(candidate(), oracle={"op": "eval"}))
        self.assertEqual(self.calls, [])

    def test_unadmitted_dependency_and_nonabsence_recommendation_refused(self):
        for op in ("vp_shell_exec", "vp_window_dismiss", "vp_forge_admit"):
            tool = candidate()
            tool["steps"][0].update(op=op, args={} if op == "vp_forge_admit" else ARGS)
            self.refused("FORGE_DEPENDENCY_UNADMITTED", self.propose, tool)
        self.refused("FORGE_DEPENDENCY_UNADMITTED", self.propose,
                     route_reply=dict(ROUTE, reason="policy_denied", detail="op_unbound"))

    def test_closed_argument_schema_and_binding_gap(self):
        params = {"type": "object", "properties": {"param": {"type": "string", "maxLength": 256}},
                  "required": ["param"], "additionalProperties": False}
        invalid = [
            dict(params, additionalProperties=True), dict(params, required=["missing"]),
            dict(params, properties={"param": {"type": "string", "maxLength": 257}}),
            dict(params, properties={"x" * 4096: {"type": "boolean"}}),
        ]
        for schema in invalid:
            self.refused("FORGE_SCHEMA_INVALID", self.propose, dict(candidate(), args_schema=schema))
        tool = dict(candidate(), args_schema=params)
        self.propose(tool)
        validation = {
            "schema": "vp.control.req/v1", "op": "vp_forge_validate", "args": {},
            "request_id": "validate-with-parameter", "context": self.forge.fixture.context, "dry_run": False,
        }
        validate_contract("vp.control.req/v1", validation)
        with self.assertRaises(ValidationError):
            validate_contract("vp.control.req/v1", dict(validation, args={"param": "value"}))
        with self.assertRaisesRegex(ForgeError, r"^SCHEMA_GAP: .*steps\[\].args.*vp_forge_validate"):
            self.validate(tool)
        self.assertEqual(len(self.forge.journal.verify()), 1)
        self.assertEqual(list(self.forge.root.glob("attempts-*")), [])
        self.assertEqual(self.calls, [])

    def test_workflow_replay_without_frozen_publication_pin_refuses_before_dispatch(self):
        replay = dict(self.window, tool=TOOL, json_op=TOOL, cli="replay-workflow")
        self.forge = self.make_forge(extra_entries=[replay])
        tool = candidate(name="workflow-fixture")
        step = tool["steps"][0]
        step.update(op=TOOL, args={"publication_receipt_sha256": "b" * 64})
        with self.assertRaises(ValidationError):
            validate_contract("vp.forge_tool/v1", tool)
        request = {
            "schema": "vp.control.req/v1", "op": TOOL, "args": {},
            "request_id": "replay-with-pin", "context": self.forge.fixture.context, "dry_run": False,
        }
        validate_contract("vp.control.req/v1", request)
        with self.assertRaises(ValidationError):
            validate_contract("vp.control.req/v1", dict(request, args=step["args"]))
        step["args"] = {}
        validate_contract("vp.forge_tool/v1", tool)
        with self.assertRaisesRegex(ForgeError, r"^SCHEMA_GAP: .*vp_replay_workflow.*publication_receipt_sha256"):
            self.propose(tool)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.forge.root / "lineage.jsonl").exists())

    def test_literal_arguments_pin_the_ordered_attempts_and_survive_reopen(self):
        tool = candidate()
        tool["steps"] *= 2
        self.propose(tool)
        receipt = self.validate(tool)
        spool, = self.forge.root.glob("attempts-*.jsonl")
        rows = read_spool(spool)
        for index, (step, request) in enumerate(zip(tool["steps"], self.calls, strict=True)):
            self.assertEqual(request["args"], step["args"])
            child_id = "c6-" + sha256(canonical_bytes([receipt["effective_request_sha256"], index])).hexdigest()
            self.assertEqual(request["request_id"], child_id)
            self.assertEqual(rows[2 * index]["request_sha256"], sha256(canonical_bytes(request)).hexdigest())
        self.forge = self.make_forge()
        self.assertEqual(self.validate(tool), receipt)
        self.assertEqual(len(self.calls), 2)
        verify_receipt(self.forge.root, receipt)

    def test_invalid_frozen_child_request_is_refused_before_an_attempt(self):
        # An invalid bound context is absent from the candidate schema and reaches the C1 preflight.
        self.forge.fixture.context["unregistered_context_field"] = True
        self.propose()
        self.refused("FORGE_SCHEMA_INVALID", self.validate)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.forge.root.glob("attempts-*")), [])
        self.assertEqual(len(self.forge.journal.verify()), 1)

    def test_mutant_rehashed_attempt_with_substituted_arguments_cannot_verify(self):
        self.propose()
        self.validate()
        event = self.forge.journal.verify()[-1]
        with TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            for name in ["run.json", *(pin["path"] for pin in event["evidence"])]:
                (root / name).write_bytes((self.forge.root / name).read_bytes())
            proof_path, = root.glob("validation-*.json")
            proof = json.loads(proof_path.read_bytes())
            spool_path = root / proof["spool"]["path"]
            rows = read_spool(spool_path)
            request = dict(self.calls[0], args={"session_id": "substituted-session"})
            validate_contract("vp.control.req/v1", request)
            substituted = sha256(canonical_bytes(request)).hexdigest()
            for row in rows:
                row["request_sha256"] = substituted
            spool_path.write_bytes(b"".join(map(canonical_bytes, rows)))
            proof.update(ordered_requests=[substituted], spool={"path": spool_path.name,
                                                               "sha256": file_sha256(spool_path)})
            proof_path.write_bytes(canonical_bytes(proof))
            evidence = [dict(pin, sha256=file_sha256(root / pin["path"])) for pin in event["evidence"]]
            ref = Journal(root).append(agent=event["agent"], op_id=event["op_id"],
                                       kind=event["kind"], payload=event["payload"], evidence=evidence)
            receipt = dict(event["payload"], lineage={"chain": "lineage.event/v1",
                                                       "run_id": self.forge.run_id, **ref})
            validate_contract("vp.forge_receipt/v1", receipt)
            self.refused("BLOCKED_SOURCE", verify_receipt, root, receipt)

    def test_mutant_workflow_without_dispatch_pin_cannot_verify(self):
        replay = dict(self.window, tool=TOOL, json_op=TOOL, cli="replay-workflow")
        self.forge = self.make_forge(extra_entries=[replay])
        # Give the mutant a provisioned handler and skip only the workflow check.
        self.forge.fixture.dispatcher._tools[TOOL] = Handler(self.backend)
        tool = candidate(name="workflow-fixture")
        tool["steps"][0].update(op=TOOL, args={})
        validate_contract("vp.forge_tool/v1", tool)
        with patch.object(Forge, "_policies", return_value=[(TOOL, replay)]) as mutant:
            self.refused("BLOCKED_SOURCE", self.propose, tool)
        mutant.assert_called()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.forge.journal.verify(), [])

    def test_passing_negative_and_failed_positive_reject(self):
        cases = (("fixture-passing-negative", "negative_control", PROOF, "passed_unexpectedly"),
                 ("fixture-failing-positive", "oracle", CONTRAST, "failed_as_required"))
        for name, field, predicate, negative in cases:
            tool = dict(candidate(name=name), **{field: predicate})
            self.propose(tool, request_id="propose-" + name)
            receipt = self.validate(tool, request_id="validate-" + name)
            self.assertEqual(receipt["code"], "FORGE_GATE_FAILED")
            self.assertEqual(receipt["transition"]["to"], "rejected")
            self.assertEqual(receipt["gates"]["negative_control"], negative)
            self.assertIsNone(self.forge.publication(name, manifest_generation=99))
            self.assert_lineage(receipt)

    def test_agent_admit_and_skipped_approval_refused(self):
        self.propose()
        self.validate()
        self.refused("FORGE_APPROVAL_REQUIRED", self.approve, actor=AGENT)
        self.refused("FORGE_APPROVAL_REQUIRED", self.admit, actor=AGENT)
        self.refused("FORGE_APPROVAL_REQUIRED", self.admit)

    def test_next_generation_and_unavailable_registry_refuse_admit(self):
        self.propose()
        self.validate()
        self.approve()
        self.refused("FORGE_APPROVAL_REQUIRED", self.admit, generation=6)
        self.refused("FORGE_APPROVAL_REQUIRED", self.admit, generation=8, request_id="forge-admit-future")
        self.forge = self.make_forge(generation=None)
        self.refused("BLOCKED_SOURCE", self.admit, request_id="forge-admit-unknown-generation")

    def test_admission_refuses_stale_citation_and_weakened_risk(self):
        self.propose()
        self.validate()
        self.approve()
        stale = self.capability()
        stale["source"][0]["sha256"] = "0" * 64
        self.refused("BLOCKED_SOURCE", self.admit, capability=stale)
        lowered = dict(self.capability(), risk_class="observar")
        self.refused("FORGE_SCHEMA_INVALID", self.admit, capability=lowered, request_id="forge-admit-risk")
        self.assertIsNone(self.forge.publication(TOOL, manifest_generation=99))

    def test_unregistered_publication_name_is_schema_gap(self):
        tool = candidate(name="new-fixture-composition")
        self.propose(tool)
        self.validate(tool)
        self.approve(tool)
        self.refused("SCHEMA_GAP", self.admit, tool)
        self.assertIsNone(self.forge.publication(tool["tool"], manifest_generation=99))

    def test_request_id_mutation_and_tampered_evidence_refused(self):
        receipt = self.propose()
        self.refused("REQUEST_ID_CONFLICT", self.propose, dict(candidate(), risk_class="enviar"))
        (self.forge.root / ("tool-" + receipt["tool_sha256"] + ".json")).write_bytes(b"{}\n")
        self.refused("BLOCKED_SOURCE", self.validate)
        self.assertEqual(self.calls, [])

    def test_missing_executor_or_unavailable_observation_refuses(self):
        for field in ("site_pins", "executor_pins"):
            self.forge = self.make_forge(**{field: {}})
            self.refused("BLOCKED_SOURCE", self.propose)
        self.forge = self.make_forge()
        tool = dict(candidate(), negative_control={"op": "frame_differs", "frame_sha256": "a" * 64})
        self.propose(tool)
        self.refused("BLOCKED_SOURCE", self.validate, tool)
        self.assertEqual(self.forge.status(TOOL)["transition"]["to"], "quarantined")

    def test_unavailable_dispatch_is_preserved_and_never_retried(self):
        self.backend = self.unavailable
        self.forge = self.make_forge()
        self.propose()
        for _ in range(2):
            self.refused("BLOCKED_SOURCE", self.validate)
        spool, = self.forge.root.glob("attempts-*.jsonl")
        rows = read_spool(spool)
        self.assertEqual([row["phase"] for row in rows], ["started", "unavailable"])
        self.assertEqual(rows[-1]["code"], "NO_LEASE")
        self.assertEqual(len(self.calls), 1)

    def test_distinct_validation_requests_keep_all_attempts(self):
        self.propose()
        self.available = False
        self.refused("BLOCKED_SOURCE", self.validate)
        self.available = True
        validated = self.validate(request_id="forge-validation-new")
        self.assertEqual(validated["transition"]["to"], "validated")
        spools = [read_spool(path) for path in self.forge.root.glob("attempts-*.jsonl")]
        self.assertEqual(sorted(rows[-1]["phase"] for rows in spools), ["unavailable", "verified"])
        self.assertEqual(len({rows[0]["request_id"] for rows in spools}), 2)

    def test_bound_fixture_and_child_pace_gate(self):
        def refuse(_request, _risk):
            return "PACE_LIMITED"

        self.forge = self.make_forge(gate=refuse)
        self.propose()
        self.refused("BLOCKED_SOURCE", self.validate)
        self.assertEqual(self.calls, [])
        spool, = self.forge.root.glob("attempts-*.jsonl")
        self.assertEqual(read_spool(spool)[-1]["code"], "PACE_LIMITED")

    def test_step_outside_f1_allowlist_never_executes(self):
        tool = candidate()
        tool["steps"][0]["args"] = {"session_id": "another-session"}
        self.propose(tool)
        self.refused("BLOCKED_SOURCE", self.validate, tool)
        self.assertEqual(self.calls, [])

    def test_declared_assertions_use_verified_fields(self):
        sha = "b" * 64
        reply = {"code": "OK", "result": {"field": sha, "ax_stable_sha256": sha, "ax_text_sha256": sha,
                                           "frame_sha256": sha, "digest_match": True}}
        for predicate in ({"op": "code_eq", "code": "OK"},
                          {"op": "digest_eq", "field": "field", "sha256": sha},
                          {"op": "ax_node_exists", "ax_stable_sha256": sha},
                          {"op": "ax_text_sha256_eq", "sha256": sha},
                          {"op": "frame_differs", "frame_sha256": "a" * 64},
                          {"digest_match": True}):
            self.assertIs(_predicate(predicate, reply), True)
        self.assertIsNone(_predicate({"op": "digest_eq", "field": "absent", "sha256": sha}, reply))
        self.assertIs(_predicate({"op": "field_eq", "field": "digest_match", "value": 1}, reply), False)

    def test_mutant_risk_max_child_is_rejected_by_composition_oracle(self):
        tool = candidate()
        tool["steps"] *= 2
        calculate = Forge._risk

        def assert_risk(receipt):
            self.assertEqual(receipt["risk"]["risk_class"], "enviar")
            self.assertEqual(receipt["risk"]["child_max"], "preparar")
            self.assertEqual(receipt["risk"]["raised_by"], ["sequencing", "binding"])

        assert_risk(self.propose(tool))
        tool["tool"] = "fixture-risk-mutant"

        def max_only(body, policies):
            risk = calculate(body, policies)
            return dict(risk, risk_class=risk["child_max"], raised_by=[])

        with patch.object(Forge, "_risk", staticmethod(max_only)), self.assertRaises(AssertionError):
            assert_risk(self.propose(tool, request_id="forge-propose-risk-mutant"))

    def test_mutant_routable_quarantine_is_rejected_by_publication_oracle(self):
        proposal = self.propose()
        self.assert_quarantine()
        with patch.object(Forge, "publication", return_value=proposal), self.assertRaises(AssertionError):
            self.assert_quarantine()

    def test_mutant_auto_admit_is_rejected_by_operator_oracle(self):
        self.propose()
        self.validate()
        self.assertIsNone(self.forge.publication(TOOL, manifest_generation=99))
        self.approve()
        transition = Forge._operator_transition

        def use_operator_for_every_actor(forge, name, sha, request_id, _actor, **kwargs):
            return transition(forge, name, sha, request_id, OPERATOR, **kwargs)

        with patch.object(Forge, "_operator_transition", use_operator_for_every_actor):
            with self.assertRaises(AssertionError):
                self.refused("FORGE_APPROVAL_REQUIRED", self.admit, actor=AGENT)
        self.assertEqual(self.forge.status(TOOL)["transition"]["to"], "admitted")

    def test_mutant_drop_lineage_is_rejected_by_frozen_receipt(self):
        receipt = self.propose()
        self.assert_lineage(receipt)
        mutant = {key: value for key, value in receipt.items() if key != "lineage"}
        with self.assertRaises(ValidationError):
            validate_contract("vp.forge_receipt/v1", mutant)
        self.refused("BLOCKED_SOURCE", verify_receipt, self.forge.root, mutant)

    def test_mutant_drop_event_links_is_rejected_at_readback(self):
        append = Journal.append

        def without_evidence(journal, **fields):
            return append(journal, **dict(fields, evidence=[]))

        with patch.object(Journal, "append", without_evidence):
            self.refused("BLOCKED_SOURCE", self.propose)
        event, = self.forge.journal.verify()
        self.assertEqual(event["evidence"], [])
        self.refused("BLOCKED_SOURCE", self.forge.status, TOOL)

    def test_mutant_append_hash_without_lf_is_rejected_then_replay_uses_durable_reference(self):
        append = Journal.append
        wrong = []

        def without_lf(journal, **fields):
            reference = append(journal, **fields)
            line, = (self.forge.root / "lineage.jsonl").read_bytes().splitlines()
            wrong.append(sha256(line).hexdigest())
            return dict(reference, event_hash=wrong[-1])

        with patch.object(Journal, "append", without_lf):
            self.refused("BLOCKED_SOURCE", self.propose)
        self.forge = self.make_forge()
        receipt = self.propose()
        self.assert_lineage(receipt)
        self.assertNotEqual(receipt["lineage"]["event_hash"], wrong[0])
        self.assertEqual(len(self.forge.journal.verify()), 1)

    def test_mutant_rebound_event_or_claims_cannot_seal_receipt(self):
        self.propose()
        event, = self.forge.journal.verify()
        # An otherwise valid U3c append must still be bound to C6's request and
        # the exact persisted candidate.
        variants = (
            dict(agent="other-agent"),
            dict(op_id="other-request"),
            dict(payload=dict(event["payload"], steps_sha256=["b" * 64])),
            dict(payload=dict(event["payload"], workflow_pin={"publication_receipt_sha256": "b" * 64})),
        )
        for mutation in variants:
            with self.subTest(fields=mutation.keys()), TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                for name in ["run.json", *(pin["path"] for pin in event["evidence"])]:
                    (root / name).write_bytes((self.forge.root / name).read_bytes())
                fields = {key: event[key] for key in ("agent", "op_id", "kind", "payload", "evidence")}
                fields.update(mutation)
                ref = Journal(root).append(**fields)
                receipt = dict(fields["payload"], lineage={
                    "chain": "lineage.event/v1", "run_id": self.forge.run_id, **ref,
                })
                validate_contract("vp.forge_receipt/v1", receipt)
                self.refused("BLOCKED_SOURCE", verify_receipt, root, receipt)

    def test_mutant_missing_preappend_evidence_gate_writes_a_refused_event(self):
        persist = self.forge._persist
        checks = []

        def remove_router_evidence(path, content):
            pin = persist(path, content)
            if path.name.startswith("router-"):
                path.unlink()
            return pin

        def assert_no_event(request_id):
            self.refused("BLOCKED_SOURCE", self.propose, request_id=request_id)
            self.assertEqual(self.forge.journal.verify(), [])

        def skip_preappend_check(directory, payload, evidence):
            checks.append(payload["request_id"])
            if len(checks) > 1:
                _verify_material(directory, payload, evidence)

        with patch.object(self.forge, "_persist", side_effect=remove_router_evidence):
            assert_no_event("missing-proof-nominal")
            with patch("forge.forge._verify_material", side_effect=skip_preappend_check):
                with self.assertRaises(self.failureException):
                    assert_no_event("missing-proof-mutant")
        self.assertEqual(checks, ["missing-proof-mutant", "missing-proof-mutant"])
        event, = self.forge.journal.verify()
        self.assertEqual(event["payload"]["request_id"], "missing-proof-mutant")
        self.refused("BLOCKED_SOURCE", self.forge.status, TOOL)

    def test_mutant_merged_attempts_fails_independent_spool_oracle(self):
        tool = candidate()
        tool["steps"] *= 2
        self.propose(tool)
        result = self.validate(tool)
        proof = json.loads((self.forge.root / ("validation-" + result["effective_request_sha256"] + ".json")).read_text())
        self.assertEqual(proof["attempts"], 2)
        self.assertEqual(len(set(proof["ordered_observations"])), 2)
        spool = self.forge.root / proof["spool"]["path"]
        rows = read_spool(spool)
        self.assertEqual(len(rows), 4)
        self.assertNotEqual(rows[0]["request_id"], rows[2]["request_id"])
        replies = [self.forge.fixture.dispatcher.dispatch_mailbox(request, AGENT) for request in self.calls.copy()]
        verify_attempts(rows, self.calls, replies)
        self.refused("BLOCKED_SOURCE", verify_attempts, rows[:2], self.calls, replies)

    def test_mutant_omitted_unavailable_attempt_fails_audit(self):
        self.backend = self.unavailable
        self.forge = self.make_forge()
        self.propose()
        dispatch = FixtureExecutor.dispatch

        def omit_failure(executor, request, index, spool):
            reply = dispatch(executor, request, index, spool)
            if reply is None:
                spool.seek(0)
                spool.truncate()
                spool.flush()
            return reply

        with patch.object(FixtureExecutor, "dispatch", omit_failure):
            with self.assertRaisesRegex(ForgeError, "attempt count"):
                self.validate()
        self.assertEqual(self.forge.status(TOOL)["transition"]["to"], "quarantined")


if __name__ == "__main__":
    unittest.main()
