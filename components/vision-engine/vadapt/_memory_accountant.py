"""w8u_memory: memory accountant."""
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

class HostAdmissionAccountant:
    """Production W8U host admission accountant, credit controller, and purge settler."""

    def __init__(
        self,
        *,
        quota_bytes: int,
        telemetry_source,
        ring_factory=None,
        workspace=None,
    ):
        self.quota_bytes = _exact_int(quota_bytes, "quota_bytes")
        if not callable(telemetry_source):
            raise W8UMemoryError("E_RAM_QUOTA", "telemetry_source injector is required and must be callable")
        if ring_factory is not None and not callable(ring_factory):
            raise W8UMemoryError("E_RAM_QUOTA", "ring_factory must be callable")
        self._telemetry_source = telemetry_source
        self._ring_factory = ring_factory or (
            lambda handle, epoch, size: FakeRingBuffer(size)
        )
        self._workspace = workspace
        self._lock = threading.RLock()
        self._active: dict[window_stream_bank.Handle, CapsuleClaimEntry] = {}
        self._seen_handles: set[window_stream_bank.Handle] = set()
        self._pending_handles: set[window_stream_bank.Handle] = set()
        self._retained: list[CapsuleClaimEntry] = []
        self._mappings: dict[int, PhysicalMappingEntry] = {}
        self._ownership_reserved_bytes: int = 0
        self._peak_ownership_bytes: int = 0
        self._baseline_swap_bytes: int | None = None
        self._last_telemetry_report: dict | None = None
        self._settlement_count: int = 0

    @property
    def ownership_reserved_bytes(self) -> int:
        with self._lock:
            return self._ownership_reserved_bytes

    @property
    def physical_shared_bytes(self) -> int:
        with self._lock:
            return sum(m.size_bytes for m in self._mappings.values() if not m.unmapped)

    @property
    def physical_total_bytes(self) -> int:
        """Total physical RAM footprint without double-counting shared mappings or zero-copy ring views."""
        with self._lock:
            shared = sum(m.size_bytes for m in self._mappings.values() if not m.unmapped)
            private_heap = 0
            for claim in list(self._active.values()) + self._retained:
                if claim.settled:
                    continue
                private_heap += (
                    claim.breakdown["pending_raw_pair"]
                    + claim.breakdown["canonical_decoding_copies"]
                    + claim.breakdown["control_buffers"]
                )
                if not claim.spec.views_share_ring_mapping:
                    private_heap += claim.breakdown["held_consumer_views"]
            return shared + private_heap

    def category_totals(self) -> dict[str, int]:
        """Return current live + retained ownership charges by reservation category."""
        with self._lock:
            totals = {k: 0 for k in RESERVATION_CATEGORIES}
            for claim in list(self._active.values()) + self._retained:
                if claim.settled:
                    continue
                for k in RESERVATION_CATEGORIES:
                    totals[k] += claim.breakdown[k]
            return totals

    def _sample_and_verify_headroom(self, required_new_bytes: int) -> dict:
        obs = self._telemetry_source()
        if self._baseline_swap_bytes is None and isinstance(obs, TelemetryObservation) and type(obs.swap_used_bytes) is int:
            self._baseline_swap_bytes = obs.swap_used_bytes
        report = validate_telemetry_for_admission(obs, baseline_swap_bytes=self._baseline_swap_bytes)
        self._last_telemetry_report = report

        new_total = _checked_sum(
            [self._ownership_reserved_bytes, required_new_bytes],
            "new_ownership_reserved_bytes",
        )
        if new_total > self.quota_bytes:
            raise W8UMemoryError(
                "E_RAM_QUOTA",
                "admission (%d + %d = %d) exceeds accountant quota (%d)"
                % (self._ownership_reserved_bytes, required_new_bytes, new_total, self.quota_bytes),
            )
        if required_new_bytes > report["tighter_headroom_bytes"]:
            raise W8UMemoryError(
                "E_RAM_QUOTA",
                "admission (%d) exceeds tighter proven %s headroom (%d)"
                % (required_new_bytes, report["governing_limit"], report["tighter_headroom_bytes"]),
            )
        return report

    def admit(self, handle, spec: ReservationSpec, *, channel_ids=("ctrl",)) -> window_stream_bank.StreamToken:
        return self.admit_many(((handle, spec, channel_ids),))[0]

    def admit_many(self, requests) -> tuple[window_stream_bank.StreamToken, ...]:
        if not requests:
            return ()
        normalized = []
        for item in requests:
            if len(item) == 2:
                h, spec = item
                ch_ids = ("ctrl",)
            else:
                h, spec, ch_ids = item
            try:
                norm_h = window_stream_bank.Handle(h.kind, h.canonical_id, h.generation)
            except (AttributeError, window_stream_bank.StreamError) as exc:
                raise W8UMemoryError("E_BINDING", "invalid handle: %s" % exc) from exc
            if not isinstance(spec, ReservationSpec):
                raise W8UMemoryError("E_RAM_QUOTA", "spec must be ReservationSpec")
            breakdown = spec.compute_breakdown()
            normalized.append((norm_h, spec, breakdown, tuple(ch_ids)))

        try:
            window_stream_bank.check_lock_order([h for h, _, _, _ in normalized])
        except window_stream_bank.StreamError as exc:
            raise W8UMemoryError("E_BINDING", str(exc)) from exc

        total_new = _checked_sum([b["total_ownership_bytes"] for _, _, b, _ in normalized], "batch_admission")

        with self._lock:
            for h, _, _, _ in normalized:
                if h in self._pending_handles:
                    raise W8UMemoryError("E_BINDING", "admission already pending for %r" % (h,))
                if h in self._active or h in self._seen_handles:
                    raise W8UMemoryError("E_BINDING", "handle generation already opened or retired: %r" % (h,))
            self._sample_and_verify_headroom(total_new)
            if self._workspace is not None:
                self._workspace.reserve(total_new)
            self._ownership_reserved_bytes += total_new
            self._peak_ownership_bytes = max(self._peak_ownership_bytes, self._ownership_reserved_bytes)
            self._pending_handles.update(h for h, _, _, _ in normalized)

        return self._stage_claims(normalized, epoch_map={h: 0 for h, _, _, _ in normalized}, old_claims=None)

    def resize(self, token: window_stream_bank.StreamToken, spec: ReservationSpec, *, channel_ids=("ctrl",)) -> window_stream_bank.StreamToken:
        return self.resize_many(((token, spec, channel_ids),))[0]

    def resize_many(self, requests) -> tuple[window_stream_bank.StreamToken, ...]:
        if not requests:
            return ()
        normalized = []
        for item in requests:
            if len(item) == 2:
                tok, spec = item
                ch_ids = ("ctrl",)
            else:
                tok, spec, ch_ids = item
            try:
                norm_h = window_stream_bank.Handle(tok.handle.kind, tok.handle.canonical_id, tok.handle.generation)
                tok_epoch = _exact_int(tok.epoch, "epoch", allow_zero=True, error_code="E_BINDING")
            except (AttributeError, window_stream_bank.StreamError) as exc:
                raise W8UMemoryError("E_BINDING", "invalid token: %s" % exc) from exc
            if not isinstance(spec, ReservationSpec):
                raise W8UMemoryError("E_RAM_QUOTA", "spec must be ReservationSpec")
            breakdown = spec.compute_breakdown()
            normalized.append((norm_h, tok_epoch, spec, breakdown, tuple(ch_ids)))

        try:
            window_stream_bank.check_lock_order([h for h, _, _, _, _ in normalized])
        except window_stream_bank.StreamError as exc:
            raise W8UMemoryError("E_BINDING", str(exc)) from exc

        total_new = _checked_sum([b["total_ownership_bytes"] for _, _, _, b, _ in normalized], "batch_resize")

        with self._lock:
            old_claims = {}
            epoch_map = {}
            for h, tok_epoch, _, _, _ in normalized:
                if h in self._pending_handles:
                    raise W8UMemoryError("E_BINDING", "resize already pending for %r" % (h,))
                cur = self._active.get(h)
                if cur is None or cur.closing or cur.epoch != tok_epoch:
                    raise W8UMemoryError("E_BINDING", "stale or closed stream token for %r" % (h,))
                old_claims[h] = cur
                epoch_map[h] = cur.epoch + 1

            self._sample_and_verify_headroom(total_new)
            if self._workspace is not None:
                self._workspace.reserve(total_new)
            self._ownership_reserved_bytes += total_new
            self._peak_ownership_bytes = max(self._peak_ownership_bytes, self._ownership_reserved_bytes)
            self._pending_handles.update(h for h, _, _, _, _ in normalized)

        stage_input = [(h, spec, b, ch_ids) for h, _, spec, b, ch_ids in normalized]
        return self._stage_claims(stage_input, epoch_map=epoch_map, old_claims=old_claims)

    def _stage_claims(self, items, *, epoch_map, old_claims):
        staged: list[CapsuleClaimEntry] = []
        try:
            for h, spec, breakdown, ch_ids in items:
                epoch = epoch_map[h]
                ring_size = breakdown["ring_storage"]
                owner = self._ring_factory(h, epoch, ring_size)
                if owner is None:
                    raise W8UMemoryError("E_RAM_QUOTA", "ring_factory returned None")
                raw_buf = getattr(owner, "buffer", getattr(owner, "_mm", owner))
                if raw_buf is None or len(raw_buf) != ring_size:
                    raise W8UMemoryError("E_RAM_QUOTA", "ring_factory returned wrongly sized buffer")
                mapping_id = id(raw_buf)
                with self._lock:
                    if mapping_id in self._mappings and not self._mappings[mapping_id].unmapped:
                        raise W8UMemoryError("E_RAM_QUOTA", "ring_factory reused a live physical mapping")
                    self._mappings[mapping_id] = PhysicalMappingEntry(
                        mapping_id=mapping_id,
                        size_bytes=ring_size,
                        owner=owner,
                        active_claims={(h, epoch)},
                    )
                claim = CapsuleClaimEntry(
                    handle=h,
                    epoch=epoch,
                    spec=spec,
                    breakdown=breakdown,
                    mapping_id=mapping_id,
                    ring_owner=owner,
                    unjoined_channels=set(ch_ids),
                )
                staged.append(claim)
        except BaseException as exc:
            with self._lock:
                self._pending_handles.difference_update(h for h, _, _, _ in items)
                uncreated_bytes = sum(b["total_ownership_bytes"] for _, _, b, _ in items[len(staged) :])
                self._ownership_reserved_bytes -= uncreated_bytes
                if self._workspace is not None and uncreated_bytes > 0:
                    self._workspace.used = max(0, self._workspace.used - uncreated_bytes)
                for claim in staged:
                    claim.closing = True
                    claim.unjoined_channels.clear()
                    self._retained.append(claim)
            for claim in staged:
                try:
                    self._try_settle_claim(claim)
                except Exception:
                    pass
            if isinstance(exc, W8UMemoryError):
                raise
            raise W8UMemoryError("E_RAM_QUOTA", "staging failed: %s" % exc) from exc

        with self._lock:
            for claim in staged:
                if old_claims is not None:
                    old = old_claims[claim.handle]
                    old.closing = True
                    self._retained.append(old)
                else:
                    self._seen_handles.add(claim.handle)
                self._active[claim.handle] = claim
            self._pending_handles.difference_update(h for h, _, _, _ in items)

        if old_claims is not None:
            for old in old_claims.values():
                try:
                    self._try_settle_claim(old)
                except Exception:
                    pass

        return tuple(window_stream_bank.StreamToken(c.handle, c.epoch) for c in staged)

    def _require_claim(self, token: window_stream_bank.StreamToken, *, allow_closing: bool = False) -> CapsuleClaimEntry:
        norm_h = window_stream_bank.Handle(token.handle.kind, token.handle.canonical_id, token.handle.generation)
        cur = self._active.get(norm_h)
        if cur is not None and cur.epoch == token.epoch and (allow_closing or not cur.closing):
            return cur
        if allow_closing:
            for ret in self._retained:
                if ret.handle == norm_h and ret.epoch == token.epoch:
                    return ret
        raise W8UMemoryError("E_BINDING", "stale or inactive stream token %r" % (token,))

    # -- Shared-memory reader, lease, and channel witnesses -------------------

    def attach_reader(self, token: window_stream_bank.StreamToken, reader_id: str) -> int:
        """Attach a shared-memory reader without double-counting physical shared mapping bytes."""
        with self._lock:
            claim = self._require_claim(token)
            claim.active_readers.add(reader_id)
            mapping = self._mappings[claim.mapping_id]
            mapping.attached_readers.add((claim.handle, claim.epoch, reader_id))
            return self.physical_shared_bytes

    def witness_reader_exit(self, token: window_stream_bank.StreamToken, reader_id: str) -> bool:
        """Record positive reader exit witness."""
        with self._lock:
            claim = self._require_claim(token, allow_closing=True)
            claim.active_readers.discard(reader_id)
            mapping = self._mappings.get(claim.mapping_id)
            if mapping is not None:
                mapping.attached_readers.discard((claim.handle, claim.epoch, reader_id))
            should_settle = claim.closing
        if should_settle:
            return self._try_settle_claim(claim)
        return False

    def acquire_consumer_lease(
        self,
        token: window_stream_bank.StreamToken,
        lease_id: str,
        *,
        frame_seq: int | None = None,
    ) -> None:
        """Acquire a consumer or recording view lease."""
        with self._lock:
            claim = self._require_claim(token)
            claim.outstanding_leases.add(lease_id)
            if frame_seq is not None:
                claim.recording_leases_by_seq.setdefault(frame_seq, set()).add(lease_id)
            if claim.spec.views_share_ring_mapping:
                self._mappings[claim.mapping_id].zero_copy_leases.add((claim.handle, claim.epoch, lease_id))

    def release_consumer_lease(
        self,
        token: window_stream_bank.StreamToken,
        lease_id: str,
        *,
        frame_seq: int | None = None,
    ) -> bool:
        """Record positive consumer/recording lease release witness."""
        with self._lock:
            claim = self._require_claim(token, allow_closing=True)
            claim.outstanding_leases.discard(lease_id)
            if frame_seq is not None and frame_seq in claim.recording_leases_by_seq:
                claim.recording_leases_by_seq[frame_seq].discard(lease_id)
                if not claim.recording_leases_by_seq[frame_seq]:
                    del claim.recording_leases_by_seq[frame_seq]
            else:
                for seq_k in list(claim.recording_leases_by_seq.keys()):
                    claim.recording_leases_by_seq[seq_k].discard(lease_id)
                    if not claim.recording_leases_by_seq[seq_k]:
                        del claim.recording_leases_by_seq[seq_k]
            mapping = self._mappings.get(claim.mapping_id)
            if mapping is not None:
                mapping.zero_copy_leases.discard((claim.handle, claim.epoch, lease_id))
            should_settle = claim.closing
        if should_settle:
            return self._try_settle_claim(claim)
        return False

    def cancel_channel(self, token: window_stream_bank.StreamToken, channel_id: str) -> None:
        """Mark channel cancelled but still unjoined: keeps channel/pending/control charges held."""
        with self._lock:
            claim = self._require_claim(token, allow_closing=True)
            claim.unjoined_channels.add(channel_id)

    def witness_channel_joined(
        self,
        token: window_stream_bank.StreamToken,
        channel_id: str,
        *,
        eof_seen: bool = True,
        exit_joined: bool = True,
    ) -> bool:
        """Positive cleanup witness for a cancelled/closed channel after EOF + joined exit."""
        if not (eof_seen and exit_joined):
            return False
        with self._lock:
            claim = self._require_claim(token, allow_closing=True)
            claim.unjoined_channels.discard(channel_id)
            should_settle = claim.closing
        if should_settle:
            return self._try_settle_claim(claim)
        return False

    # -- Credit, Grab/Ungrab, Publication, and ACK State Machine --------------

    def receive_hello(self, token: window_stream_bank.StreamToken) -> int:
        """HELLO grants no credit (returns 0) and requires reserved backing capacity."""
        with self._lock:
            claim = self._require_claim(token)
            if claim.settled or claim.total_ownership_bytes <= 0:
                raise W8UMemoryError("E_CREDIT", "cannot accept HELLO without reserved capacity")
            claim.hello_received = True
            return 0

    def send_initial_ack(self, token: window_stream_bank.StreamToken, *, grant: int | None = None) -> dict:
        """Initial ACK acknowledges seq 0 and grants min(2, max_inflight_frames); overgrant fails E_CREDIT."""
        with self._lock:
            claim = self._require_claim(token)
            if not claim.hello_received:
                raise W8UMemoryError("E_CREDIT", "initial ACK requires prior HELLO after reservation")
            if claim.initial_ack_sent:
                raise W8UMemoryError("E_CREDIT", "initial ACK already sent")
            expected_grant = min(PREFERRED_INITIAL_CREDIT_GRANT, claim.spec.max_inflight_frames)
            actual_grant = expected_grant if grant is None else _exact_int(grant, "grant", allow_zero=False, error_code="E_CREDIT")
            if actual_grant > expected_grant:
                raise W8UMemoryError(
                    "E_CREDIT",
                    "initial overgrant (%d > min(2, %d) = %d)"
                    % (actual_grant, claim.spec.max_inflight_frames, expected_grant),
                )
            claim.initial_ack_sent = True
            claim.last_ack_seq = 0
            claim.cumulative_credit_total = actual_grant
            return {"ack_seq": 0, "cumulative_credit_total": actual_grant}

    def available_credit(self, token: window_stream_bank.StreamToken) -> int:
        with self._lock:
            claim = self._require_claim(token)
            return max(0, claim.cumulative_credit_total - claim.last_grabbed_seq)

    def acquire_credit_and_grab(self, token: window_stream_bank.StreamToken, seq: int) -> None:
        """Acquire credit before grab; fails E_CREDIT on exhaustion or out-of-order seq."""
        with self._lock:
            claim = self._require_claim(token)
            seq_i = _exact_int(seq, "seq", allow_zero=False, error_code="E_SEQ")
            if seq_i != claim.last_grabbed_seq + 1:
                raise W8UMemoryError("E_SEQ", "grab seq %d != expected %d" % (seq_i, claim.last_grabbed_seq + 1))
            if not claim.initial_ack_sent or claim.cumulative_credit_total - claim.last_grabbed_seq <= 0:
                raise W8UMemoryError("E_CREDIT", "credit exhausted before grab for seq %d" % seq_i)
            if claim.grab_in_progress_seq is not None:
                raise W8UMemoryError("E_TORN", "previous grab not ungrabbed")
            claim.last_grabbed_seq = seq_i
            claim.grab_in_progress_seq = seq_i

    def complete_checked_ungrab(self, token: window_stream_bank.StreamToken, seq: int, *, ungrab_ok: bool = True) -> None:
        """Complete checked ungrab before blocking output."""
        with self._lock:
            claim = self._require_claim(token)
            if claim.grab_in_progress_seq != seq:
                raise W8UMemoryError("E_TORN", "no active grab for seq %r" % (seq,))
            if ungrab_ok is not True:
                raise W8UMemoryError("E_TORN", "checked ungrab failed for seq %d" % seq)
            claim.grab_in_progress_seq = None
            claim.ungrab_checked_seq = seq

    def record_validated_publication(self, token: window_stream_bank.StreamToken, seq: int) -> None:
        """Record validated publication after checked ungrab."""
        with self._lock:
            claim = self._require_claim(token)
            seq_i = _exact_int(seq, "seq", allow_zero=False, error_code="E_SEQ")
            if claim.grab_in_progress_seq is not None or claim.ungrab_checked_seq < seq_i:
                raise W8UMemoryError("E_TORN", "cannot publish or block output before checked ungrab completes")
            if seq_i != claim.validated_published_seq + 1:
                raise W8UMemoryError("E_SEQ", "publication seq %d != expected %d" % (seq_i, claim.validated_published_seq + 1))
            claim.validated_published_seq = seq_i

    def apply_ack(self, token: window_stream_bank.StreamToken, ack_seq: int, cumulative_credit_total: int) -> dict:
        """Validate and apply an ACK: idempotent on identical duplicate, refuses inflation or premature ACK."""
        with self._lock:
            claim = self._require_claim(token)
            a_seq = _exact_int(ack_seq, "ack_seq", allow_zero=True, error_code="E_CREDIT")
            c_tot = _exact_int(cumulative_credit_total, "cumulative_credit_total", allow_zero=True, error_code="E_CREDIT")

            if not claim.initial_ack_sent:
                if a_seq != 0:
                    raise W8UMemoryError("E_CREDIT", "first ACK must acknowledge seq 0")
                return self.send_initial_ack(token, grant=c_tot)

            # Identical duplicate ACK is idempotent
            if a_seq == claim.last_ack_seq and c_tot == claim.cumulative_credit_total:
                return {
                    "ack_seq": a_seq,
                    "cumulative_credit_total": c_tot,
                    "duplicate": True,
                    "available_credit": claim.cumulative_credit_total - claim.last_grabbed_seq,
                }

            if a_seq < claim.last_ack_seq or c_tot < claim.cumulative_credit_total:
                raise W8UMemoryError("E_CREDIT", "ACK regressed (%d, %d)" % (a_seq, c_tot))
            if a_seq == claim.last_ack_seq and c_tot != claim.cumulative_credit_total:
                raise W8UMemoryError("E_CREDIT", "conflicting duplicate ACK for seq %d" % a_seq)

            if a_seq > claim.validated_published_seq:
                raise W8UMemoryError("E_CREDIT", "cannot ACK seq %d before validated publication" % a_seq)
            for held_seq, leases in claim.recording_leases_by_seq.items():
                if held_seq <= a_seq and leases:
                    raise W8UMemoryError(
                        "E_CREDIT",
                        "cannot ACK seq %d while recording-consumer lease %r is still held" % (a_seq, sorted(leases)),
                    )

            max_allowed = _checked_sum([a_seq, claim.spec.max_inflight_frames], "ack_seq+capacity", error_code="E_CREDIT")
            if c_tot > max_allowed:
                raise W8UMemoryError(
                    "E_CREDIT",
                    "capacity inflation: cumulative_credit_total %d > ack_seq (%d) + capacity (%d)"
                    % (c_tot, a_seq, claim.spec.max_inflight_frames),
                )

            claim.last_ack_seq = a_seq
            claim.cumulative_credit_total = c_tot
            return {
                "ack_seq": a_seq,
                "cumulative_credit_total": c_tot,
                "duplicate": False,
                "available_credit": claim.cumulative_credit_total - claim.last_grabbed_seq,
            }

    # -- Close, Purge Settlement, and Retry Cleanup ---------------------------

    def close_claim(self, token: window_stream_bank.StreamToken) -> bool:
        """Initiate claim close; keeps charges held until all positive cleanup witnesses succeed."""
        with self._lock:
            claim = self._require_claim(token)
            if claim.handle in self._pending_handles:
                raise W8UMemoryError("E_BINDING", "cannot close during pending resize")
            del self._active[claim.handle]
            claim.closing = True
            self._retained.append(claim)
        return self._try_settle_claim(claim)

    def _try_settle_claim(self, claim: CapsuleClaimEntry) -> bool:
        with self._lock:
            if not claim.closing or claim.settled or claim.releasing:
                return False
            if claim.active_readers or claim.outstanding_leases or claim.unjoined_channels:
                return False
            claim.releasing = True

        unmapped_now = False
        raised_exc = None
        try:
            if claim.unmap_proven:
                unmapped_now = True
            else:
                owner = claim.ring_owner
                close_fn = getattr(owner, "close", None)
                if callable(close_fn):
                    try:
                        res = close_fn()
                        unmapped_now = (res is True) or (getattr(owner, "_released", False) is True)
                    except BufferError:
                        unmapped_now = False
                    except BaseException as exc:
                        # Ring close raising must NOT erase the ledger!
                        unmapped_now = False
                        raised_exc = exc
                else:
                    unmapped_now = False
        finally:
            with self._lock:
                claim.releasing = False
                if unmapped_now and not claim.settled:
                    claim.unmap_proven = True
                    claim.settled = True
                    charge = claim.breakdown["total_ownership_bytes"]
                    self._ownership_reserved_bytes -= charge
                    if self._workspace is not None:
                        self._workspace.used = max(0, self._workspace.used - charge)
                    mapping = self._mappings.get(claim.mapping_id)
                    if mapping is not None:
                        mapping.active_claims.discard((claim.handle, claim.epoch))
                        if not mapping.active_claims and not mapping.attached_readers and not mapping.zero_copy_leases:
                            mapping.unmapped = True
                    if claim in self._retained:
                        self._retained.remove(claim)
                    self._settlement_count += 1

        if raised_exc is not None:
            raise raised_exc
        return unmapped_now

    def retry_cleanup(self) -> int:
        """Retry retained claims; each claim settles at most once after actual release."""
        with self._lock:
            candidates = tuple(self._retained)
        settled_now = 0
        for claim in candidates:
            try:
                if self._try_settle_claim(claim):
                    settled_now += 1
            except Exception:
                pass
        return settled_now


# ---------------------------------------------------------------------------
# Receipt Projection (w8u.receipt/v1) & Input Authority Separation
# ---------------------------------------------------------------------------

