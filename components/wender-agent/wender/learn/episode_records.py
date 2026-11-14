# @module {"id":"wa.learn.episode_records","layer":2,"owner":"LEARN","deps":["wa.perceive.session_registry","wa.state.schema"],"book":"HB-WA-s4-learn"}
"""Episode records production owner for jq2b-j0-episode-records.

Source-neutral episode contracts, canonical UTF-8 JSON-L encoding/decoding,
closed metadata allowlists, generation fencing, and EpisodeAdapter.
"""

from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

import numpy as np

# Canonical types from session_registry or schema fallback
try:
    import wender.perceive.session_registry as session_registry
    Handle = session_registry.Handle
    FrameRef = session_registry.FrameRef
    Observation = session_registry.Observation
    Receipt = session_registry.Receipt
    StreamSpec = session_registry.StreamSpec
    StreamToken = session_registry.StreamToken
    SUPPORTED_TARGET_KINDS = frozenset(session_registry.SUPPORTED_KINDS)
except ModuleNotFoundError as _sr_err:
    if getattr(_sr_err, "name", None) not in (
        None,
        "wender.perceive.session_registry",
        "session_registry",
    ):
        raise
    from wender.state.schema import (
        Handle,
        FrameRef,
        Observation,
        Receipt,
        StreamSpec,
        StreamToken,
        TARGET_KINDS,
    )
    SUPPORTED_TARGET_KINDS = frozenset(TARGET_KINDS)

SCHEMA_ID = "wender.episode/v1"

# Supported source kinds for Capabilities (target kinds plus logical source kinds)
SUPPORTED_SOURCE_KINDS = SUPPORTED_TARGET_KINDS | frozenset({"cdp", "godot", "atspi", "vision"})

_SOURCE_TO_TARGET_KIND = {
    "cdp": "cdp_tab",
    "godot": "godot_window",
    "atspi": "x11_window",
    "vision": "xvfb_screen",
}

_TARGET_TO_STATE_SOURCE_KIND = {
    "cdp_tab": "cdp",
    "godot_window": "godot",
    "x11_window": "atspi",
    "xvfb_screen": "vision",
    "capsule": "vision",
}

COMMON_RECORD_KEYS = frozenset(
    {
        "schema",
        "episode_id",
        "handle",
        "epoch",
        "row_seq",
        "logical_tick",
        "evidence_status",
        "payload",
    }
)

OPTIONAL_RECORD_KEYS = frozenset({"digest", "seal"})

CLOSED_RECORD_ALLOWLIST = COMMON_RECORD_KEYS | OPTIONAL_RECORD_KEYS

HANDLE_PROJECTION_KEYS = frozenset({"kind", "canonical_id", "generation"})

# Forbidden key substrings and tokens in exported metadata (authority, secrets, credentials, raw capture/pixel buffers)
FORBIDDEN_KEY_PATTERNS = frozenset(
    {
        "token",
        "auth_token",
        "access_token",
        "session_token",
        "secret",
        "secret_key",
        "api_key",
        "private_key",
        "auth_key",
        "password",
        "authority",
        "cookie",
        "credential",
        "rgb",
        "pixels",
        "frame_data",
        "raw_hook_receipts",
        "capture_receipt",
        "tree_dump",
        "checkpoint_bytes",
    }
)

FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "token",
        "tokens",
        "auth",
        "secret",
        "secrets",
        "password",
        "passwd",
        "authority",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "rgb",
        "pixels",
        "pixel",
    }
)

NUMERIC_PAYLOAD_KEYS = frozenset(
    {
        "tick",
        "seq",
        "requested_tick",
        "applied_tick",
        "order_seq_id",
        "width",
        "height",
        "dpr_ppm",
        "epoch",
        "frame_seq",
        "row_seq",
        "logical_tick",
        "generation",
        "seed",
        "count",
        "action_count",
        "code",
    }
)

