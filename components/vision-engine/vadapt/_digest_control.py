"""w8u_digest: digest control."""
from __future__ import annotations
import hashlib
import json
import os
import posixpath
import re
import struct
from collections import namedtuple
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from vadapt import xfb

from ._digest_pixels import (
    AUSENTE, STATUS_PREPARED, STATUS_ACCEPTED, RECEIPT_SCHEMA_V1, REQUIRED_FRAMES_ROOT,
    REQUIRED_RUN_CHILD_PREFIX, W8UF_MAGIC, W8UF_VERSION, W8UF_HEADER_BYTES,
    W8UF_PIXEL_FORMAT_RGB24, W8UF_FLAG_PAIR_UNDER_GRAB, W8UF_FLAG_TICK_VALID,
    W8UF_ALLOWED_FLAGS_MASK, W8UF_HEADER_STRUCT, W8UF_INNER_LENGTH_BYTES,
    W8UF_GEOMETRY_SLACK_BYTES, W8UC_MAGIC, W8UH_MAGIC, W8UE_MAGIC, CONTROL_HEADER_STRUCT,
    CONTROL_HEADER_BYTES, HELLO_MAX_BODY_BYTES, CONTROL_KIND_HELLO, CONTROL_KIND_ACK,
    CONTROL_KIND_STOP, PREFERRED_INITIAL_CREDIT_GRANT, MAX_U64, MAX_U32, CLOSED_ERRORS,
    NATIVE_TO_W8U_ERROR_MAP, BINDING_KEYS, TARGET_HANDLE_KEYS, FRAME_METADATA_KEYS,
    FORBIDDEN_METADATA_SUBSTRINGS, FORBIDDEN_METADATA_KEY_TOKENS, HEX64_RE, W8UFFrameHeader,
    ValidatedW8UFrame, ControlFrame, W8UDigestError, _is_exact_int, _is_exact_bool, _is_hex64,
    _ensure_bytes_like, ContractEvidence, require_contract_evidence, numeric_error_code,
    translate_native_xwd_error, _extract_xwd_bounds, validate_w8u_xwd, canonical_rgb24_bytes,
    rgb24_sha256_raw, rgb24_sha256_hex, validate_raw_rgb24_buffer, rgba8_from_rgb24,
    rgba8_from_xwd, compare_rgba8_engine_buffer, _resolve_pixel_source, _normalize_digest_bytes,
)
from ._digest_binding import (
    build_synthetic_xwd, build_bgrx_xwd, build_xrgb_xwd, _validate_canonical_json_value,
    canonical_json_bytes, _validate_confined_roots, validate_binding, domain_sha256_raw,
    domain_sha256_hex, compute_geometry_record_cap, compute_effective_record_cap,
)
from ._digest_records import (
    parse_w8uf_header, build_w8uf_record, validate_w8uf_record,
    project_frame_metadata_from_header, _check_no_forbidden_pixel_or_encoding_tricks,
    validate_frame_metadata, canonicalize_frame_metadata, hash_frame_metadata_raw,
    hash_frame_metadata_hex,
)

