"""Offline S-TORO conformance oracle. All authorities and payloads here are test data."""
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
from posixpath import normpath
import subprocess
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from asset_factory import core
from asset_factory.contracts import validate_slot
from tools.pipeline.contracts import validate_stage
from tools.publish import publisher

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/s_toro"
BOUNDARIES = {"stage", "provider", "artifact", "telemetry", "lease"}
SCHEMA_IDS = {
    "stage": "urn:af:s-toro-stage-receipt:v1", "provider": "urn:af:s-toro-provider-io:v1",
    "artifact": "urn:af:artifact-ref:v1", "telemetry": "urn:af:telemetry:v1",
    "lease": "urn:af:worker-lease:v1",
}


INTENT_KEYS = ("consumer_id", "deadline_ns", "priority", "affinity_key", "expected_order")
TORO_COMMON = "schema kind session_id git_sha cpuset process_id timestamp_ns".split()
TORO_EVENT = (
    "thread sequence toro_session lane generation reference component operation status "
    "logical_bytes copy_bytes dram_bytes nvme_bytes wait_ns source destination hash"
).split()
FABRICA_OPERATIONS = (
    "input matte qualify provider mesh topology uv_bake_orm export face skin act voice phys appearance "
    "vqa3d release publish registry_resolve registry_put assemble render geometry_certificate "
    "worker_load worker_query worker_unload artifact_put artifact_open gate_leg test"
).split()


def digest(raw):
    return sha256(raw).hexdigest()


def pinned_bytes(path, expected):
    raw = path.read_bytes()
    if digest(raw) != expected:
        raise ValueError("pin_mismatch")
    return raw


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def check_artifact(ref, authority, blob):
    require(ref["locator"] == "af://sha256/" + ref["sha256"], "locator_digest")
    require((ref["read_authority"], ref["lifetime_id"]) ==
            (authority["reader"], authority["lifetime_id"]) and authority["live"], "read_authority")
    with blob.open("rb") as stream:
        raw = stream.read()
    require(digest(raw) == ref["sha256"] and len(raw) == ref["size_bytes"], "reopen")


def check_scheduling_intent(record, witnesses):
    require({key: record[key] for key in INTENT_KEYS} == witnesses["scheduling_intent"], "intent_binding")


def check_stage(record, witnesses):
    check_scheduling_intent(record, witnesses)
    native = witnesses["stage"]
    validate_stage(native)
    if native["rc"] == 0:
        identity = {key: native[key] for key in
                    ("stage", "inputs", "parameters", "previous", "toolchain_sha256")}
        require(native["key"] == core.sha256_json(identity), "stage_key")
    require(core.sha256_json(native) == record["native_receipt_sha256"], "native_receipt")
    require(record["execution_scope"] == witnesses["scope"], "scope_binding")
    for field in ("stage", "rc", "status", "reason_codes"):
        require(record[field] == native[field], "stage_binding")
    require(record["stage_verdict"] == native["verdict"], "stage_verdict")
    previous = native["previous"]
    require(record["previous_sha256"] == (previous and previous["sha256"]), "stage_link")
    for key in ("inputs", "outputs"):
        require(record[key + "_sha256"] == [item["sha256"] for item in native[key]], "stage_binding")
    subject = next((item["sha256"] for item in native["outputs"] if item["path"] == native["subject"]), None)
    require(record["subject_sha256"] == subject and record["policy_sha256"] == witnesses["policy_sha256"],
            "stage_binding")
    decision = witnesses["asset_decision"]
    if decision is None:
        require(record["asset_receipt_sha256"] is None and record["asset_verdict"] == "CANNOT_SEE",
                "asset_verdict_binding")
    else:
        require(record["asset_receipt_sha256"] == core.sha256_json(decision)
                and record["asset_verdict"] == decision["verdict"]
                and decision["subject_sha256"] == subject, "asset_verdict_binding")
    require(record["transport_verdict"] == witnesses["transport_verdict"], "transport_binding")


def check_request(request, witnesses):
    check_scheduling_intent(request, witnesses)
    slot = witnesses["slot_request"]
    validate_slot(slot, slot["slot"], "request")
    require(request["slot_request_sha256"] == core.sha256_json(slot), "slot_binding")
    for field in ("provider_id", "provider_revision", "request_id"):
        require(request[field] == slot[field], "request_binding")
    require(request["seed"] == slot["params"]["seed"]
            and request["derivative_sha256"] == slot["inputs"][0]["sha256"], "request_binding")
    for field in ("candidate_sha256", "intent_sha256", "output_contract_sha256"):
        require(request[field] == witnesses[field], "admission_binding")
    require(request["execution_scope"] == witnesses["scope"], "scope_binding")


