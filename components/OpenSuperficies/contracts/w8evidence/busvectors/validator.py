"""Strict Python stdlib validator and fakeable host port for the per-capsule stream boundary.

Bound sources (at ref bridge/integration):
- contracts/worker/os.cu.worker-task.v1.schema.json ($defs/speech_act, $defs/id, $defs/sha256, $defs/path)
- src/workerbridge/json.go (strict integer-only JSON, duplicate key refusal, lone surrogate refusal, depth <= 128)
- docs/design/W8_WORKER_BANK.md (§6 exit codes 0, 2, 3, 4, 5, 6, 7; code 1 never success or refusal)
- docs/design/w8d/W8_AMENDMENTS.md (A2 structured speech acts finding, repro, affected, status; A4 host-only authority)

Exit codes (CITED from docs/design/W8_WORKER_BANK.md §6 and contracts/worker/os.cu.worker-task.v1.schema.json):
- 0 CITED: verified success
- 2 CITED: invalid input, schema, or command/parser shape
- 3 CITED: execution or setup failure
- 4 CITED: unavailable prerequisite (e.g. missing native full-body capability; never a file-bus fallback)
- 5 CITED: gate or containment refusal (e.g. forged sender, stale/future epoch, unqualified repro, gap, STOP starvation)
- 6 CITED: indeterminate quarantine (e.g. changed replay, truncated or mutated body bytes)
- 7 CITED: operator authorization required (operator_recipe / operator_capability)
- 1 CITED: never success or refusal (mapped into contract codes at CLI boundary)
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union


# ---------------------------------------------------------------------------
# Protocol & Schema Constants
# ---------------------------------------------------------------------------

# Exit codes (CITED: docs/design/W8_WORKER_BANK.md §6, $defs/normalized_exit)
EXIT_SUCCESS: int = 0                   # CITED
EXIT_FORBIDDEN_ONE: int = 1             # CITED: code 1 is never success or refusal
EXIT_INPUT: int = 2                     # CITED
EXIT_EXECUTION_SETUP: int = 3           # CITED
EXIT_UNAVAILABLE_PREREQ: int = 4        # CITED
EXIT_GATE_CONTAINMENT: int = 5          # CITED
EXIT_INDETERMINATE_QUARANTINE: int = 6  # CITED
EXIT_OPERATOR: int = 7                  # CITED

ALLOWED_EXIT_CODES: frozenset[int] = frozenset(
    (
        EXIT_SUCCESS,
        EXIT_INPUT,
        EXIT_EXECUTION_SETUP,
        EXIT_UNAVAILABLE_PREREQ,
        EXIT_GATE_CONTAINMENT,
        EXIT_INDETERMINATE_QUARANTINE,
        EXIT_OPERATOR,
    )
)

# Frozen speech-act schema constants (CITED: contracts/worker/os.cu.worker-task.v1.schema.json#/$defs/speech_act)
SPEECH_ACT_SCHEMA: str = "os.cu.speech-act/v1"  # CITED
SPEECH_ACT_REQUIRED_KEYS: frozenset[str] = frozenset(
    (
        "schema",
        "act",
        "wave_id",
        "task_id",
        "attempt_id",
        "lease_id",
        "seq",
        "repro_sha256",
        "affected",
        "status",
        "body",
    )
)  # CITED

ALLOWED_ACTS: frozenset[str] = frozenset(("finding", "repro", "affected", "status"))  # CITED
ALLOWED_STATUS_VALUES: frozenset[str] = frozenset(("working", "blocked", "done", "stuck"))  # CITED

ID_MIN_LEN: int = 1          # CITED: $defs/id minLength
ID_MAX_LEN: int = 128        # CITED: $defs/id maxLength
SHA256_LEN: int = 64         # CITED: $defs/sha256 minLength/maxLength
SEQ_MIN: int = 0             # CITED: $defs/speech_act.properties.seq.minimum
SEQ_MAX: int = 1000000       # CITED: $defs/speech_act.properties.seq.maximum
AFFECTED_MIN_ITEMS: int = 0  # CITED: $defs/speech_act.properties.affected.minItems
AFFECTED_ACT_MIN_ITEMS: int = 1  # CITED: $defs/speech_act.allOf affected minItems
AFFECTED_MAX_ITEMS: int = 64     # CITED: $defs/speech_act.properties.affected.maxItems
PATH_MIN_LEN: int = 1        # CITED: $defs/path minLength
PATH_MAX_LEN: int = 1024     # CITED: $defs/path maxLength
BODY_MIN_LEN: int = 0        # CITED: $defs/speech_act.properties.body.minLength
BODY_MAX_LEN: int = 2048     # CITED: $defs/speech_act.properties.body.maxLength
MAX_JSON_DEPTH: int = 128    # CITED: src/workerbridge/json.go:110

# TARGET quantitative bounds for unit tests / local port
MAX_UNIX_SOCKET_PATH_BYTES: int = 100  # TARGET
DEFAULT_DATA_SLOTS: int = 4            # TARGET
DEFAULT_CONTROL_SLOTS: int = 2         # TARGET
DEFAULT_MAX_STOP_DELAY_MS: int = 2000  # TARGET
DEFAULT_MAX_SENDER_BYTES: int = 65536  # TARGET

# Unmerged reserved packages are AUSENTE
LIVE_SWITCHBOARD_PACKAGE: str = "AUSENTE"

# Sidecar transport envelope keys (separate from frozen speech payload)
SIDECAR_REQUIRED_KEYS: frozenset[str] = frozenset(
    (
        "sender_extension",
        "wave_id",
        "task_id",
        "attempt_id",
        "lease_id",
        "epoch",
        "seq",
        "body_sha256",
        "body_length",
    )
)
SIDECAR_OPTIONAL_KEYS: frozenset[str] = frozenset(
    (
        "origin",
        "guest_named_extension",
        "direct_switchboard_connect",
        "is_preview",
        "file_bus_fallback",
    )
)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", re.ASCII)  # CITED: $defs/id
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)            # CITED: $defs/sha256
PATH_RE = re.compile(
    r"^(?!(?:.*/)?\.git(?:/|$))(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
    re.ASCII,
)  # CITED: $defs/path
INTEGER_TOKEN_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$", re.ASCII)  # CITED: src/workerbridge/json.go:47


class BoundaryRefusal(Exception):
    """Structured contract refusal carrying a normalized W8 exit code (never 0 or 1)."""

    def __init__(self, code: int, class_name: str, detail: str) -> None:
        if code not in ALLOWED_EXIT_CODES or code == EXIT_SUCCESS:
            raise ValueError(f"Invalid refusal exit code: {code}")
        super().__init__(f"{class_name}: {detail}")
        self.code = code
        self.class_name = class_name
        self.detail = detail


def normalize_exit_code(err: Optional[BaseException]) -> int:
    """Maps None to 0 and exceptions to normalized contract codes (2..7, never 1)."""
    if err is None:
        return EXIT_SUCCESS
    if isinstance(err, BoundaryRefusal):
        return err.code
    if isinstance(err, KeyboardInterrupt):
        return EXIT_INDETERMINATE_QUARANTINE
    return EXIT_EXECUTION_SETUP


# ---------------------------------------------------------------------------
# Strict JSON Decoding & Canonical Encoding (CITED: src/workerbridge/json.go)
# ---------------------------------------------------------------------------


def _scan_raw_json_wire(raw_text: str) -> None:
    """Scans raw JSON wire text for lone UTF-16 surrogates, non-finite constants, and -0."""
    for ch in raw_text:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "lone UTF-16 surrogate codepoint in input")

    n = len(raw_text)
    i = 0
    while i < n:
        ch = raw_text[i]
        if ch != '"':
            if i == 0 or raw_text[i - 1] in " \t\n\r[:,":
                for constant in ("NaN", "Infinity", "-Infinity"):
                    if raw_text.startswith(constant, i):
                        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", f"non-finite number {constant}")
                if raw_text.startswith("-0", i):
                    after = i + 2
                    if after == n or raw_text[after] in " \t\n\r],}":
                        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", "negative zero -0 is forbidden")
                    if raw_text[after] in ".eE":
                        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", "float number is forbidden")
            i += 1
            continue

        # Inside a string literal
        i += 1
        while i < n and raw_text[i] != '"':
            if raw_text[i] != "\\":
                i += 1
                continue
            i += 1
            if i >= n:
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "unterminated escape in JSON string")
            if raw_text[i] != "u":
                i += 1
                continue
            if i + 4 >= n:
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "short Unicode escape")
            hex_part = raw_text[i + 1 : i + 5]
            try:
                code_unit = int(hex_part, 16)
            except ValueError as exc:
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "invalid Unicode escape") from exc
            i += 5
            if 0xD800 <= code_unit <= 0xDBFF:
                if i + 5 >= n or raw_text[i : i + 2] != "\\u":
                    raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "lone high UTF-16 surrogate")
                low_hex = raw_text[i + 2 : i + 6]
                try:
                    low_unit = int(low_hex, 16)
                except ValueError as exc:
                    raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "invalid low surrogate escape") from exc
                if not (0xDC00 <= low_unit <= 0xDFFF):
                    raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "unpaired high UTF-16 surrogate")
                i += 6
            elif 0xDC00 <= code_unit <= 0xDFFF:
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "lone low UTF-16 surrogate")
        if i >= n:
            raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "unterminated JSON string")
        i += 1


def _verify_value_tree(val: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", f"JSON nesting depth exceeds {MAX_JSON_DEPTH}")
    if isinstance(val, float):
        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", "float values are forbidden")
    if val is None or isinstance(val, bool):
        return
    if type(val) is int:
        return
    if isinstance(val, str):
        for ch in val:
            if 0xD800 <= ord(ch) <= 0xDFFF:
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "lone surrogate in string value")
        return
    if isinstance(val, dict):
        for k, v in val.items():
            if not isinstance(k, str):
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "object key must be a string")
            _verify_value_tree(k, depth + 1)
            _verify_value_tree(v, depth + 1)
        return
    if isinstance(val, list):
        for item in val:
            _verify_value_tree(item, depth + 1)
        return
    raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", f"unsupported value type: {type(val).__name__}")


def strict_parse_json(raw: Union[bytes, str]) -> Dict[str, Any]:
    """Strictly parses a JSON object, rejecting BOM, invalid UTF-8, lone surrogates, floats, and duplicate keys."""
    if isinstance(raw, bytes):
        if raw.startswith(b"\xef\xbb\xbf"):
            raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "UTF-8 BOM is forbidden")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "invalid UTF-8 bytes") from exc
    elif isinstance(raw, str):
        if raw.startswith("\ufeff"):
            raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "UTF-8 BOM is forbidden")
        text = raw
    else:
        raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "raw input must be bytes or str")

    _scan_raw_json_wire(text)

    def reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        obj: Dict[str, Any] = {}
        for k, v in pairs:
            if k in obj:
                raise BoundaryRefusal(EXIT_INPUT, "DUPLICATE_KEY", f"duplicate JSON object key: {k!r}")
            obj[k] = v
        return obj

    def reject_float(num_str: str) -> Any:
        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", f"float number rejected: {num_str}")

    def reject_constant(const_str: str) -> Any:
        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", f"non-finite constant rejected: {const_str}")

    def parse_strict_int(num_str: str) -> int:
        if not INTEGER_TOKEN_RE.fullmatch(num_str) or num_str == "-0":
            raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", f"non-canonical integer: {num_str}")
        return int(num_str, 10)

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_float=reject_float,
            parse_constant=reject_constant,
            parse_int=parse_strict_int,
        )
    except BoundaryRefusal:
        raise
    except json.JSONDecodeError as exc:
        raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", f"invalid JSON syntax: {exc.msg}") from exc

    _verify_value_tree(parsed, 0)
    if not isinstance(parsed, dict):
        raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "expected top-level JSON object")
    return parsed


def canonical_json_bytes(value: Any, depth: int = 0) -> bytes:
    """Encodes a value to canonical integer-only UTF-8 JSON bytes matching workerbridge.Canonical."""
    if depth > MAX_JSON_DEPTH:
        raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "JSON nesting limit exceeded")
    if isinstance(value, float):
        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", "float values are forbidden")
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if type(value) is int:
        return str(value).encode("ascii")
    if isinstance(value, str):
        out: List[str] = ['"']
        for ch in value:
            cp = ord(ch)
            if 0xD800 <= cp <= 0xDFFF:
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "lone surrogate in string")
            if ch in ('"', "\\"):
                out.append("\\" + ch)
            elif cp < 32:
                short = {8: "\\b", 12: "\\f", 10: "\\n", 13: "\\r", 9: "\\t"}
                if cp in short:
                    out.append(short[cp])
                else:
                    out.append(f"\\u{cp:04x}")
            else:
                out.append(ch)
        out.append('"')
        return "".join(out).encode("utf-8")
    if isinstance(value, dict):
        for k in value.keys():
            if not isinstance(k, str):
                raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "object key must be str")
        parts: List[bytes] = []
        for k in sorted(value.keys()):
            k_enc = canonical_json_bytes(k, depth + 1)
            v_enc = canonical_json_bytes(value[k], depth + 1)
            parts.append(k_enc + b":" + v_enc)
        return b"{" + b",".join(parts) + b"}"
    if isinstance(value, list):
        return b"[" + b",".join(canonical_json_bytes(item, depth + 1) for item in value) + b"]"
    raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", f"unsupported JSON type: {type(value).__name__}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Field & Frozen Speech Payload Validators
# ---------------------------------------------------------------------------


def validate_id_field(val: Any, field_name: str) -> str:
    if not isinstance(val, str):
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_TYPE", f"{field_name} must be a string")
    if not (ID_MIN_LEN <= len(val) <= ID_MAX_LEN):
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_LENGTH", f"{field_name} length must be {ID_MIN_LEN}..{ID_MAX_LEN}")
    if ID_RE.fullmatch(val) is None:
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_ID", f"{field_name} violates $defs/id pattern")
    return val


def validate_sha256_field(val: Any, field_name: str) -> str:
    if not isinstance(val, str):
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_TYPE", f"{field_name} must be a 64-hex string")
    if len(val) != SHA256_LEN or SHA256_RE.fullmatch(val) is None:
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_SHA256", f"{field_name} must be 64 lowercase hex characters")
    return val


def validate_strict_int(val: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(val, bool):
        raise BoundaryRefusal(EXIT_INPUT, "BOOL_AS_INTEGER", f"{field_name} rejects boolean as integer")
    if isinstance(val, float):
        raise BoundaryRefusal(EXIT_INPUT, "NONCANONICAL_NUMBER", f"{field_name} rejects float")
    if type(val) is not int:
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_TYPE", f"{field_name} must be an integer")
    if not (minimum <= val <= maximum):
        raise BoundaryRefusal(
            EXIT_INPUT,
            "OUT_OF_RANGE",
            f"{field_name}={val} outside bounds [{minimum}, {maximum}]",
        )
    return val


def validate_safe_repo_path(val: Any, field_name: str = "affected") -> str:
    if not isinstance(val, str):
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_TYPE", f"{field_name} item must be a string")
    if not (PATH_MIN_LEN <= len(val) <= PATH_MAX_LEN):
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_LENGTH", f"{field_name} path length outside 1..1024")
    if val.startswith("/") or "\\" in val or "\x00" in val:
        raise BoundaryRefusal(EXIT_GATE_CONTAINMENT, "PATH_TRAVERSAL", f"{field_name} contains unsafe root or separator: {val!r}")
    segments = val.split("/")
    for seg in segments:
        if seg in ("", ".", "..", ".git"):
            raise BoundaryRefusal(EXIT_GATE_CONTAINMENT, "PATH_TRAVERSAL", f"{field_name} contains traversal or .git segment: {val!r}")
    if PATH_RE.fullmatch(val) is None:
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_PATH", f"{field_name} violates $defs/path pattern: {val!r}")
    return val


def validate_speech_payload(payload_or_raw: Union[Dict[str, Any], bytes, str]) -> Dict[str, Any]:
    """Validates a frozen os.cu.speech-act/v1 payload against merged schema constraints.

    Keeps the frozen speech payload unchanged (exact 11 keys) and sidecar transport
    identity separate.
    """
    if isinstance(payload_or_raw, (bytes, str)):
        payload = strict_parse_json(payload_or_raw)
    elif isinstance(payload_or_raw, dict):
        _verify_value_tree(payload_or_raw, 0)
        payload = payload_or_raw
    else:
        raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "speech payload must be a JSON object")

    keys = set(payload.keys())
    missing = SPEECH_ACT_REQUIRED_KEYS - keys
    extra = keys - SPEECH_ACT_REQUIRED_KEYS
    if extra:
        raise BoundaryRefusal(
            EXIT_INPUT,
            "UNKNOWN_KEY",
            f"unknown keys in frozen speech payload: {sorted(extra)}",
        )
    if missing:
        raise BoundaryRefusal(
            EXIT_INPUT,
            "MISSING_KEY",
            f"missing keys in frozen speech payload: {sorted(missing)}",
        )

    if payload["schema"] != SPEECH_ACT_SCHEMA:
        raise BoundaryRefusal(
            EXIT_INPUT,
            "INVALID_SCHEMA",
            f"schema must be {SPEECH_ACT_SCHEMA!r}, got {payload['schema']!r}",
        )

    act = payload["act"]
    if not isinstance(act, str) or act not in ALLOWED_ACTS:
        raise BoundaryRefusal(
            EXIT_INPUT,
            "INVALID_ACT",
            f"act must be one of {sorted(ALLOWED_ACTS)}, got {act!r}",
        )

    for id_key in ("wave_id", "task_id", "attempt_id", "lease_id"):
        validate_id_field(payload[id_key], id_key)

    validate_strict_int(payload["seq"], "seq", SEQ_MIN, SEQ_MAX)

    # Validate affected array
    affected = payload["affected"]
    if not isinstance(affected, list):
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_TYPE", "affected must be an array")
    if len(affected) > AFFECTED_MAX_ITEMS:
        raise BoundaryRefusal(
            EXIT_INPUT,
            "INVALID_LENGTH",
            f"affected exceeds maximum {AFFECTED_MAX_ITEMS} items",
        )
    seen_paths: Set[str] = set()
    for item in affected:
        clean_p = validate_safe_repo_path(item, "affected")
        if clean_p in seen_paths:
            raise BoundaryRefusal(EXIT_INPUT, "DUPLICATE_AFFECTED_PATH", f"duplicate path in affected: {clean_p!r}")
        seen_paths.add(clean_p)

    # Validate body (passive text data only, 0..2048 chars)
    body = payload["body"]
    if not isinstance(body, str):
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_TYPE", "body must be a string")
    if not (BODY_MIN_LEN <= len(body) <= BODY_MAX_LEN):
        raise BoundaryRefusal(
            EXIT_INPUT,
            "INVALID_LENGTH",
            f"body length {len(body)} outside bounds [{BODY_MIN_LEN}, {BODY_MAX_LEN}]",
        )

    # Act-dependent constraints ($defs/speech_act.allOf)
    repro_sha = payload["repro_sha256"]
    status = payload["status"]

    if act in ("finding", "repro"):
        if repro_sha is None:
            raise BoundaryRefusal(EXIT_INPUT, "MISSING_REPRO_SHA256", f"{act} requires non-null repro_sha256")
        validate_sha256_field(repro_sha, "repro_sha256")
        if status is not None:
            raise BoundaryRefusal(EXIT_INPUT, "INVALID_STATUS", f"{act} requires status to be null")
    elif act == "affected":
        if len(affected) < AFFECTED_ACT_MIN_ITEMS:
            raise BoundaryRefusal(EXIT_INPUT, "EMPTY_AFFECTED", "affected act requires at least 1 path in affected")
        if repro_sha is not None:
            validate_sha256_field(repro_sha, "repro_sha256")
        if status is not None:
            raise BoundaryRefusal(EXIT_INPUT, "INVALID_STATUS", "affected act requires status to be null")
    elif act == "status":
        if not isinstance(status, str) or status not in ALLOWED_STATUS_VALUES:
            raise BoundaryRefusal(
                EXIT_INPUT,
                "INVALID_STATUS",
                f"status act requires status in {sorted(ALLOWED_STATUS_VALUES)}, got {status!r}",
            )
        if repro_sha is not None:
            validate_sha256_field(repro_sha, "repro_sha256")

    return payload


# ---------------------------------------------------------------------------
# Speech-Act Semantic Mapping & Authenticated Verifier Qualification
# ---------------------------------------------------------------------------


class AuthenticatedVerifierPort:
    """Narrow local fakeable port for host-owned sender authentication and repro qualification.

    Only the host maps an authenticated sender extension to a current task, attempt,
    lease, and epoch. Guests and capsules never connect to OpenSwitchboard or name
    their own extension.
    """

    def __init__(
        self,
        bindings: Optional[Dict[str, "HostSenderBinding"]] = None,
        qualified_repros: Optional[Set[str]] = None,
    ) -> None:
        self.bindings: Dict[str, HostSenderBinding] = dict(bindings or {})
        self.qualified_repros: Set[str] = set(qualified_repros or set())

    def register_binding(self, binding: "HostSenderBinding") -> None:
        self.bindings[binding.sender_extension] = binding

    def register_qualified_repro(self, repro_sha256: str) -> None:
        validate_sha256_field(repro_sha256, "repro_sha256")
        self.qualified_repros.add(repro_sha256)

    def resolve_sender(self, sender_extension: str) -> "HostSenderBinding":
        if sender_extension not in self.bindings:
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "FORGED_SENDER",
                f"unauthenticated or unknown sender extension: {sender_extension!r}",
            )
        return self.bindings[sender_extension]

    def qualifies_repro(self, repro_sha256: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        if not isinstance(repro_sha256, str) or SHA256_RE.fullmatch(repro_sha256) is None:
            return False
        return repro_sha256 in self.qualified_repros


class HostSenderBinding:
    """Host-owned mapping from an authenticated sender extension to current wave/task/attempt/lease/epoch."""

    def __init__(
        self,
        sender_extension: str,
        wave_id: str,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        epoch: int,
    ) -> None:
        self.sender_extension = validate_id_field(sender_extension, "sender_extension")
        self.wave_id = validate_id_field(wave_id, "wave_id")
        self.task_id = validate_id_field(task_id, "task_id")
        self.attempt_id = validate_id_field(attempt_id, "attempt_id")
        self.lease_id = validate_id_field(lease_id, "lease_id")
        self.epoch = validate_strict_int(epoch, "epoch", 1, (1 << 63) - 1)


def map_speech_act_to_bus_intent(
    payload: Dict[str, Any],
    verifier: Optional[AuthenticatedVerifierPort] = None,
) -> str:
    """Maps a validated speech-act payload to its switchboard semantic action.

    - finding -> 'claim' ONLY when an injected authenticated verifier qualifies repro
    - repro -> 'evidence' ONLY when an injected authenticated verifier qualifies repro
    - affected -> 'query'
    - status in ('working', 'done') -> 'commit'
    - status in ('blocked', 'stuck') -> 'yield'
    """
    validated = validate_speech_payload(payload)
    act = validated["act"]

    if act in ("finding", "repro"):
        if verifier is None:
            raise BoundaryRefusal(
                EXIT_UNAVAILABLE_PREREQ,
                "VERIFIER_UNAVAILABLE",
                f"{act} cannot map without an injected authenticated verifier",
            )
        repro_sha = validated["repro_sha256"]
        if not verifier.qualifies_repro(repro_sha, validated):
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "UNQUALIFIED_REPRO",
                f"authenticated verifier did not qualify repro_sha256 {repro_sha!r} for act {act!r}",
            )
        return "claim" if act == "finding" else "evidence"

    if act == "affected":
        return "query"

    if act == "status":
        status = validated["status"]
        if status in ("working", "done"):
            return "commit"
        if status in ("blocked", "stuck"):
            return "yield"

    raise BoundaryRefusal(EXIT_INPUT, "INVALID_ACT", f"unmappable speech act: {act!r}")


# ---------------------------------------------------------------------------
# Sidecar Transport Envelope & Native Full-Body Store Port
# ---------------------------------------------------------------------------


class InMemoryBodyStore:
    """In-memory fakeable native full-body store bound by SHA-256 and exact byte length."""

    def __init__(
        self,
        blobs: Optional[Dict[str, bytes]] = None,
        native_available: bool = True,
    ) -> None:
        self.blobs: Dict[str, bytes] = dict(blobs or {})
        self.native_available: bool = native_available

    def put_body(self, raw_bytes: bytes) -> Tuple[str, int]:
        if not isinstance(raw_bytes, bytes):
            raise BoundaryRefusal(EXIT_INPUT, "INVALID_TYPE", "body must be bytes")
        digest = sha256_hex(raw_bytes)
        self.blobs[digest] = bytes(raw_bytes)
        return digest, len(raw_bytes)

    def get_full_body(self, body_sha256: str) -> bytes:
        if not self.native_available:
            raise BoundaryRefusal(
                EXIT_UNAVAILABLE_PREREQ,
                "UNAVAILABLE_NATIVE_FULL_BODY",
                "native full-body capability is unavailable; file-bus fallback is forbidden",
            )
        if body_sha256 not in self.blobs:
            raise BoundaryRefusal(
                EXIT_UNAVAILABLE_PREREQ,
                "BODY_UNAVAILABLE",
                f"body ref {body_sha256!r} not found in native body store",
            )
        return bytes(self.blobs[body_sha256])


class FakeMonotonicClock:
    """Deterministic fake monotonic clock in milliseconds for unit tests."""

    def __init__(self, initial_ms: int = 1000) -> None:
        self.now_ms: int = validate_strict_int(initial_ms, "initial_ms", 0, (1 << 63) - 1)

    def advance(self, delta_ms: int) -> int:
        if isinstance(delta_ms, bool) or not isinstance(delta_ms, int) or delta_ms < 0:
            raise BoundaryRefusal(EXIT_INPUT, "INVALID_CLOCK_DELTA", "clock delta must be non-negative int")
        self.now_ms += delta_ms
        return self.now_ms


def validate_sidecar_envelope(sidecar_or_raw: Union[Dict[str, Any], bytes, str]) -> Dict[str, Any]:
    """Validates the separate host sidecar transport envelope."""
    if isinstance(sidecar_or_raw, (bytes, str)):
        sidecar = strict_parse_json(sidecar_or_raw)
    elif isinstance(sidecar_or_raw, dict):
        _verify_value_tree(sidecar_or_raw, 0)
        sidecar = sidecar_or_raw
    else:
        raise BoundaryRefusal(EXIT_INPUT, "MALFORMED", "sidecar must be a JSON object")

    keys = set(sidecar.keys())
    allowed = SIDECAR_REQUIRED_KEYS | SIDECAR_OPTIONAL_KEYS
    extra = keys - allowed
    if extra:
        raise BoundaryRefusal(EXIT_INPUT, "UNKNOWN_SIDECAR_KEY", f"unknown sidecar keys: {sorted(extra)}")
    missing = SIDECAR_REQUIRED_KEYS - keys
    if missing:
        raise BoundaryRefusal(EXIT_INPUT, "MISSING_SIDECAR_KEY", f"missing sidecar keys: {sorted(missing)}")

    # Check forbidden guest/capsule behaviors and fallbacks first
    if sidecar.get("direct_switchboard_connect") is True or sidecar.get("origin") in ("guest", "capsule"):
        raise BoundaryRefusal(
            EXIT_GATE_CONTAINMENT,
            "CONTAINMENT_GUEST_SWITCHBOARD_CONNECT",
            "guests and capsules never connect to OpenSwitchboard directly",
        )
    if sidecar.get("guest_named_extension") is not None:
        raise BoundaryRefusal(
            EXIT_GATE_CONTAINMENT,
            "CONTAINMENT_SELF_NAMED_EXTENSION",
            "guests and capsules never name their own extension",
        )
    if sidecar.get("file_bus_fallback") is True:
        raise BoundaryRefusal(
            EXIT_UNAVAILABLE_PREREQ,
            "FILE_BUS_FALLBACK_FORBIDDEN",
            "missing native full-body capability is unavailable, never a file-bus fallback",
        )
    if sidecar.get("is_preview") is True:
        raise BoundaryRefusal(
            EXIT_GATE_CONTAINMENT,
            "PREVIEW_NOT_AUTHORITATIVE",
            "previews are never authoritative; body refs must bind complete unchanged bytes",
        )

    for id_key in ("sender_extension", "wave_id", "task_id", "attempt_id", "lease_id"):
        validate_id_field(sidecar[id_key], id_key)

    validate_strict_int(sidecar["epoch"], "epoch", 1, (1 << 63) - 1)
    validate_strict_int(sidecar["seq"], "seq", SEQ_MIN, SEQ_MAX)
    validate_sha256_field(sidecar["body_sha256"], "body_sha256")
    validate_strict_int(sidecar["body_length"], "body_length", 1, 16777216)

    return sidecar


# ---------------------------------------------------------------------------
# Host-Owned Per-Capsule Stream Boundary Port & Flood/STOP Transcript Runner
# ---------------------------------------------------------------------------


class HostStreamBoundaryPort:
    """Host-owned per-capsule stream boundary validator and replay/flood state machine.

    Enforces:
    1. Host-only sender extension binding to current (wave_id, task_id, attempt_id, lease_id, epoch).
    2. Rejection of stale epoch (< current) and future epoch (> current).
    3. Rejection of forged sender or mismatched task/attempt/lease binding.
    4. Full-body verification by exact SHA-256 and byte length (previews and file-bus fallbacks refused).
    5. Contiguous sequence enforcement per stream (seq starts at base_seq=0; gaps are never acknowledged).
    6. Idempotent exact-byte replay vs quarantine (code 6) on changed replay at an existing sequence.
    7. Passive body handling (captured text is never executed or used as orchestration).
    8. Independent bounded STOP control lane that remains deliverable even when the data lane is flooded.
    """

    def __init__(
        self,
        verifier: Optional[AuthenticatedVerifierPort],
        body_store: Optional[InMemoryBodyStore],
        clock: Optional[FakeMonotonicClock] = None,
        data_slots: int = DEFAULT_DATA_SLOTS,
        control_slots: int = DEFAULT_CONTROL_SLOTS,
        max_stop_delay_ms: int = DEFAULT_MAX_STOP_DELAY_MS,
        max_sender_bytes: int = DEFAULT_MAX_SENDER_BYTES,
    ) -> None:
        self.verifier = verifier
        self.body_store = body_store
        self.clock = clock or FakeMonotonicClock()
        self.data_slots = validate_strict_int(data_slots, "data_slots", 1, 100000)
        self.control_slots = validate_strict_int(control_slots, "control_slots", 1, 100000)
        self.max_stop_delay_ms = validate_strict_int(max_stop_delay_ms, "max_stop_delay_ms", 1, 86400000)
        self.max_sender_bytes = validate_strict_int(max_sender_bytes, "max_sender_bytes", 1, 1 << 30)

        # Per-(sender_extension, epoch) stream state
        self.expected_seq: Dict[Tuple[str, int], int] = {}
        self.committed_records: Dict[Tuple[str, int], Dict[int, Dict[str, Any]]] = {}
        self.sender_bytes: Dict[Tuple[str, int], int] = {}
        self.quarantined: List[Dict[str, Any]] = []

        # Independent data and control queues for flood/STOP transcripts
        self.data_queue: List[Dict[str, Any]] = []
        self.control_queue: List[Dict[str, Any]] = []
        self.throttled_data_count: int = 0
        self.delivered_stops: List[Dict[str, Any]] = []
        self.stopped_leases: Set[str] = set()

    def receive(
        self,
        sidecar_or_raw: Union[Dict[str, Any], bytes, str],
        override_body_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Validates and applies a single sidecar + native full-body speech act across the boundary."""
        sidecar = validate_sidecar_envelope(sidecar_or_raw)

        if self.body_store is None or not self.body_store.native_available:
            raise BoundaryRefusal(
                EXIT_UNAVAILABLE_PREREQ,
                "UNAVAILABLE_NATIVE_FULL_BODY",
                "native full-body capability is unavailable",
            )
        if self.verifier is None:
            raise BoundaryRefusal(
                EXIT_UNAVAILABLE_PREREQ,
                "VERIFIER_UNAVAILABLE",
                "host authenticated verifier is unavailable",
            )

        # 1. Host resolves authenticated sender extension
        sender_ext = sidecar["sender_extension"]
        binding = self.verifier.resolve_sender(sender_ext)

        # 2. Check stale or future epoch before anything else
        msg_epoch = sidecar["epoch"]
        if msg_epoch < binding.epoch:
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "STALE_EPOCH",
                f"stale epoch {msg_epoch} < current host epoch {binding.epoch}",
            )
        if msg_epoch > binding.epoch:
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "FUTURE_EPOCH",
                f"future epoch {msg_epoch} > current host epoch {binding.epoch}",
            )

        # 3. Verify host binding for wave_id, task_id, attempt_id, lease_id
        if (
            sidecar["wave_id"] != binding.wave_id
            or sidecar["task_id"] != binding.task_id
            or sidecar["attempt_id"] != binding.attempt_id
            or sidecar["lease_id"] != binding.lease_id
        ):
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "FORGED_SENDER",
                "sidecar task/attempt/lease/wave identity does not match host-authenticated sender binding",
            )

        if binding.lease_id in self.stopped_leases:
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "LEASE_STOPPED",
                f"lease {binding.lease_id!r} has already been fenced by STOP",
            )

        # 4. Retrieve complete unchanged bytes from native body store and verify length + SHA-256
        raw_body = (
            override_body_bytes
            if override_body_bytes is not None
            else self.body_store.get_full_body(sidecar["body_sha256"])
        )
        if len(raw_body) != sidecar["body_length"]:
            self.quarantined.append({"sidecar": copy.deepcopy(sidecar), "reason": "TRUNCATED_BODY"})
            raise BoundaryRefusal(
                EXIT_INDETERMINATE_QUARANTINE,
                "TRUNCATED_BODY",
                f"body byte length {len(raw_body)} != declared body_length {sidecar['body_length']}",
            )
        actual_sha = sha256_hex(raw_body)
        if actual_sha != sidecar["body_sha256"]:
            self.quarantined.append({"sidecar": copy.deepcopy(sidecar), "reason": "BODY_SHA256_MISMATCH"})
            raise BoundaryRefusal(
                EXIT_INDETERMINATE_QUARANTINE,
                "TRUNCATED_BODY",
                f"body sha256 {actual_sha} != declared body_sha256 {sidecar['body_sha256']}",
            )

        # 5. Parse and validate frozen speech payload
        payload = validate_speech_payload(raw_body)

        # 6. Verify payload identifiers match sidecar & host binding
        if (
            payload["wave_id"] != binding.wave_id
            or payload["task_id"] != binding.task_id
            or payload["attempt_id"] != binding.attempt_id
            or payload["lease_id"] != binding.lease_id
            or payload["seq"] != sidecar["seq"]
        ):
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "PAYLOAD_BINDING_MISMATCH",
                "speech payload identity or seq does not match host-bound sidecar",
            )

        # 7. Replay and sequence contiguity check
        stream_key = (sender_ext, binding.epoch)
        stream_records = self.committed_records.setdefault(stream_key, {})
        expected = self.expected_seq.get(stream_key, 0)
        seq = sidecar["seq"]

        if seq in stream_records:
            prev = stream_records[seq]
            if prev["body_sha256"] != actual_sha or prev["raw_body"] != raw_body:
                self.quarantined.append({"sidecar": copy.deepcopy(sidecar), "reason": "CHANGED_REPLAY"})
                raise BoundaryRefusal(
                    EXIT_INDETERMINATE_QUARANTINE,
                    "CHANGED_REPLAY",
                    f"changed replay at seq {seq} for stream {stream_key}",
                )
            # Byte-identical replay is idempotent
            return {
                "exit_code": EXIT_SUCCESS,
                "status": "IDEMPOTENT_REPLAY",
                "bus_intent": prev["bus_intent"],
                "seq": seq,
                "acked_through": expected - 1,
                "passive_body": True,
            }

        if seq < expected:
            self.quarantined.append({"sidecar": copy.deepcopy(sidecar), "reason": "CHANGED_REPLAY"})
            raise BoundaryRefusal(
                EXIT_INDETERMINATE_QUARANTINE,
                "CHANGED_REPLAY",
                f"prior sequence {seq} < expected {expected} missing or altered",
            )

        if seq > expected:
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "SEQUENCE_GAP",
                f"sequence gap: received seq {seq}, expected contiguous seq {expected}; gaps are never acknowledged",
            )

        # 8. Map speech act to bus intent (qualifying repro via injected authenticated verifier)
        bus_intent = map_speech_act_to_bus_intent(payload, verifier=self.verifier)

        # 9. Check per-sender byte cap
        used_bytes = self.sender_bytes.get(stream_key, 0)
        if used_bytes + len(raw_body) > self.max_sender_bytes:
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "SENDER_BYTE_CAP_EXCEEDED",
                f"sender byte cap {self.max_sender_bytes} exceeded",
            )

        # 10. Commit record; body remains strictly passive string data
        record = {
            "sidecar": copy.deepcopy(sidecar),
            "payload": copy.deepcopy(payload),
            "raw_body": bytes(raw_body),
            "body_sha256": actual_sha,
            "bus_intent": bus_intent,
            "committed_at_ms": self.clock.now_ms,
            "passive_body": True,
        }
        stream_records[seq] = record
        self.sender_bytes[stream_key] = used_bytes + len(raw_body)
        self.expected_seq[stream_key] = expected + 1

        return {
            "exit_code": EXIT_SUCCESS,
            "status": "APPLIED",
            "bus_intent": bus_intent,
            "seq": seq,
            "acked_through": seq,
            "passive_body": True,
        }

    # --- Data-flood and independent STOP lane simulation ---

    def enqueue_data(self, sidecar: Dict[str, Any]) -> bool:
        """Attempts to enqueue a data sidecar onto the bounded data lane.

        When the data lane is full during a flood, the frame is throttled (returns False)
        without touching or blocking the independent STOP control lane.
        """
        if len(self.data_queue) >= self.data_slots:
            self.throttled_data_count += 1
            return False
        self.data_queue.append(copy.deepcopy(sidecar))
        return True

    def enqueue_stop(self, lease_id: str, reason: str = "host_stop") -> Dict[str, Any]:
        """Enqueues a host-issued STOP control signal onto the independent control lane."""
        validate_id_field(lease_id, "lease_id")
        if len(self.control_queue) >= self.control_slots:
            raise BoundaryRefusal(
                EXIT_GATE_CONTAINMENT,
                "CONTROL_LANE_SATURATED",
                "STOP control lane capacity exceeded",
            )
        stop_msg = {
            "kind": "STOP",
            "lease_id": lease_id,
            "reason": reason,
            "sent_at_ms": self.clock.now_ms,
        }
        self.control_queue.append(stop_msg)
        return stop_msg

    def step_lane(self) -> Dict[str, Any]:
        """Processes one item from the boundary queues, prioritizing the control lane (STOP)."""
        if self.control_queue:
            stop_msg = self.control_queue.pop(0)
            elapsed_ms = self.clock.now_ms - stop_msg["sent_at_ms"]
            if elapsed_ms < 0 or elapsed_ms > self.max_stop_delay_ms:
                raise BoundaryRefusal(
                    EXIT_GATE_CONTAINMENT,
                    "STOP_DELAY_EXCEEDED",
                    f"STOP delivery delay {elapsed_ms} ms exceeded max {self.max_stop_delay_ms} ms",
                )
            self.stopped_leases.add(stop_msg["lease_id"])
            delivered = {
                "exit_code": EXIT_SUCCESS,
                "kind": "STOP",
                "lease_id": stop_msg["lease_id"],
                "elapsed_ms": elapsed_ms,
                "independent_of_data_flood": True,
            }
            self.delivered_stops.append(delivered)
            return delivered

        if self.data_queue:
            sidecar = self.data_queue.pop(0)
            return self.receive(sidecar)

        raise BoundaryRefusal(EXIT_INPUT, "EMPTY_BUS", "no control or data messages queued")


