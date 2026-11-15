# @module {"id":"wa.perceive.w8u_electron","layer":2,"owner":"PERCEIVE","deps":["wa.state.schema","wa.perceive.cdp_pipe","wa.perceive.ax","wa.perceive.session_registry","wa.perceive.browser"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.w8u_electron - capsule-local finite CDP broker adapter, closed input channel,
pinned OS frame-pair validator, credit window controller, and w8u.receipt/v1 builder.

Operates entirely offline in RAM over injected fd3/fd4 NUL-delimited JSON pipes without launching
a host browser, opening a socket, or writing frames to disk.
"""

from __future__ import annotations

import builtins
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
import pathlib
import re
import struct
import tempfile
import threading
from typing import Callable

import numpy as np

from wender.perceive import ax
from wender.perceive.cdp_pipe import CdpError, CdpPipe, EVENT_BUFFER, frame as cdp_frame
from wender.perceive.session_registry import (
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    RegistryError,
    SessionRegistry,
    StaleHandleError,
)
from wender.state import schema
from wender.state.schema import StateError

AUSENTE = "AUSENTE"

# Frozen schemas and domain constants
OS_CONTRACT_ID = "w8u-os-contract"
OS_CONTRACT_REVISION = "w8u-os-contract/v1"
BINDING_SCHEMA = "w8u.binding/v1"
RESULT_SCHEMA = "w8u.result/v1"
ACTION_SCHEMA = "w8u.action/v1"
ACTION_RESPONSE_SCHEMA = "w8u.action.response/v1"
RECEIPT_SCHEMA = "w8u.receipt/v1"
STATE_SCHEMA = schema.SCHEMA_ID

FRAMES_ROOT = "/dev/shm/vqa-ramws"
FRAME_RUN_SUBDIR = "opensuperficies/os-cu-0"
FRAME_SOURCE = "xvfb-grab-held-pair"
PIXEL_FORMAT_RGB24 = "rgb24"

# Holdings Electron hooks and finite semantic broker APIs are absent in Wave 1
HOLDINGS_HOOK_METHODS = AUSENTE
HOLDINGS_RUNNER = AUSENTE
FINITE_SEMANTIC_BROKER_API = AUSENTE

# Control wire magic, kinds, and END error table
CONTROL_MAGIC = b"W8UC"
CONTROL_KINDS = ("HELLO", "ACK", "FRAME", "STOP", "END", "EOF")
CONTROL_BODY_KEYS = {
    "HELLO": ("contract_id", "contract_revision", "reserved_capacity", "run_id"),
    "ACK": ("grant", "seq"),
    "FRAME": ("binding_sha256", "capture_mono_ns", "height", "pixel_format", "seq", "sha256", "stride", "tick", "tick_valid", "width"),
    "STOP": ("epoch", "reason"),
    "END": ("code", "error_code", "final_seq"),
    "EOF": ("final_seq",),
}
END_NUMERIC_ERRORS = {
    "OK": 0,
    "E_SCHEMA": 2,
    "E_MODALITY": 4,
    "E_CREDIT": 5,
    "E_PAIR_MISMATCH": 5,
    "E_STALE": 5,
    "E_PROTOCOL": 5,
    "E_SECURITY": 5,
    "E_UNCERTAIN": 6,
}
DIGEST_DOMAINS = {
    "binding": b"w8u.binding/v1\x00",
    "result": b"w8u.result/v1\x00",
    "frame": b"w8u.frame/v1\x00",
    "action": b"w8u.action/v1\x00",
    "outcome": b"w8u.outcome/v1\x00",
    "receipt": b"w8u.receipt/v1\x00",
    "state": b"wender.state/v1\x00",
}

# Target limits (marked [TARGET], never host measurements)
MAX_DEFRAMER_BUFFER_BYTES = 262144  # [TARGET] 256 KiB total pending deframer buffer
MAX_CDP_MESSAGE_BYTES = 65536       # [TARGET] 64 KiB single CDP message ceiling
MAX_ACTION_BODY_BYTES = 65536       # [TARGET] 64 KiB canonical UTF-8 action body cap
MAX_TEXT_BYTES = 4096               # [TARGET] 4096-byte UTF-8 text cap
MAX_WAIT_MS = 5000                  # [TARGET] 0..5000 ms wait cap
MAX_SCROLL_DELTA = 120              # [TARGET] -120..120 scroll delta cap
PREFERRED_INITIAL_CREDITS = 2       # [TARGET] initial ACK seq 0 preferred grant

ALLOWED_CDP_METHODS = frozenset({
    "Accessibility.enable",
    "Accessibility.getFullAXTree",
    "Accessibility.disable",
    "DOM.getDocument",
    "DOM.getBoxModel",
    "Page.getLayoutMetrics",
    "Target.getTargetInfo",
})

FORBIDDEN_CDP_METHODS = frozenset({
    "Runtime.evaluate",
    "Runtime.callFunctionOn",
    "Runtime.compileScript",
    "Runtime.runScript",
    "Page.addScriptToEvaluateOnNewDocument",
    "Inspector.enable",
    "Browser.close",
})

ACTION_KEYS = (
    "action_id",
    "action_seq",
    "args",
    "deadline_ns",
    "epoch",
    "generation",
    "grant_sha256",
    "lease_id",
    "observed_frame_seq",
    "observed_frame_sha256",
    "run_id",
    "schema",
    "verb",
)

ACTION_RESPONSE_KEYS = (
    "action_id",
    "action_seq",
    "applied_tick",
    "code",
    "emitted",
    "epoch",
    "lease_id",
    "outcome_sha256",
    "result_known",
    "schema",
    "status",
)

ACTION_STATUSES = ("refused", "admitted", "applied", "uncertain")
ACTION_CODES = (
    "OK_ADMITTED",
    "E_SCHEMA",
    "E_VERB",
    "E_LOCK",
    "E_EPOCH",
    "E_STALE_FRAME",
    "E_FOCUS",
    "E_BUDGET",
    "E_DEADLINE",
    "E_UNCERTAIN",
)

RC_VALID = 0
RC_SCHEMA = 2
RC_UNAVAILABLE = 4
RC_REFUSED = 5
RC_UNCERTAIN = 6

ALLOWED_VERBS = ("pointer", "key", "text", "wait")
POINTER_OPERATIONS = ("move", "click", "down", "up", "scroll")
POINTER_BUTTONS = (1, 2, 3)
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
MODIFIER_ORDER = ("Shift", "Control", "Alt", "Super")

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

RECEIPT_HANDLE_KEYS = ("kind", "canonical_id", "generation")
RECEIPT_FIELD_KEYS = (
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

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


# @anchor:wa.perceive.w8u_electron.W8UElectronError
class W8UElectronError(CdpError):
    """Structured error carrying a frozen W8U error code and numeric return code."""

    def __init__(self, code: str, message: str, *, rc: int = RC_REFUSED) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        self.rc = rc


def _is_exact_int(val: object) -> bool:
    return type(val) is int


def _require_exact_int(val: object, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if type(val) is not int:
        raise W8UElectronError("E_SCHEMA", f"{name} must be an exact int, got {type(val).__name__}", rc=RC_SCHEMA)
    if minimum is not None and val < minimum:
        raise W8UElectronError("E_SCHEMA", f"{name} must be >= {minimum}, got {val}", rc=RC_SCHEMA)
    if maximum is not None and val > maximum:
        raise W8UElectronError("E_SCHEMA", f"{name} must be <= {maximum}, got {val}", rc=RC_SCHEMA)
    return val


def _require_nonempty_str(val: object, name: str) -> str:
    if not isinstance(val, str) or not val or val != val.strip() or "\x00" in val:
        raise W8UElectronError("E_SCHEMA", f"{name} must be a non-empty canonical string without NUL", rc=RC_SCHEMA)
    try:
        val.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise W8UElectronError("E_SCHEMA", f"{name} is not valid UTF-8: {exc}", rc=RC_SCHEMA) from exc
    return val


def _require_hex64(val: object, name: str) -> str:
    if not isinstance(val, str) or not _HEX64_RE.fullmatch(val):
        raise W8UElectronError("E_SCHEMA", f"{name} must be 64 lowercase hex chars", rc=RC_SCHEMA)
    return val


def _reject_float_and_non_canonical(obj: object, path: str = "$") -> None:
    if isinstance(obj, float):
        raise W8UElectronError("E_SCHEMA", f"float rejected at {path}", rc=RC_SCHEMA)
    if isinstance(obj, str):
        if "\x00" in obj:
            raise W8UElectronError("E_SCHEMA", f"embedded NUL rejected at {path}", rc=RC_SCHEMA)
        try:
            obj.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UElectronError("E_SCHEMA", f"invalid UTF-8 surrogate at {path}: {exc}", rc=RC_SCHEMA) from exc
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or "\x00" in k:
                raise W8UElectronError("E_SCHEMA", f"invalid key at {path}", rc=RC_SCHEMA)
            _reject_float_and_non_canonical(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _reject_float_and_non_canonical(item, f"{path}[{idx}]")


# @anchor:wa.perceive.w8u_electron.loads_strict
def loads_strict(raw: bytes | str, *, max_bytes: int = MAX_ACTION_BODY_BYTES) -> dict:
    """Parse canonical UTF-8 JSON object rejecting duplicate keys, floats, NaN, NUL, and trailing bytes."""
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise W8UElectronError("E_SCHEMA", f"payload size {len(raw)} exceeds cap {max_bytes}", rc=RC_SCHEMA)
        if b"\x00" in raw:
            raise W8UElectronError("E_SCHEMA", "embedded NUL byte in JSON payload", rc=RC_SCHEMA)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise W8UElectronError("E_SCHEMA", f"invalid UTF-8 in payload: {exc}", rc=RC_SCHEMA) from exc
    elif isinstance(raw, str):
        if "\x00" in raw:
            raise W8UElectronError("E_SCHEMA", "embedded NUL character in JSON payload", rc=RC_SCHEMA)
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UElectronError("E_SCHEMA", f"invalid UTF-8 in payload: {exc}", rc=RC_SCHEMA) from exc
        if len(encoded) > max_bytes:
            raise W8UElectronError("E_SCHEMA", f"payload size {len(encoded)} exceeds cap {max_bytes}", rc=RC_SCHEMA)
        text = raw
    else:
        raise W8UElectronError("E_SCHEMA", f"expected bytes or str, got {type(raw).__name__}", rc=RC_SCHEMA)

    if not text or not text.strip():
        raise W8UElectronError("E_SCHEMA", "empty JSON payload", rc=RC_SCHEMA)
    if text != text.strip():
        raise W8UElectronError("E_SCHEMA", "leading or trailing whitespace/data not allowed", rc=RC_SCHEMA)

    def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
        out = {}
        for k, v in pairs:
            if not isinstance(k, str) or "\x00" in k:
                raise W8UElectronError("E_SCHEMA", "invalid JSON key", rc=RC_SCHEMA)
            if k in out:
                raise W8UElectronError("E_SCHEMA", f"duplicate JSON key: {k!r}", rc=RC_SCHEMA)
            out[k] = v
        return out

    def _reject_float(val_str: str) -> None:
        raise W8UElectronError("E_SCHEMA", f"float literal rejected: {val_str}", rc=RC_SCHEMA)

    def _reject_const(val_str: str) -> None:
        raise W8UElectronError("E_SCHEMA", f"non-finite constant rejected: {val_str}", rc=RC_SCHEMA)

    decoder = json.JSONDecoder(
        object_pairs_hook=_no_dup_pairs,
        parse_float=_reject_float,
        parse_constant=_reject_const,
    )
    try:
        obj, end_idx = decoder.raw_decode(text, 0)
    except W8UElectronError:
        raise
    except Exception as exc:
        raise W8UElectronError("E_SCHEMA", f"malformed JSON: {exc}", rc=RC_SCHEMA) from exc

    if end_idx != len(text):
        raise W8UElectronError("E_SCHEMA", "trailing data after root JSON value", rc=RC_SCHEMA)
    if not isinstance(obj, dict):
        raise W8UElectronError("E_SCHEMA", "root JSON value must be an object", rc=RC_SCHEMA)
    _reject_float_and_non_canonical(obj)
    return obj


# @anchor:wa.perceive.w8u_electron.dumps_canonical
def dumps_canonical(obj: object) -> str:
    """Serialize object to canonical UTF-8 JSON without LF using schema.dumps."""
    _reject_float_and_non_canonical(obj)
    try:
        out = schema.dumps(obj)
    except StateError as exc:
        raise W8UElectronError("E_SCHEMA", str(exc), rc=RC_SCHEMA) from exc
    if "\n" in out or "\r" in out or "\x00" in out:
        raise W8UElectronError("E_SCHEMA", "canonical JSON must not contain raw LF, CR, or NUL", rc=RC_SCHEMA)
    return out


# @anchor:wa.perceive.w8u_electron.domain_sha256
def domain_sha256(domain: str, payload: bytes | str | dict) -> str:
    """Compute domain-separated or canonical SHA-256 hex digest."""
    if domain not in DIGEST_DOMAINS:
        raise W8UElectronError("E_SCHEMA", f"unknown digest domain: {domain!r}", rc=RC_SCHEMA)
    if isinstance(payload, dict):
        raw = dumps_canonical(payload).encode("utf-8")
    elif isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise W8UElectronError("E_SCHEMA", f"unsupported payload type for digest: {type(payload).__name__}", rc=RC_SCHEMA)
    return hashlib.sha256(DIGEST_DOMAINS[domain] + raw).hexdigest()


# @anchor:wa.perceive.w8u_electron.state_digest
def state_digest(state: dict) -> str:
    """Validate a wender.state/v1 dict and return SHA-256 of its UTF-8 schema.dumps without LF."""
    if not isinstance(state, dict):
        raise W8UElectronError("E_SCHEMA", "state must be a dict", rc=RC_SCHEMA)
    if "_perception" in state:
        raise W8UElectronError("E_SCHEMA", "_perception wrapper is forbidden inside STATE", rc=RC_SCHEMA)
    errs = schema.validate_state(state)
    if errs:
        raise W8UElectronError("E_SCHEMA", f"validate_state failed: {'; '.join(errs)}", rc=RC_SCHEMA)
    raw = schema.dumps(state)
    if "\n" in raw:
        raise W8UElectronError("E_SCHEMA", "schema.dumps output must not contain LF", rc=RC_SCHEMA)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# @anchor:wa.perceive.w8u_electron.BoundedDeframer
class BoundedDeframer:
    """Strict bounded NUL-delimited JSON deframer for fd3/fd4 CDP pipe streams.

    Unlike cdp_pipe.Deframer, enforces buffer size bounds, message size bounds,
    non-empty frame checks, strict UTF-8, duplicate key rejection, and float rejection.
    """

    def __init__(
        self,
        *,
        max_buffer_bytes: int = MAX_DEFRAMER_BUFFER_BYTES,
        max_message_bytes: int = MAX_CDP_MESSAGE_BYTES,
    ) -> None:
        _require_exact_int(max_buffer_bytes, "max_buffer_bytes", minimum=64)
        _require_exact_int(max_message_bytes, "max_message_bytes", minimum=64)
        self.max_buffer_bytes = max_buffer_bytes
        self.max_message_bytes = max_message_bytes
        self._buf = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[dict]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise W8UElectronError("E_SCHEMA", "BoundedDeframer.feed requires bytes-like data", rc=RC_SCHEMA)
        raw = bytes(data)
        if not raw:
            return []
        if len(self._buf) + len(raw) > self.max_buffer_bytes:
            self._buf.clear()
            raise W8UElectronError("E_SCHEMA", "deframer buffer bound exceeded", rc=RC_SCHEMA)
        self._buf.extend(raw)
        out: list[dict] = []
        while True:
            nul_pos = self._buf.find(b"\x00")
            if nul_pos < 0:
                if len(self._buf) > self.max_message_bytes:
                    self._buf.clear()
                    raise W8UElectronError("E_SCHEMA", "unterminated frame exceeds max_message_bytes", rc=RC_SCHEMA)
                return out
            chunk = bytes(self._buf[:nul_pos])
            del self._buf[: nul_pos + 1]
            if not chunk:
                raise W8UElectronError("E_SCHEMA", "empty NUL-delimited frame rejected", rc=RC_SCHEMA)
            if len(chunk) > self.max_message_bytes:
                raise W8UElectronError("E_SCHEMA", "NUL-delimited frame exceeds max_message_bytes", rc=RC_SCHEMA)
            msg = loads_strict(chunk, max_bytes=self.max_message_bytes)
            out.append(msg)

    def finish_eof(self) -> None:
        """Verify no partial/unterminated frame remains buffered at stream EOF."""
        if self._buf:
            self._buf.clear()
            raise W8UElectronError("E_SCHEMA", "truncated non-NUL-terminated bytes at EOF", rc=RC_SCHEMA)


# @anchor:wa.perceive.w8u_electron.bind_cdp_pipe_injected
def bind_cdp_pipe_injected(
    cmd_w_fd: int,
    rep_r_fd: int,
    *,
    timeout: float = 5.0,
    clock: Callable[[], float] | None = None,
) -> CdpPipe:
    """Bind a CdpPipe instance to injected fd3/fd4 file descriptors without calling start().

    Disables start() and evaluate() so no host browser subprocess or arbitrary JS can run.
    """
    _require_exact_int(cmd_w_fd, "cmd_w_fd", minimum=0)
    _require_exact_int(rep_r_fd, "rep_r_fd", minimum=0)
    pipe = CdpPipe(chrome_bin="injected-fd3-fd4-capsule", headless=True, timeout=timeout, clock=clock)

    def _forbidden_start(*_a: object, **_kw: object) -> None:
        raise W8UElectronError("E_SECURITY", "CdpPipe.start() is forbidden on injected capsule pipe", rc=RC_REFUSED)

    def _forbidden_evaluate(*_a: object, **_kw: object) -> None:
        raise W8UElectronError("E_VERB", "CdpPipe.evaluate() is forbidden on injected capsule pipe", rc=RC_REFUSED)

    pipe.start = _forbidden_start  # type: ignore[method-assign]
    pipe.evaluate = _forbidden_evaluate  # type: ignore[method-assign]
    pipe._w = cmd_w_fd
    pipe._r = rep_r_fd
    pipe._closed = False
    reader = threading.Thread(target=pipe._read_loop, name="w8u-injected-cdp-reader", daemon=True)
    reader.start()
    pipe._reader = reader
    return pipe


# @anchor:wa.perceive.w8u_electron.InMemoryPipe
class InMemoryPipe:
    """Deterministic in-memory byte pipe emulating fd3/fd4 without OS sockets or subprocesses."""

    def __init__(self, *, max_bytes: int = MAX_DEFRAMER_BUFFER_BYTES) -> None:
        self._lock = threading.Condition()
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._max_bytes = max_bytes
        self._closed = False
        self.write_count = 0
        self.read_count = 0
        self.reopen_attempts = 0

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def reopen(self) -> None:
        with self._lock:
            self.reopen_attempts += 1
        raise W8UElectronError("E_PROTOCOL", "reopening pipe per frame is forbidden", rc=RC_REFUSED)

    def write(self, data: bytes) -> int:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise W8UElectronError("E_SCHEMA", "pipe write requires bytes", rc=RC_SCHEMA)
        raw = bytes(data)
        with self._lock:
            if self._closed:
                raise W8UElectronError("E_MODALITY", "write to closed in-memory pipe", rc=RC_UNAVAILABLE)
            if self._buffered + len(raw) > self._max_bytes:
                raise W8UElectronError("E_SCHEMA", "in-memory pipe buffer capacity exceeded", rc=RC_SCHEMA)
            if raw:
                self._chunks.append(raw)
                self._buffered += len(raw)
            self.write_count += 1
            self._lock.notify_all()
            return len(raw)

    def read(self, max_len: int = 65536, *, timeout: float = 0.0) -> bytes:
        with self._lock:
            if not self._chunks and not self._closed and timeout > 0:
                self._lock.wait(timeout)
            if not self._chunks:
                return b""
            chunk = self._chunks.popleft()
            self._buffered -= len(chunk)
            if len(chunk) > max_len:
                head, tail = chunk[:max_len], chunk[max_len:]
                self._chunks.appendleft(tail)
                self._buffered += len(tail)
                chunk = head
            self.read_count += 1
            return chunk

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._lock.notify_all()


# @anchor:wa.perceive.w8u_electron.SessionCapability
@dataclass(frozen=True)
class SessionCapability:
    """Injected session capability binding a cdp_tab handle, session_id, target_id, and epoch."""

    handle: Handle
    session_id: str
    target_id: str
    epoch: int
    dpr_ppm: int = 1000000

    def __post_init__(self) -> None:
        if not isinstance(self.handle, Handle):
            raise W8UElectronError("E_SCHEMA", "capability.handle must be a Handle", rc=RC_SCHEMA)
        if self.handle.kind != "cdp_tab":
            raise W8UElectronError("E_MODALITY", f"CDP session capability requires cdp_tab handle, got {self.handle.kind!r}", rc=RC_REFUSED)
        _require_nonempty_str(self.session_id, "session_id")
        _require_nonempty_str(self.target_id, "target_id")
        _require_exact_int(self.epoch, "epoch", minimum=0)
        _require_exact_int(self.dpr_ppm, "dpr_ppm", minimum=1)


# @anchor:wa.perceive.w8u_electron.ReviewedSemanticContract
@dataclass(frozen=True)
class ReviewedSemanticContract:
    """Injected finite semantic operation contract for Electron perception.

    Holdings hook methods and runner are explicitly AUSENTE; never invent window hooks.
    """

    contract_id: str = "w8u.electron.semantic/v1"
    allowed_methods: frozenset[str] = ALLOWED_CDP_METHODS
    holdings_hook_methods: str = AUSENTE
    holdings_runner: str = AUSENTE
    allow_arbitrary_evaluate: bool = False
    allow_regex_sanitizer: bool = False
    allow_debug_port: bool = False

    def __post_init__(self) -> None:
        _require_nonempty_str(self.contract_id, "contract_id")
        if self.holdings_hook_methods != AUSENTE or self.holdings_runner != AUSENTE:
            raise W8UElectronError(
                "E_VERB",
                "Holdings hook methods and runner are AUSENTE; inventing window hooks is forbidden",
                rc=RC_REFUSED,
            )
        if self.allow_arbitrary_evaluate:
            raise W8UElectronError("E_VERB", "arbitrary Runtime.evaluate is forbidden", rc=RC_REFUSED)
        if self.allow_regex_sanitizer:
            raise W8UElectronError("E_VERB", "regex sanitizers for JavaScript strings are forbidden", rc=RC_REFUSED)
        if self.allow_debug_port:
            raise W8UElectronError("E_VERB", "TCP/WebSocket debug ports are forbidden", rc=RC_REFUSED)
        for m in self.allowed_methods:
            if m in FORBIDDEN_CDP_METHODS or m not in ALLOWED_CDP_METHODS:
                raise W8UElectronError("E_VERB", f"method {m!r} is not in the finite allowed CDP contract", rc=RC_REFUSED)


# @anchor:wa.perceive.w8u_electron.ReviewedProvenance
@dataclass(frozen=True)
class ReviewedProvenance:
    """Reviewed provenance witness required for non-CDP app-exported trees (e.g. godot, atspi)."""

    source_kind: str
    exporter_id: str
    contract_revision: str
    witness_sha256: str

    def __post_init__(self) -> None:
        if self.source_kind not in ("atspi", "godot", "vision"):
            raise W8UElectronError(
                "E_SCHEMA",
                f"ReviewedProvenance is only valid for non-CDP kinds ('atspi', 'godot', 'vision'), got {self.source_kind!r}",
                rc=RC_REFUSED,
            )
        _require_nonempty_str(self.exporter_id, "exporter_id")
        _require_nonempty_str(self.contract_revision, "contract_revision")
        _require_hex64(self.witness_sha256, "witness_sha256")


# @anchor:wa.perceive.w8u_electron.InMemoryFiniteCdpBroker
class InMemoryFiniteCdpBroker:
    """Capsule-local finite in-memory CDP responder over fd3/fd4 NUL-delimited JSON pipes.

    Supports deterministic response generation, artificial reply fragmentation (splitting frames
    across multiple chunks), coalescing (combining multiple replies into one chunk), and fault
    injection (foreign sessionId, conflicting duplicate IDs, malformed UTF-8/NUL) for gate tests.
    """

    def __init__(
        self,
        *,
        session_id: str,
        target_id: str,
        url: str,
        title: str,
        viewport: dict,
        ax_nodes: list[dict],
        box_models: dict[int, list[int]] | None = None,
        cmd_pipe: InMemoryPipe | None = None,
        rep_pipe: InMemoryPipe | None = None,
        fragment_size: int | None = None,
        coalesce_replies: bool = False,
    ) -> None:
        _require_nonempty_str(session_id, "session_id")
        _require_nonempty_str(target_id, "target_id")
        _require_nonempty_str(url, "url")
        if not isinstance(title, str):
            raise W8UElectronError("E_SCHEMA", "title must be str", rc=RC_SCHEMA)
        if not isinstance(viewport, dict):
            raise W8UElectronError("E_SCHEMA", "viewport must be dict", rc=RC_SCHEMA)
        _reject_float_and_non_canonical(viewport, "$.viewport")
        self.session_id = session_id
        self.target_id = target_id
        self.url = url
        self.title = title
        self.viewport = dict(viewport)
        self.ax_nodes = list(ax_nodes)
        self.box_models = dict(box_models or {})
        self.cmd_pipe = cmd_pipe or InMemoryPipe()
        self.rep_pipe = rep_pipe or InMemoryPipe()
        self.fragment_size = fragment_size
        self.coalesce_replies = coalesce_replies
        self._cmd_deframer = BoundedDeframer()
        self.requests_seen: list[dict] = []
        self._fault_Queue: deque[bytes | Callable[[dict], list[bytes]]] = deque()

    def queue_raw_reply_bytes(self, raw: bytes) -> None:
        """Inject raw bytes (e.g. malformed UTF-8 or conflicting replies) onto fd4."""
        self._fault_Queue.append(raw)

    def queue_fault_handler(self, fn: Callable[[dict], list[bytes]]) -> None:
        """Inject a custom per-request fault responder."""
        self._fault_Queue.append(fn)

    def _encode_reply(self, msg: dict) -> bytes:
        return cdp_frame(msg)

    def _emit_encoded_frames(self, frames: list[bytes]) -> None:
        if not frames:
            return
        if self.coalesce_replies:
            combined = b"".join(frames)
            if self.fragment_size is not None and self.fragment_size > 0:
                for i in range(0, len(combined), self.fragment_size):
                    self.rep_pipe.write(combined[i : i + self.fragment_size])
            else:
                self.rep_pipe.write(combined)
            return
        for frm in frames:
            if self.fragment_size is not None and self.fragment_size > 0:
                for i in range(0, len(frm), self.fragment_size):
                    self.rep_pipe.write(frm[i : i + self.fragment_size])
            else:
                self.rep_pipe.write(frm)

    def process_pending(self) -> int:
        """Read all pending commands from cmd_pipe (fd3) and write replies to rep_pipe (fd4)."""
        chunks = []
        while True:
            c = self.cmd_pipe.read()
            if not c:
                break
            chunks.append(c)
        if not chunks:
            return 0
        messages = self._cmd_deframer.feed(b"".join(chunks))
        encoded_out: list[bytes] = []
        for req in messages:
            self.requests_seen.append(req)
            if self._fault_Queue:
                fault = self._fault_Queue.popleft()
                if callable(fault):
                    encoded_out.extend(fault(req))
                else:
                    encoded_out.append(fault)
                continue

            mid = req.get("id")
            method = req.get("method")
            sid = req.get("sessionId")
            params = req.get("params") or {}
            if sid != self.session_id:
                encoded_out.append(
                    self._encode_reply({
                        "id": mid,
                        "sessionId": sid,
                        "error": {"code": -32001, "message": f"foreign session {sid!r}"},
                    })
                )
                continue

            if method in ("Accessibility.enable", "Accessibility.disable", "DOM.getDocument"):
                encoded_out.append(self._encode_reply({"id": mid, "sessionId": sid, "result": {}}))
            elif method == "Accessibility.getFullAXTree":
                encoded_out.append(
                    self._encode_reply({"id": mid, "sessionId": sid, "result": {"nodes": self.ax_nodes}})
                )
            elif method == "DOM.getBoxModel":
                bid = params.get("backendNodeId")
                border = self.box_models.get(bid)
                if border is None:
                    encoded_out.append(
                        self._encode_reply({
                            "id": mid,
                            "sessionId": sid,
                            "error": {"code": -32000, "message": "Could not compute box model"},
                        })
                    )
                else:
                    encoded_out.append(
                        self._encode_reply({
                            "id": mid,
                            "sessionId": sid,
                            "result": {"model": {"border": list(border)}},
                        })
                    )
            elif method == "Target.getTargetInfo":
                encoded_out.append(
                    self._encode_reply({
                        "id": mid,
                        "sessionId": sid,
                        "result": {
                            "targetInfo": {
                                "targetId": self.target_id,
                                "type": "page",
                                "url": self.url,
                                "title": self.title,
                                "attached": True,
                            }
                        },
                    })
                )
            elif method == "Page.getLayoutMetrics":
                encoded_out.append(
                    self._encode_reply({
                        "id": mid,
                        "sessionId": sid,
                        "result": {"viewport": dict(self.viewport)},
                    })
                )
            else:
                encoded_out.append(
                    self._encode_reply({
                        "id": mid,
                        "sessionId": sid,
                        "error": {"code": -32601, "message": f"Method not allowed: {method}"},
                    })
                )
        self._emit_encoded_frames(encoded_out)
        return len(messages)


def _quad_to_box(quad: list[int]) -> list[int]:
    if not isinstance(quad, (list, tuple)) or len(quad) != 8:
        raise W8UElectronError("E_SCHEMA", "DOM box border quad must have 8 integers", rc=RC_SCHEMA)
    for c in quad:
        _require_exact_int(c, "quad_coord")
    xs = quad[0::2]
    ys = quad[1::2]
    x0 = min(xs)
    y0 = min(ys)
    return [x0, y0, max(0, max(xs) - x0), max(0, max(ys) - y0)]


# @anchor:wa.perceive.w8u_electron.ElectronCdpAdapter
class ElectronCdpAdapter:
    """Capsule-local finite CDP broker adapter over injected fd3/fd4 pipes.

    Enforces:
    - Never launches a local browser (`start` is forbidden) or opens a TCP/WebSocket debug port.
    - Refuses missing broker with `E_MODALITY` (`rc=4`) rather than returning an empty tree.
    - Refuses arbitrary `Runtime.evaluate`, JavaScript expressions, regex sanitizers, unknown
      CDP methods, and invented Holdings window hooks (`AUSENTE`).
    - Strictly correlates request/reply IDs and `sessionId`, rejecting foreign sessions,
      conflicting replies, duplicate JSON keys, floats, and malformed NUL/UTF-8 frames.
    - Binds `handle` (`cdp_tab`), `generation`, `epoch`, and `FrameRef` via `SessionRegistry`.
    """

    def __init__(
        self,
        *,
        capability: SessionCapability | None,
        registry: SessionRegistry | None,
        contract: ReviewedSemanticContract | None = None,
        broker: InMemoryFiniteCdpBroker | None = None,
        cmd_pipe: InMemoryPipe | None = None,
        rep_pipe: InMemoryPipe | None = None,
        debug_port: int | str | None = None,
        chrome_bin: str | None = None,
        launch_host: bool = False,
    ) -> None:
        if debug_port is not None:
            raise W8UElectronError("E_SECURITY", "debug ports are forbidden; use fd3/fd4 pipe only", rc=RC_REFUSED)
        if launch_host or chrome_bin is not None:
            raise W8UElectronError("E_SECURITY", "host browser launch is forbidden in capsule adapter", rc=RC_REFUSED)
        if capability is None or not isinstance(capability, SessionCapability):
            raise W8UElectronError("E_MODALITY", "missing or invalid SessionCapability", rc=RC_UNAVAILABLE)
        if registry is None or not isinstance(registry, SessionRegistry):
            raise W8UElectronError("E_MODALITY", "missing or invalid SessionRegistry", rc=RC_UNAVAILABLE)

        self.capability = capability
        self.registry = registry
        self.contract = contract or ReviewedSemanticContract()
        self.broker = broker
        self.cmd_pipe = cmd_pipe if cmd_pipe is not None else (broker.cmd_pipe if broker is not None else None)
        self.rep_pipe = rep_pipe if rep_pipe is not None else (broker.rep_pipe if broker is not None else None)
        self._deframer = BoundedDeframer()
        self._next_id = 0
        self._pending_ids: set[int] = set()
        self._completed_ids: set[int] = set()
        self._replies: dict[int, dict] = {}
        self.events: deque[dict] = deque(maxlen=EVENT_BUFFER)

    def start(self, *_args: object, **_kwargs: object) -> None:
        """Explicitly block CdpPipe.start() host browser launch."""
        raise W8UElectronError("E_SECURITY", "host browser launch via start() is forbidden", rc=RC_REFUSED)

    def evaluate(self, *_args: object, **_kwargs: object) -> None:
        """Explicitly refuse Runtime.evaluate / JavaScript execution."""
        raise W8UElectronError("E_VERB", "arbitrary Runtime.evaluate and JavaScript strings are forbidden", rc=RC_REFUSED)

    def sanitize_js_regex(self, *_args: object, **_kwargs: object) -> None:
        """Explicitly refuse regex sanitizers for JavaScript evaluation."""
        raise W8UElectronError("E_VERB", "regex sanitizers for JavaScript strings are forbidden", rc=RC_REFUSED)

    def invoke_holdings_hook(self, *_args: object, **_kwargs: object) -> None:
        """Holdings window hooks and runner are AUSENTE in Wave 1."""
        raise W8UElectronError("E_MODALITY", f"Holdings hooks are {AUSENTE}; never invent window hooks", rc=RC_UNAVAILABLE)

    def _validate_bound_session(self, explicit_session_id: str | None = None) -> tuple[Handle, int]:
        try:
            rec = self.registry.resolve(self.capability.handle)
        except (StaleHandleError, RegistryError) as exc:
            raise W8UElectronError("E_EPOCH", f"stale or closed handle in registry: {exc}", rc=RC_REFUSED) from exc

        if rec.closed:
            raise W8UElectronError("E_EPOCH", "target handle is closed", rc=RC_REFUSED)
        if rec.handle.generation != self.capability.handle.generation:
            raise W8UElectronError("E_EPOCH", "handle generation mismatch", rc=RC_REFUSED)
        if rec.epoch != self.capability.epoch:
            raise W8UElectronError("E_EPOCH", f"capability epoch {self.capability.epoch} != registry epoch {rec.epoch}", rc=RC_REFUSED)
        if rec.session_id != self.capability.session_id:
            raise W8UElectronError("E_SECURITY", "registry session_id disagrees with capability session_id", rc=RC_REFUSED)
        if explicit_session_id is not None and explicit_session_id != self.capability.session_id:
            raise W8UElectronError("E_SECURITY", f"foreign session_id {explicit_session_id!r} != {self.capability.session_id!r}", rc=RC_REFUSED)
        return rec.handle, rec.epoch

    def _validate_method_and_params(self, method: str, params: dict | None) -> dict:
        if not isinstance(method, str) or not method:
            raise W8UElectronError("E_VERB", "CDP method must be a non-empty string", rc=RC_REFUSED)
        if method in FORBIDDEN_CDP_METHODS or method.startswith("Runtime.") or "evaluate" in method.lower():
            raise W8UElectronError("E_VERB", f"forbidden CDP method: {method}", rc=RC_REFUSED)
        if method not in self.contract.allowed_methods:
            raise W8UElectronError("E_VERB", f"unknown or unreviewed CDP method: {method}", rc=RC_REFUSED)
        clean_params = {} if params is None else dict(params)
        _reject_float_and_non_canonical(clean_params, "$.params")
        for k, v in clean_params.items():
            if k in ("expression", "functionDeclaration", "scriptSource", "regexSanitizer"):
                raise W8UElectronError("E_VERB", f"JavaScript parameter {k!r} is forbidden", rc=RC_REFUSED)
            if isinstance(v, str) and ("javascript:" in v.lower() or "window." in v or "document." in v):
                raise W8UElectronError("E_VERB", f"JavaScript string content in parameter {k!r} is forbidden", rc=RC_REFUSED)
        return clean_params

    def _Pump_replies_until(self, target_id: int) -> dict:
        if self.rep_pipe is None:
            raise W8UElectronError("E_MODALITY", "missing reply pipe (E_MODALITY)", rc=RC_UNAVAILABLE)
        if self.broker is not None:
            self.broker.process_pending()
        while True:
            chunk = self.rep_pipe.read()
            if not chunk:
                break
            messages = self._deframer.feed(chunk)
            for msg in messages:
                msg_sid = msg.get("sessionId")
                if msg_sid != self.capability.session_id:
                    raise W8UElectronError(
                        "E_SECURITY",
                        f"foreign session in CDP message: {msg_sid!r} != {self.capability.session_id!r}",
                        rc=RC_REFUSED,
                    )
                if "id" in msg:
                    rid = _require_exact_int(msg["id"], "reply.id", minimum=1)
                    if rid in self._completed_ids or rid in self._replies:
                        raise W8UElectronError("E_PROTOCOL", f"conflicting duplicate reply for id={rid}", rc=RC_REFUSED)
                    if rid not in self._pending_ids:
                        raise W8UElectronError("E_PROTOCOL", f"unsolicited reply for unknown id={rid}", rc=RC_REFUSED)
                    has_result = "result" in msg
                    has_error = "error" in msg
                    if has_result == has_error:
                        raise W8UElectronError(
                            "E_PROTOCOL",
                            f"conflicting reply structure for id={rid}: must have exactly one of result or error",
                            rc=RC_REFUSED,
                        )
                    if len(self._replies) >= EVENT_BUFFER:
                        raise W8UElectronError("E_PROTOCOL", "reply correlation buffer exhausted", rc=RC_REFUSED)
                    self._replies[rid] = msg
                else:
                    if "method" not in msg:
                        raise W8UElectronError("E_PROTOCOL", "CDP message lacks both id and method", rc=RC_REFUSED)
                    self.events.append(msg)

        if target_id not in self._replies:
            self._pending_ids.discard(target_id)
            raise W8UElectronError("E_MODALITY", f"no reply from finite broker for id={target_id}", rc=RC_UNAVAILABLE)

        reply = self._replies.pop(target_id)
        self._pending_ids.discard(target_id)
        self._completed_ids.add(target_id)
        if "error" in reply:
            err_obj = reply["error"]
            err_msg = err_obj.get("message", "CDP error") if isinstance(err_obj, dict) else str(err_obj)
            raise W8UElectronError("E_PROTOCOL", f"CDP command error: {err_msg}", rc=RC_REFUSED)
        res = reply.get("result")
        if not isinstance(res, dict):
            raise W8UElectronError("E_SCHEMA", "CDP result must be an object", rc=RC_SCHEMA)
        _reject_float_and_non_canonical(res, "$.result")
        return res

    def send(self, method: str, params: dict | None = None, session_id: str | None = None, timeout: float | None = None) -> dict:
        """Send one finite allowed CDP command over fd3 and correlate its reply on fd4."""
        del timeout
        if self.cmd_pipe is None or self.rep_pipe is None or self.cmd_pipe.closed or self.rep_pipe.closed:
            raise W8UElectronError("E_MODALITY", "CDP broker pipe is missing or closed", rc=RC_UNAVAILABLE)
        self._validate_bound_session(session_id)
        clean_params = self._validate_method_and_params(method, params)
        if len(self._pending_ids) >= EVENT_BUFFER:
            raise W8UElectronError("E_PROTOCOL", "active CDP command capacity exhausted", rc=RC_REFUSED)
        self._next_id += 1
        mid = self._next_id
        self._pending_ids.add(mid)
        wire_msg = {
            "id": mid,
            "method": method,
            "params": clean_params,
            "sessionId": self.capability.session_id,
        }
        self.cmd_pipe.write(cdp_frame(wire_msg))
        return self._Pump_replies_until(mid)

    def send_pipelined(self, calls: list[tuple[str, dict | None]]) -> list[dict]:
        """Write multiple finite CDP commands onto fd3 before reading coalesced/fragmented replies on fd4."""
        if self.cmd_pipe is None or self.rep_pipe is None or self.cmd_pipe.closed or self.rep_pipe.closed:
            raise W8UElectronError("E_MODALITY", "CDP broker pipe is missing or closed", rc=RC_UNAVAILABLE)
        self._validate_bound_session(self.capability.session_id)
        mids: list[int] = []
        for method, params in calls:
            clean_params = self._validate_method_and_params(method, params)
            self._next_id += 1
            mid = self._next_id
            self._pending_ids.add(mid)
            mids.append(mid)
            self.cmd_pipe.write(
                cdp_frame({
                    "id": mid,
                    "method": method,
                    "params": clean_params,
                    "sessionId": self.capability.session_id,
                })
            )
        return [self._Pump_replies_until(mid) for mid in mids]

    def perceive(self, *, frame_ref: FrameRef, with_boxes: bool = True) -> tuple[dict, dict, Observation]:
        """Perceive genuine CDP DOM/AX STATE and bind it to an independently admitted FrameRef.

        Returns (state_dict, target_descriptor_envelope, Observation).
        Refuses with E_MODALITY when the broker is missing or returns an empty AX tree.
        """
        if self.broker is None and (self.cmd_pipe is None or self.rep_pipe is None):
            raise W8UElectronError("E_MODALITY", "missing finite CDP broker", rc=RC_UNAVAILABLE)
        handle, initial_epoch = self._validate_bound_session(self.capability.session_id)
        if not isinstance(frame_ref, FrameRef):
            raise W8UElectronError("E_SCHEMA", "frame_ref must be a validated FrameRef", rc=RC_SCHEMA)
        if (frame_ref.handle, frame_ref.epoch) != (handle, initial_epoch):
            raise W8UElectronError("E_EPOCH", "frame_ref handle/epoch does not match bound session", rc=RC_REFUSED)

        sid = self.capability.session_id
        self.send("Accessibility.enable", session_id=sid)
        failed = False
        try:
            ax_res = self.send("Accessibility.getFullAXTree", session_id=sid)
            raw_nodes = ax_res.get("nodes")
            if not isinstance(raw_nodes, list) or len(raw_nodes) == 0:
                raise W8UElectronError("E_MODALITY", "CDP broker returned empty or missing AX tree", rc=RC_UNAVAILABLE)
            pruned_nodes = ax.prune(raw_nodes)
            if not pruned_nodes:
                raise W8UElectronError("E_MODALITY", "pruned AX tree is empty; refusing silent empty tree", rc=RC_UNAVAILABLE)

            tinfo_res = self.send("Target.getTargetInfo", session_id=sid)
            tinfo = tinfo_res.get("targetInfo")
            if not isinstance(tinfo, dict) or tinfo.get("targetId") != self.capability.target_id:
                raise W8UElectronError("E_SECURITY", "Target.getTargetInfo targetId mismatch", rc=RC_REFUSED)
            url = _require_nonempty_str(tinfo.get("url"), "targetInfo.url")
            title = ax.normalize_name(tinfo.get("title") or "")

            metrics_res = self.send("Page.getLayoutMetrics", session_id=sid)
            vp = metrics_res.get("viewport")
            if not isinstance(vp, dict):
                raise W8UElectronError("E_SCHEMA", "Page.getLayoutMetrics missing viewport dict", rc=RC_SCHEMA)

            boxes_map: dict[str, list[int]] = {}
            if with_boxes:
                self.send("DOM.getDocument", {"depth": 0}, session_id=sid)
                for node in pruned_nodes:
                    bid = node.get("backend_id")
                    if bid is None:
                        continue
                    _require_exact_int(bid, "backendDOMNodeId", minimum=1)
                    try:
                        bm = self.send("DOM.getBoxModel", {"backendNodeId": bid}, session_id=sid)
                    except W8UElectronError as exc:
                        if exc.code == "E_PROTOCOL":
                            continue
                        raise
                    quad = (bm.get("model") or {}).get("border")
                    if quad is not None:
                        boxes_map[node["id"]] = _quad_to_box(quad)

            post_handle, post_epoch = self._validate_bound_session(sid)
            if post_handle != handle or post_epoch != initial_epoch:
                raise W8UElectronError("E_EPOCH", "epoch or generation changed during perception", rc=RC_REFUSED)

            state = schema.build_state(
                pruned_nodes,
                kind="cdp",
                url=url,
                title=title,
                ax_sha256=ax.snapshot_sha(pruned_nodes, True),
                ax_stable_sha256=ax.snapshot_sha(pruned_nodes, False),
                prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION},
                raw_count=len(raw_nodes),
                boxes=boxes_map,
                viewport=vp,
                actionable_roles=ax.ACTIONABLE_ROLES,
            )
            state_errs = schema.validate_state(state)
            if state_errs:
                raise W8UElectronError("E_SCHEMA", f"invalid STATE: {'; '.join(state_errs)}", rc=RC_SCHEMA)

            envelope = schema.build_target_descriptor(
                handle,
                initial_epoch,
                state,
                frame_seq=frame_ref.frame_seq,
                dpr_ppm=self.capability.dpr_ppm,
            )
            env_errs = schema.validate_target_descriptor(
                envelope,
                expected_epoch=initial_epoch,
                registry_epoch=post_epoch,
            )
            if env_errs:
                raise W8UElectronError("E_SCHEMA", f"invalid target descriptor: {'; '.join(env_errs)}", rc=RC_SCHEMA)

            try:
                obs = self.registry.admit_observation(
                    handle,
                    frame_ref,
                    state=state,
                    dpr_ppm=self.capability.dpr_ppm,
                )
            except FrameValidationError as exc:
                raise W8UElectronError("E_STALE_FRAME", str(exc), rc=RC_REFUSED) from exc

            return state, envelope, obs
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self.send("Accessibility.disable", session_id=sid)
            except BaseException:
                if not failed:
                    raise


# @anchor:wa.perceive.w8u_electron.admit_state_with_provenance
def admit_state_with_provenance(
    state: dict,
    *,
    handle: Handle,
    cdp_adapter: ElectronCdpAdapter | None = None,
    provenance: ReviewedProvenance | None = None,
) -> dict:
    """Enforce source.kind provenance: only genuine CDP DOM/AX data may use kind='cdp' and cdp_tab.

    App-exported trees ('godot', 'atspi', 'vision') require a matching ReviewedProvenance witness.
    """
    if not isinstance(state, dict):
        raise W8UElectronError("E_SCHEMA", "state must be a dict", rc=RC_SCHEMA)
    if "_perception" in state:
        raise W8UElectronError("E_SCHEMA", "_perception wrapper is forbidden", rc=RC_SCHEMA)
    errs = schema.validate_state(state)
    if errs:
        raise W8UElectronError("E_SCHEMA", f"invalid state: {'; '.join(errs)}", rc=RC_SCHEMA)
    if not isinstance(handle, Handle):
        raise W8UElectronError("E_SCHEMA", "handle must be a Handle", rc=RC_SCHEMA)

    src_kind = state["source"]["kind"]
    if src_kind == "cdp":
        if handle.kind != "cdp_tab":
            raise W8UElectronError("E_SECURITY", f"source.kind 'cdp' requires handle.kind 'cdp_tab', got {handle.kind!r}", rc=RC_REFUSED)
        if cdp_adapter is None or not isinstance(cdp_adapter, ElectronCdpAdapter):
            raise W8UElectronError("E_SECURITY", "fabricated CDP source rejected: missing genuine ElectronCdpAdapter provenance", rc=RC_REFUSED)
        if cdp_adapter.capability.handle != handle or not cdp_adapter._completed_ids:
            raise W8UElectronError("E_SECURITY", "fabricated CDP source rejected: no correlated CDP replies for handle", rc=RC_REFUSED)
        return state

    if handle.kind == "cdp_tab":
        raise W8UElectronError("E_SECURITY", f"cdp_tab handle cannot be used with non-CDP source.kind {src_kind!r}", rc=RC_REFUSED)
    if provenance is None or not isinstance(provenance, ReviewedProvenance):
        raise W8UElectronError("E_MODALITY", f"app-exported tree {src_kind!r} requires ReviewedProvenance", rc=RC_UNAVAILABLE)
    if provenance.source_kind != src_kind:
        raise W8UElectronError("E_SECURITY", f"provenance source_kind {provenance.source_kind!r} != {src_kind!r}", rc=RC_REFUSED)
    expected_witness = state_digest(state)
    if provenance.witness_sha256 != expected_witness:
        raise W8UElectronError("E_SECURITY", "app-exported tree witness_sha256 mismatch", rc=RC_REFUSED)
    return state


# @anchor:wa.perceive.w8u_electron.PinnedOsContract
@dataclass(frozen=True)
class PinnedOsContract:
    """Revision-pinned w8u-os-contract with independent accepted and rejected test vectors."""

    contract_id: str = OS_CONTRACT_ID
    revision: str = OS_CONTRACT_REVISION
    accepted_vectors: tuple[str, ...] = ()
    rejected_vectors: tuple[str, ...] = ()
    holdings_hooks: str = AUSENTE

    def __post_init__(self) -> None:
        if self.contract_id != OS_CONTRACT_ID:
            raise W8UElectronError("E_SCHEMA", f"invalid OS contract_id: {self.contract_id!r}", rc=RC_SCHEMA)
        if not isinstance(self.revision, str) or not self.revision.startswith("w8u-os-contract/"):
            raise W8UElectronError("E_SCHEMA", f"unpinned or invalid OS contract revision: {self.revision!r}", rc=RC_SCHEMA)
        if not self.accepted_vectors or not self.rejected_vectors:
            raise W8UElectronError("E_SCHEMA", "PinnedOsContract requires non-empty accepted_vectors and rejected_vectors", rc=RC_SCHEMA)
        for digest in self.accepted_vectors:
            _require_hex64(digest, "accepted_vector")
        for digest in self.rejected_vectors:
            _require_hex64(digest, "rejected_vector")
        if set(self.accepted_vectors) & set(self.rejected_vectors):
            raise W8UElectronError("E_SCHEMA", "accepted_vectors and rejected_vectors must be disjoint", rc=RC_SCHEMA)
        if self.holdings_hooks != AUSENTE:
            raise W8UElectronError("E_SCHEMA", "omitted Holdings values must remain AUSENTE", rc=RC_SCHEMA)


# @anchor:wa.perceive.w8u_electron.encode_control_frame
def encode_control_frame(kind: str, body: dict) -> bytes:
    """Encode a W8U control frame with frozen magic, kind, and canonical body schema."""
    if kind not in CONTROL_KINDS:
        raise W8UElectronError("E_SCHEMA", f"unknown control kind: {kind!r}", rc=RC_SCHEMA)
    if not isinstance(body, dict):
        raise W8UElectronError("E_SCHEMA", "control body must be a dict", rc=RC_SCHEMA)
    expected_keys = CONTROL_BODY_KEYS[kind]
    if tuple(sorted(body.keys())) != tuple(sorted(expected_keys)):
        raise W8UElectronError("E_SCHEMA", f"control {kind} keys {sorted(body.keys())} != {sorted(expected_keys)}", rc=RC_SCHEMA)
    if kind == "END":
        code_name = body["code"]
        if code_name not in END_NUMERIC_ERRORS or body["error_code"] != END_NUMERIC_ERRORS[code_name]:
            raise W8UElectronError("E_SCHEMA", f"invalid END code/error_code pair: {code_name!r}/{body['error_code']!r}", rc=RC_SCHEMA)
    envelope = {"magic": CONTROL_MAGIC.decode("ascii"), "kind": kind, "body": body}
    return dumps_canonical(envelope).encode("utf-8") + b"\x00"


# @anchor:wa.perceive.w8u_electron.decode_control_frame
def decode_control_frame(raw: bytes) -> tuple[str, dict]:
    """Decode and validate a NUL-terminated W8U control frame."""
    if not isinstance(raw, bytes) or not raw.endswith(b"\x00"):
        raise W8UElectronError("E_SCHEMA", "control frame must be NUL-terminated bytes", rc=RC_SCHEMA)
    obj = loads_strict(raw[:-1])
    if sorted(obj.keys()) != ["body", "kind", "magic"]:
        raise W8UElectronError("E_SCHEMA", "invalid control envelope keys", rc=RC_SCHEMA)
    if obj["magic"] != CONTROL_MAGIC.decode("ascii"):
        raise W8UElectronError("E_SCHEMA", f"invalid control magic: {obj['magic']!r}", rc=RC_SCHEMA)
    kind = obj["kind"]
    body = obj["body"]
    if kind not in CONTROL_KINDS or not isinstance(body, dict):
        raise W8UElectronError("E_SCHEMA", "invalid control kind or body", rc=RC_SCHEMA)
    if tuple(sorted(body.keys())) != tuple(sorted(CONTROL_BODY_KEYS[kind])):
        raise W8UElectronError("E_SCHEMA", f"invalid control body keys for {kind}", rc=RC_SCHEMA)
    if kind == "END":
        code_name = body["code"]
        if code_name not in END_NUMERIC_ERRORS or body["error_code"] != END_NUMERIC_ERRORS[code_name]:
            raise W8UElectronError("E_SCHEMA", "END numeric error code mismatch", rc=RC_SCHEMA)
    return kind, body


# @anchor:wa.perceive.w8u_electron.build_xwd_bytes
def build_xwd_bytes(rgb: np.ndarray, *, bpp: int = 24, window_name: bytes = b"w8u") -> bytes:
    """Build canonical big-endian X11 XWD v7 ZPixmap bytes in RAM for testing and validation."""
    arr = np.asarray(rgb)
    if arr.dtype != np.dtype("uint8") or arr.ndim != 3 or arr.shape[2] != 3:
        raise W8UElectronError("E_SCHEMA", "RGB array must be H x W x 3 uint8", rc=RC_SCHEMA)
    height, width, _ = arr.shape
    if bpp not in (24, 32):
        raise W8UElectronError("E_SCHEMA", "XWD bpp must be 24 or 32", rc=RC_SCHEMA)
    bytes_per_pixel = bpp // 8
    stride = width * bytes_per_pixel
    header_size = 100 + len(window_name) + 1
    header = struct.pack(
        ">25I",
        header_size,
        7,                  # file_version = 7
        2,                  # pixmap_format = ZPixmap (2)
        24,                 # pixmap_depth = 24
        width,
        height,
        0,                  # xoffset
        1,                  # byte_order = MSBFirst (1)
        32,                 # bitmap_unit
        1,                  # bitmap_bit_order
        32,                 # bitmap_pad
        bpp,                # bits_per_pixel
        stride,             # bytes_per_line
        4,                  # visual_class = TrueColor (4)
        0x00FF0000,         # red_mask
        0x0000FF00,         # green_mask
        0x000000FF,         # blue_mask
        8,                  # bits_per_rgb
        256,                # colormap_entries
        0,                  # ncolors
        width,
        height,
        0,                  # window_x
        0,                  # window_y
        0,                  # window_bdrwidth
    )
    if bpp == 24:
        pixel_bytes = arr.tobytes(order="C")
    else:
        padded = np.zeros((height, width, 4), dtype=np.uint8)
        padded[:, :, 1:4] = arr
        pixel_bytes = padded.tobytes(order="C")
    return header + window_name + b"\x00" + pixel_bytes


# @anchor:wa.perceive.w8u_electron.parse_xwd_common_subset
def parse_xwd_common_subset(xwd_bytes: bytes) -> tuple[np.ndarray, dict]:
    """Validate and extract canonical RGB array and geometry from the common XWD v7 subset."""
    if not isinstance(xwd_bytes, bytes) or len(xwd_bytes) < 100:
        raise W8UElectronError("E_SCHEMA", "XWD buffer shorter than 100-byte header", rc=RC_SCHEMA)
    fields = struct.unpack(">25I", xwd_bytes[:100])
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
        _colormap_entries,
        ncolors,
        _window_width,
        _window_height,
        _window_x,
        _window_y,
        _window_bdrwidth,
    ) = fields

    if header_size < 100 or header_size > 4096:
        raise W8UElectronError("E_SCHEMA", f"invalid XWD header_size {header_size}", rc=RC_SCHEMA)
    if file_version != 7:
        raise W8UElectronError("E_SCHEMA", f"unsupported XWD file_version {file_version}", rc=RC_SCHEMA)
    if pixmap_format != 2:
        raise W8UElectronError("E_SCHEMA", f"XWD pixmap_format must be ZPixmap (2), got {pixmap_format}", rc=RC_SCHEMA)
    if pixmap_depth not in (24, 32):
        raise W8UElectronError("E_SCHEMA", f"unsupported XWD pixmap_depth {pixmap_depth}", rc=RC_SCHEMA)
    if pixmap_width <= 0 or pixmap_height <= 0:
        raise W8UElectronError("E_SCHEMA", "XWD dimensions must be positive", rc=RC_SCHEMA)
    if xoffset != 0:
        raise W8UElectronError("E_SCHEMA", "XWD xoffset must be 0", rc=RC_SCHEMA)
    if byte_order not in (0, 1) or bitmap_bit_order not in (0, 1):
        raise W8UElectronError("E_SCHEMA", "invalid XWD byte_order or bitmap_bit_order", rc=RC_SCHEMA)
    if bitmap_unit not in (8, 16, 32) or bitmap_pad not in (8, 16, 32):
        raise W8UElectronError("E_SCHEMA", "invalid XWD bitmap_unit or bitmap_pad", rc=RC_SCHEMA)
    if bits_per_pixel not in (24, 32):
        raise W8UElectronError("E_SCHEMA", f"unsupported XWD bits_per_pixel {bits_per_pixel}", rc=RC_SCHEMA)
    min_stride = pixmap_width * (bits_per_pixel // 8)
    if bytes_per_line < min_stride:
        raise W8UElectronError("E_SCHEMA", f"XWD bytes_per_line {bytes_per_line} < {min_stride}", rc=RC_SCHEMA)
    if visual_class not in (4, 5):
        raise W8UElectronError("E_SCHEMA", f"XWD visual_class must be TrueColor/DirectColor, got {visual_class}", rc=RC_SCHEMA)
    if (red_mask, green_mask, blue_mask) != (0x00FF0000, 0x0000FF00, 0x000000FF):
        raise W8UElectronError("E_SCHEMA", "XWD channel masks must be canonical 0xff0000/0x00ff00/0x0000ff", rc=RC_SCHEMA)
    if bits_per_rgb != 8:
        raise W8UElectronError("E_SCHEMA", f"XWD bits_per_rgb must be 8, got {bits_per_rgb}", rc=RC_SCHEMA)

    pixel_offset = header_size + ncolors * 12
    expected_total = pixel_offset + bytes_per_line * pixmap_height
    if len(xwd_bytes) != expected_total:
        raise W8UElectronError("E_SCHEMA", f"XWD byte length {len(xwd_bytes)} != expected {expected_total}", rc=RC_SCHEMA)

    raw_pixels = np.frombuffer(xwd_bytes, dtype=np.uint8, offset=pixel_offset).reshape((pixmap_height, bytes_per_line))
    active_bytes = raw_pixels[:, :min_stride]
    if bits_per_pixel == 24:
        rgb = active_bytes.reshape((pixmap_height, pixmap_width, 3)).copy()
    else:
        px4 = active_bytes.reshape((pixmap_height, pixmap_width, 4))
        if byte_order == 1:
            rgb = px4[:, :, 1:4].copy()
        else:
            rgb = px4[:, :, 2::-1].copy()

    rgb.flags.writeable = False
    meta = {
        "width": int(pixmap_width),
        "height": int(pixmap_height),
        "stride": int(pixmap_width * 3),
        "pixel_format": PIXEL_FORMAT_RGB24,
    }
    return rgb, meta


# @anchor:wa.perceive.w8u_electron.validate_frame_metadata
def validate_frame_metadata(meta: dict) -> list[str]:
    """Validate exact frame metadata keys and witness/tick invariants."""
    if not isinstance(meta, dict):
        return ["frame metadata must be a dict"]
    errs: list[str] = []
    if tuple(sorted(meta.keys())) != tuple(sorted(FRAME_METADATA_KEYS)):
        errs.append(f"frame metadata keys {sorted(meta.keys())} != {sorted(FRAME_METADATA_KEYS)}")
        return errs

    if not _is_exact_int(meta["seq"]) or meta["seq"] < 0:
        errs.append("seq must be a non-negative exact int")
    if type(meta["tick_valid"]) is not bool:
        errs.append("tick_valid must be bool")
    else:
        if meta["tick_valid"]:
            if not _is_exact_int(meta["tick"]) or meta["tick"] < 0:
                errs.append("tick must be a non-negative exact int when tick_valid is True")
        else:
            if meta["tick"] is not None:
                errs.append("tick must be null (None) when tick_valid is False, never committed zero")
    if not _is_exact_int(meta["capture_mono_ns"]) or meta["capture_mono_ns"] < 0:
        errs.append("capture_mono_ns must be a non-negative exact int")
    if not _is_exact_int(meta["width"]) or meta["width"] <= 0:
        errs.append("width must be a positive exact int")
    if not _is_exact_int(meta["height"]) or meta["height"] <= 0:
        errs.append("height must be a positive exact int")
    if not _is_exact_int(meta["stride"]) or (_is_exact_int(meta["width"]) and meta["stride"] != meta["width"] * 3):
        errs.append("stride must equal width * 3 for rgb24")
    if meta["pixel_format"] != PIXEL_FORMAT_RGB24:
        errs.append(f"pixel_format must be {PIXEL_FORMAT_RGB24!r}")
    for dig_key in ("sha256", "binding_sha256"):
        if not isinstance(meta[dig_key], str) or not _HEX64_RE.fullmatch(meta[dig_key]):
            errs.append(f"{dig_key} must be 64 lowercase hex chars")
    return errs


# @anchor:wa.perceive.w8u_electron.CreditWindowController
class CreditWindowController:
    """Credit window flow controller enforcing initial ACK seq 0 grant and capacity invariants.

    - Initial ACK seq 0 grants at most min(2, reserved_capacity).
    - At all times: cumulative_credits <= acknowledged_frames + reserved_capacity.
    - Overgrant, duplicate ACK, credit inflation, or emitting on exhausted credit raises E_CREDIT.
    """

    def __init__(self, *, reserved_capacity: int) -> None:
        _require_exact_int(reserved_capacity, "reserved_capacity", minimum=1)
        self.reserved_capacity = reserved_capacity
        self.initial_initialized = False
        self.cumulative_credits = 0
        self.emitted_frames = 0
        self.acknowledged_frames = 0
        self._acked_seqs: set[int] = set()

    @property
    def max_initial_grant(self) -> int:
        return min(PREFERRED_INITIAL_CREDITS, self.reserved_capacity)

    @property
    def available_credits(self) -> int:
        return self.cumulative_credits - self.emitted_frames

    def ack(self, *, seq: int, grant: int | None = None) -> int:
        _require_exact_int(seq, "ack.seq", minimum=0)
        if seq == 0:
            if self.initial_initialized:
                raise W8UElectronError("E_CREDIT", "duplicate initial ACK seq 0 rejected", rc=RC_REFUSED)
            allowed = self.max_initial_grant
            eff_grant = allowed if grant is None else _require_exact_int(grant, "ack.grant", minimum=1)
            if eff_grant > allowed:
                raise W8UElectronError(
                    "E_CREDIT",
                    f"initial ACK seq 0 overgrant {eff_grant} > min(2, capacity={self.reserved_capacity})={allowed}",
                    rc=RC_REFUSED,
                )
            self.initial_initialized = True
            self.cumulative_credits = eff_grant
            return self.cumulative_credits

        if not self.initial_initialized:
            raise W8UElectronError("E_CREDIT", "initial ACK seq 0 required before frame ACKs", rc=RC_REFUSED)
        if seq in self._acked_seqs:
            raise W8UElectronError("E_CREDIT", f"duplicate ACK for frame seq {seq}", rc=RC_REFUSED)
        if seq != self.acknowledged_frames + 1:
            raise W8UElectronError("E_CREDIT", f"out-of-order ACK seq {seq}, expected {self.acknowledged_frames + 1}", rc=RC_REFUSED)
        if seq > self.emitted_frames:
            raise W8UElectronError("E_CREDIT", f"cannot ACK unemitted frame seq {seq} > {self.emitted_frames}", rc=RC_REFUSED)

        eff_grant = 1 if grant is None else _require_exact_int(grant, "ack.grant", minimum=0)
        new_acknowledged = self.acknowledged_frames + 1
        new_cumulative = self.cumulative_credits + eff_grant
        if new_cumulative > new_acknowledged + self.reserved_capacity:
            raise W8UElectronError(
                "E_CREDIT",
                f"credit inflation/overgrant: cumulative {new_cumulative} > acknowledged {new_acknowledged} + capacity {self.reserved_capacity}",
                rc=RC_REFUSED,
            )
        self._acked_seqs.add(seq)
        self.acknowledged_frames = new_acknowledged
        self.cumulative_credits = new_cumulative
        return self.cumulative_credits

    def consume_credit_for_emit(self) -> int:
        if not self.initial_initialized:
            raise W8UElectronError("E_CREDIT", "cannot emit frame before initial ACK seq 0", rc=RC_REFUSED)
        if self.emitted_frames >= self.cumulative_credits:
            raise W8UElectronError("E_CREDIT", "frame credit exhausted", rc=RC_REFUSED)
        self.emitted_frames += 1
        return self.emitted_frames


# @anchor:wa.perceive.w8u_electron.CapsuleFrameStream
class CapsuleFrameStream:
    """In-memory held-pair XWD frame stream enforcing OS contract, credits, and STOP/END/EOF/join exit."""

    def __init__(
        self,
        *,
        os_contract: PinnedOsContract,
        registry: SessionRegistry,
        handle: Handle,
        run_id: str,
        reserved_capacity: int = 2,
    ) -> None:
        if not isinstance(os_contract, PinnedOsContract):
            raise W8UElectronError("E_SCHEMA", "os_contract must be a PinnedOsContract", rc=RC_SCHEMA)
        if not isinstance(registry, SessionRegistry):
            raise W8UElectronError("E_SCHEMA", "registry must be a SessionRegistry", rc=RC_SCHEMA)
        if not isinstance(handle, Handle):
            raise W8UElectronError("E_SCHEMA", "handle must be a Handle", rc=RC_SCHEMA)
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise W8UElectronError("E_SCHEMA", f"invalid run_id: {run_id!r}", rc=RC_SCHEMA)

        self.os_contract = os_contract
        self.registry = registry
        self.handle = handle
        self.run_id = run_id
        self.credits = CreditWindowController(reserved_capacity=reserved_capacity)
        self.pipe = InMemoryPipe()
        self.admitted_frames: list[tuple[FrameRef, dict]] = []
        self.pair_mismatches = 0
        self.frame_gaps = 0
        self.frames_dropped = 0
        self.stopped = False
        self.ended = False
        self.eof_sent = False
        self.reader_joined = False
        self.reader_exit_code: int | None = None
        self.ram_purged = False
        self._last_seq = 0
        self._reader_thread = threading.Thread(target=self._reader_worker, name="w8u-frame-reader", daemon=True)
        self._reader_stop_ev = threading.Event()
        self._reader_thread.start()

    def _reader_worker(self) -> None:
        self._reader_stop_ev.wait()

    def ack(self, *, seq: int, grant: int | None = None) -> int:
        if self.stopped or self.ended:
            raise W8UElectronError("E_STALE", "cannot ACK after STOP/END", rc=RC_REFUSED)
        return self.credits.ack(seq=seq, grant=grant)

    def reopen_channel(self) -> None:
        """Per-frame channel reopen is forbidden; delegating to pipe records and refuses."""
        self.pipe.reopen()

    def ingest_held_pair(
        self,
        grab_a_xwd: bytes,
        grab_b_xwd: bytes,
        *,
        seq: int,
        capture_mono_ns: int,
        epoch: int,
        tick: int | None = None,
        tick_valid: bool = False,
    ) -> tuple[FrameRef, dict]:
        """Validate a same-grab XWD pair, verify credit & epoch, and admit FrameRef + metadata."""
        if self.stopped or self.ended:
            self.frames_dropped += 1
            raise W8UElectronError("E_STALE", "stale frame publication after STOP/END refused", rc=RC_REFUSED)

        _require_exact_int(seq, "seq", minimum=1)
        _require_exact_int(capture_mono_ns, "capture_mono_ns", minimum=0)
        _require_exact_int(epoch, "epoch", minimum=0)
        if type(tick_valid) is not bool:
            raise W8UElectronError("E_SCHEMA", "tick_valid must be bool", rc=RC_SCHEMA)
        if tick_valid:
            _require_exact_int(tick, "tick", minimum=0)
        elif tick is not None:
            raise W8UElectronError("E_SCHEMA", "tick invalid means null (None), not committed zero", rc=RC_SCHEMA)

        try:
            rec = self.registry.resolve(self.handle)
        except (StaleHandleError, RegistryError) as exc:
            self.frames_dropped += 1
            raise W8UElectronError("E_STALE", f"stale handle on frame publication: {exc}", rc=RC_REFUSED) from exc

        if rec.epoch != epoch or rec.handle.generation != self.handle.generation:
            self.frames_dropped += 1
            raise W8UElectronError("E_STALE", f"stale frame epoch {epoch} != registry epoch {rec.epoch}", rc=RC_REFUSED)

        rgb_a, meta_a = parse_xwd_common_subset(grab_a_xwd)
        rgb_b, meta_b = parse_xwd_common_subset(grab_b_xwd)
        sha_a = hashlib.sha256(rgb_a.tobytes()).hexdigest()
        sha_b = hashlib.sha256(rgb_b.tobytes()).hexdigest()
        if meta_a != meta_b or sha_a != sha_b:
            self.pair_mismatches += 1
            self.frames_dropped += 1
            raise W8UElectronError("E_PAIR_MISMATCH", "same-grab canonical XWD pair mismatch", rc=RC_REFUSED)

        if sha_a in self.os_contract.rejected_vectors:
            self.frames_dropped += 1
            raise W8UElectronError("E_SCHEMA", "frame sha256 matches pinned rejected vector", rc=RC_REFUSED)

        self.credits.consume_credit_for_emit()
        if seq > self._last_seq + 1:
            self.frame_gaps += seq - (self._last_seq + 1)
        self._last_seq = seq

        try:
            frame_ref = self.registry.admit_frame(self.handle, seq, rgb_a, epoch=epoch)
        except FrameValidationError as exc:
            self.frames_dropped += 1
            raise W8UElectronError("E_STALE", str(exc), rc=RC_REFUSED) from exc

        binding_payload = {
            "schema": BINDING_SCHEMA,
            "contract_id": self.os_contract.contract_id,
            "contract_revision": self.os_contract.revision,
            "run_id": self.run_id,
            "handle": {
                "kind": self.handle.kind,
                "canonical_id": self.handle.canonical_id,
                "generation": self.handle.generation,
            },
            "epoch": epoch,
            "seq": seq,
            "sha256": frame_ref.digest,
        }
        binding_sha256 = domain_sha256("binding", binding_payload)
        frame_meta = {
            "seq": seq,
            "tick": tick if tick_valid else None,
            "tick_valid": tick_valid,
            "capture_mono_ns": capture_mono_ns,
            "width": meta_a["width"],
            "height": meta_a["height"],
            "stride": meta_a["stride"],
            "pixel_format": meta_a["pixel_format"],
            "sha256": frame_ref.digest,
            "binding_sha256": binding_sha256,
        }
        meta_errs = validate_frame_metadata(frame_meta)
        if meta_errs:
            raise W8UElectronError("E_SCHEMA", "; ".join(meta_errs), rc=RC_SCHEMA)
        self.pipe.write(encode_control_frame("FRAME", frame_meta))
        self.admitted_frames.append((frame_ref, frame_meta))
        return frame_ref, frame_meta

    def shutdown(self, *, end_code: str = "OK", reason: str = "normal_completion", purge_ram: bool = True) -> None:
        """Execute mandatory STOP -> END -> EOF -> joined reader exit lifecycle."""
        if end_code not in END_NUMERIC_ERRORS:
            raise W8UElectronError("E_SCHEMA", f"unknown END code: {end_code!r}", rc=RC_SCHEMA)
        try:
            rec = self.registry.resolve(self.handle)
            cur_epoch = rec.epoch
        except Exception:
            cur_epoch = 0

        if not self.stopped:
            self.pipe.write(encode_control_frame("STOP", {"epoch": cur_epoch, "reason": reason}))
            self.stopped = True
        if not self.ended:
            err_num = END_NUMERIC_ERRORS[end_code]
            self.pipe.write(
                encode_control_frame(
                    "END",
                    {"code": end_code, "error_code": err_num, "final_seq": self._last_seq},
                )
            )
            self.ended = True
            self.reader_exit_code = err_num
        if not self.eof_sent:
            self.pipe.write(encode_control_frame("EOF", {"final_seq": self._last_seq}))
            self.pipe.close()
            self.eof_sent = True

        self._reader_stop_ev.set()
        self._reader_thread.join(timeout=2.0)
        self.reader_joined = not self._reader_thread.is_alive()
        if purge_ram:
            self.ram_purged = True


# @anchor:wa.perceive.w8u_electron.ExternalLeaseAuthority
class ExternalLeaseAuthority:
    """Out-of-payload lease and grant authority for w8u.action/v1 verification."""

    def __init__(
        self,
        *,
        run_id: str,
        lease_id: str,
        handle: Handle,
        secret_nonce: bytes = b"w8u-authority-secret",
    ) -> None:
        _require_nonempty_str(run_id, "run_id")
        _require_nonempty_str(lease_id, "lease_id")
        if not isinstance(handle, Handle):
            raise W8UElectronError("E_SCHEMA", "handle must be Handle", rc=RC_SCHEMA)
        self.run_id = run_id
        self.lease_id = lease_id
        self.handle = handle
        self._secret_nonce = bytes(secret_nonce)

    def compute_grant_sha256(self, *, epoch: int, generation: int) -> str:
        _require_exact_int(epoch, "epoch", minimum=0)
        _require_exact_int(generation, "generation", minimum=1)
        material = (
            f"{self.run_id}\x00{self.lease_id}\x00{self.handle.kind}\x00"
            f"{self.handle.canonical_id}\x00{generation}\x00{epoch}\x00"
        ).encode("utf-8") + self._secret_nonce
        return hashlib.sha256(material).hexdigest()

    def verify(self, action: dict) -> bool:
        if action.get("run_id") != self.run_id:
            return False
        if action.get("lease_id") != self.lease_id:
            return False
        if action.get("generation") != self.handle.generation:
            return False
        expected = self.compute_grant_sha256(
            epoch=action["epoch"],
            generation=action["generation"],
        )
        return action.get("grant_sha256") == expected


def _validate_verb_and_args(verb: object, args: object, *, screen_width: int, screen_height: int) -> tuple[str, dict]:
    if not isinstance(verb, str) or verb not in ALLOWED_VERBS:
        raise W8UElectronError("E_VERB", f"unknown action verb: {verb!r}", rc=RC_SCHEMA)
    if not isinstance(args, dict):
        raise W8UElectronError("E_SCHEMA", "action args must be a dict", rc=RC_SCHEMA)
    _reject_float_and_non_canonical(args, "$.args")

    if verb == "pointer":
        op = args.get("operation")
        if op not in POINTER_OPERATIONS:
            raise W8UElectronError("E_SCHEMA", f"invalid pointer operation: {op!r}", rc=RC_SCHEMA)
        if op == "move":
            expected_keys = ("operation", "x", "y")
        elif op in ("click", "down", "up"):
            expected_keys = ("button", "operation", "x", "y")
        else:
            expected_keys = ("dx", "dy", "operation", "x", "y")
        if tuple(sorted(args.keys())) != expected_keys:
            raise W8UElectronError("E_SCHEMA", f"pointer {op} keys {sorted(args.keys())} != {list(expected_keys)}", rc=RC_SCHEMA)
        x = _require_exact_int(args["x"], "args.x")
        y = _require_exact_int(args["y"], "args.y")
        if not (0 <= x < screen_width and 0 <= y < screen_height):
            raise W8UElectronError("E_SCHEMA", f"pointer coordinates ({x}, {y}) out of bounds ({screen_width}x{screen_height})", rc=RC_SCHEMA)
        if op in ("click", "down", "up"):
            btn = _require_exact_int(args["button"], "args.button")
            if btn not in POINTER_BUTTONS:
                raise W8UElectronError("E_SCHEMA", f"pointer button must be in {POINTER_BUTTONS}, got {btn}", rc=RC_SCHEMA)
        elif op == "scroll":
            dx = _require_exact_int(args["dx"], "args.dx", minimum=-MAX_SCROLL_DELTA, maximum=MAX_SCROLL_DELTA)
            dy = _require_exact_int(args["dy"], "args.dy", minimum=-MAX_SCROLL_DELTA, maximum=MAX_SCROLL_DELTA)
            if dx == 0 and dy == 0:
                raise W8UElectronError("E_SCHEMA", "scroll deltas dx, dy cannot both be zero", rc=RC_SCHEMA)
        return verb, args

    if verb == "key":
        if tuple(sorted(args.keys())) != ("key", "modifiers", "operation"):
            raise W8UElectronError("E_SCHEMA", f"key args keys {sorted(args.keys())} != ['key', 'modifiers', 'operation']", rc=RC_SCHEMA)
        op = args["operation"]
        if op not in KEY_OPERATIONS:
            raise W8UElectronError("E_SCHEMA", f"invalid key operation: {op!r}", rc=RC_SCHEMA)
        k = args["key"]
        if not isinstance(k, str) or k not in FINITE_KEYS:
            raise W8UElectronError("E_SCHEMA", f"key {k!r} not in finite key allowlist", rc=RC_SCHEMA)
        mods = args["modifiers"]
        if not isinstance(mods, list):
            raise W8UElectronError("E_SCHEMA", "modifiers must be a list", rc=RC_SCHEMA)
        last_idx = -1
        for m in mods:
            if not isinstance(m, str) or m not in MODIFIER_ORDER:
                raise W8UElectronError("E_SCHEMA", f"unknown modifier: {m!r}", rc=RC_SCHEMA)
            cur_idx = MODIFIER_ORDER.index(m)
            if cur_idx <= last_idx:
                raise W8UElectronError("E_SCHEMA", "modifiers must be unique and ordered Shift,Control,Alt,Super", rc=RC_SCHEMA)
            last_idx = cur_idx
        return verb, args

    if verb == "text":
        if tuple(sorted(args.keys())) != ("mode", "text"):
            raise W8UElectronError("E_SCHEMA", f"text args keys {sorted(args.keys())} != ['mode', 'text']", rc=RC_SCHEMA)
        if args["mode"] != "type":
            raise W8UElectronError("E_SCHEMA", "text mode must be 'type'", rc=RC_SCHEMA)
        txt = args["text"]
        if not isinstance(txt, str) or "\x00" in txt or len(txt) == 0:
            raise W8UElectronError("E_SCHEMA", "text must be a non-empty UTF-8 string without NUL", rc=RC_SCHEMA)
        try:
            encoded_txt = txt.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UElectronError("E_SCHEMA", f"invalid UTF-8 in text: {exc}", rc=RC_SCHEMA) from exc
        if len(encoded_txt) > MAX_TEXT_BYTES:
            raise W8UElectronError("E_SCHEMA", f"text UTF-8 byte length {len(encoded_txt)} exceeds {MAX_TEXT_BYTES}", rc=RC_SCHEMA)
        return verb, args

    # verb == "wait"
    if tuple(sorted(args.keys())) != ("duration_ms",):
        raise W8UElectronError("E_SCHEMA", f"wait args keys {sorted(args.keys())} != ['duration_ms']", rc=RC_SCHEMA)
    _require_exact_int(args["duration_ms"], "args.duration_ms", minimum=0, maximum=MAX_WAIT_MS)
    return verb, args


# @anchor:wa.perceive.w8u_electron.validate_action_payload
def validate_action_payload(
    raw_or_dict: bytes | str | dict,
    *,
    screen_width: int = 1280,
    screen_height: int = 720,
) -> tuple[dict, str]:
    """Validate canonical w8u.action/v1 payload before any actuator call.

    Returns (validated_dict, canonical_sha256).
    """
    if isinstance(raw_or_dict, (bytes, str)):
        obj = loads_strict(raw_or_dict, max_bytes=MAX_ACTION_BODY_BYTES)
    elif isinstance(raw_or_dict, dict):
        _reject_float_and_non_canonical(raw_or_dict)
        canonical_str = dumps_canonical(raw_or_dict)
        if len(canonical_str.encode("utf-8")) > MAX_ACTION_BODY_BYTES:
            raise W8UElectronError("E_SCHEMA", "canonical action body exceeds 64 KiB cap", rc=RC_SCHEMA)
        obj = loads_strict(canonical_str, max_bytes=MAX_ACTION_BODY_BYTES)
    else:
        raise W8UElectronError("E_SCHEMA", "action payload must be bytes, str, or dict", rc=RC_SCHEMA)

    if tuple(sorted(obj.keys())) != ACTION_KEYS:
        raise W8UElectronError("E_SCHEMA", f"action keys {sorted(obj.keys())} != {list(ACTION_KEYS)}", rc=RC_SCHEMA)
    if obj["schema"] != ACTION_SCHEMA:
        raise W8UElectronError("E_SCHEMA", f"action schema must be {ACTION_SCHEMA!r}", rc=RC_SCHEMA)

    _require_nonempty_str(obj["run_id"], "run_id")
    if not _RUN_ID_RE.fullmatch(obj["run_id"]):
        raise W8UElectronError("E_SCHEMA", "run_id contains invalid characters", rc=RC_SCHEMA)
    _require_nonempty_str(obj["lease_id"], "lease_id")
    _require_exact_int(obj["epoch"], "epoch", minimum=0)
    _require_exact_int(obj["generation"], "generation", minimum=1)
    _require_nonempty_str(obj["action_id"], "action_id")
    _require_exact_int(obj["action_seq"], "action_seq", minimum=1)
    _require_exact_int(obj["observed_frame_seq"], "observed_frame_seq", minimum=0)
    _require_hex64(obj["observed_frame_sha256"], "observed_frame_sha256")
    _require_exact_int(obj["deadline_ns"], "deadline_ns", minimum=0)
    _require_hex64(obj["grant_sha256"], "grant_sha256")

    _validate_verb_and_args(
        obj["verb"],
        obj["args"],
        screen_width=screen_width,
        screen_height=screen_height,
    )
    digest = domain_sha256("action", obj)
    return obj, digest


# @anchor:wa.perceive.w8u_electron.ClosedActionChannel
class ClosedActionChannel:
    """Closed w8u.action/v1 verification gate and actuator channel.

    Enforces:
    - Schema and coordinate/key/text/wait bounds before any actuator invocation.
    - External authority verification outside payload.
    - Epoch, observed_frame_seq/sha256 freshness, editable focus for text, budget, and deadline.
    - Idempotent replay for identical payload; refusal for changed payload on replay.
    - Lost actuator ACK marks response `uncertain` (`E_UNCERTAIN`, `rc=6`) and quarantines
      the channel, refusing any new-ID retry.
    """

    def __init__(
        self,
        *,
        authority: ExternalLeaseAuthority,
        registry: SessionRegistry,
        handle: Handle,
        clock_ns: Callable[[], int],
        screen_width: int = 1280,
        screen_height: int = 720,
        budget_remaining: int = 100,
        editable_focus: bool = True,
        actuator: Callable[[dict], dict] | None = None,
    ) -> None:
        self.authority = authority
        self.registry = registry
        self.handle = handle
        self.clock_ns = clock_ns
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.budget_remaining = budget_remaining
        self.editable_focus = editable_focus
        self.actuator = actuator
        self.actuator_calls: list[dict] = []
        self._history_by_id: dict[str, tuple[str, dict, int]] = {}
        self._last_action_seq = 0
        self.quarantined_action_id: str | None = None
        self.action_log: list[dict] = []
        self.admitted_count = 0
        self.refused_count = 0
        self.uncertain_count = 0

    def _make_response(
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
        outcome_sha256: str | None,
    ) -> dict:
        resp = {
            "schema": ACTION_RESPONSE_SCHEMA,
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
        if tuple(sorted(resp.keys())) != ACTION_RESPONSE_KEYS:
            raise AssertionError("response schema keys mismatch")
        return resp

    def submit(
        self,
        raw_or_dict: bytes | str | dict,
        *,
        latest_frame: FrameRef | None,
    ) -> tuple[dict, int]:
        """Validate and execute or refuse an action; returns (response_dict, rc)."""
        try:
            action, payload_sha = validate_action_payload(
                raw_or_dict,
                screen_width=self.screen_width,
                screen_height=self.screen_height,
            )
        except W8UElectronError as exc:
            fallback_id = "invalid"
            fallback_seq = 0
            fallback_lease = self.authority.lease_id
            fallback_epoch = 0
            if isinstance(raw_or_dict, dict):
                if isinstance(raw_or_dict.get("action_id"), str) and raw_or_dict["action_id"]:
                    fallback_id = raw_or_dict["action_id"]
                if _is_exact_int(raw_or_dict.get("action_seq")):
                    fallback_seq = raw_or_dict["action_seq"]
                if isinstance(raw_or_dict.get("lease_id"), str) and raw_or_dict["lease_id"]:
                    fallback_lease = raw_or_dict["lease_id"]
                if _is_exact_int(raw_or_dict.get("epoch")) and raw_or_dict["epoch"] >= 0:
                    fallback_epoch = raw_or_dict["epoch"]
            resp = self._make_response(
                action_id=fallback_id,
                action_seq=fallback_seq,
                lease_id=fallback_lease,
                epoch=fallback_epoch,
                status="refused",
                code=exc.code if exc.code in ACTION_CODES else "E_SCHEMA",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_SCHEMA

        aid = action["action_id"]
        aseq = action["action_seq"]
        lease_id = action["lease_id"]
        epoch = action["epoch"]

        # Check replay idempotency / changed payload refusal first
        if aid in self._history_by_id:
            prev_sha, prev_resp, prev_rc = self._history_by_id[aid]
            if prev_sha == payload_sha:
                # Identical replay is idempotent: return recorded outcome without calling actuator
                return dict(prev_resp), prev_rc
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_LOCK",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # Quarantine check: if a prior action lost its ACK, refuse any new-ID retry
        if self.quarantined_action_id is not None and aid != self.quarantined_action_id:
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_LOCK",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # External authority check outside payload
        if not self.authority.verify(action):
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_LOCK",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # Registry handle & epoch verification
        try:
            rec = self.registry.resolve(self.handle)
        except (StaleHandleError, RegistryError):
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_EPOCH",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        if rec.epoch != epoch or rec.handle.generation != action["generation"]:
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_EPOCH",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # Observed frame freshness check
        if (
            latest_frame is None
            or latest_frame.epoch != epoch
            or latest_frame.frame_seq != action["observed_frame_seq"]
            or latest_frame.digest != action["observed_frame_sha256"]
        ):
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_STALE_FRAME",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # Editable focus check for text verb
        if action["verb"] == "text" and not self.editable_focus:
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_FOCUS",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # Budget check
        if self.budget_remaining <= 0:
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_BUDGET",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # Deadline check
        now_ns = self.clock_ns()
        wait_ns = action["args"]["duration_ms"] * 1_000_000 if action["verb"] == "wait" else 0
        if now_ns > action["deadline_ns"] or (now_ns + wait_ns) > action["deadline_ns"]:
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_DEADLINE",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        # Monotonic action sequence check
        if aseq != self._last_action_seq + 1:
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_LOCK",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_REFUSED

        if self.actuator is None:
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="refused",
                code="E_LOCK",
                emitted=False,
                result_known=True,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.refused_count += 1
            self.action_log.append(resp)
            return resp, RC_UNAVAILABLE

        self.budget_remaining -= 1
        self._last_action_seq = aseq
        self.actuator_calls.append(action)

        try:
            act_out = self.actuator(action)
        except Exception:
            act_out = {"lost_ack": True}

        if not isinstance(act_out, dict) or act_out.get("lost_ack"):
            self.quarantined_action_id = aid
            resp = self._make_response(
                action_id=aid,
                action_seq=aseq,
                lease_id=lease_id,
                epoch=epoch,
                status="uncertain",
                code="E_UNCERTAIN",
                emitted=True,
                result_known=False,
                applied_tick=None,
                outcome_sha256=None,
            )
            self.uncertain_count += 1
            self._history_by_id[aid] = (payload_sha, resp, RC_UNCERTAIN)
            self.action_log.append(resp)
            return resp, RC_UNCERTAIN

        effect_applied = bool(act_out.get("applied", False))
        applied_tick = act_out.get("applied_tick") if effect_applied else None
        if applied_tick is not None:
            _require_exact_int(applied_tick, "applied_tick", minimum=0)
        status = "applied" if effect_applied else "admitted"
        outcome_sha = domain_sha256(
            "outcome",
            {
                "action_id": aid,
                "action_seq": aseq,
                "status": status,
                "applied_tick": applied_tick,
            },
        )
        resp = self._make_response(
            action_id=aid,
            action_seq=aseq,
            lease_id=lease_id,
            epoch=epoch,
            status=status,
            code="OK_ADMITTED",
            emitted=True,
            result_known=True,
            applied_tick=applied_tick,
            outcome_sha256=outcome_sha,
        )
        self.admitted_count += 1
        self._history_by_id[aid] = (payload_sha, resp, RC_VALID)
        self.action_log.append(resp)
        return resp, RC_VALID


# @anchor:wa.perceive.w8u_electron.build_receipt
def build_receipt(
    *,
    run_id: str,
    lease_id: str,
    target_handle: Handle | dict,
    epoch: int,
    frames: list[dict],
    action_channel: ClosedActionChannel,
    final_state_sha256: str | None = None,
    simulation_state_sha256: str | None = None,
    t2_trace_sha256: str | None = None,
    t2_terminal_sha256: str | None = None,
    t2_ticks: int | None = None,
    pair_mismatches: int = 0,
    frame_gaps: int = 0,
    frames_dropped: int = 0,
    x11_tcp_listeners: int = 0,
    x11_unix_listeners: int = 1,
    x11_abstract_listeners: int = 0,
    reader_exit_code: int = 0,
    reader_quiescent: bool = True,
    ram_purged: bool = True,
    frames_disk_bytes: int = 0,
    swap_delta_bytes: int = 0,
    metadata_only: bool = True,
    pixel_required: bool = True,
) -> dict:
    """Build and validate a canonical w8u.receipt/v1 dictionary."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise W8UElectronError("E_SCHEMA", f"invalid run_id for receipt: {run_id!r}", rc=RC_SCHEMA)
    _require_nonempty_str(lease_id, "lease_id")
    _require_exact_int(epoch, "epoch", minimum=0)

    if isinstance(target_handle, Handle):
        h_dict = {
            "kind": target_handle.kind,
            "canonical_id": target_handle.canonical_id,
            "generation": target_handle.generation,
        }
    elif isinstance(target_handle, dict):
        h_dict = dict(target_handle)
    else:
        raise W8UElectronError("E_SCHEMA", "target_handle must be Handle or dict", rc=RC_SCHEMA)

    frames_count = len(frames)
    if frames_count == 0:
        first_frame = None
        last_frame = None
        if pixel_required:
            verdict = "indeterminate"
            gate_code = "E_ZERO_FRAMES"
        else:
            verdict = "pass"
            gate_code = "OK"
    else:
        first_frame = dict(frames[0])
        last_frame = dict(frames[-1])
        if pair_mismatches > 0 or frames_disk_bytes > 0 or x11_tcp_listeners > 0 or x11_abstract_listeners > 0 or not reader_quiescent:
            verdict = "fail"
            gate_code = "E_GATE_VIOLATION"
        else:
            verdict = "pass"
            gate_code = "OK"

    action_log_sha = domain_sha256("action", {"log": action_channel.action_log})
    ui_actions_count = (
        action_channel.admitted_count
        + action_channel.refused_count
        + action_channel.uncertain_count
    )

    receipt = {
        "run_id": run_id,
        "lease_id": lease_id,
        "target_handle": h_dict,
        "epoch": epoch,
        "frames_root": FRAMES_ROOT,
        "frame_run_root": f"{FRAMES_ROOT}/{FRAME_RUN_SUBDIR}/{run_id}",
        "source": FRAME_SOURCE,
        "frames_captured": frames_count,
        "first_frame": first_frame,
        "last_frame": last_frame,
        "ui_actions_count": ui_actions_count,
        "admitted_actions": action_channel.admitted_count,
        "refused_actions": action_channel.refused_count,
        "uncertain_actions": action_channel.uncertain_count,
        "action_log_sha256": action_log_sha,
        "t2_trace_sha256": t2_trace_sha256,
        "t2_terminal_sha256": t2_terminal_sha256,
        "t2_ticks": t2_ticks,
        "final_state_sha256": final_state_sha256,
        "simulation_state_sha256": simulation_state_sha256,
        "ui_gate_verdict": verdict,
        "ui_gate_code": gate_code,
        "pair_mismatches": pair_mismatches,
        "frame_gaps": frame_gaps,
        "frames_dropped": frames_dropped,
        "x11_tcp_listeners": x11_tcp_listeners,
        "x11_unix_listeners": x11_unix_listeners,
        "x11_abstract_listeners": x11_abstract_listeners,
        "reader_exit_code": reader_exit_code,
        "reader_quiescent": reader_quiescent,
        "ram_purged": ram_purged,
        "frames_disk_bytes": frames_disk_bytes,
        "swap_delta_bytes": swap_delta_bytes,
        "metadata_only": metadata_only,
    }
    errs = validate_receipt(receipt, pixel_required=pixel_required)
    if errs:
        raise W8UElectronError("E_SCHEMA", f"invalid receipt: {'; '.join(errs)}", rc=RC_SCHEMA)
    return receipt