def check_provider(record, witnesses, blob):
    if record["kind"] == "request":
        check_request(record, witnesses)
        return
    request = witnesses["request"]
    check_request(request, witnesses)
    require(record["request_sha256"] == core.sha256_json(request)
            and record["request_id"] == request["request_id"], "request_binding")
    require(record["execution_scope"] == request["execution_scope"], "scope_binding")
    result = witnesses["slot_result"]
    validate_slot(result, result["slot"], "result")
    require(record["slot_result_sha256"] == core.sha256_json(result)
            and all(result[k] == request[k] for k in ("provider_id", "provider_revision", "request_id"))
            and record["rc"] == result["rc"] and record["status"] == result["status"], "result_binding")
    if record["rc"] == 0:
        require(record["artifact"]["sha256"] in [item["sha256"] for item in result["outputs"]
                if item["role"] == "mesh_glb"], "result_binding")
        check_artifact(record["artifact"], witnesses, blob)


def check_telemetry(record, witnesses):
    if record["kind"] == "checkpoint":
        require(all(record[k] == 0 for k in ("ring_drops", "unregistered_drops", "invalid_drops")),
                "capture_drops")
        for key, expected in witnesses["checkpoint_join"].items():
            require(record[key] == expected, "trace_join")
        return
    # The timing oracle needs the factory span even for a structurally valid wire event.
    require("extension" in record, "missing_span")
    span = record["extension"]
    for key, expected in witnesses["trace_join"].items():
        require(record[key] == expected, "trace_join")
    for key, expected in witnesses["span_join"].items():
        require(span[key] == expected, "trace_join")
    require(record["copy_bytes"] == span["copied_bytes"]["value"], "copy_binding")
    require(span["end_ns"] >= span["start_ns"] and span["dropped"] == 0, "invalid_span")
    for sample in (span["placement_entry"], span["placement_work"]):
        require(sample["nice"] == 15 and sample["cpus"] == list(range(16)), "placement")
    require(span["parent_span_id"] != span["span_id"], "trace_join")
    for period in ("psi_pre", "psi"):
        for value in span[period].values():
            require(value["value"] is not None, "missing_psi")


def check_lease(lease, witnesses):
    current = witnesses["lease_identity"]
    for field, expected in current.items():
        require(lease[field] == expected, "lease_identity")
    require(lease["issued_ns"] < lease["expires_ns"], "lease_interval")
    require(lease["state"] == witnesses["lease_state"], "lease_state")
    if lease["state"] == "active":
        require(lease["issued_ns"] <= witnesses["now_ns"] < lease["expires_ns"], "lease_expired")
        require(witnesses["live"] and witnesses["admitted"], "lease_authority")
    else:
        require(lease["issued_ns"] <= lease["closed_ns"] <= witnesses["now_ns"], "lease_interval")