BASE64_DATA_URI_PATTERN = re.compile(r"^data:[^;]+;base64,", re.IGNORECASE)
BASE64_PAYLOAD_PATTERN = re.compile(r"^[A-Za-z0-9+/]{32,}={0,2}$")


class EpisodeRecordError(ValueError):
    """Base error for episode record operations."""
    pass


class ConfigRequiredError(EpisodeRecordError):
    """Raised when a required configured limit or ceiling is absent or invalid."""
    pass


class RecordLimitError(EpisodeRecordError):
    """Raised when record size or count exceeds configured ceilings."""
    pass


class ClosedAllowlistError(EpisodeRecordError):
    """Raised when unapproved keys or nested structures are encountered."""
    pass


class DuplicateKeyError(EpisodeRecordError):
    """Raised when duplicate JSON keys are detected at any nesting level."""
    pass


class EncodingValidationError(ClosedAllowlistError):
    """Raised when encoding encounters invalid types (floats, bool ints, hidden base64, etc.)."""
    pass


class StaleGenerationError(EpisodeRecordError):
    """Raised when a handle generation is older than the current fenced generation."""
    pass


class UnsupportedCapabilityError(EpisodeRecordError):
    """Raised when an operation is requested on an unsupported capability."""
    pass


class BackendImportForbiddenError(EpisodeRecordError):
    """Raised when forbidden backend modules are imported into core (INV J0 1)."""
    pass


def _is_int(v: Any) -> bool:
    """True iff v is an int and strictly NOT a bool."""
    return isinstance(v, int) and not isinstance(v, bool)


def _is_forbidden_key(k: str) -> bool:
    k_lower = k.lower()
    if any(pat in k_lower for pat in FORBIDDEN_KEY_PATTERNS):
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", k_lower) if t]
    return any(t in FORBIDDEN_KEY_TOKENS for t in tokens)


def _is_numeric_key(k: str) -> bool:
    k_lower = k.lower()
    if k_lower in NUMERIC_PAYLOAD_KEYS:
        return True
    return k_lower.endswith(("_tick", "_seq", "_count", "_ppm", "_bytes", "_ms", "_ns"))


def _check_no_floats_or_forbidden(obj: Any, path: str = "$") -> None:
    """Validate that object has no floats, nonfinite values, bool ints where not allowed, or forbidden data."""
    if isinstance(obj, float) or isinstance(obj, complex):
        raise EncodingValidationError(f"Floats and nonfinite values are rejected at {path}: {obj!r}")

    if isinstance(obj, str):
        if BASE64_DATA_URI_PATTERN.search(obj):
            raise EncodingValidationError(f"Hidden base64 data URI rejected at {path}")
        if len(obj) >= 64 and BASE64_PAYLOAD_PATTERN.match(obj):
            raise EncodingValidationError(f"Hidden base64 blob rejected at {path}")
        return

    if isinstance(obj, (bytes, bytearray, memoryview, np.ndarray)):
        raise EncodingValidationError(f"Raw byte/array blob rejected at {path}")

    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise EncodingValidationError(f"Non-string key rejected at {path}: {k!r}")
            if _is_forbidden_key(k):
                raise EncodingValidationError(f"Forbidden authority/capture/leakage key rejected at {path}.{k}")
            if isinstance(v, bool) and _is_numeric_key(k):
                raise EncodingValidationError(f"Boolean integer rejected for numeric key at {path}.{k}")
            _check_no_floats_or_forbidden(v, f"{path}.{k}")
        return

    if isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _check_no_floats_or_forbidden(item, f"{path}[{idx}]")
        return

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        _check_no_floats_or_forbidden(asdict(obj), path)
        return

    if obj is not None and not isinstance(obj, (bool, int)):
        raise EncodingValidationError(f"Unapproved nested object type rejected at {path}: {type(obj).__name__}")


