"""w8u_credits: credits controller."""
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
from ._credits_control import (
    DecodedControl, encode_control_frame, decode_control_frame, ConsumerLease, CaptureOutcome,
)

from ._credits_reporting import CreditReportingMixin


class W8UCreditController(CreditReportingMixin):
    """Cumulative credit controller, reverse control, and frame state machine."""

    def __init__(
        self,
        *,
        clock: InjectedClock,
        contract: ContractEvidence | Any,
        bank: window_stream_bank.WindowStreamBank | None = None,
        grant_authenticator: Callable[[dict[str, Any]], bool] | None = None,
        consumer_timeout_ns: int = 100_000_000,
        capture_timeout_ns: int = 100_000_000,
        native_xwd_validator: Callable[[bytes], str | None] | None = None,
    ):
        if not isinstance(clock, InjectedClock):
            raise W8UCreditError("E_TIMEOUT", "clock must be an InjectedClock instance")
        self.clock = clock
        self.contract = validate_contract_evidence(contract)
        self.bank = bank
        self.grant_authenticator = grant_authenticator
        self.consumer_timeout_ns = _require_exact_int(
            consumer_timeout_ns, "consumer_timeout_ns", minimum=1, maximum=U64_MAX, err_code="E_TIMEOUT"
        )
        self.capture_timeout_ns = _require_exact_int(
            capture_timeout_ns, "capture_timeout_ns", minimum=1, maximum=U64_MAX, err_code="E_TIMEOUT"
        )
        self.native_xwd_validator = native_xwd_validator

        self.confirmed_binding: ConfirmedBinding | None = None
        self.hello_received: bool = False
        self.hello_confirmed: bool = False
        self.capacity_reserved: bool = False
        self.stream_token: window_stream_bank.StreamToken | None = None
        self.reserved_ring_bytes: int = 0

        self.sent_seq: int = 0
        self.spent_total: int = 0
        self.highest_acked_seq: int = 0
        self.cumulative_credit_total: int = 0
        self.initial_ack_done: bool = False

        self.outstanding_leases: dict[int, ConsumerLease] = {}
        self.completed_leases: set[int] = set()
        self.admitted_frames: dict[int, ValidatedFrame] = {}
        self.unacked_evidence: set[int] = set()
        self._spent_seqs: set[int] = set()

        self.paused: bool = False
        self.stopped: bool = False
        self.end_sent: bool = False
        self.eof_closed: bool = False
        self.exit_joined: bool = False
        self.under_grab: bool = False
        self.status: str = "idle"
        self.last_error_code: str | None = None
        self.retained_ambiguous_charges: int = 0

        self.retention_mode: str = "full"
        self.purged_pixels: bool = False
        self.actions_admitted: list[dict[str, Any]] = []
        self.actions_applied: list[dict[str, Any]] = []
        self.input_authority: bool = False

    @property
    def max_inflight_frames(self) -> int:
        if self.confirmed_binding is None:
            return 0
        return self.confirmed_binding.max_inflight_frames

    @property
    def available_credits(self) -> int:
        return self.cumulative_credit_total - self.spent_total

    @property
    def inflight_unacked(self) -> int:
        return self.sent_seq - self.highest_acked_seq

    def receive_hello(
        self,
        binding_or_control: dict[str, Any] | bytes,
        *,
        expected_target: window_stream_bank.Handle | Any | None = None,
        expected_epoch: int | None = None,
        expected_geometry: tuple[int, int] | None = None,
    ) -> ConfirmedBinding:
        """Process HELLO and confirm binding. HELLO itself grants 0 credit and cannot start capture."""
        if self.hello_received:
            raise W8UCreditError("E_BINDING", "duplicate HELLO on bound session")
        if isinstance(binding_or_control, (bytes, bytearray, memoryview)):
            raw = bytes(binding_or_control)
            if raw.startswith(CONTROL_MAGIC_HELLO):
                dec = decode_control_frame(raw, contract=self.contract)
                if dec.kind != CONTROL_KIND_HELLO:
                    raise W8UCreditError("E_WIRE", "expected HELLO control kind 1")
                binding_input: dict[str, Any] | bytes = dec.payload["raw_body"]
                adv_sha = dec.binding_sha256
            else:
                binding_input = raw
                adv_sha = None
        else:
            binding_input = binding_or_control
            adv_sha = None

        confirmed = validate_binding(
            binding_input,
            contract=self.contract,
            grant_authenticator=self.grant_authenticator,
            expected_target=expected_target,
            expected_epoch=expected_epoch,
            expected_geometry=expected_geometry,
        )
        if adv_sha is not None and adv_sha != confirmed.binding_sha256:
            raise W8UCreditError("E_BINDING", "HELLO envelope binding_sha256 mismatch")

        self.confirmed_binding = confirmed
        self.hello_received = True
        self.hello_confirmed = True
        # HELLO grants no credit!
        self.cumulative_credit_total = 0
        self.spent_total = 0
        self.sent_seq = 0
        self.highest_acked_seq = 0
        self.status = "hello_confirmed"
        return confirmed

    def encode_hello_confirmation(self) -> bytes:
        """Encode W8UH confirmation frame according to the accepted contract."""
        if not self.hello_confirmed or self.confirmed_binding is None:
            raise W8UCreditError("E_BINDING", "cannot encode W8UH before HELLO confirmation")
        confirm_keys = [k for k in self.contract.control_specs if k[0] == CONTROL_MAGIC_CONFIRM]
        if not confirm_keys:
            raise W8UCreditError("E_BINDING", "W8UH spec is AUSENTE")
        magic, kind = confirm_keys[0]
        spec = self.contract.control_specs[(magic, kind)]
        body = spec.get("expected_body", b"")
        return encode_control_frame(
            magic=magic,
            version=spec["version"],
            kind=kind,
            binding_sha256=self.confirmed_binding.binding_sha256,
            body=body,
            contract=self.contract,
        )

    def reserve_host_capacity(self, *, quota_bytes: int | None = None) -> window_stream_bank.StreamToken:
        """Reserve host ring capacity in WindowStreamBank before initial credit grant."""
        if not self.hello_confirmed or self.confirmed_binding is None:
            raise W8UCreditError("E_CREDIT", "cannot reserve capacity before HELLO confirmation")
        if self.capacity_reserved:
            raise W8UCreditError("E_CREDIT", "host capacity already reserved")

        b = self.confirmed_binding
        needed_bytes = rtring.ring_bytes(b.max_inflight_frames, b.width, b.height)
        spec_quota = needed_bytes if quota_bytes is None else quota_bytes
        try:
            spec = window_stream_bank.StreamSpec(
                width=b.width,
                height=b.height,
                slots=b.max_inflight_frames,
                quota_bytes=spec_quota,
            )
            if self.bank is None:
                self.bank = window_stream_bank.WindowStreamBank(quota_bytes=max(spec_quota, needed_bytes))
            token = self.bank.open(b.target_handle, spec)
        except window_stream_bank.QuotaError as exc:
            self.last_error_code = "E_RAM_QUOTA"
            self.status = "failed"
            raise W8UCreditError("E_RAM_QUOTA", str(exc)) from exc
        except window_stream_bank.StaleStreamError as exc:
            self.last_error_code = "E_BINDING"
            self.status = "failed"
            raise W8UCreditError("E_BINDING", str(exc)) from exc
        except window_stream_bank.StreamError as exc:
            self.last_error_code = "E_GEOMETRY"
            self.status = "failed"
            raise W8UCreditError("E_GEOMETRY", str(exc)) from exc

        self.stream_token = token
        self.reserved_ring_bytes = needed_bytes
        self.capacity_reserved = True
        self.status = "ready"
        return token

    def preferred_initial_grant(self) -> int:
        """Return min(2, negotiated max_inflight_frames)."""
        if not self.hello_confirmed or self.confirmed_binding is None:
            raise W8UCreditError("E_CREDIT", "binding not confirmed")
        return min(PREFERRED_INITIAL_GRANT, self.confirmed_binding.max_inflight_frames)

    def issue_initial_ack(self) -> tuple[int, int]:
        """Apply and return the initial ACK (ack_seq=0, min(2, max_inflight_frames))."""
        grant = self.preferred_initial_grant()
        self.apply_ack(0, grant)
        return (0, grant)

    def apply_ack(self, ack_seq: int, cumulative_credit_total: int) -> int:
        """Validate and apply an ACK(ack_seq, cumulative_credit_total); return available credits."""
        if not self.hello_confirmed or self.confirmed_binding is None:
            raise W8UCreditError("E_CREDIT", "ACK received before HELLO confirmation")
        if not self.capacity_reserved:
            raise W8UCreditError("E_CREDIT", "host capacity must be reserved before initial grant")

        _require_exact_int(ack_seq, "ack_seq", minimum=0, maximum=U64_MAX, err_code="E_CREDIT")
        _require_exact_int(
            cumulative_credit_total,
            "cumulative_credit_total",
            minimum=0,
            maximum=U64_MAX,
            err_code="E_CREDIT",
        )

        capacity = self.confirmed_binding.max_inflight_frames

        if not self.initial_ack_done:
            if ack_seq != 0:
                raise W8UCreditError("E_CREDIT", "initial ACK must acknowledge seq 0")
            max_initial = min(PREFERRED_INITIAL_GRANT, capacity)
            if cumulative_credit_total < 1 or cumulative_credit_total > max_initial:
                raise W8UCreditError(
                    "E_CREDIT",
                    f"initial grant {cumulative_credit_total} outside [1, {max_initial}]",
                )
            self.highest_acked_seq = 0
            self.cumulative_credit_total = cumulative_credit_total
            self.initial_ack_done = True
            if self.paused and self.available_credits > 0 and not self.stopped:
                self.paused = False
                self.status = "ready"
            return self.available_credits

        # Identical duplicate ACK is idempotent and does not increase capture budget
        if ack_seq == self.highest_acked_seq and cumulative_credit_total == self.cumulative_credit_total:
            return self.available_credits

        # Conflicting duplicate ACK for the same ack_seq raises E_CREDIT
        if ack_seq == self.highest_acked_seq and cumulative_credit_total != self.cumulative_credit_total:
            raise W8UCreditError("E_CREDIT", "conflicting duplicate ACK for already acknowledged seq")

        # Regression in ack_seq or cumulative_credit_total raises E_CREDIT
        if ack_seq < self.highest_acked_seq or cumulative_credit_total < self.cumulative_credit_total:
            raise W8UCreditError("E_CREDIT", "ACK regression in ack_seq or cumulative_credit_total")

        # Unsent acknowledgment raises E_CREDIT
        if ack_seq > self.sent_seq:
            raise W8UCreditError("E_CREDIT", f"unsent acknowledgment ack_seq={ack_seq} > sent_seq={self.sent_seq}")

        # ACK eligibility requires admission plus explicit recording-consumer completion
        for s in range(self.highest_acked_seq + 1, ack_seq + 1):
            if s not in self.admitted_frames:
                raise W8UCreditError("E_CREDIT", f"frame seq {s} not admitted")
            if s in self.outstanding_leases or s not in self.completed_leases:
                raise W8UCreditError(
                    "E_CREDIT",
                    f"frame seq {s} is not ACK-eligible before recording-consumer lease completion",
                )

        # Overflow and capacity inflation check: cumulative_credit_total <= ack_seq + capacity
        if ack_seq + capacity > U64_MAX:
            raise W8UCreditError("E_CREDIT", "ack_seq + capacity overflows u64")
        if cumulative_credit_total > ack_seq + capacity:
            raise W8UCreditError(
                "E_CREDIT",
                f"capacity inflation: cumulative_credit_total {cumulative_credit_total} > ack_seq {ack_seq} + capacity {capacity}",
            )
        if cumulative_credit_total < self.spent_total:
            raise W8UCreditError("E_CREDIT", "cumulative_credit_total below spent_total")

        # Commit state update atomically after all predicates pass
        for s in range(self.highest_acked_seq + 1, ack_seq + 1):
            self.unacked_evidence.discard(s)
        self.highest_acked_seq = ack_seq
        self.cumulative_credit_total = cumulative_credit_total
        if self.paused and self.available_credits > 0 and not self.stopped:
            self.paused = False
            self.status = "ready"
        return self.available_credits

    def apply_ack_wire(self, wire_bytes: bytes) -> int:
        """Decode a W8UC ACK wire frame and apply it."""
        if self.confirmed_binding is None:
            raise W8UCreditError("E_CREDIT", "ACK wire received before HELLO confirmation")
        dec = decode_control_frame(
            wire_bytes,
            contract=self.contract,
            expected_binding_sha256=self.confirmed_binding.binding_sha256,
        )
        if dec.magic != CONTROL_MAGIC_HELLO or dec.kind != CONTROL_KIND_ACK:
            raise W8UCreditError("E_WIRE", "expected W8UC ACK control kind 2")
        return self.apply_ack(dec.payload["ack_seq"], dec.payload["cumulative_credit_total"])

    def encode_ack_wire(self, ack_seq: int, cumulative_credit_total: int) -> bytes:
        """Encode a W8UC ACK control frame."""
        if self.confirmed_binding is None:
            raise W8UCreditError("E_BINDING", "binding not confirmed")
        _require_exact_int(ack_seq, "ack_seq", minimum=0, maximum=U64_MAX, err_code="E_CREDIT")
        _require_exact_int(
            cumulative_credit_total,
            "cumulative_credit_total",
            minimum=0,
            maximum=U64_MAX,
            err_code="E_CREDIT",
        )
        spec = self.contract.control_specs[(CONTROL_MAGIC_HELLO, CONTROL_KIND_ACK)]
        body = ACK_BODY_STRUCT.pack(ack_seq, cumulative_credit_total)
        return encode_control_frame(
            magic=CONTROL_MAGIC_HELLO,
            version=spec["version"],
            kind=CONTROL_KIND_ACK,
            binding_sha256=self.confirmed_binding.binding_sha256,
            body=body,
            contract=self.contract,
        )

    def spend_for_seq(self, seq: int) -> None:
        """Explicitly spend 1 credit for sequence number seq; refuses duplicate spend or zero credit."""
        _require_exact_int(seq, "seq", minimum=1, maximum=U64_MAX, err_code="E_SEQ")
        if seq in self._spent_seqs:
            raise W8UCreditError("E_CREDIT", f"duplicate credit spend for seq {seq}")
        if seq != self.sent_seq + 1:
            raise W8UCreditError("E_SEQ", f"non-contiguous spend seq {seq}, expected {self.sent_seq + 1}")
        if self.available_credits <= 0:
            raise W8UCreditError("E_CREDIT", "cannot spend credit when available_credits == 0")
        if self.spent_total == U64_MAX:
            raise W8UCreditError("E_CREDIT", "spent_total u64 overflow")
        self._spent_seqs.add(seq)
        self.spent_total += 1

    def check_deadlines(self, *, capture_deadline_mono_ns: int | None = None) -> None:
        """Check recording-consumer lease deadlines (E_BACKPRESSURE) and capture/channel deadlines (E_TIMEOUT)."""
        now = self.clock.now_ns()
        for seq, lease in sorted(self.outstanding_leases.items()):
            if now > lease.deadline_mono_ns:
                self.status = "incomplete"
                self.last_error_code = "E_BACKPRESSURE"
                raise W8UCreditError(
                    "E_BACKPRESSURE",
                    f"consumer lease expired for seq {seq} at {now} > {lease.deadline_mono_ns}",
                    status="incomplete",
                    detail={"seq": seq, "now_ns": now, "deadline_ns": lease.deadline_mono_ns},
                )
        if capture_deadline_mono_ns is not None and now > capture_deadline_mono_ns:
            self._handle_timeout(f"capture/channel deadline expired at {now} > {capture_deadline_mono_ns}")

    def _handle_timeout(self, reason: str) -> None:
        self.status = "incomplete"
        self.last_error_code = "E_TIMEOUT"
        if self.bank is not None and self.stream_token is not None:
            try:
                self.bank.close(self.stream_token)
            except window_stream_bank.StreamError:
                pass
            self.retained_ambiguous_charges = self.bank.reserved_bytes
        raise W8UCreditError(
            "E_TIMEOUT",
            reason,
            status="incomplete",
            retained_charges=self.retained_ambiguous_charges,
        )

    def capture_frame(
        self,
        *,
        grab_fn: Callable[[], None],
        alloc_pair_fn: Callable[[int], bytes],
        ungrab_fn: Callable[[], bool],
        output_fn: Callable[[bytes], None] | None = None,
        capture_duration_ns: int = 0,
        output_duration_ns: int = 0,
    ) -> CaptureOutcome:
        """Execute one deterministic capture step on the frame state machine.

        - If HELLO is not confirmed or initial ACK has not granted credit, or available_credits == 0:
          pauses BEFORE calling grab_fn or alloc_pair_fn.
        - Never overwrites required unacknowledged/leased evidence in the stream bank.
        - Acquires credit before grab_fn, completes checked ungrab_fn before blocking output_fn,
          validates wire frame, publishes to WindowStreamBank, and opens a recording-consumer lease.
        """
        if not self.hello_confirmed or self.confirmed_binding is None:
            raise W8UCreditError("E_CREDIT", "HELLO not confirmed; cannot start capture")
        if not self.capacity_reserved or not self.initial_ack_done:
            self.paused = True
            self.status = "paused"
            return CaptureOutcome(paused=True, seq=None)
        if self.stopped:
            return CaptureOutcome(paused=True, seq=None)

        self.check_deadlines()

        # Zero credits pauses BEFORE grabbing or allocating another pending pair
        if self.available_credits <= 0:
            self.paused = True
            self.status = "paused"
            return CaptureOutcome(paused=True, seq=None)

        # Never overwrite required evidence to create capacity
        capacity = self.confirmed_binding.max_inflight_frames
        if len(self.unacked_evidence) >= capacity:
            raise W8UCreditError(
                "E_CREDIT",
                "required unacknowledged evidence occupies all capacity slots; never overwrite evidence",
            )
        if self.sent_seq == U64_MAX:
            raise W8UCreditError("E_SEQ", "seq u64 rollover is forbidden")
        next_seq = self.sent_seq + 1
        slot_occupant = next_seq - capacity
        if slot_occupant >= 1 and (
            slot_occupant in self.unacked_evidence or slot_occupant in self.outstanding_leases
        ):
            raise W8UCreditError(
                "E_CREDIT",
                f"slot for seq {next_seq} still holds required evidence for seq {slot_occupant}",
            )

        # Acquire credit before grab
        self.spend_for_seq(next_seq)
        self.paused = False
        self.status = "capturing"
        deadline_ns = self.clock.now_ns() + self.capture_timeout_ns

        self.under_grab = True
        ungrabbed_ok = False
        try:
            grab_fn()
            if capture_duration_ns:
                self.clock.advance_ns(capture_duration_ns)
            self.check_deadlines(capture_deadline_mono_ns=deadline_ns)
            wire_frame = alloc_pair_fn(next_seq)
            ungrabbed_ok = bool(ungrab_fn())
            self.under_grab = False
        except W8UCreditError:
            self.under_grab = False
            raise
        except Exception as exc:
            self.under_grab = False
            raise W8UCreditError("E_WIRE", f"capture grab/alloc failure: {exc}") from exc

        if not ungrabbed_ok:
            self._handle_timeout("checked ungrab failed before blocking output")

        # Complete checked ungrab BEFORE blocking output
        if output_fn is not None:
            if self.under_grab:
                raise W8UCreditError("E_WIRE", "blocking output forbidden while under grab")
            if output_duration_ns:
                self.clock.advance_ns(output_duration_ns)
            self.check_deadlines(capture_deadline_mono_ns=deadline_ns)
            output_fn(wire_frame)

        validated = validate_wire_frame(
            wire_frame,
            binding=self.confirmed_binding,
            expected_seq=next_seq,
            contract=self.contract,
            native_xwd_validator=self.native_xwd_validator,
        )

        # Publish to WindowStreamBank using existing numpy seam in window_stream_bank
        if self.bank is not None and self.stream_token is not None:
            np_mod = window_stream_bank.np
            rgb_arr = np_mod.frombuffer(validated.rgb24_bytes, dtype=np_mod.uint8).reshape(
                validated.height, validated.width, 3
            )
            try:
                self.bank.publish(self.stream_token, rgb_arr, frame_seq=next_seq - 1)
            except window_stream_bank.StaleStreamError as exc:
                raise W8UCreditError("E_BINDING", str(exc)) from exc
            except window_stream_bank.StreamError as exc:
                raise W8UCreditError("E_GEOMETRY", str(exc)) from exc

        now_ns = self.clock.now_ns()
        lease = ConsumerLease(
            seq=next_seq,
            acquired_mono_ns=now_ns,
            deadline_mono_ns=now_ns + self.consumer_timeout_ns,
        )
        self.sent_seq = next_seq
        self.admitted_frames[next_seq] = validated
        self.outstanding_leases[next_seq] = lease
        self.unacked_evidence.add(next_seq)
        self.status = "ready"
        return CaptureOutcome(paused=False, seq=next_seq, frame=validated, lease=lease)

    def complete_consumer_lease(self, seq: int) -> ConsumerLease:
        """Complete and release the recording-consumer lease for frame seq."""
        _require_exact_int(seq, "seq", minimum=1, maximum=U64_MAX, err_code="E_SEQ")
        lease = self.outstanding_leases.get(seq)
        if lease is None:
            raise W8UCreditError("E_CREDIT", f"no active recording-consumer lease for seq {seq}")
        now = self.clock.now_ns()
        if now > lease.deadline_mono_ns:
            self.status = "incomplete"
            self.last_error_code = "E_BACKPRESSURE"
            raise W8UCreditError(
                "E_BACKPRESSURE",
                f"consumer lease deadline expired for seq {seq} ({now} > {lease.deadline_mono_ns})",
                status="incomplete",
            )
        del self.outstanding_leases[seq]
        self.completed_leases.add(seq)
        return lease

    def trigger_stop(self, wire_bytes: bytes | None = None) -> bytes:
        """Process or issue STOP (kind 3, empty body) independently of the frame queue."""
        if self.confirmed_binding is None:
            raise W8UCreditError("E_BINDING", "STOP before confirmed binding")
        if wire_bytes is not None:
            dec = decode_control_frame(
                wire_bytes,
                contract=self.contract,
                expected_binding_sha256=self.confirmed_binding.binding_sha256,
            )
            if dec.magic != CONTROL_MAGIC_HELLO or dec.kind != CONTROL_KIND_STOP:
                raise W8UCreditError("E_WIRE", "expected W8UC STOP control kind 3")
        self.stopped = True
        self.paused = False
        if self.status not in ("incomplete", "failed"):
            self.status = "stopped"
        spec = self.contract.control_specs[(CONTROL_MAGIC_HELLO, CONTROL_KIND_STOP)]
        return encode_control_frame(
            magic=CONTROL_MAGIC_HELLO,
            version=spec["version"],
            kind=CONTROL_KIND_STOP,
            binding_sha256=self.confirmed_binding.binding_sha256,
            body=b"",
            contract=self.contract,
        )

    def emit_end(self, error_name: str = "OK") -> tuple[bytes, dict[str, Any]]:
        """Encode W8UE END(last_seq, sent, error_code) followed by EOF and joined exit."""
        if self.confirmed_binding is None:
            raise W8UCreditError("E_BINDING", "cannot emit W8UE END before binding confirmation")
        if error_name not in self.contract.end_error_codes:
            raise W8UCreditError("E_WIRE", f"unknown END error name {error_name!r}")
        end_keys = [k for k in self.contract.control_specs if k[0] == CONTROL_MAGIC_END]
        if not end_keys:
            raise W8UCreditError("E_BINDING", "W8UE spec is AUSENTE")
        magic, kind = end_keys[0]
        spec = self.contract.control_specs[(magic, kind)]
        err_code = self.contract.end_error_codes[error_name]
        last_seq = self.highest_acked_seq
        sent = self.sent_seq
        body = END_BODY_STRUCT.pack(last_seq, sent, err_code)
        wire = encode_control_frame(
            magic=magic,
            version=spec["version"],
            kind=kind,
            binding_sha256=self.confirmed_binding.binding_sha256,
            body=body,
            contract=self.contract,
        )
        self.end_sent = True
        self.eof_closed = True
        self.exit_joined = True
        if error_name == "OK" and self.status not in ("incomplete", "failed"):
            self.status = "completed"
        else:
            self.last_error_code = error_name if error_name != "OK" else self.last_error_code
        return wire, {
            "last_seq": last_seq,
            "sent": sent,
            "error_code": err_code,
            "error_name": error_name,
            "eof": self.eof_closed,
            "joined_exit": self.exit_joined,
        }

    def purge_pixel_retention(self) -> None:
        """Purge retained RGB24 pixel buffers, switching retention mode to metadata_only."""
        self.purged_pixels = True
        self.retention_mode = "metadata_only"
        for seq, frm in list(self.admitted_frames.items()):
            self.admitted_frames[seq] = ValidatedFrame(
                seq=frm.seq,
                tick=frm.tick,
                wire_tick=frm.wire_tick,
                tick_valid=frm.tick_valid,
                capture_mono_ns=frm.capture_mono_ns,
                width=frm.width,
                height=frm.height,
                stride=frm.stride,
                pixel_format=frm.pixel_format,
                sha256=frm.sha256,
                binding_sha256=frm.binding_sha256,
                rgb24_bytes=b"",
                record_bytes=frm.record_bytes,
            )

    def attempt_input_action(self, action_payload: Any) -> None:
        """Capture controller grants no input authority; closed action variants belong to input owner."""
        raise W8UCreditError(
            "E_BINDING",
            "capture controller grants no input authority; closed action variants belong to the input owner",
        )

    def record_foreign_action(
        self,
        action_id: str,
        *,
        admitted: bool,
        applied: bool,
        ui_bytes: bytes | None = None,
        sim_bytes: bytes | None = None,
    ) -> None:
        """Record foreign action admission vs application with separate UI and simulation hash domains."""
        _require_nonempty_str(action_id, "action_id")
        if type(admitted) is not bool or type(applied) is not bool:
            raise W8UCreditError("E_BINDING", "admitted and applied must be bool")
        if applied and not admitted:
            raise W8UCreditError("E_BINDING", "applied action must first be admitted")
        entry = {
            "action_id": action_id,
            "ui_sha256": compute_ui_sha256(ui_bytes, self.contract) if ui_bytes is not None else None,
            "sim_sha256": compute_sim_sha256(sim_bytes, self.contract) if sim_bytes is not None else None,
        }
        if admitted:
            self.actions_admitted.append(dict(entry))
        if applied:
            self.actions_applied.append(dict(entry))


