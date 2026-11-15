# @module {"id":"wa.learn.w8u_trace","layer":2,"owner":"LEARN","deps":["wa.state.schema","wa.perceive.godot","wa.perceive.session_registry"],"book":"HB-WA-s3-remember"}
"""@module wa.learn.w8u_trace - Metadata-only W8U transition, action, and T2 trace recorder and verifier.

Implements the Wave 1 offline metadata-only recorder and independent verifier for:
- Action stream (`w8u.action/v1`) and action responses with 64 KiB canonical UTF-8 cap,
  strict pre-actuator validation, replay idempotency, payload-conflict refusal, and
  lost-ack session quarantine.
- T2 command/outcome stream with closed injected producer schemas and LF-inclusive
  canonical UTF-8 JSON-L digesting (`t2_trace_sha256`), kept strictly separate from
  UI `wender.state/v1` digests (no LF), action log digests, simulation state digests,
  frame pixel digests, and transition hash chains.
- OS-CU frame stream metadata (`xvfb-grab-held-pair` under `/dev/shm/vqa-ramws`),
  revision-pinned `w8u-os-contract` evidence verification, common XWD subset validation,
  same-grab canonical pair equality, credit flow control (`min(2, reserved_capacity)` on
  initial `ACK(seq=0)`, `cumulative_credits <= acked_frames + reserved_capacity`,
  `E_CREDIT` on overgrant/duplicate ACK/inflation), and mandatory `STOP -> END -> EOF`
  with joined reader exit.
- Authenticated transition hash chain (`w8u.transition/v1`) rooted at a distinct
  TARGET-frozen genesis domain (`w8u.genesis/v1`), joined by authenticated session/stream,
  action sequence, requested/applied tick, and canonical simulation basis.
- Receipt contract (`w8u.receipt/v1`), volatile RAM purging, recursive metadata-only
  allowlist enforcement (`E_PIXEL_LEAK`), approved metadata sink gating, and a disk sink spy.

Timings are labelled `VM`; numeric widths, limits, waves, and fixture choices are `TARGET`
specifications, never host measurements. Omitted upstream values are frozen as `AUSENTE`.
"""

from __future__ import annotations

import base64
import builtins
from collections.abc import Mapping
import copy
from dataclasses import dataclass
import hashlib
import io
import os
import pathlib
import re
import struct
from typing import Any, Callable

import numpy as np

from ..perceive import godot
from ..perceive import session_registry
from ..state import schema


# ============================================================================
# Target-Frozen Contract Constants, Domains, and Error Codes
# ============================================================================

AUSENTE = "AUSENTE"
TIMING_DOMAIN = "VM"
SPECIFICATION_CLASS = "TARGET"
EXECUTION_WAVE = 1

# Distinct TARGET-frozen genesis domain (not an existing bridge/integration API).
GENESIS_DOMAIN = "w8u.genesis/v1"
TRANSITION_CHAIN_DOMAIN = "w8u.transition/v1"

ACTION_SCHEMA_ID = "w8u.action/v1"
ACTION_RESPONSE_SCHEMA_ID = "w8u.action/v1"
ACTION_RESPONSE_ALT_SCHEMA_ID = "w8u.action_response/v1"
RECEIPT_SCHEMA_ID = "w8u.receipt/v1"
TRANSITION_SCHEMA_ID = "w8u.transition/v1"
STATE_SCHEMA_ID = schema.SCHEMA_ID
FRAME_BINDING_SCHEMA_ID = "w8u.frame_binding/v1"
VERIFY_RESULT_SCHEMA_ID = "w8u.verify_result/v1"
OS_CONTRACT_ID = "w8u-os-contract"

FRAMES_ROOT = "/dev/shm/vqa-ramws"
FRAME_RUN_SUBDIR = "opensuperficies/os-cu-0"
FRAME_SOURCE = "xvfb-grab-held-pair"

# Frozen digest domains preventing cross-domain confusion.
DIGEST_DOMAINS: dict[str, dict[str, Any]] = {
    "genesis": {
        "domain": GENESIS_DOMAIN,
        "separator": "\x00",
        "newline_terminated": False,
        "spec_class": SPECIFICATION_CLASS,
    },
    "transition_chain": {
        "domain": TRANSITION_CHAIN_DOMAIN,
        "separator": "\x00",
        "previous_digest_encoding": "raw_bytes_32",
        "newline_terminated": False,
        "spec_class": SPECIFICATION_CLASS,
    },
    "ui_state": {
        "domain": STATE_SCHEMA_ID,
        "newline_terminated": False,
        "serializer": "wender.state.schema.dumps",
        "spec_class": SPECIFICATION_CLASS,
    },
    "t2_trace": {
        "domain": "w8u.t2.jsonl/v1",
        "newline_terminated": True,
        "line_ending": "\n",
        "spec_class": SPECIFICATION_CLASS,
    },
    "action_log": {
        "domain": "w8u.action_log.jsonl/v1",
        "newline_terminated": True,
        "line_ending": "\n",
        "spec_class": SPECIFICATION_CLASS,
    },
    "frame_binding": {
        "domain": FRAME_BINDING_SCHEMA_ID,
        "newline_terminated": False,
        "spec_class": SPECIFICATION_CLASS,
    },
    "simulation_state": {
        "domain": "w8u.simulation_state/v1",
        "newline_terminated": False,
        "spec_class": SPECIFICATION_CLASS,
    },
}

# Frozen wire control magic, kinds, body schemas, and complete END numeric errors.
CONTROL_MAGIC = b"W8U1"
CONTROL_KINDS: dict[str, int] = {
    "HELLO": 1,
    "FRAME": 2,
    "ACK": 3,
    "STOP": 4,
    "END": 5,
    "EOF": 6,
}
CONTROL_BODY_SCHEMAS: dict[str, tuple[str, ...]] = {
    "HELLO": ("magic", "kind", "run_id", "reserved_capacity", "contract_revision"),
    "FRAME": (
        "magic",
        "kind",
        "seq",
        "tick",
        "tick_valid",
        "capture_mono_ns",
        "width",
        "height",
        "stride",
        "pixel_format",
        "sha256",
        "binding_sha256",
    ),
    "ACK": ("magic", "kind", "seq", "granted_credits"),
    "STOP": ("magic", "kind", "run_id", "final_seq"),
    "END": ("magic", "kind", "run_id", "end_code", "frames_captured"),
    "EOF": ("magic", "kind", "run_id"),
}
END_NUMERIC_ERRORS: dict[int, str] = {
    0: "OK",
    1: "E_PROTOCOL",
    2: "E_SCHEMA",
    3: "E_CREDIT",
    4: "E_UNAVAILABLE",
    5: "E_VERIFY_REFUSED",
    6: "E_UNCERTAIN",
    7: "E_PAIR_MISMATCH",
    8: "E_PIXEL_LEAK",
    9: "E_STALE_PUBLICATION",
    10: "E_REOPEN",
}

# Frozen binding and verification result schemas.
BINDING_SCHEMA_KEYS = (
    "schema",
    "run_id",
    "lease_id",
    "target_handle",
    "epoch",
    "seq",
    "capture_mono_ns",
    "frame_sha256",
)
VERIFY_RESULT_SCHEMA_KEYS = (
    "schema",
    "ok",
    "verdict",
    "code",
    "genesis_sha256",
    "terminal_chain_sha256",
    "action_log_sha256",
    "t2_trace_sha256",
    "t2_terminal_sha256",
    "final_state_sha256",
    "simulation_state_sha256",
    "rows_verified",
    "accepted_t2_count",
    "rejected_t2_count",
    "economic_success",
    "timing_domain",
    "host_measurements",
)

# Predicate Failure Error Codes
E_STATE = "E_STATE"
E_T2 = "E_T2"
E_JOIN = "E_JOIN"
E_CHAIN = "E_CHAIN"
E_PIXEL_LEAK = "E_PIXEL_LEAK"
E_CREDIT = "E_CREDIT"

# Action Admission / Verification Codes
OK_ADMITTED = "OK_ADMITTED"
E_SCHEMA = "E_SCHEMA"
E_VERB = "E_VERB"
E_LOCK = "E_LOCK"
E_EPOCH = "E_EPOCH"
E_STALE_FRAME = "E_STALE_FRAME"
E_FOCUS = "E_FOCUS"
E_BUDGET = "E_BUDGET"
E_DEADLINE = "E_DEADLINE"
E_UNCERTAIN = "E_UNCERTAIN"

ACTION_CODES = frozenset({
    OK_ADMITTED,
    E_SCHEMA,
    E_VERB,
    E_LOCK,
    E_EPOCH,
    E_STALE_FRAME,
    E_FOCUS,
    E_BUDGET,
    E_DEADLINE,
    E_UNCERTAIN,
})
ACTION_STATUSES = frozenset({"refused", "admitted", "applied", "uncertain"})

# Gate return codes
RC_OK = 0
RC_SCHEMA = 2
RC_UNAVAILABLE = 4
RC_REFUSED = 5
RC_UNCERTAIN = 6
VALID_GATE_RCS = frozenset({RC_OK, RC_SCHEMA, RC_UNAVAILABLE, RC_REFUSED, RC_UNCERTAIN})

# Action Contract Constants
ACTION_MAX_BODY_BYTES = 65536
ACTION_TEXT_MAX_BYTES = 4096
ACTION_WAIT_MAX_MS = 5000
POINTER_SCROLL_MIN = -120
POINTER_SCROLL_MAX = 120
POINTER_BUTTONS = (1, 2, 3)

ACTION_KEYS = (
    "schema",
    "run_id",
    "lease_id",
    "epoch",
    "generation",
    "action_id",
    "action_seq",
    "observed_frame_seq",
    "observed_frame_sha256",
    "deadline_ns",
    "verb",
    "args",
    "grant_sha256",
)
ACTION_RESPONSE_KEYS = (
    "schema",
    "action_id",
    "action_seq",
    "lease_id",
    "epoch",
    "status",
    "code",
    "emitted",
    "result_known",
    "applied_tick",
    "outcome_sha256",
)
ACTION_VERBS = ("pointer", "key", "text", "wait")
POINTER_MOVE_KEYS = ("operation", "x", "y")
POINTER_BUTTON_KEYS = ("operation", "x", "y", "button")
POINTER_SCROLL_KEYS = ("operation", "x", "y", "dx", "dy")
KEY_ARG_KEYS = ("operation", "key", "modifiers")
KEY_OPERATIONS = ("press", "down", "up")
FINITE_KEYS = (
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "Delete",
    "Space",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "ArrowDown",
    "Home",
    "End",
    "PageUp",
    "PageDown",
)
ORDERED_MODIFIERS = ("Shift", "Control", "Alt", "Super")
TEXT_ARG_KEYS = ("mode", "text")
WAIT_ARG_KEYS = ("duration_ms",)

# Frame Metadata Contract Constants
FRAME_METADATA_KEYS = (
    "seq",
    "tick",
    "tick_valid",
    "capture_mono_ns",
    "width",
    "height",
    "stride",
    "pixel_format",
    "sha256",
    "binding_sha256",
)
ALLOWED_PIXEL_FORMATS = frozenset({"RGB24", "BGR24", "XRGB8888", "BGRX8888", "RGBA8888", "BGRA8888"})

# Common X11 XWD Subset Constants
XWD_FILE_VERSION = 7
XWD_ZPIXMAP = 2
XWD_TRUE_COLOR = 4
XWD_COMMON_SUBSET_KEYS = (
    "header_size",
    "file_version",
    "pixmap_format",
    "pixmap_depth",
    "pixmap_width",
    "pixmap_height",
    "xoffset",
    "byte_order",
    "bitmap_unit",
    "bitmap_bit_order",
    "bitmap_pad",
    "bits_per_pixel",
    "bytes_per_line",
    "visual_class",
    "red_mask",
    "green_mask",
    "blue_mask",
    "bits_per_rgb",
    "colormap_entries",
    "ncolors",
    "window_width",
    "window_height",
    "window_x",
    "window_y",
    "window_bdrwidth",
)

# Receipt Contract Keys
RECEIPT_CORE_KEYS = (
    "run_id",
    "lease_id",
    "target_handle",
    "epoch",
    "frames_root",
    "frame_run_root",
    "source",
    "frames_captured",
    "first_frame",
    "last_frame",
    "ui_actions_count",
    "admitted_actions",
    "refused_actions",
    "uncertain_actions",
    "action_log_sha256",
    "t2_trace_sha256",
    "t2_terminal_sha256",
    "t2_ticks",
    "final_state_sha256",
    "simulation_state_sha256",
    "ui_gate_verdict",
    "ui_gate_code",
    "pair_mismatches",
    "frame_gaps",
    "frames_dropped",
    "x11_tcp_listeners",
    "x11_unix_listeners",
    "x11_abstract_listeners",
    "reader_exit_code",
    "reader_quiescent",
    "ram_purged",
    "frames_disk_bytes",
    "swap_delta_bytes",
    "metadata_only",
)
RECEIPT_KEYS_WITH_SCHEMA = ("schema",) + RECEIPT_CORE_KEYS

# Transition Contract Keys
TRANSITION_CORE_KEYS = (
    "run_id",
    "lease_id",
    "target_handle",
    "epoch",
    "row_seq",
    "action_id",
    "action_sha256",
    "pre_ui_state_sha256",
    "post_ui_state_sha256",
    "pre_frame",
    "post_frame",
    "t2_outcome",
    "simulation_pre_sha256",
    "simulation_post_sha256",
    "presentation_id",
    "gate_rc",
    "phase",
    "previous_chain_sha256",
    "chain_sha256",
)
TRANSITION_KEYS_WITH_SCHEMA = ("schema",) + TRANSITION_CORE_KEYS

# T2 Outcome Contract Keys
T2_OUTCOME_KEYS = (
    "seq",
    "requested_tick",
    "applied_tick",
    "order_seq_id",
    "status",
    "reason",
    "state_sha256",
)
T2_COMMAND_KEYS = ("tick", "seq", "cmd", "args")
T2_OUTCOME_STATUSES = frozenset({"accepted", "rejected", "proposed"})
_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]*\Z")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}\Z")
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# Recursive Metadata Allowlist & Pixel Leak Guards
FORBIDDEN_PIXEL_KEYS = frozenset({
    "rgb",
    "rgba",
    "bgr",
    "bgra",
    "pixels",
    "raw_pixels",
    "pixel_bytes",
    "pixel_data",
    "frame_bytes",
    "frame_data",
    "image",
    "image_bytes",
    "bitmap",
    "framebuffer",
    "scanlines",
    "png",
    "jpeg",
    "jpg",
    "webp",
    "gif",
    "bmp",
    "ppm",
    "pgm",
    "pbm",
    "pnm",
    "qoi",
    "xwd_bytes",
    "xwd_payload",
    "zlib",
    "gzip",
    "lz4",
    "zstd",
    "b64_pixels",
    "base64_pixels",
    "encoded_pixels",
    "compressed_pixels",
    "data_uri",
    "_perception",
})

_BINARY_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
    b"BM",
    b"P6\n",
    b"P5\n",
    b"P3\n",
    b"P6 ",
    b"P5 ",
    b"P3 ",
    b"qoif",
    b"\x1f\x8b",          # gzip
    b"\x28\xb5\x2f\xfd",  # zstd
    b"\x04\x22\x4d\x18",  # lz4
    b"\x78\x01",          # zlib low
    b"\x78\x5e",          # zlib
    b"\x78\x9c",          # zlib default
    b"\x78\xda",          # zlib best
    b"\x00\x00\x00\x07",  # XWD version 7 big-endian
)

_ENCODED_PIXEL_PATTERNS = (
    re.compile(r"^data:image/[a-zA-Z0-9.+_-]+;base64,", re.IGNORECASE),
    re.compile(r"^iVBORw0KGgo"),   # Base64 PNG
    re.compile(r"^/9j/"),          # Base64 JPEG
    re.compile(r"^R0lGOD[lh]"),    # Base64 GIF
    re.compile(r"^UklGR"),         # Base64 WebP
    re.compile(r"^H4sI"),          # Base64 gzip
    re.compile(r"^eJ[wrxNo]"),     # Base64 zlib
)


# ============================================================================
# Exceptions and Structured Verification Result
# ============================================================================

