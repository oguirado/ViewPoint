"""w8u_memory: memory reservation."""
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

@dataclass(frozen=True)
class TelemetryObservation:
    """Host and cgroup slice memory telemetry snapshot."""

    cgroup_current_bytes: int | None = None
    cgroup_peak_bytes: int | None = None
    cgroup_ceiling_bytes: int | None = None
    host_mem_available_bytes: int | None = None
    tmpfs_allocated_bytes: int | None = None
    swap_used_bytes: int | None = None
    tmpfs_ceiling_bytes: int = ramws.HOST_MAX
    host_free_floor_bytes: int = 0
    max_swap_delta_bytes: int = 0


def validate_telemetry_for_admission(obs: TelemetryObservation, *, baseline_swap_bytes: int | None = None) -> dict:
    """Validate telemetry snapshot and compute the tighter proven host/slice limit.

    Absent current/ceiling or missing telemetry is unknown and refuses admission
    with E_RAM_QUOTA (never defaulted to 0).
    """
    if not isinstance(obs, TelemetryObservation):
        raise W8UMemoryError("E_RAM_QUOTA", "telemetry observation is missing or invalid")

    if obs.cgroup_current_bytes is None or obs.cgroup_ceiling_bytes is None:
        raise W8UMemoryError(
            "E_RAM_QUOTA",
            "absent cgroup current/ceiling telemetry is unknown and refuses admission",
        )
    if (
        obs.host_mem_available_bytes is None
        or obs.tmpfs_allocated_bytes is None
        or obs.swap_used_bytes is None
    ):
        raise W8UMemoryError("E_RAM_QUOTA", "missing host MemAvailable, tmpfs, or swap telemetry")

    cg_cur = _exact_int(obs.cgroup_current_bytes, "cgroup_current_bytes", allow_zero=True)
    cg_ceil = _exact_int(obs.cgroup_ceiling_bytes, "cgroup_ceiling_bytes", allow_zero=False)
    if obs.cgroup_peak_bytes is not None:
        cg_peak = _exact_int(obs.cgroup_peak_bytes, "cgroup_peak_bytes", allow_zero=True)
        if cg_peak < cg_cur:
            raise W8UMemoryError("E_RAM_QUOTA", "cgroup_peak_bytes (%d) < cgroup_current_bytes (%d)" % (cg_peak, cg_cur))
    else:
        cg_peak = None

    host_avail = _exact_int(obs.host_mem_available_bytes, "host_mem_available_bytes", allow_zero=True)
    tmpfs_alloc = _exact_int(obs.tmpfs_allocated_bytes, "tmpfs_allocated_bytes", allow_zero=True)
    swap_used = _exact_int(obs.swap_used_bytes, "swap_used_bytes", allow_zero=True)
    tmpfs_ceil = _exact_int(obs.tmpfs_ceiling_bytes, "tmpfs_ceiling_bytes", allow_zero=False)
    host_floor = _exact_int(obs.host_free_floor_bytes, "host_free_floor_bytes", allow_zero=True)
    max_swap_delta = _exact_int(obs.max_swap_delta_bytes, "max_swap_delta_bytes", allow_zero=True)

    if cg_cur >= cg_ceil:
        raise W8UMemoryError("E_RAM_QUOTA", "cgroup current (%d) >= ceiling (%d)" % (cg_cur, cg_ceil))
    if host_avail <= host_floor:
        raise W8UMemoryError("E_RAM_QUOTA", "host MemAvailable (%d) <= floor (%d)" % (host_avail, host_floor))
    if tmpfs_alloc >= tmpfs_ceil:
        raise W8UMemoryError("E_RAM_QUOTA", "tmpfs allocated (%d) >= ceiling (%d)" % (tmpfs_alloc, tmpfs_ceil))

    if baseline_swap_bytes is not None:
        base_sw = _exact_int(baseline_swap_bytes, "baseline_swap_bytes", allow_zero=True)
        swap_delta = max(0, swap_used - base_sw)
        if swap_delta > max_swap_delta:
            raise W8UMemoryError("E_RAM_QUOTA", "swap growth (%d) exceeds limit (%d)" % (swap_delta, max_swap_delta))

    slice_headroom = cg_ceil - cg_cur
    host_headroom = host_avail - host_floor
    tmpfs_headroom = tmpfs_ceil - tmpfs_alloc
    tighter_headroom = min(slice_headroom, host_headroom, tmpfs_headroom)
    governing_limit = (
        "cgroup_slice"
        if tighter_headroom == slice_headroom
        else ("host_mem_available" if tighter_headroom == host_headroom else "tmpfs_ceiling")
    )
    return {
        "cgroup_current_bytes": cg_cur,
        "cgroup_peak_bytes": cg_peak,
        "cgroup_ceiling_bytes": cg_ceil,
        "slice_headroom_bytes": slice_headroom,
        "host_headroom_bytes": host_headroom,
        "tmpfs_headroom_bytes": tmpfs_headroom,
        "tighter_headroom_bytes": tighter_headroom,
        "governing_limit": governing_limit,
    }


