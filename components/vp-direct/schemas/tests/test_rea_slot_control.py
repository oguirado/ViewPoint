# SPDX-License-Identifier: AGPL-3.0-only
"""Data-driven rea.slot.project schema tests.

Every canonical example and every projection derived from it is ACCEPTED.
Every negative/mutant is REJECTED. Kit hashes verified via SHA256SUMS.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
REA = REPO / "video-rea"
TESTS = ROOT / "tests"
KIT = TESTS / "fixtures" / "rea_kit"
SPEC = importlib.util.spec_from_file_location("vp_rea_validation", ROOT / "validation.py")
validation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation)
validate = validation.validate_contract
MANIFEST = KIT / "rea.slot_manifest_v2.schema.json"
REPLY = ROOT / "vp.control.reply_v1.schema.json"

def load(p):
    return json.loads(p.read_text(encoding="utf-8"))

def slot_envelope(route_id="ext.rea.record", dry_run=True, request_id="w2-local-slot-01"):
    return {
        "schema": "vp.control.req/v1",
        "request_id": request_id,
        "op": "rea.slot.project",
        "context": {"caller": "agent", "agent_instance": "video-rea-local"},
        "args": {"route_id": route_id, "arguments": {"slot_id": "rea", "op_id": "record"}},
        "dry_run": dry_run,
    }

def slot_reply(result, ok=True, code="OK", request_id="w2-local-slot-01"):
    return {
        "schema": "vp.control.reply/v1",
        "request_id": request_id,
        "op": "rea.slot.project",
        "ok": ok,
        "code": code,
        "result": result,
        "effective_request_sha256": "a" * 64,
        "evidence": [],
        "not_verified": [],
    }

def project_slot(manifest, digest="a"*64):
    caps = deepcopy(manifest["capabilities"])
    probes=[]
    for p in caps["prerequisite_probes"]:
        probes.append({"id": p["id"], "effects": p["effects"], "requires_binding": p["requires_binding"], "verified_state": "passed"})
    caps["prerequisite_probes"]=probes
    ops=[]
    for op in manifest["operations"]:
        ops.append({
            "id": op["id"], "role": op["role"], "input_ref": op["input_ref"], "output_ref": op["output_ref"],
            "required_effects": op["required_effects"], "required_grants": op["required_grants"],
            "limits": op["limits"], "retry_semantics": op["retry_semantics"],
            "binding_kind": op["binding"].get("kind"), "availability": "not_installed",
            "conformance_receipt_ref": None,
        })
    slot={
        "schema": manifest["schema"], "slot_id": manifest["slot_id"], "status": manifest["status"],
        "manifest_digest": digest, "upstream": deepcopy(manifest["upstream"]),
        "provider_ref": deepcopy(manifest["provider_ref"]), "capabilities": caps,
        "perception": deepcopy(manifest["perception"]), "record_replay": deepcopy(manifest["record_replay"]),
        "comparison": deepcopy(manifest["comparison"]), "evidence": deepcopy(manifest["evidence"]),
        "conformance": deepcopy(manifest["conformance"]), "discovery": deepcopy(manifest["discovery"]),
        "operations": ops,
    }
    return {"kind":"slot_describe","slot":slot}

def project_dry_external(manifest, digest="a"*64, policy="b"*64):
    op = manifest["operations"][0]
    return {
        "kind":"dry_run","dry_run":True,
        "normalized_request":{
            "route_id": f"ext.{manifest['slot_id']}.{op['id']}",
            "manifest_digest": digest,
            "slot_id": manifest["slot_id"],
            "op_id": op["id"],
            "installation_ref": "ref-1",
            "request_id": "w2-local-slot-01",
            "inputs": [], "params": {},
            "perception_level": manifest["perception"]["required_level"],
            "determinism_mode": manifest["record_replay"]["determinism_modes"][0],
            "dry_run": True, "policy_sha256": policy,
        },
        "outside_capsule": True,
    }

def project_row_ext(manifest, op, digest="a"*64):
    facets = {
        "target_kinds": manifest["capabilities"]["target_kinds"],
        "platforms": manifest["capabilities"]["platforms"],
        "adapter_identity": deepcopy(manifest["capabilities"]["adapter_identity"]),
        "comparator_id": manifest["comparison"]["comparator_id"],
        "upstream": {"name": manifest["upstream"]["name"], "version": manifest["upstream"]["version"], "licence_id": manifest["upstream"]["licence_id"]},
        "binding_kind": op["binding"].get("kind"),
        "transport_kind": manifest["transport"]["kind"],
        "determinism_modes": manifest["record_replay"]["determinism_modes"],
        "artifact_roles": manifest["evidence"]["artifact_roles"],
    }
    json_ref = {
        "control_request_schema_ref": "urn:vp.control.req:v1",
        "control_reply_schema_ref": "urn:vp.control.reply:v1",
        "op": "rea.slot.project",
        "route_id": f"ext.{manifest['slot_id']}.{op['id']}",
        **next(c for c in manifest["contract_refs"] if c["role"] == op["role"]),
        "request_selector": op["input_ref"], "result_selector": op["output_ref"],
    }
    provider = deepcopy(manifest["provider_ref"])
    row={
        "id": f"ext.{manifest['slot_id']}.{op['id']}",
        "owner": "viewpoint-rea",
        "manifest_digest": digest,
        "provider_ref": provider,
        "role": op["role"],
        "operation": op["id"],
        "summary": manifest["discovery"]["operation_descriptions"][op["id"]],
        "search_terms": sorted(set(manifest["discovery"]["search_terms"] + [manifest["slot_id"]]) ),
        "input_schema_ref": None,
        "output_schema_ref": "urn:vp.control.reply:v1",
        "mcp_name": f"rea_ext_{manifest['slot_id'].replace('-','_')}__{op['id']}",
        "cli_argv_shape": ["slot","call",manifest["slot_id"],op["id"],"--args","<json>"],
        "json_contract_ref": json_ref,
        "required_effects": sorted(op["required_effects"]),
        "perception": {"required_level": manifest["perception"]["required_level"], "optional_levels": manifest["perception"]["optional_levels"]},
        "availability": "not_installed",
        "conformance_receipt_ref": None,
        "outside_capsule": True,
        "facets": facets,
    }
    return row

def project_list(manifest):
    rows = [project_row_ext(manifest, op) for op in manifest["operations"]]
    slot_id = manifest["slot_id"]
    slug = slot_id.replace("-", "_")
    case_row = deepcopy(rows[0])
    case_row.update({
        "id": f"ext.{slot_id}.conform.case_a", "operation": "conform.case_a", "role": None,
        "mcp_name": f"rea_conform_{slug}__case_a",
        "cli_argv_shape": ["slot", "conform", slot_id, "case_a"],
        "perception": None, "availability": "parked",
    })
    case_row["facets"].update(binding_kind=None, case_id="case_a", case_ref="host/case/a")
    carrier_keys = ("control_request_schema_ref", "control_reply_schema_ref", "op")
    carrier = {key: case_row["json_contract_ref"][key] for key in carrier_keys}
    case_row["json_contract_ref"] = dict(carrier, route_id=case_row["id"])
    describe = deepcopy(rows[0])
    describe.update({
        "id": f"ext.{slot_id}", "operation": "describe", "role": None,
        "mcp_name": f"rea_slot_describe__{slug}", "cli_argv_shape": ["slot", "describe", slot_id],
        "json_contract_ref": dict(carrier, route_id=f"ext.{slot_id}"), "required_effects": [],
    })
    describe["facets"]["binding_kind"] = None
    host_row = {
        "id": "host.slot.list", "owner": "viewpoint-rea", "manifest_digest": None, "provider_ref": None,
        "role": None, "operation": "list", "summary": "List REA slot router entries",
        "search_terms": ["host.slot.list"], "input_schema_ref": None,
        "output_schema_ref": "urn:vp.control.reply:v1", "mcp_name": "rea_slot_list",
        "cli_argv_shape": ["slot", "list"], "json_contract_ref": dict(carrier, route_id="host.slot.list"),
        "required_effects": [], "perception": {"required_level": "none", "optional_levels": []},
        "availability": "admitted", "conformance_receipt_ref": None, "outside_capsule": True, "facets": {},
    }
    return {"kind": "slot_list", "rows": sorted([*rows, describe, case_row, host_row], key=lambda row: row["id"])}


def run_generator(manifest, reply, *flags, projections=KIT / "PROJECTIONS.md"):
    return subprocess.run(
        [sys.executable, "-B", str(ROOT.parent / "tools/gen_rea_slot_schemas.py"),
         "--manifest", str(manifest.relative_to(REPO)),
         "--projections", str(projections.relative_to(REPO)),
         "--examples", str((KIT / "examples").relative_to(REPO)),
         "--reply", str(reply.relative_to(REPO)), *flags],
        cwd=REPO, capture_output=True, text=True,
    )


class ReaSlotDataDrivenTests(unittest.TestCase):
    def test_kit_hashes(self):
        sums = KIT / "SHA256SUMS"
        pinned = {}
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, name = line.split(None, 1)
            self.assertNotIn(name, pinned)
            pinned[name] = digest
        files = {f"./{path.relative_to(KIT).as_posix()}": path for path in KIT.rglob("*")
                 if path.is_file() and path != sums}
        self.assertEqual(pinned.keys(), files.keys())
        for name, path in files.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), pinned[name], name)

    def test_generator_drift(self):
        result = run_generator(MANIFEST, REPLY, "--check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generator_detects_manifest_schema_drift(self):
        with TemporaryDirectory(dir=TESTS) as temp:
            manifest = Path(temp) / MANIFEST.name
            mutated = load(MANIFEST)
            mutated["properties"]["slot_id"]["maxLength"] -= 1
            manifest.write_text(json.dumps(mutated), encoding="utf-8")
            reply = Path(temp) / REPLY.name
            original = REPLY.read_bytes()
            reply.write_bytes(original)

            check = run_generator(manifest, reply, "--check")
            self.assertEqual(check.returncode, 1, check.stderr)
            self.assertIn("generated reply schema differs", check.stderr)
            self.assertEqual(reply.read_bytes(), original)

            generated = run_generator(manifest, reply)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertNotEqual(reply.read_bytes(), original)
            slot = load(reply)["$defs"]["slotDescribe"]
            self.assertEqual(slot["properties"]["slot_id"]["maxLength"],
                             mutated["properties"]["slot_id"]["maxLength"])

    def test_generator_detects_projection_rule_drift(self):
        with TemporaryDirectory(dir=TESTS) as temp:
            projections = Path(temp) / "PROJECTIONS.md"
            original = (KIT / projections.name).read_text(encoding="utf-8")
            mutated = original.replace("and `sample_count` for `replay`",
                                       "and `sample_limit` for `replay`")
            self.assertNotEqual(mutated, original)
            projections.write_text(mutated, encoding="utf-8")
            result = run_generator(MANIFEST, REPLY, "--check", projections=projections)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("generated reply schema differs", result.stderr)

    def test_canonical_slot_describe_accepted(self):
        for p in sorted((KIT/"examples").glob("*.slot.json")):
            m=load(p)
            proj=project_slot(m)
            with self.subTest(example=p.name):
                Draft202012Validator(load(MANIFEST), format_checker=Draft202012Validator.FORMAT_CHECKER).validate(m)
                self.assertEqual(m["status"], "not_installed")
                validate("vp.control.reply/v1", slot_reply(proj))

    def test_canonical_dry_run_accepted(self):
        for p in sorted((KIT/"examples").glob("*.slot.json")):
            m=load(p)
            proj=project_dry_external(m)
            with self.subTest(example=p.name):
                validate("vp.control.reply/v1", slot_reply(proj))

    def test_canonical_slot_list_accepted(self):
        for p in sorted((KIT/"examples").glob("*.slot.json")):
            m=load(p)
            proj=project_list(m)
            with self.subTest(example=p.name):
                validate("vp.control.reply/v1", slot_reply(proj))

    def test_every_negative_manifest_has_one_assertion_and_repair(self):
        cases = {
            "agent-inside-capsule": ("const", ("transport", "driver_location"), "outside_capsule"),
            "missing-licence": ("required", ("upstream", "licence_id"), "MIT"),
            "vendored-code": ("const", ("upstream", "user_installed"), True),
            "vision-without-pool": (
                "required", ("perception", "observation_sources", 1, "vision_pool"),
                {"required": True, "owner": "ViewPoint_Tools", "ticket_field": "vision_pool_ticket"},
            ),
        }
        fixtures = {p.name.removesuffix(".slot.json"): p for p in (KIT / "negative").glob("*.slot.json")}
        self.assertEqual(fixtures.keys(), cases.keys())
        validator = Draft202012Validator(load(MANIFEST), format_checker=Draft202012Validator.FORMAT_CHECKER)
        for name, (keyword, path, value) in cases.items():
            with self.subTest(fixture=name):
                manifest = load(fixtures[name])
                errors = list(validator.iter_errors(manifest))
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0].validator, keyword)
                self.assertEqual(tuple(errors[0].absolute_path), path[:-1] if keyword == "required" else path)
                if keyword == "required":
                    self.assertIn(repr(path[-1]), errors[0].message)

                parent = manifest
                for segment in path[:-1]:
                    parent = parent[segment]
                parent[path[-1]] = value
                validator.validate(manifest)
                for projection in (project_slot(manifest), project_list(manifest), project_dry_external(manifest)):
                    validate("vp.control.reply/v1", slot_reply(projection))

    def test_external_process_contract_gates_match_the_manifest(self):
        manifest = load(KIT / "examples/rea.slot.json")
        manifest["status"] = "registered"
        manifest["upstream"]["version"] = "1.2.3"
        manifest["provider_ref"].update(
            schema="rea.provider/v2", provider_revision="1.2.3", provider_sha256="a" * 64,
            implementation_sha256="b" * 64,
        )
        manifest["capabilities"]["adapter_identity"]["adapter_sha256"] = "c" * 64
        for contract in manifest["contract_refs"]:
            contract.update(
                request_schema_ref="urn:rea.slot_request:v2", request_digest="d" * 64,
                result_schema_ref="urn:rea.slot_result:v2", result_digest="e" * 64,
                execution_profile="external_process",
            )
        for operation in manifest["operations"]:
            for field in ("input_ref", "output_ref"):
                operation[field] = operation[field].replace(":v1#", ":v2#")

        schema = load(MANIFEST)
        validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
        validator.validate(manifest)
        validate("vp.control.reply/v1", slot_reply(project_list(manifest)))
        nominal = project_slot(manifest)
        validation.validate_definition("vp.control.reply/v1", "slotDescribeComplete", nominal["slot"])
        filtered = deepcopy(nominal)
        filtered["slot"]["operations"] = filtered["slot"]["operations"][:1]
        validate("vp.control.reply/v1", slot_reply(filtered))
        with self.assertRaises(ValidationError):
            validation.validate_definition("vp.control.reply/v1", "slotDescribeComplete", filtered["slot"])

        row = project_row_ext(manifest, manifest["operations"][0])
        contract = row["json_contract_ref"]
        for key in ("request", "result"):
            field = key + "_schema_ref"
            pin = key + "_digest"
            selector = key + "_selector"
            uri = contract[field].replace(":v2", ":v1")
            for mutation in ({field: uri, selector: uri + "#slot=" + contract["role"]}, {pin: None}):
                changed = dict(row, json_contract_ref=dict(contract, **mutation))
                with self.assertRaises(ValidationError):
                    validate("vp.control.reply/v1", slot_reply({"kind": "slot_list", "rows": [changed]}))
        changed = dict(row, provider_ref=dict(row["provider_ref"], schema="rea.provider/v1"))
        with self.assertRaises(ValidationError):
            validate("vp.control.reply/v1", slot_reply({"kind": "slot_list", "rows": [changed]}))

    def test_oracle_envelopes_and_declared_mutations(self):
        oracle = KIT / "oracle"
        validator = Draft202012Validator(load(oracle / "rea.conformance_case_v1.schema.json"))
        index = load(oracle / "INDEX.json")
        indexed = {entry["path"] for entry in index["cases"]}
        on_disk = {p.relative_to(oracle).as_posix() for p in oracle.glob("cases/*/*.json")}
        self.assertEqual(indexed, on_disk)
        for artifact in index["artifacts"]:
            self.assertTrue((oracle / artifact["path"]).is_file(), artifact)
        for entry in index["cases"]:
            with self.subTest(fixture=entry["path"]):
                path = oracle / entry["path"]
                case = load(path)
                validator.validate(case)
                for key in ("test_id", "case"):
                    self.assertEqual(case[key], entry[key])
                mutant = dict(case, execution_profile="external_process")
                self.assertFalse(validator.is_valid(mutant))
                if case["case"] == "negative":
                    nominal = load(path.with_name(case["base_case"]))
                    validator.validate(nominal)
                    self.assertEqual(nominal["test_id"], case["test_id"])
                    self.assertEqual(nominal["case"], "nominal")
                    stimulus = deepcopy(nominal)
                    mutation = case["mutation"]
                    pointer = [key.replace("~1", "/").replace("~0", "~")
                               for key in mutation["path"].split("/")[1:]]
                    parent = stimulus
                    for key in pointer[:-1]:
                        parent = parent[int(key) if isinstance(parent, list) else key]
                    key = int(pointer[-1]) if isinstance(parent, list) else pointer[-1]
                    self.assertEqual(parent[key], mutation["from"])
                    self.assertNotEqual(parent[key], mutation["to"])
                    parent[key] = mutation["to"]
                    self.assertNotEqual(stimulus["fixture"], nominal["fixture"])

    def test_evidence_fixture_verdicts(self):
        evidence = KIT / "evidence"
        validator = Draft202012Validator(load(evidence / "rea.slot_evidence_profile_v1.schema.json"))
        fixtures = {path.stem: path for path in (evidence / "examples").glob("*.json")}
        self.assertEqual(fixtures.keys(), {"match", "mismatch", "abstain", "shifted-events-negative"})
        for name, path in fixtures.items():
            with self.subTest(fixture=name):
                record = load(path)
                validator.validate(record)
                verdict = dict(record["verdict"])
                self.assertEqual(verdict["result"], "mismatch" if name == "shifted-events-negative" else name)
                projected = {key: verdict[key] for key in ("result", "comparator_id", "comparator_version")}
                projected["verdict_ref"] = None
                if "abstain_reason" in verdict:
                    projected["abstain_reason"] = verdict["abstain_reason"]
                validation.validate_definition("vp.control.reply/v1", "verdict", projected)
                if name == "abstain":
                    del projected["abstain_reason"]
                else:
                    projected["abstain_reason"] = "should only appear on an abstention"
                with self.assertRaises(ValidationError):
                    validation.validate_definition("vp.control.reply/v1", "verdict", projected)

    def test_dry_run_incomplete_rejected(self):
        bads=[
            {"kind":"dry_run","dry_run":True,"normalized_request":{"route_id":"ext.rea.record"},"outside_capsule":True},
            {"kind":"dry_run","dry_run":True,"normalized_request":{"route_id":"ext.rea.record","manifest_digest":"a"*64,"request_id":"w2-local-slot-01","dry_run":True,"policy_sha256":"b"*64},"outside_capsule":True},
        ]
        for b in bads:
            with self.assertRaises(ValidationError):
                validate("vp.control.reply/v1", slot_reply(b))

    def test_dry_run_params_are_bound_to_the_replay_role(self):
        reply = project_dry_external(load(KIT / "examples/rea.slot.json"))
        params = reply["normalized_request"]["params"]
        params.update(seed=1, mode="exact", sample_count=2)
        validate("vp.control.reply/v1", slot_reply(reply))
        params["budget_frames"] = 4
        with self.assertRaises(ValidationError):
            validate("vp.control.reply/v1", slot_reply(reply))

    def test_slot_describe_incomplete_rejected(self):
        # empty subtrees
        base = project_slot(load(KIT/"examples/rea.slot.json"))
        bads=[]
        for field in ["upstream","provider_ref","capabilities","perception","record_replay","comparison","evidence","conformance","discovery"]:
            b=deepcopy(base)
            b["slot"][field]={}
            bads.append(b)
        # probe argv leak
        b=deepcopy(base)
        b["slot"]["capabilities"]["prerequisite_probes"][0]["argv"]=["--secret"]
        bads.append(b)
        # probe missing verified_state
        b=deepcopy(base)
        del b["slot"]["capabilities"]["prerequisite_probes"][0]["verified_state"]
        bads.append(b)
        # comparison missing waiver
        b=deepcopy(base)
        del b["slot"]["comparison"]["waiver_policy"]
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["comparison"]["coverage_requirements"]=[]
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["comparison"]["tolerance_units"]=[]
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["comparison"]["tolerance_units"]={}
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["perception"]["required_level"]="vision"
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["status"]="registered"
        b["slot"]["upstream"].update(version="0.0.0", source_url="https://pending.invalid/x")
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["conformance"]["required_checks"]=[]
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["discovery"]["examples"]=[{"op":"record","args":123}]
        bads.append(b)
        for operations in ([], [base["slot"]["operations"][0]] * 2):
            b=deepcopy(base)
            b["slot"]["operations"]=operations
            bads.append(b)
        b=deepcopy(base)
        b["slot"]["operations"][0]["retry_semantics"]={"mode":"at_most_once","on_interrupt":"retry"}
        bads.append(b)
        b=deepcopy(base)
        b["slot"]["operations"][0]["binding_kind"]="bogus_kind"
        bads.append(b)
        # evidence wrong shape: runtime slot group instead of manifest evidence
        b=deepcopy(base)
        b["slot"]["evidence"]={"slot":{"tool_id":"x","tool_version":"1","upstream_licence_id":"MIT","source_url":"https://example.com","user_installed":True,"out_of_process":True,"use_disposition":"host_owned","manifest_sha256":"a"*64}}
        bads.append(b)
        # conformance missing supported_entry_points
        b=deepcopy(base)
        del b["slot"]["conformance"]["supported_entry_points"]
        bads.append(b)
        # discovery missing operation_descriptions
        b=deepcopy(base)
        del b["slot"]["discovery"]["operation_descriptions"]
        bads.append(b)
        for b in bads:
            with self.assertRaises(ValidationError):
                validate("vp.control.reply/v1", slot_reply(b))

    def test_slot_list_incomplete_or_misbound_rows(self):
        base = project_list(load(KIT / "examples/rea.slot.json"))
        route_rows = {row["id"]: row for row in base["rows"]}
        ext = route_rows["ext.rea.record"]
        case = route_rows["ext.rea.conform.case_a"]
        host = route_rows["host.slot.list"]
        carrier_keys = ("control_request_schema_ref", "control_reply_schema_ref", "op", "route_id")
        contract = ext["json_contract_ref"]
        mutants = (
            (ext, {"json_contract_ref": {}}),
            (ext, {"json_contract_ref": {key: contract[key] for key in carrier_keys}}),
            (ext, {"json_contract_ref": dict(contract, route_id="ext.rea.replay")}),
            (ext, {"json_contract_ref": dict(contract, role="sample")}),
            (ext, {"json_contract_ref": dict(contract, request_selector="urn:rea.slot_request:v1#slot=sample")}),
            (ext, {"json_contract_ref": dict(contract, result_schema_ref2="urn:spurious")}),
            (ext, {"provider_ref": None}),
            (ext, {"manifest_digest": None}),
            (ext, {"role": None}),
            (ext, {"provider_ref": {"__injected__": 1}}),
            (ext, {"facets": {}}),
            (ext, {"facets": dict(ext["facets"], adapter_identity={})}),
            (ext, {"required_effects": ["NOT_AN_EFFECT_GRAMMAR"]}),
            (ext, {"cli_argv_shape": ["slot", "call", "different-slot", "record", "--args", "<json>"]}),
            (case, {"facets": {k: v for k, v in case["facets"].items() if k != "case_ref"}}),
            (case, {"facets": dict(case["facets"], case_id="wrong_case")}),
            (host, {"manifest_digest": "a" * 64}),
            (host, {"facets": {"case_id": "case_a"}}),
        )
        for row, mutation in mutants:
            with self.subTest(route=row["id"], mutation=mutation):
                validate("vp.control.reply/v1", slot_reply({"kind": "slot_list", "rows": [row]}))
                altered = {"kind": "slot_list", "rows": [dict(row, **mutation)]}
                with self.assertRaises(ValidationError):
                    validate("vp.control.reply/v1", slot_reply(altered))

    def test_host_result_payload_must_be_object(self):
        with self.assertRaises(ValidationError):
            validate("vp.control.reply/v1", slot_reply({"kind":"host_result","payload":None,"outside_capsule":True}))

    def test_compare_requires_verdict(self):
        # slotExchangeCompareResult requires verdict
        store = validation._store()
        schema = store["urn:vp.control.reply:v1"]
        resolver = validation._LocalResolver.from_schema(schema, store=store)
        compare_schema = schema["$defs"]["slotExchangeCompareResult"]
        v = Draft202012Validator(compare_schema, resolver=resolver)
        without={"kind":"slot_exchange","status":"OK","rc":0,"slot_result_ref":"r","evidence_ref":"r","receipt_ref":"r","availability":"admitted","produced_by":"a","perception_used":"none","outside_capsule":True}
        with self.assertRaises(ValidationError):
            v.validate(without)
        with_verdict=dict(without, verdict={"result":"match","comparator_id":"c","comparator_version":"v1","verdict_ref":None})
        v.validate(with_verdict)

    def test_inspect_requires_close_shape(self):
        store = validation._store()
        schema = store["urn:vp.control.reply:v1"]
        resolver = validation._LocalResolver.from_schema(schema, store=store)
        inspect = schema["$defs"]["hostInspectResult"]
        v = Draft202012Validator(inspect, resolver=resolver)
        base_slot = project_slot(load(KIT/"examples/rea.slot.json"))["slot"]
        payload={"slot":base_slot,"installation_refs":["ref"],"selected_installation_ref":"ref","conformance_receipt_ref":None,"unresolved_conformance_refs":[]}
        with self.assertRaises(ValidationError):
            v.validate({"kind":"host_result","payload":None,"outside_capsule":True})
        v.validate({"kind":"host_result","payload":payload,"outside_capsule":True})

    def test_op_registered(self):
        req = load(ROOT/"vp.control.req_v1.schema.json")
        reply = load(ROOT/"vp.control.reply_v1.schema.json")
        inv = load(ROOT/"tests/contract_inventory.json")
        self.assertIn("rea.slot.project", req["properties"]["op"]["enum"])
        self.assertIn("rea.slot.project", reply["properties"]["op"]["enum"])
        self.assertIn("rea.slot.project", inv["tools"])
        self.assertEqual(len(inv["tools"]), 54)
        self.assertEqual(inv["control_wire_additions"], {
            "selectors": ["vp_rea_call"],
            "refusal_codes": {"rea.slot.project": ["LIVE_EXECUTION_PARKED"]},
        })
        self.assertEqual(req["properties"]["request_id"], {"type":"string","minLength":1})

    def test_envelope_and_refusals(self):
        # spot check original behavior preserved
        validate("vp.control.req/v1", slot_envelope())
        with self.assertRaises(ValidationError):
            validate("vp.control.req/v1", slot_envelope(dry_run="no"))
        # refusal shape
        validate("vp.control.reply/v1", slot_reply({}, ok=False, code="SCHEMA_INVALID"))
        with self.assertRaises(ValidationError):
            validate("vp.control.reply/v1", slot_reply(None, ok=False, code="SCHEMA_INVALID"))
        for code in ("HOLD","HUMAN_NEEDED","NO_LEASE","REQUEST_ID_CONFLICT","UNCERTAIN_OUTCOME","SCHEMA_INVALID","PERCEPTION_DENIED","POOL_TIMEOUT","PROFILE_NOT_ADMITTED","AVAILABILITY_NOT_INSTALLED","AVAILABILITY_BLOCKED","AVAILABILITY_PARKED"):
            validate("vp.control.reply/v1", slot_reply({}, ok=False, code=code))

    def test_rea_call_selector_in_public_carriers(self):
        args = load(REA / "contracts/tests/examples/vp_rea_call.rea.ingest.args.json")["valid"]
        request = dict(slot_envelope(), op="vp_rea_call", args=args)
        reply = dict(slot_reply({}, ok=False, code="HOLD"), op="vp_rea_call")
        for carrier in (request, reply):
            with self.subTest(schema=carrier["schema"]):
                validate(carrier["schema"], carrier)
                with self.assertRaises(ValidationError):
                    validate(carrier["schema"], dict(carrier, op="vp_rea_cal"))

    def test_rea_call_request_rejects_invalid_args(self):
        invalid_args = (None, 123, [1, 2], "str", {}, {"op": "rea.invented_op"},
                        {"op": "rea.ingest", "invented": 123})
        for args in invalid_args:
            request = dict(slot_envelope(), op="vp_rea_call", args=args)
            with self.subTest(args=args), self.assertRaises(ValidationError):
                validate(request["schema"], request)

    def test_rea_call_selector_in_local_mcp(self):
        args = load(REA / "contracts/tests/examples/vp_rea_call.rea.ingest.args.json")["valid"]
        arguments = dict(args, request_id="w2-local-rea-call", dry_run=True)
        messages = [{"jsonrpc": "2.0", "id": tool, "method": "tools/call",
                     "params": {"name": tool, "arguments": arguments}}
                    for tool in ("vp_rea_call", "vp_rea_cal")]
        run = subprocess.run([sys.executable, "-B", "-m", "video_rea.expose.mcp"],
                             cwd=REA, input="".join(json.dumps(msg) + "\n" for msg in messages),
                             text=True, capture_output=True, check=True, timeout=10)
        accepted, refused = map(json.loads, run.stdout.splitlines())
        reply = json.loads(accepted["result"]["content"][0]["text"])
        self.assertEqual(reply["op"], "vp_rea_call")
        self.assertEqual(reply["code"], "HOLD")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["result"]["plan"]["op"], args["op"])
        validate("vp.control.reply/v1", reply)
        for invalid in (dict(reply, code="INVENTED_CODE"),
                        dict(reply, result={"sessions": [{"session_id": "s1", "state": "AGENT"}]})):
            with self.subTest(reply=invalid), self.assertRaises(ValidationError):
                validate("vp.control.reply/v1", invalid)
        self.assertEqual(refused["error"]["code"], -32601)

    def test_parked_live_execution_is_a_closed_slot_refusal(self):
        reply = slot_reply({}, ok=False, code="LIVE_EXECUTION_PARKED")
        validate(reply["schema"], reply)
        for invalid in (dict(reply, code="LIVE_EXECUTION_PARKD"), dict(reply, ok=True),
                        dict(reply, result={"unexpected": True})):
            with self.subTest(reply=invalid), self.assertRaises(ValidationError):
                validate(reply["schema"], invalid)

if __name__=="__main__":
    unittest.main()
