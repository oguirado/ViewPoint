"""Compatibility exports for :mod:`vadapt.proto1_ram_policy`.

Implementation is split into contracts and cohesive operations; public names are stable.
"""
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
from ._ram_policy_frames import RamPolicyFramesMixin

class BStorageRamPolicyAdapter(RamPolicyFramesMixin):
    """Production RAM provenance, bank accounting, and borrowed-view lifetime controller for Area B."""


    def __init__(
        self,
        *,
        contract,
        workspace_quota_bytes: int = 16 * w8u_memory.MIB,
        bank_quota_bytes: int = 8 * w8u_memory.MIB,
        producer_receipt: dict | None = None,
        mountinfo: str = "/proc/self/mountinfo",
        statvfs=None,
        receipt_extension_validator=None,
    ):
        try:
            w8u_memory.require_accepted_contract(contract, error_code="E_BINDING")
        except w8u_memory.W8UMemoryError as exc:
            raise translate_storage_error(exc, default_code="E_BINDING") from exc

        merged_owners = (
            contract["merged_production_owners"]
            if isinstance(contract, dict) and "merged_production_owners" in contract
            else (contract.merged_production_owners if isinstance(contract, w8u_memory.ContractEvidence) else ())
        )
        producer_ok = "w8u-ve-memory" in merged_owners or (
            isinstance(producer_receipt, dict)
            and producer_receipt.get("producer") == "w8u-ve-memory"
            and producer_receipt.get("merged") is True
            and producer_receipt.get("verdict") in ("PASS", "ACCEPT")
        )
        if not producer_ok:
            raise RamPolicyError(
                "E_BINDING",
                "producer dependency w8u-ve-memory gate receipt is %s or not merged" % AUSENTE,
            )

        self._contract = contract
        self.workspace_quota_bytes = _require_exact_int(
            workspace_quota_bytes,
            "workspace_quota_bytes",
            allow_zero=False,
            max_value=ramws.RUN_MAX,
            error_code="E_RAM_QUOTA",
        )
        self.bank_quota_bytes = _require_exact_int(
            bank_quota_bytes,
            "bank_quota_bytes",
            allow_zero=False,
            max_value=ramws.RUN_MAX,
            error_code="E_RAM_QUOTA",
        )
        if self.bank_quota_bytes > self.workspace_quota_bytes:
            raise RamPolicyError(
                "E_RAM_QUOTA",
                "over-reservation: bank_quota_bytes (%d) exceeds workspace_quota_bytes (%d)"
                % (self.bank_quota_bytes, self.workspace_quota_bytes),
            )

        self._mountinfo = mountinfo
        self._statvfs = statvfs if statvfs is not None else _default_vm_statvfs_wrapper(os.statvfs, self.workspace_quota_bytes)
        self._receipt_extension_validator = receipt_extension_validator
        self._lock = threading.RLock()
        self._adapter_id = id(self)
        self._root_generation = 0
        self._borrow_counter = 0
        self._active_root: AuthenticatedRunRoot | None = None
        self._workspace: ramws.RamWorkspace | None = None
        self._binding: dict | None = None
        self._StagingEpochHint: dict[window_stream_bank.Handle, int] = {}
        self._StagingRelDir: dict[tuple[window_stream_bank.Handle, int], str] = {}
        self._streams: dict[tuple[window_stream_bank.Handle, int], _StreamLedgerState] = {}
        self._pending_ring_storages: dict[tuple[window_stream_bank.Handle, int], _ManagedRingStorage] = {}
        self._refusal_count = 0
        self._cleanup_witness: CleanupAuthorityWitness | None = None

        try:
            self.bank = window_stream_bank.WindowStreamBank(
                quota_bytes=self.bank_quota_bytes,
                ring_factory=self._ring_factory,
            )
        except window_stream_bank.StreamError as exc:
            raise translate_storage_error(exc, default_code="E_RAM_QUOTA") from exc


    @property
    def workspace(self) -> ramws.RamWorkspace | None:
        return self._workspace


    @property
    def reserved_bytes(self) -> int:
        return self.bank.reserved_bytes


    @property
    def workspace_used_bytes(self) -> int:
        if self._workspace is None:
            return 0
        return self._workspace.used


    @property
    def refusal_count(self) -> int:
        return self._refusal_count


    @property
    def total_active_borrows(self) -> int:
        with self._lock:
            return sum(st.ring_storage.active_borrow_count for st in self._streams.values())


    def _record_refusal(self, exc: RamPolicyError) -> RamPolicyError:
        with self._lock:
            self._refusal_count += 1
        return exc


    def _require_live_root(self, root_auth: object) -> AuthenticatedRunRoot:
        if not isinstance(root_auth, AuthenticatedRunRoot):
            raise self._record_refusal(RamPolicyError("E_ROOT", "unauthenticated or missing run-root capability"))
        if (
            self._active_root is None
            or root_auth.adapter_id != self._adapter_id
            or root_auth.root_generation != self._root_generation
            or root_auth.capability_nonce != self._active_root.capability_nonce
            or root_auth != self._active_root
        ):
            raise self._record_refusal(
                RamPolicyError("E_ROOT", "stale or unissued root identity (generation=%r)" % (root_auth.root_generation,))
            )
        if self._workspace is None or self._workspace._closed:
            raise self._record_refusal(RamPolicyError("E_ROOT", "RamWorkspace is closed or missing"))
        if not self._workspace.root.is_dir() or self._workspace.root.is_symlink():
            raise self._record_refusal(RamPolicyError("E_ROOT", "RamWorkspace root directory is missing or symlinked"))
        fstype = ramws.is_ram_fs(self._workspace.root, mountinfo=self._mountinfo)
        if fstype not in ACCEPTED_RAM_FSTYPES:
            raise self._record_refusal(RamPolicyError("E_ROOT", "live RamWorkspace is not on tmpfs/ramfs"))
        return self._active_root


    def open_run_root(
        self,
        binding_input: dict | bytes,
        *,
        grant_authenticator,
        root: str = CONFINED_ROOT,
        tool: str = CONFINED_TOOL,
        campaign: str = CONFINED_CAMPAIGN,
        share_with_uid: int | None = None,
        share_group: int | None = None,
    ) -> AuthenticatedRunRoot:
        """Validate binding, verify actual tmpfs/ramfs residency, and open a real confined RamWorkspace."""
        with self._lock:
            if self._active_root is not None and self._workspace is not None and not self._workspace._closed:
                raise self._record_refusal(RamPolicyError("E_ROOT", "a run root is already active on this adapter"))

            if root != CONFINED_ROOT or tool != CONFINED_TOOL or campaign != CONFINED_CAMPAIGN:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_ROOT",
                        "unconfined root/tool/campaign (%r, %r, %r)" % (root, tool, campaign),
                    )
                )

            try:
                binding_copy = copy.deepcopy(binding_input) if isinstance(binding_input, dict) else bytes(binding_input)
                binding, _, digest_raw = w8u_memory.validate_binding(
                    binding_copy,
                    contract=self._contract,
                    grant_authenticator=grant_authenticator,
                    check_fs_symlinks=True,
                )
            except w8u_memory.W8UMemoryError as exc:
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc

            run_id = binding["run_id"]
            expected_ws_run_root = "%s/%s/%s/%s" % (CONFINED_ROOT, CONFINED_TOOL, CONFINED_CAMPAIGN, run_id)
            if binding["frame_run_root"] != expected_ws_run_root:
                raise self._record_refusal(
                    RamPolicyError(
                        "E_ROOT",
                        "binding frame_run_root %r must match RamWorkspace path %r"
                        % (binding["frame_run_root"], expected_ws_run_root),
                    )
                )

            try:
                validate_ram_root_residency(
                    binding["frames_root"],
                    binding["frame_run_root"],
                    run_id,
                    mountinfo=self._mountinfo,
                    require_existing_run_dir=False,
                )
            except RamPolicyError as exc:
                raise self._record_refusal(exc)

            try:
                ws = ramws.open_run(
                    campaign,
                    run_id,
                    quota_bytes=self.workspace_quota_bytes,
                    tool=tool,
                    root=root,
                    statvfs=self._statvfs,
                    share_with_uid=share_with_uid,
                    share_group=share_group,
                )
            except ramws.RamError as exc:
                raise self._record_refusal(translate_storage_error(exc, default_code="E_ROOT")) from exc

            ws_receipt = ws.receipt()
            if ws_receipt.get("fstype") not in ACCEPTED_RAM_FSTYPES or str(ws.root) != expected_ws_run_root:
                ws.close()
                raise self._record_refusal(RamPolicyError("E_ROOT", "RamWorkspace failed post-open residency verification"))

            self._root_generation += 1
            binding_hex = digest_raw.hex()
            nonce_src = "%d:%d:%s:%s" % (self._adapter_id, self._root_generation, run_id, binding_hex)
            nonce = hashlib.sha256(nonce_src.encode("ascii")).hexdigest()
            auth_root = AuthenticatedRunRoot(
                run_id=run_id,
                frames_root=binding["frames_root"],
                frame_run_root=str(ws.root),
                fstype=ws_receipt["fstype"],
                quota_bytes=ws.quota,
                binding_sha256=binding_hex,
                root_generation=self._root_generation,
                adapter_id=self._adapter_id,
                capability_nonce=nonce,
            )
            self._workspace = ws
            self._binding = copy.deepcopy(binding)
            self._active_root = auth_root
            self._cleanup_witness = None
            return auth_root


    def _ring_factory(self, handle: window_stream_bank.Handle, spec: window_stream_bank.StreamSpec) -> _ManagedRingStorage:
        if self._workspace is None or self._workspace._closed:
            raise RamPolicyError("E_ROOT", "RamWorkspace is not open during ring allocation")
        epoch = self._StagingEpochHint.get(handle, 0)
        rel_dir = self._StagingRelDir[(handle, epoch)]
        ring_size = rtring.ring_bytes(spec.slots, spec.width, spec.height)
        try:
            self._workspace.reserve(ring_size)
        except ramws.RamError as exc:
            raise translate_storage_error(exc, default_code="E_RAM_QUOTA") from exc
        try:
            self._workspace.path(rel_dir)
        except ramws.RamError as exc:
            raise translate_storage_error(exc, default_code="E_ROOT") from exc
        storage = _ManagedRingStorage(
            size=ring_size,
            workspace=self._workspace,
            rel_dir=rel_dir,
            handle=handle,
            epoch=epoch,
        )
        self._pending_ring_storages[(handle, epoch)] = storage
        return storage


    def admit_stream(
        self,
        root_auth: AuthenticatedRunRoot,
        handle: object,
        spec: window_stream_bank.StreamSpec,
        *,
        configured_reservation_bytes: int | None = None,
        relative_workspace_dir: str | None = None,
    ) -> tuple[window_stream_bank.StreamToken, dict[str, object]]:
        """Reserve stream ring capacity in both RamWorkspace and WindowStreamBank."""
        with self._lock:
            auth = self._require_live_root(root_auth)
            try:
                norm_h = _normalize_handle(handle, error_code="E_BINDING")
                if not isinstance(spec, window_stream_bank.StreamSpec):
                    norm_spec = window_stream_bank.StreamSpec(spec.width, spec.height, spec.slots, spec.quota_bytes)
                else:
                    norm_spec = spec
            except (AttributeError, window_stream_bank.StreamError, RamPolicyError) as exc:
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc

            ring_size = rtring.ring_bytes(norm_spec.slots, norm_spec.width, norm_spec.height)

            if configured_reservation_bytes is not None:
                try:
                    cfg_bytes = _require_exact_int(
                        configured_reservation_bytes,
                        "configured_reservation_bytes",
                        allow_zero=False,
                        max_value=ramws.RUN_MAX,
                        error_code="E_RAM_QUOTA",
                    )
                except RamPolicyError as exc:
                    raise self._record_refusal(exc)
                if cfg_bytes < ring_size:
                    raise self._record_refusal(
                        RamPolicyError(
                            "E_RAM_QUOTA",
                            "reservation shortage: configured_reservation_bytes (%d) < ring_bytes (%d)"
                            % (cfg_bytes, ring_size),
                        )
                    )
                if norm_spec.quota_bytes > cfg_bytes:
                    raise self._record_refusal(
                        RamPolicyError(
                            "E_RAM_QUOTA",
                            "over-reservation: StreamSpec.quota_bytes (%d) > configured_reservation_bytes (%d)"
                            % (norm_spec.quota_bytes, cfg_bytes),
                        )
                    )
            else:
                cfg_bytes = norm_spec.quota_bytes

            if norm_spec.quota_bytes > self.bank.quota_bytes - self.bank.reserved_bytes:
                if ring_size <= self.bank.quota_bytes - self.bank.reserved_bytes:
                    raise self._record_refusal(
                        RamPolicyError(
                            "E_RAM_QUOTA",
                            "over-reservation: StreamSpec.quota_bytes (%d) exceeds remaining bank quota (%d)"
                            % (norm_spec.quota_bytes, self.bank.quota_bytes - self.bank.reserved_bytes),
                        )
                    )
            if norm_spec.quota_bytes > self._workspace.quota - self._workspace.used:
                if ring_size <= self._workspace.quota - self._workspace.used:
                    raise self._record_refusal(
                        RamPolicyError(
                            "E_RAM_QUOTA",
                            "over-reservation: StreamSpec.quota_bytes (%d) exceeds remaining workspace quota (%d)"
                            % (norm_spec.quota_bytes, self._workspace.quota - self._workspace.used),
                        )
                    )

            rel_dir = (
                relative_workspace_dir
                if relative_workspace_dir is not None
                else ("streams/%s_%s_g%d_e0/" % (norm_h.kind, norm_h.canonical_id, norm_h.generation))
            )
            if not rel_dir.endswith("/"):
                rel_dir = rel_dir + "/"
            try:
                preflight_workspace_relpath(self._workspace.root, rel_dir)
            except RamPolicyError as exc:
                raise self._record_refusal(exc)

            self._StagingEpochHint[norm_h] = 0
            self._StagingRelDir[(norm_h, 0)] = rel_dir
            try:
                token = self.bank.open(norm_h, norm_spec)
            except (window_stream_bank.StreamError, ramws.RamError, RamPolicyError) as exc:
                self._pending_ring_storages.pop((norm_h, 0), None)
                raise self._record_refusal(translate_storage_error(exc, default_code="E_RAM_QUOTA")) from exc
            finally:
                self._StagingEpochHint.pop(norm_h, None)
                self._StagingRelDir.pop((norm_h, 0), None)

            storage = self._pending_ring_storages.pop((norm_h, token.epoch))
            state = _StreamLedgerState(
                handle=norm_h,
                epoch=token.epoch,
                spec=norm_spec,
                ring_bytes=ring_size,
                configured_reservation_bytes=cfg_bytes,
                ring_storage=storage,
            )
            self._streams[(norm_h, token.epoch)] = state
            witness = {
                "event": "admit_stream",
                "run_id": auth.run_id,
                "target_handle": {
                    "kind": norm_h.kind,
                    "canonical_id": norm_h.canonical_id,
                    "generation": norm_h.generation,
                },
                "epoch": token.epoch,
                "ring_bytes": ring_size,
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "workspace_used_bytes": self._workspace.used,
                "active_borrows": 0,
            }
            return token, witness


    def admit_streams_many(
        self,
        root_auth: AuthenticatedRunRoot,
        requests: tuple | list,
    ) -> tuple[window_stream_bank.StreamToken, ...]:
        """Batch-admit multiple streams in canonical lock order via WindowStreamBank.open_many."""
        with self._lock:
            self._require_live_root(root_auth)
            pairs = []
            for h, s in requests:
                try:
                    norm_h = _normalize_handle(h, error_code="E_BINDING")
                    norm_s = s if isinstance(s, window_stream_bank.StreamSpec) else window_stream_bank.StreamSpec(s.width, s.height, s.slots, s.quota_bytes)
                except (AttributeError, window_stream_bank.StreamError, RamPolicyError) as exc:
                    raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc
                rel_dir = "streams/%s_%s_g%d_e0/" % (norm_h.kind, norm_h.canonical_id, norm_h.generation)
                try:
                    preflight_workspace_relpath(self._workspace.root, rel_dir)
                except RamPolicyError as exc:
                    raise self._record_refusal(exc)
                pairs.append((norm_h, norm_s, rel_dir))

            for norm_h, _, rel_dir in pairs:
                self._StagingEpochHint[norm_h] = 0
                self._StagingRelDir[(norm_h, 0)] = rel_dir
            try:
                tokens = self.bank.open_many([(h, s) for h, s, _ in pairs])
            except (window_stream_bank.StreamError, ramws.RamError, RamPolicyError) as exc:
                for norm_h, _, _ in pairs:
                    self._pending_ring_storages.pop((norm_h, 0), None)
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc
            finally:
                for norm_h, _, _ in pairs:
                    self._StagingEpochHint.pop(norm_h, None)
                    self._StagingRelDir.pop((norm_h, 0), None)

            for tok, (norm_h, norm_s, _) in zip(tokens, pairs):
                ring_size = rtring.ring_bytes(norm_s.slots, norm_s.width, norm_s.height)
                storage = self._pending_ring_storages.pop((norm_h, tok.epoch))
                self._streams[(norm_h, tok.epoch)] = _StreamLedgerState(
                    handle=norm_h,
                    epoch=tok.epoch,
                    spec=norm_s,
                    ring_bytes=ring_size,
                    configured_reservation_bytes=norm_s.quota_bytes,
                    ring_storage=storage,
                )
            return tokens


    def resize_stream(
        self,
        root_auth: AuthenticatedRunRoot,
        token: window_stream_bank.StreamToken,
        new_spec: window_stream_bank.StreamSpec,
    ) -> tuple[window_stream_bank.StreamToken, dict[str, object]]:
        """Resize an open stream to a new geometry epoch via WindowStreamBank.resize."""
        with self._lock:
            self._require_live_root(root_auth)
            try:
                norm_h = _normalize_handle(token.handle, error_code="E_BINDING")
                tok_epoch = _require_exact_int(token.epoch, "epoch", allow_zero=True, error_code="E_BINDING")
                norm_tok = window_stream_bank.StreamToken(norm_h, tok_epoch)
                norm_spec = (
                    new_spec
                    if isinstance(new_spec, window_stream_bank.StreamSpec)
                    else window_stream_bank.StreamSpec(new_spec.width, new_spec.height, new_spec.slots, new_spec.quota_bytes)
                )
            except (AttributeError, window_stream_bank.StreamError, RamPolicyError) as exc:
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc

            old_state = self._streams.get((norm_h, tok_epoch))
            new_epoch = (old_state.epoch + 1) if old_state is not None else (tok_epoch + 1)
            rel_dir = "streams/%s_%s_g%d_e%d/" % (norm_h.kind, norm_h.canonical_id, norm_h.generation, new_epoch)
            try:
                preflight_workspace_relpath(self._workspace.root, rel_dir)
            except RamPolicyError as exc:
                raise self._record_refusal(exc)

            self._StagingEpochHint[norm_h] = new_epoch
            self._StagingRelDir[(norm_h, new_epoch)] = rel_dir
            try:
                new_tok = self.bank.resize(norm_tok, norm_spec)
            except (window_stream_bank.StreamError, ramws.RamError, RamPolicyError) as exc:
                self._pending_ring_storages.pop((norm_h, new_epoch), None)
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc
            finally:
                self._StagingEpochHint.pop(norm_h, None)
                self._StagingRelDir.pop((norm_h, new_epoch), None)

            if old_state is not None:
                old_state.closing = True
                if old_state.ring_storage._released:
                    old_state.purged = True

            ring_size = rtring.ring_bytes(norm_spec.slots, norm_spec.width, norm_spec.height)
            storage = self._pending_ring_storages.pop((norm_h, new_tok.epoch))
            self._streams[(norm_h, new_tok.epoch)] = _StreamLedgerState(
                handle=norm_h,
                epoch=new_tok.epoch,
                spec=norm_spec,
                ring_bytes=ring_size,
                configured_reservation_bytes=norm_spec.quota_bytes,
                ring_storage=storage,
                last_seq=old_state.last_seq if old_state is not None else 0,
            )
            return new_tok, {
                "event": "resize_stream",
                "new_epoch": new_tok.epoch,
                "old_settled": old_state.ring_storage._released if old_state is not None else True,
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "workspace_used_bytes": self._workspace.used,
            }


    def _require_stream_state(
        self,
        token: object,
        *,
        allow_closing: bool = False,
    ) -> tuple[window_stream_bank.StreamToken, _StreamLedgerState]:
        try:
            norm_h = _normalize_handle(token.handle, error_code="E_BINDING")
            epoch = _require_exact_int(token.epoch, "epoch", allow_zero=True, error_code="E_BINDING")
        except (AttributeError, RamPolicyError) as exc:
            raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc
        norm_tok = window_stream_bank.StreamToken(norm_h, epoch)
        state = self._streams.get((norm_h, epoch))
        if state is None or state.purged or (state.closing and not allow_closing):
            raise self._record_refusal(RamPolicyError("E_BINDING", "closed generation or stale geometry epoch"))
        return norm_tok, state


    def build_seat_record(
        self,
        root_auth: AuthenticatedRunRoot,
        token: window_stream_bank.StreamToken,
        *,
        frame_seq: int,
        verdict: str = "PASS",
        code: str = "OK",
        extra_keys: dict | None = None,
    ) -> dict[str, object]:
        """Build a closed-schema seat record without unauthorized keys or pixel data."""
        with self._lock:
            auth = self._require_live_root(root_auth)
            norm_tok, state = self._require_stream_state(token, allow_closing=True)
            if frame_seq not in state.admitted_by_seq:
                raise self._record_refusal(RamPolicyError("E_SEQ", "frame_seq %d not admitted" % frame_seq))
            bound_ref = state.admitted_by_seq[frame_seq]
            rec: dict[str, object] = {
                "run_id": auth.run_id,
                "lease_id": self._binding["lease_id"],
                "target_handle": {
                    "kind": norm_tok.handle.kind,
                    "canonical_id": norm_tok.handle.canonical_id,
                    "generation": norm_tok.handle.generation,
                },
                "epoch": norm_tok.epoch,
                "binding_sha256": auth.binding_sha256,
                "frame_identity": bound_ref.identity.to_dict(),
                "reserved_bytes": self.bank.reserved_bytes,
                "active_borrows": state.ring_storage.active_borrow_count,
                "verdict": verdict,
                "code": code,
            }
            if extra_keys:
                rec.update(extra_keys)
            try:
                return validate_closed_seat_record(rec)
            except RamPolicyError as exc:
                raise self._record_refusal(exc)


    def project_metadata_receipt(
        self,
        *,
        cleanup_witness: CleanupAuthorityWitness | None,
        ws_receipt: dict,
        first_frame: dict | None = None,
        last_frame: dict | None = None,
        admitted_actions: int | None = None,
        applied_actions: int | None = None,
        frames_captured: int | None = None,
        verdict: str = "PASS",
        code: str = "OK",
        extra_storage_evidence: dict | None = None,
        require_receipt_extension_validator: bool = False,
    ) -> dict[str, object]:
        """Project a metadata-only w8u.receipt/v1 receipt requiring proven cleanup and no pixels."""
        with self._lock:
            if verdict in ("PASS", "ACCEPT") or code == "OK":
                if self._refusal_count > 0:
                    raise self._record_refusal(
                        RamPolicyError(
                            "E_BINDING",
                            "rejection must not mint receipt success (refusal_count=%d)" % self._refusal_count,
                        )
                    )
                if (
                    cleanup_witness is None
                    or not isinstance(cleanup_witness, CleanupAuthorityWitness)
                    or cleanup_witness != self._cleanup_witness
                    or cleanup_witness.bank_reserved_bytes != 0
                    or cleanup_witness.active_borrows != 0
                    or not cleanup_witness.workspace_closed
                    or self.bank.reserved_bytes != 0
                    or self.total_active_borrows != 0
                ):
                    raise self._record_refusal(
                        RamPolicyError(
                            "E_RAM_QUOTA",
                            "cannot mint PASS receipt or cleanup authority without verified CleanupAuthorityWitness",
                        )
                    )

            if require_receipt_extension_validator and not callable(self._receipt_extension_validator):
                raise self._record_refusal(
                    RamPolicyError(
                        "E_BINDING",
                        "pending authoritative receipt-extension validator is %s; w8u_memory.project_receipt cannot replace it"
                        % AUSENTE,
                    )
                )

            total_published = sum(st.frames_published for st in self._streams.values())
            unknown_timing = MeasurementRecord(
                value=None,
                reason="AUSENTE: VM timing is not host timing; live timing qualification remains conductor/os-cu-0 lane gate",
                claim_label="AUSENTE",
            )
            storage_ev = {
                "ram_peak_bytes": ws_receipt["ram_peak_bytes"],
                "quota": ws_receipt["quota"],
                "released_bytes": ws_receipt["released_bytes"],
                "swap_used_delta": ws_receipt["swap_used_delta"],
                "fstype": ws_receipt["fstype"],
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "active_borrows": self.total_active_borrows,
                "host_timing_ns": unknown_timing.to_dict(),
                "conductor_lane_gates": dict(CONDUCTOR_LANE_GATES),
                "receipt_extension_validator": (
                    "VERIFIED" if callable(self._receipt_extension_validator) else AUTHORITATIVE_RECEIPT_EXTENSION_VALIDATOR
                ),
            }
            if extra_storage_evidence:
                storage_ev.update(extra_storage_evidence)

            cleanup_ev = {
                "purged": True,
                "witness_digest": cleanup_witness.witness_digest if cleanup_witness is not None else None,
                "bank_reserved_bytes": self.bank.reserved_bytes,
                "active_borrows": self.total_active_borrows,
            }

            try:
                validate_metadata_no_pixels(storage_ev)
                validate_metadata_no_pixels(first_frame)
                validate_metadata_no_pixels(last_frame)
                receipt = w8u_memory.project_receipt(
                    contract=self._contract,
                    identity={
                        "run_id": self._binding["run_id"] if self._binding else AUSENTE,
                        "lease_id": self._binding["lease_id"] if self._binding else AUSENTE,
                    },
                    admitted_actions=admitted_actions,
                    applied_actions=applied_actions,
                    frames_captured=frames_captured,
                    frames_published=total_published,
                    first_frame=first_frame,
                    last_frame=last_frame,
                    verdict=verdict,
                    code=code,
                    storage_evidence=storage_ev,
                    listener_evidence={"eof": True},
                    exit_evidence={"joined": True},
                    cleanup_evidence=cleanup_ev,
                    retention_mode="metadata_only",
                    pixel_replay_claimed=False,
                    input_authority_claimed=False,
                )
                validate_metadata_no_pixels(receipt)
                if callable(self._receipt_extension_validator):
                    if self._receipt_extension_validator(receipt) is not True:
                        raise RamPolicyError("E_BINDING", "authoritative receipt-extension validator rejected receipt")
            except (w8u_memory.W8UMemoryError, RamPolicyError) as exc:
                raise self._record_refusal(translate_storage_error(exc, default_code="E_BINDING")) from exc

            return receipt




__all__ = [
    "ACCEPTED_RAM_FSTYPES",
    "ACCEPT",
    "AUSENTE",
    "AUTHORITATIVE_RECEIPT_EXTENSION_VALIDATOR",
    "BStorageRamPolicyAdapter",
    "BorrowedFrameView",
    "BoundFrameReference",
    "CLAIM_LABELS",
    "CLOSED_ERROR_CODES",
    "CLOSED_SEAT_RECORD_KEYS",
    "CONDUCTOR_LANE_GATES",
    "CONFINED_CAMPAIGN",
    "CONFINED_ROOT",
    "CONFINED_TOOL",
    "CleanupAuthorityWitness",
    "FRAME_IDENTITY_FIELDS",
    "FrameIdentity",
    "MeasurementRecord",
    "PREPARED",
    "PUBLIC_SIGNATURES",
    "PersistenceWriteSpy",
    "RamPolicyError",
    "install_irreversible_persistence_audit_hook",
    "join_bound_references",
    "make_immutable_rgb24",
    "preflight_workspace_relpath",
    "run_in_isolated_audit_process",
    "translate_storage_error",
    "validate_closed_seat_record",
    "validate_metadata_no_pixels",
    "validate_ram_root_residency",
]