class W8UTraceError(ValueError):
    """Structured error carrying the failed W8U predicate code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class TraceLimits:
    """Bounded declared record limits enforced prior to any recorder mutation."""

    max_transitions: int = 1024
    max_actions: int = 1024
    max_t2_records: int = 1024
    max_frames: int = 4096
    max_action_body_bytes: int = ACTION_MAX_BODY_BYTES
    max_text_bytes: int = ACTION_TEXT_MAX_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_transitions",
            "max_actions",
            "max_t2_records",
            "max_frames",
            "max_action_body_bytes",
            "max_text_bytes",
        ):
            val = getattr(self, name)
            if not _is_int(val) or val < 1:
                raise W8UTraceError(E_SCHEMA, f"{name} must be a positive integer")
        if self.max_action_body_bytes > ACTION_MAX_BODY_BYTES:
            raise W8UTraceError(E_SCHEMA, "max_action_body_bytes cannot exceed 64 KiB")
        if self.max_text_bytes > ACTION_TEXT_MAX_BYTES:
            raise W8UTraceError(E_SCHEMA, "max_text_bytes cannot exceed 4096 bytes")


@dataclass(frozen=True)
class RawKeyValuePairs:
    """Pair-sequence wrapper allowing duplicate-key evidence tests before dict construction."""

    pairs: tuple[tuple[str, Any], ...]


# ============================================================================
# Primitive Validators & Strict Pre-Mutation JSON Parsing
# ============================================================================

def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _require_int(
    x: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    code: str = E_SCHEMA,
) -> int:
    if not _is_int(x):
        raise W8UTraceError(code, f"{name} must be an exact integer (bool/float rejected), got {x!r}")
    if minimum is not None and x < minimum:
        raise W8UTraceError(code, f"{name} must be >= {minimum}, got {x}")
    if maximum is not None and x > maximum:
        raise W8UTraceError(code, f"{name} must be <= {maximum}, got {x}")
    return x


def _require_bool(x: Any, name: str, *, code: str = E_SCHEMA) -> bool:
    if type(x) is not bool:
        raise W8UTraceError(code, f"{name} must be a bool, got {x!r}")
    return x


def _require_nonempty_str(x: Any, name: str, *, code: str = E_SCHEMA) -> str:
    if not isinstance(x, str) or not x or x != x.strip() or "\x00" in x or "\r" in x or "\n" in x:
        raise W8UTraceError(code, f"{name} must be a non-empty canonical string without NUL/CR/LF")
    try:
        x.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise W8UTraceError(code, f"{name} is not valid UTF-8: {exc}") from exc
    return x


def _require_hex64(x: Any, name: str, *, code: str = E_SCHEMA) -> str:
    if not isinstance(x, str) or not _HEX64_RE.fullmatch(x):
        raise W8UTraceError(code, f"{name} must be 64 lowercase hex characters, got {x!r}")
    return x


def validate_run_id(run_id: Any, *, code: str = E_SCHEMA) -> str:
    """Validate run_id as a safe path component without traversal."""
    s = _require_nonempty_str(run_id, "run_id", code=code)
    if not _SAFE_RUN_ID_RE.fullmatch(s) or ".." in s or "/" in s or "\\" in s:
        raise W8UTraceError(code, f"invalid run_id {run_id!r}")
    return s


def frame_run_root_for(run_id: str, *, code: str = E_SCHEMA) -> str:
    """Return canonical `/dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run_id>` path."""
    valid_id = validate_run_id(run_id, code=code)
    return f"{FRAMES_ROOT}/{FRAME_RUN_SUBDIR}/{valid_id}"


def normalize_target_handle(handle: Any, *, code: str = E_SCHEMA) -> dict[str, Any]:
    """Validate and convert a Handle or dict into canonical `target_handle` dict."""
    if isinstance(handle, Mapping):
        if type(handle) is not dict:
            raise W8UTraceError(code, "target_handle mapping must be a plain dict")
        if sorted(handle.keys()) != ["canonical_id", "generation", "kind"]:
            raise W8UTraceError(code, f"invalid target_handle keys: {sorted(handle.keys())}")
        kind = handle["kind"]
        canonical_id = handle["canonical_id"]
        generation = handle["generation"]
    elif hasattr(handle, "kind") and hasattr(handle, "canonical_id") and hasattr(handle, "generation"):
        kind = getattr(handle, "kind")
        canonical_id = getattr(handle, "canonical_id")
        generation = getattr(handle, "generation")
    else:
        raise W8UTraceError(code, f"target_handle must be a Handle or dict, got {type(handle)!r}")

    if not isinstance(kind, str) or kind not in schema.TARGET_KINDS:
        raise W8UTraceError(code, f"unsupported target_handle.kind: {kind!r}")
    _require_nonempty_str(canonical_id, "target_handle.canonical_id", code=code)
    _require_int(generation, "target_handle.generation", minimum=1, code=code)
    return {
        "canonical_id": canonical_id,
        "generation": generation,
        "kind": kind,
    }


def _check_pairs_for_duplicates(pairs: list[tuple[Any, Any]], *, code: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if not isinstance(k, str):
            raise W8UTraceError(code, f"non-string key {k!r}")
        if "\x00" in k:
            raise W8UTraceError(code, "NUL byte in JSON key")
        try:
            k.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UTraceError(code, f"invalid UTF-8 in key {k!r}: {exc}") from exc
        if k in out:
            raise W8UTraceError(code, f"duplicate JSON key: {k!r}")
        if isinstance(v, RawKeyValuePairs):
            v = _check_pairs_for_duplicates(list(v.pairs), code=code)
        out[k] = v
    return out


def parse_strict_json_object(
    raw: bytes | bytearray | memoryview | str | dict[str, Any] | RawKeyValuePairs,
    *,
    max_bytes: int | None = None,
    allow_dict: bool = True,
    code: str = E_SCHEMA,
) -> dict[str, Any]:
    """Parse raw UTF-8 JSON or validate a plain dict, rejecting duplicate keys, floats, and trailing data."""
    if isinstance(raw, RawKeyValuePairs):
        obj = _check_pairs_for_duplicates(list(raw.pairs), code=code)
        _reject_floats_and_non_plain_mappings(obj, code=code)
        if max_bytes is not None:
            encoded = schema.dumps(obj).encode("utf-8")
            if len(encoded) > max_bytes:
                raise W8UTraceError(code, f"canonical body size {len(encoded)} exceeds limit {max_bytes}")
        return copy.deepcopy(obj)

    if isinstance(raw, Mapping):
        if not allow_dict or type(raw) is not dict:
            raise W8UTraceError(
                code,
                "mapping input cannot retain duplicate evidence; pass canonical UTF-8 JSON bytes/str or plain dict",
            )
        _reject_floats_and_non_plain_mappings(raw, code=code)
        if max_bytes is not None:
            encoded = schema.dumps(raw).encode("utf-8")
            if len(encoded) > max_bytes:
                raise W8UTraceError(code, f"canonical body size {len(encoded)} exceeds limit {max_bytes}")
        return copy.deepcopy(raw)

    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
        if max_bytes is not None and len(raw_bytes) > max_bytes:
            raise W8UTraceError(code, f"raw UTF-8 body size {len(raw_bytes)} exceeds cap {max_bytes}")
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise W8UTraceError(code, f"invalid UTF-8 body: {exc}") from exc
    elif isinstance(raw, str):
        try:
            raw_bytes = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UTraceError(code, f"invalid UTF-8 string: {exc}") from exc
        if max_bytes is not None and len(raw_bytes) > max_bytes:
            raise W8UTraceError(code, f"UTF-8 body size {len(raw_bytes)} exceeds cap {max_bytes}")
        text = raw
    else:
        raise W8UTraceError(code, f"expected UTF-8 bytes, str, or dict, got {type(raw)!r}")

    if "\r" in text or "\n" in text:
        raise W8UTraceError(code, "embedded CR/LF or trailing lines not allowed in single JSON payload")
    if not text or text != text.strip(" \t"):
        raise W8UTraceError(code, "empty or whitespace-padded JSON payload")

    try:
        parsed = godot.parse_canonical_json_line(text)
    except godot.GodotError as exc:
        raise W8UTraceError(code, str(exc)) from exc

    _reject_floats_and_non_plain_mappings(parsed, code=code)
    return parsed


def _reject_floats_and_non_plain_mappings(obj: Any, path: str = "$", *, code: str = E_SCHEMA) -> None:
    if isinstance(obj, float):
        raise W8UTraceError(code, f"float not allowed at {path}")
    if obj is None or isinstance(obj, bool) or _is_int(obj):
        return
    if isinstance(obj, str):
        try:
            obj.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UTraceError(code, f"invalid UTF-8 at {path}: {exc}") from exc
        if any(sep in obj for sep in ("\u2028", "\u2029")):
            raise W8UTraceError(code, f"Unicode line separator not allowed at {path}")
        return
    if isinstance(obj, RawKeyValuePairs):
        checked = _check_pairs_for_duplicates(list(obj.pairs), code=code)
        _reject_floats_and_non_plain_mappings(checked, path, code=code)
        return
    if isinstance(obj, Mapping):
        if type(obj) is not dict:
            raise W8UTraceError(code, f"custom Mapping subtype forbidden at {path}: {type(obj)!r}")
        for k, v in obj.items():
            if not isinstance(k, str):
                raise W8UTraceError(code, f"non-string key at {path}: {k!r}")
            try:
                k.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise W8UTraceError(code, f"invalid UTF-8 key at {path}: {exc}") from exc
            _reject_floats_and_non_plain_mappings(v, f"{path}.{k}", code=code)
        return
    if isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _reject_floats_and_non_plain_mappings(item, f"{path}[{i}]", code=code)
        return
    raise W8UTraceError(code, f"unsupported type at {path}: {type(obj)!r}")


# ============================================================================
# Metadata-Only Guard (`E_PIXEL_LEAK`), Approved Sinks, and Disk Sink Spy
# ============================================================================

def _looks_like_encoded_or_compressed_pixels(s: str) -> bool:
    if len(s) < 6:
        return False
    for pat in _ENCODED_PIXEL_PATTERNS:
        if pat.search(s):
            return True
    for magic in (b"\x89PNG", b"GIF87a", b"GIF89a", b"qoif"):
        try:
            if magic.decode("latin1") in s:
                return True
        except Exception:
            pass
    if s.startswith(("P6\n", "P5\n", "P3\n", "P6 ", "P5 ", "P3 ")):
        return True
    stripped = s.strip()
    if len(stripped) >= 16 and len(stripped) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", stripped):
        if len(stripped) not in (16, 64) or stripped[:16].lower() in (
            "89504e470d0a1a0a",
            "ffd8ffe000104a46",
            "1f8b080000000000",
            "0000000700000002",
        ):
            raw_prefix = bytes.fromhex(stripped[:16])
            if any(raw_prefix.startswith(m) for m in _BINARY_MAGIC_PREFIXES):
                return True
    if len(stripped) >= 12 and len(stripped) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", stripped):
        try:
            decoded = base64.b64decode(stripped[:24], validate=True)
            if any(decoded.startswith(m) for m in _BINARY_MAGIC_PREFIXES):
                return True
        except Exception:
            pass
    return False


def _looks_like_pixel_array(seq: list[Any] | tuple[Any, ...]) -> bool:
    """Detect nested numeric pixel grids or scanline arrays in metadata."""
    if not seq:
        return False
    if all(isinstance(x, (list, tuple)) and len(x) >= 3 for x in seq):
        first = seq[0]
        if all(_is_int(v) and 0 <= v <= 255 for v in first):
            return True
        if all(
            isinstance(cell, (list, tuple))
            and len(cell) in (3, 4)
            and all(_is_int(c) and 0 <= c <= 255 for c in cell)
            for cell in first
        ):
            return True
    if len(seq) > 4 and all(_is_int(x) and 0 <= x <= 255 for x in seq):
        return True
    return False


def assert_metadata_only(obj: Any, path: str = "$") -> None:
    """Recursively verify that an object contains only metadata and zero pixel/compressed payloads."""
    if isinstance(obj, (bytes, bytearray, memoryview, np.ndarray, session_registry.FrameRef)):
        raise W8UTraceError(E_PIXEL_LEAK, f"raw binary/frame object forbidden in metadata at {path}")
    if isinstance(obj, float):
        raise W8UTraceError(E_PIXEL_LEAK, f"float forbidden in metadata at {path}")
    if obj is None or isinstance(obj, bool) or _is_int(obj):
        return
    if isinstance(obj, str):
        if _looks_like_encoded_or_compressed_pixels(obj):
            raise W8UTraceError(E_PIXEL_LEAK, f"encoded or compressed pixel payload detected at {path}")
        return
    if isinstance(obj, Mapping):
        if type(obj) is not dict:
            raise W8UTraceError(E_PIXEL_LEAK, f"non-dict mapping forbidden in metadata at {path}")
        for k, v in obj.items():
            if not isinstance(k, str):
                raise W8UTraceError(E_PIXEL_LEAK, f"non-string key in metadata at {path}")
            k_lower = k.lower()
            if k in FORBIDDEN_PIXEL_KEYS or k_lower in FORBIDDEN_PIXEL_KEYS:
                raise W8UTraceError(E_PIXEL_LEAK, f"forbidden pixel/wrapper key {k!r} at {path}")
            assert_metadata_only(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        if _looks_like_pixel_array(obj):
            raise W8UTraceError(E_PIXEL_LEAK, f"nested pixel/byte array detected at {path}")
        for idx, item in enumerate(obj):
            assert_metadata_only(item, f"{path}[{idx}]")
        return
    raise W8UTraceError(E_PIXEL_LEAK, f"unapproved object type {type(obj)!r} in metadata at {path}")


class ApprovedMetadataSink:
    """In-memory metadata-only sink explicitly approved for W8U trace emission."""

    _approved_w8u_metadata_sink = True

    def __init__(self, *, max_records: int = 4096) -> None:
        _require_int(max_records, "max_records", minimum=1)
        self.max_records = max_records
        self.records: list[dict[str, Any]] = []
        self.lines: list[bytes] = []

    def write_record(self, record: dict[str, Any]) -> bytes:
        assert_metadata_only(record)
        if len(self.records) >= self.max_records:
            raise W8UTraceError(E_SCHEMA, f"sink record limit {self.max_records} exceeded")
        line = (schema.dumps(record) + "\n").encode("utf-8")
        self.records.append(copy.deepcopy(record))
        self.lines.append(line)
        return line

    def __call__(self, payload: dict[str, Any] | bytes | str) -> None:
        if isinstance(payload, dict):
            self.write_record(payload)
            return
        if isinstance(payload, (bytes, bytearray, memoryview)):
            raw = bytes(payload)
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise W8UTraceError(E_PIXEL_LEAK, f"binary non-UTF8 payload sent to metadata sink: {exc}") from exc
        elif isinstance(payload, str):
            text = payload
        else:
            raise W8UTraceError(E_PIXEL_LEAK, f"unsupported sink payload type: {type(payload)!r}")

        for raw_line in text.splitlines():
            if not raw_line:
                continue
            parsed = parse_strict_json_object(raw_line, code=E_PIXEL_LEAK)
            self.write_record(parsed)


def verify_approved_sink(sink: Any, approved_sinks: set[Any] | frozenset[Any] | None = None) -> None:
    """Refuse unapproved or file-backed sinks with `E_PIXEL_LEAK`."""
    if sink is None:
        raise W8UTraceError(E_PIXEL_LEAK, "metadata sink cannot be None")
    if isinstance(sink, (str, bytes, pathlib.Path, io.IOBase)):
        raise W8UTraceError(E_PIXEL_LEAK, "filesystem paths and IO streams are forbidden as metadata sinks")
    if hasattr(sink, "fileno") or hasattr(sink, "name"):
        raise W8UTraceError(E_PIXEL_LEAK, "file-like sink with fileno/name is forbidden")
    if isinstance(sink, ApprovedMetadataSink) or getattr(sink, "_approved_w8u_metadata_sink", False):
        return
    if approved_sinks is not None and sink in approved_sinks:
        return
    raise W8UTraceError(E_PIXEL_LEAK, "sink is not an approved in-memory metadata sink")


class DiskSinkSpy:
    """Spy and guard that intercepts filesystem write attempts during offline W8U execution."""

    def __init__(self, *, block_writes: bool = True) -> None:
        self.block_writes = block_writes
        self.write_events: list[tuple[str, str]] = []
        self._active = False
        self._orig_builtins_open: Any = None
        self._orig_io_open: Any = None
        self._orig_os_open: Any = None
        self._orig_path_open: Any = None
        self._orig_path_write_bytes: Any = None
        self._orig_path_write_text: Any = None

    def _record_and_fail(self, op: str, target: Any) -> None:
        self.write_events.append((op, str(target)))
        if self.block_writes:
            raise W8UTraceError(E_PIXEL_LEAK, f"disk write forbidden ({op} on {target!r})")

    def __enter__(self) -> DiskSinkSpy:
        if self._active:
            return self
        self._active = True
        self._orig_builtins_open = builtins.open
        self._orig_io_open = io.open
        self._orig_os_open = os.open
        self._orig_path_open = pathlib.Path.open
        self._orig_path_write_bytes = pathlib.Path.write_bytes
        self._orig_path_write_text = pathlib.Path.write_text

        spy = self
        orig_b_open = self._orig_builtins_open
        orig_os_open = self._orig_os_open
        orig_p_open = self._orig_path_open
        orig_p_wb = self._orig_path_write_bytes
        orig_p_wt = self._orig_path_write_text

        def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(m in mode for m in ("w", "a", "x", "+")):
                spy._record_and_fail("open", file)
            return orig_b_open(file, mode, *args, **kwargs)

        def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & write_flags:
                spy._record_and_fail("os.open", path)
            return orig_os_open(path, flags, *args, **kwargs)

        def guarded_path_open(path_self: pathlib.Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(m in mode for m in ("w", "a", "x", "+")):
                spy._record_and_fail("Path.open", path_self)
            return orig_p_open(path_self, mode, *args, **kwargs)

        def guarded_write_bytes(path_self: pathlib.Path, data: Any, *args: Any, **kwargs: Any) -> Any:
            spy._record_and_fail("Path.write_bytes", path_self)
            return orig_p_wb(path_self, data, *args, **kwargs)

        def guarded_write_text(path_self: pathlib.Path, data: Any, *args: Any, **kwargs: Any) -> Any:
            spy._record_and_fail("Path.write_text", path_self)
            return orig_p_wt(path_self, data, *args, **kwargs)

        builtins.open = guarded_open
        io.open = guarded_open
        os.open = guarded_os_open
        pathlib.Path.open = guarded_path_open  # type: ignore[method-assign]
        pathlib.Path.write_bytes = guarded_write_bytes  # type: ignore[method-assign]
        pathlib.Path.write_text = guarded_write_text  # type: ignore[method-assign]
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self._active:
            return
        builtins.open = self._orig_builtins_open
        io.open = self._orig_io_open
        os.open = self._orig_os_open
        pathlib.Path.open = self._orig_path_open  # type: ignore[method-assign]
        pathlib.Path.write_bytes = self._orig_path_write_bytes  # type: ignore[method-assign]
        pathlib.Path.write_text = self._orig_path_write_text  # type: ignore[method-assign]
        self._active = False


# ============================================================================
# STATE (`wender.state/v1`) and Observation Envelope Validation (`E_STATE`)
# ============================================================================

def validate_state_strict(state: Any) -> dict[str, Any]:
    """Validate canonical `wender.state/v1` base object, rejecting wrappers, unknown keys, floats, and bool coords."""
    if not isinstance(state, Mapping) or type(state) is not dict:
        raise W8UTraceError(E_STATE, "STATE must be a plain dict")
    if "_perception" in state:
        raise W8UTraceError(E_STATE, "_perception wrapper is forbidden inside STATE")
    if set(state.keys()) == set(schema.TARGET_DESCRIPTOR_KEYS) or "handle" in state or "frame_seq" in state:
        raise W8UTraceError(E_STATE, "observation envelope cannot be passed as raw wender.state/v1")

    errs = schema.validate_state(state)
    if errs:
        raise W8UTraceError(E_STATE, "; ".join(errs))

    src = state["source"]
    if type(src) is not dict or sorted(src.keys()) != ["kind", "title", "url"]:
        raise W8UTraceError(E_STATE, f"source keys must be exactly ['kind', 'title', 'url'], got {sorted(src.keys())}")
    if not isinstance(src["url"], str) or not isinstance(src["title"], str):
        raise W8UTraceError(E_STATE, "source.url and source.title must be strings")

    counts = state["counts"]
    if type(counts) is not dict or sorted(counts.keys()) != ["actionable", "nodes", "raw"]:
        raise W8UTraceError(E_STATE, "counts keys must be exactly ['actionable', 'nodes', 'raw']")
    if counts["raw"] < 0:
        raise W8UTraceError(E_STATE, "counts.raw must be non-negative")

    if not isinstance(state["prune"], str) or not state["prune"]:
        raise W8UTraceError(E_STATE, "prune must be a non-empty string")

    vp = state["viewport"]
    if vp is not None:
        if type(vp) is not dict or not vp:
            raise W8UTraceError(E_STATE, "viewport must be null or a non-empty integer dict")
        allowed_vp_keys = {"width", "height", "x", "y", "dpr_ppm"}
        if set(vp.keys()) - allowed_vp_keys or not {"width", "height"}.issubset(vp.keys()):
            raise W8UTraceError(E_STATE, f"invalid viewport keys: {sorted(vp.keys())}")
        for k, v in vp.items():
            if not _is_int(v):
                raise W8UTraceError(E_STATE, f"viewport.{k} must be an integer, got {v!r}")
        if vp["width"] <= 0 or vp["height"] <= 0:
            raise W8UTraceError(E_STATE, "viewport width and height must be positive")

    assert_metadata_only(state, "$.state")
    return copy.deepcopy(state)


def compute_state_sha256(state: dict[str, Any]) -> str:
    """Compute SHA-256 of UTF-8 `schema.dumps(state)` without trailing LF."""
    validated = validate_state_strict(state)
    canonical = schema.dumps(validated)
    if canonical.endswith("\n"):
        raise W8UTraceError(E_STATE, "STATE canonical dump must not include trailing LF")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_observation_envelope(
    envelope: Any,
    *,
    expected_epoch: int | None = None,
    registry_epoch: int | None = None,
) -> dict[str, Any]:
    """Validate separate observation/target-descriptor envelope (`handle, epoch, frame_seq, dpr_ppm, state[, frame]`)."""
    if isinstance(envelope, session_registry.Observation):
        validate_state_strict(envelope.state)
        return {
            "handle": normalize_target_handle(envelope.handle, code=E_STATE),
            "epoch": envelope.epoch,
            "frame_seq": envelope.frame_seq,
            "dpr_ppm": envelope.dpr_ppm,
            "state": copy.deepcopy(envelope.state),
            "frame": envelope.frame,
        }
    if not isinstance(envelope, Mapping) or type(envelope) is not dict:
        raise W8UTraceError(E_STATE, "observation envelope must be a dict or Observation")
    if "_perception" in envelope:
        raise W8UTraceError(E_STATE, "_perception wrapper is forbidden")

    frame_obj = envelope.get("frame")
    desc = {k: v for k, v in envelope.items() if k != "frame"}
    errs = schema.validate_target_descriptor(
        desc,
        expected_epoch=expected_epoch,
        registry_epoch=registry_epoch,
    )
    if errs:
        raise W8UTraceError(E_STATE, "; ".join(errs))

    norm_handle = normalize_target_handle(desc["handle"], code=E_STATE)
    validate_state_strict(desc["state"])

    if frame_obj is not None:
        if not isinstance(frame_obj, session_registry.FrameRef):
            raise W8UTraceError(E_STATE, "observation frame reference must be a FrameRef")
        fh = normalize_target_handle(frame_obj.handle, code=E_STATE)
        if fh != norm_handle or frame_obj.epoch != desc["epoch"] or frame_obj.frame_seq != desc["frame_seq"]:
            raise W8UTraceError(E_STATE, "observation envelope and FrameRef provenance mismatch")

    out = {
        "handle": norm_handle,
        "epoch": desc["epoch"],
        "frame_seq": desc["frame_seq"],
        "dpr_ppm": desc["dpr_ppm"],
        "state": copy.deepcopy(desc["state"]),
    }
    if frame_obj is not None:
        out["frame"] = frame_obj
    return out


# ============================================================================
# Frame Metadata, XWD Common Subset, Grab Pair, and Credit Flow Control
# ============================================================================

def compute_frame_binding_sha256(
    *,
    run_id: str,
    lease_id: str,
    target_handle: Any,
    epoch: int,
    seq: int,
    capture_mono_ns: int,
    frame_sha256: str,
) -> str:
    """Compute canonical `binding_sha256` over the frozen `w8u.frame_binding/v1` schema."""
    binding = {
        "schema": FRAME_BINDING_SCHEMA_ID,
        "run_id": validate_run_id(run_id, code=E_STATE),
        "lease_id": _require_nonempty_str(lease_id, "lease_id", code=E_STATE),
        "target_handle": normalize_target_handle(target_handle, code=E_STATE),
        "epoch": _require_int(epoch, "epoch", minimum=0, code=E_STATE),
        "seq": _require_int(seq, "seq", minimum=1, code=E_STATE),
        "capture_mono_ns": _require_int(capture_mono_ns, "capture_mono_ns", minimum=0, code=E_STATE),
        "frame_sha256": _require_hex64(frame_sha256, "frame_sha256", code=E_STATE),
    }
    return hashlib.sha256(schema.dumps(binding).encode("utf-8")).hexdigest()


def validate_frame_metadata(meta: Any, *, code: str = E_STATE) -> dict[str, Any]:
    """Validate the exact 10-key W8U frame metadata record."""
    if not isinstance(meta, Mapping) or type(meta) is not dict:
        raise W8UTraceError(code, "frame metadata must be a plain dict")
    assert_metadata_only(meta, "$.frame_metadata")
    if sorted(meta.keys()) != sorted(FRAME_METADATA_KEYS):
        raise W8UTraceError(
            code,
            f"frame metadata keys must be {sorted(FRAME_METADATA_KEYS)}, got {sorted(meta.keys())}",
        )

    seq = _require_int(meta["seq"], "frame.seq", minimum=1, code=code)
    tick_valid = _require_bool(meta["tick_valid"], "frame.tick_valid", code=code)
    tick = meta["tick"]
    if tick_valid:
        if tick is None:
            raise W8UTraceError(code, "frame.tick must be a non-negative integer when tick_valid is True")
        _require_int(tick, "frame.tick", minimum=0, code=code)
    else:
        if tick is not None:
            raise W8UTraceError(code, "frame.tick must be null when tick_valid is False (committed zero forbidden)")

    capture_mono_ns = _require_int(meta["capture_mono_ns"], "frame.capture_mono_ns", minimum=0, code=code)
    width = _require_int(meta["width"], "frame.width", minimum=1, code=code)
    height = _require_int(meta["height"], "frame.height", minimum=1, code=code)
    stride = _require_int(meta["stride"], "frame.stride", minimum=1, code=code)
    pixel_format = _require_nonempty_str(meta["pixel_format"], "frame.pixel_format", code=code)
    if pixel_format not in ALLOWED_PIXEL_FORMATS:
        raise W8UTraceError(code, f"unsupported frame.pixel_format: {pixel_format!r}")
    bpp = 4 if "8888" in pixel_format else 3
    if stride < width * bpp:
        raise W8UTraceError(code, f"frame.stride {stride} < width * bpp ({width * bpp})")

    sha256 = _require_hex64(meta["sha256"], "frame.sha256", code=code)
    binding_sha256 = meta["binding_sha256"]
    if binding_sha256 is not None:
        _require_hex64(binding_sha256, "frame.binding_sha256", code=code)

    return {
        "seq": seq,
        "tick": tick,
        "tick_valid": tick_valid,
        "capture_mono_ns": capture_mono_ns,
        "width": width,
        "height": height,
        "stride": stride,
        "pixel_format": pixel_format,
        "sha256": sha256,
        "binding_sha256": binding_sha256,
    }


def pack_xwd_header(
    *,
    width: int,
    height: int,
    stride: int | None = None,
    depth: int = 24,
    bpp: int = 24,
    red_mask: int = 0xFF0000,
    green_mask: int = 0x00FF00,
    blue_mask: int = 0x0000FF,
) -> bytes:
    """Pack a standard 100-byte big-endian X11 XWD v7 header for in-memory testing."""
    _require_int(width, "width", minimum=1, code=E_STATE)
    _require_int(height, "height", minimum=1, code=E_STATE)
    bytes_per_line = (width * (bpp // 8)) if stride is None else stride
    values = (
        100,               # header_size
        XWD_FILE_VERSION,  # file_version (7)
        XWD_ZPIXMAP,       # pixmap_format (2)
        depth,             # pixmap_depth (24 or 32)
        width,             # pixmap_width
        height,            # pixmap_height
        0,                 # xoffset
        1,                 # byte_order (MSBFirst=1)
        32,                # bitmap_unit
        1,                 # bitmap_bit_order
        32,                # bitmap_pad
        bpp,               # bits_per_pixel (24 or 32)
        bytes_per_line,    # bytes_per_line
        XWD_TRUE_COLOR,    # visual_class (4)
        red_mask,
        green_mask,
        blue_mask,
        8,                 # bits_per_rgb
        256,               # colormap_entries
        0,                 # ncolors
        width,             # window_width
        height,            # window_height
        0,                 # window_x
        0,                 # window_y
        0,                 # window_bdrwidth
    )
    return struct.pack(">25I", *values)


def validate_xwd_common_subset(xwd_header: bytes | dict[str, Any], *, code: str = E_STATE) -> dict[str, int]:
    """Validate the required common X11 XWD header subset from 100+ header bytes or parsed dict."""
    if isinstance(xwd_header, (bytes, bytearray, memoryview)):
        raw = bytes(xwd_header)
        if len(raw) < 100:
            raise W8UTraceError(code, f"XWD header requires at least 100 bytes, got {len(raw)}")
        unpacked = struct.unpack(">25I", raw[:100])
        hdr = dict(zip(XWD_COMMON_SUBSET_KEYS, unpacked))
    elif isinstance(xwd_header, Mapping) and type(xwd_header) is dict:
        if sorted(xwd_header.keys()) != sorted(XWD_COMMON_SUBSET_KEYS):
            raise W8UTraceError(code, "XWD header dict does not match common XWD subset keys")
        hdr = {}
        for k in XWD_COMMON_SUBSET_KEYS:
            hdr[k] = _require_int(xwd_header[k], f"xwd.{k}", minimum=0, code=code)
    else:
        raise W8UTraceError(code, "XWD header must be bytes or common-subset dict")

    if hdr["header_size"] < 100:
        raise W8UTraceError(code, f"invalid XWD header_size {hdr['header_size']}")
    if hdr["file_version"] != XWD_FILE_VERSION:
        raise W8UTraceError(code, f"unsupported XWD file_version {hdr['file_version']}")
    if hdr["pixmap_format"] != XWD_ZPIXMAP:
        raise W8UTraceError(code, f"unsupported XWD pixmap_format {hdr['pixmap_format']}")
    if hdr["pixmap_depth"] not in (24, 32):
        raise W8UTraceError(code, f"unsupported XWD pixmap_depth {hdr['pixmap_depth']}")
    if hdr["bits_per_pixel"] not in (24, 32):
        raise W8UTraceError(code, f"unsupported XWD bits_per_pixel {hdr['bits_per_pixel']}")
    if hdr["visual_class"] != XWD_TRUE_COLOR:
        raise W8UTraceError(code, f"unsupported XWD visual_class {hdr['visual_class']}")
    if hdr["pixmap_width"] <= 0 or hdr["pixmap_height"] <= 0:
        raise W8UTraceError(code, "XWD dimensions must be positive")
    min_bpl = hdr["pixmap_width"] * (hdr["bits_per_pixel"] // 8)
    if hdr["bytes_per_line"] < min_bpl:
        raise W8UTraceError(code, f"XWD bytes_per_line {hdr['bytes_per_line']} < {min_bpl}")
    if hdr["ncolors"] != 0:
        raise W8UTraceError(code, "common XWD subset forbids indexed colormap entries (ncolors must be 0)")
    if (hdr["red_mask"], hdr["green_mask"], hdr["blue_mask"]) not in (
        (0xFF0000, 0x00FF00, 0x0000FF),
        (0x0000FF, 0x00FF00, 0xFF0000),
    ):
        raise W8UTraceError(code, "unsupported XWD RGB masks in common subset")
    return hdr


def verify_same_grab_pair(grab_a: dict[str, Any], grab_b: dict[str, Any], *, code: str = E_STATE) -> dict[str, Any]:
    """Verify same-grab canonical pair equality and common XWD subset compliance."""
    if not isinstance(grab_a, dict) or not isinstance(grab_b, dict):
        raise W8UTraceError(code, "grab pair entries must be dicts")
    if "xwd_header" in grab_a or "xwd_header" in grab_b:
        if "xwd_header" not in grab_a or "xwd_header" not in grab_b:
            raise W8UTraceError(code, "both grabs in pair must supply xwd_header when present")
        hdr_a = validate_xwd_common_subset(grab_a["xwd_header"], code=code)
        hdr_b = validate_xwd_common_subset(grab_b["xwd_header"], code=code)
        if hdr_a != hdr_b:
            raise W8UTraceError(code, "same-grab XWD headers differ")

    meta_a = validate_frame_metadata({k: v for k, v in grab_a.items() if k != "xwd_header"}, code=code)
    meta_b = validate_frame_metadata({k: v for k, v in grab_b.items() if k != "xwd_header"}, code=code)
    if meta_a != meta_b:
        raise W8UTraceError(code, "same-grab canonical pair inequality detected")
    return meta_a


class FrameCreditController:
    """Enforces W8U OS-CU credit grants, frame sequence continuity, and mandatory shutdown lifecycle."""

    PREFERRED_INITIAL_GRANT = 2

    def __init__(self, *, reserved_capacity: int) -> None:
        self.reserved_capacity = _require_int(
            reserved_capacity, "reserved_capacity", minimum=1, code=E_CREDIT
        )
        self.open_count = 0
        self.is_open = False
        self.initial_ack_received = False
        self.acked_frames = 0
        self.emitted_frames = 0
        self.cumulative_credits = 0
        self.seen_acks: set[int] = set()
        self.stopped = False
        self.ended = False
        self.end_code: int | None = None
        self.eof_received = False
        self.reader_joined = False
        self.reader_exit_code: int | None = None
        self.pair_mismatches = 0
        self.frame_gaps = 0
        self.frames_dropped = 0
        self.frames_metadata: list[dict[str, Any]] = []

    @property
    def available_credits(self) -> int:
        return self.cumulative_credits - self.emitted_frames

    def open_stream(self) -> None:
        if self.open_count >= 1:
            raise W8UTraceError(E_CREDIT, "reopening frame stream per frame is forbidden")
        self.open_count += 1
        self.is_open = True

    def receive_ack(self, seq: int, *, grant: int | None = None) -> int:
        _require_int(seq, "ack.seq", minimum=0, code=E_CREDIT)
        if not self.is_open or self.stopped or self.ended or self.eof_received:
            raise W8UTraceError(E_CREDIT, "ACK on closed or stopped stream")
        if seq in self.seen_acks:
            raise W8UTraceError(E_CREDIT, f"duplicate ACK for seq={seq}")

        if seq == 0:
            if self.initial_ack_received or self.emitted_frames > 0:
                raise W8UTraceError(E_CREDIT, "initial ACK seq=0 out of order")
            max_initial = min(self.PREFERRED_INITIAL_GRANT, self.reserved_capacity)
            actual_grant = max_initial if grant is None else _require_int(grant, "grant", minimum=1, code=E_CREDIT)
            if actual_grant > max_initial:
                raise W8UTraceError(
                    E_CREDIT,
                    f"initial ACK seq=0 overgrant {actual_grant} > min(2, {self.reserved_capacity})={max_initial}",
                )
            self.initial_ack_received = True
            self.seen_acks.add(0)
            self.cumulative_credits += actual_grant
            return actual_grant

        if not self.initial_ack_received:
            raise W8UTraceError(E_CREDIT, "frame ACK received before initial ACK seq=0")
        if seq != self.acked_frames + 1 or seq > self.emitted_frames:
            raise W8UTraceError(
                E_CREDIT,
                f"invalid ACK seq={seq} (expected {self.acked_frames + 1}, emitted={self.emitted_frames})",
            )

        self.acked_frames += 1
        self.seen_acks.add(seq)
        max_allowed_cumulative = self.acked_frames + self.reserved_capacity
        default_grant = min(1, max_allowed_cumulative - self.cumulative_credits)
        actual_grant = default_grant if grant is None else _require_int(grant, "grant", minimum=0, code=E_CREDIT)
        if actual_grant > 1:
            raise W8UTraceError(E_CREDIT, f"credit inflation: single ACK cannot grant {actual_grant} credits")
        if self.cumulative_credits + actual_grant > max_allowed_cumulative:
            raise W8UTraceError(
                E_CREDIT,
                f"overgrant: cumulative credits {self.cumulative_credits + actual_grant} > "
                f"acked_frames ({self.acked_frames}) + capacity ({self.reserved_capacity})",
            )
        self.cumulative_credits += actual_grant
        return actual_grant

    def publish_frame_pair(
        self,
        grab_a: dict[str, Any],
        grab_b: dict[str, Any] | None = None,
        *,
        max_frames: int = 4096,
    ) -> dict[str, Any]:
        if not self.is_open:
            raise W8UTraceError(E_STATE, "frame publication before stream open")
        if self.stopped or self.ended or self.eof_received or self.reader_joined:
            raise W8UTraceError(E_STATE, "stale frame publication after STOP/END/EOF")
        if not self.initial_ack_received:
            raise W8UTraceError(E_CREDIT, "frame publication before initial ACK seq=0")
        if len(self.frames_metadata) >= max_frames:
            raise W8UTraceError(E_SCHEMA, f"frame limit {max_frames} exceeded")
        if self.available_credits <= 0:
            raise W8UTraceError(E_CREDIT, "credit exhaustion: zero available frame credits")

        if grab_b is not None:
            try:
                meta = verify_same_grab_pair(grab_a, grab_b, code=E_STATE)
            except W8UTraceError:
                self.pair_mismatches += 1
                raise
        else:
            if "xwd_header" in grab_a:
                validate_xwd_common_subset(grab_a["xwd_header"], code=E_STATE)
            meta = validate_frame_metadata({k: v for k, v in grab_a.items() if k != "xwd_header"}, code=E_STATE)

        expected_seq = self.emitted_frames + 1
        if meta["seq"] != expected_seq:
            if meta["seq"] > expected_seq:
                self.frame_gaps += meta["seq"] - expected_seq
            raise W8UTraceError(E_STATE, f"frame sequence gap or disorder: expected {expected_seq}, got {meta['seq']}")
        if self.frames_metadata and meta["capture_mono_ns"] < self.frames_metadata[-1]["capture_mono_ns"]:
            raise W8UTraceError(E_STATE, "frame capture_mono_ns regressed")

        self.emitted_frames += 1
        self.frames_metadata.append(meta)
        return copy.deepcopy(meta)

    def send_stop(self) -> None:
        if not self.is_open or self.stopped:
            raise W8UTraceError(E_STATE, "STOP requires an active open stream")
        self.stopped = True

    def receive_end(self, end_code: int = 0) -> None:
        _require_int(end_code, "end_code", minimum=0, code=E_STATE)
        if end_code not in END_NUMERIC_ERRORS:
            raise W8UTraceError(E_STATE, f"unknown END numeric error code: {end_code}")
        if not self.stopped or self.ended:
            raise W8UTraceError(E_STATE, "END must follow STOP once")
        self.ended = True
        self.end_code = end_code

    def receive_eof(self) -> None:
        if not self.ended or self.eof_received:
            raise W8UTraceError(E_STATE, "EOF must follow END once")
        self.eof_received = True

    def join_reader(self, exit_code: int = 0) -> None:
        _require_int(exit_code, "reader_exit_code", code=E_STATE)
        if not self.eof_received or self.reader_joined:
            raise W8UTraceError(E_STATE, "reader join must follow EOF once")
        self.reader_joined = True
        self.reader_exit_code = exit_code
        self.is_open = False

    def assert_quiescent(self) -> None:
        if not (self.stopped and self.ended and self.eof_received and self.reader_joined):
            raise W8UTraceError(E_STATE, "mandatory STOP -> END -> EOF -> joined exit sequence incomplete")
        if self.end_code != 0 or self.reader_exit_code != 0:
            raise W8UTraceError(
                E_STATE,
                f"non-zero stream termination (end_code={self.end_code}, reader_exit_code={self.reader_exit_code})",
            )


def validate_injected_os_contract(evidence: Any) -> dict[str, Any]:
    """Validate revision-pinned `w8u-os-contract` evidence with independent accepted and rejected vectors."""
    if not isinstance(evidence, Mapping) or type(evidence) is not dict:
        raise W8UTraceError(E_STATE, "injected frame contract evidence must be a plain dict")
    required_keys = {"contract_id", "revision", "accepted_vectors", "rejected_vectors"}
    if set(evidence.keys()) != required_keys:
        raise W8UTraceError(E_STATE, f"os contract evidence keys must be {sorted(required_keys)}")
    if evidence["contract_id"] != OS_CONTRACT_ID:
        raise W8UTraceError(E_STATE, f"contract_id must be {OS_CONTRACT_ID!r}")
    revision = _require_nonempty_str(evidence["revision"], "revision", code=E_STATE)
    if not re.fullmatch(r"[0-9a-f]{7,64}|v[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{7,64}", revision):
        raise W8UTraceError(E_STATE, f"revision must be a pinned commit/version token, got {revision!r}")

    accepted = evidence["accepted_vectors"]
    rejected = evidence["rejected_vectors"]
    if not isinstance(accepted, (list, tuple)) or not accepted:
        raise W8UTraceError(E_STATE, "accepted_vectors must be a non-empty list")
    if not isinstance(rejected, (list, tuple)) or not rejected:
        raise W8UTraceError(E_STATE, "rejected_vectors must be a non-empty list")

    for idx, vec in enumerate(accepted):
        if not isinstance(vec, dict) or "grab_a" not in vec or "grab_b" not in vec:
            raise W8UTraceError(E_STATE, f"accepted_vectors[{idx}] must contain grab_a and grab_b")
        verify_same_grab_pair(vec["grab_a"], vec["grab_b"], code=E_STATE)

    for idx, vec in enumerate(rejected):
        if not isinstance(vec, dict) or "grab_a" not in vec or "grab_b" not in vec:
            raise W8UTraceError(E_STATE, f"rejected_vectors[{idx}] must contain grab_a and grab_b")
        failed = False
        try:
            verify_same_grab_pair(vec["grab_a"], vec["grab_b"], code=E_STATE)
        except W8UTraceError:
            failed = True
        if not failed:
            raise W8UTraceError(E_STATE, f"rejected_vectors[{idx}] unexpectedly passed verification")

    return copy.deepcopy(evidence)


# ============================================================================
# Action Contract (`w8u.action/v1`) and Action Gate Dispatcher
# ============================================================================

def validate_action_request(
    raw_action: bytes | bytearray | memoryview | str | dict[str, Any] | RawKeyValuePairs,
    *,
    screen_width: int,
    screen_height: int,
    max_body_bytes: int = ACTION_MAX_BODY_BYTES,
) -> tuple[dict[str, Any], str]:
    """Validate `w8u.action/v1` request before any actuator call. Returns `(canonical_action, action_sha256)`."""
    _require_int(screen_width, "screen_width", minimum=1, code=E_SCHEMA)
    _require_int(screen_height, "screen_height", minimum=1, code=E_SCHEMA)

    action = parse_strict_json_object(raw_action, max_bytes=max_body_bytes, code=E_SCHEMA)
    assert_metadata_only(action, "$.action")

    if sorted(action.keys()) != sorted(ACTION_KEYS):
        raise W8UTraceError(
            E_SCHEMA,
            f"action keys must be {sorted(ACTION_KEYS)}, got {sorted(action.keys())}",
        )
    if action["schema"] != ACTION_SCHEMA_ID:
        raise W8UTraceError(E_SCHEMA, f"action.schema must be {ACTION_SCHEMA_ID!r}")

    validate_run_id(action["run_id"], code=E_SCHEMA)
    _require_nonempty_str(action["lease_id"], "action.lease_id", code=E_SCHEMA)
    _require_int(action["epoch"], "action.epoch", minimum=0, code=E_SCHEMA)
    _require_int(action["generation"], "action.generation", minimum=1, code=E_SCHEMA)
    _require_nonempty_str(action["action_id"], "action.action_id", code=E_SCHEMA)
    _require_int(action["action_seq"], "action.action_seq", minimum=1, code=E_SCHEMA)
    _require_int(action["observed_frame_seq"], "action.observed_frame_seq", minimum=0, code=E_SCHEMA)
    _require_hex64(action["observed_frame_sha256"], "action.observed_frame_sha256", code=E_SCHEMA)
    _require_int(action["deadline_ns"], "action.deadline_ns", minimum=0, code=E_SCHEMA)
    _require_hex64(action["grant_sha256"], "action.grant_sha256", code=E_SCHEMA)

    verb = action["verb"]
    if not isinstance(verb, str) or verb not in ACTION_VERBS:
        raise W8UTraceError(E_VERB, f"unsupported action verb: {verb!r}")

    args = action["args"]
    if not isinstance(args, Mapping) or type(args) is not dict:
        raise W8UTraceError(E_SCHEMA, "action.args must be a plain dict")
    for k in args.keys():
        if k in godot.FORBIDDEN_AUTHORITY_KEYS:
            raise W8UTraceError(E_SCHEMA, f"authority key {k!r} forbidden inside action.args")

    if verb == "pointer":
        op = args.get("operation")
        if op == "move":
            if sorted(args.keys()) != sorted(POINTER_MOVE_KEYS):
                raise W8UTraceError(E_SCHEMA, "pointer move requires exact keys operation,x,y")
        elif op in ("click", "down", "up"):
            if sorted(args.keys()) != sorted(POINTER_BUTTON_KEYS):
                raise W8UTraceError(E_SCHEMA, f"pointer {op} requires exact keys operation,x,y,button")
            btn = _require_int(args["button"], "args.button", code=E_SCHEMA)
            if btn not in POINTER_BUTTONS:
                raise W8UTraceError(E_SCHEMA, f"pointer button must be in (1, 2, 3), got {btn}")
        elif op == "scroll":
            if sorted(args.keys()) != sorted(POINTER_SCROLL_KEYS):
                raise W8UTraceError(E_SCHEMA, "pointer scroll requires exact keys operation,x,y,dx,dy")
            dx = _require_int(args["dx"], "args.dx", minimum=POINTER_SCROLL_MIN, maximum=POINTER_SCROLL_MAX, code=E_SCHEMA)
            dy = _require_int(args["dy"], "args.dy", minimum=POINTER_SCROLL_MIN, maximum=POINTER_SCROLL_MAX, code=E_SCHEMA)
            if dx == 0 and dy == 0:
                raise W8UTraceError(E_SCHEMA, "pointer scroll requires at least one nonzero delta")
        else:
            raise W8UTraceError(E_VERB, f"unsupported pointer operation: {op!r}")

        x = _require_int(args["x"], "args.x", code=E_SCHEMA)
        y = _require_int(args["y"], "args.y", code=E_SCHEMA)
        if not (0 <= x < screen_width and 0 <= y < screen_height):
            raise W8UTraceError(
                E_SCHEMA,
                f"pointer coordinates ({x}, {y}) out of bounds (0..{screen_width - 1}, 0..{screen_height - 1})",
            )

    elif verb == "key":
        if sorted(args.keys()) != sorted(KEY_ARG_KEYS):
            raise W8UTraceError(E_SCHEMA, "key verb requires exact keys operation,key,modifiers")
        if args["operation"] not in KEY_OPERATIONS:
            raise W8UTraceError(E_VERB, f"invalid key operation: {args['operation']!r}")
        if args["key"] not in FINITE_KEYS:
            raise W8UTraceError(E_SCHEMA, f"invalid key {args['key']!r}; must be in finite key allowlist")
        mods = args["modifiers"]
        if not isinstance(mods, (list, tuple)) or any(not isinstance(m, str) for m in mods):
            raise W8UTraceError(E_SCHEMA, "key modifiers must be a list of strings")
        mods_list = list(mods)
        if any(m not in ORDERED_MODIFIERS for m in mods_list):
            raise W8UTraceError(E_SCHEMA, f"unknown modifier in {mods_list!r}")
        if len(set(mods_list)) != len(mods_list):
            raise W8UTraceError(E_SCHEMA, f"duplicate modifiers in {mods_list!r}")
        expected_order = [m for m in ORDERED_MODIFIERS if m in mods_list]
        if mods_list != expected_order:
            raise W8UTraceError(
                E_SCHEMA,
                f"modifiers {mods_list!r} must be ordered as {expected_order!r}",
            )
        action["args"]["modifiers"] = mods_list

    elif verb == "text":
        if sorted(args.keys()) != sorted(TEXT_ARG_KEYS):
            raise W8UTraceError(E_SCHEMA, "text verb requires exact keys mode,text")
        if args["mode"] != "type":
            raise W8UTraceError(E_VERB, f"text.mode must be 'type', got {args['mode']!r}")
        text_val = args["text"]
        if not isinstance(text_val, str) or "\x00" in text_val:
            raise W8UTraceError(E_SCHEMA, "text.text must be a valid UTF-8 string without NUL")
        try:
            text_bytes = text_val.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UTraceError(E_SCHEMA, f"text.text is not valid UTF-8: {exc}") from exc
        if len(text_bytes) > ACTION_TEXT_MAX_BYTES:
            raise W8UTraceError(
                E_SCHEMA,
                f"text.text UTF-8 byte length {len(text_bytes)} exceeds 4096-byte cap",
            )

    elif verb == "wait":
        if sorted(args.keys()) != sorted(WAIT_ARG_KEYS):
            raise W8UTraceError(E_SCHEMA, "wait verb requires exact key duration_ms")
        _require_int(args["duration_ms"], "args.duration_ms", minimum=0, maximum=ACTION_WAIT_MAX_MS, code=E_SCHEMA)

    canonical_bytes = schema.dumps(action).encode("utf-8")
    if len(canonical_bytes) > max_body_bytes:
        raise W8UTraceError(E_SCHEMA, f"canonical action body {len(canonical_bytes)} exceeds {max_body_bytes}")
    action_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    return action, action_sha256


def validate_action_response(resp: Any, *, code: str = E_SCHEMA) -> dict[str, Any]:
    """Validate the 11-key W8U action response contract."""
    if not isinstance(resp, Mapping) or type(resp) is not dict:
        raise W8UTraceError(code, "action response must be a plain dict")
    assert_metadata_only(resp, "$.action_response")
    if sorted(resp.keys()) != sorted(ACTION_RESPONSE_KEYS):
        raise W8UTraceError(
            code,
            f"action response keys must be {sorted(ACTION_RESPONSE_KEYS)}, got {sorted(resp.keys())}",
        )
    if resp["schema"] not in (ACTION_RESPONSE_SCHEMA_ID, ACTION_RESPONSE_ALT_SCHEMA_ID):
        raise W8UTraceError(code, f"invalid action response schema {resp['schema']!r}")
    _require_nonempty_str(resp["action_id"], "response.action_id", code=code)
    _require_int(resp["action_seq"], "response.action_seq", minimum=1, code=code)
    _require_nonempty_str(resp["lease_id"], "response.lease_id", code=code)
    _require_int(resp["epoch"], "response.epoch", minimum=0, code=code)

    status = resp["status"]
    if not isinstance(status, str) or status not in ACTION_STATUSES:
        raise W8UTraceError(code, f"invalid response.status: {status!r}")
    resp_code = resp["code"]
    if not isinstance(resp_code, str) or resp_code not in ACTION_CODES:
        raise W8UTraceError(code, f"invalid response.code: {resp_code!r}")
    emitted = _require_bool(resp["emitted"], "response.emitted", code=code)
    result_known = _require_bool(resp["result_known"], "response.result_known", code=code)

    applied_tick = resp["applied_tick"]
    if applied_tick is not None:
        _require_int(applied_tick, "response.applied_tick", minimum=0, code=code)
    outcome_sha256 = resp["outcome_sha256"]
    if outcome_sha256 is not None:
        _require_hex64(outcome_sha256, "response.outcome_sha256", code=code)

    if status == "refused":
        if resp_code == OK_ADMITTED or emitted or not result_known or applied_tick is not None or outcome_sha256 is not None:
            raise W8UTraceError(code, "refused action response cannot claim emission or applied outcome")
    elif status == "admitted":
        if resp_code != OK_ADMITTED or not emitted or applied_tick is not None or outcome_sha256 is not None:
            raise W8UTraceError(code, "admitted-only response cannot carry applied_tick or outcome_sha256")
    elif status == "applied":
        if resp_code != OK_ADMITTED or not emitted or not result_known:
            raise W8UTraceError(code, "applied response requires OK_ADMITTED, emitted=True, result_known=True")
    elif status == "uncertain":
        if resp_code != E_UNCERTAIN or not emitted or result_known or applied_tick is not None or outcome_sha256 is not None:
            raise W8UTraceError(code, "uncertain response requires E_UNCERTAIN, emitted=True, result_known=False")

    return copy.deepcopy(resp)


class ActionGateDispatcher:
    """Authenticates authority outside the payload, enforces action schema & preconditions before
    actuator invocation, provides idempotent replay on identical payload, refuses modified payloads,
    and quarantines the session on lost ack (`E_UNCERTAIN`).
    """

    def __init__(
        self,
        *,
        run_id: str,
        lease_id: str,
        epoch: int,
        generation: int,
        grant_sha256: str,
        screen_width: int,
        screen_height: int,
        clock: Callable[[], int],
        actuator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        budget: int = 1024,
        editable_focus: bool = True,
        available: bool = True,
    ) -> None:
        self.run_id = validate_run_id(run_id)
        self.lease_id = _require_nonempty_str(lease_id, "lease_id")
        self.epoch = _require_int(epoch, "epoch", minimum=0)
        self.generation = _require_int(generation, "generation", minimum=1)
        self.grant_sha256 = _require_hex64(grant_sha256, "grant_sha256")
        self.screen_width = _require_int(screen_width, "screen_width", minimum=1)
        self.screen_height = _require_int(screen_height, "screen_height", minimum=1)
        if not callable(clock):
            raise W8UTraceError(E_SCHEMA, "clock must be callable")
        self.clock = clock
        self.actuator = actuator
        self.budget = _require_int(budget, "budget", minimum=0)
        self.editable_focus = _require_bool(editable_focus, "editable_focus")
        self.available = _require_bool(available, "available")

        self.latest_frame_seq = 0
        self.latest_frame_sha256 = "0" * 64
        self.next_action_seq = 1
        self.quarantined = False
        self.actuator_calls: list[dict[str, Any]] = []
        self._by_action_id: dict[str, tuple[str, dict[str, Any], int]] = {}
        self._by_action_seq: dict[int, tuple[str, str, dict[str, Any], int]] = {}

    def update_observed_frame(self, frame_seq: int, frame_sha256: str) -> None:
        self.latest_frame_seq = _require_int(frame_seq, "frame_seq", minimum=0)
        self.latest_frame_sha256 = _require_hex64(frame_sha256, "frame_sha256")

    def _refusal(
        self,
        *,
        action_id: str,
        action_seq: int,
        code: str,
        rc: int,
    ) -> tuple[dict[str, Any], int]:
        resp = {
            "schema": ACTION_RESPONSE_SCHEMA_ID,
            "action_id": action_id,
            "action_seq": action_seq,
            "lease_id": self.lease_id,
            "epoch": self.epoch,
            "status": "refused",
            "code": code,
            "emitted": False,
            "result_known": True,
            "applied_tick": None,
            "outcome_sha256": None,
        }
        return resp, rc

    def dispatch(
        self,
        raw_action: bytes | bytearray | memoryview | str | dict[str, Any] | RawKeyValuePairs,
    ) -> tuple[dict[str, Any], int]:
        """Validate and dispatch an action request. Returns `(response_dict, gate_rc)`."""
        try:
            action, action_sha256 = validate_action_request(
                raw_action,
                screen_width=self.screen_width,
                screen_height=self.screen_height,
            )
        except W8UTraceError as exc:
            err_code = E_VERB if exc.code == E_VERB else E_SCHEMA
            return self._refusal(
                action_id="invalid",
                action_seq=max(1, self.next_action_seq),
                code=err_code,
                rc=RC_SCHEMA,
            )

        aid = action["action_id"]
        aseq = action["action_seq"]

        if aid in self._by_action_id:
            prev_sha, prev_resp, prev_rc = self._by_action_id[aid]
            if prev_sha == action_sha256 and prev_resp["action_seq"] == aseq:
                return copy.deepcopy(prev_resp), prev_rc
            return self._refusal(action_id=aid, action_seq=aseq, code=E_LOCK, rc=RC_REFUSED)

        if aseq in self._by_action_seq:
            prev_aid, prev_sha, prev_resp, prev_rc = self._by_action_seq[aseq]
            if prev_aid == aid and prev_sha == action_sha256:
                return copy.deepcopy(prev_resp), prev_rc
            return self._refusal(action_id=aid, action_seq=aseq, code=E_LOCK, rc=RC_REFUSED)

        if self.quarantined:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_LOCK, rc=RC_REFUSED)

        if not self.available:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_LOCK, rc=RC_UNAVAILABLE)

        if action["run_id"] != self.run_id or action["lease_id"] != self.lease_id or action["grant_sha256"] != self.grant_sha256:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_LOCK, rc=RC_REFUSED)
        if action["epoch"] != self.epoch or action["generation"] != self.generation:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_EPOCH, rc=RC_REFUSED)
        if aseq != self.next_action_seq:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_LOCK, rc=RC_REFUSED)

        if (
            action["observed_frame_seq"] != self.latest_frame_seq
            or action["observed_frame_sha256"] != self.latest_frame_sha256
        ):
            return self._refusal(action_id=aid, action_seq=aseq, code=E_STALE_FRAME, rc=RC_REFUSED)

        if action["verb"] == "text" and not self.editable_focus:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_FOCUS, rc=RC_REFUSED)

        if self.budget <= 0:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_BUDGET, rc=RC_REFUSED)

        now_ns = self.clock()
        wait_ns = (action["args"]["duration_ms"] * 1_000_000) if action["verb"] == "wait" else 0
        if now_ns + wait_ns > action["deadline_ns"]:
            return self._refusal(action_id=aid, action_seq=aseq, code=E_DEADLINE, rc=RC_REFUSED)

        self.budget -= 1
        self.next_action_seq += 1
        self.actuator_calls.append(copy.deepcopy(action))

        act_out = self.actuator(copy.deepcopy(action)) if self.actuator is not None else {"status": "admitted"}
        if not isinstance(act_out, dict):
            raise W8UTraceError(E_SCHEMA, "actuator callback must return a dict")

        if act_out.get("lost_ack") is True or act_out.get("status") == "uncertain":
            self.quarantined = True
            resp = {
                "schema": ACTION_RESPONSE_SCHEMA_ID,
                "action_id": aid,
                "action_seq": aseq,
                "lease_id": self.lease_id,
                "epoch": self.epoch,
                "status": "uncertain",
                "code": E_UNCERTAIN,
                "emitted": True,
                "result_known": False,
                "applied_tick": None,
                "outcome_sha256": None,
            }
            rc = RC_UNCERTAIN
        elif act_out.get("status") == "applied":
            resp = {
                "schema": ACTION_RESPONSE_SCHEMA_ID,
                "action_id": aid,
                "action_seq": aseq,
                "lease_id": self.lease_id,
                "epoch": self.epoch,
                "status": "applied",
                "code": OK_ADMITTED,
                "emitted": True,
                "result_known": True,
                "applied_tick": act_out.get("applied_tick"),
                "outcome_sha256": act_out.get("outcome_sha256"),
            }
            rc = RC_OK
        else:
            resp = {
                "schema": ACTION_RESPONSE_SCHEMA_ID,
                "action_id": aid,
                "action_seq": aseq,
                "lease_id": self.lease_id,
                "epoch": self.epoch,
                "status": "admitted",
                "code": OK_ADMITTED,
                "emitted": True,
                "result_known": bool(act_out.get("result_known", False)),
                "applied_tick": None,
                "outcome_sha256": None,
            }
            rc = RC_OK

        validate_action_response(resp)
        self._by_action_id[aid] = (action_sha256, copy.deepcopy(resp), rc)
        self._by_action_seq[aseq] = (aid, action_sha256, copy.deepcopy(resp), rc)
        return resp, rc


# ============================================================================
# T2 Command, Outcome, and LF-Inclusive Stream Digest Validation (`E_T2`)
# ============================================================================

def validate_t2_command(
    raw_record: bytes | bytearray | memoryview | str | dict[str, Any] | RawKeyValuePairs,
    producer_schemas: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate a T2 command record (`tick, seq, cmd, args`) against closed injected producer schemas."""
    if not isinstance(producer_schemas, Mapping) or not producer_schemas:
        raise W8UTraceError(E_T2, "closed producer_schemas mapping is required for T2 command validation")
    parsed = parse_strict_json_object(raw_record, code=E_T2)
    try:
        canonical = godot.validate_inbox_record(parsed)
    except (ValueError, godot.GodotError) as exc:
        raise W8UTraceError(E_T2, str(exc)) from exc

    cmd = canonical["cmd"]
    if cmd not in producer_schemas:
        raise W8UTraceError(E_T2, f"command {cmd!r} not in closed producer_schemas")

    cmd_spec = producer_schemas[cmd]
    if not isinstance(cmd_spec, Mapping):
        raise W8UTraceError(E_T2, f"invalid schema specification for command {cmd!r}")

    args = canonical["args"]
    allowed_keys = set(cmd_spec.get("allowed_args", cmd_spec.get("required_args", ()))) | set(
        cmd_spec.get("required_args", ())
    )
    required_keys = set(cmd_spec.get("required_args", ()))
    if "arg_types" in cmd_spec and isinstance(cmd_spec["arg_types"], Mapping):
        allowed_keys |= set(cmd_spec["arg_types"].keys())

    if required_keys - set(args.keys()):
        raise W8UTraceError(E_T2, f"missing required T2 args for {cmd!r}: {sorted(required_keys - set(args.keys()))}")
    if "arg_types" in cmd_spec or "allowed_args" in cmd_spec or "required_args" in cmd_spec:
        if set(args.keys()) - allowed_keys:
            raise W8UTraceError(E_T2, f"unknown T2 args for {cmd!r}: {sorted(set(args.keys()) - allowed_keys)}")

    arg_types = cmd_spec.get("arg_types")
    if isinstance(arg_types, Mapping):
        for k, expected_type in arg_types.items():
            if k not in args:
                continue
            val = args[k]
            if expected_type in ("int", int):
                _require_int(val, f"t2.args.{k}", code=E_T2)
            elif expected_type in ("str", str):
                _require_nonempty_str(val, f"t2.args.{k}", code=E_T2)
            elif expected_type in ("bool", bool):
                _require_bool(val, f"t2.args.{k}", code=E_T2)

    assert_metadata_only(canonical, "$.t2_command")
    return canonical