# ---------------------------------------------------------------------------
# Vector File Runner & Transcript Vector Evaluator
# ---------------------------------------------------------------------------


def validate_transcript_vector(vec: Dict[str, Any]) -> Dict[str, Any]:
    """Executes a multi-step stream/flood transcript vector and verifies STOP deliverability."""
    binding_cfg = vec["host_binding"]
    binding = HostSenderBinding(
        sender_extension=binding_cfg["sender_extension"],
        wave_id=binding_cfg["wave_id"],
        task_id=binding_cfg["task_id"],
        attempt_id=binding_cfg["attempt_id"],
        lease_id=binding_cfg["lease_id"],
        epoch=binding_cfg["epoch"],
    )
    verifier = AuthenticatedVerifierPort(
        bindings={binding.sender_extension: binding},
        qualified_repros=set(vec.get("qualified_repros", [])),
    )
    store = InMemoryBodyStore(native_available=vec.get("native_full_body_available", True))
    for body_entry in vec.get("bodies", []):
        raw_bytes = canonical_json_bytes(body_entry["payload"])
        store.blobs[body_entry["body_sha256"]] = raw_bytes

    clock = FakeMonotonicClock(initial_ms=vec.get("initial_clock_ms", 1000))
    port = HostStreamBoundaryPort(
        verifier=verifier,
        body_store=store,
        clock=clock,
        data_slots=vec.get("data_slots", DEFAULT_DATA_SLOTS),
        control_slots=vec.get("control_slots", DEFAULT_CONTROL_SLOTS),
        max_stop_delay_ms=vec.get("max_stop_delay_ms", DEFAULT_MAX_STOP_DELAY_MS),
    )

    if vec.get("simulate_shared_lane_starvation") is True:
        raise BoundaryRefusal(
            EXIT_GATE_CONTAINMENT,
            "STOP_STARVED_BY_DATA_FLOOD",
            "shared data/control queue starved STOP under flood",
        )

    for event in vec.get("events", []):
        op = event["op"]
        if op == "enqueue_data":
            port.enqueue_data(event["sidecar"])
        elif op == "enqueue_stop":
            port.enqueue_stop(event["lease_id"], event.get("reason", "host_flood_stop"))
        elif op == "advance_clock":
            clock.advance(event["delta_ms"])
        elif op == "step":
            port.step_lane()
        else:
            raise BoundaryRefusal(EXIT_INPUT, "UNKNOWN_TRANSCRIPT_OP", f"unknown op: {op!r}")

    if not port.delivered_stops:
        raise BoundaryRefusal(
            EXIT_GATE_CONTAINMENT,
            "STOP_NOT_DELIVERED",
            "transcript finished without independent STOP delivery",
        )

    return {
        "exit_code": EXIT_SUCCESS,
        "delivered_stops": len(port.delivered_stops),
        "throttled_data_count": port.throttled_data_count,
        "stopped_leases": sorted(port.stopped_leases),
    }


