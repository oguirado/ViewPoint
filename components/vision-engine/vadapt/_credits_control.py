"""w8u_credits: credits control."""
from dataclasses import dataclass
import hashlib
import json
import os
import struct
from typing import Any, Callable
from vadapt import rtring
from vadapt import window_stream_bank

from ._credits_binding import (
    AUSENTE, U16_MAX, U32_MAX, U64_MAX, FRAME_MAGIC, FRAME_VERSION, FRAME_HEADER_BYTES,
    PIXEL_FORMAT_RGB24, FLAG_PAIR_UNDER_GRAB, FLAG_TICK_VALID, ALLOWED_FRAME_FLAGS_MASK,
    GEOMETRY_OVERHEAD_BYTES, INNER_LENGTH_BYTES, CONTROL_MAGIC_HELLO, CONTROL_MAGIC_CONFIRM,
    CONTROL_MAGIC_END, CONTROL_HEADER_BYTES, CONTROL_KIND_HELLO, CONTROL_KIND_ACK,
    CONTROL_KIND_STOP, HELLO_MAX_BODY_BYTES, PREFERRED_INITIAL_GRANT, FRAMES_ROOT,
    CONFINED_RUN_PREFIX, RECEIPT_SCHEMA, FRAME_HEADER_STRUCT, INNER_LEN_STRUCT,
    CONTROL_HEADER_STRUCT, ACK_BODY_STRUCT, END_BODY_STRUCT, XWD_HEADER_STRUCT, CLOSED_ERRORS,
    BINDING_KEYS, TARGET_HANDLE_KEYS, FRAME_METADATA_KEYS, W8UCreditError, _require_exact_int,
    _require_hex_sha256, _require_nonempty_str, InjectedClock, ContractEvidence,
    validate_contract_evidence, _validate_canonical_json_value, canonical_json_bytes,
    parse_canonical_binding_json, validate_roots, geometry_cap_bytes, effective_record_cap,
    ConfirmedBinding, validate_binding,
)
from ._credits_wire import (
    translate_native_xwd_error, _mask_shift, validate_and_convert_xwd, build_xwd_bytes,
    compute_rgb24_sha256, compute_ui_sha256, compute_sim_sha256, ValidatedFrame,
    encode_wire_frame, validate_wire_frame,
)

@dataclass(frozen=True)
class DecodedControl:
    """Decoded W8U control envelope and parsed body."""

    magic: bytes
    version: int
    kind: int
    body_bytes: int
    binding_sha256: bytes
    payload: dict[str, Any]


def encode_control_frame(
    *,
    magic: bytes,
    version: int,
    kind: int,
    binding_sha256: bytes,
    body: bytes,
    contract: ContractEvidence,
) -> bytes:
    """Encode a control message using frozen contract magic/version/kind/body bounds."""
    contract = validate_contract_evidence(contract)
    spec = contract.control_specs.get((bytes(magic), kind))
    if spec is None or spec.get("version") != version:
        raise W8UCreditError("E_WIRE", f"unrecognized control magic/kind/version: {(magic, kind, version)!r}")
    min_b = spec["min_body_bytes"]
    max_b = spec["max_body_bytes"]
    if len(body) < min_b or len(body) > max_b:
        raise W8UCreditError("E_LENGTH", f"control body length {len(body)} outside [{min_b}, {max_b}]")
    if len(binding_sha256) != 32:
        raise W8UCreditError("E_BINDING", "control binding_sha256 must be 32 raw bytes")
    hdr = CONTROL_HEADER_STRUCT.pack(magic, version, kind, len(body), bytes(binding_sha256))
    return hdr + bytes(body)


def decode_control_frame(
    raw_bytes: bytes,
    *,
    contract: ContractEvidence,
    expected_binding_sha256: bytes | None = None,
) -> DecodedControl:
    """Decode and validate a W8U control frame without magic resync."""
    contract = validate_contract_evidence(contract)
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise W8UCreditError("E_WIRE", "control frame must be bytes")
    raw = bytes(raw_bytes)
    if len(raw) < CONTROL_HEADER_BYTES:
        raise W8UCreditError("E_LENGTH", "control frame shorter than 44-byte header")

    magic, version, kind, body_bytes, binding_sha256 = CONTROL_HEADER_STRUCT.unpack_from(raw, 0)
    spec = contract.control_specs.get((magic, kind))
    if spec is None or spec.get("version") != version:
        raise W8UCreditError("E_WIRE", f"invalid control header {(magic, version, kind)!r} (no magic resync)")

    min_b = spec["min_body_bytes"]
    max_b = spec["max_body_bytes"]
    if body_bytes < min_b or body_bytes > max_b or len(raw) != CONTROL_HEADER_BYTES + body_bytes:
        raise W8UCreditError("E_LENGTH", "control body_bytes mismatch or out of contract bounds")

    if expected_binding_sha256 is not None and binding_sha256 != expected_binding_sha256:
        raise W8UCreditError("E_BINDING", "control binding_sha256 mismatch")

    body = raw[CONTROL_HEADER_BYTES:]
    role = spec["role"]
    if role == "hello":
        binding_dict = parse_canonical_binding_json(body)
        payload: dict[str, Any] = {"binding": binding_dict, "raw_body": body}
    elif role == "hello_confirm":
        if body != spec.get("expected_body", b""):
            raise W8UCreditError("E_WIRE", "invalid W8UH confirmation body")
        payload = {"confirmed": True}
    elif role == "ack":
        if len(body) != 16:
            raise W8UCreditError("E_LENGTH", "ACK body must be 16 bytes")
        ack_seq, credit_total = ACK_BODY_STRUCT.unpack(body)
        payload = {"ack_seq": ack_seq, "cumulative_credit_total": credit_total}
    elif role == "stop":
        if len(body) != 0:
            raise W8UCreditError("E_LENGTH", "STOP body must be empty")
        payload = {"stop": True}
    elif role == "end":
        if len(body) != 20:
            raise W8UCreditError("E_LENGTH", "W8UE END body must be 20 bytes")
        last_seq, sent, error_code = END_BODY_STRUCT.unpack(body)
        inv_map = {code_num: name for name, code_num in contract.end_error_codes.items()}
        if error_code not in inv_map:
            raise W8UCreditError("E_WIRE", f"unknown numeric END error_code {error_code}")
        payload = {
            "last_seq": last_seq,
            "sent": sent,
            "error_code": error_code,
            "error_name": inv_map[error_code],
        }
    else:
        raise W8UCreditError("E_WIRE", f"unsupported control role {role!r}")

    return DecodedControl(
        magic=magic,
        version=version,
        kind=kind,
        body_bytes=body_bytes,
        binding_sha256=binding_sha256,
        payload=payload,
    )


@dataclass(frozen=True)
class ConsumerLease:
    """Active recording-consumer lease on a validated published frame."""

    seq: int
    acquired_mono_ns: int
    deadline_mono_ns: int


@dataclass(frozen=True)
class CaptureOutcome:
    """Result of a single capture attempt on the frame state machine."""

    paused: bool
    seq: int | None
    frame: ValidatedFrame | None = None
    lease: ConsumerLease | None = None


