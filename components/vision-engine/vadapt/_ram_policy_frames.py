"""proto1_ram_policy: ram policy adapter."""
from __future__ import annotations
import builtins
import copy
from dataclasses import dataclass, fields
import hashlib
import io
import json
import os
import pathlib
from pathlib import Path
import sys
import threading
import zlib
import numpy as np
from vadapt import ramws, rtring, w8u_memory, window_stream_bank

from ._ram_policy_contract import (
    AUSENTE, PREPARED, ACCEPT, CLAIM_LABELS, CONFINED_ROOT, CONFINED_TOOL, CONFINED_CAMPAIGN,
    ACCEPTED_RAM_FSTYPES, CLOSED_ERROR_CODES, FRAME_IDENTITY_FIELDS, CLOSED_SEAT_RECORD_KEYS,
    FORBIDDEN_PIXEL_KEYS, AUTHORITATIVE_RECEIPT_EXTENSION_VALIDATOR, CONDUCTOR_LANE_GATES,
    PUBLIC_SIGNATURES, _HEX64, RamPolicyError, _require_exact_int, _require_hex64,
    _normalize_handle, translate_storage_error, FrameIdentity, BoundFrameReference,
    MeasurementRecord, AuthenticatedRunRoot, CleanupAuthorityWitness, make_immutable_rgb24,
    preflight_workspace_relpath, validate_ram_root_residency,
    _looks_like_reconstructible_pixels, validate_metadata_no_pixels,
    validate_closed_seat_record, join_bound_references,
)
from ._ram_policy_audit import (
    _is_write_mode, _is_write_flags, _is_frame_like_path, _is_allowed_ramws_control_path,
    PersistenceWriteSpy, install_irreversible_persistence_audit_hook,
    run_in_isolated_audit_process,
)
from ._ram_policy_storage import (
    BorrowedFrameView, _ManagedRingStorage, _StreamLedgerState, _default_vm_statvfs_wrapper,
)