def validate_t2_outcome(
    raw_outcome: bytes | bytearray | memoryview | str | dict[str, Any] | RawKeyValuePairs,
) -> dict[str, Any]:
    """Validate a 7-key T2 outcome record (`seq, requested_tick, applied_tick, order_seq_id, status, reason, state_sha256`)."""
    outcome = parse_strict_json_object(raw_outcome, code=E_T2)
    assert_metadata_only(outcome, "$.t2_outcome")

    if sorted(outcome.keys()) != sorted(T2_OUTCOME_KEYS):
        raise W8UTraceError(
            E_T2,
            f"T2 outcome keys must be {sorted(T2_OUTCOME_KEYS)}, got {sorted(outcome.keys())}",
        )

    seq = _require_int(outcome["seq"], "outcome.seq", minimum=1, code=E_T2)
    requested_tick = _require_int(outcome["requested_tick"], "outcome.requested_tick", minimum=0, code=E_T2)
    applied_tick = _require_int(outcome["applied_tick"], "outcome.applied_tick", minimum=0, code=E_T2)
    order_seq_id = _require_int(outcome["order_seq_id"], "outcome.order_seq_id", minimum=1, code=E_T2)

    status = outcome["status"]
    if not isinstance(status, str) or status not in T2_OUTCOME_STATUSES:
        raise W8UTraceError(E_T2, f"invalid T2 outcome status: {status!r}")

    reason = outcome["reason"]
    if reason is not None:
        if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
            raise W8UTraceError(
                E_T2,
                f"outcome.reason must be null or match ^[A-Z][A-Z0-9_]*\\Z, got {reason!r}",
            )
    if status == "rejected" and reason is None:
        raise W8UTraceError(E_T2, "rejected T2 outcome must provide a non-null uppercase reason code")
    if status == "accepted" and reason is not None:
        raise W8UTraceError(E_T2, "accepted T2 outcome must have reason=null")

    state_sha256 = _require_hex64(outcome["state_sha256"], "outcome.state_sha256", code=E_T2)

    return {
        "seq": seq,
        "requested_tick": requested_tick,
        "applied_tick": applied_tick,
        "order_seq_id": order_seq_id,
        "status": status,
        "reason": reason,
        "state_sha256": state_sha256,
    }