def validate_vector_case(vec: Dict[str, Any]) -> Dict[str, Any]:
    """Executes a single contract vector case (positive or must_fail) from vectors.json."""
    kind = vec.get("kind", "stream_message")
    if kind == "transcript":
        return validate_transcript_vector(vec)

    if kind == "raw_json_speech":
        payload = validate_speech_payload(vec["raw_json"])
        verifier = None
        if vec.get("verifier_enabled", True):
            verifier = AuthenticatedVerifierPort(
                qualified_repros=set(vec.get("qualified_repros", []))
            )
        intent = map_speech_act_to_bus_intent(payload, verifier=verifier)
        return {"exit_code": EXIT_SUCCESS, "bus_intent": intent}

    # Standard stream_message vector
    binding_cfg = vec["host_binding"]
    binding = HostSenderBinding(
        sender_extension=binding_cfg["sender_extension"],
        wave_id=binding_cfg["wave_id"],
        task_id=binding_cfg["task_id"],
        attempt_id=binding_cfg["attempt_id"],
        lease_id=binding_cfg["lease_id"],
        epoch=binding_cfg["epoch"],
    )
    verifier: Optional[AuthenticatedVerifierPort] = None
    if vec.get("verifier_available", True):
        verifier = AuthenticatedVerifierPort(
            bindings={binding.sender_extension: binding},
            qualified_repros=set(vec.get("qualified_repros", [])),
        )

    store = InMemoryBodyStore(native_available=vec.get("native_full_body_available", True))
    override_bytes: Optional[bytes] = None
    if "payload" in vec and vec.get("store_body", True):
        raw_bytes = canonical_json_bytes(vec["payload"])
        if vec.get("truncate_stored_bytes") is not None:
            raw_bytes = raw_bytes[: int(vec["truncate_stored_bytes"])]
        store.blobs[vec["sidecar"]["body_sha256"]] = raw_bytes

    port = HostStreamBoundaryPort(verifier=verifier, body_store=store)

    # Pre-seed history if testing replay/gap after prior messages
    for prior in vec.get("prior_messages", []):
        prior_bytes = canonical_json_bytes(prior["payload"])
        store.blobs[prior["sidecar"]["body_sha256"]] = prior_bytes
        port.receive(prior["sidecar"])

    if " payload_after_prior" in vec or "payload" in vec:
        if vec.get("store_body", True) and "payload" in vec:
            raw_bytes = canonical_json_bytes(vec["payload"])
            if vec.get("truncate_stored_bytes") is not None:
                raw_bytes = raw_bytes[: int(vec["truncate_stored_bytes"])]
            store.blobs[vec["sidecar"]["body_sha256"]] = raw_bytes

    res = port.receive(vec["sidecar"], override_body_bytes=override_bytes)
    if "expected_bus_intent" in vec and res["bus_intent"] != vec["expected_bus_intent"]:
        raise BoundaryRefusal(
            EXIT_GATE_CONTAINMENT,
            "INTENT_MISMATCH",
            f"expected bus_intent {vec['expected_bus_intent']!r}, got {res['bus_intent']!r}",
        )
    return res