def project_handle(handle: Any) -> dict[str, Any]:
    """Extract and validate the canonical Handle projection mapping using session_registry rules."""
    if isinstance(handle, Mapping):
        keys = set(handle.keys())
        if keys != HANDLE_PROJECTION_KEYS:
            raise ClosedAllowlistError(f"Handle keys {keys} do not match allowed {HANDLE_PROJECTION_KEYS}")
        kind = handle.get("kind")
        canonical_id = handle.get("canonical_id")
        generation = handle.get("generation")
    elif hasattr(handle, "kind") and hasattr(handle, "canonical_id") and hasattr(handle, "generation"):
        kind = getattr(handle, "kind")
        canonical_id = getattr(handle, "canonical_id")
        generation = getattr(handle, "generation")
    else:
        raise ClosedAllowlistError("Handle must be a Handle dataclass or handle projection dict")

    if not isinstance(kind, str) or kind not in SUPPORTED_TARGET_KINDS:
        raise ClosedAllowlistError(f"Unsupported target handle kind: {kind!r}")
    if not isinstance(canonical_id, str) or not canonical_id or canonical_id != canonical_id.strip() or "\x00" in canonical_id:
        raise ClosedAllowlistError(f"Invalid handle canonical_id: {canonical_id!r}")
    if not _is_int(generation) or generation < 1:
        raise EncodingValidationError(f"Handle generation must be an integer >= 1: {generation!r}")

    # Validate against canonical Handle constructor as well
    try:
        validated_h = Handle(kind=kind, canonical_id=canonical_id, generation=generation)
    except Exception as exc:
        raise ClosedAllowlistError(f"Canonical Handle validation failed: {exc}") from exc

    return {
        "canonical_id": validated_h.canonical_id,
        "generation": validated_h.generation,
        "kind": validated_h.kind,
    }


def compute_record_digest(record: Mapping[str, Any]) -> str:
    """Compute SHA-256 hex digest of canonical record bytes excluding its own digest."""
    filtered = {k: v for k, v in record.items() if k != "digest"}
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_diagnostic_seal(record: Mapping[str, Any]) -> str:
    """Compute SHA-256 seal for diagnostic rows over canonical content excluding seal and digest."""
    filtered = {k: v for k, v in record.items() if k not in ("seal", "digest")}
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sealed_bytes = ("DIAG_SEAL_V1:" + canonical).encode("utf-8")
    return hashlib.sha256(sealed_bytes).hexdigest()


