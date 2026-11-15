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

class CreditReportingMixin:
    """Receipt projection and deterministic schedule traces for the credit controller."""

    def project_receipt(
        self,
        *,
        claim_pixel_replay: bool = False,
        foreign_t2: dict[str, Any] | None = None,
        foreign_state: dict[str, Any] | None = None,
        listener_evidence: dict[str, Any] | None = None,
        cleanup_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project closed w8u.receipt/v1 receipt using the accepted contract's closed fields."""
        if self.confirmed_binding is None:
            raise W8UCreditError("E_BINDING", "cannot project receipt without confirmed binding")
        if self.retention_mode == "metadata_only" and claim_pixel_replay:
            raise W8UCreditError(
                "E_BINDING",
                "metadata_only retention cannot claim pixel replay after purge",
            )

        b = self.confirmed_binding
        ordered_seqs = sorted(self.admitted_frames.keys())
        first_frame = self.admitted_frames[ordered_seqs[0]].metadata_dict() if ordered_seqs else None
        last_frame = self.admitted_frames[ordered_seqs[-1]].metadata_dict() if ordered_seqs else None

        verdict_status = "PASS" if (self.status == "completed" and self.last_error_code is None) else "FAIL"
        verdict_code = "OK" if verdict_status == "PASS" else (self.last_error_code or "E_TIMEOUT")

        receipt = {
            "schema": RECEIPT_SCHEMA,
            "contract_revision": self.contract.revision,
            "identity": {
                "run_id": b.run_id,
                "lease_id": b.lease_id,
                "container_id": b.container_id,
                "binding_sha256": b.binding_sha256_hex,
                "target_handle": {
                    "kind": b.target_handle.kind,
                    "canonical_id": b.target_handle.canonical_id,
                    "generation": b.target_handle.generation,
                },
                "epoch": b.epoch,
            },
            "populations": {
                "sent_frames": self.sent_seq,
                "admitted_frames": len(self.admitted_frames),
                "acked_frames": self.highest_acked_seq,
                "spent_credits": self.spent_total,
                "cumulative_credit_total": self.cumulative_credit_total,
                "outstanding_consumer_leases": len(self.outstanding_leases),
                "actions_admitted": len(self.actions_admitted),
                "actions_applied": len(self.actions_applied),
            },
            "first_frame": first_frame,
            "last_frame": last_frame,
            "foreign": {
                "action_result_schema": self.contract.action_result_schema_id,
                "admitted": list(self.actions_admitted),
                "applied": list(self.actions_applied),
                "t2": foreign_t2 if foreign_t2 is not None else None,
                "state": foreign_state if foreign_state is not None else None,
            },
            "verdict": {
                "status": verdict_status,
                "code": verdict_code,
                "controller_status": self.status,
            },
            "evidence": {
                "retention_mode": self.retention_mode,
                "pixel_replay": bool(claim_pixel_replay and self.retention_mode != "metadata_only"),
                "reserved_bytes": self.bank.reserved_bytes if self.bank is not None else None,
                "retained_ambiguous_charges": self.retained_ambiguous_charges,
                "listener": listener_evidence if listener_evidence is not None else None,
                "exit": {
                    "end_sent": self.end_sent,
                    "eof": self.eof_closed,
                    "joined_exit": self.exit_joined,
                } if self.end_sent else None,
                "cleanup": cleanup_evidence if cleanup_evidence is not None else None,
            },
        }
        return receipt


    def run_schedule(
        self,
        steps: list[dict[str, Any]],
        *,
        grab_fn: Callable[[], None],
        alloc_pair_fn: Callable[[int], bytes],
        ungrab_fn: Callable[[], bool],
        output_fn: Callable[[bytes], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a deterministic schedule of pause, consume, complete, duplicate ACK, cancellation, and release."""
        trace: list[dict[str, Any]] = []
        for step in steps:
            op = step["op"]
            if op == "advance_clock":
                now = self.clock.advance_ns(step["delta_ns"])
                trace.append({"op": op, "now_ns": now})
            elif op == "initial_ack":
                ack_seq, total = self.issue_initial_ack()
                trace.append({
                    "op": op,
                    "ack_seq": ack_seq,
                    "cumulative_credit_total": total,
                    "available_credits": self.available_credits,
                })
            elif op == "capture":
                outcome = self.capture_frame(
                    grab_fn=grab_fn,
                    alloc_pair_fn=alloc_pair_fn,
                    ungrab_fn=ungrab_fn,
                    output_fn=output_fn,
                    capture_duration_ns=step.get("capture_duration_ns", 0),
                    output_duration_ns=step.get("output_duration_ns", 0),
                )
                trace.append({
                    "op": op,
                    "paused": outcome.paused,
                    "seq": outcome.seq,
                    "available_credits": self.available_credits,
                    "status": self.status,
                })
            elif op == "complete":
                lease = self.complete_consumer_lease(step["seq"])
                trace.append({
                    "op": op,
                    "seq": lease.seq,
                    "outstanding_leases": len(self.outstanding_leases),
                })
            elif op in ("ack", "duplicate_ack"):
                avail = self.apply_ack(step["ack_seq"], step["cumulative_credit_total"])
                trace.append({
                    "op": op,
                    "ack_seq": step["ack_seq"],
                    "cumulative_credit_total": step["cumulative_credit_total"],
                    "available_credits": avail,
                    "highest_acked_seq": self.highest_acked_seq,
                    "paused": self.paused,
                })
            elif op in ("stop", "cancel"):
                self.trigger_stop()
                trace.append({"op": op, "stopped": self.stopped, "paused": self.paused, "status": self.status})
            elif op == "release":
                freed = self.bank.retry_cleanup() if self.bank is not None else 0
                if self.bank is not None:
                    self.retained_ambiguous_charges = self.bank.reserved_bytes
                trace.append({
                    "op": op,
                    "freed_count": freed,
                    "reserved_bytes": self.bank.reserved_bytes if self.bank is not None else 0,
                })
            elif op == "check_deadlines":
                self.check_deadlines(capture_deadline_mono_ns=step.get("capture_deadline_mono_ns"))
                trace.append({"op": op, "status": self.status})
            else:
                raise W8UCreditError("E_WIRE", f"unknown schedule step op {op!r}")
        return trace