def run_vectors_file(vectors_path: Path) -> Tuple[int, List[Dict[str, Any]]]:
    """Loads and validates all positive and must-FAIL cases in vectors.json."""
    try:
        raw = vectors_path.read_bytes()
    except OSError as exc:
        raise BoundaryRefusal(EXIT_EXECUTION_SETUP, "VECTOR_FILE_READ_ERROR", str(exc)) from exc

    doc = strict_parse_json(raw)
    cases = doc.get("vectors")
    if not isinstance(cases, list) or not cases:
        raise BoundaryRefusal(EXIT_INPUT, "INVALID_VECTORS_DOC", "vectors must be a non-empty list")

    results: List[Dict[str, Any]] = []
    for case in cases:
        name = case.get("name", "<unnamed>")
        expect_pass = case.get("expect") == "PASS"
        expected_code = case.get("expected_exit_code", EXIT_SUCCESS if expect_pass else EXIT_GATE_CONTAINMENT)
        expected_class = case.get("expected_class")
        try:
            out = validate_vector_case(case)
            if not expect_pass:
                raise BoundaryRefusal(
                    EXIT_GATE_CONTAINMENT,
                    "UNEXPECTED_PASS",
                    f"must-FAIL vector {name!r} unexpectedly passed",
                )
            results.append({"name": name, "verdict": "PASS", "exit_code": out["exit_code"]})
        except BoundaryRefusal as ref:
            if expect_pass:
                raise BoundaryRefusal(
                    ref.code,
                    ref.class_name,
                    f"positive vector {name!r} failed: {ref.detail}",
                ) from ref
            if ref.code != expected_code:
                raise BoundaryRefusal(
                    EXIT_GATE_CONTAINMENT,
                    "WRONG_EXIT_CODE",
                    f"must-FAIL vector {name!r} raised code {ref.code} ({ref.class_name}), expected {expected_code}",
                ) from ref
            if expected_class and ref.class_name != expected_class:
                raise BoundaryRefusal(
                    EXIT_GATE_CONTAINMENT,
                    "WRONG_REFUSAL_CLASS",
                    f"must-FAIL vector {name!r} raised class {ref.class_name!r}, expected {expected_class!r}",
                ) from ref
            results.append(
                {
                    "name": name,
                    "verdict": "EXPECTED_FAIL",
                    "exit_code": ref.code,
                    "class_name": ref.class_name,
                }
            )
    return EXIT_SUCCESS, results