# ---------------------------------------------------------------------------
# Separate Ownership Charges, Physical Shared Mappings & Cleanup Settlement
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReservationSpec:
    """Input specification for a capsule stream reservation claim."""

    slots: int
    width: int
    height: int
    max_record_bytes: int
    max_inflight_frames: int
    decoding_copy_frames: int = 2
    consumer_view_slots: int = 1
    control_buffer_bytes: int = HELLO_MAX_BODY_BYTES + 4096
    views_share_ring_mapping: bool = True
    quota_bytes: int | None = None

    def compute_breakdown(self) -> dict[str, int]:
        slots = _exact_int(self.slots, "slots", max_value=MAX_U32)
        width = _exact_int(self.width, "width", max_value=MAX_U32)
        height = _exact_int(self.height, "height", max_value=MAX_U32)
        inflight = _exact_int(self.max_inflight_frames, "max_inflight_frames", max_value=MAX_U32)
        dec_copies = _exact_int(self.decoding_copy_frames, "decoding_copy_frames", max_value=MAX_U32)
        view_slots = _exact_int(self.consumer_view_slots, "consumer_view_slots", allow_zero=True, max_value=MAX_U32)
        ctrl_bytes = _exact_int(self.control_buffer_bytes, "control_buffer_bytes", max_value=MAX_U64)

        if slots < inflight:
            raise W8UMemoryError("E_RAM_QUOTA", "ring slots (%d) < max_inflight_frames (%d)" % (slots, inflight))

        ring_b = actual_ring_bytes(slots, width, height)
        rec_cap = effective_record_cap(width, height, self.max_record_bytes)
        pending_b = _checked_mul(inflight, rec_cap, "pending_raw_pair")
        rgb_b = canonical_rgb24_bytes(width, height)
        decoding_b = _checked_mul(dec_copies, rgb_b, "canonical_decoding_copies")
        views_b = _checked_mul(view_slots, rgb_b, "held_consumer_views")
        total = _checked_sum([ring_b, pending_b, decoding_b, views_b, ctrl_bytes], "total_ownership_bytes")

        if self.quota_bytes is not None:
            q = _exact_int(self.quota_bytes, "quota_bytes", max_value=MAX_U64)
            if total > q:
                raise W8UMemoryError("E_RAM_QUOTA", "claim total (%d) > spec quota_bytes (%d)" % (total, q))

        return {
            "ring_storage": ring_b,
            "pending_raw_pair": pending_b,
            "canonical_decoding_copies": decoding_b,
            "held_consumer_views": views_b,
            "control_buffers": ctrl_bytes,
            "total_ownership_bytes": total,
        }


class FakeRingBuffer:
    """In-memory ring buffer with controllable close/unmap behavior for offline tests."""

    def __init__(self, size: int, *, fail_close_times: int = 0, raise_on_close: Exception | None = None):
        self.size = _exact_int(size, "size")
        self.buffer = bytearray(self.size)
        self._fail_close_times = fail_close_times
        self._raise_on_close = raise_on_close
        self._released = False
        self.close_attempts = 0

    def close(self) -> bool:
        self.close_attempts += 1
        if self._raise_on_close is not None:
            exc = self._raise_on_close
            if self._fail_close_times > 0:
                self._fail_close_times -= 1
                if self._fail_close_times == 0:
                    self._raise_on_close = None
            raise exc
        if self._fail_close_times > 0:
            self._fail_close_times -= 1
            return False
        self._released = True
        self.buffer = None
        return True


class FakeChannelPipe:
    """In-memory fake control/frame channel tracking queue capacity, cancellation, EOF, and join witness."""

    def __init__(self, channel_id: str, *, max_queue_depth: int = 4):
        self.channel_id = channel_id
        self.max_queue_depth = _exact_int(max_queue_depth, "max_queue_depth")
        self.queue = []
        self.cancelled = False
        self.eof_seen = False
        self.exit_joined = False

    def send(self, msg: dict):
        if self.exit_joined:
            raise W8UMemoryError("E_WIRE", "channel already closed/joined")
        is_stop = isinstance(msg, dict) and msg.get("type") == "STOP"
        if not is_stop and len(self.queue) >= self.max_queue_depth:
            raise W8UMemoryError("E_BACKPRESSURE", "channel queue full")
        if is_stop:
            # STOP bypasses full queues and inserts at the head
            self.queue.insert(0, msg)
        else:
            self.queue.append(msg)
        return True


@dataclass
class PhysicalMappingEntry:
    """Physical shared mapping tracked separately from logical ownership claims."""

    mapping_id: int
    size_bytes: int
    owner: object
    active_claims: set = field(default_factory=set)
    attached_readers: set = field(default_factory=set)
    zero_copy_leases: set = field(default_factory=set)
    unmapped: bool = False


@dataclass
class CapsuleClaimEntry:
    """Per-capsule / per-epoch ownership claim and credit/cleanup ledger."""

    handle: window_stream_bank.Handle
    epoch: int
    spec: ReservationSpec
    breakdown: dict[str, int]
    mapping_id: int
    ring_owner: object
    active_readers: set = field(default_factory=set)
    outstanding_leases: set = field(default_factory=set)
    recording_leases_by_seq: dict = field(default_factory=dict)
    unjoined_channels: set = field(default_factory=set)
    closing: bool = False
    releasing: bool = False
    settled: bool = False
    unmap_proven: bool = False
    # Credit and frame sequencing state
    hello_received: bool = False
    initial_ack_sent: bool = False
    last_ack_seq: int = 0
    cumulative_credit_total: int = 0
    last_grabbed_seq: int = 0
    grab_in_progress_seq: int | None = None
    ungrab_checked_seq: int = 0
    validated_published_seq: int = 0
    end_received: bool = False
    end_info: dict | None = None

    @property
    def total_ownership_bytes(self) -> int:
        return 0 if self.settled else self.breakdown["total_ownership_bytes"]

    def current_Breakdown(self) -> dict[str, int]:
        if self.settled:
            return {k: 0 for k in RESERVATION_CATEGORIES}
        return {k: self.breakdown[k] for k in RESERVATION_CATEGORIES}


