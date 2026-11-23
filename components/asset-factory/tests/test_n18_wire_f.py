"""Exercise the ViewPoint wire exchange against an actual local registry."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_d18_reg_store as reg_fixtures
import test_d18_vpfix_wire as vpfix
from tools.registry import Registry
from viewpoint import handlers


class WireFTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        vpfix.OwnerWireTests.setUpClass()
        cls.owner = vpfix.OwnerWireTests.owner

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "registry"
        self.root.mkdir()
        self.writer = Registry(self.root, namespace="studio")
        self.initial = self.writer.initialize()
        self.taxonomy, self.provenance = b"taxonomy", b"source identity"
        parent, parent_blobs = reg_fixtures.RegistryTests.fixture(self, b"parent mesh")
        parent_snapshot = self.writer.put("parent", parent, blobs=parent_blobs,
                                          expected_snapshot=self.initial)
        raw, blobs = reg_fixtures.RegistryTests.fixture(self, b"first mesh", parent=parent)
        record = json.loads(raw)
        record["annotations"].update(pipeline_scope="fixture", pipeline_asset_verdict="PASS")
        self.raw = reg_fixtures.encode(record)
        self.snapshot = self.writer.put("hero", self.raw, blobs=blobs, tag="candidate",
                                        expected_snapshot=parent_snapshot, expected_binding=None)
        self.version = reg_fixtures.pin(self.raw)
        self.locator = "af://studio/hero@" + self.version
        self.reader = Registry(self.root, namespace="studio")

    def request(self, *, locator=None, pin=None):
        request = vpfix.request("af.registry.resolve", "wire-f")
        request["args"]["inputs"] = [dict(
            vpfix.reference("locator", (pin or self.version).removeprefix("sha256:")),
            locator=locator or self.locator,
        )]
        return request

    def exchange(self, request, code):
        reply = handlers.LocalDryRun(registry=self.reader).handle(request)
        for contract in ("vp.control.reply/v1", "vp.oflane.reply/v1"):
            self.owner.validate_contract(contract, reply)
        self.owner.validate_adapter_exchange(request, reply)
        self.assertEqual(reply["code"], code)
        self.assertEqual(reply["ok"], code == "OK")
        if code != "OK":
            self.assertEqual((reply["result"], reply["evidence"]), ({}, []))
        return reply

    def cli(self, request):
        output = io.StringIO()
        rc = handlers.main(["--dry-run", "--registry-root", str(self.root),
                            "--registry-namespace", "studio"],
                           io.StringIO(json.dumps(request)), output)
        return rc, json.loads(output.getvalue())

    def test_exact_lookup_returns_read_only_owner_schema_closure(self):
        expected = self.reader.resolve(self.locator, snapshot=self.snapshot)
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        with patch.object(self.reader, "put", side_effect=AssertionError("registry write")):
            reply = self.exchange(self.request(), "OK")
        after = {p.relative_to(self.root): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        result = reply["result"]
        gate = json.loads(result["gate"])
        self.assertEqual(gate["asset_name"], "studio/hero")
        self.assertEqual(gate["version"], self.version)
        self.assertEqual(gate["snapshot"], self.snapshot)
        self.assertEqual(gate["content_digests"], expected["objects"])
        self.assertTrue(expected["lineage_records"])
        self.assertEqual(set(gate["gate_verdicts"]), {self.version, *expected["lineage_records"]})
        self.assertEqual((gate["transport_verdict"], gate["production_verdict"],
                          gate["pipeline_asset_verdict"]), ("PASS", "CANNOT_SEE", "PASS"))
        self.assertEqual(result["receipt"], {"path": self.locator, "sha256": self.version[7:]})
        self.assertEqual(reply["evidence"], [result["receipt"]])
        ref, = result["artifact_refs"]
        self.assertEqual((ref["locator"], ref["sha256"], ref["bytes"], ref["copy"]),
                         (self.locator, self.version[7:], len(self.raw), "none"))
        self.assertEqual(self.cli(self.request()), (0, reply))

    def test_tag_resolves_in_the_requested_historical_snapshot(self):
        newer, blobs = reg_fixtures.RegistryTests.fixture(self, b"second mesh")
        current = self.writer.put("hero", newer, blobs=blobs, tag="candidate",
                                  expected_snapshot=self.snapshot, expected_binding=self.version)
        name = "af://studio/hero:candidate"
        old = self.exchange(self.request(locator=name, pin=self.snapshot), "OK")
        new = self.exchange(self.request(locator=name, pin=current), "OK")
        old_gate = json.loads(old["result"]["gate"])
        self.assertEqual((old_gate["version"], old_gate["requested_tag"]), (self.version, "candidate"))
        self.assertEqual(json.loads(new["result"]["gate"])["version"], reg_fixtures.pin(newer))

    def test_verified_gate_with_service_evidence_stays_distinct_from_production(self):
        evidence = reg_fixtures.base_fixtures.EvidenceTests()
        evidence.setUp()
        decision = evidence.assess()
        self.assertEqual(decision["rc"], 0, decision)
        gate = decision["gate"]
        record = evidence.make_record("asset", gate)
        record["files"][0]["size_bytes"] = len(b"subject")
        blobs = {pin: raw for (_, pin), raw in evidence.store.items()}
        blobs.update({reg_fixtures.pin(raw): raw for raw in
                      (b"subject", b"policy", b"domain", b"eval", b"tool", b"method", b"evidence")})
        record["dependencies"] = sorted(blobs)
        raw = reg_fixtures.encode(record)
        version = reg_fixtures.pin(raw)
        scopes = {(gate["gate_id"], gate["subject_digest"]): {gate["profile_digest"]}}
        policy = dict(evidence_policy=lambda _: scopes,
                      read_checked=lambda role, pin: evidence.store.get((role, pin)))
        Registry(self.root, namespace="studio", **policy).put(
            "verified", raw, blobs=blobs, expected_snapshot=self.snapshot,
        )
        request = self.request(locator="af://studio/verified@" + version, pin=version)
        self.exchange(request, "REGISTRY_STALE")
        self.reader = Registry(self.root, namespace="studio", **policy)
        result = self.exchange(request, "OK")["result"]
        closure = json.loads(result["gate"])
        self.assertEqual(closure["gate_verdicts"][version], [{
            "gate_id": gate["gate_id"], "receipt_digest": gate["receipt_digest"], "verdict": "PASS",
        }])
        self.assertEqual(closure["production_verdict"], "CANNOT_SEE")

    def test_unknown_name_or_namespace_refuses_without_disclosure(self):
        for name in ("af://studio/unknown@", "af://elsewhere/hero@"):
            with self.subTest(name=name):
                self.exchange(self.request(locator=name + self.version), "REGISTRY_NAME_UNAVAILABLE")

    def test_unknown_snapshot_is_stale_at_the_cli(self):
        request = self.request(locator="af://studio/hero:candidate", pin="sha256:" + "0" * 64)
        self.assertEqual(self.cli(request), (4, self.exchange(request, "REGISTRY_STALE")))

    def test_corrupt_object_prevents_closure_success(self):
        blob = reg_fixtures.pin(b"first mesh")
        (self.root / "blobs" / "sha256" / blob[7:]).write_bytes(b"other mesh")
        self.exchange(self.request(), "REGISTRY_STALE")

    def test_non_dry_run_and_traversal_refuse_before_registry_read(self):
        with patch.object(self.reader, "resolve", side_effect=AssertionError("unexpected read")):
            self.exchange(self.request() | {"dry_run": False}, "DRY_RUN_REQUIRED")
            self.exchange(self.request(locator="af://studio/../hero@" + self.version), "INVALID_ARGS")

    def test_file_locator_refuses_even_inside_allowed_artifact_root(self):
        path = self.root / "CURRENT"
        request = self.request(locator=path.as_uri())
        with patch.object(self.reader, "resolve", side_effect=AssertionError("unexpected read")):
            reply = handlers.LocalDryRun(allowed_root=self.root, registry=self.reader).handle(request)
        self.owner.validate_adapter_exchange(request, reply)
        self.assertEqual(reply["code"], "INVALID_ARGS")

    def test_extra_resolve_argument_refuses_before_registry_read(self):
        request = self.request()
        request["args"]["seed"] = 7
        with patch.object(self.reader, "resolve", side_effect=AssertionError("unexpected read")):
            reply = handlers.LocalDryRun(registry=self.reader).handle(request)
        self.assertEqual(reply["code"], "INVALID_ARGS")

    def test_resolve_row_with_write_policy_refuses_before_registry_read(self):
        rows = handlers._wire_rows()
        changed = {name: dict(row) for name, row in rows.items()}
        changed["af.registry.resolve"]["risk_class"] = "enviar"
        with patch.object(handlers, "_wire_rows", return_value=changed), \
                patch.object(self.reader, "resolve", side_effect=AssertionError("unexpected read")):
            self.exchange(self.request(), "POLICY_MISMATCH")

    def test_other_stage_stays_on_hold_without_registry_read(self):
        with patch.object(self.reader, "resolve", side_effect=AssertionError("unexpected read")):
            self.exchange(vpfix.request("af.registry.put"), "HOLD")


if __name__ == "__main__":
    unittest.main()
