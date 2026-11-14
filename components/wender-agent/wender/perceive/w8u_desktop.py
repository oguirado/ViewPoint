# @module {"id":"wa.perceive.w8u_desktop","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.session_registry","wa.perceive.atspi_tree","wa.perceive.atspi","wa.perceive.ax","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Target-bound W8U desktop observation adapter, frame-pair verifier, credit controller,
action gate (`w8u.action/v1`), and receipt builder (`w8u.receipt/v1`).

Enforces admitted private X identity, current generation/epoch, revision-pinned
`w8u-os-contract` held-pair XWD evidence, complete AT-SPI conversion vs. explicit
vision-only policy (`E_MODALITY`), credit flow control (`E_CREDIT`), strict action
validation, and zero-disk-write storage law. Offline stdlib + NumPy only.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
import copy
from dataclasses import dataclass
import hashlib
import io
import json
import os
import pathlib
import re
import socket
import struct
import subprocess
import tempfile
from typing import Any, Callable
from unittest import mock

import numpy as np

from wender.perceive import atspi, atspi_tree, ax
from wender.perceive.session_registry import (
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    PerceptionResult,
    QuarantinedTargetError,
    RegistryError,
    SessionRegistry,
    StaleHandleError,
    TargetRecord,
)
from wender.state import schema


# ---------------------------------------------------------------------------
# Frozen W8U Contract Constants & Schemas (omitted values are AUSENTE)
# ---------------------------------------------------------------------------

AUSENTE = "AUSENTE"

W8U_OS_CONTRACT_REVISION = "w8u-os-contract/v1"
W8U_BINDING_SCHEMA = "w8u.binding/v1"
W8U_RESULT_SCHEMA = "w8u.result/v1"
W8U_ACTION_SCHEMA = "w8u.action/v1"
W8U_ACTION_RESPONSE_SCHEMA = "w8u.action.response/v1"
W8U_RECEIPT_SCHEMA = "w8u.receipt/v1"
W8U_STATE_SCHEMA = schema.SCHEMA_ID

FRAMES_ROOT = "/dev/shm/vqa-ramws"
FRAME_RUN_SUBPATH = "opensuperficies/os-cu-0"
RECEIPT_SOURCE = "xvfb-grab-held-pair"
PIXEL_FORMAT_RGB24 = "rgb24"

DESKTOP_TARGET_KINDS = frozenset({"x11_window", "xvfb_screen", "capsule"})
MODALITY_POLICIES = frozenset({"atspi", "vision_only"})

# Control wire protocol magic, kinds, and body schemas
CONTROL_MAGIC = b"W8UCTRL1"
FRAME_PAIR_MAGIC = b"W8UPAIR1"

CONTROL_KIND_HELLO = 1
CONTROL_KIND_ACK = 2
CONTROL_KIND_FRAME_PAIR = 3
CONTROL_KIND_STOP = 4
CONTROL_KIND_END = 5
CONTROL_KIND_EOF = 6

CONTROL_KINDS = {
    "HELLO": CONTROL_KIND_HELLO,
    "ACK": CONTROL_KIND_ACK,
    "FRAME_PAIR": CONTROL_KIND_FRAME_PAIR,
    "STOP": CONTROL_KIND_STOP,
    "END": CONTROL_KIND_END,
    "EOF": CONTROL_KIND_EOF,
}

CONTROL_BODY_SCHEMAS = {
    "HELLO": ("contract_revision", "run_id", "lease_id", "canonical_id", "generation", "epoch", "reserved_capacity"),
    "ACK": ("seq", "grant"),
    "FRAME_PAIR": ("seq", "tick", "tick_valid", "capture_mono_ns", "grab_a_len", "grab_b_len", "binding_sha256"),
    "STOP": ("reason",),
    "END": ("error_code", "frames_captured", "reason"),
    "EOF": (),
}

BINDING_SCHEMA_KEYS = (
    "schema",
    "contract_revision",
    "run_id",
    "lease_id",
    "handle",
    "epoch",
    "display",
    "window_id",
    "seq",
    "width",
    "height",
    "stride",
    "pixel_format",
    "frame_sha256",
)

RESULT_SCHEMA_KEYS = (
    "schema",
    "run_id",
    "lease_id",
    "handle",
    "epoch",
    "frame_seq",
    "dpr_ppm",
    "modality_policy",
    "semantic_modality",
    "state_sha256",
    "frame_sha256",
    "binding_sha256",
)

# Complete END numeric errors
END_OK = 0
END_E_MODALITY = 1001
END_E_CREDIT = 1002
END_E_PAIR_MISMATCH = 1003
END_E_FOREIGN_TARGET = 1004
END_E_STALE_GENERATION = 1005
END_E_EPOCH = 1006
END_E_TICK_INVENTED = 1007
END_E_DPR = 1008
END_E_COORDINATES = 1009
END_E_STORAGE_LAW = 1010
END_E_PROTOCOL = 1011
END_E_REOPEN = 1012

END_NUMERIC_ERRORS = {
    END_OK: "OK",
    END_E_MODALITY: "E_MODALITY",
    END_E_CREDIT: "E_CREDIT",
    END_E_PAIR_MISMATCH: "E_PAIR_MISMATCH",
    END_E_FOREIGN_TARGET: "E_FOREIGN_TARGET",
    END_E_STALE_GENERATION: "E_STALE_GENERATION",
    END_E_EPOCH: "E_EPOCH",
    END_E_TICK_INVENTED: "E_TICK_INVENTED",
    END_E_DPR: "E_DPR",
    END_E_COORDINATES: "E_COORDINATES",
    END_E_STORAGE_LAW: "E_STORAGE_LAW",
    END_E_PROTOCOL: "E_PROTOCOL",
    END_E_REOPEN: "E_REOPEN",
}

# Frozen digest domains
DIGEST_DOMAINS = {
    "frame_rgb24": b"",
    "frame_binding": b"w8u.binding/v1\x00",
    "state": b"",
    "action_grant": b"w8u.action.grant/v1\x00",
    "action_outcome": b"w8u.action.outcome/v1\x00",
    "action_log": b"w8u.action.log/v1\x00",
    "t2_trace": b"w8u.t2.trace/v1\x00",
}

# Canonical Error & Return Codes
OK_ADMITTED = "OK_ADMITTED"
E_MODALITY = "E_MODALITY"
E_CREDIT = "E_CREDIT"
E_SCHEMA = "E_SCHEMA"
E_VERB = "E_VERB"
E_LOCK = "E_LOCK"
E_EPOCH = "E_EPOCH"
E_STALE_FRAME = "E_STALE_FRAME"
E_FOCUS = "E_FOCUS"
E_BUDGET = "E_BUDGET"
E_DEADLINE = "E_DEADLINE"
E_UNCERTAIN = "E_UNCERTAIN"
E_FOREIGN_TARGET = "E_FOREIGN_TARGET"
E_STALE_GENERATION = "E_STALE_GENERATION"
E_PAIR_MISMATCH = "E_PAIR_MISMATCH"
E_TICK_INVENTED = "E_TICK_INVENTED"
E_DPR = "E_DPR"
E_COORDINATES = "E_COORDINATES"
E_STORAGE_LAW = "E_STORAGE_LAW"
E_PROTOCOL = "E_PROTOCOL"
E_REOPEN = "E_REOPEN"

RC_OK = 0
RC_SCHEMA = 2
RC_UNAVAILABLE = 4
RC_REFUSED = 5
RC_UNCERTAIN = 6

ACTION_MAX_UTF8_BYTES = 65536
TEXT_MAX_UTF8_BYTES = 4096
WAIT_MAX_DURATION_MS = 5000
SCROLL_MIN_DELTA = -120
SCROLL_MAX_DELTA = 120
PREFERRED_INITIAL_CREDITS = 2