class RamPolicyFramesMixin:
    """Internal cohesive method group for the RAM policy adapter."""

    def admit_frame(
        self,
        root_auth: AuthenticatedRunRoot,
        token: window_stream_bank.StreamToken,
        rgb: np.ndarray,
        *,
        frame_seq: int,
        grant_credit_on_admit: int = 1,
        channel: w8u_memory.FakeChannelPipe | None = None,
    ) -> dict[str, object]:
        """Admit an immutable owned RGB24 frame into the real WindowStreamBank."""
        with self._lock:
            auth = self._require_live_root(root_auth)
            norm_tok, state = self._require_stream_state(token, allow_closing=False)

            try:
                seq_i = _require_exact_int(
                    frame_seq,
                    "frame_seq",
                    allow_zero=False,
                    max_value=w8u_memory.MAX_U64,
                    error_code="E_SEQ",
                )
                credit_delta = _require_exact_int(
                    grant_credit_on_admit,
                    "grant_credit_on_admit",
                    allow_zero=True,
                    max_value=w8u_memory.MAX_U32,
                    error_code="E_CREDIT",
                )
            except RamPolicyError as exc:
                raise self._record_refusal(exc)

            expected_seq = state.last_seq + 1
            if seq_i != expected_seq:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_SEQ",
                        "frame_seq must start at 1 and advance by 1 (expected %d, got %d; frame_seq must increase monotonically per stream)"
                        % (expected_seq, seq_i),
                    )
                )

            if not isinstance(rgb, np.ndarray):
                raise self._record_refusal(
                    RamPolicyError("E_GEOMETRY", "source must be exact uint8 HxWx3 RGB24")
                )
            if rgb.dtype != np.uint8 or rgb.shape != (state.spec.height, state.spec.width, 3):
                raise self._record_refusal(
                    RamPolicyError("E_GEOMETRY", "source must be exact uint8 HxWx3 RGB24")
                )
            if rgb.flags.writeable:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_WIRE",
                        "admitted frame must be an immutable owned snapshot (writeable=False)",
                    )
                )
            for st_other in self._streams.values():
                if st_other.ring_storage.shares_memory_with(rgb):
                    raise self._record_refusal(
                        RamPolicyError(
                            "E_WIRE",
                            "zero-copy violation: cannot admit borrowed ring storage as an owned snapshot",
                        )
                    )

            try:
                self.bank.publish(norm_tok, rgb, frame_seq=seq_i)
                read_out = self.bank.read(norm_tok)
            except window_stream_bank.StreamError as exc:
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc

            if isinstance(read_out, window_stream_bank.Drop):
                raise self._record_refusal(
                    RamPolicyError("E_TORN", "bank read returned Drop(%r)" % (read_out.reason,))
                )

            identity = FrameIdentity(
                frame_seq=seq_i,
                width=read_out.width,
                height=read_out.height,
                rgb24_sha256=read_out.digest,
                binding_sha256=auth.binding_sha256,
            )
            bound_ref = BoundFrameReference(
                run_id=auth.run_id,
                target_handle=norm_tok.handle,
                epoch=norm_tok.epoch,
                identity=identity,
            )
            state.last_seq = seq_i
            state.frames_published += 1
            state.credit_granted += credit_delta
            state.admitted_by_seq[seq_i] = bound_ref

            if channel is not None:
                channel.send({
                    "type": "FRAME_ADMITTED",
                    "frame_seq": seq_i,
                    "rgb24_sha256": identity.rgb24_sha256,
                    "binding_sha256": identity.binding_sha256,
                })

            return {
                "event": "admit_frame",
                "bound_reference": bound_ref,
                "frame_identity": identity,
                "frame_ref": read_out,
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "workspace_used_bytes": self._workspace.used,
                "frames_published": state.frames_published,
                "credit_granted": state.credit_granted,
            }


    def borrow_frame_view(
        self,
        root_auth: AuthenticatedRunRoot,
        token: window_stream_bank.StreamToken,
        *,
        frame_seq: int | None = None,
        borrow_id: str | None = None,
    ) -> tuple[BorrowedFrameView, dict[str, object]]:
        """Borrow an affine read-only zero-copy view from the live stream ring."""
        with self._lock:
            auth = self._require_live_root(root_auth)
            norm_tok, state = self._require_stream_state(token, allow_closing=False)
            if state.last_seq < 1:
                raise self._record_refusal(RamPolicyError("E_SEQ", "no frames published to borrow"))

            target_seq = state.last_seq if frame_seq is None else _require_exact_int(
                frame_seq, "frame_seq", allow_zero=False, error_code="E_SEQ"
            )
            if target_seq not in state.admitted_by_seq:
                raise self._record_refusal(
                    RamPolicyError("E_SEQ", "frame_seq %d was not admitted on stream" % target_seq)
                )
            bound_ref = state.admitted_by_seq[target_seq]

            buf = state.ring_storage.buffer
            if buf is None:
                raise self._record_refusal(RamPolicyError("E_BINDING", "ring buffer already released"))

            slot_idx = target_seq % state.spec.slots
            hdr_off = rtring.HDR + slot_idx * rtring.SLOT_HDR
            seq_val, render_seq = rtring._QQ.unpack_from(buf, hdr_off)
            crc_val = rtring._I.unpack_from(buf, hdr_off + rtring.CRC_OFF)[0]
            if (seq_val & 1) or render_seq != target_seq:
                raise self._record_refusal(
                    RamPolicyError("E_TORN", "slot for frame_seq %d has been lapped or is in flight" % target_seq)
                )
            frame_bytes = state.spec.width * state.spec.height * 3
            begin = rtring.data_off(state.spec.slots) + slot_idx * frame_bytes
            if (zlib.crc32(buf[begin : begin + frame_bytes]) & 0xFFFFFFFF) != (crc_val & 0xFFFFFFFF):
                raise self._record_refusal(RamPolicyError("E_TORN", "slot CRC mismatch on borrow"))

            self._borrow_counter += 1
            b_id = borrow_id if (type(borrow_id) is str and borrow_id) else ("borrow-%d" % self._borrow_counter)
            try:
                view = state.ring_storage.create_borrow(
                    borrow_id=b_id,
                    run_id=auth.run_id,
                    identity=bound_ref.identity,
                    begin_offset=begin,
                    byte_length=frame_bytes,
                    reserved_bytes_at_borrow=self.bank.reserved_bytes,
                )
            except RamPolicyError as exc:
                raise self._record_refusal(exc)

            witness = {
                "event": "borrow",
                "borrow_id": view.borrow_id,
                "run_id": auth.run_id,
                "frame_identity": bound_ref.identity.to_dict(),
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "workspace_used_bytes": self._workspace.used,
                "active_borrows": state.ring_storage.active_borrow_count,
            }
            return view, witness


    def release_borrowed_view(
        self,
        root_auth: AuthenticatedRunRoot,
        view: BorrowedFrameView,
    ) -> dict[str, object]:
        """Release an affine borrowed view and settle retained bank charges if stream was closing."""
        with self._lock:
            auth = self._require_live_root(root_auth)
            if not isinstance(view, BorrowedFrameView) or view.run_id != auth.run_id:
                raise self._record_refusal(RamPolicyError("E_BINDING", "borrowed view does not belong to active run_id"))
            key = (view.target_handle, view.epoch)
            state = self._streams.get(key)
            if state is None:
                raise self._record_refusal(RamPolicyError("E_BINDING", "unknown stream for borrowed view"))
            try:
                state.ring_storage.release_borrow(view.borrow_id)
            except RamPolicyError as exc:
                raise self._record_refusal(exc)

            settled_on_release = False
            if state.closing and state.ring_storage.active_borrow_count == 0:
                freed_count = self.bank.retry_cleanup()
                if freed_count > 0 and state.ring_storage._released:
                    state.purged = True
                    settled_on_release = True

            return {
                "event": "release_borrow",
                "borrow_id": view.borrow_id,
                "frame_seq": view.identity.frame_seq,
                "settled_on_release": settled_on_release,
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "workspace_used_bytes": self._workspace.used,
                "active_borrows": state.ring_storage.active_borrow_count,
            }


    def close_stream(
        self,
        root_auth: AuthenticatedRunRoot,
        token: window_stream_bank.StreamToken,
    ) -> dict[str, object]:
        """Close a stream in WindowStreamBank; retains obligation if any borrowed view is still held."""
        with self._lock:
            self._require_live_root(root_auth)
            norm_tok, state = self._require_stream_state(token, allow_closing=False)
            state.closing = True
            try:
                self.bank.close(norm_tok)
            except window_stream_bank.StreamError as exc:
                state.closing = False
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc

            if state.ring_storage._released:
                state.purged = True

            return {
                "event": "close_stream",
                "settled": state.ring_storage._released,
                "obligations_retained": not state.ring_storage._released,
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "workspace_used_bytes": self._workspace.used,
                "active_borrows": state.ring_storage.active_borrow_count,
            }


    def purge_stream(
        self,
        root_auth: AuthenticatedRunRoot,
        token: window_stream_bank.StreamToken,
    ) -> dict[str, object]:
        """Purge a stream; MUST FAIL with E_RAM_QUOTA if any borrowed view is still unreleased."""
        with self._lock:
            self._require_live_root(root_auth)
            norm_tok, state = self._require_stream_state(token, allow_closing=True)
            if state.ring_storage.active_borrow_count > 0:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_RAM_QUOTA",
                        "cannot purge stream while %d borrowed view(s) remain unreleased"
                        % state.ring_storage.active_borrow_count,
                    )
                )
            if not state.closing:
                state.closing = True
                self.bank.close(norm_tok)
            elif not state.ring_storage._released:
                self.bank.retry_cleanup()

            if not state.ring_storage._released:
                raise self._record_refusal(
                    RamPolicyError("E_RAM_QUOTA", "stream ring failed to unmap during purge")
                )
            state.purged = True
            return {
                "event": "purge_stream",
                "purged": True,
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "workspace_used_bytes": self._workspace.used,
                "active_borrows": state.ring_storage.active_borrow_count,
            }


    def purge_run(
        self,
        root_auth: AuthenticatedRunRoot,
        *,
        fabricated_cleanup_evidence: dict | None = None,
    ) -> tuple[CleanupAuthorityWitness, dict[str, object]]:
        """Verify all borrows and bank obligations are released, close RamWorkspace, and mint cleanup witness."""
        with self._lock:
            auth = self._require_live_root(root_auth)

            if fabricated_cleanup_evidence is not None:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_RAM_QUOTA",
                        "fabricated cleanup evidence rejected: receipt or close alone proves no authoritative cleanup",
                    )
                )

            active_borrows = self.total_active_borrows
            if active_borrows > 0:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_RAM_QUOTA",
                        "cannot purge run while %d borrowed view(s) remain unreleased" % active_borrows,
                    )
                )

            unpurged_streams = [
                st for st in self._streams.values() if not st.purged or not st.ring_storage._released
            ]
            if unpurged_streams or self.bank.reserved_bytes != 0:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_RAM_QUOTA",
                        "cannot purge run while bank reserved_bytes=%d or %d stream(s) remain unpurged"
                        % (self.bank.reserved_bytes, len(unpurged_streams)),
                    )
                )

            offending_before = ramws.check_disk(self._workspace.root, allow=())
            if offending_before:
                raise self._record_refusal(
                    RamPolicyError("E_ROOT", "disk/frame files detected in workspace: %r" % (offending_before,))
                )

            ws_receipt = self._workspace.receipt()
            self._workspace.close()
            if self._workspace.root.exists():
                raise self._record_refusal(
                    RamPolicyError("E_RAM_QUOTA", "RamWorkspace.close did not remove run directory")
                )

            digest_input = "%s:%d:%d:%d:%s" % (
                auth.run_id,
                auth.root_generation,
                self.bank.reserved_bytes,
                active_borrows,
                auth.capability_nonce,
            )
            witness = CleanupAuthorityWitness(
                run_id=auth.run_id,
                root_generation=auth.root_generation,
                adapter_id=self._adapter_id,
                bank_reserved_bytes=self.bank.reserved_bytes,
                active_borrows=active_borrows,
                workspace_closed=self._workspace._closed,
                witness_digest=hashlib.sha256(digest_input.encode("ascii")).hexdigest(),
            )
            self._cleanup_witness = witness
            self._root_generation += 1
            self._active_root = None
            return witness, ws_receipt