def project_u5_vision_receipt(
    *,
    contract: Any = AUSENTE,
    binding: Mapping[str, Any],
    binding_sha256: str,
    frames: Sequence[Mapping[str, Any]],
    retention_mode: str = "metadata_only",
    pixels_purged: bool = True,
    claim_pixel_replay: bool = False,
    foreign_action_receipt: Optional[Mapping[str, Any]] = None,
    foreign_t2_receipt: Optional[Mapping[str, Any]] = None,
    foreign_state_receipt: Optional[Mapping[str, Any]] = None,
    verdict: Optional[str] = None,
    error_code: Optional[str] = None,
    storage_evidence: Optional[Mapping[str, Any]] = None,
    listener_evidence: Optional[Mapping[str, Any]] = None,
    exit_evidence: Optional[Mapping[str, Any]] = None,
    cleanup_evidence: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Project U5 w8u.receipt/v1 vision metadata fields without fabricating foreign action/T2/STATE receipts.

    - Unknown foreign fields remain None (null), never 0.
    - Applied actions remain distinct from admitted actions.
    - Hash-only retention is metadata_only and cannot claim pixel replay after purge.
    - Capture grants no input authority.
    """
    ev = require_contract_evidence(contract, "receipt_closed_fields", "binding_schema_id", "digest_domains")
    if retention_mode != "metadata_only":
        raise W8UDigestError("E_WIRE", f"unsupported retention_mode {retention_mode!r}; require 'metadata_only'")
    if not _is_exact_bool(pixels_purged) or not _is_exact_bool(claim_pixel_replay):
        raise W8UDigestError("E_WIRE", "pixels_purged and claim_pixel_replay must be exact booleans")
    if retention_mode == "metadata_only" and pixels_purged and claim_pixel_replay:
        raise W8UDigestError("E_WIRE", "metadata_only retention cannot claim pixel replay after purge")

    validated_frames: List[Dict[str, Any]] = []
    tick_mode = binding.get("tick_mode") if isinstance(binding, Mapping) else None
    for idx, frame in enumerate(frames, start=1):
        clean_f = validate_frame_metadata(frame, tick_mode=tick_mode, expected_binding_sha256=binding_sha256)
        if clean_f["seq"] != idx:
            raise W8UDigestError("E_SEQ", f"receipt frame seq {clean_f['seq']} != contiguous {idx}")
        validated_frames.append(clean_f)

    first_frame = validated_frames[0] if validated_frames else None
    last_frame = validated_frames[-1] if validated_frames else None

    admitted_actions: Optional[int] = None
    applied_actions: Optional[int] = None
    action_receipt_proj: Optional[Dict[str, Any]] = None
    if foreign_action_receipt is not None:
        if not isinstance(foreign_action_receipt, Mapping):
            raise W8UDigestError("E_WIRE", "foreign_action_receipt must be a mapping or None")
        if foreign_action_receipt.get("input_authority_from_capture"):
            raise W8UDigestError("E_WIRE", "capture grants no input authority")
        adm = foreign_action_receipt.get("admitted_actions")
        app = foreign_action_receipt.get("applied_actions")
        if adm is not None and (not _is_exact_int(adm) or adm < 0):
            raise W8UDigestError("E_WIRE", "admitted_actions must be a non-negative exact int or None")
        if app is not None and (not _is_exact_int(app) or app < 0):
            raise W8UDigestError("E_WIRE", "applied_actions must be a non-negative exact int or None")
        admitted_actions = adm
        applied_actions = app
        action_receipt_proj = dict(foreign_action_receipt)

    numeric_err: Optional[int] = None
    if error_code is not None:
        numeric_err = numeric_error_code(error_code, contract=ev)

    receipt = {
        "schema": RECEIPT_SCHEMA_V1,
        "identity": {
            "run_id": binding["run_id"],
            "lease_id": binding["lease_id"],
            "container_id": binding["container_id"],
            "binding_sha256": binding_sha256,
            "target_handle": dict(binding["target_handle"]),
            "epoch": binding["epoch"],
            "display": binding["display"],
            "xid": binding["xid"],
            "width": binding["width"],
            "height": binding["height"],
            "tick_mode": binding["tick_mode"],
        },
        "populations": {
            "frames_validated": len(validated_frames),
            "committed_tick_frames": sum(1 for f in validated_frames if f["tick_valid"]),
            "unavailable_tick_frames": sum(1 for f in validated_frames if not f["tick_valid"]),
            "admitted_actions": admitted_actions,
            "applied_actions": applied_actions,
        },
        "first_frame": first_frame,
        "last_frame": last_frame,
        "retention": {
            "mode": retention_mode,
            "pixels_purged": pixels_purged,
            "pixel_replay_available": False if (retention_mode == "metadata_only" and pixels_purged) else claim_pixel_replay,
            "capture_grants_input_authority": False,
        },
        "foreign_receipts": {
            "action": action_receipt_proj,
            "t2": dict(foreign_t2_receipt) if isinstance(foreign_t2_receipt, Mapping) else None,
            "state": dict(foreign_state_receipt) if isinstance(foreign_state_receipt, Mapping) else None,
        },
        "verdict": {
            "status": verdict,
            "error_code": error_code,
            "numeric_error_code": numeric_err,
        },
        "evidence": {
            "storage": dict(storage_evidence) if isinstance(storage_evidence, Mapping) else None,
            "listener": dict(listener_evidence) if isinstance(listener_evidence, Mapping) else None,
            "exit": dict(exit_evidence) if isinstance(exit_evidence, Mapping) else None,
            "cleanup": dict(cleanup_evidence) if isinstance(cleanup_evidence, Mapping) else None,
        },
    }
    if set(receipt.keys()) != set(ev.receipt_closed_fields):
        raise W8UDigestError("E_WIRE", "receipt projection keys do not match contract receipt_closed_fields")
    return receipt


def parse_control_frame(
    buf: Any,
    *,
    contract: Any = AUSENTE,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
    eof_after_end: bool = True,
    joined_exit_after_end: bool = True,
) -> ControlFrame:
    """Validate a W8UC / W8UH / W8UE control frame without magic resync, requiring accepted contract authority."""
    ev = require_contract_evidence(contract, "control_specs", "end_error_codes", "binding_schema_id")
    mv = _ensure_bytes_like(buf, "E_WIRE")
    if len(mv) < CONTROL_HEADER_BYTES:
        raise W8UDigestError("E_LENGTH", f"control frame shorter than {CONTROL_HEADER_BYTES} bytes")

    magic, version, kind, body_bytes, binding_sha256 = CONTROL_HEADER_STRUCT.unpack(bytes(mv[:CONTROL_HEADER_BYTES]))
    if magic not in (W8UC_MAGIC, W8UH_MAGIC, W8UE_MAGIC):
        raise W8UDigestError("E_WIRE", f"unrecognized control magic {magic!r} (no magic resync)")
    if version != 1:
        raise W8UDigestError("E_WIRE", f"unsupported control version {version}")
    if len(mv) != CONTROL_HEADER_BYTES + body_bytes:
        raise W8UDigestError("E_LENGTH", f"control frame length {len(mv)} != {CONTROL_HEADER_BYTES + body_bytes}")

    if expected_binding_sha256 is not None:
        exp_bind = _normalize_digest_bytes(expected_binding_sha256, "E_BINDING")
        if binding_sha256 != exp_bind:
            raise W8UDigestError("E_BINDING", "control binding_sha256 mismatch")

    body = bytes(mv[CONTROL_HEADER_BYTES:])
    control_specs = ev.control_specs
    spec_key = (magic.decode("ascii"), kind)
    if not isinstance(control_specs, Mapping) or spec_key not in control_specs:
        raise W8UDigestError("E_WIRE", f"control magic/kind {spec_key!r} not frozen in contract")

    if magic == W8UC_MAGIC:
        if kind == CONTROL_KIND_HELLO:
            if body_bytes == 0 or body_bytes > HELLO_MAX_BODY_BYTES:
                raise W8UDigestError("E_LENGTH", f"HELLO body_bytes {body_bytes} outside [1, {HELLO_MAX_BODY_BYTES}]")
            try:
                decoded = json.loads(body.decode("utf-8", errors="strict"))
            except Exception as exc:
                raise W8UDigestError("E_WIRE", "invalid UTF-8 JSON in HELLO") from exc
            canon_bytes, raw_bind, hex_bind = validate_binding(decoded, contract=ev)
            if body != canon_bytes:
                raise W8UDigestError("E_WIRE", "HELLO body is not canonical integer-only compact UTF-8 JSON")
            if binding_sha256 != raw_bind:
                raise W8UDigestError("E_BINDING", "HELLO header binding_sha256 does not match body digest")
            parsed = {"type": "HELLO", "binding": decoded, "binding_sha256": hex_bind, "credit_granted": 0}
        elif kind == CONTROL_KIND_ACK:
            if body_bytes != 16:
                raise W8UDigestError("E_LENGTH", f"ACK body_bytes {body_bytes} != 16")
            ack_seq, cumulative_credit_total = struct.unpack(">QQ", body)
            parsed = {
                "type": "ACK",
                "ack_seq": ack_seq,
                "cumulative_credit_total": cumulative_credit_total,
            }
        elif kind == CONTROL_KIND_STOP:
            if body_bytes != 0:
                raise W8UDigestError("E_LENGTH", f"STOP body_bytes {body_bytes} != 0")
            parsed = {"type": "STOP"}
        else:
            raise W8UDigestError("E_WIRE", f"unknown W8UC kind {kind}")
    elif magic == W8UH_MAGIC:
        spec = control_specs[spec_key]
        expected_len = spec.get("body_bytes")
        if expected_len is not None and body_bytes != expected_len:
            raise W8UDigestError("E_LENGTH", f"W8UH body_bytes {body_bytes} != {expected_len}")
        parsed = {"type": "HELLO_CONFIRM", "kind": kind, "body": body}
    else:
        # W8UE END
        if body_bytes != 20:
            raise W8UDigestError("E_LENGTH", f"W8UE END body_bytes {body_bytes} != 20")
        last_seq, sent, error_code_num = struct.unpack(">QQI", body)
        end_codes = ev.end_error_codes
        if not isinstance(end_codes, Mapping) or error_code_num not in end_codes:
            raise W8UDigestError("E_WIRE", f"unknown W8UE numeric error_code {error_code_num}")
        if not eof_after_end or not joined_exit_after_end:
            raise W8UDigestError("E_WIRE", "W8UE END requires immediate EOF and joined exit")
        parsed = {
            "type": "END",
            "last_seq": last_seq,
            "sent": sent,
            "error_code": error_code_num,
            "error_symbol": end_codes[error_code_num],
        }

    return ControlFrame(
        magic=magic,
        version=version,
        kind=kind,
        body_bytes=body_bytes,
        binding_sha256=binding_sha256,
        body=body,
        parsed=parsed,
    )


class CreditWindowController:
    """Enforces W8U HELLO/ACK/STOP credit window, grab/ungrab ordering, and consumer lease release."""

    def __init__(self, max_inflight_frames: int, *, reserved_capacity_frames: Optional[int] = None) -> None:
        if not _is_exact_int(max_inflight_frames) or max_inflight_frames <= 0:
            raise W8UDigestError("E_CREDIT", "max_inflight_frames must be a positive exact integer")
        if reserved_capacity_frames is not None:
            if not _is_exact_int(reserved_capacity_frames) or reserved_capacity_frames < max_inflight_frames:
                raise W8UDigestError("E_CREDIT", "max_inflight_frames exceeds reserved capacity")
        self.max_inflight_frames = max_inflight_frames
        self.hello_received = False
        self.initial_ack_received = False
        self.ack_seq = 0
        self.cumulative_credit_total = 0
        self.acquired_seq = 0
        self.sent_seq = 0
        self.grab_active = False
        self.ungrab_completed_for_seq = 0
        self.published_seq = 0
        self.lease_released_seq = 0
        self.stopped = False

    @property
    def available_credit(self) -> int:
        return self.cumulative_credit_total - self.acquired_seq

    @property
    def inflight_frames(self) -> int:
        return self.acquired_seq - self.ack_seq

    def on_hello(self) -> int:
        """HELLO grants no credit."""
        if self.hello_received:
            raise W8UDigestError("E_WIRE", "duplicate HELLO")
        self.hello_received = True
        return 0

    def on_ack(self, ack_seq: int, cumulative_credit_total: int) -> Dict[str, Any]:
        """Process an ACK control frame and enforce credit window invariants."""
        if not self.hello_received:
            raise W8UDigestError("E_CREDIT", "ACK received before HELLO")
        if not (_is_exact_int(ack_seq) and _is_exact_int(cumulative_credit_total)):
            raise W8UDigestError("E_CREDIT", "ack_seq and cumulative_credit_total must be exact integers")
        if ack_seq < 0 or cumulative_credit_total < 0:
            raise W8UDigestError("E_CREDIT", "negative ACK parameters")

        if not self.initial_ack_received:
            expected_initial = min(PREFERRED_INITIAL_CREDIT_GRANT, self.max_inflight_frames)
            if ack_seq != 0 or cumulative_credit_total != expected_initial:
                raise W8UDigestError(
                    "E_CREDIT",
                    f"initial ACK must have ack_seq=0 and credit={expected_initial}, got ({ack_seq}, {cumulative_credit_total})",
                )
            self.initial_ack_received = True
            self.ack_seq = 0
            self.cumulative_credit_total = cumulative_credit_total
            return {"duplicate": False, "available_credit": self.available_credit}

        if ack_seq == self.ack_seq and cumulative_credit_total == self.cumulative_credit_total:
            return {"duplicate": True, "available_credit": self.available_credit}

        if ack_seq < self.ack_seq or cumulative_credit_total < self.cumulative_credit_total:
            raise W8UDigestError("E_CREDIT", "non-monotonic ACK regression or conflicting duplicate ACK")
        if ack_seq > self.sent_seq:
            raise W8UDigestError("E_SEQ", f"ACK ack_seq {ack_seq} exceeds sent_seq {self.sent_seq}")
        if ack_seq > self.published_seq or ack_seq > self.lease_released_seq:
            raise W8UDigestError(
                "E_CREDIT",
                "cannot ACK frame before validated publication and recording-consumer lease release",
            )
        if cumulative_credit_total - ack_seq > self.max_inflight_frames:
            raise W8UDigestError("E_CREDIT", "cumulative_credit_total inflates capacity beyond max_inflight_frames")

        self.ack_seq = ack_seq
        self.cumulative_credit_total = cumulative_credit_total
        return {"duplicate": False, "available_credit": self.available_credit}

    def acquire_credit_before_grab(self) -> int:
        """Acquire 1 frame credit before X server grab."""
        if self.stopped:
            raise W8UDigestError("E_WIRE", "stream already stopped")
        if not self.initial_ack_received or self.available_credit <= 0:
            raise W8UDigestError("E_BACKPRESSURE", "no credit available before grab")
        if self.grab_active:
            raise W8UDigestError("E_WIRE", "previous grab still active")
        self.acquired_seq += 1
        self.grab_active = True
        return self.acquired_seq

    def complete_checked_ungrab(self, seq: int, *, ungrab_ok: bool = True) -> None:
        """Complete checked ungrab before blocking output."""
        if not self.grab_active or seq != self.acquired_seq:
            raise W8UDigestError("E_WIRE", "ungrab does not match active grab seq")
        if not _is_exact_bool(ungrab_ok) or not ungrab_ok:
            raise W8UDigestError("E_WIRE", "checked ungrab failed")
        self.grab_active = False
        self.ungrab_completed_for_seq = seq

    def send_frame_after_ungrab(self, seq: int) -> None:
        """Emit frame to blocking output only after checked ungrab has completed."""
        if self.grab_active or self.ungrab_completed_for_seq != seq:
            raise W8UDigestError("E_WIRE", "must complete checked ungrab before blocking output")
        if seq != self.sent_seq + 1:
            raise W8UDigestError("E_SEQ", f"non-contiguous send seq {seq}")
        self.sent_seq = seq

    def record_validated_publication(self, seq: int) -> None:
        if seq != self.published_seq + 1 or seq > self.sent_seq:
            raise W8UDigestError("E_SEQ", "invalid publication sequence")
        self.published_seq = seq

    def release_recording_consumer_lease(self, seq: int) -> None:
        if seq != self.lease_released_seq + 1 or seq > self.published_seq:
            raise W8UDigestError("E_CREDIT", "cannot release recording-consumer lease before validated publication")
        self.lease_released_seq = seq

    def trigger_stop(self) -> bool:
        """STOP bypasses full queues immediately even when available_credit == 0."""
        self.stopped = True
        return True
