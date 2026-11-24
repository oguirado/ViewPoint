# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the MAIN prerequisite through the signed-policy live gate."""

import copy
import datetime
import hashlib
import inspect
import json
import os
import tempfile
import unittest
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from slot_host.runtime import binding as binding_runtime
from slot_host.runtime.conformance_authority import (
    MAIN_ENROLMENT_DENY_REASON,
    TOOLS_RETENTION_DENY_REASON,
    create_fixture_receipt_for_binding,
)
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED, make_conformance_authority
from slot_host.runtime.runner import run_bound
from slot_host.runtime.tests.test_conformance_authority import (
    _KIT,
    _NOW,
    _REGISTRY_GEN,
    _bind,
    _commit_policy,
    _manifest,
    _policy,
    _write_fixture,
)


_REF = "enrollment:fixture@1"
_START = "2026-06-01T00:00:00Z"
_EXPIRES = "2026-06-01T00:30:00Z"


class MainEnrolmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "rea-enrolment"
        self.directory.mkdir()
        self.exe = self.root / "tool.py"
        self.counter = self.root / "counter"
        implementation = _write_fixture(self.exe, str(self.counter))
        manifest = _manifest(implementation)
        with patch.object(binding_runtime, "_retention_available", return_value=True):
            self.binding = _bind(manifest, self.exe)
        self.policy = _commit_policy(self.root, _policy())
        self.receipt = create_fixture_receipt_for_binding(
            self.binding, "record", conformance_kit_sha256=_KIT,
            registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600,
        )
        provider = manifest["provider_ref"]
        adapter = manifest["capabilities"]["adapter_identity"]
        self.document = {
            "schema": "rea.main_enrolment/v1",
            "slot": "fixture-slot",
            "issued_by": "ViewPoint_MAIN",
            "binding_identity": {
                "slot_id": "fixture-slot",
                "operation_id": "record",
                "installation_ref": "installation:fixture-slot@1",
                "manifest_digest": self.binding.manifest_digest,
                "shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2",
                "shared_schema_sha256": self.binding.shared_schema_sha256,
                **{name: provider[name] for name in (
                    "provider_id", "provider_revision", "provider_sha256", "implementation_sha256",
                )},
                **{name: adapter[name] for name in ("adapter_id", "adapter_sha256")},
                **manifest["contract_refs"][0],
            },
            "policy_row": {
                "control_authority": {"owner": "ViewPoint_MAIN", "route_id": "rea.slot.project"},
                "registry_generation": _REGISTRY_GEN,
                "enrollment_ref": _REF,
                "issued_at": _START,
                "expires_at": _EXPIRES,
                "restrictions": [],
                "revocation": _policy()["revocation"],
            },
        }

    def _publish_bytes(self, raw):
        pin = hashlib.sha256(raw).hexdigest()
        (self.directory / f"{pin}.json").write_bytes(raw)
        return pin

    def _publish_json(self, value):
        return self._publish_bytes(json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\n")

    def _publish(self, document=None, *, attestation=None, carbon=None, raw_transform=None):
        document = copy.deepcopy(self.document if document is None else document)
        inputs = {key: document[key] for key in ("binding_identity", "policy_row", "slot") if key in document}
        canonical = json.dumps(inputs, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        document.setdefault("inputs_sha256", hashlib.sha256(canonical).hexdigest())
        raw = json.dumps(document, ensure_ascii=False, indent=2).encode() + b"\n"
        pin = self._publish_bytes(raw_transform(raw) if raw_transform else raw)
        link = {
            "enrollment_ref": _REF,
            "enrolment_sha256": pin,
            "inputs_sha256": document["inputs_sha256"],
        }
        config = {
            "enrolment_directory": self.directory,
            "enrolment_attestation_ref": self._publish_json(link | {
                "schema": "rea.main_enrolment_attestation/v1", "issued_by": "ViewPoint_MAIN",
            } | (attestation or {})),
            "enrolment_carbon_read_ref": self._publish_json(link | {
                "schema": "rea.main_enrolment_carbon_read/v1", "issued_by": "Carbon", "outcome": "ACCEPT",
            } | (carbon or {})),
        }
        return config, pin

    def _authority(self, config, **options):
        # The parent has no configuration parameters. Run its old default
        # through the same assertions so the contrast fails on gate behavior.
        parameters = inspect.signature(make_conformance_authority).parameters
        supported = {key: value for key, value in config.items() if key in parameters}
        return make_conformance_authority(**({
            "policy": self.policy, "receipts": [self.receipt], "now": _NOW,
        } | options | supported))

    def _assert_parked_at(self, authority, reason, *, binding=None):
        self.assertIs(authority.verify(
            binding=self.binding if binding is None else binding,
            operation_id="record", request_id="req-main-fixture-01", attempt_id="att-main-fixture-01",
        ), False)
        self.assertEqual(authority.last_reason, reason)

    def _valid_control(self):
        config, pin = self._publish()
        self._assert_parked_at(self._authority(config), TOOLS_RETENTION_DENY_REASON)
        return config, pin

    def test_valid_main_fixture_reaches_tools_gate_and_stays_parked(self):
        config, _ = self._valid_control()
        authority = self._authority(config)
        opened = Mock(return_value=True)
        result = run_bound(
            self.binding, [], cwd=self.root, live_authority=authority, attempt_store=opened,
            request_id="req-main-fixture-01", attempt_id="att-main-fixture-01",
        )
        self.assertEqual(authority.last_reason, TOOLS_RETENTION_DENY_REASON)
        self.assertEqual(result["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
        self.assertIs(result["evidence_accepted"], False)
        self.assertIsNone(result["pid"])
        self.assertFalse(self.counter.exists())
        opened.assert_not_called()

    def test_unconfigured_directory_or_witness_refs_deny(self):
        config, _ = self._valid_control()
        for missing in (tuple(config), *((key,) for key in config)):
            with self.subTest(missing=missing):
                options = {key: value for key, value in config.items() if key not in missing}
                self._assert_parked_at(self._authority(options), MAIN_ENROLMENT_DENY_REASON)
        for key in ("enrolment_attestation_ref", "enrolment_carbon_read_ref"):
            with self.subTest(unresolved=key):
                self._assert_parked_at(self._authority(config | {key: "a" * 64}), MAIN_ENROLMENT_DENY_REASON)

    def test_every_file_is_read_and_checked_against_its_exact_pin(self):
        config, enrolment_pin = self._valid_control()
        for pin in (enrolment_pin, config["enrolment_attestation_ref"], config["enrolment_carbon_read_ref"]):
            with self.subTest(pin=pin):
                file = self.directory / f"{pin}.json"
                raw = file.read_bytes()
                file.write_bytes(raw + b"\n")
                try:
                    self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
                finally:
                    file.write_bytes(raw)
        self._assert_parked_at(self._authority(config), TOOLS_RETENTION_DENY_REASON)

    def test_inputs_digest_is_recomputed_even_when_both_witnesses_agree(self):
        self._valid_control()
        document = self.document | {"inputs_sha256": "a" * 64}
        config, _ = self._publish(document)
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)

    def test_independent_carbon_accept_must_bind_the_same_enrolment(self):
        valid, _ = self._valid_control()
        configs = [valid | {"enrolment_carbon_read_ref": valid["enrolment_attestation_ref"]}]
        for changes in ({"issued_by": "ViewPoint_MAIN"}, {"outcome": "REVISE"},
                        {"enrolment_sha256": "a" * 64}, {"inputs_sha256": "a" * 64},
                        {"enrollment_ref": "enrollment:foreign@1"}):
            configs.append(self._publish(carbon=changes)[0])
        for index, config in enumerate(configs):
            with self.subTest(contrast=index):
                self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)

    def test_main_attestation_must_bind_the_activated_reference(self):
        valid, _ = self._valid_control()
        for changes in ({"issued_by": "Carbon"}, {"enrollment_ref": "enrollment:foreign@1"}):
            with self.subTest(attestation=changes):
                config, _ = self._publish(attestation=changes)
                self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        body = _policy()
        body["baseline"]["enrollment_ref"] = "enrollment:replacement@1"
        other_policy = _commit_policy(self.root, body)
        self._assert_parked_at(self._authority(valid, policy=other_policy), MAIN_ENROLMENT_DENY_REASON)

    def test_policy_row_must_match_signed_baseline_and_main_route(self):
        valid, _ = self._valid_control()
        for changes in (
            {"enrollment_ref": "enrollment:foreign@1"}, {"registry_generation": _REGISTRY_GEN + 1},
            {"registry_generation": float(_REGISTRY_GEN)}, {"registry_generation": True},
            {"control_authority": {"owner": "Carbon", "route_id": "rea.slot.project"}},
            {"control_authority": {"owner": "ViewPoint_MAIN", "route_id": "runtime.execute"}},
        ):
            with self.subTest(policy_row=changes):
                config, _ = self._publish(self.document | {"policy_row": self.document["policy_row"] | changes})
                self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        generation = _REGISTRY_GEN + 1
        policy = _commit_policy(self.root, _policy(registry_gen=generation))
        receipt = replace(self.receipt, registry_generation=generation)
        self._assert_parked_at(self._authority(valid, policy=policy, receipts=[receipt]), MAIN_ENROLMENT_DENY_REASON)

    def test_stale_or_not_yet_issued_enrolment_denies(self):
        config, _ = self._valid_control()
        expires = datetime.datetime.fromisoformat(_EXPIRES)
        self._assert_parked_at(self._authority(config, now=expires - datetime.timedelta(seconds=1)),
                               TOOLS_RETENTION_DENY_REASON)
        self._assert_parked_at(self._authority(config, now=expires), MAIN_ENROLMENT_DENY_REASON)
        future = self.document["policy_row"] | {"issued_at": "2026-06-01T00:00:01Z"}
        config, _ = self._publish(self.document | {"policy_row": future})
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)

    def test_main_revocation_and_restrictions_narrow_authority(self):
        self._valid_control()
        row = self.document["policy_row"]
        for selector, value in (
            ("slot_ids", self.binding.slot_id), ("operation_ids", "record"),
            ("installation_refs", self.binding.installation_ref),
            ("implementation_sha256s", self.binding.implementation_sha256),
            ("revoked_generations", _REGISTRY_GEN),
        ):
            with self.subTest(revoked=selector):
                revoked = row | {"revocation": row["revocation"] | {selector: [value]}}
                config, _ = self._publish(self.document | {"policy_row": revoked})
                self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        for restriction, reason in (
            ({"kind": "disable", "target": "record"}, MAIN_ENROLMENT_DENY_REASON),
            ({"kind": "preview_only", "target": self.binding.slot_id}, MAIN_ENROLMENT_DENY_REASON),
            ({"kind": "limit_timeout", "target": "record", "timeout_s": self.binding.timeout_s - 1},
             MAIN_ENROLMENT_DENY_REASON),
            ({"kind": "limit_timeout", "target": "record", "timeout_s": self.binding.timeout_s},
             TOOLS_RETENTION_DENY_REASON),
        ):
            with self.subTest(restriction=restriction):
                restricted = row | {"restrictions": [restriction]}
                config, _ = self._publish(self.document | {"policy_row": restricted})
                self._assert_parked_at(self._authority(config), reason)

    def test_foreign_slot_and_each_exact_binding_pin_deny(self):
        self._valid_control()
        for name, value in (
            ("slot_id", "foreign-slot"), ("operation_id", "replay"),
            ("installation_ref", "installation:fixture-slot@2"), ("manifest_digest", "1" * 64),
            ("provider_id", "foreign-provider"), ("provider_revision", "2.0.0"),
            ("provider_sha256", "1" * 64), ("implementation_sha256", "1" * 64),
            ("adapter_id", "foreign-adapter"), ("adapter_sha256", "1" * 64), ("role", "verify"),
            ("request_schema_ref", "urn:rea.slot_request:v1"), ("request_digest", "1" * 64),
            ("result_schema_ref", "urn:rea.slot_result:v1"), ("result_digest", "1" * 64),
            ("execution_profile", "offline_fixture"), ("shared_schema_ref", "shared:fix"),
            ("shared_schema_sha256", "1" * 64),
        ):
            with self.subTest(binding_pin=name):
                foreign = self.document["binding_identity"] | {name: value}
                config, _ = self._publish(self.document | {"binding_identity": foreign})
                self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        config, _ = self._publish(self.document | {"slot": "foreign-slot"})
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)

    def test_enrolment_requires_current_measurement_and_admitted_intended_use(self):
        config, _ = self._valid_control()
        for changes in ({"observed_sha256": "1" * 64}, {"use_disposition": "internal_research"},
                        {"required_effects": ()}):
            with self.subTest(binding=changes):
                self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON,
                                       binding=replace(self.binding, **changes))

    def test_closed_enrolment_schema_and_strict_json_deny_malformed_input(self):
        self._valid_control()
        for document in (
            self.document | {"unexpected": True}, self.document | {"issued_by": "Carbon"},
            self.document | {"schema": "rea.main_enrolment/v0"},
            self.document | {"binding_identity": self.document["binding_identity"] | {"path": "tool.py"}},
            self.document | {"policy_row": self.document["policy_row"] | {"unexpected": True}},
            {key: value for key, value in self.document.items() if key != "slot"},
        ):
            with self.subTest(document=document):
                config, _ = self._publish(document)
                self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        config, _ = self._publish(raw_transform=lambda raw: b'{"slot":"fixture-slot",' + raw[1:])
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        config, _ = self._publish(raw_transform=lambda raw: raw + b"\xff")
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)

    def test_nonregular_and_symlink_enrolment_files_deny(self):
        config, pin = self._valid_control()
        file = self.directory / f"{pin}.json"
        unchanged = self.directory / "unchanged.json"
        file.rename(unchanged)
        file.symlink_to(unchanged.name)
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        file.unlink()
        os.mkfifo(file)
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
        config, _ = self._publish(raw_transform=lambda raw: raw + b" " * (1024 * 1024))
        self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)

    def test_symlinked_enrolment_directory_denies_with_or_without_trailing_slash(self):
        config, _ = self._valid_control()
        link = self.root / "linked-enrolment"
        link.symlink_to(self.directory, target_is_directory=True)
        for directory in (link, f"{link}/"):
            with self.subTest(directory=directory):
                self._assert_parked_at(self._authority(config | {"enrolment_directory": directory}),
                                       MAIN_ENROLMENT_DENY_REASON)

    def test_symlinked_enrolment_ancestor_and_witnesses_deny(self):
        config, _ = self._valid_control()
        link = self.root / "linked-parent"
        link.symlink_to(self.root, target_is_directory=True)
        with self.subTest(component="directory ancestor"):
            self._assert_parked_at(self._authority(config | {"enrolment_directory": link / self.directory.name}),
                                   MAIN_ENROLMENT_DENY_REASON)

        for key in ("enrolment_attestation_ref", "enrolment_carbon_read_ref"):
            with self.subTest(witness=key):
                file = self.directory / f"{config[key]}.json"
                target = file.with_suffix(".original.json")
                file.rename(target)
                file.symlink_to(target.name)
                try:
                    self._assert_parked_at(self._authority(config), MAIN_ENROLMENT_DENY_REASON)
                finally:
                    file.unlink()
                    target.rename(file)

    def test_noncanonical_enrolment_directory_and_witness_paths_deny(self):
        config, enrolment_pin = self._valid_control()
        sub = self.directory / "sub"
        sub.mkdir()
        for directory in (
            "", f"{self.directory.parent}/./{self.directory.name}", f"{sub}/..",
            f"{self.directory.parent}//{self.directory.name}", os.fsencode(self.directory),
            self.directory.name,
        ):
            with self.subTest(directory=directory), chdir(self.root):
                self._assert_parked_at(self._authority(config | {"enrolment_directory": directory}),
                                       MAIN_ENROLMENT_DENY_REASON)

        keys = ("enrolment_attestation_ref", "enrolment_carbon_read_ref")
        for pin in (enrolment_pin, *(config[key] for key in keys)):
            name = f"{pin}.json"
            raw = (self.directory / name).read_bytes()
            for other in (self.root, sub):
                (other / name).write_bytes(raw)
        for key in keys:
            pin = config[key]
            for ref in (f"../{pin}", f"sub/{pin}", "0" * 64):
                with self.subTest(witness=key, ref=ref):
                    self._assert_parked_at(self._authority(config | {key: ref}), MAIN_ENROLMENT_DENY_REASON)

        for ref in (f"../{enrolment_pin}", f"sub/{enrolment_pin}", "0" * 64):
            with self.subTest(enrolment_sha256=ref):
                witness = {"enrolment_sha256": ref}
                invalid, _ = self._publish(attestation=witness, carbon=witness)
                self._assert_parked_at(self._authority(invalid), MAIN_ENROLMENT_DENY_REASON)

    def test_swapped_enrolment_ancestor_cannot_redirect_witness_reads(self):
        config, _ = self._valid_control()
        ancestor = self.root / "configured"
        ancestor.mkdir()
        configured = ancestor / self.directory.name
        configured.mkdir()
        replacement = self.root / "replacement"
        replacement.mkdir()
        self.directory.rename(replacement / configured.name)
        displaced = self.root / "displaced"
        authority = self._authority(config | {"enrolment_directory": configured})
        self._assert_parked_at(authority, MAIN_ENROLMENT_DENY_REASON)
        real_open = os.open

        def swap_ancestor():
            ancestor.rename(displaced)
            replacement.rename(ancestor)

        def open_with_swap(path, flags, mode=0o777, *, dir_fd=None):
            path = os.fspath(path)
            if path == str(configured):
                # A single pathname lookup first acquires the ancestor after the swap.
                swap_ancestor()
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == ancestor.name and dir_fd is not None:
                # Once the ancestor is open, replacing its pathname cannot replace its fd.
                swap_ancestor()
            return fd

        with patch("slot_host.runtime.main_enrolment.os.open", new=open_with_swap):
            self._assert_parked_at(authority, MAIN_ENROLMENT_DENY_REASON)
        self.assertTrue((displaced / configured.name).is_dir())
        self._assert_parked_at(authority, TOOLS_RETENTION_DENY_REASON)