def format_t2_jsonl_line(record: dict[str, Any]) -> bytes:
    """Format a validated T2 command or outcome dict as a canonical UTF-8 LF-terminated JSON-L line."""
    return (schema.dumps(record) + "\n").encode("utf-8")


def compute_t2_trace_sha256(lines_or_bytes: bytes | str | list[bytes | str | dict[str, Any]]) -> str:
    """Compute the literal LF-inclusive SHA-256 digest of a canonical T2 UTF-8 JSON-L stream."""
    if isinstance(lines_or_bytes, (bytes, bytearray, memoryview)):
        raw = bytes(lines_or_bytes)
        if b"\r" in raw:
            raise W8UTraceError(E_T2, "CR forbidden in canonical LF-delimited T2 stream")
        if not raw or not raw.endswith(b"\n"):
            raise W8UTraceError(E_T2, "T2 JSON-L stream must be non-empty and LF-terminated")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise W8UTraceError(E_T2, f"invalid UTF-8 in T2 stream: {exc}") from exc
        for line in text.split("\n")[:-1]:
            if not line:
                raise W8UTraceError(E_T2, "empty line in T2 JSON-L stream")
            parsed = parse_strict_json_object(line, code=E_T2)
            if schema.dumps(parsed) != line:
                raise W8UTraceError(E_T2, "non-canonical JSON line in T2 stream")
        return hashlib.sha256(raw).hexdigest()

    if isinstance(lines_or_bytes, str):
        return compute_t2_trace_sha256(lines_or_bytes.encode("utf-8"))

    if isinstance(lines_or_bytes, (list, tuple)) and lines_or_bytes:
        buf = bytearray()
        for item in lines_or_bytes:
            if isinstance(item, dict):
                buf.extend(format_t2_jsonl_line(item))
            elif isinstance(item, str):
                if not item.endswith("\n"):
                    raise W8UTraceError(E_T2, "every T2 JSON-L line must include trailing LF")
                buf.extend(item.encode("utf-8"))
            elif isinstance(item, (bytes, bytearray, memoryview)):
                b = bytes(item)
                if not b.endswith(b"\n"):
                    raise W8UTraceError(E_T2, "every T2 JSON-L line must include trailing LF")
                buf.extend(b)
            else:
                raise W8UTraceError(E_T2, f"unsupported T2 stream item: {type(item)!r}")
        return compute_t2_trace_sha256(bytes(buf))

    raise W8UTraceError(E_T2, "T2 stream cannot be empty when computing t2_trace_sha256")


