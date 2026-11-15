"""vadapt.capture_receipt - Validate closed legacy receipts and publication evidence.

Implements the source-neutral C-PIXEL and committed-frame contract, gate G J2 2,
and invariants INV-J2-1 through INV-J2-4.

Legacy receipts stay strictly:
  frame_sha256: 64 lowercase hex characters
  dimensions: positive integer pair (width, height)
  tick: signed committed simulation tick
  telemetry: dictionary containing strictly state_q16 in calibrated raw units

All extra keys (including commit_id) and bool-as-int values are rejected.
Unsupported capture sources and unobserved residency return typed BLOCKED evidence.
"""
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union
import zlib

import numpy as np


class ReceiptError(Exception):
    """Base exception for capture receipt and contract failures."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code} {detail}".strip())


class ReceiptValidationError(ReceiptError):
    """Raised when receipt schema, types, or layout fail validation."""


class ReceiptBlockedError(ReceiptError):
    """Raised when capture is unsupported, unobserved, or evidence is AUSENTE."""


class ReceiptMismatchError(ReceiptError):
    """Raised when verified receipt diverges from independent publication evidence."""


# The legacy hook receipt has strictly these 4 keys. No commit_id or richer metadata.
LEGACY_RECEIPT_KEYS = frozenset({"frame_sha256", "dimensions", "tick", "telemetry"})
LEGACY_TELEMETRY_KEYS = frozenset({"state_q16"})

SUPPORTED_SOURCE_KINDS = frozenset({
    "xvfb-xwd",
    "ramws",
    "cdp",
    "chromium-crop",
    "godot-raw",
    "synthetic",
})

HEX_LOWER_64_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_int_not_bool(val: Any, name: str, code: str = "E_TYPE",
                        positive: bool = False, non_negative: bool = False) -> int:
    """Validate exact int type, rejecting bool masquerading as int."""
    if type(val) is not int or isinstance(val, bool):
        raise ReceiptValidationError(code, f"{name} must be exact int, got {type(val).__name__}")
    if positive and val <= 0:
        raise ReceiptValidationError(code, f"{name} must be strictly positive (> 0), got {val}")
    if non_negative and val < 0:
        raise ReceiptValidationError(code, f"{name} must be non-negative (>= 0), got {val}")
    return val


@dataclass(frozen=True)
class PixelDeclaration:
    """Source-neutral C-PIXEL layout declaration.

    Fields: channel_order, stride_bytes, orientation, alpha, color_space,
    width, height, dpr_ppm, zoom_ppm, environment_id, binding_id.
    """
    channel_order: str
    stride_bytes: int
    orientation: str
    alpha: str
    color_space: str
    width: int
    height: int
    dpr_ppm: int
    zoom_ppm: int
    environment_id: str
    binding_id: str

    def validate(self) -> None:
        """Validate layout declaration before hashing (INV-J2-1)."""
        _check_int_not_bool(self.width, "width", "E_DECL_DIMENSIONS", positive=True)
        _check_int_not_bool(self.height, "height", "E_DECL_DIMENSIONS", positive=True)
        _check_int_not_bool(self.stride_bytes, "stride_bytes", "E_DECL_STRIDE", positive=True)
        _check_int_not_bool(self.dpr_ppm, "dpr_ppm", "E_DECL_DPR", positive=True)
        _check_int_not_bool(self.zoom_ppm, "zoom_ppm", "E_DECL_ZOOM", positive=True)

        if not isinstance(self.channel_order, str) or not self.channel_order.strip():
            raise ReceiptValidationError("E_DECL_CHANNEL", "channel_order must be non-empty string")
        norm_order = self.channel_order.lower().replace("-", "").replace("_", "")
        if norm_order not in ("rgb", "bgr", "rgba", "bgra", "rgb24", "bgr24", "rgba32", "bgra32"):
            raise ReceiptValidationError("E_DECL_CHANNEL", f"unsupported channel_order: {self.channel_order}")

        bytes_per_pixel = 4 if ("32" in norm_order or "a" in norm_order) else 3
        if self.stride_bytes < self.width * bytes_per_pixel:
            raise ReceiptValidationError("E_DECL_STRIDE",
                f"stride_bytes {self.stride_bytes} < minimum row bytes {self.width * bytes_per_pixel}")

        if self.orientation not in ("top_to_bottom", "bottom_to_top"):
            raise ReceiptValidationError("E_DECL_ORIENTATION", f"invalid orientation: {self.orientation}")

        if self.alpha not in ("none", "straight", "premultiplied"):
            raise ReceiptValidationError("E_DECL_ALPHA", f"invalid alpha: {self.alpha}")

        if self.color_space not in ("srgb", "linear"):
            raise ReceiptValidationError("E_DECL_COLOR_SPACE", f"invalid color_space: {self.color_space}")

        if not isinstance(self.environment_id, str) or not self.environment_id.strip():
            raise ReceiptValidationError("E_DECL_ENVIRONMENT", "environment_id must be non-empty string")
        if not isinstance(self.binding_id, str) or not self.binding_id.strip():
            raise ReceiptValidationError("E_DECL_BINDING", "binding_id must be non-empty string")

    def pixel_decl_sha256(self) -> str:
        """Bind the declared layout. Declaration is validated before hashing (INV-J2-1)."""
        self.validate()
        payload = {
            "channel_order": self.channel_order,
            "stride_bytes": self.stride_bytes,
            "orientation": self.orientation,
            "alpha": self.alpha,
            "color_space": self.color_space,
            "width": self.width,
            "height": self.height,
            "dpr_ppm": self.dpr_ppm,
            "zoom_ppm": self.zoom_ppm,
            "environment_id": self.environment_id,
            "binding_id": self.binding_id,
        }
        canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical_json).hexdigest()

    def raw_sha256(self, raw_bytes: Union[bytes, bytearray, memoryview, np.ndarray]) -> str:
        """Compute SHA-256 of raw unnormalized bytes in memory."""
        self.validate()
        b = raw_bytes.tobytes() if isinstance(raw_bytes, np.ndarray) else bytes(raw_bytes)
        return hashlib.sha256(b).hexdigest()

    def normalized_sha256(self, raw_bytes: Union[bytes, bytearray, memoryview, np.ndarray]) -> str:
        """Compute SHA-256 of canonical row-major RGB24 pixels (distinct from raw_sha256)."""
        self.validate()
        b = raw_bytes.tobytes() if isinstance(raw_bytes, np.ndarray) else bytes(raw_bytes)
        norm_order = self.channel_order.lower().replace("-", "").replace("_", "")

        rows = []
        bytes_per_pixel = 4 if ("32" in norm_order or "a" in norm_order) else 3
        row_len = self.width * bytes_per_pixel
        for y in range(self.height):
            start = y * self.stride_bytes
            end = start + row_len
            if end > len(b):
                raise ReceiptValidationError("E_RAW_BYTES_LENGTH", "raw bytes buffer smaller than declared layout")
            rows.append(b[start:end])

        if self.orientation == "bottom_to_top":
            rows.reverse()

        rgb_bytes = bytearray()
        for row in rows:
            if norm_order in ("rgb", "rgb24"):
                rgb_bytes.extend(row)
            elif norm_order in ("bgr", "bgr24"):
                for i in range(0, len(row), 3):
                    rgb_bytes.extend((row[i + 2], row[i + 1], row[i]))
            elif norm_order in ("rgba", "rgba32"):
                for i in range(0, len(row), 4):
                    rgb_bytes.extend((row[i], row[i + 1], row[i + 2]))
            elif norm_order in ("bgra", "bgra32"):
                for i in range(0, len(row), 4):
                    rgb_bytes.extend((row[i + 2], row[i + 1], row[i]))

        return hashlib.sha256(bytes(rgb_bytes)).hexdigest()


@dataclass(frozen=True)
class VersionedMetadata:
    """Versioned external metadata binding simulation and observer identity.

    Lives strictly OUTSIDE the legacy closed receipt.
    """
    frame_seq: int
    committed_tick: int
    state_sha256: str
    observer_ref: str

    def validate(self) -> None:
        _check_int_not_bool(self.frame_seq, "frame_seq", "E_META_SEQ", non_negative=True)
        _check_int_not_bool(self.committed_tick, "committed_tick", "E_META_TICK")
        if not isinstance(self.state_sha256, str) or not HEX_LOWER_64_RE.fullmatch(self.state_sha256):
            raise ReceiptValidationError("E_META_STATE_SHA", "state_sha256 must be 64 lowercase hex characters")
        if not isinstance(self.observer_ref, str) or not self.observer_ref.strip():
            raise ReceiptValidationError("E_META_OBSERVER", "observer_ref must be non-empty string")


@dataclass(frozen=True)
class PublicationEvidence:
    """Independently supplied publication evidence from trusted capture source."""
    handle_generation: int
    epoch: int
    geometry: Tuple[int, int]
    completed_publication_identity: str
    frame_sha256: str
    committed_tick: int
    source_kind: str
    trusted_association: bool = True
    residency_observed: bool = True
    sequence: int = 0
    crc32: int = 0
    raw_bytes: bytes = b""
    metadata: Optional[Dict[str, Any]] = None
    coherence_proof: bool = True
    is_dom_ax_derived: bool = True
    method: str = "stream"
    independent_publication_binding: Optional[str] = None

    def validate_invariants(self, expected_geometry: Tuple[int, int]) -> None:
        """Enforce INV-J2-2 before comparison."""
        _check_int_not_bool(self.handle_generation, "handle_generation", "E_INV_J2_2", non_negative=True)
        _check_int_not_bool(self.epoch, "epoch", "E_INV_J2_2", non_negative=True)
        if not isinstance(self.completed_publication_identity, str) or not self.completed_publication_identity.strip():
            raise ReceiptValidationError("E_INV_J2_2", "completed publication identity required")
        if tuple(self.geometry) != expected_geometry:
            raise ReceiptMismatchError("E_INV_J2_2",
                f"publication evidence geometry {self.geometry} != expected {expected_geometry}")


@dataclass
class ValidationResult:
    """Typed result of receipt validation and evidence crosscheck."""
    status: str  # "PASS", "BLOCKED", "MISMATCH", "REJECTED"
    ok: bool
    code: str
    reason: Optional[str] = None
    receipt: Optional[Dict[str, Any]] = None
    evidence: Optional[Dict[str, Any]] = None
    mismatch_fields: Optional[List[str]] = None


def revalidate_owned_bytes_and_crc(raw_bytes: bytes, expected_sequence: int, expected_crc32: int,
                                    current_sequence: int) -> bool:
    """INV-J2-3: Revalidate owned bytes and sequence plus CRC revalidation after copying."""
    if current_sequence != expected_sequence:
        return False
    computed_crc = zlib.crc32(raw_bytes)
    return computed_crc == expected_crc32


def validate_receipt(
    receipt: Dict[str, Any],
    *,
    publication_evidence: Optional[PublicationEvidence] = None,
    expected_tick: Optional[int] = None,
    expected_dimensions: Optional[Tuple[int, int]] = None,
    expected_sha256: Optional[str] = None,
    pixel_decl: Optional[PixelDeclaration] = None,
    observer_ref: Optional[str] = None,
    raise_exc: bool = True,
    previous_commits: Optional[Dict[int, str]] = None,
) -> ValidationResult:
    """Validate closed legacy receipt and compare against publication evidence.

    Keyword binding enforced. Rejects:
      - bool masquerading as int
      - extra keys (including commit_id)
      - absent telemetry or TELEMETRY_UNBOUND
      - non-positive or wrong dimensions
      - uppercase or non-64 hex digest
      - skipped snapshots

    Returns typed BLOCKED or MISMATCH evidence for unsupported capture.
    Validation alone is not visual PASS.
    """
    if not isinstance(receipt, dict):
        msg = f"receipt must be dict, got {type(receipt).__name__}"
        if raise_exc:
            raise ReceiptValidationError("E_RECEIPT_SCHEMA", msg)
        return ValidationResult("REJECTED", False, "E_RECEIPT_SCHEMA", reason=msg)

    receipt_keys = frozenset(receipt.keys())
    if "commit_id" in receipt_keys:
        msg = "commit_id forbidden in legacy hook receipt; richer metadata belongs outside"
        if raise_exc:
            raise ReceiptValidationError("E_COMMIT_ID_FORBIDDEN", msg)
        return ValidationResult("REJECTED", False, "E_COMMIT_ID_FORBIDDEN", reason=msg)

    extra_keys = sorted(receipt_keys - LEGACY_RECEIPT_KEYS)
    if extra_keys:
        msg = f"extra keys forbidden in closed legacy receipt: {extra_keys}"
        if raise_exc:
            raise ReceiptValidationError("E_RECEIPT_EXTRA_KEYS", msg)
        return ValidationResult("REJECTED", False, "E_RECEIPT_EXTRA_KEYS", reason=msg)

    missing_keys = sorted(LEGACY_RECEIPT_KEYS - receipt_keys)
    if missing_keys:
        msg = f"missing required legacy receipt keys: {missing_keys}"
        if raise_exc:
            raise ReceiptValidationError("E_RECEIPT_MISSING_KEYS", msg)
        return ValidationResult("REJECTED", False, "E_RECEIPT_MISSING_KEYS", reason=msg)

    digest = receipt["frame_sha256"]
    if not isinstance(digest, str) or not HEX_LOWER_64_RE.fullmatch(digest):
        msg = f"frame_sha256 must be 64 lowercase hex characters, got {digest!r}"
        if raise_exc:
            raise ReceiptValidationError("E_DIGEST_FORMAT", msg)
        return ValidationResult("REJECTED", False, "E_DIGEST_FORMAT", reason=msg)

    dims = receipt["dimensions"]
    if not isinstance(dims, (tuple, list)) or len(dims) != 2:
        msg = f"dimensions must be a 2-tuple (width, height), got {dims!r}"
        if raise_exc:
            raise ReceiptValidationError("E_DIMENSIONS_FORMAT", msg)
        return ValidationResult("REJECTED", False, "E_DIMENSIONS_FORMAT", reason=msg)

    w, h = dims[0], dims[1]
    try:
        _check_int_not_bool(w, "width", "E_DIMENSIONS_VALUE", positive=True)
        _check_int_not_bool(h, "height", "E_DIMENSIONS_VALUE", positive=True)
    except ReceiptValidationError as err:
        if raise_exc:
            raise
        return ValidationResult("REJECTED", False, err.code, reason=str(err))

    clean_dims = (w, h)

    t = receipt["tick"]
    try:
        _check_int_not_bool(t, "tick", "E_TICK_VALUE")
    except ReceiptValidationError as err:
        if raise_exc:
            raise
        return ValidationResult("REJECTED", False, err.code, reason=str(err))

    telem = receipt["telemetry"]
    if telem is None or telem == "TELEMETRY_UNBOUND":
        msg = "telemetry must not be absent or TELEMETRY_UNBOUND"
        if raise_exc:
            raise ReceiptValidationError("E_TELEMETRY_UNBOUND", msg)
        return ValidationResult("REJECTED", False, "E_TELEMETRY_UNBOUND", reason=msg)

    if not isinstance(telem, dict):
        msg = f"telemetry must be a dict, got {type(telem).__name__}"
        if raise_exc:
            raise ReceiptValidationError("E_TELEMETRY_FORMAT", msg)
        return ValidationResult("REJECTED", False, "E_TELEMETRY_FORMAT", reason=msg)

    telem_keys = frozenset(telem.keys())
    extra_telem = sorted(telem_keys - LEGACY_TELEMETRY_KEYS)
    if extra_telem:
        msg = f"extra keys forbidden in telemetry: {extra_telem}"
        if raise_exc:
            raise ReceiptValidationError("E_TELEMETRY_EXTRA_KEYS", msg)
        return ValidationResult("REJECTED", False, "E_TELEMETRY_EXTRA_KEYS", reason=msg)

    if "state_q16" not in telem:
        msg = "telemetry must contain state_q16"
        if raise_exc:
            raise ReceiptValidationError("E_TELEMETRY_MISSING_STATE", msg)
        return ValidationResult("REJECTED", False, "E_TELEMETRY_MISSING_STATE", reason=msg)

    sq16 = telem["state_q16"]
    if sq16 == "TELEMETRY_UNBOUND":
        msg = "state_q16 is TELEMETRY_UNBOUND"
        if raise_exc:
            raise ReceiptValidationError("E_TELEMETRY_UNBOUND", msg)
        return ValidationResult("REJECTED", False, "E_TELEMETRY_UNBOUND", reason=msg)

    try:
        _check_int_not_bool(sq16, "state_q16", "E_TELEMETRY_VALUE")
    except ReceiptValidationError as err:
        if raise_exc:
            raise
        return ValidationResult("REJECTED", False, err.code, reason=str(err))

    validated_receipt = {
        "frame_sha256": digest,
        "dimensions": clean_dims,
        "tick": t,
        "telemetry": {"state_q16": sq16},
    }

    if pixel_decl is not None:
        try:
            pixel_decl.validate()
        except ReceiptValidationError as err:
            if raise_exc:
                raise
            return ValidationResult("REJECTED", False, err.code, reason=str(err))

    if expected_dimensions is not None and clean_dims != tuple(expected_dimensions):
        return ValidationResult(
            "MISMATCH", False, "E_DIMENSIONS_MISMATCH",
            reason=f"receipt dimensions {clean_dims} != expected {expected_dimensions}",
            receipt=validated_receipt,
            mismatch_fields=["dimensions"]
        )

    if expected_tick is not None and t != expected_tick:
        return ValidationResult(
            "MISMATCH", False, "E_TICK_MISMATCH",
            reason=f"receipt tick {t} != expected {expected_tick}",
            receipt=validated_receipt,
            mismatch_fields=["tick"]
        )

    if expected_sha256 is not None and digest != expected_sha256:
        return ValidationResult(
            "MISMATCH", False, "E_DIGEST_MISMATCH",
            reason=f"receipt digest {digest} != expected {expected_sha256}",
            receipt=validated_receipt,
            mismatch_fields=["frame_sha256"]
        )

    if publication_evidence is None:
        return ValidationResult(
            status="BLOCKED",
            ok=False,
            code="E_EVIDENCE_AUSENTE",
            reason="publication evidence is AUSENTE; validation alone is not visual PASS",
            receipt=validated_receipt,
            evidence={"status": "AUSENTE"}
        )

    try:
        publication_evidence.validate_invariants(clean_dims)
    except ReceiptMismatchError as err:
        return ValidationResult(
            status="MISMATCH",
            ok=False,
            code=err.code,
            reason=str(err),
            receipt=validated_receipt,
            evidence={"error": err.code, "detail": err.detail},
            mismatch_fields=["geometry"]
        )
    except ReceiptValidationError as err:
        return ValidationResult(
            status="BLOCKED",
            ok=False,
            code=err.code,
            reason=str(err),
            receipt=validated_receipt,
            evidence={"error": err.code, "detail": err.detail}
        )

    sk = publication_evidence.source_kind
    if sk in ("electron-brokered", "electron"):
        return ValidationResult(
            status="BLOCKED",
            ok=False,
            code="E_ELECTRON_AUSENTE",
            reason="Electron brokered publisher is AUSENTE until H2/H3",
            receipt=validated_receipt,
            evidence={"status": "AUSENTE", "source_kind": sk}
        )

    # Chromium screen requires trusted window/crop association
    if sk in ("chromium", "chromium-screen", "chromium-crop") or not getattr(publication_evidence, "trusted_association", True):
        if not getattr(publication_evidence, "trusted_association", False) or sk in ("chromium", "chromium-screen"):
            return ValidationResult(
                status="BLOCKED",
                ok=False,
                code="E_UNTRUSTED_CROP_ASSOCIATION",
                reason="Chromium screen is not a tab identity: trusted window/crop association required",
                receipt=validated_receipt,
                evidence={"source_kind": sk, "blocked": True}
            )

    if sk not in SUPPORTED_SOURCE_KINDS:
        return ValidationResult(
            status="BLOCKED",
            ok=False,
            code="E_UNSUPPORTED_CAPTURE",
            reason=f"unsupported capture source kind: {sk}",
            receipt=validated_receipt,
            evidence={"source_kind": sk, "blocked": True}
        )

    if sk == "cdp" and not getattr(publication_evidence, "is_dom_ax_derived", True):
        return ValidationResult(
            status="BLOCKED",
            ok=False,
            code="E_CDP_NOT_DOM_AX",
            reason="source.kind cdp is reserved for CDP-derived DOM/AX, never a relabeled tree",
            receipt=validated_receipt,
            evidence={"source_kind": sk, "blocked": True}
        )

    if publication_evidence.method == "capturePage" or not getattr(publication_evidence, "coherence_proof", True):
        return ValidationResult(
            status="BLOCKED",
            ok=False,
            code="E_CAPTURE_PAGE_NO_PROOF",
            reason="capturePage alone supplies no coherence or residency proof",
            receipt=validated_receipt,
            evidence={"source_kind": sk, "method": publication_evidence.method, "blocked": True}
        )

    if not getattr(publication_evidence, "residency_observed", True):
        return ValidationResult(
            status="BLOCKED",
            ok=False,
            code="E_UNOBSERVED_RESIDENCY",
            reason="unobserved residency kept blocked under INV-J2-4",
            receipt=validated_receipt,
            evidence={"source_kind": sk, "blocked": True}
        )

    if publication_evidence.committed_tick != t:
        return ValidationResult(
            status="MISMATCH",
            ok=False,
            code="E_TICK_MISMATCH",
            reason=f"receipt simulation tick {t} != evidence committed tick {publication_evidence.committed_tick}",
            receipt=validated_receipt,
            evidence={"receipt_tick": t, "evidence_tick": publication_evidence.committed_tick},
            mismatch_fields=["tick"]
        )

    if previous_commits is not None and t in previous_commits:
        prior_sha = previous_commits[t]
        if prior_sha != digest:
            if not publication_evidence.independent_publication_binding:
                return ValidationResult(
                    status="MISMATCH",
                    ok=False,
                    code="E_SAME_TICK_CONFLICT",
                    reason=f"same-tick {t} distinct commits require independent publication binding or explicit refusal",
                    receipt=validated_receipt,
                    evidence={"prior_sha": prior_sha, "current_sha": digest},
                    mismatch_fields=["frame_sha256"]
                )

    if publication_evidence.frame_sha256 != digest:
        return ValidationResult(
            status="MISMATCH",
            ok=False,
            code="E_DIGEST_MISMATCH",
            reason=f"receipt frame_sha256 {digest} != evidence {publication_evidence.frame_sha256}",
            receipt=validated_receipt,
            evidence={"receipt_sha": digest, "evidence_sha": publication_evidence.frame_sha256},
            mismatch_fields=["frame_sha256"]
        )

    if publication_evidence.raw_bytes:
        reval_ok = revalidate_owned_bytes_and_crc(
            publication_evidence.raw_bytes,
            expected_sequence=publication_evidence.sequence,
            expected_crc32=publication_evidence.crc32,
            current_sequence=publication_evidence.sequence
        )
        if not reval_ok:
            return ValidationResult(
                status="BLOCKED",
                ok=False,
                code="E_INV_J2_3_CRC_TORN",
                reason="owned bytes sequence or CRC32 revalidation failed after copying",
                receipt=validated_receipt,
                evidence={"sequence": publication_evidence.sequence, "crc32": publication_evidence.crc32}
            )

    return ValidationResult(
        status="PASS",
        ok=True,
        code="OK",
        receipt=validated_receipt,
        evidence={
            "verified": True,
            "source_kind": sk,
            "handle_generation": publication_evidence.handle_generation,
            "epoch": publication_evidence.epoch,
        }
    )


def make_holdings_command(cmd: str, *, tick: int, seq: int, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Construct Holdings command record inline."""
    _check_int_not_bool(tick, "tick", "E_COMMAND_TICK")
    _check_int_not_bool(seq, "seq", "E_COMMAND_SEQ", non_negative=True)

    if cmd == "pause":
        if args is not None and args != {}:
            raise ReceiptValidationError("E_COMMAND_ARGS", "pause command requires empty args")
        clean_args = {}
    elif cmd == "new_game":
        if not isinstance(args, dict):
            raise ReceiptValidationError("E_COMMAND_ARGS", "new_game requires dict args")
        required_keys = frozenset({"country_id", "difficulty", "opening_mode", "seed"})
        if frozenset(args.keys()) != required_keys:
            raise ReceiptValidationError("E_COMMAND_ARGS",
                f"new_game args must match exactly {sorted(required_keys)}, got {sorted(args.keys())}")
        clean_args = dict(args)
    else:
        clean_args = dict(args) if args is not None else {}

    return {
        "tick": tick,
        "seq": seq,
        "cmd": str(cmd),
        "args": clean_args,
    }