class TestSTContract(unittest.TestCase):
    def setUp(self):
        manifest = CONTRACT / "pins.json"
        self.assertTrue(manifest.is_file(), "ST-CONTRACT pins.json missing")
        self.pins = core.load_json(manifest, max_bytes=2**20)

    def validators(self):
        schemas = {}
        for key, entry in self.pins["boundaries"].items():
            path = ROOT / entry["path"]
            self.assertEqual(path, CONTRACT / "schemas" / (key + ".schema.json"))
            pinned_bytes(path, entry["sha256"])
            schema = core.load_json(path, max_bytes=2**20)
            self.assertEqual(schema["$id"], entry["schema_id"])
            self.assertEqual(schema["$id"], SCHEMA_IDS[key])
            Draft202012Validator.check_schema(schema)
            schemas[key] = schema
        registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
        return {k: Draft202012Validator(s, registry=registry) for k, s in schemas.items()}

    def check_owner_pins(self, root, inventory):
        root = root.resolve()
        active_paths = set()
        for item in inventory:
            if item["binding"] in {"frozen", "integrated"}:
                path = (root / item["path"]).resolve()
                self.assertTrue(path.is_relative_to(root))
                self.assertNotIn(path.name, {"auth.json", ".netrc", ".npmrc"})
                require(path not in active_paths, "duplicate_owner_pin")
                active_paths.add(path)
                pinned_bytes(path, item["sha256"])

    def check_base_owner_pin(self, item):
        path = normpath(item["path"])
        base = self.pins["base_commit"]
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "show", f"{base}:./{path}"],
                cwd=ROOT,
                env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""},
                capture_output=True,
                check=True,
                timeout=10,
            )
        except FileNotFoundError:
            self.skipTest("git executable unavailable for offline base-object verification")
        require(digest(result.stdout) == item["sha256"], "base_pin_mismatch")

    def test_base_claiming_owner_pins(self):
        for item in self.pins["owner_inventory"]:
            if item["binding"] in {"frozen", "base-only"}:
                with self.subTest(path=item["path"], binding=item["binding"]):
                    self.check_base_owner_pin(item)

    def test_changed_base_owner_digest(self):
        owner = next(item for item in self.pins["owner_inventory"]
                     if item["binding"] == "frozen")
        claimed = owner["sha256"]
        changed = ("0" if claimed[0] != "0" else "1") + claimed[1:]
        with self.assertRaisesRegex(ValueError, "^base_pin_mismatch$"):
            self.check_base_owner_pin(dict(owner, sha256=changed))

    def test_missing_base_owner_blob(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.check_base_owner_pin({
                "path": "asset_factory/core.py/missing",
                "sha256": digest(b""),
            })

    def test_resolve_five_boundaries(self):
        self.assertEqual(self.pins["base_commit"], "81b4845a12e6df6e91a2582618628a0dbfef6d5d")
        self.assertEqual(set(self.validators()), BOUNDARIES)
        self.check_owner_pins(ROOT, self.pins["owner_inventory"])
        for doc in ("semantics.md", "preregistration.md"):
            pinned_bytes(CONTRACT / doc, self.pins["documents"][doc])
        self.assertEqual(core.sha256_json(self.pins["semantic_fixtures"]), self.pins["fixtures_sha256"])

    def test_changed_integrated_owner_byte(self):
        owner = next(item for item in self.pins["owner_inventory"] if item["binding"] == "integrated")
        raw = pinned_bytes(ROOT / owner["path"], owner["sha256"])
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            changed = root / owner["path"]
            changed.parent.mkdir(parents=True)
            changed.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            with self.assertRaisesRegex(ValueError, "^pin_mismatch$"):
                self.check_owner_pins(root, [owner])

    def test_duplicate_active_owner_pin(self):
        owner = next(item for item in self.pins["owner_inventory"] if item["binding"] == "integrated")
        for alias in ("./" + owner["path"], "slots/../" + owner["path"]):
            with self.subTest(alias=alias):
                with self.assertRaisesRegex(ValueError, "^duplicate_owner_pin$"):
                    self.check_owner_pins(ROOT, [owner, dict(owner, path=alias, binding="frozen")])

    def test_toro_event_contract(self):
        validator = self.validators()["telemetry"]
        event = self.pins["semantic_fixtures"]["records"]["trace"]
        validator.validate(event)
        for field in TORO_COMMON + TORO_EVENT:
            with self.subTest(missing=field):
                self.assertFalse(validator.is_valid({k: v for k, v in event.items() if k != field}))
        event_branch = next(branch for branch in validator.schema["oneOf"]
                            if branch["properties"]["kind"] == {"const": "event"})
        self.assertCountEqual(event_branch["properties"]["operation"]["enum"], FABRICA_OPERATIONS)
        self.assertTrue(validator.is_valid({key: event[key] for key in TORO_COMMON + TORO_EVENT}))
        for op in FABRICA_OPERATIONS:
            with self.subTest(operation=op):
                self.assertTrue(validator.is_valid(dict(event, operation=op)))
        for override in ({"operation": "invented"}, {"component": "selector"}, {"session_id": "invalid"},
                         {"stage": "provider"}, {"sequence": 0}, {"cpuset": "0-15"},
                         {"copy_bytes": 1, "source": None}, {"kind": "checkpoint"}):
            with self.subTest(override=override):
                self.assertFalse(validator.is_valid(dict(event, **override)))

    def test_toro_checkpoint_contract(self):
        validator = self.validators()["telemetry"]
        checkpoint = {"schema": "af.telemetry/v1", "kind": "checkpoint",
                      "session_id": "12345678-abcd-4567-8123-123456789abc", "git_sha": None,
                      "cpuset": None, "process_id": 7, "timestamp_ns": 301, "final": True,
                      "record_count": 1, "ring_drops": 0, "unregistered_drops": 0, "invalid_drops": 0}
        self.assertTrue(validator.is_valid(checkpoint))
        for key in checkpoint:
            with self.subTest(missing=key):
                self.assertFalse(validator.is_valid({k: v for k, v in checkpoint.items() if k != key}))
        self.assertFalse(validator.is_valid(dict(checkpoint, operation="provider")))

    def test_scheduling_intent_required(self):
        validators = self.validators()
        records = self.pins["semantic_fixtures"]["records"]
        for boundary, name in (("stage", "stage"), ("provider", "request")):
            validator = validators[boundary]
            for order in (None, 0, 2):
                with self.subTest(boundary=boundary, order=order):
                    self.assertTrue(validator.is_valid(dict(records[name], expected_order=order)))
            for override in ({"expected_order": -1}, {"priority": "high"}, {"consumer_id": " "},
                             {"deadline_ns": -1}, {"affinity_key": ""}):
                with self.subTest(boundary=boundary, override=override):
                    self.assertFalse(validator.is_valid(dict(records[name], **override)))
            for field in INTENT_KEYS:
                with self.subTest(boundary=boundary, missing=field):
                    incomplete = {k: v for k, v in records[name].items() if k != field}
                    self.assertFalse(validators[boundary].is_valid(incomplete))

    def test_lease_epoch_required(self):
        lease = self.pins["semantic_fixtures"]["records"]["lease"]
        self.assertFalse(self.validators()["lease"].is_valid({k: v for k, v in lease.items()
                                                              if k != "lease_epoch"}))

    def test_changed_schema_byte(self):
        entry = self.pins["boundaries"]["stage"]
        with TemporaryDirectory() as tmp:
            changed = Path(tmp) / "stage.json"
            raw = pinned_bytes(ROOT / entry["path"], entry["sha256"])
            changed.write_bytes(raw + b" ")
            with self.assertRaisesRegex(ValueError, "^pin_mismatch$"):
                pinned_bytes(changed, entry["sha256"])

    def test_native_publisher_preserves_verdicts(self):
        legacy = self.pins["semantic_fixtures"]["witnesses"]["legacy_publish_receipt"]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            for asset in (None, "FAIL", "CANNOT_SEE"):
                rows = [{"destination": "_review/fixture", "verdict": asset}] if asset else []
                path.write_text(core.canonical_json(dict(legacy, asset_verdicts=rows)))
                normalized = publisher.read_receipt(path)
                self.assertEqual(normalized["transport_verdict"], "PASS")
                self.assertEqual(normalized["asset_verdict"], asset)
                if asset:
                    prefix = publisher.asset_prefix(dict(asset_class="prop", asset_id="fixture",
                                                         version="v1", final_verdict=asset))
                    self.assertTrue(prefix.startswith("_review/"))

    def test_semantic_fixtures(self):
        validators = self.validators()
        fixtures = self.pins["semantic_fixtures"]
        self.assertEqual(core.sha256_json(fixtures), self.pins["fixtures_sha256"])
        seen = set()
        positive, negative = set(), set()
        with TemporaryDirectory() as tmp:
            blob = Path(tmp) / "payload"
            for case in fixtures["cases"]:
                name = case["id"]
                with self.subTest(case=name):
                    self.assertNotIn(name, seen)
                    seen.add(name)
                    key = case["boundary"]
                    value = deepcopy(fixtures["records"][case["record"]])
                    value.update(case.get("replace", {}))
                    if "extension_replace" in case:
                        value["extension"].update(case["extension_replace"])
                    for field in case.get("remove", []):
                        del value[field]
                    witnesses = deepcopy(fixtures["witnesses"])
                    witnesses.update(case.get("witness_replace", {}))
                    blob.write_bytes(witnesses["payload_utf8"].encode("utf-8"))
                    def verify():
                        if not validators[key].is_valid(value):
                            raise ValueError("schema_invalid")
                        if key == "artifact":
                            check_artifact(value, witnesses, blob)
                        elif key == "provider":
                            check_provider(value, witnesses, blob)
                        else:
                            {"stage": check_stage, "telemetry": check_telemetry,
                             "lease": check_lease}[key](value, witnesses)
                    if case["expect"] == "accept":
                        verify()
                        positive.add(key)
                    else:
                        with self.assertRaisesRegex(ValueError, "^" + case["expect"] + "$"):
                            verify()
                        negative.add(key)
        self.assertEqual(positive, BOUNDARIES)
        self.assertEqual(negative, BOUNDARIES)
        self.assertTrue({"fixture-as-production", "transport-as-asset", "changed-candidate-revision",
                         "replaced-backing", "expired-lease", "missing-psi"} <= seen)