def compute_t2_terminal_sha256(outcomes: list[dict[str, Any]]) -> str:
    """Compute the terminal T2 outcome chain digest across validated T2 outcomes."""
    if not outcomes:
        raise W8UTraceError(E_T2, "cannot compute t2_terminal_sha256 on empty T2 outcome sequence")
    prev = b"\x00" * 32
    for outcome in outcomes:
        v = validate_t2_outcome(outcome)
        payload = schema.dumps(v).encode("utf-8") + b"\n"
        prev = hashlib.sha256(b"w8u.t2.terminal/v1\x00" + prev + payload).digest()
    return prev.hex()


def compute_action_log_sha256(actions: list[dict[str, Any]]) -> str:
    """Compute canonical `action_log_sha256` over the sequence of validated actions."""
    buf = bytearray()
    for act in actions:
        assert_metadata_only(act, "$.action_log")
        buf.extend((schema.dumps(act) + "\n").encode("utf-8"))
    return hashlib.sha256(b"w8u.action_log.jsonl/v1\x00" + bytes(buf)).hexdigest()


# ============================================================================
# Genesis Domain (`w8u.genesis/v1`) and Transition Hash Chain (`w8u.transition/v1`)
# ============================================================================

def build_run_descriptor(
    *,
    run_id: str,
    lease_id: str,
    target_handle: Any,
    epoch: int,
    t2_enabled: bool = True,
    session_id: str = "session-0",
) -> dict[str, Any]:
    """Build canonical authenticated run descriptor rooted in `w8u.genesis/v1`."""
    return {
        "run_id": validate_run_id(run_id, code=E_CHAIN),
        "lease_id": _require_nonempty_str(lease_id, "lease_id", code=E_CHAIN),
        "target_handle": normalize_target_handle(target_handle, code=E_CHAIN),
        "epoch": _require_int(epoch, "epoch", minimum=0, code=E_CHAIN),
        "t2_enabled": _require_bool(t2_enabled, "t2_enabled", code=E_CHAIN),
        "session_id": _require_nonempty_str(session_id, "session_id", code=E_CHAIN),
    }


