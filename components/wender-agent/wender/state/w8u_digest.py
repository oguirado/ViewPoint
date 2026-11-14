# @module {"id":"wa.state.w8u_digest","layer":0,"owner":"STATE","deps":["wa.state.schema"],"book":"HB-WA-a0-arch"}
"""@module wa.state.w8u_digest - Closed STATE canonical digest, observation/sidecar binding,
domain separation (STATE / UI / simulation / frame), injected structural frame evidence,
and Receipt T2 STATE / training evidence helpers.

Wave-1 core depends only on Python stdlib and `wender.state.schema` (which optionally
binds `wender.perceive.session_registry` when present). It never imports unmerged sibling
W8U modules, never launches external processes, and never writes frames to disk.
"""
from __future__ import annotations

import builtins
import copy
import dataclasses
from dataclasses import dataclass
import hashlib
import io
import math
import os
import pathlib
import re
from typing import Any, Callable, Mapping, Sequence

from wender.state import schema

SCHEMA_ID = schema.SCHEMA_ID
SOURCE_KINDS = schema.SOURCE_KINDS
NODE_KEYS = schema.NODE_KEYS
TOP_KEYS = schema.TOP_KEYS
TARGET_KINDS = schema.TARGET_KINDS
TARGET_DESCRIPTOR_KEYS = schema.TARGET_DESCRIPTOR_KEYS
HANDLE_KEYS = schema.HANDLE_KEYS

OBSERVATION_ENVELOPE_KEYS = ("dpr_ppm", "epoch", "frame", "frame_seq", "handle", "state")
SIDECAR_REQUIRED_KEYS = ("epoch", "frame_digest", "frame_seq", "handle", "source_kind")
SIDECAR_OPTIONAL_KEYS = ("binding_sha256", "capture_mono_ns", "contract_revision", "dpr_ppm", "tick", "tick_valid")
SIDECAR_ALL_KEYS = tuple(sorted(SIDECAR_REQUIRED_KEYS + SIDECAR_OPTIONAL_KEYS))

AUSENTE = "AUSENTE"
TIMING_LABEL_VM = "VM"
LIMITS_STATUS_TARGET = "TARGET"

W8U_OS_CONTRACT_REVISION = "w8u-os-contract/v1"
W8U_BINDING_SCHEMA_ID = "w8u.state-binding/v1"
W8U_TRAINING_EVIDENCE_SCHEMA_ID = "w8u.training-state-evidence/v1"
W8U_RECEIPT_SCHEMA_ID = "w8u.receipt/v1"
W8U_ACTION_SCHEMA_ID = "w8u.action/v1"
W8U_ACTION_RESPONSE_SCHEMA_ID = "w8u.action-response/v1"

DIGEST_DOMAINS = {
    "state": None,
    "ui": "w8u.domain.ui/v1",
    "simulation": "w8u.domain.simulation/v1",
    "frame": "w8u.domain.frame/v1",
    "sidecar": "w8u.domain.sidecar/v1",
    "binding": "w8u.domain.binding/v1",
    "t2_trace": "w8u.domain.t2_trace/v1",
    "t2_terminal": "w8u.domain.t2_terminal/v1",
    "training_evidence": "w8u.domain.training_evidence/v1",
}

CONTROL_MAGIC = b"W8U\x01"
CONTROL_KINDS = ("HELLO", "ACK", "FRAME_PAIR", "STOP", "END", "EOF")
END_ERROR_CODES = {
    "OK": 0, "E_SCHEMA": 2, "E_CREDIT": 3, "E_UNAVAILABLE": 4, "E_VERIFY": 5,
    "E_UNCERTAIN": 6, "E_PAIR_MISMATCH": 7, "E_STALE_PUBLICATION": 8, "E_REOPEN_FORBIDDEN": 9,
}

OK_ADMITTED = "OK_ADMITTED"
E_SCHEMA = "E_SCHEMA"
E_STATE = "E_STATE"
E_FLOAT = "E_FLOAT"
E_WRAPPER = "E_WRAPPER"
E_SOURCE_KIND = "E_SOURCE_KIND"
E_VALIDATOR = "E_VALIDATOR"
E_ENVELOPE = "E_ENVELOPE"
E_HANDLE = "E_HANDLE"
E_EPOCH = "E_EPOCH"
E_STALE_FRAME = "E_STALE_FRAME"
E_STALE_SIDECAR = "E_STALE_SIDECAR"
E_FOREIGN_FRAME = "E_FOREIGN_FRAME"
E_FRAME_EVIDENCE = "E_FRAME_EVIDENCE"
E_DOMAIN_COLLISION = "E_DOMAIN_COLLISION"
E_CREDIT = "E_CREDIT"
E_VERB = "E_VERB"
E_LOCK = "E_LOCK"
E_FOCUS = "E_FOCUS"
E_BUDGET = "E_BUDGET"
E_DEADLINE = "E_DEADLINE"
E_UNCERTAIN = "E_UNCERTAIN"

ACTION_CODES = (
    OK_ADMITTED, E_SCHEMA, E_VERB, E_LOCK, E_EPOCH,
    E_STALE_FRAME, E_FOCUS, E_BUDGET, E_DEADLINE, E_UNCERTAIN,
)
ACTION_STATUSES = ("refused", "admitted", "applied", "uncertain")
ACTION_RC_MAP = {"valid_protocol": 0, "schema": 2, "unavailable": 4, "verification_refusal": 5, "uncertainty": 6}

RECEIPT_KEYS = (
    "run_id", "lease_id", "target_handle", "epoch", "frames_root", "frame_run_root", "source",
    "frames_captured", "first_frame", "last_frame", "ui_actions_count", "admitted_actions",
    "refused_actions", "uncertain_actions", "action_log_sha256", "t2_trace_sha256",
    "t2_terminal_sha256", "t2_ticks", "final_state_sha256", "simulation_state_sha256",
    "ui_gate_verdict", "ui_gate_code", "pair_mismatches", "frame_gaps", "frames_dropped",
    "x11_tcp_listeners", "x11_unix_listeners", "x11_abstract_listeners", "reader_exit_code",
    "reader_quiescent", "ram_purged", "frames_disk_bytes", "swap_delta_bytes", "metadata_only",
)
FRAME_METADATA_KEYS = (
    "seq", "tick", "tick_valid", "capture_mono_ns", "width",
    "height", "stride", "pixel_format", "sha256", "binding_sha256",
)
RECEIPT_FRAMES_ROOT = "/dev/shm/vqa-ramws"
RECEIPT_SOURCE = "xvfb-grab-held-pair"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