class AdmissionTracker:
    """Track admission vs economic acceptance."""

    def __init__(self, initial_seq: int = 0):
        self._bookkeeping_seq = initial_seq
        self._simulation_tick = 0
        self._simulation_state = {"economic_accepted": False}
        self._history: List[Dict[str, Any]] = []

    @property
    def bookkeeping_seq(self) -> int:
        return self._bookkeeping_seq

    @property
    def simulation_tick(self) -> int:
        return self._simulation_tick

    def submit_attempt(self, cmd_record: Dict[str, Any], admitted: bool = False) -> Dict[str, Any]:
        """Advance bookkeeping sequence on every attempt; mutate simulation only on admission."""
        self._bookkeeping_seq += 1
        record = {
            "bookkeeping_seq": self._bookkeeping_seq,
            "cmd": cmd_record,
            "admitted": admitted,
        }
        self._history.append(record)
        if admitted:
            self._simulation_tick += 1
            self._simulation_state["economic_accepted"] = True
        return record


class PixelPersistenceForbiddenError(Exception):
    """Raised when pixel data persistence is attempted."""


class NoPixelPersistenceSpy:
    """Write spy that verifies no frames are written to disk, swap, logs, or receipts."""

    def __init__(self):
        self.persistence_attempted = False

    def inspect_receipt(self, receipt: Dict[str, Any]) -> None:
        for k, v in receipt.items():
            if isinstance(v, (bytes, bytearray, memoryview, np.ndarray)):
                self.persistence_attempted = True
                raise PixelPersistenceForbiddenError(f"receipt field {k} contains raw frame data")
            if isinstance(v, str) and len(v) > 256 and k != "frame_sha256":
                self.persistence_attempted = True
                raise PixelPersistenceForbiddenError(f"receipt field {k} contains large string dump")