def compute_genesis_sha256(run_descriptor: dict[str, Any]) -> str:
    """Hash authenticated run descriptor with the distinct TARGET-frozen genesis domain `w8u.genesis/v1`."""
    if not isinstance(run_descriptor, Mapping) or type(run_descriptor) is not dict:
        raise W8UTraceError(E_CHAIN, "run_descriptor must be a plain dict")
    assert_metadata_only(run_descriptor, "$.run_descriptor")
    required = {"run_id", "lease_id", "target_handle", "epoch"}
    if not required.issubset(run_descriptor.keys()):
        raise W8UTraceError(E_CHAIN, f"run_descriptor missing required keys: {sorted(required - set(run_descriptor.keys()))}")
    validate_run_id(run_descriptor["run_id"], code=E_CHAIN)
    _require_nonempty_str(run_descriptor["lease_id"], "run_descriptor.lease_id", code=E_CHAIN)
    normalize_target_handle(run_descriptor["target_handle"], code=E_CHAIN)
    _require_int(run_descriptor["epoch"], "run_descriptor.epoch", minimum=0, code=E_CHAIN)

    canonical_bytes = schema.dumps(run_descriptor).encode("utf-8")
    return hashlib.sha256(GENESIS_DOMAIN.encode("utf-8") + b"\x00" + canonical_bytes).hexdigest()


def compute_transition_chain_sha256(transition_record: dict[str, Any], previous_chain_sha256: str) -> str:
    """Compute `chain_sha256` over `w8u.transition/v1\\x00` + 32-byte raw `previous_chain_sha256` + canonical record without `chain_sha256`."""
    _require_hex64(previous_chain_sha256, "previous_chain_sha256", code=E_CHAIN)
    if transition_record.get("previous_chain_sha256") != previous_chain_sha256:
        raise W8UTraceError(E_CHAIN, "transition.previous_chain_sha256 does not match previous chain digest")
    record_without_chain = {k: v for k, v in transition_record.items() if k != "chain_sha256"}
    canonical_bytes = schema.dumps(record_without_chain).encode("utf-8")
    raw_prev = bytes.fromhex(previous_chain_sha256)
    return hashlib.sha256(
        TRANSITION_CHAIN_DOMAIN.encode("utf-8") + b"\x00" + raw_prev + canonical_bytes
    ).hexdigest()


def validate_transition_record(
    raw_transition: bytes | bytearray | memoryview | str | dict[str, Any] | RawKeyValuePairs,
    *,
    t2_enabled: bool | None = None,
    code: str = E_CHAIN,
) -> dict[str, Any]:
    """Validate the versioned `w8u.transition/v1` record schema and recursive metadata allowlist."""
    tr = parse_strict_json_object(raw_transition, code=code)
    assert_metadata_only(tr, "$.transition")

    keys = tuple(sorted(tr.keys()))
    if keys not in (tuple(sorted(TRANSITION_CORE_KEYS)), tuple(sorted(TRANSITION_KEYS_WITH_SCHEMA))):
        raise W8UTraceError(code, f"invalid transition keys: {sorted(tr.keys())}")
    if "schema" in tr and tr["schema"] != TRANSITION_SCHEMA_ID:
        raise W8UTraceError(code, f"transition.schema must be {TRANSITION_SCHEMA_ID!r}")

    validate_run_id(tr["run_id"], code=code)
    _require_nonempty_str(tr["lease_id"], "transition.lease_id", code=code)
    tr["target_handle"] = normalize_target_handle(tr["target_handle"], code=code)
    _require_int(tr["epoch"], "transition.epoch", minimum=0, code=code)
    _require_int(tr["row_seq"], "transition.row_seq", minimum=1, code=code)
    _require_nonempty_str(tr["action_id"], "transition.action_id", code=code)
    _require_hex64(tr["action_sha256"], "transition.action_sha256", code=code)
    _require_hex64(tr["pre_ui_state_sha256"], "transition.pre_ui_state_sha256", code=E_STATE)
    _require_hex64(tr["post_ui_state_sha256"], "transition.post_ui_state_sha256", code=E_STATE)

    if tr["pre_frame"] is not None:
        tr["pre_frame"] = validate_frame_metadata(tr["pre_frame"], code=E_STATE)
    if tr["post_frame"] is not None:
        tr["post_frame"] = validate_frame_metadata(tr["post_frame"], code=E_STATE)

    if tr["t2_outcome"] is not None:
        if t2_enabled is False:
            raise W8UTraceError(E_T2, "non-T2 policy transition must retain t2_outcome=null")
        tr["t2_outcome"] = validate_t2_outcome(tr["t2_outcome"])
    elif t2_enabled is True:
        raise W8UTraceError(E_T2, "T2 policy transition requires non-null t2_outcome")

    for sim_field in ("simulation_pre_sha256", "simulation_post_sha256"):
        val = tr[sim_field]
        if val is not None:
            if t2_enabled is False:
                raise W8UTraceError(E_T2, f"non-T2 policy transition must retain {sim_field}=null")
            _require_hex64(val, f"transition.{sim_field}", code=E_STATE)
        elif t2_enabled is True:
            raise W8UTraceError(E_T2, f"T2 policy transition requires {sim_field}")

    for sim_field in ("simulation_pre_sha256", "simulation_post_sha256"):
        sim_val = tr[sim_field]
        if sim_val is not None and sim_val in (tr["pre_ui_state_sha256"], tr["post_ui_state_sha256"]):
            raise W8UTraceError(E_STATE, f"simulation hash ({sim_field}) substituted as UI state hash")
    if tr["t2_outcome"] is not None:
        out_sha = tr["t2_outcome"]["state_sha256"]
        if out_sha in (tr["pre_ui_state_sha256"], tr["post_ui_state_sha256"]):
            raise W8UTraceError(E_STATE, "T2 outcome simulation state_sha256 substituted as UI state hash")

    pres_id = tr["presentation_id"]
    if pres_id is not None and not (_is_int(pres_id) or isinstance(pres_id, str)):
        raise W8UTraceError(code, "transition.presentation_id must be int, str, or null")

    gate_rc = _require_int(tr["gate_rc"], "transition.gate_rc", code=code)
    if gate_rc not in VALID_GATE_RCS:
        raise W8UTraceError(code, f"invalid transition.gate_rc: {gate_rc}")
    _require_nonempty_str(tr["phase"], "transition.phase", code=code)
    _require_hex64(tr["previous_chain_sha256"], "transition.previous_chain_sha256", code=E_CHAIN)
    _require_hex64(tr["chain_sha256"], "transition.chain_sha256", code=E_CHAIN)

    expected_chain = compute_transition_chain_sha256(tr, tr["previous_chain_sha256"])
    if tr["chain_sha256"] != expected_chain:
        raise W8UTraceError(E_CHAIN, f"transition chain_sha256 mismatch at row_seq={tr['row_seq']}")

    return tr


# ============================================================================
# Receipt (`w8u.receipt/v1`) Validation
# ============================================================================

def validate_receipt(
    raw_receipt: bytes | bytearray | memoryview | str | dict[str, Any] | RawKeyValuePairs,
    *,
    t2_enabled: bool | None = None,
    pixel_required: bool = True,
) -> dict[str, Any]:
    """Validate a `w8u.receipt/v1` object and its metadata-only, frame, state, and T2 invariants."""
    rec = parse_strict_json_object(raw_receipt, code=E_STATE)
    assert_metadata_only(rec, "$.receipt")

    keys = tuple(sorted(rec.keys()))
    if keys not in (tuple(sorted(RECEIPT_CORE_KEYS)), tuple(sorted(RECEIPT_KEYS_WITH_SCHEMA))):
        raise W8UTraceError(
            E_STATE,
            f"receipt keys do not match w8u.receipt/v1 contract: got {sorted(rec.keys())}",
        )
    if "schema" in rec and rec["schema"] != RECEIPT_SCHEMA_ID:
        raise W8UTraceError(E_STATE, f"receipt.schema must be {RECEIPT_SCHEMA_ID!r}")

    run_id = validate_run_id(rec["run_id"], code=E_STATE)
    _require_nonempty_str(rec["lease_id"], "receipt.lease_id", code=E_STATE)
    rec["target_handle"] = normalize_target_handle(rec["target_handle"], code=E_STATE)
    _require_int(rec["epoch"], "receipt.epoch", minimum=0, code=E_STATE)

    if rec["frames_root"] != FRAMES_ROOT:
        raise W8UTraceError(E_STATE, f"receipt.frames_root must be {FRAMES_ROOT!r}")
    expected_run_root = frame_run_root_for(run_id, code=E_STATE)
    if rec["frame_run_root"] != expected_run_root:
        raise W8UTraceError(E_STATE, f"receipt.frame_run_root must be {expected_run_root!r}")
    if rec["source"] != FRAME_SOURCE:
        raise W8UTraceError(E_STATE, f"receipt.source must be {FRAME_SOURCE!r}")

    frames_captured = _require_int(rec["frames_captured"], "receipt.frames_captured", minimum=0, code=E_STATE)
    first_frame = rec["first_frame"]
    last_frame = rec["last_frame"]
    if frames_captured == 0:
        if first_frame is not None or last_frame is not None:
            raise W8UTraceError(E_STATE, "zero-frame run requires first_frame=null and last_frame=null")
        if pixel_required and rec["ui_gate_verdict"] != "INDETERMINATE":
            raise W8UTraceError(
                E_STATE,
                f"pixel-required zero-frame run must have ui_gate_verdict='INDETERMINATE', got {rec['ui_gate_verdict']!r}",
            )
    else:
        if first_frame is None or last_frame is None:
            raise W8UTraceError(E_STATE, "positive frames_captured requires non-null first_frame and last_frame")
        rec["first_frame"] = validate_frame_metadata(first_frame, code=E_STATE)
        rec["last_frame"] = validate_frame_metadata(last_frame, code=E_STATE)
        if rec["first_frame"]["seq"] > rec["last_frame"]["seq"]:
            raise W8UTraceError(E_STATE, "first_frame.seq cannot exceed last_frame.seq")

    ui_actions = _require_int(rec["ui_actions_count"], "receipt.ui_actions_count", minimum=0, code=E_STATE)
    admitted = _require_int(rec["admitted_actions"], "receipt.admitted_actions", minimum=0, code=E_STATE)
    refused = _require_int(rec["refused_actions"], "receipt.refused_actions", minimum=0, code=E_STATE)
    uncertain = _require_int(rec["uncertain_actions"], "receipt.uncertain_actions", minimum=0, code=E_STATE)
    if ui_actions != admitted + refused + uncertain:
        raise W8UTraceError(E_STATE, "ui_actions_count must equal admitted_actions + refused_actions + uncertain_actions")

    _require_hex64(rec["action_log_sha256"], "receipt.action_log_sha256", code=E_CHAIN)

    if t2_enabled is False:
        for k in ("t2_trace_sha256", "t2_terminal_sha256", "t2_ticks", "simulation_state_sha256"):
            if rec[k] is not None:
                raise W8UTraceError(E_T2, f"non-T2 policy receipt must retain {k}=null")
    elif t2_enabled is True:
        _require_hex64(rec["t2_trace_sha256"], "receipt.t2_trace_sha256", code=E_T2)
        _require_hex64(rec["t2_terminal_sha256"], "receipt.t2_terminal_sha256", code=E_T2)
        _require_int(rec["t2_ticks"], "receipt.t2_ticks", minimum=0, code=E_T2)
        _require_hex64(rec["simulation_state_sha256"], "receipt.simulation_state_sha256", code=E_T2)
    else:
        if rec["t2_trace_sha256"] is not None:
            _require_hex64(rec["t2_trace_sha256"], "receipt.t2_trace_sha256", code=E_T2)
        if rec["t2_terminal_sha256"] is not None:
            _require_hex64(rec["t2_terminal_sha256"], "receipt.t2_terminal_sha256", code=E_T2)
        if rec["t2_ticks"] is not None:
            _require_int(rec["t2_ticks"], "receipt.t2_ticks", minimum=0, code=E_T2)
        if rec["simulation_state_sha256"] is not None:
            _require_hex64(rec["simulation_state_sha256"], "receipt.simulation_state_sha256", code=E_T2)

    if rec["final_state_sha256"] is not None:
        _require_hex64(rec["final_state_sha256"], "receipt.final_state_sha256", code=E_STATE)
    if rec["final_state_sha256"] is not None and rec["final_state_sha256"] == rec["simulation_state_sha256"]:
        raise W8UTraceError(E_STATE, "simulation_state_sha256 cannot be substituted as final_state_sha256")

    _require_nonempty_str(rec["ui_gate_verdict"], "receipt.ui_gate_verdict", code=E_STATE)
    _require_nonempty_str(rec["ui_gate_code"], "receipt.ui_gate_code", code=E_STATE)

    for int_field in (
        "pair_mismatches",
        "frame_gaps",
        "frames_dropped",
        "x11_tcp_listeners",
        "x11_unix_listeners",
        "x11_abstract_listeners",
    ):
        _require_int(rec[int_field], f"receipt.{int_field}", minimum=0, code=E_STATE)

    if rec["pair_mismatches"] > 0:
        raise W8UTraceError(E_STATE, f"receipt reports pair_mismatches={rec['pair_mismatches']}")
    if rec["frame_gaps"] > 0:
        raise W8UTraceError(E_STATE, f"receipt reports frame_gaps={rec['frame_gaps']}")
    if rec["x11_tcp_listeners"] != 0 or rec["x11_unix_listeners"] != 0 or rec["x11_abstract_listeners"] != 0:
        raise W8UTraceError(E_STATE, "X11 listener leak detected in receipt")

    reader_exit = _require_int(rec["reader_exit_code"], "receipt.reader_exit_code", code=E_STATE)
    reader_quiescent = _require_bool(rec["reader_quiescent"], "receipt.reader_quiescent", code=E_STATE)
    if reader_exit != 0 or not reader_quiescent:
        raise W8UTraceError(E_STATE, "reader must exit cleanly (exit_code=0) and be quiescent")

    ram_purged = _require_bool(rec["ram_purged"], "receipt.ram_purged", code=E_PIXEL_LEAK)
    frames_disk_bytes = _require_int(rec["frames_disk_bytes"], "receipt.frames_disk_bytes", minimum=0, code=E_PIXEL_LEAK)
    swap_delta_bytes = _require_int(rec["swap_delta_bytes"], "receipt.swap_delta_bytes", minimum=0, code=E_PIXEL_LEAK)
    metadata_only = _require_bool(rec["metadata_only"], "receipt.metadata_only", code=E_PIXEL_LEAK)

    if not metadata_only or frames_disk_bytes != 0 or swap_delta_bytes != 0 or not ram_purged:
        raise W8UTraceError(
            E_PIXEL_LEAK,
            "receipt violates metadata-only or volatile RAM purge requirements",
        )

    return rec


