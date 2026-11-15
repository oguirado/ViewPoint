"""Compatibility exports for :mod:`vadapt.w8u_memory`.

Implementation is split into contracts and cohesive operations; public names are stable.
"""
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
from ._memory_receipt import (
    project_receipt, _FaultyUnmapDropsChargeLedger, verify_purge_settlement_invariant,
)
