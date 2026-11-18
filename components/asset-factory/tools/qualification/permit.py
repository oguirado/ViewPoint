"""Host-only, single-use authorization for the first TRELLIS geometry pilot.

The caller supplies the broker's independent permit pin, the configured factory
state directory, and launch facts obtained from the checked preflight snapshot.
Successful consumption reserves an attempt before any inference. Retain the
ledger across job failures, process restarts and retries.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from asset_factory import core, proc
from .gpu import SCOPE_LIMITS
from .manifest import CANDIDATE_SCHEMA, JSON_LIMIT, REVISION, SHA256, Refusal, check_legacy_subject, fields

SCHEMA = "af.run-permit/v1"
CONSUMPTION_SCHEMA = "af.run-permit-consumption/v1"
ISSUER_SHA256 = "77a1c9d9938f41fffe874882a1081a590c2d22806f4cc77366a7a9927049630c"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/af.run-permit.v1.schema.json"
PERMIT_ID = re.compile(r"[0-9a-f]{32}")
GEOMETRY_OUTPUTS = frozenset({"raw.glb", "model-native.npz", "result.json", "cuda-peaks.json"})


@dataclass(frozen=True)
class PilotLaunch:
    """Facts from the same protected CPU preflight and launch snapshot.

    The provider revision comes from the verified provider manifest. The
    derivative digest comes from the exact bytes staged for the adapter.
    Neither value is read from the presented permit.
    """

    provider_revision: str
    candidate: dict
    request_bytes: bytes
    derivative_sha256: str
    output_contract_bytes: bytes
    memory_limits: proc.MemoryLimits
    rights_bytes: bytes | None = None


def _digest(raw, code):
    if type(raw) is not bytes or not 0 < len(raw) <= JSON_LIMIT:
        raise Refusal(2, code)
    return hashlib.sha256(raw).hexdigest()


def _unique_pairs(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON key")
    return value


def _json_object(raw, code):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        core.canonical_json(value)
    except (ValueError, core.ContractError, RecursionError) as exc:
        raise Refusal(2, code) from exc
    if type(value) is not dict:
        raise Refusal(2, code)
    return value


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _bound_digest(candidate, key, raw, stale_code):
    descriptor = candidate[key]
    fields(descriptor, "path sha256", "permit_candidate_pin_invalid")
    if not _matches(SHA256, descriptor["sha256"]):
        raise Refusal(2, "permit_candidate_pin_invalid")
    digest = _digest(raw, "permit_evidence_bytes_invalid")
    if digest != descriptor["sha256"]:
        raise Refusal(4, stale_code)
    return digest


def _bindings(launch):
    if type(launch) is not PilotLaunch or not _matches(REVISION, launch.provider_revision):
        raise Refusal(2, "permit_launch_invalid")
    candidate = launch.candidate
    fields(candidate, "schema provider code weights conditioners dependencies runtime request "
                      "input_schema output_schema outputs", "permit_candidate_invalid",
           optional=("rights", "licence_metadata"))
    if candidate["schema"] != CANDIDATE_SCHEMA or candidate["provider"] != "trellis2":
        raise Refusal(4, "permit_candidate_stale")

    optional_bindings = {}
    if "rights" in candidate or launch.rights_bytes is not None:
        if "rights" not in candidate:
            raise Refusal(4, "permit_rights_bytes_stale")
        rights_digest = _bound_digest(candidate, "rights", launch.rights_bytes, "permit_rights_bytes_stale")
        check_legacy_subject(candidate, _json_object(launch.rights_bytes, "invalid_rights_decision"))
        optional_bindings["rights_sha256"] = rights_digest
    try:
        candidate_digest = core.sha256_json(candidate)
    except core.ContractError as exc:
        raise Refusal(2, "permit_candidate_invalid") from exc

    request_digest = _bound_digest(candidate, "request", launch.request_bytes, "permit_request_bytes_stale")
    request = _json_object(launch.request_bytes, "permit_request_invalid")
    output_digest = _bound_digest(candidate, "output_schema", launch.output_contract_bytes,
                                  "permit_output_contract_stale")
    outputs = candidate["outputs"]
    if (request.get("pipeline_type") != "512" or type(request.get("seed")) is not int
            or request["seed"] not in (42, 43, 45)
            or type(candidate["runtime"]) is not dict or candidate["runtime"].get("cuda") is not True
            or type(outputs) is not list or any(type(name) is not str for name in outputs)
            or len(outputs) != len(GEOMETRY_OUTPUTS) or set(outputs) != GEOMETRY_OUTPUTS):
        raise Refusal(5, "trellis_geometry_required")
    if not _matches(SHA256, launch.derivative_sha256):
        raise Refusal(2, "permit_derivative_digest_invalid")
    if type(launch.memory_limits) is not proc.MemoryLimits or asdict(launch.memory_limits) != SCOPE_LIMITS:
        raise Refusal(5, "trellis_pilot_scope_required")

    return {
        "schema": SCHEMA,
        "purpose": "trellis2-geometry-pilot",
        "provider": "trellis2",
        "provider_revision": launch.provider_revision,
        "candidate_sha256": candidate_digest,
        **optional_bindings,
        "derivative_sha256": launch.derivative_sha256,
        "request_sha256": request_digest,
        "seed": request["seed"],
        "output_contract_sha256": output_digest,
        "resource_scope": asdict(launch.memory_limits),
        "issuer_sha256": ISSUER_SHA256,
    }


def prepare_permit(launch, *, expires_unix_ns):
    """Prepare a candidate-bound record for the trusted issuer to pin."""
    if type(expires_unix_ns) is not int:
        raise Refusal(2, "run_permit_invalid")
    record = _bindings(launch)
    _require_fresh(expires_unix_ns, time.time_ns())
    return {**record, "permit_id": uuid4().hex, "expires_unix_ns": expires_unix_ns}


def _require_fresh(expires_unix_ns, now_ns):
    if now_ns >= expires_unix_ns:
        raise Refusal(5, "permit_expired")


def _validate_record(record, raw):
    try:
        schema = core.load_json(SCHEMA_PATH, max_bytes=JSON_LIMIT)
        core.validate(record, schema)
    except core.ContractError as exc:
        raise Refusal(2, "run_permit_invalid") from exc
    if (core.canonical_json(record).encode("utf-8") != raw
            or not _matches(PERMIT_ID, record["permit_id"])
            or not _matches(REVISION, record["provider_revision"])
            or any(not _matches(SHA256, record[key]) for key in (
                "candidate_sha256", "derivative_sha256",
                "request_sha256", "output_contract_sha256"))
            or ("rights_sha256" in record and not _matches(SHA256, record["rights_sha256"]))
            or record["expires_unix_ns"] <= 0):
        raise Refusal(2, "run_permit_invalid")


def _reserve(state_dir, record, permit_digest):
    try:
        root = Path(state_dir)
        if not root.is_absolute() or ".." in root.parts:
            raise Refusal(3, "permit_ledger_unavailable")
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise Refusal(3, "permit_ledger_unavailable")
            now_ns = time.time_ns()
            _require_fresh(record["expires_unix_ns"], now_ns)
            receipt = {
                "schema": CONSUMPTION_SCHEMA,
                "permit_id": record["permit_id"],
                "permit_sha256": permit_digest,
                "consumed_unix_ns": now_ns,
            }
            name = f'trellis-run-permit-{record["permit_id"]}.json'
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
            except FileExistsError as exc:
                raise Refusal(5, "permit_already_consumed") from exc
            # Once the name exists, even an empty marker spends the permit.
            with os.fdopen(fd, "wb") as stream:
                stream.write(core.canonical_json(receipt).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
            _require_fresh(record["expires_unix_ns"], time.time_ns())
            return receipt
        finally:
            os.close(directory)
    except (OSError, TypeError) as exc:
        raise Refusal(3, "permit_ledger_unavailable") from exc


def consume_permit(raw, *, trusted_permit_sha256, launch, state_dir):
    """Return the durable consumption receipt or raise Refusal before launch.

    Call only from the live host launch path. The trusted pin must originate
    with the issuer, and the directory must be the same protected local
    ledger for every attempt on this factory.
    """
    if not _matches(SHA256, trusted_permit_sha256):
        raise Refusal(2, "permit_pin_required")
    permit_digest = _digest(raw, "run_permit_invalid")
    if permit_digest != trusted_permit_sha256:
        raise Refusal(4, "permit_identity_stale")
    record = _json_object(raw, "run_permit_invalid")
    _validate_record(record, raw)
    bindings = _bindings(launch)
    if ({key: record.get(key) for key in bindings} != bindings
            or record.get("rights_sha256") != bindings.get("rights_sha256")):
        raise Refusal(4, "permit_binding_stale")
    return _reserve(state_dir, record, permit_digest)