# ============================================================================
# W8U Metadata-Only Trace Recorder
# ============================================================================

class W8UTraceRecorder:
    """Complete Wave 1 metadata-only recorder for W8U transition, action, and T2 streams."""

    def __init__(
        self,
        *,
        run_id: str,
        lease_id: str,
        target_handle: Any,
        epoch: int = 0,
        session_id: str = "session-0",
        t2_enabled: bool = True,
        pixel_required: bool = True,
        producer_schemas: Mapping[str, Mapping[str, Any]] | None = None,
        initial_simulation_sha256: str | None = None,
        reserved_capacity: int = 2,
        limits: TraceLimits | None = None,
        sink: ApprovedMetadataSink | Callable[[Any], None] | None = None,
        approved_sinks: set[Any] | frozenset[Any] | None = None,
        os_contract_evidence: dict[str, Any] | None = None,
    ) -> None:
        self.limits = limits or TraceLimits()
        self.run_id = validate_run_id(run_id)
        self.lease_id = _require_nonempty_str(lease_id, "lease_id")
        self.target_handle = normalize_target_handle(target_handle)
        self.epoch = _require_int(epoch, "epoch", minimum=0)
        self.session_id = _require_nonempty_str(session_id, "session_id")
        self.t2_enabled = _require_bool(t2_enabled, "t2_enabled")
        self.pixel_required = _require_bool(pixel_required, "pixel_required")
        self.producer_schemas = dict(producer_schemas) if producer_schemas is not None else {}

        if self.t2_enabled:
            if not self.producer_schemas:
                raise W8UTraceError(E_T2, "T2-enabled recorder requires closed producer_schemas")
            if initial_simulation_sha256 is None:
                raise W8UTraceError(E_T2, "T2-enabled recorder requires initial_simulation_sha256")
            self.committed_simulation_sha256: str | None = _require_hex64(
                initial_simulation_sha256, "initial_simulation_sha256", code=E_T2
            )
            self.committed_tick: int | None = 0
        else:
            if initial_simulation_sha256 is not None:
                raise W8UTraceError(E_T2, "non-T2 policy recorder must keep initial_simulation_sha256=None")
            self.committed_simulation_sha256 = None
            self.committed_tick = None

        if sink is not None:
            verify_approved_sink(sink, approved_sinks)
        self.sink = sink
        self.approved_sinks = approved_sinks

        if os_contract_evidence is not None:
            self.os_contract_evidence = validate_injected_os_contract(os_contract_evidence)
        else:
            self.os_contract_evidence = None

        self.credit_controller = FrameCreditController(reserved_capacity=reserved_capacity)
        self.run_descriptor = build_run_descriptor(
            run_id=self.run_id,
            lease_id=self.lease_id,
            target_handle=self.target_handle,
            epoch=self.epoch,
            t2_enabled=self.t2_enabled,
            session_id=self.session_id,
        )
        self.genesis_sha256 = compute_genesis_sha256(self.run_descriptor)
        self.last_chain_sha256 = self.genesis_sha256

        self.actions: list[dict[str, Any]] = []
        self.action_responses: list[dict[str, Any]] = []
        self.t2_commands: list[dict[str, Any]] = []
        self.t2_outcomes: list[dict[str, Any]] = []
        self.t2_jsonl_lines: list[bytes] = []
        self.transitions: list[dict[str, Any]] = []
        self.state_digests: list[str] = []

        self._volatile_frames: list[Any] = []
        self._volatile_states: list[dict[str, Any]] = []
        self.ram_purged = False
        self.finalized = False

    def record_step(
        self,
        *,
        row_seq: int,
        action: bytes | str | dict[str, Any] | RawKeyValuePairs,
        action_response: dict[str, Any],
        pre_state: dict[str, Any],
        post_state: dict[str, Any],
        pre_frame: dict[str, Any] | None,
        post_frame: dict[str, Any] | None,
        screen_width: int,
        screen_height: int,
        t2_command: bytes | str | dict[str, Any] | RawKeyValuePairs | None = None,
        t2_outcome: bytes | str | dict[str, Any] | RawKeyValuePairs | None = None,
        simulation_pre_sha256: str | None = None,
        simulation_post_sha256: str | None = None,
        session_id: str | None = None,
        presentation_id: str | int | None = None,
        gate_rc: int = RC_OK,
        phase: str = "act",
    ) -> dict[str, Any]:
        """Validate all inputs and join predicates BEFORE mutating recorder state, then append transition."""
        if self.finalized:
            raise W8UTraceError(E_STATE, "cannot record step after finalize()")

        if len(self.transitions) >= self.limits.max_transitions:
            raise W8UTraceError(E_SCHEMA, f"max_transitions limit ({self.limits.max_transitions}) exceeded")
        if len(self.actions) >= self.limits.max_actions:
            raise W8UTraceError(E_SCHEMA, f"max_actions limit ({self.limits.max_actions}) exceeded")
        if self.t2_enabled and len(self.t2_outcomes) >= self.limits.max_t2_records:
            raise W8UTraceError(E_T2, f"max_t2_records limit ({self.limits.max_t2_records}) exceeded")

        expected_row_seq = len(self.transitions) + 1
        _require_int(row_seq, "row_seq", minimum=1, code=E_JOIN)
        if row_seq != expected_row_seq:
            raise W8UTraceError(
                E_JOIN,
                f"row_seq discontinuity: expected {expected_row_seq}, got {row_seq}",
            )

        if session_id is not None and session_id != self.session_id:
            raise W8UTraceError(
                E_JOIN,
                f"wrong session_id: expected {self.session_id!r}, got {session_id!r}",
            )

        val_action, action_sha256 = validate_action_request(
            action,
            screen_width=screen_width,
            screen_height=screen_height,
            max_body_bytes=self.limits.max_action_body_bytes,
        )
        val_resp = validate_action_response(action_response)

        if (
            val_action["run_id"] != self.run_id
            or val_action["lease_id"] != self.lease_id
            or val_action["epoch"] != self.epoch
            or val_action["generation"] != self.target_handle["generation"]
        ):
            raise W8UTraceError(E_JOIN, "action authority envelope does not match authenticated run/session")
        if val_action["action_seq"] != row_seq:
            raise W8UTraceError(
                E_JOIN,
                f"action_seq {val_action['action_seq']} does not match transition row_seq {row_seq}",
            )
        if val_resp["action_id"] != val_action["action_id"] or val_resp["action_seq"] != val_action["action_seq"]:
            raise W8UTraceError(E_JOIN, "action_response does not match action_id/action_seq")

        val_pre_state = validate_state_strict(pre_state)
        val_post_state = validate_state_strict(post_state)
        pre_ui_sha256 = compute_state_sha256(val_pre_state)
        post_ui_sha256 = compute_state_sha256(val_post_state)

        if self.state_digests and pre_ui_sha256 != self.state_digests[-1]:
            raise W8UTraceError(E_JOIN, "pre_ui_state_sha256 does not match previous post_ui_state_sha256")

        val_pre_frame = validate_frame_metadata(pre_frame, code=E_STATE) if pre_frame is not None else None
        val_post_frame = validate_frame_metadata(post_frame, code=E_STATE) if post_frame is not None else None
        if val_pre_frame is not None:
            if (
                val_action["observed_frame_seq"] != val_pre_frame["seq"]
                or val_action["observed_frame_sha256"] != val_pre_frame["sha256"]
            ):
                raise W8UTraceError(E_JOIN, "action observed_frame does not match pre_frame metadata")
        if val_pre_frame is not None and val_post_frame is not None:
            if val_post_frame["seq"] < val_pre_frame["seq"]:
                raise W8UTraceError(E_JOIN, "post_frame.seq regressed relative to pre_frame.seq")

        val_t2_cmd: dict[str, Any] | None = None
        val_t2_out: dict[str, Any] | None = None
        if self.t2_enabled:
            if t2_command is None or t2_outcome is None:
                raise W8UTraceError(E_T2, "T2 policy requires both t2_command and t2_outcome")
            if simulation_pre_sha256 is None or simulation_post_sha256 is None:
                raise W8UTraceError(E_T2, "T2 policy requires simulation_pre_sha256 and simulation_post_sha256")
            _require_hex64(simulation_pre_sha256, "simulation_pre_sha256", code=E_STATE)
            _require_hex64(simulation_post_sha256, "simulation_post_sha256", code=E_STATE)

            val_t2_cmd = validate_t2_command(t2_command, self.producer_schemas)
            val_t2_out = validate_t2_outcome(t2_outcome)

            expected_t2_seq = len(self.t2_outcomes) + 1
            if val_t2_cmd["seq"] != expected_t2_seq or val_t2_out["seq"] != expected_t2_seq:
                raise W8UTraceError(
                    E_JOIN,
                    f"T2 sequence mismatch: expected {expected_t2_seq}, got cmd={val_t2_cmd['seq']}, outcome={val_t2_out['seq']}",
                )
            if val_t2_out["seq"] != val_action["action_seq"]:
                raise W8UTraceError(E_JOIN, "T2 outcome seq does not match action_seq")
            if self.t2_outcomes and val_t2_out["order_seq_id"] <= self.t2_outcomes[-1]["order_seq_id"]:
                raise W8UTraceError(E_T2, "T2 outcome order_seq_id must strictly increase")

            if val_t2_cmd["tick"] != val_t2_out["requested_tick"]:
                raise W8UTraceError(
                    E_JOIN,
                    f"T2 command tick ({val_t2_cmd['tick']}) != outcome requested_tick ({val_t2_out['requested_tick']})",
                )
            cmd_spec = self.producer_schemas[val_t2_cmd["cmd"]]
            is_economic = cmd_spec.get("type", "economic") == "economic"
            assert self.committed_tick is not None
            if is_economic and val_t2_out["status"] == "accepted":
                if val_t2_out["requested_tick"] != self.committed_tick + 1:
                    raise W8UTraceError(
                        E_JOIN,
                        f"wrong economic tick: expected {self.committed_tick + 1}, got {val_t2_out['requested_tick']}",
                    )
                if val_t2_out["applied_tick"] != val_t2_out["requested_tick"]:
                    raise W8UTraceError(E_JOIN, "accepted economic outcome applied_tick != requested_tick")

            if simulation_pre_sha256 != self.committed_simulation_sha256:
                raise W8UTraceError(
                    E_JOIN,
                    f"stale simulation basis: expected {self.committed_simulation_sha256}, got {simulation_pre_sha256}",
                )

            if val_t2_out["status"] == "accepted":
                if val_resp["status"] != "applied":
                    raise W8UTraceError(
                        E_T2,
                        "admitted-only action response cannot claim accepted economic/T2 outcome",
                    )
                if simulation_post_sha256 != val_t2_out["state_sha256"]:
                    raise W8UTraceError(E_JOIN, "accepted outcome state_sha256 != simulation_post_sha256")
            elif val_t2_out["status"] == "rejected":
                if simulation_post_sha256 != simulation_pre_sha256 or val_t2_out["state_sha256"] != simulation_pre_sha256:
                    raise W8UTraceError(
                        E_T2,
                        "rejected T2 outcome must preserve simulation_pre_sha256 without advancing state",
                    )

            if val_pre_frame is not None and val_pre_frame["tick_valid"]:
                if val_pre_frame["tick"] != self.committed_tick:
                    raise W8UTraceError(
                        E_JOIN,
                        f"pre_frame.tick ({val_pre_frame['tick']}) != committed_tick ({self.committed_tick})",
                    )
            if val_post_frame is not None and val_post_frame["tick_valid"]:
                if val_post_frame["tick"] != val_t2_out["applied_tick"]:
                    raise W8UTraceError(
                        E_JOIN,
                        f"post_frame.tick ({val_post_frame['tick']}) != outcome.applied_tick ({val_t2_out['applied_tick']})",
                    )
        else:
            if any(x is not None for x in (t2_command, t2_outcome, simulation_pre_sha256, simulation_post_sha256, presentation_id)):
                raise W8UTraceError(E_T2, "non-T2 policy retains null for all T2/simulation fields")

        transition_draft = {
            "schema": TRANSITION_SCHEMA_ID,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "target_handle": copy.deepcopy(self.target_handle),
            "epoch": self.epoch,
            "row_seq": row_seq,
            "action_id": val_action["action_id"],
            "action_sha256": action_sha256,
            "pre_ui_state_sha256": pre_ui_sha256,
            "post_ui_state_sha256": post_ui_sha256,
            "pre_frame": val_pre_frame,
            "post_frame": val_post_frame,
            "t2_outcome": val_t2_out,
            "simulation_pre_sha256": simulation_pre_sha256,
            "simulation_post_sha256": simulation_post_sha256,
            "presentation_id": presentation_id,
            "gate_rc": gate_rc,
            "phase": phase,
            "previous_chain_sha256": self.last_chain_sha256,
        }
        chain_sha256 = compute_transition_chain_sha256(transition_draft, self.last_chain_sha256)
        transition_draft["chain_sha256"] = chain_sha256
        validated_transition = validate_transition_record(transition_draft, t2_enabled=self.t2_enabled)

        if self.sink is not None:
            verify_approved_sink(self.sink, self.approved_sinks)
            self.sink(validated_transition)

        self.actions.append(val_action)
        self.action_responses.append(val_resp)
        if not self.state_digests:
            self.state_digests.append(pre_ui_sha256)
        self.state_digests.append(post_ui_sha256)
        self._volatile_states.extend([val_pre_state, val_post_state])

        if self.t2_enabled and val_t2_cmd is not None and val_t2_out is not None:
            self.t2_commands.append(val_t2_cmd)
            self.t2_outcomes.append(val_t2_out)
            self.t2_jsonl_lines.append(format_t2_jsonl_line(val_t2_cmd))
            self.t2_jsonl_lines.append(format_t2_jsonl_line(val_t2_out))
            self.committed_simulation_sha256 = simulation_post_sha256
            if val_t2_out["status"] == "accepted":
                self.committed_tick = val_t2_out["applied_tick"]

        self.last_chain_sha256 = chain_sha256
        self.transitions.append(validated_transition)
        return copy.deepcopy(validated_transition)

    def purge_volatile_ram(self) -> None:
        """Purge volatile frame buffers and raw STATE dicts from memory."""
        self._volatile_frames.clear()
        self._volatile_states.clear()
        self.ram_purged = True

    def finalize_receipt(self) -> dict[str, Any]:
        """Purge volatile RAM, verify stream quiescence, and build canonical `w8u.receipt/v1`."""
        self.credit_controller.assert_quiescent()
        self.purge_volatile_ram()
        self.finalized = True

        frames = self.credit_controller.frames_metadata
        frames_captured = len(frames)
        first_frame = copy.deepcopy(frames[0]) if frames_captured > 0 else None
        last_frame = copy.deepcopy(frames[-1]) if frames_captured > 0 else None

        admitted_count = sum(1 for r in self.action_responses if r["status"] in ("admitted", "applied"))
        refused_count = sum(1 for r in self.action_responses if r["status"] == "refused")
        uncertain_count = sum(1 for r in self.action_responses if r["status"] == "uncertain")
        ui_actions_count = admitted_count + refused_count + uncertain_count

        action_log_sha256 = compute_action_log_sha256(self.actions)
        if self.t2_enabled:
            t2_trace_sha256: str | None = compute_t2_trace_sha256(self.t2_jsonl_lines)
            t2_terminal_sha256: str | None = compute_t2_terminal_sha256(self.t2_outcomes)
            t2_ticks: int | None = self.committed_tick
            sim_sha256: str | None = self.committed_simulation_sha256
        else:
            t2_trace_sha256 = None
            t2_terminal_sha256 = None
            t2_ticks = None
            sim_sha256 = None

        final_state_sha256 = self.state_digests[-1] if self.state_digests else None
        if frames_captured == 0 and self.pixel_required:
            verdict = "INDETERMINATE"
            code = "INDETERMINATE_ZERO_FRAMES"
        else:
            verdict = "ACCEPT"
            code = "OK"

        receipt = {
            "schema": RECEIPT_SCHEMA_ID,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "target_handle": copy.deepcopy(self.target_handle),
            "epoch": self.epoch,
            "frames_root": FRAMES_ROOT,
            "frame_run_root": frame_run_root_for(self.run_id),
            "source": FRAME_SOURCE,
            "frames_captured": frames_captured,
            "first_frame": first_frame,
            "last_frame": last_frame,
            "ui_actions_count": ui_actions_count,
            "admitted_actions": admitted_count,
            "refused_actions": refused_count,
            "uncertain_actions": uncertain_count,
            "action_log_sha256": action_log_sha256,
            "t2_trace_sha256": t2_trace_sha256,
            "t2_terminal_sha256": t2_terminal_sha256,
            "t2_ticks": t2_ticks,
            "final_state_sha256": final_state_sha256,
            "simulation_state_sha256": sim_sha256,
            "ui_gate_verdict": verdict,
            "ui_gate_code": code,
            "pair_mismatches": self.credit_controller.pair_mismatches,
            "frame_gaps": self.credit_controller.frame_gaps,
            "frames_dropped": self.credit_controller.frames_dropped,
            "x11_tcp_listeners": 0,
            "x11_unix_listeners": 0,
            "x11_abstract_listeners": 0,
            "reader_exit_code": 0,
            "reader_quiescent": True,
            "ram_purged": self.ram_purged,
            "frames_disk_bytes": 0,
            "swap_delta_bytes": 0,
            "metadata_only": True,
        }
        return validate_receipt(receipt, t2_enabled=self.t2_enabled, pixel_required=self.pixel_required)