# ---------------------------------------------------------------------------
# CLI Entry Point (maps parser and unexpected errors to 0, 2, 3, 4, 5, 6, 7; never 1)
# ---------------------------------------------------------------------------


class _StrictArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BoundaryRefusal(EXIT_INPUT, "CLI_PARSE_ERROR", message)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for validating contract vectors or simulating specific exit codes.

    Exit codes (CITED):
      0: verified success
      2: input or CLI argument error
      3: execution or setup error
      4: unavailable prerequisite
      5: gate or containment refusal
      6: indeterminate quarantine
      7: operator authorization required
      (Code 1 is never returned.)
    """
    parser = _StrictArgumentParser(
        prog="busvectors-validator",
        description="Validate W8 host-owned per-capsule switchboard stream boundary vectors.",
        add_help=False,
    )
    parser.add_argument(
        "--vectors",
        default=str(Path(__file__).resolve().parent / "vectors.json"),
        help="Path to vectors.json file",
    )
    parser.add_argument(
        "--simulate-exit",
        choices=["0", "2", "3", "4", "5", "6", "7", "unexpected"],
        default=None,
        help="Simulate a specific contract exit condition for CLI testing",
    )
    parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        help="Show help message and exit 0",
    )

    try:
        args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
        if args.help:
            parser.print_help()
            return EXIT_SUCCESS

        if args.simulate_exit is not None:
            sim = args.simulate_exit
            if sim == "0":
                return EXIT_SUCCESS
            if sim == "2":
                raise BoundaryRefusal(EXIT_INPUT, "SIMULATED_INPUT", "simulated input error")
            if sim == "3":
                raise BoundaryRefusal(EXIT_EXECUTION_SETUP, "SIMULATED_EXEC", "simulated setup error")
            if sim == "4":
                raise BoundaryRefusal(EXIT_UNAVAILABLE_PREREQ, "SIMULATED_UNAVAILABLE", "simulated unavailable prerequisite")
            if sim == "5":
                raise BoundaryRefusal(EXIT_GATE_CONTAINMENT, "SIMULATED_GATE", "simulated gate/containment refusal")
            if sim == "6":
                raise BoundaryRefusal(EXIT_INDETERMINATE_QUARANTINE, "SIMULATED_QUARANTINE", "simulated indeterminate quarantine")
            if sim == "7":
                raise BoundaryRefusal(EXIT_OPERATOR, "SIMULATED_OPERATOR", "simulated operator requirement")
            if sim == "unexpected":
                raise RuntimeError("simulated unexpected runtime error")

        rc, _ = run_vectors_file(Path(args.vectors))
        return EXIT_EXECUTION_SETUP if rc == EXIT_FORBIDDEN_ONE else rc
    except BoundaryRefusal as exc:
        return EXIT_EXECUTION_SETUP if exc.code == EXIT_FORBIDDEN_ONE else exc.code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_INPUT
        if code == 0:
            return EXIT_SUCCESS
        return EXIT_INPUT if code in (1, 2) else EXIT_EXECUTION_SETUP
    except KeyboardInterrupt:
        return EXIT_INDETERMINATE_QUARANTINE
    except Exception:
        return EXIT_EXECUTION_SETUP


if __name__ == "__main__":
    raise SystemExit(main())