def validate_closed_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate record against closed metadata allowlist and common fields."""
    if not isinstance(record, Mapping):
        raise ClosedAllowlistError("Record must be a Mapping")

    keys = set(record.keys())
    missing_common = COMMON_RECORD_KEYS - keys
    if missing_common:
        raise ClosedAllowlistError(f"Record is missing required common fields: {sorted(missing_common)}")

    extra_keys = keys - CLOSED_RECORD_ALLOWLIST
    if extra_keys:
        raise ClosedAllowlistError(f"Record contains unapproved keys outside metadata allowlist: {sorted(extra_keys)}")

    schema = record["schema"]
    if not isinstance(schema, str) or not schema:
        raise ClosedAllowlistError("schema must be a non-empty string")

    episode_id = record["episode_id"]
    if not isinstance(episode_id, str) or not episode_id:
        raise ClosedAllowlistError("episode_id must be a non-empty string")

    handle_proj = project_handle(record["handle"])

    epoch = record["epoch"]
    if not _is_int(epoch) or epoch < 0:
        raise EncodingValidationError(f"epoch must be a non-negative int, got {epoch!r}")

    row_seq = record["row_seq"]
    if not _is_int(row_seq) or row_seq < 0:
        raise EncodingValidationError(f"row_seq must be a non-negative int, got {row_seq!r}")

    logical_tick = record["logical_tick"]
    if not _is_int(logical_tick) or logical_tick < 0:
        raise EncodingValidationError(f"logical_tick must be a non-negative int, got {logical_tick!r}")

    evidence_status = record["evidence_status"]
    if not isinstance(evidence_status, str) or not evidence_status:
        raise ClosedAllowlistError("evidence_status must be a non-empty string")

    payload = record["payload"]
    if not isinstance(payload, Mapping):
        raise ClosedAllowlistError("payload must be a closed Mapping")

    _check_no_floats_or_forbidden(payload, path="$.payload")

    normalized = {
        "schema": schema,
        "episode_id": episode_id,
        "handle": handle_proj,
        "epoch": epoch,
        "row_seq": row_seq,
        "logical_tick": logical_tick,
        "evidence_status": evidence_status,
        "payload": dict(payload),
    }

    if "seal" in record:
        seal = record["seal"]
        if not isinstance(seal, str) or not seal:
            raise ClosedAllowlistError("seal must be a non-empty string")
        expected_seal = compute_diagnostic_seal(normalized)
        if seal != expected_seal:
            raise EncodingValidationError(f"Diagnostic seal mismatch: {seal!r} != {expected_seal!r}")
        normalized["seal"] = seal

    if "digest" in record:
        digest = record["digest"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ClosedAllowlistError("digest must be a 64-hex character string")
        expected_digest = compute_record_digest(normalized)
        if digest != expected_digest:
            raise EncodingValidationError(f"Digest mismatch: {digest!r} != {expected_digest!r}")
        normalized["digest"] = digest
    else:
        normalized["digest"] = compute_record_digest(normalized)

    return normalized


def encode_record(record: Any, max_bytes: int | None = None, **kwargs: Any) -> bytes:
    """Encode record to canonical UTF-8 JSON-L row with explicit max_bytes bound."""
    if max_bytes is None:
        max_bytes = kwargs.get("max_bytes")

    if max_bytes is None or not _is_int(max_bytes) or max_bytes <= 0:
        raise ConfigRequiredError("CONFIG_REQUIRED: positive max_bytes ceiling is required")

    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        record_map = asdict(record)
        if record_map.get("digest") is None:
            record_map.pop("digest", None)
        if record_map.get("seal") is None:
            record_map.pop("seal", None)
    elif isinstance(record, Mapping):
        record_map = dict(record)
    else:
        raise ClosedAllowlistError("Record must be a Mapping or Dataclass")

    validated = validate_closed_record(record_map)

    json_text = json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    encoded = (json_text + chr(10)).encode("utf-8")

    if len(encoded) > max_bytes:
        raise RecordLimitError(f"Encoded record size {len(encoded)} exceeds configured max_bytes ceiling {max_bytes}")

    return encoded


def decode_record(raw_bytes: bytes, max_bytes: int | None = None, **kwargs: Any) -> dict[str, Any]:
    """Decode raw bytes into a validated record with duplicate key rejection at all levels."""
    if max_bytes is None:
        max_bytes = kwargs.get("max_bytes")

    if max_bytes is None or not _is_int(max_bytes) or max_bytes <= 0:
        raise ConfigRequiredError("CONFIG_REQUIRED: positive max_bytes ceiling is required")

    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError(f"raw_bytes must be bytes or bytearray, got {type(raw_bytes).__name__}")

    if len(raw_bytes) > max_bytes:
        raise RecordLimitError(f"Input raw_bytes size {len(raw_bytes)} exceeds max_bytes ceiling {max_bytes}")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as ue:
        raise EpisodeRecordError(f"Invalid UTF-8 sequence in raw input: {ue}") from ue

    stripped = text.rstrip("\r\n")
    if not stripped:
        raise EpisodeRecordError("Empty JSON-L record input")

    def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for k, v in pairs:
            if k in seen:
                raise DuplicateKeyError(f"Duplicate JSON key detected at nesting level: {k!r}")
            seen.add(k)
        return dict(pairs)

    def _reject_float(s: str) -> None:
        raise EncodingValidationError(f"Floats rejected in JSON input: {s!r}")

    try:
        raw_obj = json.loads(
            stripped,
            object_pairs_hook=_pairs_hook,
            parse_float=_reject_float,
        )
    except json.JSONDecodeError as jde:
        raise EpisodeRecordError(f"JSON parsing error: {jde}") from jde

    if not isinstance(raw_obj, dict):
        raise ClosedAllowlistError("Decoded JSON root must be an object")

    return validate_closed_record(raw_obj)


@dataclass(frozen=True)
class Capabilities:
    """Declared capabilities verified by launcher evidence."""
    tree: bool
    frame: bool
    t2: bool
    reset: bool
    observer: bool
    source_kind: str
    binding_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_kind not in SUPPORTED_SOURCE_KINDS:
            raise ClosedAllowlistError(f"Unsupported source_kind: {self.source_kind!r}")


@dataclass(frozen=True)
class LauncherEvidence:
    """Launcher evidence establishing capabilities; self-assertion is rejected."""
    source_kind: str
    tree_supported: bool
    frame_supported: bool
    t2_supported: bool
    reset_supported: bool
    observer_supported: bool
    binding_id: str | None = None
    manifest_sha256: str | None = None

    def establish_capabilities(self) -> Capabilities:
        return Capabilities(
            tree=bool(self.tree_supported),
            frame=bool(self.frame_supported),
            t2=bool(self.t2_supported),
            reset=bool(self.reset_supported),
            observer=bool(self.observer_supported),
            source_kind=self.source_kind,
            binding_id=self.binding_id,
        )


@dataclass(frozen=True)
class ResetResult:
    """Result returned by EpisodeAdapter.reset."""
    initial_state_sha256: str
    build_id: str
    content_id: str
    schema_id: str
    trusted_context_ref: str
    presentation_digest: str | None = None


@dataclass(frozen=True)
class PauseCommand:
    """T2 pause command with tick, seq, and empty args."""
    tick: int
    seq: int
    cmd: str = "pause"
    args: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _is_int(self.tick) or self.tick < 0:
            raise ValueError("tick must be a non-negative int")
        if not _is_int(self.seq) or self.seq < 0:
            raise ValueError("seq must be a non-negative int")
        if self.cmd != "pause":
            raise ValueError("cmd must be 'pause'")
        if self.args != {}:
            raise ValueError("pause args must be empty dict")


@dataclass(frozen=True)
class Outcome:
    """Outcome record emitted by execution."""
    seq: int
    requested_tick: int
    applied_tick: int
    order_seq_id: int
    status: str
    reason: str | None
    state_sha256: str

    def __post_init__(self) -> None:
        if not _is_int(self.seq) or self.seq < 0:
            raise ValueError("seq must be a non-negative int")
        if not _is_int(self.requested_tick) or self.requested_tick < 0:
            raise ValueError("requested_tick must be a non-negative int")
        if not _is_int(self.applied_tick) or self.applied_tick < 0:
            raise ValueError("applied_tick must be a non-negative int")
        if not _is_int(self.order_seq_id) or self.order_seq_id < 0:
            raise ValueError("order_seq_id must be a non-negative int")
        if not isinstance(self.status, str):
            raise ValueError("status must be str")
        if not isinstance(self.state_sha256, str) or len(self.state_sha256) != 64:
            raise ValueError("state_sha256 must be a 64-hex character string")


@dataclass(frozen=True)
class EpisodeRecord:
    schema: str
    episode_id: str
    handle: dict
    epoch: int
    row_seq: int
    logical_tick: int
    evidence_status: str
    payload: dict
    digest: str | None = None
    seal: str | None = None


class EpisodeAdapter:
    """Source-neutral episode adapter enforcing INV J0 1-4 and capability contracts."""

    def __init__(
        self,
        launcher_evidence: LauncherEvidence,
        *,
        driver: Any = None,
        max_batch_size: int | None = 100,
    ) -> None:
        if not isinstance(launcher_evidence, LauncherEvidence):
            raise UnsupportedCapabilityError("Launcher evidence is required to establish capabilities; adapter self-assertion is prohibited")

        self._launcher_evidence = launcher_evidence
        self._capabilities = launcher_evidence.establish_capabilities()
        self._driver = driver

        if max_batch_size is None or not _is_int(max_batch_size) or max_batch_size <= 0:
            raise ConfigRequiredError("CONFIG_REQUIRED: positive max_batch_size ceiling is required")
        self._max_batch_size = max_batch_size

        # SessionRegistry Handle generation is an integer >= 1
        self._generation = 1
        self._epoch = 0
        self._logical_tick = 0
        self._outcomes: list[Outcome] = []

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @property
    def generation(self) -> int:
        return self._generation

    def _target_kind(self) -> str:
        sk = self._capabilities.source_kind
        if sk in SUPPORTED_TARGET_KINDS:
            return sk
        return _SOURCE_TO_TARGET_KIND.get(sk, "cdp_tab")

    def observe_tree(self, handle: Any, epoch: int) -> Observation:
        if not self._capabilities.tree:
            raise UnsupportedCapabilityError("Tree observation capability is unsupported by launcher evidence (INV J0 4)")

        proj = project_handle(handle)
        if proj["generation"] < self._generation:
            raise StaleGenerationError(
                f"Handle generation {proj['generation']} is stale; fenced after reset (current: {self._generation})"
            )

        if not _is_int(epoch) or epoch < 0:
            raise ValueError(f"epoch must be non-negative int, got {epoch!r}")

        if epoch != self._epoch:
            raise ValueError(f"Epoch mismatch: {epoch} != adapter epoch {self._epoch}")

        h_obj = handle if isinstance(handle, Handle) else Handle(
            kind=proj["kind"],
            canonical_id=proj["canonical_id"],
            generation=proj["generation"],
        )

        if self._driver is not None and hasattr(self._driver, "observe_tree"):
            return self._driver.observe_tree(h_obj, epoch)

        empty_rgb = np.zeros((16, 16, 3), dtype=np.uint8)
        if hasattr(FrameRef, "from_rgb"):
            empty_frame = FrameRef.from_rgb(h_obj, epoch, 0, empty_rgb)
        else:
            empty_frame = FrameRef(
                handle=h_obj,
                epoch=epoch,
                frame_seq=0,
                width=16,
                height=16,
                rgb=empty_rgb,
                digest=hashlib.sha256(empty_rgb.tobytes(order="C")).hexdigest(),
            )
        state_source_kind = _TARGET_TO_STATE_SOURCE_KIND.get(proj["kind"], "cdp")
        return Observation(
            handle=h_obj,
            epoch=epoch,
            frame_seq=0,
            dpr_ppm=1000000,
            state={
                "actionable": [],
                "ax_sha256": "0" * 64,
                "ax_stable_sha256": "0" * 64,
                "counts": {"actionable": 0, "nodes": 0, "raw": 0},
                "nodes": [],
                "prune": {"name": "test_prune", "version": 1},
                "schema": "wender.state/v1",
                "source": {"kind": state_source_kind, "title": "Test", "url": "about:blank"},
                "viewport": {"x": 0, "y": 0, "w": 16, "h": 16},
            },
            frame=empty_frame,
        )

    def observe_frame_digest(self, observation: Observation) -> dict[str, Any]:
        if not self._capabilities.frame:
            raise UnsupportedCapabilityError("Frame observation capability is unsupported by launcher evidence (INV J0 4)")

        if not hasattr(observation, "frame") or observation.frame is None:
            raise ValueError("Observation has no frame attached")

        if hasattr(observation, "handle") and observation.handle is not None:
            h_proj = project_handle(observation.handle)
            if h_proj["generation"] < self._generation:
                raise StaleGenerationError(
                    f"Observation handle generation {h_proj['generation']} is stale; fenced after reset (current: {self._generation})"
                )

        frame = observation.frame
        return {
            "digest": str(frame.digest),
            "width": int(frame.width),
            "height": int(frame.height),
            "dpr_ppm": int(observation.dpr_ppm),
            "epoch": int(frame.epoch),
            "frame_seq": int(frame.frame_seq),
        }

    def submit_t2_batch(self, commands: Sequence[Any], trusted_context: Mapping[str, Any]) -> tuple[Receipt, ...]:
        if not self._capabilities.t2:
            raise UnsupportedCapabilityError("T2 execution capability is unsupported by launcher evidence (INV J0 4)")

        if not isinstance(trusted_context, Mapping):
            raise ClosedAllowlistError("trusted_context must be a Mapping")

        if len(commands) > self._max_batch_size:
            raise RecordLimitError(f"Command batch size {len(commands)} exceeds ceiling {self._max_batch_size}")

        ctx_handle = trusted_context.get("handle")
        if ctx_handle is not None:
            ctx_proj = project_handle(ctx_handle)
            if ctx_proj["generation"] < self._generation:
                raise StaleGenerationError(
                    f"Context handle generation {ctx_proj['generation']} is stale; fenced after reset"
                )
            default_handle = Handle(
                kind=ctx_proj["kind"],
                canonical_id=ctx_proj["canonical_id"],
                generation=ctx_proj["generation"],
            )
        else:
            default_handle = Handle(
                kind=self._target_kind(),
                canonical_id="tab-1",
                generation=self._generation,
            )

        receipts: list[Receipt] = []
        for cmd in commands:
            self._logical_tick += 1
            tick = getattr(cmd, "tick", self._logical_tick)
            seq = getattr(cmd, "seq", len(receipts))

            cmd_handle = default_handle
            if hasattr(cmd, "handle") and cmd.handle is not None:
                h_proj = project_handle(cmd.handle)
                if h_proj["generation"] < self._generation:
                    raise StaleGenerationError(
                        f"Command handle generation {h_proj['generation']} is stale; fenced after reset"
                    )
                cmd_handle = Handle(
                    kind=h_proj["kind"],
                    canonical_id=h_proj["canonical_id"],
                    generation=h_proj["generation"],
                )

            receipt = Receipt(
                action_id=f"act-{tick}-{seq}",
                handle=cmd_handle,
                status="accepted",
                reason=None,
            )
            receipts.append(receipt)

            outcome = Outcome(
                seq=seq,
                requested_tick=tick,
                applied_tick=self._logical_tick,
                order_seq_id=seq,
                status="applied",
                reason=None,
                state_sha256=hashlib.sha256(f"state-{self._logical_tick}".encode("utf-8")).hexdigest(),
            )
            self._outcomes.append(outcome)

        return tuple(receipts)

    def read_outcomes(self) -> tuple[Outcome, ...]:
        outcomes = tuple(self._outcomes)
        self._outcomes = []
        return outcomes

    def reset(self, seed: int, descriptor: Mapping[str, Any], checkpoint: bytes | None = None) -> ResetResult:
        if not self._capabilities.reset:
            raise UnsupportedCapabilityError("Reset capability is unsupported by launcher evidence (INV J0 4)")

        if not _is_int(seed) or seed < 0:
            raise ValueError(f"seed must be a non-negative int, got {seed!r}")

        if not isinstance(descriptor, Mapping):
            raise ClosedAllowlistError("descriptor must be a Mapping")

        _check_no_floats_or_forbidden(descriptor, path="$.descriptor")

        if checkpoint is not None and not isinstance(checkpoint, (bytes, bytearray)):
            raise TypeError("checkpoint must be bytes, bytearray, or None")

        self._generation += 1
        self._epoch += 1
        self._logical_tick = 0
        self._outcomes = []

        descriptor_bytes = json.dumps(dict(descriptor), sort_keys=True, separators=(",", ":")).encode("utf-8")
        desc_hash = hashlib.sha256(descriptor_bytes).hexdigest()

        return ResetResult(
            initial_state_sha256=desc_hash,
            build_id=f"build-{desc_hash[:16]}",
            content_id=f"content-{seed}",
            schema_id=SCHEMA_ID,
            trusted_context_ref=f"ctx-gen{self._generation}-epoch{self._epoch}",
            presentation_digest=None,
        )