# ============================================================================
# Independent W8U Trace, Chain, STATE, T2, and Receipt Verifier
# ============================================================================

def verify_w8u_trace(
    *,
    run_descriptor: dict[str, Any],
    receipt: dict[str, Any],
    transitions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    action_responses: list[dict[str, Any]],
    ui_states: Mapping[str, dict[str, Any]] | list[dict[str, Any]],
    screen_width: int,
    screen_height: int,
    t2_jsonl_stream: bytes | str | list[bytes | str | dict[str, Any]] | None = None,
    producer_schemas: Mapping[str, Mapping[str, Any]] | None = None,
    initial_simulation_sha256: str | None = None,
    expected_row_count: int | None = None,
    claim_economic_success: bool = False,
    pixel_required: bool = True,
) -> dict[str, Any]:
    """Independently recompute and verify the authenticated transition chain, canonical STATEs,
    T2 LF-inclusive stream, join invariants, and receipt training evidence.
    """
    assert_metadata_only(run_descriptor, "$.run_descriptor")
    assert_metadata_only(receipt, "$.receipt")
    assert_metadata_only(transitions, "$.transitions")
    assert_metadata_only(actions, "$.actions")
    assert_metadata_only(action_responses, "$.action_responses")

    t2_enabled = bool(run_descriptor.get("t2_enabled", True))
    val_receipt = validate_receipt(receipt, t2_enabled=t2_enabled, pixel_required=pixel_required)

    state_map: dict[str, dict[str, Any]] = {}
    newline_confused_digests: set[str] = set()
    state_items = ui_states.values() if isinstance(ui_states, Mapping) else ui_states
    for st in state_items:
        validated_st = validate_state_strict(st)
        canon_no_lf = schema.dumps(validated_st).encode("utf-8")
        digest_no_lf = hashlib.sha256(canon_no_lf).hexdigest()
        digest_with_lf = hashlib.sha256(canon_no_lf + b"\n").hexdigest()
        state_map[digest_no_lf] = validated_st
        newline_confused_digests.add(digest_with_lf)
    if isinstance(ui_states, Mapping):
        for declared_sha, st in ui_states.items():
            actual_sha = compute_state_sha256(st)
            if declared_sha != actual_sha:
                raise W8UTraceError(E_STATE, f"declared UI state digest {declared_sha} != canonical {actual_sha}")

    if expected_row_count is not None and len(transitions) != expected_row_count:
        raise W8UTraceError(
            E_JOIN,
            f"missing or extra transition rows: expected {expected_row_count}, got {len(transitions)}",
        )
    if len(actions) != len(transitions) or len(action_responses) != len(transitions):
        raise W8UTraceError(E_JOIN, "actions, action_responses, and transitions counts differ")

    validated_actions: list[dict[str, Any]] = []
    action_shas: list[str] = []
    for act in actions:
        v_act, a_sha = validate_action_request(act, screen_width=screen_width, screen_height=screen_height)
        validated_actions.append(v_act)
        action_shas.append(a_sha)
    recomputed_action_log_sha = compute_action_log_sha256(validated_actions)
    if val_receipt["action_log_sha256"] != recomputed_action_log_sha:
        raise W8UTraceError(E_CHAIN, "receipt.action_log_sha256 mismatch")

    parsed_t2_commands: list[dict[str, Any]] = []
    parsed_t2_outcomes: list[dict[str, Any]] = []
    recomputed_t2_trace_sha: str | None = None
    recomputed_t2_terminal_sha: str | None = None

    if t2_enabled:
        if t2_jsonl_stream is None:
            raise W8UTraceError(E_T2, "T2 policy requires t2_jsonl_stream")
        if not producer_schemas:
            raise W8UTraceError(E_T2, "T2 policy requires closed producer_schemas")
        if initial_simulation_sha256 is None:
            raise W8UTraceError(E_T2, "T2 policy requires initial_simulation_sha256")
        recomputed_t2_trace_sha = compute_t2_trace_sha256(t2_jsonl_stream)
        if val_receipt["t2_trace_sha256"] != recomputed_t2_trace_sha:
            raise W8UTraceError(E_T2, "receipt.t2_trace_sha256 mismatch (or newline domain confusion)")

        if isinstance(t2_jsonl_stream, (bytes, bytearray, memoryview)):
            raw_text = bytes(t2_jsonl_stream).decode("utf-8")
            raw_lines = [ln for ln in raw_text.split("\n") if ln]
        elif isinstance(t2_jsonl_stream, str):
            raw_lines = [ln for ln in t2_jsonl_stream.split("\n") if ln]
        else:
            raw_lines = []
            for item in t2_jsonl_stream:
                if isinstance(item, dict):
                    raw_lines.append(schema.dumps(item))
                elif isinstance(item, bytes):
                    raw_lines.append(item.decode("utf-8").rstrip("\n"))
                elif isinstance(item, str):
                    raw_lines.append(item.rstrip("\n"))

        for line in raw_lines:
            rec = parse_strict_json_object(line, code=E_T2)
            if "cmd" in rec:
                parsed_t2_commands.append(validate_t2_command(rec, producer_schemas))
            else:
                parsed_t2_outcomes.append(validate_t2_outcome(rec))

        if len(parsed_t2_commands) != len(transitions) or len(parsed_t2_outcomes) != len(transitions):
            raise W8UTraceError(E_JOIN, "T2 commands/outcomes count does not match transitions")

        recomputed_t2_terminal_sha = compute_t2_terminal_sha256(parsed_t2_outcomes)
        if val_receipt["t2_terminal_sha256"] != recomputed_t2_terminal_sha:
            raise W8UTraceError(E_T2, "receipt.t2_terminal_sha256 mismatch")
    else:
        if t2_jsonl_stream is not None:
            raise W8UTraceError(E_T2, "non-T2 policy must retain t2_jsonl_stream=None")

    genesis_sha = compute_genesis_sha256(run_descriptor)
    prev_chain_sha = genesis_sha
    sim_basis = initial_simulation_sha256 if t2_enabled else None
    committed_tick = 0 if t2_enabled else None
    accepted_t2_count = 0
    rejected_t2_count = 0
    prev_ui_post_sha: str | None = None
    seen_row_seqs: set[int] = set()

    for idx, raw_tr in enumerate(transitions):
        expected_seq = idx + 1
        if not isinstance(raw_tr, dict):
            raise W8UTraceError(E_JOIN, f"transition[{idx}] must be a dict")
        rseq = raw_tr.get("row_seq")
        if rseq in seen_row_seqs or rseq != expected_seq:
            raise W8UTraceError(
                E_JOIN,
                f"missing, reordered, or conflicting transition row_seq at index {idx}: got {rseq!r}, expected {expected_seq}",
            )
        seen_row_seqs.add(rseq)

        if raw_tr.get("previous_chain_sha256") != prev_chain_sha:
            raise W8UTraceError(
                E_CHAIN,
                f"row_seq={expected_seq} previous_chain_sha256 mismatch",
            )
        tr = validate_transition_record(raw_tr, t2_enabled=t2_enabled)

        if (
            tr["run_id"] != run_descriptor["run_id"]
            or tr["lease_id"] != run_descriptor["lease_id"]
            or tr["target_handle"] != normalize_target_handle(run_descriptor["target_handle"])
            or tr["epoch"] != run_descriptor["epoch"]
        ):
            raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} authenticated session/handle mismatch")

        act = validated_actions[idx]
        resp = validate_action_response(action_responses[idx])
        if tr["action_id"] != act["action_id"] or tr["action_sha256"] != action_shas[idx] or act["action_seq"] != expected_seq:
            raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} action join mismatch")
        if resp["action_id"] != act["action_id"] or resp["action_seq"] != expected_seq:
            raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} action_response join mismatch")

        for ui_field in ("pre_ui_state_sha256", "post_ui_state_sha256"):
            ui_sha = tr[ui_field]
            if ui_sha in newline_confused_digests:
                raise W8UTraceError(E_STATE, f"newline domain confusion detected in {ui_field} at row_seq={expected_seq}")
            if ui_sha not in state_map:
                raise W8UTraceError(E_STATE, f"unverified or non-UI digest in {ui_field} at row_seq={expected_seq}")

        if prev_ui_post_sha is not None and tr["pre_ui_state_sha256"] != prev_ui_post_sha:
            raise W8UTraceError(E_JOIN, f"UI state continuity broken at row_seq={expected_seq}")
        prev_ui_post_sha = tr["post_ui_state_sha256"]

        if t2_enabled:
            cmd = parsed_t2_commands[idx]
            out = parsed_t2_outcomes[idx]
            if tr["t2_outcome"] != out:
                raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} transition t2_outcome != T2 stream outcome")
            if cmd["seq"] != expected_seq or out["seq"] != expected_seq:
                raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} T2 seq mismatch")
            if cmd["tick"] != out["requested_tick"]:
                raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} T2 command tick != requested_tick")
            if tr["simulation_pre_sha256"] != sim_basis:
                raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} stale simulation basis")

            assert producer_schemas is not None and committed_tick is not None
            is_econ = producer_schemas[cmd["cmd"]].get("type", "economic") == "economic"
            if out["status"] == "accepted":
                accepted_t2_count += 1
                if resp["status"] != "applied":
                    raise W8UTraceError(E_T2, f"row_seq={expected_seq} admitted-only response cannot be accepted T2 effect")
                if is_econ and out["requested_tick"] != committed_tick + 1:
                    raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} wrong economic requested_tick")
                if tr["simulation_post_sha256"] != out["state_sha256"]:
                    raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} simulation_post_sha256 != outcome.state_sha256")
                committed_tick = out["applied_tick"]
                sim_basis = tr["simulation_post_sha256"]
            elif out["status"] == "rejected":
                rejected_t2_count += 1
                if tr["simulation_post_sha256"] != sim_basis or out["state_sha256"] != sim_basis:
                    raise W8UTraceError(E_T2, f"row_seq={expected_seq} rejected outcome mutated simulation state")

            if tr["pre_frame"] is not None and tr["pre_frame"]["tick_valid"]:
                expected_pre_tick = out["requested_tick"] - 1 if (out["status"] == "accepted" and is_econ) else committed_tick
                if tr["pre_frame"]["tick"] != expected_pre_tick:
                    raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} wrong pre_frame.tick")
            if tr["post_frame"] is not None and tr["post_frame"]["tick_valid"]:
                if tr["post_frame"]["tick"] != out["applied_tick"]:
                    raise W8UTraceError(E_JOIN, f"row_seq={expected_seq} wrong post_frame.tick")

        prev_chain_sha = tr["chain_sha256"]

    if prev_ui_post_sha is not None and val_receipt["final_state_sha256"] != prev_ui_post_sha:
        raise W8UTraceError(E_STATE, "receipt.final_state_sha256 does not match final transition post_ui_state_sha256")
    if t2_enabled:
        if val_receipt["simulation_state_sha256"] != sim_basis:
            raise W8UTraceError(E_T2, "receipt.simulation_state_sha256 does not match terminal simulation basis")
        if val_receipt["t2_ticks"] != committed_tick:
            raise W8UTraceError(E_T2, "receipt.t2_ticks does not match terminal committed_tick")

    economic_success = bool(
        t2_enabled
        and accepted_t2_count > 0
        and all(
            r["status"] == "applied"
            for r, o in zip(action_responses, parsed_t2_outcomes)
            if o["status"] == "accepted"
        )
    )
    if claim_economic_success and not economic_success:
        raise W8UTraceError(
            E_T2,
            "admitted-only or rejected T2 execution cannot claim economic/training success",
        )

    result = {
        "schema": VERIFY_RESULT_SCHEMA_ID,
        "ok": True,
        "verdict": val_receipt["ui_gate_verdict"],
        "code": val_receipt["ui_gate_code"],
        "genesis_sha256": genesis_sha,
        "terminal_chain_sha256": prev_chain_sha,
        "action_log_sha256": recomputed_action_log_sha,
        "t2_trace_sha256": recomputed_t2_trace_sha,
        "t2_terminal_sha256": recomputed_t2_terminal_sha,
        "final_state_sha256": val_receipt["final_state_sha256"],
        "simulation_state_sha256": val_receipt["simulation_state_sha256"],
        "rows_verified": len(transitions),
        "accepted_t2_count": accepted_t2_count,
        "rejected_t2_count": rejected_t2_count,
        "economic_success": economic_success,
        "timing_domain": TIMING_DOMAIN,
        "host_measurements": AUSENTE,
    }
    assert_metadata_only(result, "$.verify_result")
    return result