# @anchor:wa.state.w8u_digest.StateDigestError
class StateDigestError(schema.StateError):
    """Refusal raised when STATE, observation, sidecar, frame evidence, or domain checks fail."""
    def __init__(self, message: str, *, code: str = E_STATE, errors: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.errors = tuple(errors) if errors else (message,)


# @anchor:wa.state.w8u_digest.InvalidStateContractError
class InvalidStateContractError(StateDigestError):
    """Refusal raised when closed wender.state/v1 validation or canonical serialization fails."""


# @anchor:wa.state.w8u_digest.ObservationBindingError
class ObservationBindingError(StateDigestError):
    """Refusal raised when observation identity or envelope validation fails."""


# @anchor:wa.state.w8u_digest.SidecarStaleError
class SidecarStaleError(StateDigestError):
    """Refusal raised when the sidecar basis is stale or mismatched against the observation."""


# @anchor:wa.state.w8u_digest.ForeignFrameError
class ForeignFrameError(StateDigestError):
    """Refusal raised when a frame or sidecar belongs to another target handle or generation."""


# @anchor:wa.state.w8u_digest.DomainSeparationError
class DomainSeparationError(StateDigestError):
    """Refusal raised when simulation, UI, frame, or STATE digest domains collide or are confused."""


def _is_strict_int(value: object) -> bool:
    return type(value) is int


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def _check_no_float_or_nan(obj: object, path: str = "$") -> list[str]:
    errs: list[str] = []
    if isinstance(obj, float):
        if math.isnan(obj):
            errs.append(f"NaN float at {path}")
        elif math.isinf(obj):
            errs.append(f"infinite float at {path}")
        else:
            errs.append(f"float at {path}")
        return errs
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            errs.extend(_check_no_float_or_nan(k, f"{path}.<key>"))
            errs.extend(_check_no_float_or_nan(v, f"{path}.{k}"))
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            errs.extend(_check_no_float_or_nan(item, f"{path}[{idx}]"))
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            errs.extend(_check_no_float_or_nan(getattr(obj, f.name), f"{path}.{f.name}"))
    return errs


def _check_no_perception_wrapper(obj: object, path: str = "$") -> list[str]:
    errs: list[str] = []
    if isinstance(obj, Mapping):
        if "_perception" in obj:
            errs.append(f"_perception wrapper forbidden at {path}._perception")
        for k, v in obj.items():
            if isinstance(v, Mapping) and "_perception" in v:
                errs.append(f"_perception wrapper forbidden at {path}.{k}._perception")
    return errs


def _normalize_handle(value: object, *, field_name: str = "handle") -> schema.Handle:
    if isinstance(value, Mapping):
        if sorted(value.keys()) != sorted(HANDLE_KEYS):
            raise ObservationBindingError(f"{field_name} keys {sorted(value.keys())} != {sorted(HANDLE_KEYS)}", code=E_HANDLE)
        kind, canonical_id, generation = value.get("kind"), value.get("canonical_id"), value.get("generation")
    elif hasattr(value, "kind") and hasattr(value, "canonical_id") and hasattr(value, "generation"):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            f_names = [f.name for f in dataclasses.fields(value)]
            if sorted(f_names) != sorted(HANDLE_KEYS):
                raise ObservationBindingError(f"{field_name} fields {sorted(f_names)} != {sorted(HANDLE_KEYS)}", code=E_HANDLE)
        kind, canonical_id, generation = getattr(value, "kind"), getattr(value, "canonical_id"), getattr(value, "generation")
    else:
        raise ObservationBindingError(f"{field_name} must be a Handle or dict with {HANDLE_KEYS}", code=E_HANDLE)

    float_errs = _check_no_float_or_nan({"kind": kind, "canonical_id": canonical_id, "generation": generation}, path=f"$.{field_name}")
    if float_errs:
        raise ObservationBindingError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
    if not isinstance(kind, str) or kind not in TARGET_KINDS:
        raise ObservationBindingError(f"{field_name}.kind must be one of {TARGET_KINDS}", code=E_HANDLE)
    if not isinstance(canonical_id, str) or not canonical_id or canonical_id != canonical_id.strip() or "\x00" in canonical_id:
        raise ObservationBindingError(f"{field_name}.canonical_id must be a non-empty canonical str", code=E_HANDLE)
    if not _is_strict_int(generation) or generation < 1:
        raise ObservationBindingError(f"{field_name}.generation must be an int >= 1", code=E_HANDLE)
    return schema.Handle(kind=kind, canonical_id=canonical_id, generation=generation)


# @anchor:wa.state.w8u_digest.domain_sha256
def domain_sha256(domain_tag: str, payload_bytes: bytes) -> str:
    """Compute a domain-separated SHA-256 hex digest using NUL-delimited ASCII domain prefix."""
    if not isinstance(domain_tag, str) or not domain_tag or "\x00" in domain_tag:
        raise DomainSeparationError("domain_tag must be a non-empty string without NUL", code=E_DOMAIN_COLLISION)
    if not isinstance(payload_bytes, (bytes, bytearray, memoryview)):
        raise DomainSeparationError("payload_bytes must be bytes-like", code=E_DOMAIN_COLLISION)
    h = hashlib.sha256()
    h.update(domain_tag.encode("ascii"))
    h.update(b"\x00")
    h.update(bytes(payload_bytes))
    return h.hexdigest()


# @anchor:wa.state.w8u_digest.permute_state_mapping_order
def permute_state_mapping_order(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy of `state` with every dictionary's key insertion order reversed."""
    def _rev(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            items = list(obj.items())
            items.reverse()
            return {k: _rev(v) for k, v in items}
        if isinstance(obj, list):
            return [_rev(x) for x in obj]
        return copy.deepcopy(obj)
    return _rev(state)


# @anchor:wa.state.w8u_digest.validate_closed_state
def validate_closed_state(state: object, *, validator: Callable[[Any], Sequence[str]] | None = None) -> list[str]:
    """Validate a candidate STATE against the closed `wender.state/v1` contract."""
    errors: list[str] = []
    if not isinstance(state, dict):
        base_errs = schema.validate_state(state)
        if validator is not None and validator is not schema.validate_state:
            extra = validator(state)
            if not isinstance(extra, (list, tuple)) or any(not isinstance(x, str) for x in extra):
                raise InvalidStateContractError("injected validator must return a sequence of error strings", code=E_VALIDATOR)
            for item in extra:
                if item not in base_errs:
                    base_errs.append(item)
        return base_errs if base_errs else ["state is not an object"]

    errors.extend(_check_no_perception_wrapper(state))
    leaked_env = sorted(set(state.keys()).intersection({"handle", "epoch", "frame_seq", "dpr_ppm", "frame"}))
    if leaked_env:
        errors.append(f"observation envelope keys {leaked_env} forbidden in base STATE")

    try:
        schema_errs = schema.validate_state(state)
    except Exception as exc:
        schema_errs = [f"validate_state raised {type(exc).__name__}: {exc}"]
    if not isinstance(schema_errs, list):
        raise InvalidStateContractError("schema.validate_state must return a list", code=E_VALIDATOR)
    for err in schema_errs:
        if err not in errors:
            errors.append(err)

    prune = state.get("prune")
    if isinstance(prune, Mapping):
        if sorted(prune.keys()) != ["name", "version"] or not isinstance(prune.get("name"), str) or not _is_strict_int(prune.get("version")):
            msg = "prune must be {'name': str, 'version': int}"
            if msg not in errors:
                errors.append(msg)
    elif "prune" in state and sorted(state.keys()) == sorted(TOP_KEYS):
        if "prune must be an object" not in errors:
            errors.append("prune must be an object")

    if validator is not None and validator is not schema.validate_state:
        custom_errs = validator(state)
        if not isinstance(custom_errs, (list, tuple)) or any(not isinstance(x, str) for x in custom_errs):
            raise InvalidStateContractError("injected validator must return a sequence of error strings", code=E_VALIDATOR)
        for err in custom_errs:
            if err not in errors:
                errors.append(err)

    for ferr in _check_no_float_or_nan(state):
        if ferr not in errors:
            errors.append(ferr)
    return errors


# @anchor:wa.state.w8u_digest.validate_and_dump_state
def validate_and_dump_state(state: object, *, validator: Callable[[Any], Sequence[str]] | None = None) -> str:
    """Validate `state` against the closed contract and return canonical `schema.dumps(state)` without LF."""
    errs = validate_closed_state(state, validator=validator)
    if errs:
        code = E_STATE
        if any("_perception" in e for e in errs):
            code = E_WRAPPER
        elif any("float" in e or "NaN" in e for e in errs):
            code = E_FLOAT
        elif any("source.kind" in e for e in errs):
            code = E_SOURCE_KIND
        raise InvalidStateContractError("; ".join(errs), code=code, errors=errs)
    try:
        serialized = schema.dumps(state)
    except schema.StateError as exc:
        raise InvalidStateContractError(str(exc), code=E_FLOAT, errors=(str(exc),)) from exc
    if not isinstance(serialized, str) or "\n" in serialized or "\r" in serialized:
        raise InvalidStateContractError("canonical schema.dumps output must be a single line without LF or CR", code=E_STATE)
    return serialized


# @anchor:wa.state.w8u_digest.canonical_state_bytes
def canonical_state_bytes(state: object, *, validator: Callable[[Any], Sequence[str]] | None = None) -> bytes:
    """Validate `state` and return exact UTF-8 bytes of `schema.dumps(state)` without LF."""
    return validate_and_dump_state(state, validator=validator).encode("utf-8")


# @anchor:wa.state.w8u_digest.compute_state_digest
def compute_state_digest(state: object, *, validator: Callable[[Any], Sequence[str]] | None = None) -> str:
    """Compute the canonical 64-hex SHA-256 of UTF-8 `schema.dumps(state)` without LF."""
    return hashlib.sha256(canonical_state_bytes(state, validator=validator)).hexdigest()


# @anchor:wa.state.w8u_digest.state_digest
def state_digest(state: object, *, validator: Callable[[Any], Sequence[str]] | None = None) -> str:
    """Alias for `compute_state_digest`."""
    return compute_state_digest(state, validator=validator)


# @anchor:wa.state.w8u_digest.verify_state_digest
def verify_state_digest(state: object, expected_sha256: str, *, validator: Callable[[Any], Sequence[str]] | None = None) -> bool:
    """Verify that `state` is valid and its canonical STATE SHA-256 equals `expected_sha256`."""
    if not _is_sha256_hex(expected_sha256):
        raise InvalidStateContractError("expected_sha256 must be 64 lowercase hex chars", code=E_STATE)
    actual = compute_state_digest(state, validator=validator)
    if actual != expected_sha256:
        raise InvalidStateContractError(f"state digest mismatch: expected {expected_sha256}, got {actual}", code=E_STATE)
    return True


# @anchor:wa.state.w8u_digest.compute_simulation_state_digest
def compute_simulation_state_digest(simulation_state: object) -> str:
    """Compute a domain-separated SHA-256 digest for a T2 / simulation state mapping."""
    if not isinstance(simulation_state, Mapping) or not simulation_state:
        raise DomainSeparationError("simulation_state must be a non-empty mapping", code=E_DOMAIN_COLLISION)
    if simulation_state.get("schema") == SCHEMA_ID:
        raise DomainSeparationError("simulation_state cannot use UI STATE schema wender.state/v1", code=E_DOMAIN_COLLISION)
    if "_perception" in simulation_state:
        raise DomainSeparationError("_perception wrapper forbidden in simulation_state", code=E_WRAPPER)
    float_errs = _check_no_float_or_nan(simulation_state, path="$.simulation_state")
    if float_errs:
        raise DomainSeparationError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
    try:
        raw = schema.dumps(dict(simulation_state)).encode("utf-8")
    except schema.StateError as exc:
        raise DomainSeparationError(str(exc), code=E_FLOAT, errors=(str(exc),)) from exc
    return domain_sha256(DIGEST_DOMAINS["simulation"], raw)


# @anchor:wa.state.w8u_digest.SidecarBasis
@dataclass(frozen=True)
class SidecarBasis:
    """Validated sidecar basis tying observation provenance to a specific frame and source kind."""
    handle: schema.Handle
    epoch: int
    frame_seq: int
    frame_digest: str
    source_kind: str
    dpr_ppm: int = 1000000
    contract_revision: str = W8U_OS_CONTRACT_REVISION
    binding_sha256: str | None = None
    tick: int | None = None
    tick_valid: bool = False
    capture_mono_ns: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _normalize_handle(self.handle, field_name="sidecar.handle"))
        if not _is_strict_int(self.epoch) or self.epoch < 0:
            raise SidecarStaleError("sidecar.epoch must be a non-negative int", code=E_STALE_SIDECAR)
        if not _is_strict_int(self.frame_seq) or self.frame_seq < 0:
            raise SidecarStaleError("sidecar.frame_seq must be a non-negative int", code=E_STALE_SIDECAR)
        if not _is_sha256_hex(self.frame_digest):
            raise SidecarStaleError("sidecar.frame_digest must be 64 lowercase hex", code=E_STALE_SIDECAR)
        if not isinstance(self.source_kind, str) or self.source_kind not in SOURCE_KINDS:
            raise InvalidStateContractError(f"sidecar.source_kind must be one of {SOURCE_KINDS}", code=E_SOURCE_KIND)
        if not _is_strict_int(self.dpr_ppm) or self.dpr_ppm <= 0:
            raise SidecarStaleError("sidecar.dpr_ppm must be a positive int", code=E_STALE_SIDECAR)
        if not isinstance(self.contract_revision, str) or not self.contract_revision.strip():
            raise SidecarStaleError("sidecar.contract_revision must be non-empty str", code=E_FRAME_EVIDENCE)
        if self.binding_sha256 is not None and not _is_sha256_hex(self.binding_sha256):
            raise SidecarStaleError("sidecar.binding_sha256 must be 64 lowercase hex or None", code=E_STALE_SIDECAR)
        if type(self.tick_valid) is not bool:
            raise SidecarStaleError("sidecar.tick_valid must be bool", code=E_STALE_SIDECAR)
        if not self.tick_valid:
            if self.tick is not None:
                raise SidecarStaleError("sidecar.tick must be None when tick_valid is False", code=E_STALE_SIDECAR)
        elif not _is_strict_int(self.tick) or self.tick < 0:
            raise SidecarStaleError("sidecar.tick must be a non-negative int when tick_valid is True", code=E_STALE_SIDECAR)
        if self.capture_mono_ns is not None and (not _is_strict_int(self.capture_mono_ns) or self.capture_mono_ns < 0):
            raise SidecarStaleError("sidecar.capture_mono_ns must be a non-negative int or None", code=E_STALE_SIDECAR)


# @anchor:wa.state.w8u_digest.validate_sidecar_basis
def validate_sidecar_basis(sidecar: object) -> SidecarBasis:
    """Validate and normalize a `SidecarBasis` instance or mapping."""
    if isinstance(sidecar, SidecarBasis):
        return sidecar
    if isinstance(sidecar, Mapping):
        if "_perception" in sidecar:
            raise InvalidStateContractError("_perception wrapper forbidden in sidecar", code=E_WRAPPER)
        float_errs = _check_no_float_or_nan(sidecar, path="$.sidecar")
        if float_errs:
            raise InvalidStateContractError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
        unknown = sorted(set(sidecar.keys()) - set(SIDECAR_ALL_KEYS))
        if unknown:
            raise SidecarStaleError(f"unknown sidecar keys: {unknown}", code=E_STALE_SIDECAR)
        missing = sorted(set(SIDECAR_REQUIRED_KEYS) - set(sidecar.keys()))
        if missing:
            raise SidecarStaleError(f"missing required sidecar keys: {missing}", code=E_STALE_SIDECAR)
        return SidecarBasis(
            handle=sidecar["handle"], epoch=sidecar["epoch"], frame_seq=sidecar["frame_seq"],
            frame_digest=sidecar["frame_digest"], source_kind=sidecar["source_kind"],
            dpr_ppm=sidecar.get("dpr_ppm", 1000000),
            contract_revision=sidecar.get("contract_revision", W8U_OS_CONTRACT_REVISION),
            binding_sha256=sidecar.get("binding_sha256"), tick=sidecar.get("tick"),
            tick_valid=sidecar.get("tick_valid", False), capture_mono_ns=sidecar.get("capture_mono_ns"),
        )
    raise SidecarStaleError("sidecar must be a SidecarBasis or mapping", code=E_STALE_SIDECAR)


# @anchor:wa.state.w8u_digest.StructuralFrameEvidence
@dataclass(frozen=True)
class StructuralFrameEvidence:
    """Validated structural frame evidence injected into the wave-1 STATE digest core."""
    handle: schema.Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    digest: str
    contract_revision: str = W8U_OS_CONTRACT_REVISION
    stride: int | None = None
    pixel_format: str = "RGB24"
    capture_mono_ns: int | None = None
    tick: int | None = None
    tick_valid: bool = False
    binding_sha256: str | None = None
    pair_verified: bool = True
    xwd_common_subset_ok: bool = True


# @anchor:wa.state.w8u_digest.validate_structural_frame_evidence
def validate_structural_frame_evidence(
    frame: object,
    *,
    expected_contract_revision: str = W8U_OS_CONTRACT_REVISION,
    trust_check: Callable[[object], bool | Sequence[str]] | None = None,
) -> StructuralFrameEvidence:
    """Validate structural frame evidence without importing any sibling W8U module."""
    if frame is None:
        raise ObservationBindingError("frame evidence is required", code=E_FRAME_EVIDENCE)
    if isinstance(frame, Mapping):
        float_errs = _check_no_float_or_nan(frame, path="$.frame")
        if float_errs:
            raise InvalidStateContractError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
        raw_handle = frame.get("handle")
        epoch = frame.get("epoch")
        frame_seq = frame.get("frame_seq", frame.get("seq"))
        width, height = frame.get("width"), frame.get("height")
        digest = frame.get("digest", frame.get("sha256"))
        rgb = frame.get("rgb")
        contract_revision = frame.get("contract_revision", W8U_OS_CONTRACT_REVISION)
        stride = frame.get("stride")
        pixel_format = frame.get("pixel_format", "RGB24")
        capture_mono_ns = frame.get("capture_mono_ns")
        tick, tick_valid = frame.get("tick"), frame.get("tick_valid", False)
        binding_sha256 = frame.get("binding_sha256")
        pair_verified = frame.get("pair_verified", True)
        xwd_common_subset_ok = frame.get("xwd_common_subset_ok", True)
    else:
        raw_handle = getattr(frame, "handle", None)
        epoch = getattr(frame, "epoch", None)
        frame_seq = getattr(frame, "frame_seq", getattr(frame, "seq", None))
        width, height = getattr(frame, "width", None), getattr(frame, "height", None)
        digest = getattr(frame, "digest", getattr(frame, "sha256", None))
        rgb = getattr(frame, "rgb", None)
        contract_revision = getattr(frame, "contract_revision", W8U_OS_CONTRACT_REVISION)
        stride = getattr(frame, "stride", None)
        pixel_format = getattr(frame, "pixel_format", "RGB24")
        capture_mono_ns = getattr(frame, "capture_mono_ns", None)
        tick, tick_valid = getattr(frame, "tick", None), getattr(frame, "tick_valid", False)
        binding_sha256 = getattr(frame, "binding_sha256", None)
        pair_verified = getattr(frame, "pair_verified", True)
        xwd_common_subset_ok = getattr(frame, "xwd_common_subset_ok", True)

    handle = _normalize_handle(raw_handle, field_name="frame.handle")
    if not _is_strict_int(epoch) or epoch < 0:
        raise ObservationBindingError("frame.epoch must be a non-negative int", code=E_FRAME_EVIDENCE)
    if not _is_strict_int(frame_seq) or frame_seq < 0:
        raise ObservationBindingError("frame.frame_seq must be a non-negative int", code=E_FRAME_EVIDENCE)
    if not _is_strict_int(width) or width < 1:
        raise ObservationBindingError("frame.width must be an int >= 1", code=E_FRAME_EVIDENCE)
    if not _is_strict_int(height) or height < 1:
        raise ObservationBindingError("frame.height must be an int >= 1", code=E_FRAME_EVIDENCE)
    if not _is_sha256_hex(digest):
        raise ObservationBindingError("frame.digest must be 64 lowercase hex", code=E_FRAME_EVIDENCE)
    if expected_contract_revision is not None and contract_revision != expected_contract_revision:
        raise ObservationBindingError(
            f"frame contract_revision {contract_revision!r} != pinned {expected_contract_revision!r}",
            code=E_FRAME_EVIDENCE,
        )
    if type(pair_verified) is not bool or not pair_verified:
        raise ObservationBindingError("same-grab canonical pair equality check failed", code=E_FRAME_EVIDENCE)
    if type(xwd_common_subset_ok) is not bool or not xwd_common_subset_ok:
        raise ObservationBindingError("common XWD subset check failed", code=E_FRAME_EVIDENCE)
    if stride is not None and (not _is_strict_int(stride) or stride < width * 3):
        raise ObservationBindingError("frame.stride must be an int >= width * 3", code=E_FRAME_EVIDENCE)
    if not isinstance(pixel_format, str) or not pixel_format:
        raise ObservationBindingError("frame.pixel_format must be a non-empty str", code=E_FRAME_EVIDENCE)
    if type(tick_valid) is not bool:
        raise ObservationBindingError("frame.tick_valid must be bool", code=E_FRAME_EVIDENCE)
    if not tick_valid:
        if tick is not None:
            raise ObservationBindingError("frame.tick must be None when tick_valid is False", code=E_FRAME_EVIDENCE)
    elif not _is_strict_int(tick) or tick < 0:
        raise ObservationBindingError("frame.tick must be a non-negative int when tick_valid is True", code=E_FRAME_EVIDENCE)
    if capture_mono_ns is not None and (not _is_strict_int(capture_mono_ns) or capture_mono_ns < 0):
        raise ObservationBindingError("frame.capture_mono_ns must be a non-negative int or None", code=E_FRAME_EVIDENCE)
    if binding_sha256 is not None and not _is_sha256_hex(binding_sha256):
        raise ObservationBindingError("frame.binding_sha256 must be 64 lowercase hex or None", code=E_FRAME_EVIDENCE)

    if rgb is not None:
        if isinstance(rgb, (bytes, bytearray, memoryview)):
            raw_bytes = bytes(rgb)
            if len(raw_bytes) != width * height * 3:
                raise ObservationBindingError("RGB byte length does not match width * height * 3", code=E_FRAME_EVIDENCE)
            actual_digest = hashlib.sha256(raw_bytes).hexdigest()
        elif hasattr(rgb, "tobytes") and hasattr(rgb, "shape") and hasattr(rgb, "dtype"):
            if str(rgb.dtype) != "uint8" or len(rgb.shape) != 3 or rgb.shape[2] != 3:
                raise ObservationBindingError("RGB must be H x W x 3 uint8", code=E_FRAME_EVIDENCE)
            if (rgb.shape[0], rgb.shape[1]) != (height, width):
                raise ObservationBindingError("RGB dimensions do not match width/height", code=E_FRAME_EVIDENCE)
            actual_digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        else:
            raise ObservationBindingError("unsupported frame.rgb buffer type", code=E_FRAME_EVIDENCE)
        if actual_digest != digest:
            raise ObservationBindingError("RGB content digest mismatch", code=E_FRAME_EVIDENCE)

    if trust_check is not None:
        verdict = trust_check(frame)
        if isinstance(verdict, bool):
            if not verdict:
                raise ObservationBindingError("injected frame trust_check rejected frame evidence", code=E_FRAME_EVIDENCE)
        elif isinstance(verdict, (list, tuple)):
            if verdict:
                raise ObservationBindingError("; ".join(str(x) for x in verdict), code=E_FRAME_EVIDENCE, errors=tuple(str(x) for x in verdict))
        else:
            raise ObservationBindingError("injected frame trust_check must return bool or error sequence", code=E_FRAME_EVIDENCE)

    return StructuralFrameEvidence(
        handle=handle, epoch=epoch, frame_seq=frame_seq, width=width, height=height,
        digest=digest, contract_revision=contract_revision, stride=width * 3 if stride is None else stride,
        pixel_format=pixel_format, capture_mono_ns=capture_mono_ns, tick=tick, tick_valid=tick_valid,
        binding_sha256=binding_sha256, pair_verified=pair_verified, xwd_common_subset_ok=xwd_common_subset_ok,
    )


# @anchor:wa.state.w8u_digest.BoundStateDigest
@dataclass(frozen=True)
class BoundStateDigest:
    """Bound digest record produced only after STATE, observation, frame, and sidecar validation."""
    state_sha256: str
    ui_domain_sha256: str
    frame_sha256: str
    frame_domain_sha256: str
    simulation_state_sha256: str | None
    sidecar_sha256: str
    binding_sha256: str
    handle: schema.Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    source_kind: str
    contract_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": W8U_BINDING_SCHEMA_ID,
            "handle": {"kind": self.handle.kind, "canonical_id": self.handle.canonical_id, "generation": self.handle.generation},
            "epoch": self.epoch, "frame_seq": self.frame_seq, "dpr_ppm": self.dpr_ppm,
            "source_kind": self.source_kind, "contract_revision": self.contract_revision,
            "state_sha256": self.state_sha256, "ui_domain_sha256": self.ui_domain_sha256,
            "frame_sha256": self.frame_sha256, "frame_domain_sha256": self.frame_domain_sha256,
            "simulation_state_sha256": self.simulation_state_sha256,
            "sidecar_sha256": self.sidecar_sha256, "binding_sha256": self.binding_sha256,
        }


# @anchor:wa.state.w8u_digest.validate_observation_identity
def validate_observation_identity(
    observation: object,
    *,
    expected_handle: object | None = None,
    expected_epoch: int | None = None,
    expected_frame_seq: int | None = None,
    registry: object | None = None,
    expected_contract_revision: str = W8U_OS_CONTRACT_REVISION,
    frame_trust_check: Callable[[object], bool | Sequence[str]] | None = None,
    validator: Callable[[Any], Sequence[str]] | None = None,
) -> tuple[schema.Handle, int, int, int, dict[str, Any], StructuralFrameEvidence]:
    """Validate an Observation object or observation envelope dict and its target descriptor."""
    if observation is None:
        raise ObservationBindingError("observation is required", code=E_ENVELOPE)
    if isinstance(observation, Mapping):
        if "_perception" in observation:
            raise InvalidStateContractError("_perception wrapper forbidden in observation", code=E_WRAPPER)
        float_errs = _check_no_float_or_nan({k: v for k, v in observation.items() if k != "frame"}, path="$.observation")
        if float_errs:
            raise InvalidStateContractError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
        if sorted(observation.keys()) != sorted(OBSERVATION_ENVELOPE_KEYS):
            raise ObservationBindingError(f"observation envelope keys {sorted(observation.keys())} != {sorted(OBSERVATION_ENVELOPE_KEYS)}", code=E_ENVELOPE)
        raw_handle, epoch, frame_seq = observation["handle"], observation["epoch"], observation["frame_seq"]
        dpr_ppm, state, raw_frame = observation["dpr_ppm"], observation["state"], observation["frame"]
    elif all(hasattr(observation, attr) for attr in OBSERVATION_ENVELOPE_KEYS):
        raw_handle, epoch, frame_seq = getattr(observation, "handle"), getattr(observation, "epoch"), getattr(observation, "frame_seq")
        dpr_ppm, state, raw_frame = getattr(observation, "dpr_ppm"), getattr(observation, "state"), getattr(observation, "frame")
    else:
        raise ObservationBindingError(f"observation must be an Observation or dict with keys {OBSERVATION_ENVELOPE_KEYS}", code=E_ENVELOPE)

    handle = _normalize_handle(raw_handle, field_name="observation.handle")
    if expected_handle is not None:
        exp_h = _normalize_handle(expected_handle, field_name="expected_handle")
        if handle != exp_h:
            raise ForeignFrameError(f"observation handle {handle!r} does not match expected target {exp_h!r}", code=E_FOREIGN_FRAME)

    registry_epoch = None
    if registry is not None:
        resolve_fn = getattr(registry, "resolve", None)
        if not callable(resolve_fn):
            raise ObservationBindingError("registry must expose resolve(handle)", code=E_HANDLE)
        try:
            record = resolve_fn(handle)
        except Exception as exc:
            raise ObservationBindingError(f"registry resolution failed: {exc}", code=E_HANDLE) from exc
        rec_handle = _normalize_handle(getattr(record, "handle", None), field_name="registry.record.handle")
        if rec_handle != handle or getattr(record, "closed", False):
            raise ObservationBindingError("resolved target record is closed or mismatched", code=E_HANDLE)
        registry_epoch = getattr(record, "epoch", None)

    descriptor = {"handle": handle, "epoch": epoch, "frame_seq": frame_seq, "dpr_ppm": dpr_ppm, "state": state}
    td_errs = schema.validate_target_descriptor(descriptor, expected_epoch=expected_epoch, registry_epoch=registry_epoch)
    state_errs = validate_closed_state(state, validator=validator)
    for serr in state_errs:
        if serr not in td_errs:
            td_errs.append(serr)
    if td_errs:
        joined = "; ".join(td_errs)
        if any("_perception" in e for e in td_errs):
            raise InvalidStateContractError(joined, code=E_WRAPPER, errors=td_errs)
        if any("float" in e or "NaN" in e for e in td_errs):
            raise InvalidStateContractError(joined, code=E_FLOAT, errors=td_errs)
        if any("source.kind" in e for e in td_errs):
            raise InvalidStateContractError(joined, code=E_SOURCE_KIND, errors=td_errs)
        if any("does not match registry epoch" in e for e in td_errs):
            raise ObservationBindingError(joined, code=E_EPOCH, errors=td_errs)
        if state_errs:
            raise InvalidStateContractError(joined, code=E_STATE, errors=td_errs)
        raise ObservationBindingError(joined, code=E_ENVELOPE, errors=td_errs)

    if expected_frame_seq is not None and (not _is_strict_int(expected_frame_seq) or frame_seq != expected_frame_seq):
        raise ObservationBindingError(f"observation frame_seq {frame_seq!r} != expected {expected_frame_seq!r}", code=E_STALE_FRAME)

    frame_ev = validate_structural_frame_evidence(raw_frame, expected_contract_revision=expected_contract_revision, trust_check=frame_trust_check)
    if frame_ev.handle != handle:
        raise ForeignFrameError(f"another target's frame: frame handle {frame_ev.handle!r} != observation handle {handle!r}", code=E_FOREIGN_FRAME)
    if frame_ev.epoch != epoch:
        raise ObservationBindingError(f"stale frame epoch: frame.epoch {frame_ev.epoch} != observation.epoch {epoch}", code=E_STALE_FRAME)
    if frame_ev.frame_seq != frame_seq:
        raise ObservationBindingError(f"stale frame_seq: frame.frame_seq {frame_ev.frame_seq} != observation.frame_seq {frame_seq}", code=E_STALE_FRAME)

    vp = state.get("viewport")
    if isinstance(vp, Mapping) and "dpr_ppm" in vp and vp["dpr_ppm"] != dpr_ppm:
        raise ObservationBindingError(f"viewport.dpr_ppm {vp['dpr_ppm']} != observation.dpr_ppm {dpr_ppm}", code=E_ENVELOPE)

    return handle, epoch, frame_seq, dpr_ppm, copy.deepcopy(state), frame_ev


# @anchor:wa.state.w8u_digest.bind_observation_digest
def bind_observation_digest(
    observation: object,
    sidecar: object,
    *,
    simulation_state: object | None = None,
    expected_handle: object | None = None,
    expected_epoch: int | None = None,
    expected_frame_seq: int | None = None,
    registry: object | None = None,
    expected_contract_revision: str = W8U_OS_CONTRACT_REVISION,
    frame_trust_check: Callable[[object], bool | Sequence[str]] | None = None,
    validator: Callable[[Any], Sequence[str]] | None = None,
) -> BoundStateDigest:
    """Validate separate observation identity, closed STATE, frame evidence, and sidecar basis
    before exposing a `BoundStateDigest`. Keeps STATE, UI, simulation, and frame domains distinct.
    """
    handle, epoch, frame_seq, dpr_ppm, state, frame_ev = validate_observation_identity(
        observation, expected_handle=expected_handle, expected_epoch=expected_epoch,
        expected_frame_seq=expected_frame_seq, registry=registry,
        expected_contract_revision=expected_contract_revision,
        frame_trust_check=frame_trust_check, validator=validator,
    )
    sidecar_basis = validate_sidecar_basis(sidecar)
    if sidecar_basis.handle != handle:
        raise ForeignFrameError(f"another target's sidecar: sidecar.handle {sidecar_basis.handle!r} != observation.handle {handle!r}", code=E_FOREIGN_FRAME)
    if sidecar_basis.epoch != epoch:
        raise SidecarStaleError(f"stale sidecar: sidecar.epoch {sidecar_basis.epoch} != observation.epoch {epoch}", code=E_STALE_SIDECAR)
    if sidecar_basis.frame_seq != frame_seq:
        raise SidecarStaleError(f"stale sidecar: sidecar.frame_seq {sidecar_basis.frame_seq} != observation.frame_seq {frame_seq}", code=E_STALE_SIDECAR)
    if sidecar_basis.frame_digest != frame_ev.digest:
        raise SidecarStaleError(f"stale sidecar: sidecar.frame_digest {sidecar_basis.frame_digest} != frame.digest {frame_ev.digest}", code=E_STALE_SIDECAR)
    state_source_kind = state["source"]["kind"]
    if sidecar_basis.source_kind != state_source_kind:
        raise SidecarStaleError(f"stale sidecar: sidecar.source_kind {sidecar_basis.source_kind!r} != state.source.kind {state_source_kind!r}", code=E_STALE_SIDECAR)
    if sidecar_basis.dpr_ppm != dpr_ppm:
        raise SidecarStaleError(f"stale sidecar: sidecar.dpr_ppm {sidecar_basis.dpr_ppm} != observation.dpr_ppm {dpr_ppm}", code=E_STALE_SIDECAR)
    if expected_contract_revision is not None and sidecar_basis.contract_revision != expected_contract_revision:
        raise ObservationBindingError(f"sidecar.contract_revision {sidecar_basis.contract_revision!r} != pinned {expected_contract_revision!r}", code=E_FRAME_EVIDENCE)

    state_bytes = canonical_state_bytes(state, validator=validator)
    state_sha256 = hashlib.sha256(state_bytes).hexdigest()
    ui_domain_sha256 = domain_sha256(DIGEST_DOMAINS["ui"], state_bytes)
    frame_sha256 = frame_ev.digest
    frame_domain_sha256 = domain_sha256(DIGEST_DOMAINS["frame"], frame_sha256.encode("ascii"))
    sim_sha256 = compute_simulation_state_digest(simulation_state) if simulation_state is not None else None

    sidecar_payload = schema.dumps({
        "handle": {"kind": sidecar_basis.handle.kind, "canonical_id": sidecar_basis.handle.canonical_id, "generation": sidecar_basis.handle.generation},
        "epoch": sidecar_basis.epoch, "frame_seq": sidecar_basis.frame_seq, "frame_digest": sidecar_basis.frame_digest,
        "source_kind": sidecar_basis.source_kind, "dpr_ppm": sidecar_basis.dpr_ppm, "contract_revision": sidecar_basis.contract_revision,
        "tick": sidecar_basis.tick, "tick_valid": sidecar_basis.tick_valid, "capture_mono_ns": sidecar_basis.capture_mono_ns,
    }).encode("utf-8")
    sidecar_sha256 = domain_sha256(DIGEST_DOMAINS["sidecar"], sidecar_payload)

    binding_payload = schema.dumps({
        "schema": W8U_BINDING_SCHEMA_ID,
        "handle": {"kind": handle.kind, "canonical_id": handle.canonical_id, "generation": handle.generation},
        "epoch": epoch, "frame_seq": frame_seq, "dpr_ppm": dpr_ppm, "source_kind": state_source_kind,
        "contract_revision": frame_ev.contract_revision, "state_sha256": state_sha256,
        "ui_domain_sha256": ui_domain_sha256, "frame_sha256": frame_sha256,
        "frame_domain_sha256": frame_domain_sha256, "simulation_state_sha256": sim_sha256,
        "sidecar_sha256": sidecar_sha256,
    }).encode("utf-8")
    binding_sha256 = domain_sha256(DIGEST_DOMAINS["binding"], binding_payload)

    if sidecar_basis.binding_sha256 is not None and sidecar_basis.binding_sha256 != binding_sha256:
        raise SidecarStaleError(f"stale sidecar: sidecar.binding_sha256 {sidecar_basis.binding_sha256} != {binding_sha256}", code=E_STALE_SIDECAR)
    if frame_ev.binding_sha256 is not None and frame_ev.binding_sha256 != binding_sha256:
        raise SidecarStaleError(f"stale frame binding_sha256: {frame_ev.binding_sha256} != {binding_sha256}", code=E_STALE_SIDECAR)

    distinct_hashes = [
        ("state_sha256", state_sha256), ("ui_domain_sha256", ui_domain_sha256),
        ("frame_sha256", frame_sha256), ("frame_domain_sha256", frame_domain_sha256),
        ("sidecar_sha256", sidecar_sha256), ("binding_sha256", binding_sha256),
    ]
    if sim_sha256 is not None:
        distinct_hashes.append(("simulation_state_sha256", sim_sha256))
    seen_values: dict[str, str] = {}
    for label, digest_val in distinct_hashes:
        if digest_val in seen_values:
            raise DomainSeparationError(f"digest domain collision between {seen_values[digest_val]} and {label}", code=E_DOMAIN_COLLISION)
        seen_values[digest_val] = label

    return BoundStateDigest(
        state_sha256=state_sha256, ui_domain_sha256=ui_domain_sha256, frame_sha256=frame_sha256,
        frame_domain_sha256=frame_domain_sha256, simulation_state_sha256=sim_sha256,
        sidecar_sha256=sidecar_sha256, binding_sha256=binding_sha256, handle=handle,
        epoch=epoch, frame_seq=frame_seq, dpr_ppm=dpr_ppm, source_kind=state_source_kind,
        contract_revision=frame_ev.contract_revision,
    )


# @anchor:wa.state.w8u_digest.build_frame_metadata
def build_frame_metadata(frame_ev: StructuralFrameEvidence, *, binding_sha256: str | None = None) -> dict[str, Any]:
    """Build a canonical `w8u.receipt/v1` frame metadata dict with exact required keys."""
    if not isinstance(frame_ev, StructuralFrameEvidence):
        frame_ev = validate_structural_frame_evidence(frame_ev)
    resolved_binding = binding_sha256 if binding_sha256 is not None else frame_ev.binding_sha256
    if resolved_binding is not None and not _is_sha256_hex(resolved_binding):
        raise StateDigestError("binding_sha256 must be 64 lowercase hex or None", code=E_FRAME_EVIDENCE)
    meta = {
        "seq": frame_ev.frame_seq, "tick": frame_ev.tick if frame_ev.tick_valid else None,
        "tick_valid": frame_ev.tick_valid, "capture_mono_ns": frame_ev.capture_mono_ns,
        "width": frame_ev.width, "height": frame_ev.height,
        "stride": frame_ev.stride if frame_ev.stride is not None else frame_ev.width * 3,
        "pixel_format": frame_ev.pixel_format, "sha256": frame_ev.digest, "binding_sha256": resolved_binding,
    }
    validate_frame_metadata(meta)
    return meta


# @anchor:wa.state.w8u_digest.validate_frame_metadata
def validate_frame_metadata(meta: object) -> list[str]:
    """Validate frame metadata dictionary (`FRAME_METADATA_KEYS`)."""
    if not isinstance(meta, Mapping):
        raise StateDigestError("frame metadata must be a mapping", code=E_SCHEMA)
    float_errs = _check_no_float_or_nan(meta, path="$.frame_metadata")
    if float_errs:
        raise InvalidStateContractError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
    if sorted(meta.keys()) != sorted(FRAME_METADATA_KEYS):
        raise StateDigestError(f"frame metadata keys {sorted(meta.keys())} != {sorted(FRAME_METADATA_KEYS)}", code=E_SCHEMA)
    if not _is_strict_int(meta["seq"]) or meta["seq"] < 0:
        raise StateDigestError("frame metadata seq must be non-negative int", code=E_SCHEMA)
    if type(meta["tick_valid"]) is not bool:
        raise StateDigestError("frame metadata tick_valid must be bool", code=E_SCHEMA)
    if not meta["tick_valid"]:
        if meta["tick"] is not None:
            raise StateDigestError("frame metadata tick must be None when tick_valid is False", code=E_SCHEMA)
    elif not _is_strict_int(meta["tick"]) or meta["tick"] < 0:
        raise StateDigestError("frame metadata tick must be non-negative int when tick_valid is True", code=E_SCHEMA)
    if meta["capture_mono_ns"] is not None and (not _is_strict_int(meta["capture_mono_ns"]) or meta["capture_mono_ns"] < 0):
        raise StateDigestError("frame metadata capture_mono_ns must be non-negative int or None", code=E_SCHEMA)
    if not _is_strict_int(meta["width"]) or meta["width"] < 1:
        raise StateDigestError("frame metadata width must be int >= 1", code=E_SCHEMA)
    if not _is_strict_int(meta["height"]) or meta["height"] < 1:
        raise StateDigestError("frame metadata height must be int >= 1", code=E_SCHEMA)
    if not _is_strict_int(meta["stride"]) or meta["stride"] < meta["width"] * 3:
        raise StateDigestError("frame metadata stride must be int >= width * 3", code=E_SCHEMA)
    if not isinstance(meta["pixel_format"], str) or not meta["pixel_format"]:
        raise StateDigestError("frame metadata pixel_format must be non-empty str", code=E_SCHEMA)
    if not _is_sha256_hex(meta["sha256"]):
        raise StateDigestError("frame metadata sha256 must be 64 lowercase hex", code=E_SCHEMA)
    if meta["binding_sha256"] is not None and not _is_sha256_hex(meta["binding_sha256"]):
        raise StateDigestError("frame metadata binding_sha256 must be 64 lowercase hex or None", code=E_SCHEMA)
    return []


# @anchor:wa.state.w8u_digest.build_receipt_t2_state_evidence
def build_receipt_t2_state_evidence(
    *,
    run_id: str,
    lease_id: str,
    target_handle: object,
    epoch: int,
    bound_final_state: BoundStateDigest | None = None,
    simulation_state: object | None = None,
    t2_trace_events: Sequence[Mapping[str, Any]] | None = None,
    t2_terminal_record: Mapping[str, Any] | None = None,
    t2_ticks: int | None = None,
    frames_captured: int = 0,
    first_frame: Mapping[str, Any] | None = None,
    last_frame: Mapping[str, Any] | None = None,
    ui_actions_count: int = 0,
    admitted_actions: int = 0,
    refused_actions: int = 0,
    uncertain_actions: int = 0,
    action_log_sha256: str | None = None,
    ui_gate_verdict: str = "accepted",
    ui_gate_code: str = OK_ADMITTED,
    pair_mismatches: int = 0,
    frame_gaps: int = 0,
    frames_dropped: int = 0,
    x11_tcp_listeners: int = 0,
    x11_unix_listeners: int = 0,
    x11_abstract_listeners: int = 0,
    reader_exit_code: int = 0,
    reader_quiescent: bool = True,
    ram_purged: bool = True,
    frames_disk_bytes: int = 0,
    swap_delta_bytes: int = 0,
    metadata_only: bool = False,
) -> dict[str, Any]:
    """Assemble and validate a `w8u.receipt/v1` receipt dictionary with T2 STATE and simulation separation."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id) or ".." in run_id or "/" in run_id:
        raise StateDigestError("invalid run_id for w8u.receipt/v1", code=E_SCHEMA)
    if not isinstance(lease_id, str) or not lease_id.strip():
        raise StateDigestError("invalid lease_id for w8u.receipt/v1", code=E_SCHEMA)
    norm_h = _normalize_handle(target_handle, field_name="target_handle")
    if not _is_strict_int(epoch) or epoch < 0:
        raise StateDigestError("receipt epoch must be a non-negative int", code=E_SCHEMA)

    final_state_sha256: str | None = None
    sim_sha256: str | None = None
    if bound_final_state is not None:
        if not isinstance(bound_final_state, BoundStateDigest):
            raise StateDigestError("bound_final_state must be a BoundStateDigest", code=E_STATE)
        if bound_final_state.handle != norm_h:
            raise ForeignFrameError("bound_final_state handle != receipt target_handle", code=E_FOREIGN_FRAME)
        if bound_final_state.epoch != epoch:
            raise ObservationBindingError("bound_final_state epoch != receipt epoch", code=E_EPOCH)
        final_state_sha256 = bound_final_state.state_sha256
        sim_sha256 = bound_final_state.simulation_state_sha256

    if simulation_state is not None:
        computed_sim = compute_simulation_state_digest(simulation_state)
        if sim_sha256 is not None and sim_sha256 != computed_sim:
            raise DomainSeparationError("simulation_state digest differs from bound_final_state.simulation_state_sha256", code=E_DOMAIN_COLLISION)
        sim_sha256 = computed_sim

    t2_trace_sha256: str | None = None
    if t2_trace_events is not None:
        float_errs = _check_no_float_or_nan(list(t2_trace_events), path="$.t2_trace_events")
        if float_errs:
            raise InvalidStateContractError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
        t2_trace_sha256 = domain_sha256(DIGEST_DOMAINS["t2_trace"], schema.dumps(list(t2_trace_events)).encode("utf-8"))

    t2_terminal_sha256: str | None = None
    if t2_terminal_record is not None:
        float_errs = _check_no_float_or_nan(t2_terminal_record, path="$.t2_terminal_record")
        if float_errs:
            raise InvalidStateContractError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
        t2_terminal_sha256 = domain_sha256(DIGEST_DOMAINS["t2_terminal"], schema.dumps(dict(t2_terminal_record)).encode("utf-8"))

    if frames_captured == 0 and not metadata_only:
        first_frame, last_frame, ui_gate_verdict = None, None, "indeterminate"

    receipt = {
        "run_id": run_id, "lease_id": lease_id,
        "target_handle": {"kind": norm_h.kind, "canonical_id": norm_h.canonical_id, "generation": norm_h.generation},
        "epoch": epoch, "frames_root": RECEIPT_FRAMES_ROOT,
        "frame_run_root": f"{RECEIPT_FRAMES_ROOT}/opensuperficies/os-cu-0/{run_id}",
        "source": RECEIPT_SOURCE, "frames_captured": frames_captured,
        "first_frame": copy.deepcopy(dict(first_frame)) if first_frame is not None else None,
        "last_frame": copy.deepcopy(dict(last_frame)) if last_frame is not None else None,
        "ui_actions_count": ui_actions_count, "admitted_actions": admitted_actions,
        "refused_actions": refused_actions, "uncertain_actions": uncertain_actions,
        "action_log_sha256": action_log_sha256, "t2_trace_sha256": t2_trace_sha256,
        "t2_terminal_sha256": t2_terminal_sha256, "t2_ticks": t2_ticks,
        "final_state_sha256": final_state_sha256, "simulation_state_sha256": sim_sha256,
        "ui_gate_verdict": ui_gate_verdict, "ui_gate_code": ui_gate_code,
        "pair_mismatches": pair_mismatches, "frame_gaps": frame_gaps, "frames_dropped": frames_dropped,
        "x11_tcp_listeners": x11_tcp_listeners, "x11_unix_listeners": x11_unix_listeners,
        "x11_abstract_listeners": x11_abstract_listeners, "reader_exit_code": reader_exit_code,
        "reader_quiescent": reader_quiescent, "ram_purged": ram_purged,
        "frames_disk_bytes": frames_disk_bytes, "swap_delta_bytes": swap_delta_bytes, "metadata_only": metadata_only,
    }
    validate_receipt_t2_state_evidence(receipt)
    return receipt


# @anchor:wa.state.w8u_digest.validate_receipt_t2_state_evidence
def validate_receipt_t2_state_evidence(receipt: object) -> list[str]:
    """Validate a `w8u.receipt/v1` dictionary including T2 STATE and frame witness invariants."""
    if not isinstance(receipt, Mapping):
        raise StateDigestError("receipt must be a mapping", code=E_SCHEMA)
    float_errs = _check_no_float_or_nan(receipt, path="$.receipt")
    if float_errs:
        raise InvalidStateContractError("; ".join(float_errs), code=E_FLOAT, errors=float_errs)
    if sorted(receipt.keys()) != sorted(RECEIPT_KEYS):
        raise StateDigestError(f"receipt keys {sorted(receipt.keys())} != {sorted(RECEIPT_KEYS)}", code=E_SCHEMA)
    run_id = receipt["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id) or ".." in run_id or "/" in run_id:
        raise StateDigestError("receipt.run_id is invalid", code=E_SCHEMA)
    if receipt["frames_root"] != RECEIPT_FRAMES_ROOT:
        raise StateDigestError(f"receipt.frames_root must be {RECEIPT_FRAMES_ROOT!r}", code=E_SCHEMA)
    expected_run_root = f"{RECEIPT_FRAMES_ROOT}/opensuperficies/os-cu-0/{run_id}"
    if receipt["frame_run_root"] != expected_run_root:
        raise StateDigestError(f"receipt.frame_run_root must be {expected_run_root!r}", code=E_SCHEMA)
    if receipt["source"] != RECEIPT_SOURCE:
        raise StateDigestError(f"receipt.source must be {RECEIPT_SOURCE!r}", code=E_SCHEMA)
    _normalize_handle(receipt["target_handle"], field_name="receipt.target_handle")

    for int_field in (
        "epoch", "frames_captured", "ui_actions_count", "admitted_actions", "refused_actions",
        "uncertain_actions", "pair_mismatches", "frame_gaps", "frames_dropped", "x11_tcp_listeners",
        "x11_unix_listeners", "x11_abstract_listeners", "reader_exit_code", "frames_disk_bytes",
    ):
        if not _is_strict_int(receipt[int_field]) or receipt[int_field] < 0:
            raise StateDigestError(f"receipt.{int_field} must be a non-negative int", code=E_SCHEMA)
    if not _is_strict_int(receipt["swap_delta_bytes"]):
        raise StateDigestError("receipt.swap_delta_bytes must be an int", code=E_SCHEMA)
    if receipt["frames_disk_bytes"] != 0:
        raise StateDigestError("STORAGE LAW violation: frames_disk_bytes must be 0", code=E_SCHEMA)
    for bool_field in ("reader_quiescent", "ram_purged", "metadata_only"):
        if type(receipt[bool_field]) is not bool:
            raise StateDigestError(f"receipt.{bool_field} must be bool", code=E_SCHEMA)
    if receipt["t2_ticks"] is not None and (not _is_strict_int(receipt["t2_ticks"]) or receipt["t2_ticks"] < 0):
        raise StateDigestError("receipt.t2_ticks must be a non-negative int or None", code=E_SCHEMA)

    for sha_field in ("action_log_sha256", "t2_trace_sha256", "t2_terminal_sha256", "final_state_sha256", "simulation_state_sha256"):
        val = receipt[sha_field]
        if val is not None and not _is_sha256_hex(val):
            raise StateDigestError(f"receipt.{sha_field} must be 64 lowercase hex or None", code=E_SCHEMA)
    if receipt["final_state_sha256"] is not None and receipt["simulation_state_sha256"] is not None and receipt["final_state_sha256"] == receipt["simulation_state_sha256"]:
        raise DomainSeparationError("final_state_sha256 and simulation_state_sha256 must not collide", code=E_DOMAIN_COLLISION)

    if receipt["frames_captured"] == 0:
        if receipt["first_frame"] is not None or receipt["last_frame"] is not None:
            raise StateDigestError("zero-frame run must have first_frame=None and last_frame=None", code=E_SCHEMA)
        if not receipt["metadata_only"] and receipt["ui_gate_verdict"] != "indeterminate":
            raise StateDigestError("pixel-required zero-frame run must have ui_gate_verdict='indeterminate'", code=E_SCHEMA)
    else:
        if receipt["first_frame"] is None or receipt["last_frame"] is None:
            raise StateDigestError("captured frames > 0 requires first_frame and last_frame metadata", code=E_SCHEMA)
        validate_frame_metadata(receipt["first_frame"])
        validate_frame_metadata(receipt["last_frame"])
        for fmeta in (receipt["first_frame"], receipt["last_frame"]):
            if receipt["final_state_sha256"] is not None and fmeta["sha256"] == receipt["final_state_sha256"]:
                raise DomainSeparationError("frame sha256 cannot equal final_state_sha256", code=E_DOMAIN_COLLISION)
            if receipt["simulation_state_sha256"] is not None and fmeta["sha256"] == receipt["simulation_state_sha256"]:
                raise DomainSeparationError("frame sha256 cannot equal simulation_state_sha256", code=E_DOMAIN_COLLISION)
    return []


# @anchor:wa.state.w8u_digest.build_training_state_evidence
def build_training_state_evidence(
    observation: object,
    sidecar: object,
    *,
    simulation_state: object | None = None,
    t2_tick: int | None = None,
    t2_tick_valid: bool = False,
    expected_contract_revision: str = W8U_OS_CONTRACT_REVISION,
    frame_trust_check: Callable[[object], bool | Sequence[str]] | None = None,
    validator: Callable[[Any], Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Build verified training evidence from a bound observation without persisting pixel bytes."""
    handle, epoch, frame_seq, dpr_ppm, state, _ = validate_observation_identity(
        observation, expected_contract_revision=expected_contract_revision,
        frame_trust_check=frame_trust_check, validator=validator,
    )
    bound = bind_observation_digest(
        observation, sidecar, simulation_state=simulation_state,
        expected_contract_revision=expected_contract_revision,
        frame_trust_check=frame_trust_check, validator=validator,
    )
    if type(t2_tick_valid) is not bool:
        raise StateDigestError("t2_tick_valid must be bool", code=E_SCHEMA)
    if not t2_tick_valid:
        if t2_tick is not None:
            raise StateDigestError("t2_tick must be None when t2_tick_valid is False", code=E_SCHEMA)
    elif not _is_strict_int(t2_tick) or t2_tick < 0:
        raise StateDigestError("t2_tick must be a non-negative int when t2_tick_valid is True", code=E_SCHEMA)

    payload = {
        "schema": W8U_TRAINING_EVIDENCE_SCHEMA_ID,
        "handle": {"kind": handle.kind, "canonical_id": handle.canonical_id, "generation": handle.generation},
        "epoch": epoch, "frame_seq": frame_seq, "dpr_ppm": dpr_ppm,
        "source_kind": bound.source_kind, "contract_revision": bound.contract_revision,
        "state": state, "state_sha256": bound.state_sha256, "ui_domain_sha256": bound.ui_domain_sha256,
        "frame_sha256": bound.frame_sha256, "frame_domain_sha256": bound.frame_domain_sha256,
        "simulation_state_sha256": bound.simulation_state_sha256,
        "sidecar_sha256": bound.sidecar_sha256, "binding_sha256": bound.binding_sha256,
        "t2_tick": t2_tick, "t2_tick_valid": t2_tick_valid, "metadata_only": True,
    }
    payload["evidence_sha256"] = domain_sha256(DIGEST_DOMAINS["training_evidence"], schema.dumps(payload).encode("utf-8"))
    return payload


# @anchor:wa.state.w8u_digest.validate_training_state_evidence
def validate_training_state_evidence(evidence: object) -> list[str]:
    """Validate a training evidence dictionary produced by `build_training_state_evidence`."""
    if not isinstance(evidence, Mapping):
        raise StateDigestError("training evidence must be a mapping", code=E_SCHEMA)
    if evidence.get("schema") != W8U_TRAINING_EVIDENCE_SCHEMA_ID:
        raise StateDigestError("invalid training evidence schema", code=E_SCHEMA)
    if evidence.get("metadata_only") is not True:
        raise StateDigestError("training evidence must have metadata_only=True", code=E_SCHEMA)
    for forbidden_key in ("rgb", "pixels", "frame_bytes", "raw_frame", "png", "xwd"):
        if forbidden_key in evidence:
            raise StateDigestError(f"STORAGE LAW violation: {forbidden_key} forbidden in training evidence", code=E_SCHEMA)
    state_errs = validate_closed_state(evidence.get("state"))
    if state_errs:
        raise InvalidStateContractError("; ".join(state_errs), code=E_STATE, errors=state_errs)
    verify_state_digest(evidence["state"], evidence.get("state_sha256", ""))
    return []


# @anchor:wa.state.w8u_digest.DiskSinkSpy
class DiskSinkSpy:
    """Context manager spy that intercepts filesystem write attempts to enforce the STORAGE LAW."""
    def __init__(self) -> None:
        self.write_attempts: list[tuple[str, str]] = []
        self._orig_open = builtins.open
        self._orig_io_open = io.open
        self._orig_os_open = os.open
        self._orig_write_bytes = pathlib.Path.write_bytes
        self._orig_write_text = pathlib.Path.write_text

    def __enter__(self) -> DiskSinkSpy:
        spy = self

        def _guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(m in mode for m in ("w", "a", "x", "+")):
                spy.write_attempts.append(("open", str(file)))
                raise PermissionError(f"STORAGE LAW disk write blocked by DiskSinkSpy: {file!r}")
            return spy._orig_open(file, mode, *args, **kwargs)

        def _guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                spy.write_attempts.append(("os.open", str(path)))
                raise PermissionError(f"STORAGE LAW os.open write blocked by DiskSinkSpy: {path!r}")
            return spy._orig_os_open(path, flags, *args, **kwargs)

        def _guarded_write_bytes(path_self: pathlib.Path, data: bytes) -> int:
            spy.write_attempts.append(("Path.write_bytes", str(path_self)))
            raise PermissionError(f"STORAGE LAW Path.write_bytes blocked by DiskSinkSpy: {path_self!r}")

        def _guarded_write_text(path_self: pathlib.Path, data: str, *args: Any, **kwargs: Any) -> int:
            spy.write_attempts.append(("Path.write_text", str(path_self)))
            raise PermissionError(f"STORAGE LAW Path.write_text blocked by DiskSinkSpy: {path_self!r}")

        builtins.open = _guarded_open
        io.open = _guarded_open
        os.open = _guarded_os_open
        pathlib.Path.write_bytes = _guarded_write_bytes  # type: ignore[method-assign]
        pathlib.Path.write_text = _guarded_write_text  # type: ignore[method-assign]
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        builtins.open = self._orig_open
        io.open = self._orig_io_open
        os.open = self._orig_os_open
        pathlib.Path.write_bytes = self._orig_write_bytes  # type: ignore[method-assign]
        pathlib.Path.write_text = self._orig_write_text  # type: ignore[method-assign]


# @anchor:wa.state.w8u_digest.benchmark_state_digest_vm
def benchmark_state_digest_vm(
    state: object,
    *,
    clock: Callable[[], int],
    iterations: int = 1,
    validator: Callable[[Any], Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Run a deterministic VM-labeled STATE digest timing measurement using an injected clock."""
    if not callable(clock):
        raise StateDigestError("clock must be callable", code=E_SCHEMA)
    if not _is_strict_int(iterations) or iterations < 1:
        raise StateDigestError("iterations must be an int >= 1", code=E_SCHEMA)
    start_ns = clock()
    if not _is_strict_int(start_ns) or start_ns < 0:
        raise StateDigestError("clock must return non-negative int nanoseconds", code=E_SCHEMA)
    digest = ""
    for _ in range(iterations):
        digest = compute_state_digest(state, validator=validator)
    end_ns = clock()
    if not _is_strict_int(end_ns) or end_ns < start_ns:
        raise StateDigestError("clock must be monotonic non-decreasing", code=E_SCHEMA)
    elapsed_ns = end_ns - start_ns
    return {
        "digest": digest, "iterations": iterations, "elapsed_ns": elapsed_ns,
        "per_digest_ns": elapsed_ns // iterations, "timing_label": TIMING_LABEL_VM,
        "host_state_digest_ns": AUSENTE, "limits_status": LIMITS_STATUS_TARGET,
    }