ACTION_REQUEST_KEYS = (
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

ACTION_STATUSES = frozenset({"refused", "admitted", "applied", "uncertain"})
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

VALID_VERBS = frozenset({"pointer", "key", "text", "wait"})
POINTER_OPERATIONS = frozenset({"move", "click", "down", "up", "scroll"})
POINTER_BUTTONS = (1, 2, 3)
KEY_OPERATIONS = frozenset({"press", "down", "up"})
FINITE_KEYS = frozenset({
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
})
ORDERED_MODIFIERS = ("Shift", "Control", "Alt", "Super")

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

RECEIPT_TARGET_HANDLE_KEYS = ("kind", "canonical_id", "generation")

RECEIPT_REQUIRED_KEYS = (
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

# XWD Common Subset Constants (Big-Endian X11 XWDFileHeader)
XWD_HEADER_STRUCT = struct.Struct(">25I")
XWD_MIN_HEADER_SIZE = 100
XWD_FILE_VERSION = 7
XWD_ZPIXMAP = 2
XWD_DEPTH_24 = 24
XWD_VISUAL_TRUE_COLOR = 4
XWD_VISUAL_DIRECT_COLOR = 5
XWD_RED_MASK = 0x00FF0000
XWD_GREEN_MASK = 0x0000FF00
XWD_BLUE_MASK = 0x000000FF
XWD_BITS_PER_RGB = 8

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PRIVATE_DISPLAY_RE = re.compile(r"^:[1-9][0-9]*(?:\.0)?\Z")
_WINDOW_ID_RE = re.compile(r"^(?:0x[0-9a-fA-F]+|[1-9][0-9]*|xvfb-screen-[A-Za-z0-9._-]+|capsule-[A-Za-z0-9._-]+)\Z")

FORBIDDEN_FALLBACK_KEYS = frozenset({
    "active_window_discovery",
    "display_discovery_fallback",
    "host_clipboard",
    "global_focus",
    "xdotool",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class W8UDesktopError(RegistryError):
    """Fail-closed W8U desktop error carrying a canonical `code` and `rc`."""

    def __init__(self, code: str, message: str, *, rc: int = RC_REFUSED) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.rc = rc


class ModalityError(W8UDesktopError):
    def __init__(self, message: str) -> None:
        super().__init__(E_MODALITY, message, rc=RC_REFUSED)


class CreditError(W8UDesktopError):
    def __init__(self, message: str) -> None:
        super().__init__(E_CREDIT, message, rc=RC_REFUSED)


class ForeignTargetError(W8UDesktopError):
    def __init__(self, message: str, *, code: str = E_FOREIGN_TARGET) -> None:
        super().__init__(code, message, rc=RC_REFUSED)


class PairMismatchError(W8UDesktopError):
    def __init__(self, message: str) -> None:
        super().__init__(E_PAIR_MISMATCH, message, rc=RC_REFUSED)


class TickInventedError(W8UDesktopError):
    def __init__(self, message: str) -> None:
        super().__init__(E_TICK_INVENTED, message, rc=RC_REFUSED)


class StorageLawError(W8UDesktopError):
    def __init__(self, message: str) -> None:
        super().__init__(E_STORAGE_LAW, message, rc=RC_REFUSED)


class ProtocolError(W8UDesktopError):
    def __init__(self, message: str, *, code: str = E_PROTOCOL) -> None:
        super().__init__(code, message, rc=RC_REFUSED)


# ---------------------------------------------------------------------------
# Strict Type & JSON Helpers
# ---------------------------------------------------------------------------

def _is_exact_int(val: Any) -> bool:
    return type(val) is int


def _is_exact_bool(val: Any) -> bool:
    return type(val) is bool


def _is_nonempty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val) and val == val.strip() and "\x00" not in val


def _is_hex64(val: Any) -> bool:
    return isinstance(val, str) and bool(_HEX64_RE.fullmatch(val))


def _ensure_valid_utf8(s: str, name: str = "value") -> bytes:
    if not isinstance(s, str):
        raise W8UDesktopError(E_SCHEMA, f"{name} must be str", rc=RC_SCHEMA)
    try:
        return s.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise W8UDesktopError(E_SCHEMA, f"{name} contains invalid UTF-8: {exc}", rc=RC_SCHEMA) from exc


def _check_no_float_or_perception_wrapper(obj: Any, path: str = "$") -> None:
    if isinstance(obj, float):
        raise W8UDesktopError(E_SCHEMA, f"float or NaN forbidden at {path}", rc=RC_SCHEMA)
    if isinstance(obj, str):
        _ensure_valid_utf8(obj, path)
        return
    if obj is None or _is_exact_bool(obj) or _is_exact_int(obj):
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, float) or not isinstance(k, str):
                raise W8UDesktopError(E_SCHEMA, f"invalid key at {path}: {k!r}", rc=RC_SCHEMA)
            if k == "_perception":
                raise W8UDesktopError(E_SCHEMA, f"_perception wrapper forbidden at {path}.{k}", rc=RC_SCHEMA)
            _ensure_valid_utf8(k, f"{path}.<key>")
            _check_no_float_or_perception_wrapper(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _check_no_float_or_perception_wrapper(item, f"{path}[{idx}]")
        return
    raise W8UDesktopError(E_SCHEMA, f"unsupported type at {path}: {type(obj).__name__}", rc=RC_SCHEMA)


def parse_strict_json_object(raw: bytes | str, *, max_bytes: int = ACTION_MAX_UTF8_BYTES) -> dict[str, Any]:
    """Parse canonical UTF-8 JSON object rejecting duplicate keys, floats, invalid UTF-8, and trailing data."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
        if len(raw_bytes) > max_bytes:
            raise W8UDesktopError(E_SCHEMA, f"UTF-8 payload length {len(raw_bytes)} exceeds cap {max_bytes}", rc=RC_SCHEMA)
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise W8UDesktopError(E_SCHEMA, f"invalid UTF-8 payload: {exc}", rc=RC_SCHEMA) from exc
    elif isinstance(raw, str):
        raw_bytes = _ensure_valid_utf8(raw, "payload")
        if len(raw_bytes) > max_bytes:
            raise W8UDesktopError(E_SCHEMA, f"UTF-8 payload length {len(raw_bytes)} exceeds cap {max_bytes}", rc=RC_SCHEMA)
        text = raw
    else:
        raise W8UDesktopError(E_SCHEMA, "payload must be UTF-8 bytes or str", rc=RC_SCHEMA)

    if not text or not text.strip():
        raise W8UDesktopError(E_SCHEMA, "empty JSON payload", rc=RC_SCHEMA)

    def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if not isinstance(k, str):
                raise W8UDesktopError(E_SCHEMA, "JSON key must be string", rc=RC_SCHEMA)
            _ensure_valid_utf8(k, "key")
            if k in out:
                raise W8UDesktopError(E_SCHEMA, f"duplicate JSON key: {k!r}", rc=RC_SCHEMA)
            out[k] = v
        return out

    def _reject_float(token: str) -> None:
        raise W8UDesktopError(E_SCHEMA, f"float literal forbidden: {token!r}", rc=RC_SCHEMA)

    def _reject_const(token: str) -> None:
        raise W8UDesktopError(E_SCHEMA, f"constant literal forbidden: {token!r}", rc=RC_SCHEMA)

    decoder = json.JSONDecoder(
        object_pairs_hook=_pairs_hook,
        parse_float=_reject_float,
        parse_constant=_reject_const,
    )
    stripped = text.lstrip(" \t")
    if "\n" in stripped or "\r" in stripped:
        raise W8UDesktopError(E_SCHEMA, "multiline or trailing newline data forbidden", rc=RC_SCHEMA)
    try:
        obj, end_idx = decoder.raw_decode(stripped)
    except W8UDesktopError:
        raise
    except Exception as exc:
        raise W8UDesktopError(E_SCHEMA, f"malformed JSON object: {exc}", rc=RC_SCHEMA) from exc

    if end_idx != len(stripped):
        raise W8UDesktopError(E_SCHEMA, "trailing data after JSON object", rc=RC_SCHEMA)
    if not isinstance(obj, dict):
        raise W8UDesktopError(E_SCHEMA, "root JSON value must be an object", rc=RC_SCHEMA)
    _check_no_float_or_perception_wrapper(obj)
    return obj


def canonical_digest(domain: str, payload: bytes | str | dict[str, Any]) -> str:
    """Compute domain-separated SHA-256 hex digest without LF."""
    if domain not in DIGEST_DOMAINS:
        raise ProtocolError(f"unknown digest domain: {domain!r}")
    prefix = DIGEST_DOMAINS[domain]
    if isinstance(payload, dict):
        dumped = schema.dumps(payload)
        if "\n" in dumped or "\r" in dumped:
            raise W8UDesktopError(E_SCHEMA, "canonical JSON dump must not contain CR/LF", rc=RC_SCHEMA)
        body = dumped.encode("utf-8", errors="strict")
    elif isinstance(payload, str):
        if "\n" in payload or "\r" in payload:
            raise W8UDesktopError(E_SCHEMA, "canonical text payload must not contain CR/LF", rc=RC_SCHEMA)
        body = payload.encode("utf-8", errors="strict")
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        body = bytes(payload)
    else:
        raise TypeError(f"unsupported digest payload type: {type(payload).__name__}")
    return hashlib.sha256(prefix + body).hexdigest()


def state_digest(state: dict[str, Any]) -> str:
    """Validate `wender.state/v1` and compute SHA-256 of `schema.dumps(state)` without LF."""
    _check_no_float_or_perception_wrapper(state)
    errs = schema.validate_state(state)
    if errs:
        raise W8UDesktopError(E_SCHEMA, "; ".join(errs), rc=RC_SCHEMA)
    dumped = schema.dumps(state)
    if "\n" in dumped or "\r" in dumped:
        raise W8UDesktopError(E_SCHEMA, "schema.dumps output contained newline", rc=RC_SCHEMA)
    return hashlib.sha256(dumped.encode("utf-8", errors="strict")).hexdigest()


# ---------------------------------------------------------------------------
# Storage Law Disk Sink Spy
# ---------------------------------------------------------------------------

class DiskSinkSpy:
    """Records and blocks any disk write, temp file creation, socket, or process launch."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.blocked_writes: int = 0
        self.frames_disk_bytes: int = 0
        self._registered_frame_tokens: set[bytes] = set()

    def register_frame_bytes(self, raw_rgb: bytes, xwd_bytes: bytes = b"") -> None:
        if raw_rgb:
            self._registered_frame_tokens.add(raw_rgb[:min(32, len(raw_rgb))])
        if xwd_bytes:
            self._registered_frame_tokens.add(xwd_bytes[:min(32, len(xwd_bytes))])

    def record_forbidden(self, op: str, detail: str = "") -> None:
        self.events.append((op, detail))
        self.blocked_writes += 1
        raise StorageLawError(f"forbidden external or disk sink operation: {op} ({detail})")

    @contextmanager
    def guard(self):
        def _guarded_open(file, mode="r", *args, **kwargs):
            self.record_forbidden("builtins.open", f"file={file!r} mode={mode!r}")

        def _guarded_os_open(path, flags, *args, **kwargs):
            self.record_forbidden("os.open", f"path={path!r} flags={flags!r}")

        def _guarded_write_bytes(path_self, data, *args, **kwargs):
            self.record_forbidden("Path.write_bytes", f"path={path_self!r} len={len(data)}")

        def _guarded_write_text(path_self, data, *args, **kwargs):
            self.record_forbidden("Path.write_text", f"path={path_self!r}")

        def _guarded_named_temp(*args, **kwargs):
            self.record_forbidden("tempfile.NamedTemporaryFile", "")

        def _guarded_mkstemp(*args, **kwargs):
            self.record_forbidden("tempfile.mkstemp", "")

        def _guarded_socket(*args, **kwargs):
            self.record_forbidden("socket.socket", "")

        def _guarded_popen(*args, **kwargs):
            self.record_forbidden("subprocess.Popen", "")

        def _guarded_run(*args, **kwargs):
            self.record_forbidden("subprocess.run", "")

        with (
            mock.patch("builtins.open", side_effect=_guarded_open),
            mock.patch("io.open", side_effect=_guarded_open),
            mock.patch("os.open", side_effect=_guarded_os_open),
            mock.patch.object(pathlib.Path, "open", _guarded_open),
            mock.patch.object(pathlib.Path, "write_bytes", _guarded_write_bytes),
            mock.patch.object(pathlib.Path, "write_text", _guarded_write_text),
            mock.patch("tempfile.NamedTemporaryFile", side_effect=_guarded_named_temp),
            mock.patch("tempfile.mkstemp", side_effect=_guarded_mkstemp),
            mock.patch("socket.socket", side_effect=_guarded_socket),
            mock.patch("socket.create_connection", side_effect=_guarded_socket),
            mock.patch("subprocess.Popen", side_effect=_guarded_popen),
            mock.patch("subprocess.run", side_effect=_guarded_run),
        ):
            yield self


# ---------------------------------------------------------------------------
# XWD Common Subset & Held-Pair Equality Verifier
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class XwdFrameInfo:
    width: int
    height: int
    stride: int
    bits_per_pixel: int
    byte_order: int
    visual_class: int
    window_x: int
    window_y: int
    rgb_bytes: bytes
    rgb_sha256: str


def build_xwd_bytes(
    rgb: np.ndarray | bytes,
    width: int | None = None,
    height: int | None = None,
    *,
    bits_per_pixel: int = 24,
    byte_order: int = 1,
    visual_class: int = XWD_VISUAL_TRUE_COLOR,
    window_name: bytes = b"w8u-xvfb\x00",
    pad_bytes_per_line: int = 0,
) -> bytes:
    """Build a canonical in-memory XWD v7 ZPixmap buffer from RGB24 input."""
    if isinstance(rgb, np.ndarray):
        if rgb.dtype != np.dtype("uint8") or rgb.ndim != 3 or rgb.shape[2] != 3:
            raise FrameValidationError("RGB must be H x W x 3 uint8")
        h, w, _ = rgb.shape
        raw_rgb = rgb.tobytes(order="C")
    elif isinstance(rgb, (bytes, bytearray, memoryview)):
        if not _is_exact_int(width) or not _is_exact_int(height) or width <= 0 or height <= 0:
            raise FrameValidationError("width and height must be positive integers for bytes input")
        w, h = width, height
        raw_rgb = bytes(rgb)
        if len(raw_rgb) != w * h * 3:
            raise FrameValidationError("raw RGB byte length does not match width * height * 3")
    else:
        raise FrameValidationError("rgb must be ndarray or bytes")

    if not window_name.endswith(b"\x00"):
        window_name = window_name + b"\x00"
    header_size = XWD_MIN_HEADER_SIZE + len(window_name)
    if bits_per_pixel not in (24, 32):
        raise ValueError("bits_per_pixel must be 24 or 32")
    bytes_per_pixel = bits_per_pixel // 8
    bytes_per_line = w * bytes_per_pixel + pad_bytes_per_line

    header = XWD_HEADER_STRUCT.pack(
        header_size,
        XWD_FILE_VERSION,
        XWD_ZPIXMAP,
        XWD_DEPTH_24,
        w,
        h,
        0,  # xoffset
        byte_order,
        32,  # bitmap_unit
        byte_order,  # bitmap_bit_order
        32,  # bitmap_pad
        bits_per_pixel,
        bytes_per_line,
        visual_class,
        XWD_RED_MASK,
        XWD_GREEN_MASK,
        XWD_BLUE_MASK,
        XWD_BITS_PER_RGB,
        256,  # colormap_entries
        0,  # ncolors
        w,  # window_width
        h,  # window_height
        0,  # window_x
        0,  # window_y
        0,  # window_bdrwidth
    )

    rows = []
    src_stride = w * 3
    for y in range(h):
        row_rgb = raw_rgb[y * src_stride:(y + 1) * src_stride]
        if bits_per_pixel == 24:
            if byte_order == 1:
                row_out = row_rgb
            else:
                buf = bytearray(len(row_rgb))
                buf[0::3] = row_rgb[2::3]
                buf[1::3] = row_rgb[1::3]
                buf[2::3] = row_rgb[0::3]
                row_out = bytes(buf)
        else:
            buf = bytearray(w * 4)
            if byte_order == 1:
                buf[0::4] = b"\x00" * w
                buf[1::4] = row_rgb[0::3]
                buf[2::4] = row_rgb[1::3]
                buf[3::4] = row_rgb[2::3]
            else:
                buf[0::4] = row_rgb[2::3]
                buf[1::4] = row_rgb[1::3]
                buf[2::4] = row_rgb[0::3]
                buf[3::4] = b"\x00" * w
            row_out = bytes(buf)
        if pad_bytes_per_line > 0:
            row_out = row_out + (b"\x00" * pad_bytes_per_line)
        rows.append(row_out)

    return header + window_name + b"".join(rows)


def parse_xwd_common_subset(raw: bytes) -> XwdFrameInfo:
    """Validate common XWD subset and extract canonical RGB24 bytes."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise PairMismatchError("XWD payload must be bytes")
    buf = bytes(raw)
    if len(buf) < XWD_MIN_HEADER_SIZE:
        raise PairMismatchError("XWD buffer shorter than 100-byte header")

    (
        header_size,
        file_version,
        pixmap_format,
        pixmap_depth,
        pixmap_width,
        pixmap_height,
        xoffset,
        byte_order,
        bitmap_unit,
        bitmap_bit_order,
        bitmap_pad,
        bits_per_pixel,
        bytes_per_line,
        visual_class,
        red_mask,
        green_mask,
        blue_mask,
        bits_per_rgb,
        colormap_entries,
        ncolors,
        window_width,
        window_height,
        window_x,
        window_y,
        window_bdrwidth,
    ) = XWD_HEADER_STRUCT.unpack_from(buf, 0)

    if header_size < XWD_MIN_HEADER_SIZE:
        raise PairMismatchError(f"invalid XWD header_size {header_size}")
    if file_version != XWD_FILE_VERSION:
        raise PairMismatchError(f"unsupported XWD file_version {file_version}")
    if pixmap_format != XWD_ZPIXMAP:
        raise PairMismatchError(f"unsupported XWD pixmap_format {pixmap_format}")
    if pixmap_depth != XWD_DEPTH_24:
        raise PairMismatchError(f"unsupported XWD pixmap_depth {pixmap_depth}")
    if pixmap_width <= 0 or pixmap_height <= 0:
        raise PairMismatchError("XWD width and height must be positive")
    if (window_width, window_height) != (pixmap_width, pixmap_height):
        raise PairMismatchError("XWD window dimensions differ from pixmap dimensions")
    if xoffset != 0 or window_bdrwidth != 0:
        raise PairMismatchError("XWD xoffset and window_bdrwidth must be 0")
    if byte_order not in (0, 1) or bitmap_bit_order not in (0, 1):
        raise PairMismatchError("XWD byte_order/bitmap_bit_order must be 0 or 1")
    if bitmap_unit not in (8, 16, 32) or bitmap_pad not in (8, 16, 32):
        raise PairMismatchError("XWD bitmap_unit/bitmap_pad must be 8, 16, or 32")
    if bits_per_pixel not in (24, 32):
        raise PairMismatchError(f"unsupported XWD bits_per_pixel {bits_per_pixel}")
    if visual_class not in (XWD_VISUAL_TRUE_COLOR, XWD_VISUAL_DIRECT_COLOR):
        raise PairMismatchError(f"unsupported XWD visual_class {visual_class}")
    if (red_mask, green_mask, blue_mask) != (XWD_RED_MASK, XWD_GREEN_MASK, XWD_BLUE_MASK):
        raise PairMismatchError("XWD RGB masks do not match 0xff0000/0x00ff00/0x0000ff")
    if bits_per_rgb != XWD_BITS_PER_RGB:
        raise PairMismatchError(f"unsupported XWD bits_per_rgb {bits_per_rgb}")

    min_stride = pixmap_width * (bits_per_pixel // 8)
    if bytes_per_line < min_stride:
        raise PairMismatchError("XWD bytes_per_line smaller than row width")

    colormap_bytes = ncolors * 12
    pixel_offset = header_size + colormap_bytes
    expected_total = pixel_offset + pixmap_height * bytes_per_line
    if len(buf) != expected_total:
        raise PairMismatchError(f"XWD total byte length {len(buf)} != expected {expected_total}")

    name_slice = buf[XWD_MIN_HEADER_SIZE:header_size]
    if not name_slice or not name_slice.endswith(b"\x00"):
        raise PairMismatchError("XWD window name must be NUL-terminated")

    out_rgb = bytearray(pixmap_width * pixmap_height * 3)
    dst_stride = pixmap_width * 3
    for y in range(pixmap_height):
        row_start = pixel_offset + y * bytes_per_line
        row_data = buf[row_start:row_start + min_stride]
        dst_start = y * dst_stride
        if bits_per_pixel == 24:
            if byte_order == 1:
                out_rgb[dst_start:dst_start + dst_stride] = row_data
            else:
                out_rgb[dst_start + 0:dst_start + dst_stride:3] = row_data[2::3]
                out_rgb[dst_start + 1:dst_start + dst_stride:3] = row_data[1::3]
                out_rgb[dst_start + 2:dst_start + dst_stride:3] = row_data[0::3]
        else:
            if byte_order == 1:
                out_rgb[dst_start + 0:dst_start + dst_stride:3] = row_data[1::4]
                out_rgb[dst_start + 1:dst_start + dst_stride:3] = row_data[2::4]
                out_rgb[dst_start + 2:dst_start + dst_stride:3] = row_data[3::4]
            else:
                out_rgb[dst_start + 0:dst_start + dst_stride:3] = row_data[2::4]
                out_rgb[dst_start + 1:dst_start + dst_stride:3] = row_data[1::4]
                out_rgb[dst_start + 2:dst_start + dst_stride:3] = row_data[0::4]

    rgb_bytes = bytes(out_rgb)
    rgb_sha256 = hashlib.sha256(rgb_bytes).hexdigest()
    return XwdFrameInfo(
        width=pixmap_width,
        height=pixmap_height,
        stride=dst_stride,
        bits_per_pixel=bits_per_pixel,
        byte_order=byte_order,
        visual_class=visual_class,
        window_x=window_x,
        window_y=window_y,
        rgb_bytes=rgb_bytes,
        rgb_sha256=rgb_sha256,
    )


def verify_same_grab_pair(grab_a: bytes, grab_b: bytes) -> XwdFrameInfo:
    """Verify both grabs in a held pair satisfy common XWD subset and canonical RGB24 equality."""
    info_a = parse_xwd_common_subset(grab_a)
    info_b = parse_xwd_common_subset(grab_b)
    if (
        info_a.width != info_b.width
        or info_a.height != info_b.height
        or info_a.stride != info_b.stride
        or info_a.visual_class != info_b.visual_class
        or info_a.window_x != info_b.window_x
        or info_a.window_y != info_b.window_y
        or info_a.rgb_sha256 != info_b.rgb_sha256
        or info_a.rgb_bytes != info_b.rgb_bytes
    ):
        raise PairMismatchError("same-grab held pair mismatch between grab_a and grab_b")
    return info_a


# ---------------------------------------------------------------------------
# Credit Flow Control (`E_CREDIT`) & In-Memory Pipe Stream
# ---------------------------------------------------------------------------

class CreditController:
    """Enforces W8U ACK/credit invariants:
    - Initial ACK seq 0 grants min(2, reserved_capacity).
    - Cumulative credits <= acknowledged_frames + reserved_capacity.
    - Duplicate ACK or credit inflation raises CreditError (`E_CREDIT`).
    """

    def __init__(self, reserved_capacity: int, *, preferred_initial: int = PREFERRED_INITIAL_CREDITS) -> None:
        if not _is_exact_int(reserved_capacity) or reserved_capacity < 1:
            raise CreditError("reserved_capacity must be an integer >= 1")
        if not _is_exact_int(preferred_initial) or preferred_initial < 1:
            raise CreditError("preferred_initial must be an integer >= 1")
        self.reserved_capacity = reserved_capacity
        self.preferred_initial = preferred_initial
        self.initial_acked = False
        self.cumulative_credits = 0
        self.frames_emitted = 0
        self.acknowledged_frames = 0
        self._acked_seqs: set[int] = set()
        self._emitted_seqs: set[int] = set()

    @property
    def available_credits(self) -> int:
        return self.cumulative_credits - self.frames_emitted

    @property
    def in_flight(self) -> int:
        return self.frames_emitted - self.acknowledged_frames

    def ack_initial(self, seq: int = 0, *, grant: int | None = None) -> int:
        if not _is_exact_int(seq) or seq != 0:
            raise CreditError("initial ACK must have seq == 0")
        if self.initial_acked:
            raise CreditError("duplicate initial ACK seq 0")
        max_initial = min(self.preferred_initial, self.reserved_capacity)
        actual_grant = max_initial if grant is None else grant
        if not _is_exact_int(actual_grant) or actual_grant < 1 or actual_grant > max_initial:
            raise CreditError(f"initial grant {actual_grant!r} violates min(2, capacity)={max_initial}")
        self.initial_acked = True
        self.cumulative_credits = actual_grant
        return actual_grant

    def consume_for_frame(self, seq: int) -> None:
        if not self.initial_acked:
            raise CreditError("cannot emit frame before initial ACK seq 0")
        if not _is_exact_int(seq) or seq < 0:
            raise CreditError("frame seq must be a non-negative integer")
        if seq in self._emitted_seqs:
            raise CreditError(f"duplicate frame emission for seq {seq}")
        if self.frames_emitted >= self.cumulative_credits:
            raise CreditError("credit exhausted: frames_emitted == cumulative_credits")
        if (self.frames_emitted + 1) - self.acknowledged_frames > self.reserved_capacity:
            raise CreditError("in-flight frames would exceed reserved_capacity")
        self._emitted_seqs.add(seq)
        self.frames_emitted += 1

    def ack_frame(self, seq: int, *, grant: int = 1) -> int:
        if not self.initial_acked:
            raise CreditError("cannot ACK frame before initial ACK seq 0")
        if not _is_exact_int(seq) or seq < 0:
            raise CreditError("ACK seq must be a non-negative integer")
        if not _is_exact_int(grant) or grant < 0:
            raise CreditError("ACK grant must be a non-negative integer")
        if seq in self._acked_seqs:
            raise CreditError(f"duplicate ACK for frame seq {seq}")
        if seq not in self._emitted_seqs:
            raise CreditError(f"ACK for unseen frame seq {seq}")
        expected_next = self.acknowledged_frames
        if seq != expected_next:
            raise CreditError(f"out-of-order ACK seq {seq} != expected {expected_next}")

        self._acked_seqs.add(seq)
        self.acknowledged_frames += 1
        new_cumulative = self.cumulative_credits + grant
        max_allowed = self.acknowledged_frames + self.reserved_capacity
        if new_cumulative > max_allowed:
            self._acked_seqs.remove(seq)
            self.acknowledged_frames -= 1
            raise CreditError(
                f"credit overgrant/inflation: cumulative {new_cumulative} > acked ({self.acknowledged_frames}) + capacity ({self.reserved_capacity})"
            )
        self.cumulative_credits = new_cumulative
        return self.cumulative_credits


class InMemoryExecPipe:
    """In-memory control/frame pipe enforcing single-open and STOP/END/EOF lifecycle."""

    def __init__(self, *, reserved_capacity: int = 2) -> None:
        self.credit = CreditController(reserved_capacity)
        self.opened = False
        self.open_count = 0
        self.stopped = False
        self.ended = False
        self.eof = False
        self.joined = False
        self.end_error_code: int | None = None
        self.control_log: list[tuple[str, dict[str, Any]]] = []

    def open_once(self, hello_body: dict[str, Any]) -> int:
        if self.opened or self.open_count > 0:
            raise ProtocolError("reopen per frame is forbidden", code=E_REOPEN)
        for key in CONTROL_BODY_SCHEMAS["HELLO"]:
            if key not in hello_body:
                raise ProtocolError(f"HELLO missing {key!r}")
        if hello_body["contract_revision"] != W8U_OS_CONTRACT_REVISION:
            raise ProtocolError("unpinned or mismatched w8u-os-contract revision")
        self.opened = True
        self.open_count += 1
        self.control_log.append(("HELLO", copy.deepcopy(hello_body)))
        granted = self.credit.ack_initial(0)
        self.control_log.append(("ACK", {"seq": 0, "grant": granted}))
        return granted

    @property
    def is_terminal(self) -> bool:
        return self.stopped or self.ended or self.eof

    def ensure_active(self) -> None:
        if not self.opened:
            raise ProtocolError("exec pipe has not been opened")
        if self.is_terminal:
            raise ProtocolError("stale publication after STOP/END/EOF is forbidden")

    def ack(self, seq: int, *, grant: int = 1) -> int:
        self.ensure_active()
        total = self.credit.ack_frame(seq, grant=grant)
        self.control_log.append(("ACK", {"seq": seq, "grant": grant}))
        return total

    def send_stop(self, reason: str = "operator_stop") -> None:
        if not self.opened:
            raise ProtocolError("cannot STOP unopened pipe")
        self.stopped = True
        self.control_log.append(("STOP", {"reason": reason}))

    def send_end(self, error_code: int = END_OK, *, frames_captured: int = 0, reason: str = "complete") -> None:
        if not self.opened:
            raise ProtocolError("cannot END unopened pipe")
        if error_code not in END_NUMERIC_ERRORS:
            raise ProtocolError(f"unknown END numeric error code: {error_code!r}")
        self.ended = True
        self.end_error_code = error_code
        self.control_log.append(("END", {
            "error_code": error_code,
            "frames_captured": frames_captured,
            "reason": reason,
        }))

    def close_eof(self) -> None:
        if not self.opened:
            raise ProtocolError("cannot EOF unopened pipe")
        self.eof = True
        self.control_log.append(("EOF", {}))

    def join(self) -> int:
        if not (self.stopped or self.ended or self.eof):
            raise ProtocolError("cannot join reader before STOP/END and EOF")
        if not self.ended:
            self.send_end(END_OK, frames_captured=self.credit.frames_emitted, reason="joined")
        if not self.eof:
            self.close_eof()
        self.joined = True
        return 0 if self.end_error_code == END_OK else int(self.end_error_code or 0)


# ---------------------------------------------------------------------------
# Authenticated Capabilities & Evidence Records
# ---------------------------------------------------------------------------

class RegistryFocusAdapter:
    """Bridges `SessionRegistry.resolve(handle)` to the `registry.get(handle)` interface required by `FocusPolicy`."""

    def __init__(self, registry: SessionRegistry) -> None:
        if not isinstance(registry, SessionRegistry):
            raise RegistryError("registry must be a SessionRegistry")
        self._registry = registry

    def get(self, handle: Handle) -> TargetRecord | None:
        try:
            return self._registry.resolve(handle)
        except RegistryError:
            return None


@dataclass(frozen=True)
class PrivateXIdentity:
    """Admitted private Xvfb/X11 identity bound to a specific SessionRegistry handle."""

    handle: Handle
    display: str
    window_id: str
    pid: int
    auth_token: bytes
    x11_unix_listeners: int = 1
    x11_tcp_listeners: int = 0
    x11_abstract_listeners: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.handle, Handle) or self.handle.kind not in DESKTOP_TARGET_KINDS:
            raise ForeignTargetError("handle must be a desktop Handle (x11_window, xvfb_screen, or capsule)")
        if not isinstance(self.display, str) or not _PRIVATE_DISPLAY_RE.fullmatch(self.display):
            raise ForeignTargetError(f"display {self.display!r} is not an admitted private Xvfb display")
        if not isinstance(self.window_id, str) or not _WINDOW_ID_RE.fullmatch(self.window_id):
            raise ForeignTargetError(f"window_id {self.window_id!r} is not an admitted window identifier")
        if not _is_exact_int(self.pid) or self.pid <= 0:
            raise ForeignTargetError("pid must be a positive integer")
        if not isinstance(self.auth_token, bytes) or len(self.auth_token) < 8:
            raise ForeignTargetError("auth_token must be at least 8 bytes")
        if not _is_exact_int(self.x11_tcp_listeners) or self.x11_tcp_listeners != 0:
            raise ForeignTargetError("private X server must have 0 TCP listeners")
        if not _is_exact_int(self.x11_unix_listeners) or self.x11_unix_listeners < 0:
            raise ForeignTargetError("x11_unix_listeners must be a non-negative int")
        if not _is_exact_int(self.x11_abstract_listeners) or self.x11_abstract_listeners != 0:
            raise ForeignTargetError("private X server must have 0 abstract listeners")


@dataclass(frozen=True)
class TickWitness:
    """Authenticated simulation/T2 tick witness (never invented from timestamp)."""

    tick: int
    source_id: str
    witness_token: bytes

    def __post_init__(self) -> None:
        if not _is_exact_int(self.tick) or self.tick < 0:
            raise TickInventedError("witness tick must be a non-negative integer")
        if not _is_nonempty_str(self.source_id):
            raise TickInventedError("witness source_id must be a non-empty string")
        if not isinstance(self.witness_token, bytes) or not self.witness_token:
            raise TickInventedError("witness_token must be non-empty bytes")


@dataclass(frozen=True)
class InjectedPairEvidence:
    """Authenticated revision-pinned held-pair frame evidence."""

    contract_revision: str
    run_id: str
    lease_id: str
    handle: Handle
    epoch: int
    display: str
    window_id: str
    seq: int
    capture_mono_ns: int
    grab_a: bytes
    grab_b: bytes
    auth_token: bytes
    tick: int | None = None
    tick_valid: bool = False
    tick_witness: TickWitness | None = None


@dataclass(frozen=True)
class AuthenticatedAtspiCapability:
    """Injected target-bound AT-SPI capability wrapping `atspi_tree.walk` and `atspi_state`."""

    handle: Handle
    display: str
    window_id: str
    pid: int
    address: str
    bus: str
    root_path: str
    title: str
    auth_token: bytes
    call: Callable[..., str | None] | None = None
    boxes: dict[str, list[int]] | None = None
    require_boxes: bool = False
    discovery_only: bool = False
    max_nodes: int = 2000
    max_depth: int = 40
    timeout: int = 10

    def convert(
        self,
        *,
        expected_identity: PrivateXIdentity,
        frame_width: int,
        frame_height: int,
    ) -> dict[str, Any]:
        """Prove target-bound identity and complete AT-SPI tree conversion into `wender.state/v1`."""
        if (
            self.handle != expected_identity.handle
            or self.display != expected_identity.display
            or self.window_id != expected_identity.window_id
            or self.pid != expected_identity.pid
            or self.auth_token != expected_identity.auth_token
        ):
            raise ForeignTargetError("AT-SPI capability does not match bound private X target identity")
        if self.discovery_only or self.call is None or self.call is atspi._call:
            raise ModalityError("discovery alone or live bus default is insufficient; complete injected converter required")
        if not _is_nonempty_str(self.address) or not _is_nonempty_str(self.bus) or not _is_nonempty_str(self.root_path):
            raise ModalityError("AT-SPI capability requires non-empty address, bus, and root_path")

        walk_result = atspi_tree.walk(
            self.address,
            self.bus,
            self.root_path,
            call=self.call,
            max_nodes=self.max_nodes,
            max_depth=self.max_depth,
            timeout=self.timeout,
        )
        if not isinstance(walk_result, dict):
            raise ModalityError("AT-SPI walk did not return a dict")
        if walk_result.get("truncated") is True:
            raise ModalityError("AT-SPI walk was truncated; partial coverage is refused")
        raw_count = walk_result.get("raw_count", 0)
        nodes = walk_result.get("nodes")
        if not _is_exact_int(raw_count) or raw_count <= 0 or not isinstance(nodes, list) or len(nodes) == 0:
            raise ModalityError("empty AT-SPI semantics cannot be sold as complete")

        validated_boxes: dict[str, list[int]] | None = None
        if self.boxes is not None:
            if not isinstance(self.boxes, dict):
                raise ModalityError("boxes must be a dict mapping node id to [x, y, w, h]")
            node_ids = {n["id"] for n in nodes}
            if self.require_boxes and set(self.boxes.keys()) != node_ids:
                raise ModalityError("partial box coverage across emitted AT-SPI nodes")
            validated_boxes = {}
            for nid, box in self.boxes.items():
                if nid not in node_ids:
                    raise ModalityError(f"box provided for unknown node id {nid!r}")
                if (
                    not isinstance(box, (list, tuple))
                    or len(box) != 4
                    or not all(_is_exact_int(v) for v in box)
                ):
                    raise W8UDesktopError(E_COORDINATES, f"box for {nid!r} must be 4 exact ints", rc=RC_REFUSED)
                bx, by, bw, bh = box
                if bw <= 0 or bh <= 0 or bx < 0 or by < 0 or (bx + bw) > frame_width or (by + bh) > frame_height:
                    raise W8UDesktopError(E_COORDINATES, f"box {box!r} out of frame bounds {frame_width}x{frame_height}", rc=RC_REFUSED)
                validated_boxes[nid] = [bx, by, bw, bh]
        elif self.require_boxes:
            raise ModalityError("required node boxes missing for AT-SPI conversion")

        state = schema.build_state(
            nodes,
            kind="atspi",
            url="",
            title=ax.normalize_name(self.title),
            ax_sha256=ax.snapshot_sha(nodes),
            ax_stable_sha256=ax.snapshot_sha(nodes, with_state=False),
            prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION},
            raw_count=raw_count,
            boxes=validated_boxes,
            viewport={"width": frame_width, "height": frame_height},
            actionable_roles=ax.ACTIONABLE_ROLES,
        )
        errs = schema.validate_state(state)
        if errs:
            raise ModalityError("; ".join(errs))
        return state


def build_vision_only_state(*, title: str, width: int, height: int) -> dict[str, Any]:
    """Build canonical vision-only `wender.state/v1` retaining semantic absence."""
    if not _is_exact_int(width) or not _is_exact_int(height) or width <= 0 or height <= 0:
        raise W8UDesktopError(E_COORDINATES, "viewport width and height must be positive integers", rc=RC_REFUSED)
    state = schema.build_state(
        [],
        kind="vision",
        url="",
        title=ax.normalize_name(title),
        ax_sha256=ax.snapshot_sha([]),
        ax_stable_sha256=ax.snapshot_sha([], with_state=False),
        prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION},
        raw_count=0,
        boxes=None,
        viewport={"width": width, "height": height},
        actionable_roles=ax.ACTIONABLE_ROLES,
    )
    errs = schema.validate_state(state)
    if errs:
        raise ModalityError("; ".join(errs))
    return state


def build_frame_binding(
    *,
    contract_revision: str,
    run_id: str,
    lease_id: str,
    handle: Handle,
    epoch: int,
    display: str,
    window_id: str,
    seq: int,
    width: int,
    height: int,
    stride: int,
    frame_sha256: str,
) -> tuple[dict[str, Any], str]:
    """Build canonical `w8u.binding/v1` record and its domain-separated SHA-256 digest."""
    binding = {
        "schema": W8U_BINDING_SCHEMA,
        "contract_revision": contract_revision,
        "run_id": run_id,
        "lease_id": lease_id,
        "handle": {
            "kind": handle.kind,
            "canonical_id": handle.canonical_id,
            "generation": handle.generation,
        },
        "epoch": epoch,
        "display": display,
        "window_id": window_id,
        "seq": seq,
        "width": width,
        "height": height,
        "stride": stride,
        "pixel_format": PIXEL_FORMAT_RGB24,
        "frame_sha256": frame_sha256,
    }
    return binding, canonical_digest("frame_binding", binding)


def build_observation_result(
    *,
    run_id: str,
    lease_id: str,
    handle: Handle,
    epoch: int,
    frame_seq: int,
    dpr_ppm: int,
    modality_policy: str,
    semantic_modality: str | None,
    state_sha256: str,
    frame_sha256: str,
    binding_sha256: str,
) -> dict[str, Any]:
    """Build frozen `w8u.result/v1` summary record (omitted semantic modality becomes AUSENTE)."""
    return {
        "schema": W8U_RESULT_SCHEMA,
        "run_id": run_id,
        "lease_id": lease_id,
        "handle": {
            "kind": handle.kind,
            "canonical_id": handle.canonical_id,
            "generation": handle.generation,
        },
        "epoch": epoch,
        "frame_seq": frame_seq,
        "dpr_ppm": dpr_ppm,
        "modality_policy": modality_policy,
        "semantic_modality": AUSENTE if semantic_modality is None else semantic_modality,
        "state_sha256": state_sha256,
        "frame_sha256": frame_sha256,
        "binding_sha256": binding_sha256,
    }


# ---------------------------------------------------------------------------
# Action Gate (`w8u.action/v1`)
# ---------------------------------------------------------------------------

def compute_action_grant_sha256(
    *,
    run_id: str,
    lease_id: str,
    handle: Handle,
    epoch: int,
    action_id: str,
    action_seq: int,
    authority_secret: bytes,
) -> str:
    """Compute external authority grant digest for a specific action."""
    if not isinstance(authority_secret, bytes) or not authority_secret:
        raise ValueError("authority_secret must be non-empty bytes")
    grant_obj = {
        "run_id": run_id,
        "lease_id": lease_id,
        "handle": {
            "kind": handle.kind,
            "canonical_id": handle.canonical_id,
            "generation": handle.generation,
        },
        "epoch": epoch,
        "action_id": action_id,
        "action_seq": action_seq,
        "secret_sha256": hashlib.sha256(authority_secret).hexdigest(),
    }
    return canonical_digest("action_grant", grant_obj)


@dataclass(frozen=True)
class ActionAuthorityContext:
    """Authenticated authority context evaluated outside the action payload."""

    run_id: str
    lease_id: str
    handle: Handle
    epoch: int
    authority_secret: bytes
    lock_owned: bool = True
    focused: bool = True
    editable_focus: bool = False
    remaining_budget: int = 100


class W8UActionGate:
    """Validates and executes `w8u.action/v1` against a target-bound desktop session."""

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        authority: ActionAuthorityContext,
        clock: Callable[[], int],
        actuator: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        self.registry = registry
        self.authority = authority
        self.clock = clock
        self.actuator = actuator
        self.next_action_seq = 0
        self.quarantined = False
        self.remaining_budget = authority.remaining_budget
        self.latest_frame_seq: int | None = None
        self.latest_frame_sha256: str | None = None
        self.frame_width: int = 0
        self.frame_height: int = 0
        self._history_by_id: dict[str, tuple[str, dict[str, Any], int]] = {}
        self.action_log: list[dict[str, Any]] = []
        self.admitted_count = 0
        self.refused_count = 0
        self.uncertain_count = 0

    def update_observed_frame(self, frame_ref: FrameRef) -> None:
        self.latest_frame_seq = frame_ref.frame_seq
        self.latest_frame_sha256 = frame_ref.digest
        self.frame_width = frame_ref.width
        self.frame_height = frame_ref.height

    def _build_response(
        self,
        *,
        action_id: str,
        action_seq: int,
        lease_id: str,
        epoch: int,
        status: str,
        code: str,
        emitted: bool,
        result_known: bool,
        applied_tick: int | None,
        rc: int,
        record_log: bool = True,
    ) -> tuple[dict[str, Any], int]:
        outcome_payload = {
            "action_id": action_id,
            "action_seq": action_seq,
            "lease_id": lease_id,
            "epoch": epoch,
            "status": status,
            "code": code,
            "emitted": emitted,
            "result_known": result_known,
            "applied_tick": applied_tick,
        }
        outcome_sha256 = canonical_digest("action_outcome", outcome_payload)
        resp = {
            "schema": W8U_ACTION_RESPONSE_SCHEMA,
            "action_id": action_id,
            "action_seq": action_seq,
            "lease_id": lease_id,
            "epoch": epoch,
            "status": status,
            "code": code,
            "emitted": emitted,
            "result_known": result_known,
            "applied_tick": applied_tick,
            "outcome_sha256": outcome_sha256,
        }
        if record_log:
            self.action_log.append(copy.deepcopy(resp))
            if status in ("admitted", "applied"):
                self.admitted_count += 1
            elif status == "uncertain":
                self.uncertain_count += 1
            else:
                self.refused_count += 1
        return resp, rc

    def _validate_verb_and_args(self, verb: Any, args: Any) -> str | None:
        if not isinstance(verb, str) or verb not in VALID_VERBS:
            return E_VERB
        if not isinstance(args, dict):
            return E_SCHEMA

        if verb == "pointer":
            op = args.get("operation")
            if not isinstance(op, str) or op not in POINTER_OPERATIONS:
                return E_SCHEMA
            if op == "move":
                if sorted(args.keys()) != ["operation", "x", "y"]:
                    return E_SCHEMA
            elif op in ("click", "down", "up"):
                if sorted(args.keys()) != ["button", "operation", "x", "y"]:
                    return E_SCHEMA
                if not _is_exact_int(args["button"]) or args["button"] not in POINTER_BUTTONS:
                    return E_SCHEMA
            elif op == "scroll":
                if sorted(args.keys()) != ["dx", "dy", "operation", "x", "y"]:
                    return E_SCHEMA
                dx, dy = args["dx"], args["dy"]
                if (
                    not _is_exact_int(dx)
                    or not _is_exact_int(dy)
                    or dx < SCROLL_MIN_DELTA
                    or dx > SCROLL_MAX_DELTA
                    or dy < SCROLL_MIN_DELTA
                    or dy > SCROLL_MAX_DELTA
                    or (dx == 0 and dy == 0)
                ):
                    return E_SCHEMA
            x, y = args.get("x"), args.get("y")
            if not _is_exact_int(x) or not _is_exact_int(y):
                return E_SCHEMA
            if self.frame_width <= 0 or self.frame_height <= 0 or x < 0 or x >= self.frame_width or y < 0 or y >= self.frame_height:
                return E_SCHEMA
            return None

        if verb == "key":
            if sorted(args.keys()) != ["key", "modifiers", "operation"]:
                return E_SCHEMA
            if not isinstance(args["operation"], str) or args["operation"] not in KEY_OPERATIONS:
                return E_SCHEMA
            if not isinstance(args["key"], str) or args["key"] not in FINITE_KEYS:
                return E_SCHEMA
            mods = args["modifiers"]
            if not isinstance(mods, list) or not all(isinstance(m, str) for m in mods):
                return E_SCHEMA
            if len(set(mods)) != len(mods):
                return E_SCHEMA
            if any(m not in ORDERED_MODIFIERS for m in mods):
                return E_SCHEMA
            expected_order = [m for m in ORDERED_MODIFIERS if m in mods]
            if mods != expected_order:
                return E_SCHEMA
            return None

        if verb == "text":
            if sorted(args.keys()) != ["mode", "text"]:
                return E_SCHEMA
            if args["mode"] != "type":
                return E_SCHEMA
            txt = args["text"]
            if not isinstance(txt, str) or "\x00" in txt:
                return E_SCHEMA
            try:
                encoded = txt.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return E_SCHEMA
            if len(encoded) == 0 or len(encoded) > TEXT_MAX_UTF8_BYTES:
                return E_SCHEMA
            return None

        if verb == "wait":
            if sorted(args.keys()) != ["duration_ms"]:
                return E_SCHEMA
            dur = args["duration_ms"]
            if not _is_exact_int(dur) or dur < 0 or dur > WAIT_MAX_DURATION_MS:
                return E_SCHEMA
            return None

        return E_VERB

    def submit(self, raw_request: bytes | str | dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Validate and execute a single `w8u.action/v1` request."""
        fallback_id = "invalid-action"
        fallback_seq = self.next_action_seq
        fallback_lease = self.authority.lease_id
        fallback_epoch = self.authority.epoch

        try:
            if isinstance(raw_request, dict):
                _check_no_float_or_perception_wrapper(raw_request)
                canonical_text = schema.dumps(raw_request)
                if len(canonical_text.encode("utf-8", errors="strict")) > ACTION_MAX_UTF8_BYTES:
                    raise W8UDesktopError(E_SCHEMA, "action payload exceeds 64 KiB", rc=RC_SCHEMA)
                req = copy.deepcopy(raw_request)
            else:
                req = parse_strict_json_object(raw_request, max_bytes=ACTION_MAX_UTF8_BYTES)
                canonical_text = schema.dumps(req)
        except W8UDesktopError:
            return self._build_response(
                action_id=fallback_id,
                action_seq=fallback_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=E_SCHEMA,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_SCHEMA,
            )

        if isinstance(req.get("action_id"), str) and req["action_id"]:
            fallback_id = req["action_id"]
        if _is_exact_int(req.get("action_seq")) and req["action_seq"] >= 0:
            fallback_seq = req["action_seq"]

        if sorted(req.keys()) != sorted(ACTION_REQUEST_KEYS):
            return self._build_response(
                action_id=fallback_id,
                action_seq=fallback_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=E_SCHEMA,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_SCHEMA,
            )

        if (
            req["schema"] != W8U_ACTION_SCHEMA
            or not _is_nonempty_str(req["run_id"])
            or not _is_nonempty_str(req["lease_id"])
            or not _is_nonempty_str(req["action_id"])
            or not _is_exact_int(req["epoch"])
            or req["epoch"] < 0
            or not _is_exact_int(req["generation"])
            or req["generation"] < 1
            or not _is_exact_int(req["action_seq"])
            or req["action_seq"] < 0
            or not _is_exact_int(req["observed_frame_seq"])
            or req["observed_frame_seq"] < 0
            or not _is_hex64(req["observed_frame_sha256"])
            or not _is_exact_int(req["deadline_ns"])
            or req["deadline_ns"] < 0
            or not _is_hex64(req["grant_sha256"])
        ):
            return self._build_response(
                action_id=fallback_id,
                action_seq=fallback_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=E_SCHEMA,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_SCHEMA,
            )

        action_id = req["action_id"]
        action_seq = req["action_seq"]

        # Replay check BEFORE actuator call: identical payload is idempotent; changed payload refuses.
        if action_id in self._history_by_id:
            prev_canonical, prev_resp, prev_rc = self._history_by_id[action_id]
            if prev_canonical == canonical_text:
                return copy.deepcopy(prev_resp), prev_rc
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=E_SCHEMA,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_SCHEMA,
            )

        verb_err = self._validate_verb_and_args(req["verb"], req["args"])
        if verb_err is not None:
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=verb_err,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_SCHEMA,
            )

        # Lost ack quarantine blocks new-ID retries
        if self.quarantined:
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=E_LOCK,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_REFUSED,
            )

        # Verify target handle & epoch against live SessionRegistry
        try:
            record = self.registry.resolve(self.authority.handle)
        except RegistryError:
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=E_EPOCH,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_REFUSED,
            )

        if req["epoch"] != record.epoch or req["epoch"] != self.authority.epoch or req["generation"] != record.handle.generation:
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=fallback_lease,
                epoch=record.epoch,
                status="refused",
                code=E_EPOCH,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_REFUSED,
            )

        # External authority check (never trust payload alone)
        expected_grant = compute_action_grant_sha256(
            run_id=self.authority.run_id,
            lease_id=self.authority.lease_id,
            handle=self.authority.handle,
            epoch=self.authority.epoch,
            action_id=action_id,
            action_seq=action_seq,
            authority_secret=self.authority.authority_secret,
        )
        if (
            not self.authority.lock_owned
            or req["run_id"] != self.authority.run_id
            or req["lease_id"] != self.authority.lease_id
            or req["grant_sha256"] != expected_grant
            or action_seq != self.next_action_seq
        ):
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=self.authority.lease_id,
                epoch=self.authority.epoch,
                status="refused",
                code=E_LOCK,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_REFUSED,
            )

        # Stale frame check
        if (
            self.latest_frame_seq is None
            or req["observed_frame_seq"] != self.latest_frame_seq
            or req["observed_frame_sha256"] != self.latest_frame_sha256
        ):
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=self.authority.lease_id,
                epoch=self.authority.epoch,
                status="refused",
                code=E_STALE_FRAME,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_REFUSED,
            )

        # Focus check
        if req["verb"] != "wait":
            if not self.authority.focused:
                return self._build_response(
                    action_id=action_id,
                    action_seq=action_seq,
                    lease_id=self.authority.lease_id,
                    epoch=self.authority.epoch,
                    status="refused",
                    code=E_FOCUS,
                    emitted=False,
                    result_known=True,
                    applied_tick=None,
                    rc=RC_REFUSED,
                )
            if req["verb"] == "text" and not self.authority.editable_focus:
                return self._build_response(
                    action_id=action_id,
                    action_seq=action_seq,
                    lease_id=self.authority.lease_id,
                    epoch=self.authority.epoch,
                    status="refused",
                    code=E_FOCUS,
                    emitted=False,
                    result_known=True,
                    applied_tick=None,
                    rc=RC_REFUSED,
                )

        # Budget check
        if self.remaining_budget <= 0:
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=self.authority.lease_id,
                epoch=self.authority.epoch,
                status="refused",
                code=E_BUDGET,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_REFUSED,
            )

        # Deadline check
        now_ns = self.clock()
        wait_ns = req["args"]["duration_ms"] * 1_000_000 if req["verb"] == "wait" else 0
        if now_ns > req["deadline_ns"] or (now_ns + wait_ns) > req["deadline_ns"]:
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=self.authority.lease_id,
                epoch=self.authority.epoch,
                status="refused",
                code=E_DEADLINE,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_REFUSED,
            )

        if self.actuator is None:
            return self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=self.authority.lease_id,
                epoch=self.authority.epoch,
                status="refused",
                code=E_UNCERTAIN,
                emitted=False,
                result_known=True,
                applied_tick=None,
                rc=RC_UNAVAILABLE,
            )

        self.remaining_budget -= 1
        self.next_action_seq += 1
        act_result = self.actuator(req["verb"], copy.deepcopy(req["args"])) or {}

        if act_result.get("lost_ack") is True or act_result.get("uncertain") is True:
            self.quarantined = True
            self.registry.stop(self.authority.handle)
            resp, rc = self._build_response(
                action_id=action_id,
                action_seq=action_seq,
                lease_id=self.authority.lease_id,
                epoch=self.authority.epoch,
                status="uncertain",
                code=E_UNCERTAIN,
                emitted=True,
                result_known=False,
                applied_tick=None,
                rc=RC_UNCERTAIN,
            )
            self._history_by_id[action_id] = (canonical_text, copy.deepcopy(resp), rc)
            return resp, rc

        applied = bool(act_result.get("applied", False))
        applied_tick = act_result.get("applied_tick")
        if not applied:
            applied_tick = None
        elif applied_tick is not None and (not _is_exact_int(applied_tick) or applied_tick < 0):
            raise TickInventedError("applied_tick must be a non-negative int or None")

        status = "applied" if applied else "admitted"
        resp, rc = self._build_response(
            action_id=action_id,
            action_seq=action_seq,
            lease_id=self.authority.lease_id,
            epoch=self.authority.epoch,
            status=status,
            code=OK_ADMITTED,
            emitted=True,
            result_known=applied,
            applied_tick=applied_tick,
            rc=RC_OK,
        )
        self._history_by_id[action_id] = (canonical_text, copy.deepcopy(resp), rc)
        return resp, rc


