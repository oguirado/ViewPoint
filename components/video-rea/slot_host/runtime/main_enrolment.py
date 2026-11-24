# SPDX-License-Identifier: AGPL-3.0-only
"""Verify a host-pinned MAIN enrolment at the live authority boundary."""

import datetime
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..projections.schema import accepts, digest, standalone
from .binding import Binding
from .policy_signature import (
    _parse_timestamp,
    _strict_loads,
    _validate_restrictions,
    _validate_revocation,
)


_SCHEMA_FILE = "rea.main_enrolment_v1.schema.json"
_INPUT_FIELDS = ("binding_identity", "policy_row", "slot")
_MAX_DOCUMENT_BYTES = 256 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


@lru_cache(maxsize=1)
def _schemas():
    directory = Path(__file__).resolve().parents[2] / "contracts" / "manifest"
    enrolment = json.loads((directory / _SCHEMA_FILE).read_bytes())
    registry = {enrolment["$id"]: enrolment}
    for name in ("rea.live_policy_v1.schema.json", "rea.slot_manifest_v2.schema.json"):
        schema = json.loads((directory / name).read_bytes())
        registry[schema["$id"]] = schema
    base = enrolment["$id"]
    witnesses = (
        standalone({"$id": f"{base}/{name}", "$ref": f"{base}#/$defs/{name}"}, registry)
        for name in ("mainAttestation", "carbonRead")
    )
    return (standalone(enrolment, registry), *witnesses)


@contextmanager
def _enrolment_directory(directory: Path | str):
    """Pin the directory by opening each component relative to its parent fd."""
    raw = os.fspath(directory)
    if type(raw) is not str or not raw.startswith("/"):
        raise ValueError("enrolment directory must be an absolute text path")
    components = raw.split("/")[1:]
    if any(component in ("", ".", "..") for component in components):
        raise ValueError("enrolment directory path contains an invalid component")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in components:
            parent_fd = fd
            fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
        yield fd
    finally:
        os.close(fd)


def _read_pinned(directory_fd, pin, schema):
    if type(pin) is not str or _SHA256.fullmatch(pin) is None or pin == "0" * 64:
        raise ValueError("a concrete SHA-256 file reference is required")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(f"{pin}.json", flags, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_DOCUMENT_BYTES:
            raise ValueError("enrolment inputs require a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(_MAX_DOCUMENT_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_DOCUMENT_BYTES or hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("enrolment file differs from its SHA-256 pin")
    document = _strict_loads(raw, what="MAIN enrolment material")
    if not accepts(document, schema):
        raise ValueError("enrolment file differs from its schema")
    return document


def _row_allows(row, binding, now):
    from .conformance_authority import _restriction_met

    if not (
        _parse_timestamp(row["issued_at"], "issued_at") <= now
        < _parse_timestamp(row["expires_at"], "expires_at")
    ):
        return False

    restrictions = row["restrictions"]
    revocation = row["revocation"]
    _validate_restrictions(restrictions)
    _validate_revocation(revocation)
    selectors = (
        ("slot_ids", binding.slot_id),
        ("operation_ids", binding.operation_id),
        ("installation_refs", binding.installation_ref),
        ("implementation_sha256s", binding.implementation_sha256),
        ("revoked_generations", row["registry_generation"]),
    )
    targets = (binding.slot_id, binding.operation_id, binding.installation_ref)
    return not any(value in revocation[key] for key, value in selectors) and all(
        item["target"] not in targets or _restriction_met(item, binding)
        for item in restrictions
    )


@dataclass(frozen=True, kw_only=True)
class MainEnrolmentCheck:
    """Host configuration pins exact, separately obtained witness bytes.

    The directory supplies data only. The signed policy supplies the current
    enrolment reference and registry generation. No request selects a file
    or supplies either witness pin.
    """

    directory: Path | str | None
    attestation_ref: str | None
    carbon_read_ref: str | None

    def __call__(self, *, binding, operation_id, policy, now: datetime.datetime) -> bool:
        if (
            self.directory is None or self.attestation_ref == self.carbon_read_ref
            or type(binding) is not Binding or binding.operation_id != operation_id
        ):
            return False
        try:
            baseline = policy["baseline"]
            reference = baseline["enrollment_ref"]
            enrolment_schema, attestation_schema, carbon_schema = _schemas()
            with _enrolment_directory(self.directory) as directory_fd:
                attestation = _read_pinned(directory_fd, self.attestation_ref, attestation_schema)
                carbon = _read_pinned(directory_fd, self.carbon_read_ref, carbon_schema)
                file_pin = attestation["enrolment_sha256"]
                inputs_pin = attestation["inputs_sha256"]
                if not (
                    attestation["enrollment_ref"] == carbon["enrollment_ref"] == reference
                    and file_pin == carbon["enrolment_sha256"]
                    and inputs_pin == carbon["inputs_sha256"]
                ):
                    return False
                enrolment = _read_pinned(directory_fd, file_pin, enrolment_schema)

            row = enrolment["policy_row"]
            identity = enrolment["binding_identity"]
            generation = row["registry_generation"]
            return (
                inputs_pin == enrolment["inputs_sha256"]
                == digest({key: enrolment[key] for key in _INPUT_FIELDS})
                and row["enrollment_ref"] == reference
                and type(generation) is int and generation == baseline["registry_generation"]
                and enrolment["slot"] == binding.slot_id
                and all(getattr(binding, name) == value for name, value in identity.items())
                and binding.observed_sha256 == binding.implementation_sha256
                and binding.use_disposition == "external_execution"
                and "grant.exec.external_process" in binding.grants
                and "process.spawn:child" in binding.required_effects
                and _row_allows(row, binding, now)
            )
        except (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError):
            return False
