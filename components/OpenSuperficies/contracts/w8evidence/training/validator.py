"""Python stdlib schema-like validator for the proposed local text-only training-signal carrier.

Scope and contract notes:
- Proposed local contract (`w8-training-signal-v1`) inside `contracts/w8evidence/training/`.
- Not a JSON Schema file, does not touch `contracts/worker/`, and makes no claim about any
  absent `wender` API, live framebuffer capture, or learning success.
- Captured text is passive data only, never orchestration.
- Raw frames remain in bounded RAM outside this validator (`ram_only=True`); only 64-hex
  SHA-256 frame metadata references (or `GRAB_UNAVAILABLE` for a dummy renderer) are allowed.
- No credentials, tokens, keys, environment dumps, or process command lines are read or accepted.

Exit code contract (CITED from `docs/design/W8_WORKER_BANK.md` §6 and `src/workerbridge/json.go:35-45`):
- 0 CITED: verified success
- 2 CITED: invalid input, CLI usage, or top-level schema/version shape
- 3 CITED: execution or setup failure
- 4 CITED: unavailable prerequisite (e.g. missing supply port or missing referenced bytes)
- 5 CITED: gate, validation, policy, or containment refusal (including AUSENTE blocking promotion)
- 6 CITED: indeterminate quarantine (e.g. mutated replay or corrupt/truncated supplied bytes)
- 7 CITED: operator decision or capability authorization required
- 1 CITED: never returned for success or refusal.

Constants provenance:
- Protocol constants (`EXIT_*`, `MAX_JSON_DEPTH = 128`, `SHA256_HEX_LEN = 64`, `COMMIT_HEX_LEN = 40`)
  are CITED from `src/workerbridge/json.go` and `docs/design/W8_WORKER_BANK.md`.
- Quantitative thresholds in gate command literals and default byte/socket caps
  (`TARGET_MAX_UNIX_SOCKET_BYTES = 100`, `TARGET_DEFAULT_BYTE_BOUNDS`) are TARGET.
- Unknown or missing qualification/retention/bounds facts are `AUSENTE` and block promotion.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

# Protocol constants (CITED from docs/design/W8_WORKER_BANK.md §6 & src/workerbridge/json.go)
EXIT_OK: int = 0  # CITED
EXIT_NEVER: int = 1  # CITED: never success or refusal
EXIT_INPUT: int = 2  # CITED
EXIT_SETUP: int = 3  # CITED
EXIT_UNAVAILABLE: int = 4  # CITED
EXIT_GATE: int = 5  # CITED
EXIT_QUARANTINE: int = 6  # CITED
EXIT_OPERATOR: int = 7  # CITED

MAX_JSON_DEPTH: int = 128  # CITED: src/workerbridge/json.go:124,225
SHA256_HEX_LEN: int = 64  # CITED: src/workerbridge/json.go:330
COMMIT_HEX_LEN: int = 40  # CITED: docs/design/W8_WORKER_BANK.md:61

# Quantitative settings in gate/test literals (TARGET)
TARGET_MAX_UNIX_SOCKET_BYTES: int = 100  # TARGET
TARGET_DEFAULT_BYTE_BOUNDS: Dict[str, int] = {
    "max_carrier_bytes": 65536,  # TARGET
    "max_state_bytes": 16384,  # TARGET
    "max_diff_bytes": 16384,  # TARGET
    "max_trace_bytes": 32768,  # TARGET
}

# Sentinel literals
AUSENTE: str = "AUSENTE"
GRAB_UNAVAILABLE: str = "GRAB_UNAVAILABLE"
CARRIER_SCHEMA: str = "w8-training-signal-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_INTEGER_TOKEN_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_NONFINITE_TOKEN_RE = re.compile(r'(?:^|[\s\[\{:,\t\r\n])(-?Infinity|NaN)(?=$|[\s\]\}:,\t\r\n])')

# Forbidden payload patterns: HTML, raw AX trees for models, base64 images, raw pixels, secrets, orchestration
_HTML_PATTERN = re.compile(
    r"<\s*(?:!doctype\s+html|html|head|body|div|span|script|iframe|img|canvas|table|svg|form|input|button)\b",
    re.IGNORECASE,
)
_RAW_AX_PATTERN = re.compile(
    r"(?:\bAXWebArea\b|\bAXRootWebArea\b|\bAXStaticText\b|\bAXWindow\b|\braw_ax_tree\b|\bax_tree_dump\b|\bmodel_ax_payload\b)"
)
_BASE64_IMAGE_PATTERN = re.compile(
    r"(?:data:image/[a-zA-Z0-9.+_-]+;base64,[A-Za-z0-9+/=]{8,}"
    r"|(?:^|[^A-Za-z0-9])(?:iVBORw0KGgo|/9j/4AAQSkZJRg|R0lGODlh|R0lGODdh|UklGR)[A-Za-z0-9+/=]{12,})"
)
_CONTINUOUS_BASE64_BLOB_RE = re.compile(r"^[A-Za-z0-9+/]{128,}={0,2}$")
_SECRET_OR_CMDLINE_PATTERN = re.compile(
    r"(?:PRIVATE" + r"_KEY_MATERIAL"
    r"|\b(?:CREDENTIAL_TOKEN|SECRET_KEY_VALUE|ENV_DUMP_PAYLOAD|CMDLINE_DUMP_PAYLOAD)\b"
    r"|\b(?:operator\.token|launch\.token)\b)"
)
_ORCHESTRATION_PATTERN = re.compile(
    r"(?:\bORCHESTRATE_COMMAND\b|\bEXEC_DIRECTIVE\b|\bMODEL_TOOL_CALL\b|#!/bin/sh)"
)

_FORBIDDEN_KEY_NAMES: Set[str] = {
    "pixels",
    "raw_pixels",
    "rgb_bytes",
    "rgba_bytes",
    "xwd_bytes",
    "framebuffer_bytes",
    "raw_frame",
    "image_base64",
    "frame_base64",
    "screenshot_base64",
    "html",
    "raw_html",
    "dom_html",
    "raw_ax_tree",
    "ax_tree",
    "model_prompt",
    "destined_for_model",
    "credentials",
    "env_dump",
    "proc_cmdline",
}

REQUIRED_CARRIER_KEYS: Set[str] = {
    "schema",
    "versions",
    "seed",
    "requested_ticks",
    "actual_ticks",
    "target",
    "tab_id",
    "session_id",
    "segment_id",
    "acquisition_provenance",
    "state",
    "gameplay_state_hash",
    "state_sha256",
    "ax_sha256",
    "ax_stable_sha256",
    "ui_tree_diff",
    "diff_sha256",
    "t2_trace",
    "t2_trace_sha256",
    "frame_metadata",
    "claims",
}

REQUIRED_VERSIONS_KEYS: Set[str] = {
    "repository_commit",
    "engine_version",
    "source_version",
    "tool_version",
}

REQUIRED_PROVENANCE_KEYS: Set[str] = {
    "source",
    "source_qualification",
    "capture_retention",
    "renderer_mode",
    "byte_bounds",
}

REQUIRED_BYTE_BOUNDS_KEYS: Set[str] = {
    "max_carrier_bytes",
    "max_state_bytes",
    "max_diff_bytes",
    "max_trace_bytes",
}

REQUIRED_UI_DIFF_KEYS: Set[str] = {
    "format",
    "session_id",
    "segment_id",
    "tab_id",
    "target",
    "ops",
}

REQUIRED_UI_DIFF_OP_KEYS: Set[str] = {
    "op",
    "node_id",
    "role",
    "summary",
}

REQUIRED_TRACE_EVENT_KEYS: Set[str] = {
    "session_id",
    "segment_id",
    "tab_id",
    "target",
    "tick",
    "seq",
    "action",
    "admission_verdict",
    "executor_verdict",
    "state_sha256",
    "diff_sha256",
    "frame_ref",
}

REQUIRED_FRAME_METADATA_KEYS: Set[str] = {
    "status",
    "frame_sha256",
    "width",
    "height",
    "format",
    "ram_only",
}

REQUIRED_CLAIMS_KEYS: Set[str] = {
    "synthetic",
    "real_capture",
    "learning_success",
    "labels",
}

ALLOWED_ADMISSION_VERDICTS: Set[str] = {"ADMITTED", "DENIED", "DEFERRED"}
ALLOWED_EXECUTOR_VERDICTS: Set[str] = {"APPLIED", "REFUSED", "NOOP", "ABORTED"}
ALLOWED_RENDERER_MODES: Set[str] = {"standard", "synthetic", "dummy"}
ALLOWED_QUALIFICATIONS: Set[str] = {"qualified", "synthetic_offline", AUSENTE}
ALLOWED_RETENTIONS: Set[str] = {"ram_bounded", "none_synthetic", AUSENTE}
ALLOWED_FRAME_FORMATS: Set[str] = {"RGB24", "XWD_V7", "NONE"}


class RejectError(Exception):
    """Typed contract rejection carrying a W8 normalized exit code (never 1)."""

    def __init__(self, code: int, reject_class: str, detail: str) -> None:
        if code not in (
            EXIT_INPUT,
            EXIT_SETUP,
            EXIT_UNAVAILABLE,
            EXIT_GATE,
            EXIT_QUARANTINE,
            EXIT_OPERATOR,
        ):
            code = EXIT_GATE
        super().__init__(f"{reject_class}: {detail}")
        self.code: int = code
        self.reject_class: str = reject_class
        self.detail: str = detail


def exit_code_for(err: Optional[BaseException]) -> int:
    """Map None to 0 CITED and any exception to 2..7 CITED (never 1)."""
    if err is None:
        return EXIT_OK
    if isinstance(err, RejectError):
        if 2 <= err.code <= 7:
            return err.code
        return EXIT_GATE
    return EXIT_SETUP


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256_hex(val: Any) -> bool:
    return isinstance(val, str) and bool(_SHA256_RE.match(val))


class FakeClock:
    """Deterministic integer monotonic fake clock for offline unit tests."""

    def __init__(self, initial_ns: int = 1_000_000) -> None:
        self._now_ns: int = initial_ns

    def now_ns(self) -> int:
        return self._now_ns

    def advance_ns(self, delta_ns: int) -> int:
        if delta_ns < 0:
            raise RejectError(EXIT_GATE, "NON_MONOTONIC_CLOCK", "fake clock cannot move backwards")
        self._now_ns += delta_ns
        return self._now_ns


def _scan_raw_json_wire(raw_text: str) -> None:
    """Refuse non-finite constants, -0, and lone UTF-16 surrogate escapes before json.loads."""
    n = len(raw_text)
    i = 0
    in_string = False
    token_start = 0
    while i < n:
        ch = raw_text[i]
        if not in_string:
            if ch == '"':
                segment = raw_text[token_start:i]
                if _NONFINITE_TOKEN_RE.search(segment):
                    raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", "non-finite number in JSON")
                if re.search(r"(?:^|[\s\[\{:,\t\r\n])-0(?=$|[\s\]\}:,\t\r\n])", segment):
                    raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", "-0 is not canonical integer-JSON")
                in_string = True
                i += 1
            else:
                i += 1
        else:
            if ch == '"':
                in_string = False
                token_start = i + 1
                i += 1
            elif ch == "\\":
                if i + 1 >= n:
                    raise RejectError(EXIT_GATE, "MALFORMED", "unterminated escape in JSON string")
                esc = raw_text[i + 1]
                if esc == "u":
                    if i + 6 > n:
                        raise RejectError(EXIT_GATE, "MALFORMED", "short Unicode escape")
                    hex_part = raw_text[i + 2 : i + 6]
                    try:
                        codepoint = int(hex_part, 16)
                    except ValueError as exc:
                        raise RejectError(EXIT_GATE, "MALFORMED", "invalid Unicode escape") from exc
                    if 0xD800 <= codepoint <= 0xDBFF:
                        if i + 12 > n or raw_text[i + 6 : i + 8] != "\\u":
                            raise RejectError(EXIT_GATE, "MALFORMED", "lone high surrogate in JSON")
                        try:
                            low = int(raw_text[i + 8 : i + 12], 16)
                        except ValueError as exc:
                            raise RejectError(EXIT_GATE, "MALFORMED", "invalid low surrogate escape") from exc
                        if not (0xDC00 <= low <= 0xDFFF):
                            raise RejectError(EXIT_GATE, "MALFORMED", "lone high surrogate in JSON")
                        i += 12
                        continue
                    if 0xDC00 <= codepoint <= 0xDFFF:
                        raise RejectError(EXIT_GATE, "MALFORMED", "lone low surrogate in JSON")
                    i += 6
                else:
                    i += 2
            else:
                i += 1

    if not in_string and token_start < n:
        tail = raw_text[token_start:]
        if _NONFINITE_TOKEN_RE.search(tail):
            raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", "non-finite number in JSON")
        if re.search(r"(?:^|[\s\[\{:,\t\r\n])-0(?=$|[\s\]\}:,\t\r\n])", tail):
            raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", "-0 is not canonical integer-JSON")


def _validate_integer_json_tree(val: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise RejectError(EXIT_GATE, "MALFORMED", "JSON nesting limit exceeded")
    if isinstance(val, float):
        raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", "float values are forbidden in integer-JSON")
    if val is None or isinstance(val, bool):
        return
    if isinstance(val, int):
        return
    if isinstance(val, str):
        for ch in val:
            cp = ord(ch)
            if 0xD800 <= cp <= 0xDFFF:
                raise RejectError(EXIT_GATE, "MALFORMED", "lone surrogate in string")
        if "\x00" in val:
            raise RejectError(EXIT_GATE, "RAW_BINARY_OR_NUL", "NUL byte forbidden in text-only carrier")
        return
    if isinstance(val, dict):
        for k, v in val.items():
            if not isinstance(k, str):
                raise RejectError(EXIT_GATE, "MALFORMED", "JSON object keys must be strings")
            _validate_integer_json_tree(k, depth + 1)
            _validate_integer_json_tree(v, depth + 1)
        return
    if isinstance(val, list):
        for item in val:
            _validate_integer_json_tree(item, depth + 1)
        return
    raise RejectError(EXIT_GATE, "MALFORMED", f"unsupported value type in integer-JSON: {type(val).__name__}")


def strict_json_parse(raw: Union[str, bytes]) -> Any:
    """Decode strict integer-only JSON, rejecting BOM, invalid UTF-8, floats, and duplicate keys."""
    if isinstance(raw, bytes):
        if raw.startswith(b"\xef\xbb\xbf"):
            raise RejectError(EXIT_GATE, "MALFORMED", "UTF-8 BOM is forbidden")
        if b"\x00" in raw:
            raise RejectError(EXIT_GATE, "RAW_BINARY_OR_NUL", "NUL byte in raw wire input")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RejectError(EXIT_GATE, "MALFORMED", "invalid UTF-8 bytes") from exc
    elif isinstance(raw, str):
        if raw.startswith("\ufeff"):
            raise RejectError(EXIT_GATE, "MALFORMED", "UTF-8 BOM is forbidden")
        if "\x00" in raw:
            raise RejectError(EXIT_GATE, "RAW_BINARY_OR_NUL", "NUL character in wire input")
        text = raw
    else:
        raise RejectError(EXIT_INPUT, "INVALID_INPUT", "raw JSON input must be str or bytes")

    _scan_raw_json_wire(text)

    def _reject_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise RejectError(EXIT_GATE, "DUPLICATE_KEY", f"duplicate JSON object key: {k!r}")
            out[k] = v
        return out

    def _reject_float(token: str) -> Any:
        raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", f"float token forbidden: {token!r}")

    def _reject_constant(token: str) -> Any:
        raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", f"non-finite JSON constant forbidden: {token!r}")

    def _parse_int(token: str) -> int:
        if not _INTEGER_TOKEN_RE.match(token) or token == "-0":
            raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", f"non-canonical integer token: {token!r}")
        return int(token, 10)

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            parse_int=_parse_int,
        )
    except RejectError:
        raise
    except json.JSONDecodeError as exc:
        raise RejectError(EXIT_GATE, "MALFORMED", f"invalid JSON syntax: {exc.msg}") from exc

    _validate_integer_json_tree(parsed)
    return parsed


def canonical_encode(val: Any, depth: int = 0) -> bytes:
    """Encode a Python value into workerbridge-compatible canonical integer-JSON UTF-8 bytes."""
    if depth > MAX_JSON_DEPTH:
        raise RejectError(EXIT_GATE, "MALFORMED", "JSON nesting limit exceeded")
    if isinstance(val, float):
        raise RejectError(EXIT_GATE, "NONCANONICAL_NUMBER", "float value forbidden in canonical_encode")
    if val is None:
        return b"null"
    if isinstance(val, bool):
        return b"true" if val else b"false"
    if isinstance(val, int):
        return str(val).encode("ascii")
    if isinstance(val, str):
        if "\x00" in val:
            raise RejectError(EXIT_GATE, "RAW_BINARY_OR_NUL", "NUL byte forbidden in canonical string")
        out: List[str] = ['"']
        short_map = {8: "\\b", 12: "\\f", 10: "\\n", 13: "\\r", 9: "\\t"}
        for ch in val:
            cp = ord(ch)
            if 0xD800 <= cp <= 0xDFFF:
                raise RejectError(EXIT_GATE, "MALFORMED", "lone surrogate forbidden in canonical string")
            if ch == '"':
                out.append('\\"')
            elif ch == "\\":
                out.append("\\\\")
            elif cp < 32:
                if cp in short_map:
                    out.append(short_map[cp])
                else:
                    out.append(f"\\u{cp:04x}")
            else:
                out.append(ch)
        out.append('"')
        return "".join(out).encode("utf-8")
    if isinstance(val, dict):
        for k in val.keys():
            if not isinstance(k, str):
                raise RejectError(EXIT_GATE, "MALFORMED", "object key must be string")
        parts: List[bytes] = []
        for k in sorted(val.keys()):
            kb = canonical_encode(k, depth + 1)
            vb = canonical_encode(val[k], depth + 1)
            parts.append(kb + b":" + vb)
        return b"{" + b",".join(parts) + b"}"
    if isinstance(val, list):
        return b"[" + b",".join(canonical_encode(x, depth + 1) for x in val) + b"]"
    raise RejectError(EXIT_GATE, "MALFORMED", f"unsupported type for canonical_encode: {type(val).__name__}")


def _scan_text_prohibitions(val: Any, path: str = "root") -> None:
    """Recursively reject raw pixels, base64 blobs, HTML, raw AX trees, secrets, and orchestration."""
    if isinstance(val, (bytes, bytearray)):
        raise RejectError(EXIT_GATE, "RAW_PIXELS_OR_BINARY", f"raw binary/pixel bytes at {path}")
    if isinstance(val, str):
        if "\x00" in val:
            raise RejectError(EXIT_GATE, "RAW_PIXELS_OR_BINARY", f"NUL/binary signature in text at {path}")
        if val.startswith("XWD\x00") or "srf-fbread xid=" in val:
            raise RejectError(EXIT_GATE, "RAW_PIXELS_OR_BINARY", f"raw XWD frame payload at {path}")
        if _BASE64_IMAGE_PATTERN.search(val) or _CONTINUOUS_BASE64_BLOB_RE.match(val):
            raise RejectError(EXIT_GATE, "BASE64_IMAGE_BLOB", f"base64 image/binary blob rejected at {path}")
        if _HTML_PATTERN.search(val):
            raise RejectError(EXIT_GATE, "HTML_OR_RAW_AX_FOR_MODEL", f"HTML markup destined for model rejected at {path}")
        if _RAW_AX_PATTERN.search(val):
            raise RejectError(EXIT_GATE, "HTML_OR_RAW_AX_FOR_MODEL", f"raw AX tree destined for model rejected at {path}")
        if _SECRET_OR_CMDLINE_PATTERN.search(val):
            raise RejectError(EXIT_GATE, "SECRET_OR_ENV_LEAK", f"credential/token/env/cmdline content rejected at {path}")
        if _ORCHESTRATION_PATTERN.search(val):
            raise RejectError(EXIT_GATE, "PASSIVE_DATA_VIOLATION", f"active orchestration command rejected at {path}")
        return
    if isinstance(val, dict):
        for k, v in val.items():
            k_lower = k.lower()
            if k_lower in _FORBIDDEN_KEY_NAMES:
                if (
                    "pixel" in k_lower
                    or "rgb" in k_lower
                    or "xwd" in k_lower
                    or "raw_frame" in k_lower
                    or "framebuffer" in k_lower
                ):
                    raise RejectError(EXIT_GATE, "RAW_PIXELS_OR_BINARY", f"forbidden raw pixel field {k!r} at {path}")
                if "base64" in k_lower:
                    raise RejectError(EXIT_GATE, "BASE64_IMAGE_BLOB", f"forbidden base64 field {k!r} at {path}")
                if k_lower in ("credentials", "env_dump", "proc_cmdline"):
                    raise RejectError(EXIT_GATE, "SECRET_OR_ENV_LEAK", f"forbidden secret/env field {k!r} at {path}")
                raise RejectError(EXIT_GATE, "HTML_OR_RAW_AX_FOR_MODEL", f"forbidden HTML/AX/model field {k!r} at {path}")
            _scan_text_prohibitions(k, f"{path}.{k}")
            _scan_text_prohibitions(v, f"{path}.{k}")
        return
    if isinstance(val, list):
        if len(val) >= 16 and all(isinstance(x, int) and not isinstance(x, bool) for x in val):
            raise RejectError(EXIT_GATE, "RAW_PIXELS_OR_BINARY", f"raw numeric pixel array rejected at {path}")
        for idx, item in enumerate(val):
            _scan_text_prohibitions(item, f"{path}[{idx}]")


def _require_closed_keys(obj: Any, required: Set[str], context_name: str, schema_code: int = EXIT_GATE) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise RejectError(schema_code, "MALFORMED", f"{context_name} must be a JSON object")
    keys = set(obj.keys())
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        raise RejectError(schema_code, "MISSING_KEY", f"{context_name} missing required keys: {', '.join(missing)}")
    if extra:
        raise RejectError(schema_code, "UNKNOWN_KEY", f"{context_name} has unknown keys: {', '.join(extra)}")
    return obj


def _require_nonempty_str(val: Any, field_name: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise RejectError(EXIT_GATE, "MALFORMED", f"{field_name} must be a non-empty string")
    return val


def _require_nonneg_int(val: Any, field_name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise RejectError(EXIT_GATE, "MALFORMED", f"{field_name} must be a non-negative integer")
    return val


def _require_pos_int(val: Any, field_name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise RejectError(EXIT_GATE, "MISSING_BOUNDS", f"{field_name} must be a positive integer bound")
    return val


@dataclass(frozen=True)
class SupplyBundle:
    """Supplied text bytes and reference digests verified against carrier refs, joins, and session scope.

    Raw frames remain in bounded RAM outside this validator and are never carried here.
    """

    session_id: str
    segment_id: str
    tab_id: str
    target: str
    state_bytes: bytes
    ui_diff_bytes: bytes
    t2_trace_bytes: bytes
    ax_sha256: str
    ax_stable_sha256: str
    frame_sha256: Optional[str] = None
    renderer_status: str = "REFERENCED"
    verified_labels_proof: Optional[str] = None


class SupplyPort:
    """Narrow local fakeable port for verifying supplied bytes, session scope, and replay protection."""

    def resolve_supply(self, session_id: str, segment_id: str) -> SupplyBundle:
        raise NotImplementedError

    def check_and_record_replay(self, session_id: str, segment_id: str, carrier_digest: str, max_seq: int) -> None:
        raise NotImplementedError


class InMemorySupplyPort(SupplyPort):
    """In-memory fakeable port for offline unit tests (no disk frames, no network)."""

    def __init__(
        self,
        allow_idempotent_redelivery: bool = False,
        clock: Optional[FakeClock] = None,
    ) -> None:
        self._bundles: Dict[Tuple[str, str], SupplyBundle] = {}
        self._committed_segments: Dict[Tuple[str, str], Tuple[str, int, int]] = {}
        self._allow_idempotent_redelivery = allow_idempotent_redelivery
        self._clock = clock or FakeClock()

    def register_bundle(self, bundle: SupplyBundle) -> None:
        self._bundles[(bundle.session_id, bundle.segment_id)] = bundle

    def resolve_supply(self, session_id: str, segment_id: str) -> SupplyBundle:
        key = (session_id, segment_id)
        if key not in self._bundles:
            raise RejectError(
                EXIT_UNAVAILABLE,
                "UNAVAILABLE_PREREQUISITE",
                f"supplied byte bundle absent for session={session_id!r} segment={segment_id!r}",
            )
        return self._bundles[key]

    def check_and_record_replay(self, session_id: str, segment_id: str, carrier_digest: str, max_seq: int) -> None:
        key = (session_id, segment_id)
        now = self._clock.now_ns()
        if key in self._committed_segments:
            prev_digest, prev_seq, _ = self._committed_segments[key]
            if prev_digest != carrier_digest or prev_seq != max_seq:
                raise RejectError(
                    EXIT_QUARANTINE,
                    "QUARANTINE_MUTATED_REPLAY",
                    f"mutated replay for committed session={session_id!r} segment={segment_id!r}",
                )
            if not self._allow_idempotent_redelivery:
                raise RejectError(
                    EXIT_GATE,
                    "REPLAY_DETECTED",
                    f"duplicate replay rejected for session={session_id!r} segment={segment_id!r}",
                )
            return
        self._committed_segments[key] = (carrier_digest, max_seq, now)


class DummyRendererPort:
    """Local fakeable port representing a dummy renderer that never claims a frame."""

    @staticmethod
    def frame_metadata() -> Dict[str, Any]:
        return {
            "status": GRAB_UNAVAILABLE,
            "frame_sha256": None,
            "width": 0,
            "height": 0,
            "format": "NONE",
            "ram_only": True,
        }


@dataclass
class ValidationResult:
    """Structured result of validating a training-signal carrier."""

    valid: bool
    promotable: bool
    carrier_sha256: str
    state_sha256: str
    diff_sha256: str
    t2_trace_sha256: str
    absent_facts: List[str] = field(default_factory=list)
    renderer_status: str = "REFERENCED"


def validate_carrier(
    carrier_input: Union[str, bytes, Mapping[str, Any]],
    *,
    supply_port: Optional[SupplyPort] = None,
    require_promotion: bool = True,
) -> ValidationResult:
    """Validate a text-only training-signal carrier against structural, hash, join, and safety rules."""
    raw_bytes: Optional[bytes] = None
    if isinstance(carrier_input, (str, bytes)):
        if isinstance(carrier_input, bytes):
            raw_bytes = carrier_input
        else:
            raw_bytes = carrier_input.encode("utf-8")
        doc = strict_json_parse(carrier_input)
    elif isinstance(carrier_input, Mapping):
        doc_copy = dict(carrier_input)
        _validate_integer_json_tree(doc_copy)
        raw_bytes = canonical_encode(doc_copy)
        doc = strict_json_parse(raw_bytes)
    else:
        raise RejectError(EXIT_INPUT, "INVALID_INPUT", "carrier input must be JSON str, bytes, or mapping")

    if not isinstance(doc, dict):
        raise RejectError(EXIT_INPUT, "INVALID_INPUT", "carrier root must be a JSON object")

    if doc.get("schema") != CARRIER_SCHEMA:
        raise RejectError(
            EXIT_INPUT,
            "INVALID_SCHEMA",
            f"expected schema {CARRIER_SCHEMA!r}, got {doc.get('schema')!r}",
        )

    _require_closed_keys(doc, REQUIRED_CARRIER_KEYS, "carrier")
    _scan_text_prohibitions(doc, "carrier")

    # 1. Versions binding
    versions = _require_closed_keys(doc["versions"], REQUIRED_VERSIONS_KEYS, "versions")
    repo_commit = _require_nonempty_str(versions["repository_commit"], "versions.repository_commit")
    if not _COMMIT_RE.match(repo_commit):
        raise RejectError(
            EXIT_GATE,
            "INVALID_VERSION_PIN",
            "versions.repository_commit must be a 40-character lowercase hex SHA-1 commit",
        )
    _require_nonempty_str(versions["engine_version"], "versions.engine_version")
    _require_nonempty_str(versions["source_version"], "versions.source_version")
    _require_nonempty_str(versions["tool_version"], "versions.tool_version")

    # 2. Seed, ticks, target, tab, session, segment binding
    _require_nonneg_int(doc["seed"], "seed")
    req_ticks = _require_nonneg_int(doc["requested_ticks"], "requested_ticks")
    act_ticks = _require_nonneg_int(doc["actual_ticks"], "actual_ticks")
    if req_ticks == 0:
        raise RejectError(EXIT_GATE, "MISSING_BOUNDS", "requested_ticks must be > 0")
    if act_ticks > req_ticks:
        raise RejectError(
            EXIT_GATE,
            "TICK_BOUND_EXCEEDED",
            f"actual_ticks ({act_ticks}) exceeds requested_ticks ({req_ticks})",
        )

    target = _require_nonempty_str(doc["target"], "target")
    tab_id = _require_nonempty_str(doc["tab_id"], "tab_id")
    session_id = _require_nonempty_str(doc["session_id"], "session_id")
    segment_id = _require_nonempty_str(doc["segment_id"], "segment_id")

    # 3. Acquisition provenance, AUSENTE tracking, and byte bounds
    prov = _require_closed_keys(doc["acquisition_provenance"], REQUIRED_PROVENANCE_KEYS, "acquisition_provenance")
    source = _require_nonempty_str(prov["source"], "acquisition_provenance.source")
    qualification = _require_nonempty_str(prov["source_qualification"], "acquisition_provenance.source_qualification")
    retention = _require_nonempty_str(prov["capture_retention"], "acquisition_provenance.capture_retention")
    renderer_mode = _require_nonempty_str(prov["renderer_mode"], "acquisition_provenance.renderer_mode")

    if renderer_mode not in ALLOWED_RENDERER_MODES:
        raise RejectError(EXIT_GATE, "INVALID_PROVENANCE", f"unsupported renderer_mode: {renderer_mode!r}")
    if qualification not in ALLOWED_QUALIFICATIONS:
        raise RejectError(EXIT_GATE, "INVALID_PROVENANCE", f"unsupported source_qualification: {qualification!r}")
    if retention not in ALLOWED_RETENTIONS:
        raise RejectError(EXIT_GATE, "INVALID_PROVENANCE", f"unsupported capture_retention: {retention!r}")

    absent_facts: List[str] = []
    if source == AUSENTE:
        absent_facts.append(f"source:{AUSENTE}")
    if qualification == AUSENTE:
        absent_facts.append(f"source_qualification:{AUSENTE}")
    if retention == AUSENTE:
        absent_facts.append(f"capture_retention:{AUSENTE}")

    raw_bounds = prov["byte_bounds"]
    bounds_dict: Optional[Dict[str, int]] = None
    if raw_bounds == AUSENTE or raw_bounds is None:
        absent_facts.append(f"byte_bounds:{AUSENTE}")
    else:
        b_obj = _require_closed_keys(raw_bounds, REQUIRED_BYTE_BOUNDS_KEYS, "acquisition_provenance.byte_bounds")
        bounds_dict = {
            k: _require_pos_int(b_obj[k], f"byte_bounds.{k}")
            for k in REQUIRED_BYTE_BOUNDS_KEYS
        }

    if absent_facts and require_promotion:
        raise RejectError(
            EXIT_GATE,
            "AUSENTE_BLOCKS_PROMOTION",
            f"missing or AUSENTE facts block promotion: {', '.join(absent_facts)}",
        )

    # 4. Canonical integer-JSON STATE and explicit gameplay_state_hash <-> state_sha256 mapping
    state_obj = doc["state"]
    if not isinstance(state_obj, dict) or not state_obj:
        raise RejectError(EXIT_GATE, "INVALID_STATE", "state must be a non-empty canonical integer-JSON object")
    canonical_state_bytes = canonical_encode(state_obj)
    computed_state_sha = sha256_hex(canonical_state_bytes)

    state_sha256 = doc["state_sha256"]
    gameplay_state_hash = doc["gameplay_state_hash"]
    if not is_sha256_hex(state_sha256) or not is_sha256_hex(gameplay_state_hash):
        raise RejectError(EXIT_GATE, "INVALID_HASH", "state_sha256 and gameplay_state_hash must be 64-hex SHA-256")
    if gameplay_state_hash != state_sha256:
        raise RejectError(
            EXIT_GATE,
            "STATE_HASH_MISMATCH",
            "gameplay_state_hash must map explicitly to state_sha256",
        )
    if computed_state_sha != state_sha256:
        raise RejectError(
            EXIT_GATE,
            "STATE_HASH_MISMATCH",
            f"canonical integer-JSON STATE digest {computed_state_sha} != state_sha256 {state_sha256}",
        )

    # 5. Distinct ax_sha256, ax_stable_sha256, and distinctness from gameplay state hash
    ax_sha256 = doc["ax_sha256"]
    ax_stable_sha256 = doc["ax_stable_sha256"]
    if not is_sha256_hex(ax_sha256) or not is_sha256_hex(ax_stable_sha256):
        raise RejectError(EXIT_GATE, "INVALID_HASH", "ax_sha256 and ax_stable_sha256 must be 64-hex SHA-256")
    if ax_sha256 == ax_stable_sha256:
        raise RejectError(
            EXIT_GATE,
            "AX_HASH_COLLISION",
            "ax_sha256 and ax_stable_sha256 must be distinct",
        )
    if state_sha256 in (ax_sha256, ax_stable_sha256):
        raise RejectError(
            EXIT_GATE,
            "GAMEPLAY_AX_HASH_COLLISION",
            "gameplay hashes (state_sha256/gameplay_state_hash) and AX hashes must be distinct",
        )

    # 6. UI-tree diff bound by diff_sha256
    ui_diff = _require_closed_keys(doc["ui_tree_diff"], REQUIRED_UI_DIFF_KEYS, "ui_tree_diff")
    if ui_diff["format"] != "canonical-ui-diff-v1":
        raise RejectError(EXIT_GATE, "INVALID_UI_DIFF", "ui_tree_diff.format must be 'canonical-ui-diff-v1'")
    if (
        ui_diff["session_id"] != session_id
        or ui_diff["segment_id"] != segment_id
        or ui_diff["tab_id"] != tab_id
        or ui_diff["target"] != target
    ):
        raise RejectError(
            EXIT_GATE,
            "WRONG_SESSION_JOIN",
            "ui_tree_diff scope does not match carrier session/segment/tab/target",
        )
    ops = ui_diff["ops"]
    if not isinstance(ops, list) or not ops:
        raise RejectError(EXIT_GATE, "INVALID_UI_DIFF", "ui_tree_diff.ops must be a non-empty list")
    for idx, op in enumerate(ops):
        op_obj = _require_closed_keys(op, REQUIRED_UI_DIFF_OP_KEYS, f"ui_tree_diff.ops[{idx}]")
        if op_obj["op"] not in ("add", "remove", "replace"):
            raise RejectError(EXIT_GATE, "INVALID_UI_DIFF", f"unsupported diff op {op_obj['op']!r}")
        _require_nonempty_str(op_obj["node_id"], f"ui_tree_diff.ops[{idx}].node_id")
        _require_nonempty_str(op_obj["role"], f"ui_tree_diff.ops[{idx}].role")
        _require_nonempty_str(op_obj["summary"], f"ui_tree_diff.ops[{idx}].summary")

    canonical_diff_bytes = canonical_encode(ui_diff)
    computed_diff_sha = sha256_hex(canonical_diff_bytes)
    diff_sha256 = doc["diff_sha256"]
    if not is_sha256_hex(diff_sha256) or computed_diff_sha != diff_sha256:
        raise RejectError(
            EXIT_GATE,
            "DIFF_HASH_MISMATCH",
            f"ui_tree_diff digest {computed_diff_sha} != diff_sha256 {diff_sha256!r}",
        )

    # 7. Frame metadata referenced by SHA only & dummy renderer GRAB_UNAVAILABLE rule
    frame_meta = _require_closed_keys(doc["frame_metadata"], REQUIRED_FRAME_METADATA_KEYS, "frame_metadata")
    if frame_meta["ram_only"] is not True:
        raise RejectError(
            EXIT_GATE,
            "FRAME_STORAGE_VIOLATION",
            "frame_metadata.ram_only must be True; raw frames must remain in bounded RAM outside validator",
        )
    frame_status = frame_meta["status"]
    frame_sha = frame_meta["frame_sha256"]
    frame_w = _require_nonneg_int(frame_meta["width"], "frame_metadata.width")
    frame_h = _require_nonneg_int(frame_meta["height"], "frame_metadata.height")
    frame_fmt = frame_meta["format"]
    if frame_fmt not in ALLOWED_FRAME_FORMATS:
        raise RejectError(EXIT_GATE, "INVALID_FRAME_METADATA", f"unsupported frame format {frame_fmt!r}")

    if renderer_mode == "dummy":
        if (
            frame_status != GRAB_UNAVAILABLE
            or frame_sha is not None
            or frame_w != 0
            or frame_h != 0
            or frame_fmt != "NONE"
        ):
            raise RejectError(
                EXIT_GATE,
                "DUMMY_CLAIMED_FRAME",
                "dummy renderer must say GRAB_UNAVAILABLE and never claim a frame",
            )
        expected_trace_frame_ref = GRAB_UNAVAILABLE
    else:
        if frame_status == GRAB_UNAVAILABLE:
            if frame_sha is not None or frame_w != 0 or frame_h != 0 or frame_fmt != "NONE":
                raise RejectError(
                    EXIT_GATE,
                    "INVALID_FRAME_METADATA",
                    "GRAB_UNAVAILABLE frame_metadata must have null frame_sha256 and zero geometry",
                )
            expected_trace_frame_ref = GRAB_UNAVAILABLE
        elif frame_status == "REFERENCED":
            if not is_sha256_hex(frame_sha):
                raise RejectError(
                    EXIT_GATE,
                    "INVALID_FRAME_METADATA",
                    "REFERENCED frame_metadata requires a 64-hex frame_sha256",
                )
            if frame_w <= 0 or frame_h <= 0 or frame_fmt == "NONE":
                raise RejectError(
                    EXIT_GATE,
                    "INVALID_FRAME_METADATA",
                    "REFERENCED frame_metadata requires positive width/height and non-NONE format",
                )
            if frame_sha in (state_sha256, ax_sha256, ax_stable_sha256, diff_sha256):
                raise RejectError(
                    EXIT_GATE,
                    "HASH_DOMAIN_COLLISION",
                    "frame_sha256 must be distinct from state, AX, and diff hashes",
                )
            expected_trace_frame_ref = frame_sha
        else:
            raise RejectError(EXIT_GATE, "INVALID_FRAME_METADATA", f"unknown frame status {frame_status!r}")

    # 8. Validated T2 trace bytes with monotonic tick and seq, explicit admission & executor verdict
    t2_trace = doc["t2_trace"]
    if not isinstance(t2_trace, list) or not t2_trace:
        raise RejectError(EXIT_GATE, "INVALID_T2_TRACE", "t2_trace must be a non-empty list of events")

    prev_tick = -1
    prev_seq = 0
    seen_seqs: Set[int] = set()
    for idx, ev in enumerate(t2_trace):
        ev_obj = _require_closed_keys(ev, REQUIRED_TRACE_EVENT_KEYS, f"t2_trace[{idx}]")
        if (
            ev_obj["session_id"] != session_id
            or ev_obj["segment_id"] != segment_id
            or ev_obj["tab_id"] != tab_id
            or ev_obj["target"] != target
        ):
            raise RejectError(
                EXIT_GATE,
                "WRONG_SESSION_JOIN",
                f"t2_trace[{idx}] scope mismatch against carrier session/segment/tab/target",
            )
        tick = _require_nonneg_int(ev_obj["tick"], f"t2_trace[{idx}].tick")
        seq = _require_nonneg_int(ev_obj["seq"], f"t2_trace[{idx}].seq")
        if seq <= 0:
            raise RejectError(EXIT_GATE, "NON_MONOTONIC_TRACE", f"t2_trace[{idx}].seq must be >= 1")
        if seq in seen_seqs or seq <= prev_seq:
            raise RejectError(
                EXIT_GATE,
                "NON_MONOTONIC_TRACE",
                f"t2_trace[{idx}].seq ({seq}) is not strictly monotonic after {prev_seq}",
            )
        if tick < prev_tick or tick > act_ticks:
            raise RejectError(
                EXIT_GATE,
                "NON_MONOTONIC_TRACE",
                f"t2_trace[{idx}].tick ({tick}) violates monotonic range [{prev_tick}, {act_ticks}]",
            )
        prev_tick = tick
        prev_seq = seq
        seen_seqs.add(seq)

        _require_nonempty_str(ev_obj["action"], f"t2_trace[{idx}].action")
        adm = ev_obj["admission_verdict"]
        exe = ev_obj["executor_verdict"]
        if adm not in ALLOWED_ADMISSION_VERDICTS:
            raise RejectError(
                EXIT_GATE,
                "INVALID_TRACE_VERDICT",
                f"t2_trace[{idx}].admission_verdict {adm!r} invalid",
            )
        if exe not in ALLOWED_EXECUTOR_VERDICTS:
            raise RejectError(
                EXIT_GATE,
                "INVALID_TRACE_VERDICT",
                f"t2_trace[{idx}].executor_verdict {exe!r} invalid",
            )
        if adm != "ADMITTED" and exe == "APPLIED":
            raise RejectError(
                EXIT_GATE,
                "INVALID_TRACE_VERDICT",
                f"t2_trace[{idx}] cannot have executor_verdict APPLIED when admission_verdict is {adm!r}",
            )
        if not is_sha256_hex(ev_obj["state_sha256"]) or not is_sha256_hex(ev_obj["diff_sha256"]):
            raise RejectError(
                EXIT_GATE,
                "INVALID_TRACE_REF",
                f"t2_trace[{idx}] state_sha256 and diff_sha256 must be 64-hex digests",
            )
        if ev_obj["frame_ref"] != expected_trace_frame_ref:
            if renderer_mode == "dummy":
                raise RejectError(
                    EXIT_GATE,
                    "DUMMY_CLAIMED_FRAME",
                    f"t2_trace[{idx}].frame_ref must be {GRAB_UNAVAILABLE!r} for dummy renderer",
                )
            raise RejectError(
                EXIT_GATE,
                "FRAME_REF_MISMATCH",
                f"t2_trace[{idx}].frame_ref {ev_obj['frame_ref']!r} != expected {expected_trace_frame_ref!r}",
            )

    if t2_trace[-1]["state_sha256"] != state_sha256 or t2_trace[-1]["diff_sha256"] != diff_sha256:
        raise RejectError(
            EXIT_GATE,
            "TRACE_TERMINAL_REF_MISMATCH",
            "terminal t2_trace event must bind to carrier state_sha256 and diff_sha256",
        )

    canonical_trace_bytes = canonical_encode(t2_trace)
    computed_trace_sha = sha256_hex(canonical_trace_bytes)
    t2_trace_sha256 = doc["t2_trace_sha256"]
    if not is_sha256_hex(t2_trace_sha256) or computed_trace_sha != t2_trace_sha256:
        raise RejectError(
            EXIT_GATE,
            "TRACE_HASH_MISMATCH",
            f"t2_trace digest {computed_trace_sha} != t2_trace_sha256 {t2_trace_sha256!r}",
        )

    # 9. Claims and labels validation (unproven reproduced/verified labels, real capture, learning success)
    claims = _require_closed_keys(doc["claims"], REQUIRED_CLAIMS_KEYS, "claims")
    if not isinstance(claims["synthetic"], bool):
        raise RejectError(EXIT_GATE, "MALFORMED", "claims.synthetic must be a boolean")
    if not isinstance(claims["real_capture"], bool):
        raise RejectError(EXIT_GATE, "MALFORMED", "claims.real_capture must be a boolean")
    if not isinstance(claims["learning_success"], bool):
        raise RejectError(EXIT_GATE, "MALFORMED", "claims.learning_success must be a boolean")
    if claims["learning_success"]:
        raise RejectError(
            EXIT_GATE,
            "UNPROVEN_CLAIM",
            "carrier must not claim model learning_success",
        )
    if claims["synthetic"] and claims["real_capture"]:
        raise RejectError(
            EXIT_GATE,
            "UNPROVEN_CLAIM",
            "synthetic fixture must not claim a real_capture",
        )
    if claims["real_capture"] and qualification != "qualified":
        raise RejectError(
            EXIT_GATE,
            "UNPROVEN_CLAIM",
            "real_capture claim requires qualified source_qualification",
        )

    labels = claims["labels"]
    if not isinstance(labels, list) or not all(isinstance(x, str) and x.strip() for x in labels):
        raise RejectError(EXIT_GATE, "MALFORMED", "claims.labels must be a list of non-empty strings")
    if len(set(labels)) != len(labels):
        raise RejectError(EXIT_GATE, "MALFORMED", "claims.labels must not contain duplicate labels")

    if "operator_override" in labels:
        raise RejectError(
            EXIT_OPERATOR,
            "OPERATOR_AUTHORIZATION_REQUIRED",
            "operator_override label requires explicit operator capability authorization",
        )

    # 10. Byte bounds enforcement
    canonical_carrier_bytes = canonical_encode(doc)
    carrier_sha256 = sha256_hex(canonical_carrier_bytes)
    effective_wire_len = len(raw_bytes) if raw_bytes is not None else len(canonical_carrier_bytes)

    if bounds_dict is not None:
        if max(effective_wire_len, len(canonical_carrier_bytes)) > bounds_dict["max_carrier_bytes"]:
            raise RejectError(
                EXIT_GATE,
                "BYTE_BOUND_EXCEEDED",
                "carrier byte length exceeds max_carrier_bytes",
            )
        if len(canonical_state_bytes) > bounds_dict["max_state_bytes"]:
            raise RejectError(
                EXIT_GATE,
                "BYTE_BOUND_EXCEEDED",
                "canonical state bytes exceed max_state_bytes",
            )
        if len(canonical_diff_bytes) > bounds_dict["max_diff_bytes"]:
            raise RejectError(
                EXIT_GATE,
                "BYTE_BOUND_EXCEEDED",
                "canonical ui_tree_diff bytes exceed max_diff_bytes",
            )
        if len(canonical_trace_bytes) > bounds_dict["max_trace_bytes"]:
            raise RejectError(
                EXIT_GATE,
                "BYTE_BOUND_EXCEEDED",
                "canonical t2_trace bytes exceed max_trace_bytes",
            )

    # 11. Verify supplied bytes against refs, joins, session scope, and replay via SupplyPort
    verified_proof: Optional[str] = None
    if supply_port is not None:
        bundle = supply_port.resolve_supply(session_id, segment_id)
        if (
            bundle.session_id != session_id
            or bundle.segment_id != segment_id
            or bundle.tab_id != tab_id
            or bundle.target != target
        ):
            raise RejectError(
                EXIT_GATE,
                "WRONG_SESSION_JOIN",
                "supplied bundle scope does not match carrier session/segment/tab/target",
            )
        _ = strict_json_parse(bundle.state_bytes)
        _ = strict_json_parse(bundle.ui_diff_bytes)
        _ = strict_json_parse(bundle.t2_trace_bytes)
        if sha256_hex(bundle.state_bytes) != state_sha256 or bundle.state_bytes != canonical_state_bytes:
            raise RejectError(
                EXIT_QUARANTINE,
                "SUPPLY_REF_MISMATCH",
                "supplied state_bytes do not match carrier state_sha256",
            )
        if sha256_hex(bundle.ui_diff_bytes) != diff_sha256 or bundle.ui_diff_bytes != canonical_diff_bytes:
            raise RejectError(
                EXIT_QUARANTINE,
                "SUPPLY_REF_MISMATCH",
                "supplied ui_diff_bytes do not match carrier diff_sha256",
            )
        if sha256_hex(bundle.t2_trace_bytes) != t2_trace_sha256 or bundle.t2_trace_bytes != canonical_trace_bytes:
            raise RejectError(
                EXIT_QUARANTINE,
                "SUPPLY_REF_MISMATCH",
                "supplied t2_trace_bytes do not match carrier t2_trace_sha256",
            )
        if bundle.ax_sha256 != ax_sha256 or bundle.ax_stable_sha256 != ax_stable_sha256:
            raise RejectError(
                EXIT_GATE,
                "SUPPLY_AX_MISMATCH",
                "supplied AX digests do not match carrier ax_sha256 / ax_stable_sha256",
            )
        if bundle.renderer_status != frame_status or bundle.frame_sha256 != frame_sha:
            raise RejectError(
                EXIT_GATE,
                "SUPPLY_FRAME_MISMATCH",
                "supplied frame metadata reference does not match carrier frame_metadata",
            )
        verified_proof = bundle.verified_labels_proof
        supply_port.check_and_record_replay(session_id, segment_id, carrier_sha256, prev_seq)

    for restricted_label in ("reproduced", "verified"):
        if restricted_label in labels:
            if claims["synthetic"] or not verified_proof:
                raise RejectError(
                    EXIT_GATE,
                    "UNPROVEN_LABEL",
                    f"label {restricted_label!r} is unproven and forbidden without host verification proof",
                )

    return ValidationResult(
        valid=True,
        promotable=(len(absent_facts) == 0),
        carrier_sha256=carrier_sha256,
        state_sha256=state_sha256,
        diff_sha256=diff_sha256,
        t2_trace_sha256=t2_trace_sha256,
        absent_facts=absent_facts,
        renderer_status=frame_status,
    )


def make_positive_synthetic_fixture(*, dummy_renderer: bool = False) -> Tuple[Dict[str, Any], SupplyBundle]:
    """Build a deterministic, valid synthetic training-signal carrier and its matching SupplyBundle.

    Validates synthetic metadata without claiming a real capture or learning success.
    """
    session_id = "sess-w8-train-001"
    segment_id = "seg-0001"
    tab_id = "tab-main-0"
    target = "srf-target-grid-v1"

    state: Dict[str, Any] = {
        "cursor_x": 120,
        "cursor_y": 240,
        "inventory_count": 3,
        "level_id": 2,
        "score": 450,
        "step_completed": True,
        "tick": 12,
    }
    state_bytes = canonical_encode(state)
    state_sha = sha256_hex(state_bytes)

    ui_diff: Dict[str, Any] = {
        "format": "canonical-ui-diff-v1",
        "session_id": session_id,
        "segment_id": segment_id,
        "tab_id": tab_id,
        "target": target,
        "ops": [
            {
                "op": "replace",
                "node_id": "hud-score",
                "role": "label",
                "summary": "score: 400 -> 450",
            },
            {
                "op": "add",
                "node_id": "badge-step-12",
                "role": "status",
                "summary": "step_completed=true",
            },
        ],
    }
    diff_bytes = canonical_encode(ui_diff)
    diff_sha = sha256_hex(diff_bytes)

    ax_sha = sha256_hex(b"w8-ax-raw-digest-seed:sess-w8-train-001:seg-0001:tick-12")
    ax_stable_sha = sha256_hex(b"w8-ax-stable-digest-seed:sess-w8-train-001:seg-0001:tick-12")

    if dummy_renderer:
        renderer_mode = "dummy"
        frame_meta = DummyRendererPort.frame_metadata()
        trace_frame_ref = GRAB_UNAVAILABLE
        bundle_frame_sha: Optional[str] = None
        bundle_renderer_status = GRAB_UNAVAILABLE
    else:
        renderer_mode = "synthetic"
        bundle_frame_sha = sha256_hex(b"w8-ram-frame-ref:sess-w8-train-001:seg-0001:seq-2")
        bundle_renderer_status = "REFERENCED"
        frame_meta = {
            "status": "REFERENCED",
            "frame_sha256": bundle_frame_sha,
            "width": 1280,
            "height": 900,
            "format": "RGB24",
            "ram_only": True,
        }
        trace_frame_ref = bundle_frame_sha

    prev_state_sha = sha256_hex(canonical_encode({**state, "score": 400, "tick": 6}))
    prev_diff_sha = sha256_hex(b"w8-diff-prev:sess-w8-train-001:seg-0001:seq-1")

    t2_trace: List[Dict[str, Any]] = [
        {
            "session_id": session_id,
            "segment_id": segment_id,
            "tab_id": tab_id,
            "target": target,
            "tick": 6,
            "seq": 1,
            "action": "key_press:ArrowRight",
            "admission_verdict": "ADMITTED",
            "executor_verdict": "APPLIED",
            "state_sha256": prev_state_sha,
            "diff_sha256": prev_diff_sha,
            "frame_ref": trace_frame_ref,
        },
        {
            "session_id": session_id,
            "segment_id": segment_id,
            "tab_id": tab_id,
            "target": target,
            "tick": 12,
            "seq": 2,
            "action": "click:hud-confirm",
            "admission_verdict": "ADMITTED",
            "executor_verdict": "APPLIED",
            "state_sha256": state_sha,
            "diff_sha256": diff_sha,
            "frame_ref": trace_frame_ref,
        },
    ]
    trace_bytes = canonical_encode(t2_trace)
    trace_sha = sha256_hex(trace_bytes)

    carrier: Dict[str, Any] = {
        "schema": CARRIER_SCHEMA,
        "versions": {
            "repository_commit": "1372a80c59c8d3384c64adac49a735f6eb4bffc4",
            "engine_version": "srf-engine-0.4.0-synthetic",
            "source_version": "w8-training-carrier-v1",
            "tool_version": "w8-validator-stdlib-1.0",
        },
        "seed": 424242,
        "requested_ticks": 16,
        "actual_ticks": 12,
        "target": target,
        "tab_id": tab_id,
        "session_id": session_id,
        "segment_id": segment_id,
        "acquisition_provenance": {
            "source": "synthetic-fixture",
            "source_qualification": "synthetic_offline",
            "capture_retention": "none_synthetic" if dummy_renderer else "ram_bounded",
            "renderer_mode": renderer_mode,
            "byte_bounds": copy.deepcopy(TARGET_DEFAULT_BYTE_BOUNDS),
        },
        "state": state,
        "gameplay_state_hash": state_sha,
        "state_sha256": state_sha,
        "ax_sha256": ax_sha,
        "ax_stable_sha256": ax_stable_sha,
        "ui_tree_diff": ui_diff,
        "diff_sha256": diff_sha,
        "t2_trace": t2_trace,
        "t2_trace_sha256": trace_sha,
        "frame_metadata": frame_meta,
        "claims": {
            "synthetic": True,
            "real_capture": False,
            "learning_success": False,
            "labels": ["synthetic_fixture", "offline_unit"],
        },
    }

    bundle = SupplyBundle(
        session_id=session_id,
        segment_id=segment_id,
        tab_id=tab_id,
        target=target,
        state_bytes=state_bytes,
        ui_diff_bytes=diff_bytes,
        t2_trace_bytes=trace_bytes,
        ax_sha256=ax_sha,
        ax_stable_sha256=ax_stable_sha,
        frame_sha256=bundle_frame_sha,
        renderer_status=bundle_renderer_status,
        verified_labels_proof=None,
    )
    return carrier, bundle


class _ContractArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises RejectError(EXIT_INPUT, ...) instead of exiting with code 1 or 2."""

    def error(self, message: str) -> None:
        raise RejectError(EXIT_INPUT, "CLI_USAGE_ERROR", message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        if status == 0:
            raise SystemExit(EXIT_OK)
        raise RejectError(EXIT_INPUT, "CLI_EXIT", message or "argument parser exit")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stderr: Optional[io.TextIOBase] = None,
    read_file: Optional[Callable[[Path], bytes]] = None,
) -> int:
    """CLI entry point enforcing the W8 exit-code contract (0, 2, 3, 4, 5, 6, 7; never 1)."""
    err_stream = stderr if stderr is not None else sys.stderr
    reader = read_file if read_file is not None else (lambda p: p.read_bytes())

    parser = _ContractArgumentParser(
        prog="w8-training-validator",
        description="Validate a proposed local W8 text-only training-signal carrier.",
        add_help=True,
    )
    parser.add_argument(
        "--carrier",
        type=str,
        default=None,
        help="Path to a UTF-8 JSON training-signal carrier file.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Validate the built-in synthetic fixture and exit 0.",
    )
    parser.add_argument(
        "--allow-ausente-unpromoted",
        action="store_true",
        help="Inspect carrier allowing AUSENTE fields while recording promotion_blocked.",
    )
    parser.add_argument(
        "--require-supply-port",
        action="store_true",
        help="Require an external supply bundle (exits 4 if not provided).",
    )

    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if args.selftest and args.carrier is not None:
            raise RejectError(EXIT_INPUT, "CLI_USAGE_ERROR", "--selftest and --carrier are mutually exclusive")
        if not args.selftest and args.carrier is None:
            raise RejectError(EXIT_INPUT, "CLI_USAGE_ERROR", "one of --selftest or --carrier is required")

        if args.selftest:
            carrier, bundle = make_positive_synthetic_fixture()
            port = InMemorySupplyPort()
            port.register_bundle(bundle)
            validate_carrier(carrier, supply_port=port, require_promotion=not args.allow_ausente_unpromoted)
            return EXIT_OK

        carrier_path = Path(args.carrier)
        try:
            raw_bytes = reader(carrier_path)
        except OSError as exc:
            raise RejectError(EXIT_SETUP, "IO_SETUP_ERROR", f"cannot read carrier file: {exc}") from exc

        port_opt: Optional[SupplyPort] = None
        if args.require_supply_port:
            port_opt = InMemorySupplyPort()

        validate_carrier(
            raw_bytes,
            supply_port=port_opt,
            require_promotion=not args.allow_ausente_unpromoted,
        )
        return EXIT_OK
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_SETUP
        if code == 0:
            return EXIT_OK
        if code == 1:
            return EXIT_GATE
        return code if 2 <= code <= 7 else EXIT_SETUP
    except RejectError as err:
        err_stream.write(f"{err.reject_class}: {err.detail}\n")
        return exit_code_for(err)
    except Exception as exc:  # pragma: no cover - defensive catch-all mapping unexpected errors to 3
        err_stream.write(f"INTERNAL_SETUP_ERROR: {exc}\n")
        return EXIT_SETUP


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