# ---------------------------------------------------------------------------
# Receipt (`w8u.receipt/v1`) Builder & Validator
# ---------------------------------------------------------------------------

def validate_frame_metadata(meta: Any) -> list[str]:
    """Validate a single frame metadata dict against the 10-field contract."""
    errs: list[str] = []
    if not isinstance(meta, dict):
        return ["frame metadata must be a dict"]
    if sorted(meta.keys()) != sorted(FRAME_METADATA_KEYS):
        errs.append(f"frame metadata keys {sorted(meta.keys())} != {sorted(FRAME_METADATA_KEYS)}")
        return errs
    if not _is_exact_int(meta["seq"]) or meta["seq"] < 0:
        errs.append("seq must be a non-negative int")
    if not _is_exact_bool(meta["tick_valid"]):
        errs.append("tick_valid must be bool")
    else:
        if meta["tick_valid"]:
            if not _is_exact_int(meta["tick"]) or meta["tick"] < 0:
                errs.append("tick must be a non-negative int when tick_valid is True")
            elif _is_exact_int(meta["capture_mono_ns"]) and meta["tick"] == meta["capture_mono_ns"]:
                errs.append("timestamp invented as tick")
        else:
            if meta["tick"] is not None:
                errs.append("tick invalid means null, not committed zero or integer")
    if not _is_exact_int(meta["capture_mono_ns"]) or meta["capture_mono_ns"] < 0:
        errs.append("capture_mono_ns must be a non-negative int")
    if not _is_exact_int(meta["width"]) or meta["width"] <= 0:
        errs.append("width must be a positive int")
    if not _is_exact_int(meta["height"]) or meta["height"] <= 0:
        errs.append("height must be a positive int")
    if not _is_exact_int(meta["stride"]) or meta["stride"] != meta.get("width", 0) * 3:
        errs.append("stride must equal width * 3")
    if meta["pixel_format"] != PIXEL_FORMAT_RGB24:
        errs.append(f"pixel_format must be {PIXEL_FORMAT_RGB24!r}")
    if not _is_hex64(meta["sha256"]):
        errs.append("sha256 must be 64 lowercase hex")
    if not _is_hex64(meta["binding_sha256"]):
        errs.append("binding_sha256 must be 64 lowercase hex")
    return errs


