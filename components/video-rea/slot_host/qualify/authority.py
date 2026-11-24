# SPDX-License-Identifier: AGPL-3.0-only
"""Create qualification-only policy and enrolment in the disposable deployment."""

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from subprocess import DEVNULL, PIPE, run

from ..measure.seal import MANIFEST_SHA256
from ..projections.schema import digest
from ..runtime import evidence_projection as projection, policy_activation
from ..runtime.conformance_authority import ReceiptStore, create_fixture_receipt_for_binding
from ..runtime.live_park import make_conformance_authority
from ..runtime.main_enrolment import MainEnrolmentCheck


_AUDIENCE = "viewpoint-rea-local-qualification"
_ENROLMENT = "enrollment:local-qualification@1"
_GENERATION = 1
_REVOCATION = {key: [] for key in (
    "slot_ids", "operation_ids", "installation_refs", "implementation_sha256s", "revoked_generations",
)}


def _openssl(root, *args):
    return run(
        ("/usr/bin/openssl", *args), cwd=root, env={"LC_ALL": "C"}, stdin=DEVNULL,
        stdout=PIPE, stderr=DEVNULL, timeout=10, shell=False, check=True,
    ).stdout


def _private_file(target, raw):
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def _authority_policy(root, bindings, now, expiry):
    private, public = root / "private.pem", root / "public.pem"
    _private_file(private, b"")
    _openssl(root, "genpkey", "-algorithm", "ED25519", "-out", private)
    _openssl(root, "pkey", "-in", private, "-pubout", "-out", public)
    key_id = sha256(_openssl(root, "pkey", "-pubin", "-in", public, "-outform", "DER")).hexdigest()
    stamp = now.isoformat().replace("+00:00", "Z")
    policy = {
        "schema": "rea.live_policy/v1", "purpose": "viewpoint.rea.external_execution",
        "policy_id": "local-qualification", "issuer": "operator", "key_id": key_id,
        "audience": _AUDIENCE, "generation": _GENERATION,
        "issued_at": stamp, "not_before": stamp, "expires_at": expiry,
        "baseline": {
            "mode": "live_after_conformance",
            "allowed_profiles": ["external_process"], "allowed_transports": ["cli_subcommand", "cli_argv"],
            "required_grants": ["grant.exec.external_process"], "perception_ceiling": "none",
            "shared_evidence_ref": projection.V03_REF, "shared_evidence_sha256": projection.V03_SHA,
            "conformance_kit_sha256": MANIFEST_SHA256, "registry_generation": _GENERATION,
            "enrollment_ref": _ENROLMENT,
        },
        "scope": [{
            "slot_id": bindings["record"].slot_id, "operation_ids": list(bindings),
            "implementation_sha256": bindings["record"].implementation_sha256,
            "installation_ref": bindings["record"].installation_ref,
        }],
        "restrictions": [], "revocation": _REVOCATION,
    }
    payload = root / "policy.json"
    raw = projection.record_bytes(policy)
    _private_file(payload, raw)
    signature = _openssl(root, "pkeyutl", "-sign", "-inkey", private, "-rawin", "-in", payload)
    private.unlink()
    envelope = {
        "payload_b64": base64.b64encode(raw).decode("ascii"),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    envelope = projection.record_bytes(envelope)
    _private_file(root / "signed-policy.json", envelope)
    state = root / "activation.json"
    policy_activation.initialize_activation_state(state, audience=_AUDIENCE, key_id=key_id, now=now)
    policy_activation.activate(envelope, public_key_path=public, expected_audience=_AUDIENCE,
                               expected_key_id=key_id, state_path=state, now=now)
    return policy_activation.SignedPolicy(
        envelope=envelope, public_key_path=public, expected_audience=_AUDIENCE,
        expected_key_id=key_id, state_path=state,
    )


def _enrolment(root, binding, now, expiry):
    def publish(document):
        raw = projection.record_bytes(document)
        pin = sha256(raw).hexdigest()
        _private_file(root / f"{pin}.json", raw)
        return pin

    inputs = {
        "slot": binding.slot_id,
        "binding_identity": {name: getattr(binding, name) for name in (
            "slot_id", "operation_id", "installation_ref", "manifest_digest", "shared_schema_ref",
            "shared_schema_sha256", "provider_id", "provider_revision", "provider_sha256",
            "implementation_sha256", "adapter_id", "adapter_sha256", "role", "request_schema_ref",
            "request_digest", "result_schema_ref", "result_digest", "execution_profile",
        )},
        "policy_row": {
            "control_authority": {"owner": "ViewPoint_MAIN", "route_id": "rea.slot.project"},
            "registry_generation": _GENERATION, "enrollment_ref": _ENROLMENT,
            "issued_at": now.isoformat().replace("+00:00", "Z"), "expires_at": expiry,
            "restrictions": [], "revocation": _REVOCATION,
        },
    }
    binding_digest = digest(inputs)
    link = {
        "enrollment_ref": _ENROLMENT, "inputs_sha256": binding_digest,
        "enrolment_sha256": publish({
            "schema": "rea.main_enrolment/v1", "issued_by": "ViewPoint_MAIN",
            "inputs_sha256": binding_digest, **inputs,
        }),
    }
    return MainEnrolmentCheck(
        directory=root,
        attestation_ref=publish(link | {
            "schema": "rea.main_enrolment_attestation/v1", "issued_by": "ViewPoint_MAIN",
        }),
        carbon_read_ref=publish(link | {
            "schema": "rea.main_enrolment_carbon_read/v1", "issued_by": "Carbon", "outcome": "ACCEPT",
        }),
    )


def authorities(root, bindings, clients, control):
    """Pin disposable enrolment per operation and load the sealed fixture receipts."""
    security = root / "authority"
    security.mkdir(mode=0o700)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expiry = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    signed = _authority_policy(security, bindings, now, expiry)
    checks, receipts = {}, ReceiptStore()
    for operation, binding in bindings.items():
        checks[operation] = _enrolment(security, binding, now, expiry)
        receipts.put(create_fixture_receipt_for_binding(
            binding, operation, conformance_kit_sha256=MANIFEST_SHA256,
            registry_generation=_GENERATION, now=now,
        ))

    def enrolled(*, binding, operation_id, policy):
        check = checks.get(operation_id)
        return check is not None and check(binding=binding, operation_id=operation_id, policy=policy,
                                           now=datetime.now(timezone.utc))

    return tuple(make_conformance_authority(policy=signed, receipt_store=receipts,
                                            enrolment_check=enrolled, retention=ports)
                 for ports in (clients, control))
