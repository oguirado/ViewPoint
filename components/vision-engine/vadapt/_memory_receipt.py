"""w8u_memory: memory receipt."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
from vadapt import ramws, rtring, window_stream_bank

from ._memory_contract import (
    AUSENTE, PREPARED, ACCEPT, GATE_NAMES, CLOSED_ERROR_CODES, RESERVATION_CATEGORIES,
    BINDING_KEYS, TARGET_HANDLE_KEYS, FRAME_METADATA_KEYS, RECEIPT_SCHEMA,
    DEFAULT_RECEIPT_SECTIONS, FRAME_MAGIC, FRAME_VERSION, FRAME_HEADER_BYTES,
    FRAME_PIXEL_FORMAT_RGB24, FLAG_PAIR_UNDER_GRAB, FLAG_TICK_VALID, FLAG_ALLOWED_MASK,
    CONTROL_MAGIC_CLIENT, CONTROL_MAGIC_HELLO_ACK, CONTROL_MAGIC_END, CONTROL_VERSION,
    CONTROL_HEADER_BYTES, CONTROL_KIND_HELLO, CONTROL_KIND_ACK, CONTROL_KIND_STOP,
    HELLO_MAX_BODY_BYTES, PREFERRED_INITIAL_CREDIT_GRANT, MIB, MAX_U16, MAX_U32, MAX_U64,
    CONFINED_TOOL, CONFINED_CAMPAIGN, _FRAME_HDR_STRUCT, _PAIR_LEN_STRUCT, _CTRL_HDR_STRUCT,
    _ACK_BODY_STRUCT, _END_BODY_STRUCT, _XWD_HDR_STRUCT, _HEX64_CHARS, W8UMemoryError,
    _exact_int, _checked_mul, _checked_sum, _is_hex64, domain_sha256, REQUIRED_CONTRACT_FIELDS,
    ContractEvidence, inspect_contract_evidence, require_accepted_contract,
    translate_native_error, canonical_rgb24_bytes, actual_ring_bytes, geometry_record_cap,
    effective_record_cap, canonical_json_bytes, parse_canonical_json_bytes,
    validate_confined_roots, validate_binding,
)
from ._memory_wire import (
    validate_and_convert_xwd, build_synthetic_xwd, pack_frame_record, validate_frame_record,
    pack_control_frame, parse_control_frame,
)
from ._memory_reservation import (
    TelemetryObservation, validate_telemetry_for_admission, ReservationSpec, FakeRingBuffer,
    FakeChannelPipe, PhysicalMappingEntry, CapsuleClaimEntry,
)
from ._memory_accountant import (HostAdmissionAccountant)

def project_receipt(
    *,
    contract,
    identity: dict,
    admitted_actions: int | None = None,
    applied_actions: int | None = None,
    frames_captured: int | None = None,
    frames_published: int | None = None,
    first_frame: dict | None = None,
    last_frame: dict | None = None,
    foreign_action: dict | None = None,
    foreign_t2: dict | None = None,
    foreign_state: dict | None = None,
    verdict: str = "PASS",
    code: str = "OK",
    storage_evidence: dict | None = None,
    listener_evidence: dict | None = None,
    exit_evidence: dict | None = None,
    cleanup_evidence: dict | None = None,
    retention_mode: str = "metadata_only",
    pixel_replay_claimed: bool = False,
    input_authority_claimed: bool = False,
) -> dict:
    """Project a w8u.receipt/v1 receipt keeping unknown values None (never 0) and enforcing storage/input laws."""
    require_accepted_contract(contract, error_code="E_BINDING")
    if input_authority_claimed:
        raise W8UMemoryError("E_BINDING", "capture grants no input authority; closed action variants belong to input owner")

    purged = bool(cleanup_evidence and cleanup_evidence.get("purged") is True)
    if (retention_mode == "metadata_only" or purged) and pixel_replay_claimed:
        raise W8UMemoryError(
            "E_RAM_QUOTA",
            "hash-only retention is metadata_only and cannot claim pixel replay after purge",
        )

    for frame_meta in (first_frame, last_frame):
        if frame_meta is not None:
            if tuple(frame_meta.keys()) != FRAME_METADATA_KEYS:
                raise W8UMemoryError("E_WIRE", "receipt frame metadata must match closed FRAME_METADATA_KEYS")

    # Unknown remains None (null), never defaulted to 0; applied_actions and admitted_actions are distinct
    populations = {
        "admitted_actions": admitted_actions,
        "applied_actions": applied_actions,
        "frames_captured": frames_captured,
        "frames_published": frames_published,
    }

    storage_ev = dict(storage_evidence or {})
    storage_ev["retention_mode"] = retention_mode
    storage_ev["pixel_replay_available"] = bool(not purged and retention_mode != "metadata_only" and pixel_replay_claimed)
    storage_ev["input_authority_granted"] = False

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "identity": identity,
        "populations": populations,
        "first_frame": first_frame,
        "last_frame": last_frame,
        "foreign_action": foreign_action,
        "foreign_t2": foreign_t2,
        "foreign_state": foreign_state,
        "verdict": verdict,
        "code": code,
        "storage_evidence": storage_ev,
        "listener_evidence": listener_evidence,
        "exit_evidence": exit_evidence,
        "cleanup_evidence": cleanup_evidence,
    }

    closed_fields = (
        getattr(contract, "receipt_closed_fields", DEFAULT_RECEIPT_SECTIONS)
        if not isinstance(contract, dict)
        else contract.get("receipt_closed_fields", DEFAULT_RECEIPT_SECTIONS)
    )
    if set(receipt.keys()) != set(closed_fields):
        raise W8UMemoryError("E_BINDING", "receipt keys do not match contract closed receipt fields")
    return receipt


# ---------------------------------------------------------------------------
# Must-FAIL Fixture Auditor & Independent Positive Control
# ---------------------------------------------------------------------------

class _FaultyUnmapDropsChargeLedger:
    """Deliberately faulty fixture: unmap error drops outstanding charge (must-FAIL fixture)."""

    def __init__(self, charge: int):
        self.reserved_bytes = charge
        self.unmap_proven = False

    def close_with_unmap_error(self):
        # BUG: drops outstanding charge even though unmap failed!
        self.unmap_proven = False
        self.reserved_bytes = 0


def verify_purge_settlement_invariant(ledger_or_accountant, *, expected_charge: int, unmap_proven: bool) -> None:
    """Audit that an unmap failure never drops the outstanding charge."""
    actual = getattr(ledger_or_accountant, "ownership_reserved_bytes", getattr(ledger_or_accountant, "reserved_bytes", None))
    if not unmap_proven and actual != expected_charge:
        raise W8UMemoryError(
            "E_RAM_QUOTA",
            "Purge settlement violation: unmap error dropped outstanding charge (%r != %d)" % (actual, expected_charge),
        )