def validate_receipt(receipt: Any, *, modality_policy: str | None = None) -> list[str]:
    """Validate a `w8u.receipt/v1` dictionary. Empty error list means valid."""
    errs: list[str] = []
    if not isinstance(receipt, dict):
        return ["receipt must be a dict"]
    try:
        _check_no_float_or_perception_wrapper(receipt)
    except W8UDesktopError as exc:
        errs.append(exc.message)

    keys = set(receipt.keys())
    if "schema" in keys:
        if receipt["schema"] != W8U_RECEIPT_SCHEMA:
            errs.append(f"receipt.schema must be {W8U_RECEIPT_SCHEMA!r}")
        keys_without_schema = keys - {"schema"}
    else:
        keys_without_schema = keys

    if sorted(keys_without_schema) != sorted(RECEIPT_REQUIRED_KEYS):
        errs.append(f"receipt keys {sorted(keys_without_schema)} != {sorted(RECEIPT_REQUIRED_KEYS)}")
        return errs

    run_id = receipt["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        errs.append("run_id is invalid")
    if not _is_nonempty_str(receipt["lease_id"]):
        errs.append("lease_id must be non-empty str")

    th = receipt["target_handle"]
    if not isinstance(th, dict) or sorted(th.keys()) != sorted(RECEIPT_TARGET_HANDLE_KEYS):
        errs.append("target_handle must have exact keys kind, canonical_id, generation")
    else:
        if th["kind"] not in DESKTOP_TARGET_KINDS:
            errs.append(f"target_handle.kind {th['kind']!r} not in {sorted(DESKTOP_TARGET_KINDS)}")
        if not _is_nonempty_str(th["canonical_id"]):
            errs.append("target_handle.canonical_id must be non-empty str")
        if not _is_exact_int(th["generation"]) or th["generation"] < 1:
            errs.append("target_handle.generation must be int >= 1")

    if not _is_exact_int(receipt["epoch"]) or receipt["epoch"] < 0:
        errs.append("epoch must be non-negative int")
    if receipt["frames_root"] != FRAMES_ROOT:
        errs.append(f"frames_root must be {FRAMES_ROOT!r}")
    expected_run_root = f"{FRAMES_ROOT}/{FRAME_RUN_SUBPATH}/{run_id}"
    if receipt["frame_run_root"] != expected_run_root:
        errs.append(f"frame_run_root must be {expected_run_root!r}")
    if receipt["source"] != RECEIPT_SOURCE:
        errs.append(f"source must be {RECEIPT_SOURCE!r}")

    fc = receipt["frames_captured"]
    if not _is_exact_int(fc) or fc < 0:
        errs.append("frames_captured must be non-negative int")
    elif fc == 0:
        if receipt["first_frame"] is not None or receipt["last_frame"] is not None:
            errs.append("zero-frame run must have null first_frame and last_frame")
        if receipt["ui_gate_verdict"] != "INDETERMINATE":
            errs.append("pixel-required zero-frame run must have ui_gate_verdict == 'INDETERMINATE'")
    else:
        for fname in ("first_frame", "last_frame"):
            f_errs = validate_frame_metadata(receipt[fname])
            errs.extend(f"{fname}.{e}" for e in f_errs)

    for count_key in (
        "ui_actions_count",
        "admitted_actions",
        "refused_actions",
        "uncertain_actions",
        "pair_mismatches",
        "frame_gaps",
        "frames_dropped",
        "x11_tcp_listeners",
        "x11_unix_listeners",
        "x11_abstract_listeners",
        "frames_disk_bytes",
    ):
        if not _is_exact_int(receipt[count_key]) or receipt[count_key] < 0:
            errs.append(f"{count_key} must be non-negative int")

    if (
        _is_exact_int(receipt["ui_actions_count"])
        and _is_exact_int(receipt["admitted_actions"])
        and _is_exact_int(receipt["refused_actions"])
        and _is_exact_int(receipt["uncertain_actions"])
    ):
        if receipt["ui_actions_count"] != (
            receipt["admitted_actions"] + receipt["refused_actions"] + receipt["uncertain_actions"]
        ):
            errs.append("ui_actions_count must equal sum of admitted, refused, and uncertain actions")

    if not _is_hex64(receipt["action_log_sha256"]):
        errs.append("action_log_sha256 must be 64 hex")

    for opt_sha in ("t2_trace_sha256", "t2_terminal_sha256", "final_state_sha256", "simulation_state_sha256"):
        if receipt[opt_sha] is not None and not _is_hex64(receipt[opt_sha]):
            errs.append(f"{opt_sha} must be 64 hex or null")

    if receipt["t2_ticks"] is not None and (not _is_exact_int(receipt["t2_ticks"]) or receipt["t2_ticks"] < 0):
        errs.append("t2_ticks must be non-negative int or null")

    if modality_policy == "vision_only":
        if (
            receipt["t2_trace_sha256"] is not None
            or receipt["t2_terminal_sha256"] is not None
            or receipt["t2_ticks"] is not None
        ):
            errs.append("vision_only policy requires null T2 trace/terminal digests and null t2_ticks")
    elif modality_policy == "atspi" and fc > 0 and receipt["ui_gate_verdict"] == "PASS":
        if (
            not _is_hex64(receipt["t2_trace_sha256"])
            or not _is_hex64(receipt["t2_terminal_sha256"])
            or not _is_exact_int(receipt["t2_ticks"])
            or receipt["t2_ticks"] <= 0
            or not _is_hex64(receipt["final_state_sha256"])
        ):
            errs.append("atspi PASS receipt requires non-null T2 trace/terminal sha256, positive t2_ticks, and final_state_sha256")

    if receipt["x11_tcp_listeners"] != 0:
        errs.append("x11_tcp_listeners must be 0")
    if receipt["frames_disk_bytes"] != 0:
        errs.append("frames_disk_bytes must be 0 (STORAGE LAW)")
    if not _is_exact_int(receipt["swap_delta_bytes"]):
        errs.append("swap_delta_bytes must be int")
    if not _is_exact_int(receipt["reader_exit_code"]):
        errs.append("reader_exit_code must be int")
    for bool_key in ("reader_quiescent", "ram_purged", "metadata_only"):
        if not _is_exact_bool(receipt[bool_key]):
            errs.append(f"{bool_key} must be bool")

    if receipt["ui_gate_verdict"] not in ("PASS", "FAIL", "INDETERMINATE"):
        errs.append("ui_gate_verdict must be PASS, FAIL, or INDETERMINATE")
    if receipt["ui_gate_verdict"] == "PASS":
        if (
            receipt["pair_mismatches"] != 0
            or receipt["frame_gaps"] != 0
            or not receipt["reader_quiescent"]
            or receipt["reader_exit_code"] != 0
            or not receipt["ram_purged"]
            or not receipt["metadata_only"]
        ):
            errs.append("PASS verdict requires 0 pair_mismatches/frame_gaps, quiescent reader rc 0, ram_purged, metadata_only")

    return errs


# ---------------------------------------------------------------------------
# Target-Bound Desktop Observation Adapter (`W8UDesktopAdapter`)
# ---------------------------------------------------------------------------

class W8UDesktopAdapter:
    """Target-bound desktop observation adapter enforcing private X identity,
    current generation/epoch, held-pair XWD validation, AT-SPI vs. vision-only
    modality policy, credit flow control, and receipt generation.
    """

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        handle: Handle,
        identity: PrivateXIdentity,
        run_id: str,
        lease_id: str,
        modality_policy: str = "atspi",
        atspi_capability: AuthenticatedAtspiCapability | None = None,
        dpr_ppm: int = 1_000_000,
        reserved_capacity: int = 2,
        clock: Callable[[], int] | None = None,
        authority_secret: bytes = b"w8u-default-authority-secret",
        options: dict[str, Any] | None = None,
    ) -> None:
        if options:
            bad = set(options.keys()) & FORBIDDEN_FALLBACK_KEYS
            if bad and any(options[k] for k in bad):
                raise ModalityError(f"forbidden discovery/clipboard/focus fallback enabled: {sorted(bad)}")
        if not isinstance(registry, SessionRegistry):
            raise RegistryError("registry must be a SessionRegistry instance")
        if not isinstance(handle, Handle) or handle.kind not in DESKTOP_TARGET_KINDS:
            raise ForeignTargetError("handle must be an admitted desktop Handle")
        if not isinstance(identity, PrivateXIdentity):
            raise ForeignTargetError("identity must be a PrivateXIdentity")
        if identity.handle != handle:
            raise ForeignTargetError("identity.handle does not match target handle")
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise W8UDesktopError(E_SCHEMA, f"invalid run_id: {run_id!r}", rc=RC_SCHEMA)
        if not _is_nonempty_str(lease_id):
            raise W8UDesktopError(E_SCHEMA, "lease_id must be a non-empty string", rc=RC_SCHEMA)
        if modality_policy not in MODALITY_POLICIES:
            raise ModalityError(f"unsupported modality_policy: {modality_policy!r}")
        if not _is_exact_int(dpr_ppm) or dpr_ppm <= 0:
            raise W8UDesktopError(E_DPR, "dpr_ppm must be a positive integer", rc=RC_REFUSED)

        record = registry.resolve(handle)
        if record.closed or record.handle != handle:
            raise StaleHandleError("target handle is closed or stale")

        if modality_policy == "atspi":
            if atspi_capability is None:
                raise ModalityError("missing mandatory AT-SPI converter capability for atspi policy")
        elif modality_policy == "vision_only":
            if atspi_capability is not None:
                raise ModalityError("vision_only policy must not attach an AT-SPI semantic capability")

        self.registry = registry
        self.handle = handle
        self.identity = identity
        self.run_id = run_id
        self.lease_id = lease_id
        self.modality_policy = modality_policy
        self.atspi_capability = atspi_capability
        self.dpr_ppm = dpr_ppm
        self.clock = clock if clock is not None else registry._clock
        if not callable(self.clock):
            raise RegistryError("clock must be callable")

        self.pipe = InMemoryExecPipe(reserved_capacity=reserved_capacity)
        self.pipe.open_once({
            "contract_revision": W8U_OS_CONTRACT_REVISION,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "canonical_id": self.handle.canonical_id,
            "generation": self.handle.generation,
            "epoch": record.epoch,
            "reserved_capacity": reserved_capacity,
        })

        self.authority = ActionAuthorityContext(
            run_id=self.run_id,
            lease_id=self.lease_id,
            handle=self.handle,
            epoch=record.epoch,
            authority_secret=authority_secret,
        )
        self.action_gate = W8UActionGate(
            registry=self.registry,
            authority=self.authority,
            clock=self.clock,
        )

        self._observations: list[Observation] = []
        self._descriptors: list[dict[str, Any]] = []
        self._results: list[dict[str, Any]] = []
        self._frame_metas: list[dict[str, Any]] = []
        self._t2_state_digests: list[str] = []
        self._final_state_sha256: str | None = None
        self._simulation_state_sha256: str | None = None
        self.pair_mismatches = 0
        self.frame_gaps = 0
        self.frames_dropped = 0
        self.ram_purged = False
        self._expected_seq = 0

    @property
    def semantic_modality(self) -> str | None:
        return "atspi" if self.modality_policy == "atspi" else None

    def _verify_live_target(self, expected_epoch: int) -> TargetRecord:
        try:
            record = self.registry.resolve(self.handle)
        except StaleHandleError as exc:
            raise ForeignTargetError(str(exc), code=E_STALE_GENERATION) from exc
        if record.closed or record.handle != self.handle:
            raise ForeignTargetError("stale or superseded target generation", code=E_STALE_GENERATION)
        if not _is_exact_int(expected_epoch) or expected_epoch != record.epoch:
            raise W8UDesktopError(E_EPOCH, f"evidence epoch {expected_epoch!r} != live epoch {record.epoch}", rc=RC_REFUSED)
        return record

    def observe_pair(
        self,
        evidence: InjectedPairEvidence,
        *,
        override_state: dict[str, Any] | None = None,
        simulation_state_sha256: str | None = None,
    ) -> tuple[Observation, dict[str, Any], dict[str, Any]]:
        """Validate injected held-pair evidence, convert/validate STATE, admit FrameRef & Observation.

        Returns `(observation, target_descriptor_envelope, frame_metadata)`.
        """
        self.pipe.ensure_active()
        if not isinstance(evidence, InjectedPairEvidence):
            raise ProtocolError("evidence must be InjectedPairEvidence")
        if evidence.contract_revision != W8U_OS_CONTRACT_REVISION:
            raise ProtocolError(f"unpinned contract_revision: {evidence.contract_revision!r}")

        # Verify target binding, private X identity, generation, and epoch
        if (
            evidence.run_id != self.run_id
            or evidence.lease_id != self.lease_id
            or not isinstance(evidence.handle, Handle)
            or evidence.handle.kind != self.handle.kind
            or evidence.handle.canonical_id != self.handle.canonical_id
            or evidence.display != self.identity.display
            or evidence.window_id != self.identity.window_id
            or evidence.auth_token != self.identity.auth_token
        ):
            raise ForeignTargetError("injected frame evidence belongs to a foreign target, window, or display")

        if evidence.handle.generation != self.handle.generation:
            raise ForeignTargetError("injected frame evidence has stale generation", code=E_STALE_GENERATION)

        record = self._verify_live_target(evidence.epoch)

        # Validate capture_mono_ns and tick witness rules
        if not _is_exact_int(evidence.capture_mono_ns) or evidence.capture_mono_ns <= 0:
            raise ProtocolError("capture_mono_ns must be a positive int")
        if not _is_exact_bool(evidence.tick_valid):
            raise TickInventedError("tick_valid must be bool")
        if evidence.tick_valid:
            if (
                not _is_exact_int(evidence.tick)
                or evidence.tick < 0
                or evidence.tick_witness is None
                or evidence.tick_witness.tick != evidence.tick
                or evidence.tick == evidence.capture_mono_ns
            ):
                raise TickInventedError("tick is missing authenticated witness or invented from timestamp")
            committed_tick: int | None = evidence.tick
        else:
            if evidence.tick is not None:
                raise TickInventedError("tick invalid means null, not committed zero or integer")
            committed_tick = None

        # Verify same-grab held pair XWD equality BEFORE consuming credit or admitting
        try:
            xwd_info = verify_same_grab_pair(evidence.grab_a, evidence.grab_b)
        except PairMismatchError:
            self.pair_mismatches += 1
            self.frames_dropped += 1
            raise

        # Check sequence gap & credit flow control
        if not _is_exact_int(evidence.seq) or evidence.seq < 0:
            raise FrameValidationError("seq must be a non-negative integer")
        if evidence.seq > self._expected_seq:
            self.frame_gaps += evidence.seq - self._expected_seq
        self.pipe.credit.consume_for_frame(evidence.seq)
        self._expected_seq = evidence.seq + 1

        # Build or validate STATE according to modality_policy
        if override_state is not None:
            _check_no_float_or_perception_wrapper(override_state)
            errs = schema.validate_state(override_state)
            if errs:
                raise ModalityError("; ".join(errs))
            if self.modality_policy == "atspi":
                if override_state["source"]["kind"] != "atspi" or len(override_state["nodes"]) == 0:
                    raise ModalityError("atspi policy requires non-empty atspi STATE")
            else:
                if override_state["source"]["kind"] != "vision" or len(override_state["nodes"]) != 0:
                    raise ModalityError("vision_only policy requires source.kind == 'vision' and empty semantic nodes")
            state = copy.deepcopy(override_state)
        elif self.modality_policy == "atspi":
            if self.atspi_capability is None:
                raise ModalityError("AT-SPI capability missing")
            state = self.atspi_capability.convert(
                expected_identity=self.identity,
                frame_width=xwd_info.width,
                frame_height=xwd_info.height,
            )
        else:
            state = build_vision_only_state(
                title=f"vision:{self.identity.window_id}",
                width=xwd_info.width,
                height=xwd_info.height,
            )

        # Convert canonical RGB24 bytes into H x W x 3 uint8 array and admit into SessionRegistry
        rgb_arr = np.frombuffer(xwd_info.rgb_bytes, dtype=np.uint8).reshape(
            (xwd_info.height, xwd_info.width, 3)
        )
        frame_ref = self.registry.admit_frame(self.handle, evidence.seq, rgb_arr, epoch=record.epoch)
        obs = self.registry.admit_observation(
            self.handle,
            frame_ref,
            state=state,
            dpr_ppm=self.dpr_ppm,
        )
        descriptor = schema.build_target_descriptor(
            self.handle,
            record.epoch,
            copy.deepcopy(obs.state),
            frame_seq=evidence.seq,
            dpr_ppm=self.dpr_ppm,
        )

        _, binding_sha256 = build_frame_binding(
            contract_revision=evidence.contract_revision,
            run_id=self.run_id,
            lease_id=self.lease_id,
            handle=self.handle,
            epoch=record.epoch,
            display=self.identity.display,
            window_id=self.identity.window_id,
            seq=evidence.seq,
            width=xwd_info.width,
            height=xwd_info.height,
            stride=xwd_info.stride,
            frame_sha256=frame_ref.digest,
        )

        frame_meta = {
            "seq": evidence.seq,
            "tick": committed_tick,
            "tick_valid": evidence.tick_valid,
            "capture_mono_ns": evidence.capture_mono_ns,
            "width": xwd_info.width,
            "height": xwd_info.height,
            "stride": xwd_info.stride,
            "pixel_format": PIXEL_FORMAT_RGB24,
            "sha256": frame_ref.digest,
            "binding_sha256": binding_sha256,
        }
        meta_errs = validate_frame_metadata(frame_meta)
        if meta_errs:
            raise ProtocolError("; ".join(meta_errs))

        st_sha = state_digest(obs.state)
        self._final_state_sha256 = st_sha
        if self.modality_policy == "atspi":
            self._t2_state_digests.append(st_sha)
        if simulation_state_sha256 is not None:
            if not _is_hex64(simulation_state_sha256):
                raise W8UDesktopError(E_SCHEMA, "simulation_state_sha256 must be 64 hex", rc=RC_SCHEMA)
            self._simulation_state_sha256 = simulation_state_sha256

        result_rec = build_observation_result(
            run_id=self.run_id,
            lease_id=self.lease_id,
            handle=self.handle,
            epoch=record.epoch,
            frame_seq=evidence.seq,
            dpr_ppm=self.dpr_ppm,
            modality_policy=self.modality_policy,
            semantic_modality=self.semantic_modality,
            state_sha256=st_sha,
            frame_sha256=frame_ref.digest,
            binding_sha256=binding_sha256,
        )

        self._observations.append(obs)
        self._descriptors.append(copy.deepcopy(descriptor))
        self._results.append(copy.deepcopy(result_rec))
        self._frame_metas.append(copy.deepcopy(frame_meta))
        self.action_gate.update_observed_frame(frame_ref)

        return obs, copy.deepcopy(descriptor), copy.deepcopy(frame_meta)

    def ack_frame(self, seq: int, *, grant: int = 1) -> int:
        return self.pipe.ack(seq, grant=grant)

    def purge_ram_and_close(self, *, stop: bool = False, end_error_code: int = END_OK) -> dict[str, Any]:
        """Complete stream lifecycle (STOP/END, EOF, joined exit), purge RAM references, and build receipt."""
        if stop and not self.pipe.stopped:
            self.pipe.send_stop("adapter_stop")
        if not self.pipe.ended:
            self.pipe.send_end(end_error_code, frames_captured=len(self._frame_metas), reason="closed")
        if not self.pipe.eof:
            self.pipe.close_eof()
        exit_code = self.pipe.join()
        self._observations.clear()
        self.ram_purged = True
        return self.build_receipt(reader_exit_code=exit_code)

    def build_receipt(self, *, reader_exit_code: int = 0, include_schema: bool = False) -> dict[str, Any]:
        """Build canonical `w8u.receipt/v1` dictionary."""
        record = self.registry.resolve(self.handle) if self.handle in self.registry._active else None
        epoch = record.epoch if record is not None else self.authority.epoch

        frames_captured = len(self._frame_metas)
        first_frame = copy.deepcopy(self._frame_metas[0]) if frames_captured > 0 else None
        last_frame = copy.deepcopy(self._frame_metas[-1]) if frames_captured > 0 else None

        action_log_sha256 = canonical_digest("action_log", {"actions": self.action_gate.action_log})

        if self.modality_policy == "vision_only":
            t2_trace_sha256 = None
            t2_terminal_sha256 = None
            t2_ticks = None
        else:
            if self._t2_state_digests:
                t2_trace_sha256 = canonical_digest("t2_trace", {"states": self._t2_state_digests})
                t2_terminal_sha256 = self._t2_state_digests[-1]
                t2_ticks = len(self._t2_state_digests)
            else:
                t2_trace_sha256 = None
                t2_terminal_sha256 = None
                t2_ticks = 0

        if frames_captured == 0:
            verdict = "INDETERMINATE"
            gate_code = E_PROTOCOL
        elif (
            self.pair_mismatches > 0
            or self.frame_gaps > 0
            or reader_exit_code != 0
            or not self.pipe.joined
            or self.action_gate.uncertain_count > 0
        ):
            verdict = "FAIL"
            gate_code = (
                E_PAIR_MISMATCH
                if self.pair_mismatches > 0
                else (E_UNCERTAIN if self.action_gate.uncertain_count > 0 else E_PROTOCOL)
            )
        else:
            verdict = "PASS"
            gate_code = "OK"

        receipt: dict[str, Any] = {}
        if include_schema:
            receipt["schema"] = W8U_RECEIPT_SCHEMA
        receipt.update({
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "target_handle": {
                "kind": self.handle.kind,
                "canonical_id": self.handle.canonical_id,
                "generation": self.handle.generation,
            },
            "epoch": epoch,
            "frames_root": FRAMES_ROOT,
            "frame_run_root": f"{FRAMES_ROOT}/{FRAME_RUN_SUBPATH}/{self.run_id}",
            "source": RECEIPT_SOURCE,
            "frames_captured": frames_captured,
            "first_frame": first_frame,
            "last_frame": last_frame,
            "ui_actions_count": len(self.action_gate.action_log),
            "admitted_actions": self.action_gate.admitted_count,
            "refused_actions": self.action_gate.refused_count,
            "uncertain_actions": self.action_gate.uncertain_count,
            "action_log_sha256": action_log_sha256,
            "t2_trace_sha256": t2_trace_sha256,
            "t2_terminal_sha256": t2_terminal_sha256,
            "t2_ticks": t2_ticks,
            "final_state_sha256": self._final_state_sha256,
            "simulation_state_sha256": self._simulation_state_sha256,
            "ui_gate_verdict": verdict,
            "ui_gate_code": gate_code,
            "pair_mismatches": self.pair_mismatches,
            "frame_gaps": self.frame_gaps,
            "frames_dropped": self.frames_dropped,
            "x11_tcp_listeners": self.identity.x11_tcp_listeners,
            "x11_unix_listeners": self.identity.x11_unix_listeners,
            "x11_abstract_listeners": self.identity.x11_abstract_listeners,
            "reader_exit_code": reader_exit_code,
            "reader_quiescent": self.pipe.joined,
            "ram_purged": self.ram_purged,
            "frames_disk_bytes": 0,
            "swap_delta_bytes": 0,
            "metadata_only": True,
        })
        return receipt