# @anchor:wa.perceive.w8u_electron.validate_receipt
def validate_receipt(receipt: dict, *, pixel_required: bool = True) -> list[str]:
    """Validate a w8u.receipt/v1 object and return a list of violations (empty = valid)."""
    if not isinstance(receipt, dict):
        return ["receipt must be a dict"]
    errs: list[str] = []
    try:
        _reject_float_and_non_canonical(receipt)
    except W8UElectronError as exc:
        errs.append(str(exc))

    keys = [k for k in receipt.keys() if k != "schema"]
    if tuple(sorted(keys)) != tuple(sorted(RECEIPT_FIELD_KEYS)):
        errs.append(f"receipt keys {sorted(keys)} != {sorted(RECEIPT_FIELD_KEYS)}")
        return errs
    if "schema" in receipt and receipt["schema"] != RECEIPT_SCHEMA:
        errs.append(f"receipt.schema must be {RECEIPT_SCHEMA!r}")

    run_id = receipt["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        errs.append("run_id is invalid")
    if not isinstance(receipt["lease_id"], str) or not receipt["lease_id"]:
        errs.append("lease_id must be non-empty str")

    th = receipt["target_handle"]
    if not isinstance(th, dict) or tuple(sorted(th.keys())) != tuple(sorted(RECEIPT_HANDLE_KEYS)):
        errs.append(f"target_handle must be nested object with keys {RECEIPT_HANDLE_KEYS}")
    else:
        if th["kind"] not in schema.TARGET_KINDS:
            errs.append(f"target_handle.kind {th['kind']!r} not in {schema.TARGET_KINDS}")
        if not isinstance(th["canonical_id"], str) or not th["canonical_id"]:
            errs.append("target_handle.canonical_id must be non-empty str")
        if not _is_exact_int(th["generation"]) or th["generation"] < 1:
            errs.append("target_handle.generation must be positive exact int")

    if not _is_exact_int(receipt["epoch"]) or receipt["epoch"] < 0:
        errs.append("epoch must be non-negative exact int")
    if receipt["frames_root"] != FRAMES_ROOT:
        errs.append(f"frames_root must be {FRAMES_ROOT!r}")
    if isinstance(run_id, str) and receipt["frame_run_root"] != f"{FRAMES_ROOT}/{FRAME_RUN_SUBDIR}/{run_id}":
        errs.append("frame_run_root must equal frames_root/opensuperficies/os-cu-0/<run_id>")
    if receipt["source"] != FRAME_SOURCE:
        errs.append(f"source must be {FRAME_SOURCE!r}")

    fc = receipt["frames_captured"]
    if not _is_exact_int(fc) or fc < 0:
        errs.append("frames_captured must be non-negative exact int")
    elif fc == 0:
        if receipt["first_frame"] is not None or receipt["last_frame"] is not None:
            errs.append("zero-frame run must have first_frame and last_frame equal to null")
        if pixel_required and receipt["ui_gate_verdict"] != "indeterminate":
            errs.append("pixel-required zero-frame run must have ui_gate_verdict == 'indeterminate'")
    else:
        for fk in ("first_frame", "last_frame"):
            if not isinstance(receipt[fk], dict):
                errs.append(f"{fk} must be a frame metadata object when frames_captured > 0")
            else:
                errs.extend(f"{fk}: {e}" for e in validate_frame_metadata(receipt[fk]))

    for int_field in (
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
        "swap_delta_bytes",
    ):
        if not _is_exact_int(receipt[int_field]) or receipt[int_field] < 0:
            errs.append(f"{int_field} must be a non-negative exact int")

    if (
        _is_exact_int(receipt["ui_actions_count"])
        and _is_exact_int(receipt["admitted_actions"])
        and _is_exact_int(receipt["refused_actions"])
        and _is_exact_int(receipt["uncertain_actions"])
        and receipt["ui_actions_count"]
        != receipt["admitted_actions"] + receipt["refused_actions"] + receipt["uncertain_actions"]
    ):
        errs.append("ui_actions_count must equal admitted_actions + refused_actions + uncertain_actions")

    if not _is_exact_int(receipt["reader_exit_code"]):
        errs.append("reader_exit_code must be an exact int")

    for bool_field in ("reader_quiescent", "ram_purged", "metadata_only"):
        if type(receipt[bool_field]) is not bool:
            errs.append(f"{bool_field} must be bool")

    if receipt["frames_disk_bytes"] != 0:
        errs.append("frames_disk_bytes must be 0 (STORAGE LAW)")

    if not isinstance(receipt["action_log_sha256"], str) or not _HEX64_RE.fullmatch(receipt["action_log_sha256"]):
        errs.append("action_log_sha256 must be 64 lowercase hex chars")

    for opt_sha in ("t2_trace_sha256", "t2_terminal_sha256", "final_state_sha256", "simulation_state_sha256"):
        val = receipt[opt_sha]
        if val is not None and (not isinstance(val, str) or not _HEX64_RE.fullmatch(val)):
            errs.append(f"{opt_sha} must be null or 64 lowercase hex chars")

    if receipt["t2_ticks"] is not None:
        if not _is_exact_int(receipt["t2_ticks"]) or receipt["t2_ticks"] < 0:
            errs.append("t2_ticks must be null or a non-negative exact int")
        if receipt["t2_trace_sha256"] is None and receipt["t2_ticks"] == 0:
            errs.append("missing t2 witness must leave t2_ticks as null, not committed zero")

    return errs


# @anchor:wa.perceive.w8u_electron.DiskSinkSpy
class DiskSinkSpy:
    """Spy and guard ensuring zero frame or perception bytes are written to disk (STORAGE LAW)."""

    def __init__(self) -> None:
        self.write_attempts: list[str] = []
        self._orig_open = builtins.open
        self._orig_os_open = os.open
        self._orig_write_bytes = pathlib.Path.write_bytes
        self._orig_write_text = pathlib.Path.write_text
        self._orig_named_temp = tempfile.NamedTemporaryFile

    def _check_mode(self, target: object, mode: str) -> None:
        if any(ch in mode for ch in ("w", "a", "x", "+")):
            self.write_attempts.append(f"{target}:{mode}")
            raise W8UElectronError("E_STORAGE_LAW", f"disk write blocked by DiskSinkSpy: {target} ({mode})", rc=RC_REFUSED)

    def __enter__(self) -> DiskSinkSpy:
        spy = self

        def _guarded_open(file, mode="r", *args, **kwargs):
            spy._check_mode(file, str(mode))
            return spy._orig_open(file, mode, *args, **kwargs)

        def _guarded_os_open(path, flags, *args, **kwargs):
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & write_flags:
                spy.write_attempts.append(f"{path}:os.open({flags})")
                raise W8UElectronError("E_STORAGE_LAW", f"os.open write blocked by DiskSinkSpy: {path}", rc=RC_REFUSED)
            return spy._orig_os_open(path, flags, *args, **kwargs)

        def _guarded_write_bytes(path_self, data):
            del data
            spy.write_attempts.append(f"{path_self}:write_bytes")
            raise W8UElectronError("E_STORAGE_LAW", f"Path.write_bytes blocked by DiskSinkSpy: {path_self}", rc=RC_REFUSED)

        def _guarded_write_text(path_self, data, *args, **kwargs):
            del data, args, kwargs
            spy.write_attempts.append(f"{path_self}:write_text")
            raise W8UElectronError("E_STORAGE_LAW", f"Path.write_text blocked by DiskSinkSpy: {path_self}", rc=RC_REFUSED)

        def _guarded_named_temp(*args, **kwargs):
            del args, kwargs
            spy.write_attempts.append("tempfile.NamedTemporaryFile")
            raise W8UElectronError("E_STORAGE_LAW", "NamedTemporaryFile blocked by DiskSinkSpy", rc=RC_REFUSED)

        builtins.open = _guarded_open
        os.open = _guarded_os_open
        pathlib.Path.write_bytes = _guarded_write_bytes
        pathlib.Path.write_text = _guarded_write_text
        tempfile.NamedTemporaryFile = _guarded_named_temp
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        builtins.open = self._orig_open
        os.open = self._orig_os_open
        pathlib.Path.write_bytes = self._orig_write_bytes
        pathlib.Path.write_text = self._orig_write_text
        tempfile.NamedTemporaryFile = self._orig_named_temp
        return False
