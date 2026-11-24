# SPDX-License-Identifier: AGPL-3.0-only
"""Binding admission and passive discovery regressions using host-owned fixtures."""

import copy
import datetime
import io
import itertools
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from slot_host.runtime import binding, conformance_authority, tools
from slot_host.runtime.tests.binding_fixtures import fixture_binding_options, fixture_policy
from slot_host.runtime.tests.test_live_park import _manifest

_CANONICAL_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
_CANONICAL_ID = "https://metaviewer.local/contracts/v0.2/attempt_evidence_record.schema.json"
_CANONICAL_SHA = "b3bfd6715abfa60a0866b655d23088d8bee09b0cf8e0a3b44c156e5692c19d8c"


class BindingPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.executable = Path(temp.name) / "fixture"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.manifest = _manifest(binding.sha256_of_file(self.executable))
        self.ref = "installation:fixture-slot@binding-policy"
        self.serial = itertools.count()
        self.launch = self.enterContext(patch.object(tools, "run_bound"))
        self.authorize = self.enterContext(patch.object(tools, "authorize_live", return_value=True))
        self.retention_original = getattr(binding, "_retention_available", lambda _: False)
        self.enterContext(patch.object(
            binding, "_retention_available", return_value=True, create=True,
        ))
        self.addCleanup(self.launch.assert_not_called)

    def _dispatch(self, manifest, *, policy=None, dry_run=True, payload=None):
        return tools.dispatch(
            "behaviour.record",
            {"request_id": f"req-binding-policy-{self._testMethodName}-{next(self.serial)}",
             "dry_run": dry_run, "payload": {"installation_ref": self.ref, **(payload or {})}},
            host={self.ref: (manifest, self.executable)},
            host_policy=fixture_policy(manifest) if policy is None else policy,
        )

    def _refusal(self, code, *args, **kwargs):
        with self.assertRaises(tools._ControlError) as refusal:
            self._dispatch(*args, **kwargs)
        self.assertEqual(refusal.exception.code, code)
        return str(refusal.exception)

    def _inspect(self, manifest, *, policy=None, payload=None, executable=True):
        return tools.dispatch(
            "slot.inspect",
            {"request_id": f"req-inspect-policy-{self._testMethodName}-{next(self.serial)}",
             "payload": {"installation_ref": self.ref, **(payload or {})}},
            host={self.ref: {"manifest": manifest, "executable_path": self.executable if executable else None}},
            host_policy=policy,
        )["result"]

    def test_public_grants_and_missing_binding_records_refuse_external_process(self):
        options = fixture_binding_options(self.manifest, "record", {
            "host_grants": {"grant.exec.external_process"}, "installation_ref": self.ref,
            "allow_pending_evidence": True,
        })
        for field, reason in {
            "auth_grants": "grant_refused",
            "auth_spawn_granted": "grant_refused",
            "auth_slot_id": "manifest_invalid",
            "auth_manifest_digest": "manifest_invalid",
            "installation_ref": "manifest_invalid",
            "auth_registry": "profile_not_admitted",
            "auth_licence_disposition": "availability_blocked",
        }.items():
            with self.subTest(field=field):
                with self.assertRaises(binding.BindingRejected) as refusal:
                    binding.verify_binding(self.manifest, self.executable, operation_id="record",
                                           **(options | {field: None}))
                self.assertEqual(refusal.exception.reason, reason)

    def test_external_grant_and_spawn_declarations_are_required_on_live_and_preview(self):
        policy = fixture_policy(self.manifest)
        for field in ("required_grants", "required_effects"):
            manifest = copy.deepcopy(self.manifest)
            manifest["operations"][0][field] = []
            for preview in (False, True):
                with self.subTest(field=field, preview=preview):
                    self._refusal("SCHEMA_INVALID", manifest, dry_run=preview)
        for preview in (False, True):
            with self.subTest(preview=preview):
                self._refusal("NO_LEASE", self.manifest, dry_run=preview,
                              policy=policy | {"grants": []},
                              payload={"host_grants": ["grant.exec.external_process"]})
                self._refusal("NO_LEASE", self.manifest, dry_run=preview,
                              policy=policy | {"spawn_granted": None})
        self.authorize.assert_not_called()

    def test_registry_binds_provider_adapter_and_role_identity(self):
        policy = fixture_policy(self.manifest)
        self.assertTrue(self._dispatch(self.manifest, policy=policy)["result"]["planned"])
        for field in ("provider_id", "adapter_id", "role", "request_digest", "result_digest"):
            with self.subTest(field=field):
                for substitute in (None, "foreign"):
                    registry = dict(policy["registry"])
                    if substitute is None:
                        del registry[field]
                    else:
                        registry[field] = substitute
                    self._refusal("PROFILE_NOT_ADMITTED", self.manifest, policy=policy | {"registry": registry})

    def test_pending_and_unresolved_references_refuse_live_and_preview(self):
        for ref in ("evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending",
                    "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v1"):
            manifest = copy.deepcopy(self.manifest)
            manifest["evidence"]["shared_schema_ref"] = ref
            policy = fixture_policy(manifest) | {"evidence_ref": _CANONICAL_REF, "evidence_sha256": _CANONICAL_SHA}
            for preview in (False, True):
                with self.subTest(ref=ref, preview=preview):
                    self._refusal("PROFILE_NOT_ADMITTED", manifest, policy=policy, dry_run=preview)
        self.authorize.assert_not_called()

    def test_canonical_digest_and_matching_evidence_version_are_required(self):
        policy = fixture_policy(self.manifest)
        self.assertTrue(self._dispatch(self.manifest, policy=policy | {"evidence_ref": _CANONICAL_ID})["result"]["planned"])
        for wrong in (
            {"evidence_sha256": None},
            {"evidence_sha256": "99db48313f14d8c9a9d709b9a390bbc6b34c41934983b6f273bf4c3b37d7ee1b"},
            {"evidence_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.3",
             "evidence_sha256": "9402c78a4780cab53d9ad9726b103d05e76b504fe693c16b572078c9d8c"},
        ):
            with self.subTest(wrong=wrong):
                self._refusal("PROFILE_NOT_ADMITTED", self.manifest, policy=policy | wrong)

    def test_canonical_pin_without_tools_t01_refuses_live_and_preview(self):
        self.manifest["evidence"]["shared_schema_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.3"
        with patch.object(binding, "_retention_available", new=self.retention_original):
            for preview in (False, True):
                with self.subTest(preview=preview):
                    detail = self._refusal("PROFILE_NOT_ADMITTED", self.manifest, dry_run=preview)
                    self.assertIn("owner-pending: T01", detail)
        self.authorize.assert_not_called()

    def test_v02_live_projection_refuses_before_tools_or_spawn(self):
        with (
            patch.object(binding, "_retention_available", new=self.retention_original),
            patch.object(conformance_authority, "tools_retention_available", return_value=True) as retention,
        ):
            for ref in (_CANONICAL_REF, _CANONICAL_ID):
                self.manifest["evidence"]["shared_schema_ref"] = ref
                for preview in (False, True):
                    with self.subTest(ref=ref, preview=preview):
                        detail = self._refusal("PROFILE_NOT_ADMITTED", self.manifest, dry_run=preview)
                        self.assertIn("owner-pending: T02", detail)
            retention.assert_not_called()
        self.authorize.assert_not_called()

    def test_current_installation_probe_required_and_static_inspection_is_passive(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["capabilities"]["prerequisite_probes"] = [
            {"id": "probe-1", "argv": ["probe"], "effects": [], "requires_binding": True},
        ]
        now = datetime.datetime.now(datetime.timezone.utc)
        record = {"installation_ref": self.ref, "outcome": "passed",
                  "measured_at": (now - datetime.timedelta(seconds=60)).isoformat(),
                  "expires_at": (now + datetime.timedelta(seconds=60)).isoformat()}
        policy = fixture_policy(manifest) | {"probes": {"probe-1": record}}
        self.assertTrue(self._dispatch(manifest, policy=policy)["result"]["planned"])
        self.assertEqual(self._inspect(manifest, policy=policy)["readiness"]["availability"], "admitted")
        for witness in (
            "passed", None, record | {"outcome": "failed"},
            record | {"installation_ref": "installation:other-slot@fixture"},
            record | {"expires_at": record["measured_at"]},
            record | {"measured_at": record["expires_at"]},
            record | {"measured_at": "invalid"},
        ):
            with self.subTest(witness=witness):
                denied = policy | {"probes": {"probe-1": witness}}
                self._refusal("AVAILABILITY_BLOCKED", manifest, policy=denied, dry_run=False)
                inspected = self._inspect(manifest, policy=denied)
                self.assertTrue(inspected["validation"]["ok"])
                self.assertEqual(inspected["readiness"]["availability"], "blocked")

    def test_foreign_installation_rejected_for_target_in_execution_and_inspection(self):
        self.assertTrue(self._dispatch(self.manifest, payload={"slot_id": "fixture-slot"})["result"]["planned"])
        foreign = copy.deepcopy(self.manifest)
        foreign["slot_id"] = "other-slot"
        target = {"slot_id": "fixture-slot"}
        self.assertIn("requested slot", self._refusal("SCHEMA_INVALID", foreign, payload=target))
        with self.assertRaises(tools._ControlError) as refusal:
            self._inspect(foreign, payload=target)
        self.assertEqual(refusal.exception.code, "SCHEMA_INVALID")

    def test_static_manifest_schema_is_validated_before_execution_and_inspection(self):
        invalid = copy.deepcopy(self.manifest)
        invalid["record_replay"]["freshness_inputs"] = "not-an-array"
        self._refusal("SCHEMA_INVALID", invalid)
        with self.assertRaises(tools._ControlError) as refusal:
            self._inspect(invalid)
        self.assertEqual(refusal.exception.code, "SCHEMA_INVALID")
        self.authorize.assert_not_called()

    def test_removed_executable_reports_not_installed_with_static_plan(self):
        self.executable.unlink()
        for executable in (True, False):
            with self.subTest(executable=executable):
                result = self._inspect(self.manifest, executable=executable)
                self.assertTrue(result["validation"]["ok"])
                self.assertEqual(result["plan"], {
                    "operation_id": "record", "role": "replay", "transport_kind": "cli_argv",
                })
                self.assertEqual(result["readiness"], {"ready": False, "availability": "not_installed"})
        self._refusal("AVAILABILITY_NOT_INSTALLED", self.manifest, dry_run=False)
        payload = {"request_id": "req-binding-policy-cli-absent",
                   "payload": {"installation_ref": self.ref, "operation_id": "record"}}
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            rc = tools.main(
                ["mini", "behaviour.record", "--args", json.dumps(payload)],
                dispatch_fn=lambda tool, arguments, **kwargs: tools.dispatch(
                    tool, arguments, host={self.ref: (self.manifest, self.executable)},
                    host_policy=fixture_policy(self.manifest), **kwargs,
                ),
            )
        self.assertEqual(rc, 16)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(error.getvalue().startswith("availability_not_installed:"))
        self.authorize.assert_not_called()

    def test_declared_mcp_alternate_is_parked_while_primary_cli_is_admitted(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["transport"]["alternates"] = [
            {"kind": "mcp_stdio", "argv_prefix": ["mcp"], "framing": "jsonrpc_stdio"},
        ]
        manifest["operations"][1]["binding"] = {"kind": "mcp_tool", "tool": "fixture.replay"}
        policy = fixture_policy(manifest)
        self.assertTrue(self._dispatch(manifest, policy=policy)["result"]["planned"])
        result = self._inspect(manifest, policy=policy, payload={"operation_id": "replay"})
        self.assertEqual(result["plan"]["transport_kind"], "mcp_stdio")
        self.assertEqual(result["readiness"]["availability"], "parked")
        self._refusal("AVAILABILITY_PARKED", manifest, payload={"operation_id": "replay"}, dry_run=False)

    def test_missing_or_invalid_perception_ceiling_refuses_live_and_preview(self):
        policy = fixture_policy(self.manifest)
        del policy["perception_ceiling"]
        for configuration in ({}, {"perception_ceiling": None}, {"perception_ceiling": "invalid"}):
            for preview in (True, False):
                with self.subTest(configuration=configuration, preview=preview):
                    self._refusal("PERCEPTION_DENIED", self.manifest, policy=policy | configuration, dry_run=preview)
        self.authorize.assert_not_called()
