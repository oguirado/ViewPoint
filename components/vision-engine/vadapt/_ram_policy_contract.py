"""Production RAM provenance and borrowed-view lifetime enforcement for Area B storage.

Owner: ``p1-va-ram-policy`` (Area B).
Producer dependency: ``w8u-ve-memory`` (``vadapt.w8u_memory``).

Proposed Public Call Signatures
-------------------------------
- ``RamPolicyError(code: str, msg: str = "", detail: dict | None = None)``
    Typed refusal deriving from ``w8u_memory.W8UMemoryError`` and ``ramws.RamError``.
- ``translate_storage_error(exc: BaseException, *, default_code: str = "E_BINDING") -> RamPolicyError``
    Explicit adapter translation mapping ``ramws.RamError`` (``E_RAM_NOT_TMPFS``,
    ``E_RAM_ESCAPE``, ``E_RAM_LOCKED``, ``E_RAM_SHARE``) to ``E_ROOT``,
    ``window_stream_bank.QuotaError`` and ``RamError("E_RAM_QUOTA")`` to
    ``E_RAM_QUOTA``, ``window_stream_bank.StaleStreamError`` to ``E_BINDING``,
    and ``window_stream_bank.StreamError`` geometry/sequence/torn errors to
    ``E_GEOMETRY`` / ``E_SEQ`` / ``E_TORN`` / ``E_BINDING``.
- ``FrameIdentity(frame_seq: int, width: int, height: int, rgb24_sha256: str, binding_sha256: str)``
    Frozen dataclass with fields in exact order:
    ``(frame_seq, width, height, rgb24_sha256, binding_sha256)``.
- ``BoundFrameReference(run_id: str, target_handle: window_stream_bank.Handle, epoch: int, identity: FrameIdentity)``
    Frozen reference binding a ``FrameIdentity`` to ``run_id``, structural
    ``target_handle`` (``kind``, ``canonical_id``, ``generation``), and ``epoch``.
- ``MeasurementRecord(value: int | None, reason: str | None = None, claim_label: str = "MEASURED")``
    Frozen measurement wrapper enforcing that unknown measurements stay ``None``
    plus a non-empty ``reason``, never ``0``, with explicit claim labels
    ``TARGET``, ``CITED``, ``MEASURED``, or ``AUSENTE``.
- ``make_immutable_rgb24(raw_bytes: bytes, width: int, height: int) -> np.ndarray``
    Construct a bytes-backed immutable ``uint8`` ``(height, width, 3)`` RGB24
    NumPy array (``writeable=False``).
- ``preflight_workspace_relpath(ws_root: str | Path, rel: str) -> str``
    Pure preflight containment check without creating parent directories.
- ``validate_metadata_no_pixels(record: object, *, width: int | None = None, height: int | None = None) -> bool``
    Verify that a metadata receipt or record excludes raw, encoded, compressed,
    and reconstructible pixels.
- ``validate_closed_seat_record(record: dict, *, allowed_keys: frozenset[str] = CLOSED_SEAT_RECORD_KEYS) -> dict``
    Reject unauthorized keys in closed seat schemas with ``E_BINDING``.
- ``join_bound_references(producer_refs: tuple | list, consumer_refs: tuple | list, *, join_mode: str = "exact_binding") -> tuple[tuple[BoundFrameReference, BoundFrameReference], ...]``
    Join frame references strictly by ``(run_id, target_handle, epoch, frame_seq, binding_sha256)``,
    refusing nearest-timestamp or positional zipping with ``E_BINDING``.
- ``PersistenceWriteSpy(*, allow_ramws_control_files: bool = False, refusal_code: str = "E_ROOT")``
    Context manager intercepting actual persistence entry points (``builtins.open``,
    ``os.open``, ``io.open``, ``io.FileIO``, ``pathlib.Path.open``,
    ``pathlib.Path.write_bytes``, ``pathlib.Path.write_text``, ``numpy.save``,
    ``numpy.savez``, ``numpy.savez_compressed``, ``numpy.savetxt``).
- ``install_irreversible_persistence_audit_hook(*, refusal_code: str = "E_ROOT") -> list[tuple[str, object, object]]``
    Install an irreversible process-wide ``sys.addaudithook`` blocking write
    opens and offline frame files. Must only be called in an isolated child process.
- ``run_in_isolated_audit_process(fn, *args, **kwargs) -> dict``
    Execute ``fn(*args, **kwargs)`` in a forked child process with the global
    irreversible audit hook installed, keeping the parent process clean.
- ``BStorageRamPolicyAdapter(*, contract, workspace_quota_bytes: int = ..., bank_quota_bytes: int = ..., producer_receipt: dict | None = None, mountinfo: str = "/proc/self/mountinfo", statvfs=None, receipt_extension_validator=None)``
    Production B-storage RAM provenance and borrowed-view lifetime controller
    backed by real ``ramws.RamWorkspace`` and ``window_stream_bank.WindowStreamBank``.
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

AUSENTE = w8u_memory.AUSENTE
PREPARED = w8u_memory.PREPARED
ACCEPT = w8u_memory.ACCEPT

CLAIM_LABELS = ("TARGET", "CITED", "MEASURED", "AUSENTE")

CONFINED_ROOT = ramws.ROOT
CONFINED_TOOL = w8u_memory.CONFINED_TOOL
CONFINED_CAMPAIGN = w8u_memory.CONFINED_CAMPAIGN
ACCEPTED_RAM_FSTYPES = ramws.RAM_FS

CLOSED_ERROR_CODES = w8u_memory.CLOSED_ERROR_CODES

FRAME_IDENTITY_FIELDS = (
    "frame_seq",
    "width",
    "height",
    "rgb24_sha256",
    "binding_sha256",
)

CLOSED_SEAT_RECORD_KEYS = frozenset({
    "run_id",
    "lease_id",
    "target_handle",
    "epoch",
    "binding_sha256",
    "frame_identity",
    "reserved_bytes",
    "active_borrows",
    "verdict",
    "code",
})

FORBIDDEN_PIXEL_KEYS = frozenset({
    "rgb",
    "rgb24",
    "rgba",
    "rgba8",
    "bgr",
    "bgrx",
    "xrgb",
    "xwd",
    "xwd_a",
    "xwd_b",
    "pixels",
    "raw_pixels",
    "encoded_pixels",
    "compressed_pixels",
    "reconstructible_pixels",
    "png",
    "jpeg",
    "jpg",
    "webp",
    "bmp",
    "gif",
    "zlib_pixels",
    "rle_pixels",
    "base64_pixels",
    "frame_bytes",
    "payload_bytes_raw",
    "pixel_buffer",
})

AUTHORITATIVE_RECEIPT_EXTENSION_VALIDATOR = AUSENTE

CONDUCTOR_LANE_GATES = {
    "live_image_profile": AUSENTE,
    "host_timing_qualification": AUSENTE,
    "conductor_root_qualification": AUSENTE,
}

PUBLIC_SIGNATURES = {
    "RamPolicyError": "(code: str, msg: str = '', detail: dict | None = None)",
    "translate_storage_error": "(exc: BaseException, *, default_code: str = 'E_BINDING') -> RamPolicyError",
    "FrameIdentity": "(frame_seq: int, width: int, height: int, rgb24_sha256: str, binding_sha256: str)",
    "BoundFrameReference": "(run_id: str, target_handle: Handle, epoch: int, identity: FrameIdentity)",
    "MeasurementRecord": "(value: int | None, reason: str | None = None, claim_label: str = 'MEASURED')",
    "make_immutable_rgb24": "(raw_bytes: bytes, width: int, height: int) -> np.ndarray",
    "preflight_workspace_relpath": "(ws_root: str | Path, rel: str) -> str",
    "validate_metadata_no_pixels": "(record: object, *, width: int | None = None, height: int | None = None) -> bool",
    "validate_closed_seat_record": "(record: dict, *, allowed_keys: frozenset[str] = CLOSED_SEAT_RECORD_KEYS) -> dict",
    "join_bound_references": "(producer_refs, consumer_refs, *, join_mode: str = 'exact_binding') -> tuple",
    "PersistenceWriteSpy": "(*, allow_ramws_control_files: bool = False, refusal_code: str = 'E_ROOT')",
    "install_irreversible_persistence_audit_hook": "(*, refusal_code: str = 'E_ROOT') -> list",
    "run_in_isolated_audit_process": "(fn, *args, **kwargs) -> dict",
    "BStorageRamPolicyAdapter": "(*, contract, workspace_quota_bytes: int, bank_quota_bytes: int, producer_receipt: dict | None = None, mountinfo: str = '/proc/self/mountinfo', statvfs=None, receipt_extension_validator=None)",
}

_HEX64 = frozenset("0123456789abcdef")


class RamPolicyError(w8u_memory.W8UMemoryError):
    """Closed Area B RAM provenance and lifetime policy refusal."""

    def __init__(self, code: str, msg: str = "", detail: dict | None = None):
        super().__init__(code, msg, detail=detail)


def _require_exact_int(
    value: object,
    name: str,
    *,
    allow_zero: bool = False,
    max_value: int | None = w8u_memory.MAX_U64,
    error_code: str = "E_RAM_QUOTA",
) -> int:
    if type(value) is not int:
        raise RamPolicyError(error_code, "%s must be an exact int (got %r)" % (name, value))
    lower = 0 if allow_zero else 1
    if value < lower or (max_value is not None and value > max_value):
        raise RamPolicyError(
            error_code,
            "%s=%d outside allowed range [%d, %s]" % (name, value, lower, max_value),
        )
    return value


def _require_hex64(value: object, name: str, *, error_code: str) -> str:
    if type(value) is not str or len(value) != 64 or not all(ch in _HEX64 for ch in value):
        raise RamPolicyError(error_code, "%s must be 64 lowercase hex characters" % name)
    return value


def _normalize_handle(handle: object, *, error_code: str = "E_BINDING") -> window_stream_bank.Handle:
    if isinstance(handle, window_stream_bank.Handle):
        return handle
    if isinstance(handle, dict):
        if set(handle.keys()) != w8u_memory.TARGET_HANDLE_KEYS:
            raise RamPolicyError(error_code, "target_handle dict must have exact keys kind/canonical_id/generation")
        try:
            return window_stream_bank.Handle(handle["kind"], handle["canonical_id"], handle["generation"])
        except window_stream_bank.StreamError as exc:
            raise RamPolicyError(error_code, str(exc)) from exc
    try:
        kind = handle.kind
        canonical_id = handle.canonical_id
        generation = handle.generation
    except AttributeError as exc:
        raise RamPolicyError(error_code, "expected structural handle with kind, canonical_id, generation") from exc
    try:
        return window_stream_bank.Handle(kind, canonical_id, generation)
    except window_stream_bank.StreamError as exc:
        raise RamPolicyError(error_code, str(exc)) from exc


def translate_storage_error(exc: BaseException, *, default_code: str = "E_BINDING") -> RamPolicyError:
    """Translate native ramws and window_stream_bank exceptions to closed adapter codes."""
    if isinstance(exc, RamPolicyError):
        return exc
    if isinstance(exc, w8u_memory.W8UMemoryError):
        if exc.code in ("E_RAM_NOT_TMPFS", "E_RAM_ESCAPE", "E_RAM_LOCKED", "E_RAM_SHARE"):
            return RamPolicyError(
                "E_ROOT",
                exc.message or str(exc),
                detail={"native_code": exc.code, **dict(exc.detail)},
            )
        return RamPolicyError(exc.code, exc.message or str(exc), detail=dict(exc.detail))
    if isinstance(exc, ramws.RamError):
        if exc.code in ("E_RAM_NOT_TMPFS", "E_RAM_ESCAPE", "E_RAM_LOCKED", "E_RAM_SHARE"):
            return RamPolicyError("E_ROOT", str(exc), detail={"native_code": exc.code})
        if exc.code == "E_RAM_QUOTA":
            return RamPolicyError("E_RAM_QUOTA", str(exc), detail={"native_code": exc.code})
        return RamPolicyError(default_code, str(exc), detail={"native_code": exc.code})
    if isinstance(exc, window_stream_bank.QuotaError):
        return RamPolicyError("E_RAM_QUOTA", str(exc), detail={"native_type": "QuotaError"})
    if isinstance(exc, window_stream_bank.StaleStreamError):
        return RamPolicyError("E_BINDING", str(exc), detail={"native_type": "StaleStreamError"})
    if isinstance(exc, window_stream_bank.StreamError):
        msg = str(exc)
        if "source must be exact uint8 HxWx3 RGB24" in msg:
            return RamPolicyError("E_GEOMETRY", msg, detail={"native_type": "StreamError"})
        if "frame_seq must increase monotonically per stream" in msg:
            return RamPolicyError("E_SEQ", msg, detail={"native_type": "StreamError"})
        if "slot already has in-flight writer" in msg:
            return RamPolicyError("E_TORN", msg, detail={"native_type": "StreamError"})
        return RamPolicyError("E_BINDING", msg, detail={"native_type": "StreamError"})
    return RamPolicyError(default_code, str(exc), detail={"native_type": type(exc).__name__})


@dataclass(frozen=True)
class FrameIdentity:
    """Ordered frame identity: (frame_seq, width, height, rgb24_sha256, binding_sha256)."""

    frame_seq: int
    width: int
    height: int
    rgb24_sha256: str
    binding_sha256: str

    def __post_init__(self) -> None:
        _require_exact_int(self.frame_seq, "frame_seq", allow_zero=False, max_value=w8u_memory.MAX_U64, error_code="E_SEQ")
        _require_exact_int(self.width, "width", allow_zero=False, max_value=w8u_memory.MAX_U32, error_code="E_GEOMETRY")
        _require_exact_int(self.height, "height", allow_zero=False, max_value=w8u_memory.MAX_U32, error_code="E_GEOMETRY")
        _require_hex64(self.rgb24_sha256, "rgb24_sha256", error_code="E_HASH")
        _require_hex64(self.binding_sha256, "binding_sha256", error_code="E_BINDING")

    def as_tuple(self) -> tuple[int, int, int, str, str]:
        return (
            self.frame_seq,
            self.width,
            self.height,
            self.rgb24_sha256,
            self.binding_sha256,
        )

    def to_dict(self) -> dict[str, int | str]:
        out = {
            "frame_seq": self.frame_seq,
            "width": self.width,
            "height": self.height,
            "rgb24_sha256": self.rgb24_sha256,
            "binding_sha256": self.binding_sha256,
        }
        if tuple(out.keys()) != FRAME_IDENTITY_FIELDS:
            raise RamPolicyError("E_WIRE", "FrameIdentity key order violated")
        return out


@dataclass(frozen=True)
class BoundFrameReference:
    """FrameIdentity bound to run_id, structural target_handle (kind, canonical_id, generation), and epoch."""

    run_id: str
    target_handle: window_stream_bank.Handle
    epoch: int
    identity: FrameIdentity

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not ramws.NAME_RE.match(self.run_id) or self.run_id in (".", ".."):
            raise RamPolicyError("E_BINDING", "invalid run_id %r in BoundFrameReference" % (self.run_id,))
        norm_h = _normalize_handle(self.target_handle, error_code="E_BINDING")
        object.__setattr__(self, "target_handle", norm_h)
        _require_exact_int(self.epoch, "epoch", allow_zero=True, max_value=w8u_memory.MAX_U64, error_code="E_BINDING")
        if not isinstance(self.identity, FrameIdentity):
            raise RamPolicyError("E_BINDING", "identity must be a FrameIdentity instance")

    def binding_key(self) -> tuple[str, str, str, int, int, int, str]:
        return (
            self.run_id,
            self.target_handle.kind,
            self.target_handle.canonical_id,
            self.target_handle.generation,
            self.epoch,
            self.identity.frame_seq,
            self.identity.binding_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "target_handle": {
                "kind": self.target_handle.kind,
                "canonical_id": self.target_handle.canonical_id,
                "generation": self.target_handle.generation,
            },
            "epoch": self.epoch,
            "frame_identity": self.identity.to_dict(),
        }


@dataclass(frozen=True)
class MeasurementRecord:
    """Measurement value keeping unknown metrics None plus a reason (never 0)."""

    value: int | None
    reason: str | None = None
    claim_label: str = "MEASURED"

    def __post_init__(self) -> None:
        if self.claim_label not in CLAIM_LABELS:
            raise RamPolicyError("E_WIRE", "invalid claim_label %r" % (self.claim_label,))
        if self.value is None:
            if type(self.reason) is not str or not self.reason.strip():
                raise RamPolicyError("E_WIRE", "unknown measurement (None) must carry a non-empty reason")
        else:
            _require_exact_int(self.value, "measurement.value", allow_zero=True, error_code="E_WIRE")
            if self.claim_label == AUSENTE:
                raise RamPolicyError("E_WIRE", "AUSENTE measurement must have value=None plus reason, never %r" % (self.value,))
            if type(self.reason) is str and ("unknown" in self.reason.lower() or "ausente" in self.reason.lower() or "vm timing" in self.reason.lower()):
                raise RamPolicyError("E_WIRE", "unknown or VM-for-host measurement must stay None plus reason, never %r" % (self.value,))

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "reason": self.reason,
            "claim_label": self.claim_label,
        }


@dataclass(frozen=True)
class AuthenticatedRunRoot:
    """Generational capability representing a live, tmpfs/ramfs-verified confined run root."""

    run_id: str
    frames_root: str
    frame_run_root: str
    fstype: str
    quota_bytes: int
    binding_sha256: str
    root_generation: int
    adapter_id: int
    capability_nonce: str


@dataclass(frozen=True)
class CleanupAuthorityWitness:
    """Unforgeable witness minted only after all borrows and bank reservations settle and workspace closes."""

    run_id: str
    root_generation: int
    adapter_id: int
    bank_reserved_bytes: int
    active_borrows: int
    workspace_closed: bool
    witness_digest: str


def make_immutable_rgb24(raw_bytes: bytes, width: int, height: int) -> np.ndarray:
    """Create a bytes-backed immutable uint8 (height, width, 3) RGB24 array."""
    w = _require_exact_int(width, "width", allow_zero=False, max_value=w8u_memory.MAX_U32, error_code="E_GEOMETRY")
    h = _require_exact_int(height, "height", allow_zero=False, max_value=w8u_memory.MAX_U32, error_code="E_GEOMETRY")
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise RamPolicyError("E_GEOMETRY", "raw_bytes must be bytes-like")
    immutable_buf = bytes(raw_bytes)
    expected = w * h * 3
    if len(immutable_buf) != expected:
        raise RamPolicyError("E_GEOMETRY", "RGB24 buffer length %d != %d" % (len(immutable_buf), expected))
    arr = np.frombuffer(immutable_buf, dtype=np.uint8).reshape(h, w, 3)
    if arr.flags.writeable:
        arr.setflags(writeable=False)
    return arr


def preflight_workspace_relpath(ws_root: str | Path, rel: str) -> str:
    """Validate a workspace-relative path without creating parent directories."""
    if type(rel) is not str or not rel or "\x00" in rel:
        raise RamPolicyError("E_ROOT", "bad relative workspace path %r" % (rel,))
    if os.path.isabs(rel):
        raise RamPolicyError("E_ROOT", "workspace path must be relative: %r" % (rel,))
    parts = Path(rel).parts
    if not parts or "." in parts or ".." in parts:
        raise RamPolicyError("E_ROOT", "traversal component in workspace path: %r" % (rel,))
    root_real = os.path.realpath(str(ws_root))
    cur = Path(root_real)
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise RamPolicyError("E_ROOT", "symlink in workspace path: %s" % cur)
    resolved = os.path.realpath(str(Path(root_real) / rel))
    if resolved != root_real and not resolved.startswith(root_real + os.sep):
        raise RamPolicyError("E_ROOT", "workspace path escapes root: %r" % (rel,))
    return rel


def validate_ram_root_residency(
    frames_root: str,
    frame_run_root: str,
    run_id: str,
    *,
    mountinfo: str = "/proc/self/mountinfo",
    require_existing_run_dir: bool = False,
) -> str:
    """Validate confined RAM root paths AND actual tmpfs/ramfs residency from mountinfo."""
    try:
        w8u_memory.validate_confined_roots(
            frames_root,
            frame_run_root,
            run_id,
            check_fs_symlinks=True,
        )
    except w8u_memory.W8UMemoryError as exc:
        raise translate_storage_error(exc, default_code="E_ROOT") from exc

    if not os.path.isfile(mountinfo):
        raise RamPolicyError("E_ROOT", "mountinfo %r is absent (%s)" % (mountinfo, AUSENTE))

    anc = Path(frames_root)
    while not anc.exists() and anc != anc.parent:
        anc = anc.parent
    fstype = ramws.is_ram_fs(anc, mountinfo=mountinfo)
    if fstype not in ACCEPTED_RAM_FSTYPES:
        raise RamPolicyError(
            "E_ROOT",
            "frames_root %r resides on non-RAM filesystem %r (path prefix alone proves no RAM residency)"
            % (frames_root, fstype),
        )

    run_anc = Path(frame_run_root)
    if require_existing_run_dir and not run_anc.is_dir():
        raise RamPolicyError("E_ROOT", "frame_run_root directory %r does not exist" % (frame_run_root,))
    while not run_anc.exists() and run_anc != run_anc.parent:
        run_anc = run_anc.parent
    run_fstype = ramws.is_ram_fs(run_anc, mountinfo=mountinfo)
    if run_fstype not in ACCEPTED_RAM_FSTYPES:
        raise RamPolicyError(
            "E_ROOT",
            "frame_run_root %r resides on non-RAM filesystem %r" % (frame_run_root, run_fstype),
        )
    return run_fstype


def _looks_like_reconstructible_pixels(value: object, *, min_pixel_bytes: int) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview, np.ndarray)):
        return True
    if type(value) is str:
        s_lower = value.lower()
        if s_lower.startswith("data:image/") or ";base64," in s_lower:
            return True
        for magic in ("\x89png", "w8uf", "riff", "gif87a", "gif89a"):
            if magic in s_lower:
                return True
        if len(value) > 64 and all(ch in "0123456789abcdefABCDEF" for ch in value):
            return True
        if len(value) >= max(24, min_pixel_bytes):
            b64_chars = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
            if len(value) % 4 == 0 and all(ch in b64_chars for ch in value) and ("=" in value or len(value) >= 128):
                return True
    if isinstance(value, (list, tuple)) and len(value) >= max(12, min_pixel_bytes):
        if all(type(x) is int and 0 <= x <= 255 for x in value):
            return True
    return False


def validate_metadata_no_pixels(
    record: object,
    *,
    width: int | None = None,
    height: int | None = None,
) -> bool:
    """Ensure metadata receipts and records contain no raw, encoded, compressed, or reconstructible pixels."""
    min_px = 12
    if type(width) is int and type(height) is int and width > 0 and height > 0:
        min_px = max(6, width * height)

    def _walk(node: object, path: str) -> None:
        if _looks_like_reconstructible_pixels(node, min_pixel_bytes=min_px):
            raise RamPolicyError(
                "E_WIRE",
                "metadata receipt contains forbidden raw/encoded/compressed/reconstructible pixels at %s" % path,
            )
        if isinstance(node, dict):
            for k, v in node.items():
                if type(k) is not str:
                    raise RamPolicyError("E_WIRE", "metadata key must be str at %s" % path)
                if k.lower() in FORBIDDEN_PIXEL_KEYS:
                    raise RamPolicyError(
                        "E_WIRE",
                        "forbidden pixel key %r in metadata receipt at %s" % (k, path),
                    )
                _walk(v, "%s.%s" % (path, k))
        elif isinstance(node, (list, tuple)):
            for idx, item in enumerate(node):
                _walk(item, "%s[%d]" % (path, idx))

    _walk(record, "root")
    return True


def validate_closed_seat_record(
    record: dict,
    *,
    allowed_keys: frozenset[str] = CLOSED_SEAT_RECORD_KEYS,
) -> dict:
    """Validate that an adapter seat record does not add unauthorized keys to a closed schema."""
    if not isinstance(record, dict):
        raise RamPolicyError("E_BINDING", "seat record must be a dict")
    actual_keys = frozenset(record.keys())
    extra_keys = actual_keys - allowed_keys
    missing_keys = allowed_keys - actual_keys
    if extra_keys or missing_keys:
        raise RamPolicyError(
            "E_BINDING",
            "closed seat schema violation: extra=%s missing=%s"
            % (sorted(extra_keys), sorted(missing_keys)),
        )
    validate_metadata_no_pixels(record)
    return record


def join_bound_references(
    producer_refs: tuple | list,
    consumer_refs: tuple | list,
    *,
    join_mode: str = "exact_binding",
) -> tuple[tuple[BoundFrameReference, BoundFrameReference], ...]:
    """Bind and join frame references strictly by (run_id, target_handle, epoch, frame_seq, binding_sha256).

    Never uses nearest timestamps or positional zipping of unrelated lists.
    """
    if join_mode != "exact_binding":
        raise RamPolicyError(
            "E_BINDING",
            "forbidden join_mode %r: nearest-timestamp and positional list zipping are prohibited" % (join_mode,),
        )
    if not isinstance(producer_refs, (tuple, list)) or not isinstance(consumer_refs, (tuple, list)):
        raise RamPolicyError("E_BINDING", "producer_refs and consumer_refs must be sequences")

    by_key: dict[tuple, BoundFrameReference] = {}
    for ref in producer_refs:
        if not isinstance(ref, BoundFrameReference):
            raise RamPolicyError("E_BINDING", "producer reference must be BoundFrameReference")
        key = ref.binding_key()
        if key in by_key:
            raise RamPolicyError("E_BINDING", "duplicate producer reference key %r" % (key,))
        by_key[key] = ref

    joined: list[tuple[BoundFrameReference, BoundFrameReference]] = []
    for cref in consumer_refs:
        if not isinstance(cref, BoundFrameReference):
            raise RamPolicyError("E_BINDING", "consumer reference must be BoundFrameReference")
        key = cref.binding_key()
        if key not in by_key:
            raise RamPolicyError(
                "E_BINDING",
                "consumer reference %r has no exact bound producer match (nearest-timestamp/zipping forbidden)"
                % (key,),
            )
        pref = by_key[key]
        if pref.identity != cref.identity:
            raise RamPolicyError(
                "E_HASH",
                "bound reference FrameIdentity mismatch for seq %d" % cref.identity.frame_seq,
            )
        joined.append((pref, cref))
    return tuple(joined)


# ---------------------------------------------------------------------------
# Persistence Write Interception & Isolated Process Irreversible Audit Hook
# ---------------------------------------------------------------------------

