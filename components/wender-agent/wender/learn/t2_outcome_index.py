# @module {"id":"wa.learn.t2_outcome_index","layer":2,"owner":"LEARN","deps":["wa.state.schema"],"book":"HB-WA-s4-learn"}
"""@module wa.learn.t2_outcome_index - bounded episode attempt and engine outcome index.

Production owner for jq2b-j1-t2-outcome-index.
Implements bounded OutcomeIndex.add with engine outcome correlation and authenticated
stream reference verification, following invariants INV J1 1 through INV J1 4:
  - INV J1 1: Local admission is distinct from engine acceptance. External command
    envelope contains strictly tick, seq, cmd, args. Trusted principal, company,
    role, mandate, session, and ticket remain outside that envelope.
  - INV J1 2: Canonical attempt and engine outcome records with correlated authenticated
    stream, ticket, sequence, requested tick, and actual post-commit hash binding.
  - INV J1 3: Local refusal emits nothing and burns no sequence; an identified engine
    rejection consumes trusted bookkeeping without mutating simulation.
  - INV J1 4: A missing outcome remains PENDING, and timeout transitions to UNKNOWN;
    conflicting duplicates fail without partial mutation or eviction of unmatched live
    entries; identical authenticated duplicates are idempotent. Never repair identity
    by arrival order or substitute a pre-action basis hash for a post-commit hash.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from ..state import schema

# Attempt phases per INV J1 2
PHASE_REFUSED = "REFUSED"
PHASE_PROPOSED = "PROPOSED"
PHASE_PENDING = "PENDING"
PHASE_ACCEPTED = "ACCEPTED"
PHASE_REJECTED = "REJECTED"
PHASE_UNKNOWN = "UNKNOWN"
PHASES = frozenset({
    PHASE_REFUSED,
    PHASE_PROPOSED,
    PHASE_PENDING,
    PHASE_ACCEPTED,
    PHASE_REJECTED,
    PHASE_UNKNOWN,
})

# Command envelope keys strictly per INV J1 1
COMMAND_FIELDS = ("tick", "seq", "cmd", "args")
COMMAND_FIELD_SET = frozenset(COMMAND_FIELDS)
FORBIDDEN_COMMAND_FIELDS = frozenset({
    "principal", "company", "role", "mandate", "session", "ticket",
    "actor", "channel", "slot", "consent", "order_seq_id",
})

# Engine outcome fields per INV J1 2
ENGINE_OUTCOME_FIELDS = (
    "seq", "requested_tick", "applied_tick", "order_seq_id", "status", "reason", "state_sha256"
)
ENGINE_OUTCOME_FIELD_SET = frozenset(ENGINE_OUTCOME_FIELDS)
VALID_OUTCOME_STATUSES = frozenset({"accepted", "rejected", "proposed", "unknown"})

# Attempt fields per INV J1 2
ATTEMPT_FIELDS = (
    "attempt_id", "action_id", "stream_ref", "source_ticket", "command",
    "local_status", "local_reason", "emitted_sha256", "engine_outcome",
    "phase", "committed_state_sha256"
)
ATTEMPT_FIELD_SET = frozenset(ATTEMPT_FIELDS)

SUPPORTED_TARGET_KINDS = frozenset(schema.TARGET_KINDS)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_UNICODE_LINE_SEPS = ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


class OutcomeIndexError(ValueError):
    """Base exception for all OutcomeIndex operations."""
    pass


class CanonicalFramingError(OutcomeIndexError):
    """Raised when record framing violates canonical rules (floats, duplicate keys, etc.)."""
    pass


class ConflictingDuplicateError(OutcomeIndexError):
    """Raised when an attempt or outcome conflicts with an existing entry."""
    pass


class CapacityExceededError(OutcomeIndexError):
    """Raised when index capacity is reached and no completed entry can be evicted."""
    pass


class UnauthenticatedStreamError(OutcomeIndexError):
    """Raised when stream_ref is missing or fails authentication."""
    pass


class CorrelationError(OutcomeIndexError):
    """Raised when correlation between command and outcome fails."""
    pass


# ---------------------------------------------------------------------------
# Strict canonical JSON parsing & validation helpers
# ---------------------------------------------------------------------------

def _strict_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if not isinstance(k, str):
            raise CanonicalFramingError("JSON key must be string")
        if k in out:
            raise CanonicalFramingError(f"Duplicate JSON key: {k!r}")
        out[k] = v
    return out


def _reject_float(token: str) -> None:
    raise CanonicalFramingError(f"Float not permitted in canonical integer domain: {token!r}")


def _reject_constant(token: str) -> None:
    raise CanonicalFramingError(f"JSON constant not permitted: {token!r}")


def _ensure_no_float(obj: Any, path: str = "$") -> None:
    if isinstance(obj, float):
        raise CanonicalFramingError(f"Float value not permitted at {path}: {obj!r}")
    if isinstance(obj, str):
        if "\x00" in obj or "\n" in obj:
            raise CanonicalFramingError(f"Control character forbidden in canonical string at {path}")
        for sep in _UNICODE_LINE_SEPS:
            if sep in obj:
                raise CanonicalFramingError(f"Unicode line separator {sep!r} forbidden at {path}")
        return
    if isinstance(obj, (int, bytes, bool)) or obj is None:
        return
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalFramingError(f"Dict key at {path} must be str, got {type(k)!r}")
            _ensure_no_float(k, f"{path}.<key>")
            _ensure_no_float(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _ensure_no_float(item, f"{path}[{i}]")
        return
    if hasattr(obj, "__dataclass_fields__"):
        for fld in obj.__dataclass_fields__:
            _ensure_no_float(getattr(obj, fld), f"{path}.{fld}")
        return
    return


def _reject_forbidden_in_mapping(obj: Any, path: str = "$") -> None:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in FORBIDDEN_COMMAND_FIELDS:
                raise CanonicalFramingError(
                    f"Authority or engine field {k!r} forbidden in command envelope at {path}"
                )
            _reject_forbidden_in_mapping(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _reject_forbidden_in_mapping(item, f"{path}[{i}]")


# @anchor:wa.learn.t2_outcome_index.parse_canonical_json
def parse_canonical_json(raw: bytes | str) -> dict[str, Any]:
    """Parse UTF-8 JSON rejecting duplicate keys, floats, constants and non-LF linebreaks."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
        if b"\r" in raw_bytes or b"\x00" in raw_bytes:
            raise CanonicalFramingError("CR or NUL bytes forbidden in canonical JSON")
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CanonicalFramingError(f"Invalid UTF-8 in canonical JSON: {exc}") from exc
    elif isinstance(raw, str):
        if "\r" in raw or "\x00" in raw:
            raise CanonicalFramingError("CR or NUL characters forbidden in canonical JSON")
        try:
            raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CanonicalFramingError(f"Surrogate or invalid UTF-8 in canonical JSON: {exc}") from exc
        text = raw
    else:
        raise CanonicalFramingError("Expected bytes or str")

    for sep in _UNICODE_LINE_SEPS:
        if sep in text:
            raise CanonicalFramingError(f"Unicode line separator {sep!r} forbidden")

    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text:
        raise CanonicalFramingError("Multiple lines inside single canonical JSON record")

    text = text.strip(" \t")
    if not text:
        raise CanonicalFramingError("Empty JSON input")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_pairs_hook,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except CanonicalFramingError:
        raise
    except Exception as exc:
        raise CanonicalFramingError(f"Malformed JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise CanonicalFramingError("Canonical record must decode to a JSON object")

    _ensure_no_float(parsed)
    return parsed


def _to_canonical_json_obj(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    elif hasattr(obj, "__dataclass_fields__"):
        obj = {fld: _to_canonical_json_obj(getattr(obj, fld)) for fld in obj.__dataclass_fields__}
    if isinstance(obj, Mapping):
        return {str(k): _to_canonical_json_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_canonical_json_obj(x) for x in obj]
    return obj


# @anchor:wa.learn.t2_outcome_index.dumps_canonical
def dumps_canonical(obj: Any) -> str:
    """Serialize object to canonical deterministic sorted LF-only JSON string without floats."""
    _ensure_no_float(obj)
    canon = _to_canonical_json_obj(obj)
    return json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# @anchor:wa.learn.t2_outcome_index.compute_canonical_sha256
def compute_canonical_sha256(obj: Any) -> str:
    """Compute 64-hex SHA-256 digest of canonical serialized object."""
    return hashlib.sha256(dumps_canonical(obj).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Record Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """External command envelope strictly containing tick, seq, cmd, args."""
    tick: int
    seq: int
    cmd: str
    args: dict[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.tick, bool) or not isinstance(self.tick, int) or self.tick < 0:
            raise CanonicalFramingError("command.tick must be non-negative integer")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq <= 0:
            raise CanonicalFramingError("command.seq must be positive integer")
        if not isinstance(self.cmd, str) or not self.cmd.strip() or self.cmd != self.cmd.strip():
            raise CanonicalFramingError("command.cmd must be non-empty canonical string")
        if not isinstance(self.args, Mapping):
            raise CanonicalFramingError("command.args must be a dictionary")
        _ensure_no_float(self.cmd, "command.cmd")
        _ensure_no_float(self.args, "command.args")
        _reject_forbidden_in_mapping(self.args, "command.args")

    def __getitem__(self, item: str) -> Any:
        if item in COMMAND_FIELD_SET:
            return getattr(self, item)
        raise KeyError(item)

    def keys(self):
        return COMMAND_FIELDS

    def to_dict(self) -> dict[str, Any]:
        return {"tick": self.tick, "seq": self.seq, "cmd": self.cmd, "args": dict(self.args)}


@dataclass(frozen=True)
class EngineOutcome:
    """Authoritative engine outcome record matching Holdings outcome envelope."""
    seq: int
    requested_tick: int
    applied_tick: int
    order_seq_id: int
    status: str
    reason: str | None = None
    state_sha256: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq <= 0:
            raise CanonicalFramingError("outcome.seq must be positive integer")
        if isinstance(self.requested_tick, bool) or not isinstance(self.requested_tick, int) or self.requested_tick < 0:
            raise CanonicalFramingError("outcome.requested_tick must be non-negative integer")
        if isinstance(self.applied_tick, bool) or not isinstance(self.applied_tick, int) or self.applied_tick < 0:
            raise CanonicalFramingError("outcome.applied_tick must be non-negative integer")
        if isinstance(self.order_seq_id, bool) or not isinstance(self.order_seq_id, int) or self.order_seq_id <= 0:
            raise CanonicalFramingError("outcome.order_seq_id must be positive integer")
        if not isinstance(self.status, str) or not self.status or self.status.lower() not in VALID_OUTCOME_STATUSES:
            raise CanonicalFramingError(f"outcome.status must be one of {sorted(VALID_OUTCOME_STATUSES)}")
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason):
            raise CanonicalFramingError("outcome.reason must be None or non-empty string")
        if self.reason is not None:
            _ensure_no_float(self.reason, "outcome.reason")
        if not isinstance(self.state_sha256, str) or not _HEX64_RE.fullmatch(self.state_sha256):
            raise CanonicalFramingError("outcome.state_sha256 must be 64 lowercase hex characters")

    def __getitem__(self, item: str) -> Any:
        if item in ENGINE_OUTCOME_FIELD_SET:
            return getattr(self, item)
        raise KeyError(item)

    def keys(self):
        return ENGINE_OUTCOME_FIELDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "requested_tick": self.requested_tick,
            "applied_tick": self.applied_tick,
            "order_seq_id": self.order_seq_id,
            "status": self.status,
            "reason": self.reason,
            "state_sha256": self.state_sha256,
        }


@dataclass(frozen=True)
class Attempt:
    """Canonical attempt record per INV J1 2."""
    attempt_id: str
    action_id: str
    stream_ref: Any
    source_ticket: Any
    command: Command | dict[str, Any] | None
    local_status: str
    local_reason: str | None = None
    emitted_sha256: str | None = None
    engine_outcome: EngineOutcome | dict[str, Any] | None = None
    phase: str = PHASE_PENDING
    committed_state_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise CanonicalFramingError("attempt_id must be non-empty string")
        if not isinstance(self.action_id, str) or not self.action_id:
            raise CanonicalFramingError("action_id must be non-empty string")
        if self.stream_ref is None:
            raise UnauthenticatedStreamError("stream_ref must not be None")
        if self.source_ticket is None:
            raise CanonicalFramingError("source_ticket must not be None")
        if self.phase not in PHASES:
            raise CanonicalFramingError(f"invalid attempt phase: {self.phase!r}")
        if self.emitted_sha256 is not None:
            if not isinstance(self.emitted_sha256, str) or not _HEX64_RE.fullmatch(self.emitted_sha256):
                raise CanonicalFramingError("emitted_sha256 must be 64-hex string or None")
        if self.committed_state_sha256 is not None:
            if not isinstance(self.committed_state_sha256, str) or not _HEX64_RE.fullmatch(self.committed_state_sha256):
                raise CanonicalFramingError("committed_state_sha256 must be 64-hex string or None")

    @property
    def seq(self) -> int | None:
        if self.engine_outcome is not None:
            return self.engine_outcome["seq"] if isinstance(self.engine_outcome, Mapping) else self.engine_outcome.seq
        if self.command is not None:
            return self.command["seq"] if isinstance(self.command, Mapping) else self.command.seq
        return None

    @property
    def requested_tick(self) -> int | None:
        if self.engine_outcome is not None:
            return (
                self.engine_outcome["requested_tick"]
                if isinstance(self.engine_outcome, Mapping)
                else self.engine_outcome.requested_tick
            )
        if self.command is not None:
            return self.command["tick"] if isinstance(self.command, Mapping) else self.command.tick
        return None

    @property
    def applied_tick(self) -> int | None:
        if self.engine_outcome is not None:
            return (
                self.engine_outcome["applied_tick"]
                if isinstance(self.engine_outcome, Mapping)
                else self.engine_outcome.applied_tick
            )
        return None

    @property
    def order_seq_id(self) -> int | None:
        if self.engine_outcome is not None:
            return (
                self.engine_outcome["order_seq_id"]
                if isinstance(self.engine_outcome, Mapping)
                else self.engine_outcome.order_seq_id
            )
        return None

    @property
    def status(self) -> str:
        if self.engine_outcome is not None:
            return (
                self.engine_outcome["status"]
                if isinstance(self.engine_outcome, Mapping)
                else self.engine_outcome.status
            )
        return self.local_status

    @property
    def reason(self) -> str | None:
        if self.engine_outcome is not None:
            r = (
                self.engine_outcome.get("reason")
                if isinstance(self.engine_outcome, Mapping)
                else self.engine_outcome.reason
            )
            if r is not None:
                return r
        return self.local_reason

    @property
    def state_sha256(self) -> str | None:
        if self.committed_state_sha256 is not None:
            return self.committed_state_sha256
        if self.engine_outcome is not None:
            return (
                self.engine_outcome["state_sha256"]
                if isinstance(self.engine_outcome, Mapping)
                else self.engine_outcome.state_sha256
            )
        return None

    def __getitem__(self, item: str) -> Any:
        if item in ATTEMPT_FIELD_SET:
            return getattr(self, item)
        if item in ENGINE_OUTCOME_FIELD_SET:
            return getattr(self, item)
        raise KeyError(item)

    def keys(self):
        return ATTEMPT_FIELDS

    def to_dict(self) -> dict[str, Any]:
        cmd_dict = None
        if self.command is not None:
            cmd_dict = self.command.to_dict() if hasattr(self.command, "to_dict") else dict(self.command)
        outcome_dict = None
        if self.engine_outcome is not None:
            outcome_dict = (
                self.engine_outcome.to_dict()
                if hasattr(self.engine_outcome, "to_dict")
                else dict(self.engine_outcome)
            )
        st_ref = _to_canonical_json_obj(self.stream_ref)
        tk_ref = _to_canonical_json_obj(self.source_ticket)

        return {
            "attempt_id": self.attempt_id,
            "action_id": self.action_id,
            "stream_ref": st_ref,
            "source_ticket": tk_ref,
            "command": cmd_dict,
            "local_status": self.local_status,
            "local_reason": self.local_reason,
            "emitted_sha256": self.emitted_sha256,
            "engine_outcome": outcome_dict,
            "phase": self.phase,
            "committed_state_sha256": self.committed_state_sha256,
        }


# ---------------------------------------------------------------------------
# Public Validators
# ---------------------------------------------------------------------------

# @anchor:wa.learn.t2_outcome_index.validate_command
def validate_command(cmd: Any) -> Command:
    """Validate external command envelope: exact tick, seq, cmd, args; no authority fields."""
    if isinstance(cmd, Command):
        return cmd
    if isinstance(cmd, (str, bytes, bytearray, memoryview)):
        cmd = parse_canonical_json(cmd)
    if not isinstance(cmd, Mapping):
        raise CanonicalFramingError("Command must be a dictionary or Command instance")

    keys = set(cmd.keys())
    forbidden_present = keys & FORBIDDEN_COMMAND_FIELDS
    if forbidden_present:
        raise CanonicalFramingError(
            f"Authority fields forbidden in external command envelope: {sorted(forbidden_present)}"
        )
    if keys != COMMAND_FIELD_SET:
        raise CanonicalFramingError(
            f"Command envelope keys must be exactly {sorted(COMMAND_FIELD_SET)}, got {sorted(keys)}"
        )

    _ensure_no_float(cmd, "command")
    _reject_forbidden_in_mapping(cmd["args"], "command.args")
    return Command(
        tick=cmd["tick"],
        seq=cmd["seq"],
        cmd=cmd["cmd"],
        args=dict(cmd["args"]),
    )


# @anchor:wa.learn.t2_outcome_index.validate_outcome
def validate_outcome(outcome: Any) -> EngineOutcome:
    """Validate engine outcome record: exact Holdings fields, positive integers, 64-hex hash."""
    if isinstance(outcome, EngineOutcome):
        return outcome
    if isinstance(outcome, (str, bytes, bytearray, memoryview)):
        outcome = parse_canonical_json(outcome)
    if not isinstance(outcome, Mapping):
        raise CanonicalFramingError("Outcome must be a dictionary or EngineOutcome instance")

    keys = set(outcome.keys())
    if keys != ENGINE_OUTCOME_FIELD_SET:
        raise CanonicalFramingError(
            f"Outcome record keys must be exactly {sorted(ENGINE_OUTCOME_FIELD_SET)}, got {sorted(keys)}"
        )

    _ensure_no_float(outcome, "outcome")
    return EngineOutcome(
        seq=outcome["seq"],
        requested_tick=outcome["requested_tick"],
        applied_tick=outcome["applied_tick"],
        order_seq_id=outcome["order_seq_id"],
        status=outcome["status"],
        reason=outcome.get("reason"),
        state_sha256=outcome["state_sha256"],
    )


# @anchor:wa.learn.t2_outcome_index.validate_attempt
def validate_attempt(attempt: Any) -> Attempt:
    """Validate Attempt record or dictionary, ensuring canonical fields and sub-record constraints."""
    if isinstance(attempt, Attempt):
        return attempt
    if isinstance(attempt, (str, bytes, bytearray, memoryview)):
        attempt = parse_canonical_json(attempt)
    if not isinstance(attempt, Mapping):
        raise CanonicalFramingError("Attempt must be a dictionary or Attempt instance")

    keys = set(attempt.keys())
    if keys != ATTEMPT_FIELD_SET:
        raise CanonicalFramingError(
            f"Attempt record keys must be exactly {sorted(ATTEMPT_FIELD_SET)}, got {sorted(keys)}"
        )

    _ensure_no_float(attempt, "attempt")
    cmd = validate_command(attempt["command"])
    out = validate_outcome(attempt["engine_outcome"]) if attempt.get("engine_outcome") is not None else None

    return Attempt(
        attempt_id=attempt["attempt_id"],
        action_id=attempt["action_id"],
        stream_ref=attempt["stream_ref"],
        source_ticket=attempt["source_ticket"],
        command=cmd,
        local_status=attempt["local_status"],
        local_reason=attempt.get("local_reason"),
        emitted_sha256=attempt.get("emitted_sha256"),
        engine_outcome=out,
        phase=attempt.get("phase", PHASE_PENDING),
        committed_state_sha256=attempt.get("committed_state_sha256"),
    )


def _extract_handle_tuple(obj: Any) -> tuple[str, str, int] | None:
    """Extract (kind, canonical_id, generation) from a Handle or handle-bearing object/dict."""
    h = obj
    if hasattr(obj, "handle"):
        h = obj.handle
    elif isinstance(obj, Mapping) and "handle" in obj:
        h = obj["handle"]

    if hasattr(h, "kind") and hasattr(h, "canonical_id") and hasattr(h, "generation"):
        return (h.kind, h.canonical_id, h.generation)
    if isinstance(h, Mapping) and ("kind" in h or "canonical_id" in h or "generation" in h):
        return (h.get("kind"), h.get("canonical_id"), h.get("generation"))
    return None


def _validate_handle_fields(kind: Any, canonical_id: Any, generation: Any) -> None:
    if not isinstance(kind, str) or kind not in SUPPORTED_TARGET_KINDS:
        raise UnauthenticatedStreamError(f"unsupported target kind: {kind!r}")
    if (
        not isinstance(canonical_id, str)
        or not canonical_id
        or canonical_id != canonical_id.strip()
        or "\x00" in canonical_id
    ):
        raise UnauthenticatedStreamError("canonical_id must be a nonempty canonical string")
    if type(generation) is not int or generation < 1:
        raise UnauthenticatedStreamError("generation must be an integer >= 1")


def _canonical_stream_key(stream_ref: Any) -> Any:
    """Extract a stable, hashable correlation key for stream_ref including kind, id, generation, epoch."""
    if hasattr(stream_ref, "handle"):
        h = stream_ref.handle
        epoch = getattr(stream_ref, "epoch", 0)
        h_kind = getattr(h, "kind", "")
        h_id = getattr(h, "canonical_id", str(h))
        h_gen = getattr(h, "generation", 1)
        return ("stream_token", h_kind, h_id, h_gen, epoch)
    if hasattr(stream_ref, "canonical_id"):
        return (
            "handle",
            getattr(stream_ref, "kind", ""),
            stream_ref.canonical_id,
            getattr(stream_ref, "generation", 1),
        )
    if isinstance(stream_ref, (str, int, bytes)):
        return stream_ref
    if isinstance(stream_ref, Mapping):
        if "handle" in stream_ref:
            h = stream_ref["handle"]
            if isinstance(h, Mapping):
                h_kind = h.get("kind", "")
                h_id = h.get("canonical_id", "")
                h_gen = h.get("generation", 1)
            else:
                h_kind = getattr(h, "kind", "")
                h_id = getattr(h, "canonical_id", str(h))
                h_gen = getattr(h, "generation", 1)
            return ("stream_token", h_kind, h_id, h_gen, stream_ref.get("epoch", 0))
        if "canonical_id" in stream_ref:
            return (
                "handle",
                stream_ref.get("kind", ""),
                stream_ref["canonical_id"],
                stream_ref.get("generation", 1),
            )
        return dumps_canonical(stream_ref)
    return str(stream_ref)


def _canonical_ticket_key(source_ticket: Any) -> Any:
    """Extract a stable, hashable correlation key for source_ticket."""
    if hasattr(source_ticket, "action_id"):
        ht = _extract_handle_tuple(source_ticket)
        return (source_ticket.action_id, ht)
    if isinstance(source_ticket, (str, int, bytes)):
        return source_ticket
    if isinstance(source_ticket, Mapping) and "action_id" in source_ticket:
        ht = _extract_handle_tuple(source_ticket)
        return (source_ticket["action_id"], ht)
    return str(source_ticket)


def _records_equal(a: Any, b: Any) -> bool:
    """Deep canonical equality between attempts or sub-records."""
    return dumps_canonical(a) == dumps_canonical(b)


# ---------------------------------------------------------------------------
# OutcomeIndex
# ---------------------------------------------------------------------------

# @anchor:wa.learn.t2_outcome_index.outcome_index
class OutcomeIndex:
    """Bounded, authenticated outcome and attempt index with exact state hash binding.

    Configured with required explicit capacity and producer-complete boundaries.
    Conflicting duplicates refuse without partial mutation or eviction of unmatched
    live entries. Identical authenticated duplicates are idempotent.
    """

    def __init__(
        self,
        capacity: int,
        *,
        validator_boundary: Callable[[Any], Any] | Mapping[str, Callable] | None = None,
        stream_authenticator: Callable[[Any], bool] | Any | None = None,
        producer_complete_boundary: Callable[[Attempt], bool] | Any | None = None,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError(f"capacity must be a positive integer, got {capacity!r}")

        self._capacity: int = capacity
        self._validator_boundary = validator_boundary
        self._stream_authenticator = stream_authenticator
        self._producer_complete_boundary = producer_complete_boundary

        # Primary storage: attempt_id -> Attempt
        self._attempts: dict[str, Attempt] = {}
        # Correlation index: (stream_key, seq) -> attempt_id
        self._by_stream_seq: dict[tuple[Any, int], str] = {}
        # Order sequence index per stream: (stream_key, order_seq_id) -> attempt_id
        self._by_stream_order_seq: dict[tuple[Any, int], str] = {}
        # Track outcome-only entries that have not yet been matched to a command
        self._unmatched_outcomes: set[str] = set()
        # Explicitly marked producer-complete attempt IDs or stream keys
        self._completed_ids: set[str] = set()
        self._completed_streams: set[Any] = set()
        # Completed / producer-complete attempt order for bounded capacity FIFO eviction
        self._settled_queue: list[str] = []

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._attempts)

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, str) and key in self._attempts:
            return True
        if isinstance(key, tuple) and len(key) == 2:
            sk = _canonical_stream_key(key[0])
            return (sk, key[1]) in self._by_stream_seq
        return False

    def __iter__(self):
        return iter(self._attempts.values())

    def get(self, attempt_id_or_stream: Any, seq: int | None = None) -> Attempt | None:
        if seq is not None:
            return self.get_by_correlation(attempt_id_or_stream, seq)
        if isinstance(attempt_id_or_stream, tuple) and len(attempt_id_or_stream) == 2:
            return self.get_by_correlation(attempt_id_or_stream[0], attempt_id_or_stream[1])
        return self._attempts.get(attempt_id_or_stream)

    def get_by_correlation(self, stream_ref: Any, seq: int) -> Attempt | None:
        sk = _canonical_stream_key(stream_ref)
        aid = self._by_stream_seq.get((sk, seq))
        return self._attempts.get(aid) if aid is not None else None

    def mark_producer_complete(self, target: Any, seq: int | None = None) -> None:
        """Mark an attempt, (stream_ref, seq), or entire stream_ref as producer-complete."""
        if seq is not None:
            sk = _canonical_stream_key(target)
            aid = self._by_stream_seq.get((sk, seq))
            if aid is not None:
                self._completed_ids.add(aid)
                self._unmatched_outcomes.discard(aid)
                if aid not in self._settled_queue:
                    self._settled_queue.append(aid)
            return
        if isinstance(target, str) and target in self._attempts:
            self._completed_ids.add(target)
            self._unmatched_outcomes.discard(target)
            if target not in self._settled_queue:
                self._settled_queue.append(target)
            return
        sk = _canonical_stream_key(target)
        self._completed_streams.add(sk)
        for (entry_sk, _), aid in list(self._by_stream_seq.items()):
            if entry_sk == sk:
                self._completed_ids.add(aid)
                self._unmatched_outcomes.discard(aid)
                if aid not in self._settled_queue:
                    self._settled_queue.append(aid)

    def is_producer_complete(self, attempt: Attempt) -> bool:
        aid = attempt.attempt_id
        sk = _canonical_stream_key(attempt.stream_ref)
        if aid in self._completed_ids or sk in self._completed_streams:
            return True
        if self._producer_complete_boundary is not None:
            if callable(self._producer_complete_boundary):
                return bool(self._producer_complete_boundary(attempt))
            if isinstance(self._producer_complete_boundary, (set, frozenset, list, tuple, Mapping)):
                seq = attempt.seq
                return (
                    aid in self._producer_complete_boundary
                    or (attempt.stream_ref, seq) in self._producer_complete_boundary
                    or (sk, seq) in self._producer_complete_boundary
                    or attempt.stream_ref in self._producer_complete_boundary
                    or sk in self._producer_complete_boundary
                )
        if aid in self._unmatched_outcomes:
            return False
        return attempt.phase in (PHASE_ACCEPTED, PHASE_REJECTED, PHASE_REFUSED, PHASE_UNKNOWN)

    def _authenticate_stream(self, stream_ref: Any) -> None:
        if stream_ref is None:
            raise UnauthenticatedStreamError("stream_ref must not be None")

        # Validate Handle / StreamToken target kind and generation >= 1 when present
        ht = _extract_handle_tuple(stream_ref)
        if ht is not None:
            _validate_handle_fields(ht[0], ht[1], ht[2])

        if hasattr(stream_ref, "epoch"):
            ep = getattr(stream_ref, "epoch")
            if type(ep) is not int or ep < 0:
                raise UnauthenticatedStreamError("stream_ref.epoch must be a non-negative integer")
        elif isinstance(stream_ref, Mapping) and "epoch" in stream_ref:
            ep = stream_ref["epoch"]
            if type(ep) is not int or ep < 0:
                raise UnauthenticatedStreamError("stream_ref.epoch must be a non-negative integer")

        if self._stream_authenticator is not None:
            if hasattr(self._stream_authenticator, "resolve"):
                handle_obj = stream_ref.handle if hasattr(stream_ref, "handle") else stream_ref
                try:
                    record = self._stream_authenticator.resolve(handle_obj)
                except Exception as exc:
                    raise UnauthenticatedStreamError(f"Stream registry resolution failed: {exc}") from exc
                if getattr(record, "closed", False):
                    raise UnauthenticatedStreamError(f"Stream handle is closed: {handle_obj!r}")
                if hasattr(stream_ref, "epoch") and hasattr(record, "epoch"):
                    if record.epoch != stream_ref.epoch:
                        raise UnauthenticatedStreamError(
                            f"Stream epoch {stream_ref.epoch} does not match registry epoch {record.epoch}"
                        )
            elif callable(self._stream_authenticator):
                try:
                    authed = self._stream_authenticator(stream_ref)
                except Exception as exc:
                    raise UnauthenticatedStreamError(f"Stream authentication failed: {exc}") from exc
                if not authed:
                    raise UnauthenticatedStreamError(f"Stream authentication refused: {stream_ref!r}")
            elif isinstance(self._stream_authenticator, (set, frozenset, list, tuple, Mapping)):
                sk = _canonical_stream_key(stream_ref)
                if stream_ref not in self._stream_authenticator and sk not in self._stream_authenticator:
                    raise UnauthenticatedStreamError(f"Stream not in authenticated set: {stream_ref!r}")

    def _validate_ticket_against_stream(self, source_ticket: Any, stream_ref: Any) -> None:
        if source_ticket is None:
            return
        tk_ht = _extract_handle_tuple(source_ticket)
        st_ht = _extract_handle_tuple(stream_ref)
        if tk_ht is not None:
            _validate_handle_fields(tk_ht[0], tk_ht[1], tk_ht[2])
            if st_ht is not None and tk_ht != st_ht:
                raise CorrelationError(
                    f"source_ticket handle {tk_ht!r} does not match stream_ref handle {st_ht!r}"
                )

    def _validate_command_boundary(self, cmd: Any, *, has_outcome: bool = False) -> Command:
        if isinstance(self._validator_boundary, Mapping):
            val_fn = self._validator_boundary.get("validate_command") or self._validator_boundary.get("command")
            if callable(val_fn):
                res = val_fn(cmd)
                return validate_command(res if res is not None and res is not True else cmd)
        elif callable(self._validator_boundary) and not has_outcome:
            res = self._validator_boundary(cmd)
            return validate_command(res if res is not None and res is not True else cmd)
        return validate_command(cmd)

    def _validate_outcome_boundary(self, outcome: Any) -> EngineOutcome:
        if isinstance(self._validator_boundary, Mapping):
            val_fn = self._validator_boundary.get("validate_outcome") or self._validator_boundary.get("outcome")
            if callable(val_fn):
                res = val_fn(outcome)
                return validate_outcome(res if res is not None and res is not True else outcome)
        elif callable(self._validator_boundary):
            res = self._validator_boundary(outcome)
            return validate_outcome(res if res is not None and res is not True else outcome)
        return validate_outcome(outcome)

    def _compute_phase_and_hashes(
        self,
        command: Command | None,
        local_status: str,
        local_reason: str | None,
        outcome: EngineOutcome | None,
        explicit_phase: str | None,
        explicit_committed_sha: str | None,
    ) -> tuple[str, str | None, str | None, str | None]:
        """Compute (phase, emitted_sha256, committed_state_sha256, local_reason) per INV J1 1..4."""
        st_norm = local_status.lower() if isinstance(local_status, str) else ""

        # INV J1 3: Local refusal emits nothing, burns no sequence, has no outcome/post-commit hash
        if st_norm in ("refused", "rejected_local", "local_refusal") or explicit_phase == PHASE_REFUSED:
            phase = PHASE_REFUSED
            emitted_sha256 = None
            committed_state_sha256 = None
            return phase, emitted_sha256, committed_state_sha256, local_reason

        # Confirmation proposal
        if st_norm in ("proposed", "needs_confirm") or explicit_phase == PHASE_PROPOSED:
            phase = PHASE_PROPOSED
            emitted_sha256 = None
            committed_state_sha256 = None
            return phase, emitted_sha256, committed_state_sha256, local_reason

        # Emitted hash for admitted command
        emitted_sha256 = compute_canonical_sha256(command) if command is not None else None

        if outcome is None:
            phase = explicit_phase if explicit_phase in (PHASE_PENDING, PHASE_UNKNOWN) else PHASE_PENDING
            committed_state_sha256 = None
            return phase, emitted_sha256, committed_state_sha256, local_reason

        # Outcome is present: bind post-commit hash from outcome per INV J1 2 & INV J1 4
        # Never substitute pre-action basis hash for post-commit hash!
        out_st = outcome.status.lower()
        committed_state_sha256 = outcome.state_sha256

        if out_st == "accepted":
            phase = PHASE_ACCEPTED
        elif out_st == "rejected":
            phase = PHASE_REJECTED
            # Preserve exact engine rejection string
            if outcome.reason is not None and not local_reason:
                local_reason = outcome.reason
        elif out_st == "proposed":
            phase = PHASE_PROPOSED
        else:
            phase = PHASE_UNKNOWN

        if explicit_phase is not None and explicit_phase != phase:
            phase = explicit_phase

        return phase, emitted_sha256, committed_state_sha256, local_reason

    def _ensure_capacity_for_new_entry(self) -> None:
        if len(self._attempts) < self._capacity:
            return
        # Must evict one producer-complete entry; NEVER evict unmatched live entries!
        evicted = False
        idx = 0
        while idx < len(self._settled_queue):
            old_aid = self._settled_queue[idx]
            if old_aid not in self._attempts:
                self._settled_queue.pop(idx)
                continue
            if self.is_producer_complete(self._attempts[old_aid]):
                self._settled_queue.pop(idx)
                old_att = self._attempts.pop(old_aid)
                old_sk = _canonical_stream_key(old_att.stream_ref)
                old_seq = old_att.seq
                if old_seq is not None:
                    self._by_stream_seq.pop((old_sk, old_seq), None)
                if old_att.order_seq_id is not None:
                    self._by_stream_order_seq.pop((old_sk, old_att.order_seq_id), None)
                self._unmatched_outcomes.discard(old_aid)
                self._completed_ids.discard(old_aid)
                evicted = True
                break
            idx += 1

        if not evicted:
            # Also scan all entries in insertion order in case producer_complete_boundary became True dynamically
            for cand_aid, cand_att in list(self._attempts.items()):
                if self.is_producer_complete(cand_att):
                    old_att = self._attempts.pop(cand_aid)
                    if cand_aid in self._settled_queue:
                        self._settled_queue.remove(cand_aid)
                    old_sk = _canonical_stream_key(old_att.stream_ref)
                    old_seq = old_att.seq
                    if old_seq is not None:
                        self._by_stream_seq.pop((old_sk, old_seq), None)
                    if old_att.order_seq_id is not None:
                        self._by_stream_order_seq.pop((old_sk, old_att.order_seq_id), None)
                    self._unmatched_outcomes.discard(cand_aid)
                    self._completed_ids.discard(cand_aid)
                    evicted = True
                    break

        if not evicted:
            raise CapacityExceededError(
                f"Index capacity of {self._capacity} reached with all entries live; cannot evict unmatched live entries"
            )

    def add(
        self,
        attempt: Attempt | EngineOutcome | dict[str, Any] | str | bytes | None = None,
        stream_ref_pos: Any = None,
        *,
        outcome: EngineOutcome | dict[str, Any] | str | bytes | None = None,
        stream_ref: Any = None,
        attempt_id: str | None = None,
        action_id: str | None = None,
        source_ticket: Any = None,
        command: Command | dict[str, Any] | str | bytes | None = None,
        local_status: str | None = None,
        local_reason: str | None = None,
        emitted_sha256: str | None = None,
        engine_outcome: EngineOutcome | dict[str, Any] | str | bytes | None = None,
        phase: str | None = None,
        committed_state_sha256: str | None = None,
        producer_complete: bool = False,
    ) -> Attempt:
        """Add or correlate an attempt and outcome into the bounded index.

        Strict invariants:
          - Conflicting duplicates refuse without partial mutation or eviction of unmatched live entries.
          - Identical authenticated duplicates are idempotent.
          - Capacity bounding evicts only producer-complete entries, never live entries.
          - Exact rejection strings and post-commit state hashes are preserved.
        """
        if stream_ref is None and stream_ref_pos is not None:
            stream_ref = stream_ref_pos

        actual_outcome = outcome if outcome is not None else engine_outcome

        if attempt is not None:
            if isinstance(attempt, EngineOutcome):
                actual_outcome = attempt
                attempt = None
            elif isinstance(attempt, Attempt):
                attempt_id = attempt.attempt_id if attempt_id is None else attempt_id
                action_id = attempt.action_id if action_id is None else action_id
                stream_ref = attempt.stream_ref if stream_ref is None else stream_ref
                source_ticket = attempt.source_ticket if source_ticket is None else source_ticket
                command = attempt.command if command is None else command
                local_status = attempt.local_status if local_status is None else local_status
                local_reason = attempt.local_reason if local_reason is None else local_reason
                emitted_sha256 = attempt.emitted_sha256 if emitted_sha256 is None else emitted_sha256
                if actual_outcome is None:
                    actual_outcome = attempt.engine_outcome
                phase = attempt.phase if phase is None else phase
                committed_state_sha256 = (
                    attempt.committed_state_sha256
                    if committed_state_sha256 is None
                    else committed_state_sha256
                )
            else:
                if isinstance(attempt, (str, bytes, bytearray, memoryview)):
                    att_dict = parse_canonical_json(attempt)
                elif isinstance(attempt, Mapping):
                    _ensure_no_float(attempt, "attempt")
                    att_dict = dict(attempt)
                else:
                    raise CanonicalFramingError(f"Invalid attempt/outcome type: {type(attempt)!r}")

                # Check if positional dict is actually an outcome record rather than an attempt record
                if "attempt_id" not in att_dict and "command" not in att_dict and (
                    set(att_dict.keys()) == ENGINE_OUTCOME_FIELD_SET
                    or "order_seq_id" in att_dict
                    or "applied_tick" in att_dict
                    or "requested_tick" in att_dict
                    or "state_sha256" in att_dict
                ):
                    actual_outcome = att_dict
                else:
                    attempt_id = att_dict.get("attempt_id", attempt_id)
                    action_id = att_dict.get("action_id", action_id)
                    stream_ref = att_dict.get("stream_ref", stream_ref)
                    source_ticket = att_dict.get("source_ticket", source_ticket)
                    command = att_dict.get("command", command)
                    local_status = att_dict.get("local_status", local_status)
                    local_reason = att_dict.get("local_reason", local_reason)
                    emitted_sha256 = att_dict.get("emitted_sha256", emitted_sha256)
                    if actual_outcome is None:
                        actual_outcome = att_dict.get("engine_outcome")
                    phase = att_dict.get("phase", phase)
                    committed_state_sha256 = att_dict.get("committed_state_sha256", committed_state_sha256)

        # Case 1: Outcome-only addition or correlation into an existing attempt
        if command is None and actual_outcome is not None:
            self._authenticate_stream(stream_ref)
            self._validate_ticket_against_stream(source_ticket, stream_ref)
            validated_out = self._validate_outcome_boundary(actual_outcome)
            sk = _canonical_stream_key(stream_ref)
            corr_key = (sk, validated_out.seq)
            order_key = (sk, validated_out.order_seq_id)

            existing_aid = self._by_stream_seq.get(corr_key)
            if existing_aid is None and attempt_id is not None and attempt_id in self._attempts:
                existing_aid = attempt_id

            # Check order_seq_id collision in same stream with different seq
            if order_key in self._by_stream_order_seq:
                ord_aid = self._by_stream_order_seq[order_key]
                if existing_aid is not None and ord_aid != existing_aid:
                    raise ConflictingDuplicateError(
                        f"Conflicting order_seq_id={validated_out.order_seq_id} on stream {stream_ref!r}"
                    )
                if existing_aid is None:
                    raise ConflictingDuplicateError(
                        f"Conflicting order_seq_id={validated_out.order_seq_id} on stream {stream_ref!r}"
                    )

            if existing_aid is not None:
                existing_attempt = self._attempts[existing_aid]
                updated = self._attach_outcome_to_attempt(
                    existing_attempt, validated_out, producer_complete=producer_complete
                )
                return updated

            # No prior attempt exists: store outcome as an indexed entry
            synth_aid = attempt_id or f"outcome:{sk}:{validated_out.seq}"
            synth_act = action_id or (
                getattr(source_ticket, "action_id", None)
                if source_ticket is not None
                else f"action:{validated_out.seq}"
            ) or f"action:{validated_out.seq}"
            synth_ticket = source_ticket if source_ticket is not None else f"ticket:{sk}:{validated_out.seq}"

            calc_phase, _, calc_committed, calc_reason = self._compute_phase_and_hashes(
                None,
                validated_out.status,
                local_reason,
                validated_out,
                phase,
                committed_state_sha256,
            )

            candidate = Attempt(
                attempt_id=synth_aid,
                action_id=synth_act,
                stream_ref=stream_ref,
                source_ticket=synth_ticket,
                command=None,
                local_status=validated_out.status,
                local_reason=calc_reason,
                emitted_sha256=None,
                engine_outcome=validated_out,
                phase=calc_phase,
                committed_state_sha256=calc_committed,
            )

            self._ensure_capacity_for_new_entry()
            self._attempts[synth_aid] = candidate
            self._by_stream_seq[corr_key] = synth_aid
            self._by_stream_order_seq[order_key] = synth_aid
            if producer_complete:
                self._completed_ids.add(synth_aid)
            else:
                self._unmatched_outcomes.add(synth_aid)
            if self.is_producer_complete(candidate) and synth_aid not in self._settled_queue:
                self._settled_queue.append(synth_aid)
            return candidate

        # Case 2: Standard Attempt addition (with command and optional outcome)
        if attempt_id is None or not isinstance(attempt_id, str) or not attempt_id:
            raise CanonicalFramingError("attempt_id must be a non-empty string")
        if action_id is None or not isinstance(action_id, str) or not action_id:
            raise CanonicalFramingError("action_id must be a non-empty string")
        if source_ticket is None:
            raise CanonicalFramingError("source_ticket must be provided")

        self._authenticate_stream(stream_ref)
        self._validate_ticket_against_stream(source_ticket, stream_ref)
        validated_cmd = self._validate_command_boundary(command, has_outcome=actual_outcome is not None)
        validated_out = self._validate_outcome_boundary(actual_outcome) if actual_outcome is not None else None

        if local_status is None:
            local_status = "admitted"

        # Correlation checks per INV J1 2
        if validated_out is not None:
            if validated_out.seq != validated_cmd.seq:
                raise CorrelationError(
                    f"Outcome seq {validated_out.seq} does not match command seq {validated_cmd.seq}"
                )
            if validated_out.requested_tick != validated_cmd.tick:
                raise CorrelationError(
                    f"Outcome requested_tick {validated_out.requested_tick} does not match command tick {validated_cmd.tick}"
                )

        calc_phase, calc_emitted, calc_committed, calc_reason = self._compute_phase_and_hashes(
            validated_cmd, local_status, local_reason, validated_out, phase, committed_state_sha256
        )

        candidate = Attempt(
            attempt_id=attempt_id,
            action_id=action_id,
            stream_ref=stream_ref,
            source_ticket=source_ticket,
            command=validated_cmd,
            local_status=local_status,
            local_reason=calc_reason,
            emitted_sha256=calc_emitted,
            engine_outcome=validated_out,
            phase=calc_phase,
            committed_state_sha256=calc_committed,
        )

        sk = _canonical_stream_key(stream_ref)
        corr_key = (sk, validated_cmd.seq)

        # Check existing attempt by attempt_id
        if attempt_id in self._attempts:
            existing = self._attempts[attempt_id]
            if _records_equal(existing, candidate):
                # Idempotent duplicate: return without mutation
                return existing
            # If existing was PENDING and incoming candidate is the correlated outcome update
            if (
                existing.phase == PHASE_PENDING
                and existing.command is not None
                and validated_out is not None
                and candidate.phase in (PHASE_ACCEPTED, PHASE_REJECTED)
            ):
                if (
                    existing.action_id == candidate.action_id
                    and _canonical_stream_key(existing.stream_ref) == sk
                    and _canonical_ticket_key(existing.source_ticket) == _canonical_ticket_key(candidate.source_ticket)
                    and existing.seq == candidate.seq
                    and existing.requested_tick == candidate.requested_tick
                ):
                    return self._attach_outcome_to_attempt(
                        existing, validated_out, producer_complete=producer_complete
                    )
            # Otherwise, conflicting duplicate: refuse without partial mutation or eviction!
            raise ConflictingDuplicateError(
                f"Conflicting duplicate attempt for attempt_id={attempt_id!r}"
            )

        # Check existing entry by (stream, seq) correlation key
        # Note: per INV J1 3, a local refusal burns no sequence!
        is_local_refusal = candidate.phase == PHASE_REFUSED
        if not is_local_refusal and corr_key in self._by_stream_seq:
            existing_aid = self._by_stream_seq[corr_key]
            existing = self._attempts[existing_aid]
            if _records_equal(existing, candidate):
                return existing
            # If existing was an unmatched outcome-only entry at (stream, seq), correlate it!
            if existing_aid in self._unmatched_outcomes and existing.command is None and existing.engine_outcome is not None:
                prior_out = validate_outcome(existing.engine_outcome)
                if validated_out is not None and not _records_equal(prior_out, validated_out):
                    raise ConflictingDuplicateError(
                        f"Conflicting outcome for stream {stream_ref!r} sequence {validated_cmd.seq}"
                    )
                if prior_out.requested_tick != validated_cmd.tick:
                    raise CorrelationError(
                        f"Stored outcome requested_tick {prior_out.requested_tick} does not match command tick {validated_cmd.tick}"
                    )
                merged_phase, merged_emitted, merged_committed, merged_reason = self._compute_phase_and_hashes(
                    validated_cmd, local_status, local_reason, prior_out, None, None
                )
                merged = Attempt(
                    attempt_id=attempt_id,
                    action_id=action_id,
                    stream_ref=stream_ref,
                    source_ticket=source_ticket,
                    command=validated_cmd,
                    local_status=local_status,
                    local_reason=merged_reason,
                    emitted_sha256=merged_emitted,
                    engine_outcome=prior_out,
                    phase=merged_phase,
                    committed_state_sha256=merged_committed,
                )
                self._unmatched_outcomes.discard(existing_aid)
                self._attempts.pop(existing_aid, None)
                if existing_aid in self._settled_queue:
                    self._settled_queue.remove(existing_aid)
                self._attempts[attempt_id] = merged
                self._by_stream_seq[corr_key] = attempt_id
                self._by_stream_order_seq[(sk, prior_out.order_seq_id)] = attempt_id
                if producer_complete:
                    self._completed_ids.add(attempt_id)
                if self.is_producer_complete(merged) and attempt_id not in self._settled_queue:
                    self._settled_queue.append(attempt_id)
                return merged

            # Conflicting duplicate sequence in same stream
            raise ConflictingDuplicateError(
                f"Conflicting duplicate for stream {stream_ref!r} sequence {validated_cmd.seq}"
            )

        if validated_out is not None:
            order_key = (sk, validated_out.order_seq_id)
            if order_key in self._by_stream_order_seq:
                raise ConflictingDuplicateError(
                    f"Conflicting order_seq_id={validated_out.order_seq_id} for stream {stream_ref!r}"
                )

        # Check bounded capacity before mutation
        self._ensure_capacity_for_new_entry()

        # Store candidate cleanly
        self._attempts[attempt_id] = candidate
        if not is_local_refusal:
            self._by_stream_seq[corr_key] = attempt_id
        if validated_out is not None:
            self._by_stream_order_seq[(sk, validated_out.order_seq_id)] = attempt_id
        if producer_complete:
            self._completed_ids.add(attempt_id)
        if self.is_producer_complete(candidate) and attempt_id not in self._settled_queue:
            self._settled_queue.append(attempt_id)

        return candidate

    def _attach_outcome_to_attempt(
        self,
        existing: Attempt,
        outcome: EngineOutcome,
        *,
        producer_complete: bool = False,
    ) -> Attempt:
        """Attach engine outcome to an existing attempt or verify idempotency on existing outcome."""
        if existing.command is not None:
            cmd = existing.command
            cmd_seq = cmd.seq if hasattr(cmd, "seq") else cmd["seq"]
            cmd_tick = cmd.tick if hasattr(cmd, "tick") else cmd["tick"]

            if outcome.seq != cmd_seq:
                raise CorrelationError(
                    f"Outcome seq {outcome.seq} does not match attempt command seq {cmd_seq}"
                )
            if outcome.requested_tick != cmd_tick:
                raise CorrelationError(
                    f"Outcome requested_tick {outcome.requested_tick} does not match attempt command tick {cmd_tick}"
                )

        # If existing already had an outcome
        if existing.engine_outcome is not None:
            if _records_equal(existing.engine_outcome, outcome):
                if producer_complete:
                    self.mark_producer_complete(existing.attempt_id)
                return self._attempts[existing.attempt_id]
            raise ConflictingDuplicateError(
                f"Attempt {existing.attempt_id} already has a conflicting engine outcome"
            )

        sk = _canonical_stream_key(existing.stream_ref)
        order_key = (sk, outcome.order_seq_id)
        if order_key in self._by_stream_order_seq and self._by_stream_order_seq[order_key] != existing.attempt_id:
            raise ConflictingDuplicateError(
                f"Conflicting order_seq_id={outcome.order_seq_id} for stream {existing.stream_ref!r}"
            )

        calc_phase, calc_emitted, calc_committed, calc_reason = self._compute_phase_and_hashes(
            validate_command(existing.command) if existing.command is not None else None,
            existing.local_status,
            existing.local_reason,
            outcome,
            None,
            None,
        )

        updated = Attempt(
            attempt_id=existing.attempt_id,
            action_id=existing.action_id,
            stream_ref=existing.stream_ref,
            source_ticket=existing.source_ticket,
            command=existing.command,
            local_status=existing.local_status,
            local_reason=calc_reason,
            emitted_sha256=existing.emitted_sha256 or calc_emitted,
            engine_outcome=outcome,
            phase=calc_phase,
            committed_state_sha256=calc_committed,
        )

        self._attempts[existing.attempt_id] = updated
        self._by_stream_order_seq[order_key] = existing.attempt_id
        self._unmatched_outcomes.discard(existing.attempt_id)
        if producer_complete:
            self._completed_ids.add(existing.attempt_id)
        if self.is_producer_complete(updated) and existing.attempt_id not in self._settled_queue:
            self._settled_queue.append(existing.attempt_id)

        return updated

    def mark_timeout(self, attempt_id: str) -> Attempt:
        """Transition an unresolved pending attempt to UNKNOWN phase on timeout."""
        if attempt_id not in self._attempts:
            raise OutcomeIndexError(f"Attempt not found: {attempt_id!r}")
        existing = self._attempts[attempt_id]
        if existing.phase != PHASE_PENDING:
            return existing

        updated = Attempt(
            attempt_id=existing.attempt_id,
            action_id=existing.action_id,
            stream_ref=existing.stream_ref,
            source_ticket=existing.source_ticket,
            command=existing.command,
            local_status=existing.local_status,
            local_reason=existing.local_reason or "TIMEOUT",
            emitted_sha256=existing.emitted_sha256,
            engine_outcome=None,
            phase=PHASE_UNKNOWN,
            committed_state_sha256=None,
        )
        self._attempts[attempt_id] = updated
        if self.is_producer_complete(updated) and attempt_id not in self._settled_queue:
            self._settled_queue.append(attempt_id)
        return updated
