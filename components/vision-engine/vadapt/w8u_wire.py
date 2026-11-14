"""Bounded W8U paired XWD wire reader (U3, pre-contract integration seam).

Only the authored, unambiguous W8UF and W8UC HELLO/ACK/STOP fields are
encoded here. W8UH confirmation and W8UE END semantics are supplied by an
accepted external contract via callbacks, never guessed locally. In particular,
receiving HELLO does *not* authorize frames or grant credit.

All input is in-memory bytes. Readers may return short reads or coalesced reads;
WireSession preserves surplus bytes between records. No resynchronisation.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from vadapt import xfb

FRAME_HEADER = struct.Struct(">4sHHQQQQIIIHHQ32s32s")
CONTROL_HEADER = struct.Struct(">4sHHI32s")
U64 = struct.Struct(">Q")
ACK_BODY = struct.Struct(">QQ")
END_BODY = struct.Struct(">QQI")
assert FRAME_HEADER.size == 128
assert CONTROL_HEADER.size == 44
FRAME_MAGIC = b"W8UF"
CONTROL_MAGIC = b"W8UC"
CONFIRM_MAGIC = b"W8UH"
END_MAGIC = b"W8UE"
VERSION = 1
PIXEL_RGB24 = 1
FLAG_PAIR_UNDER_GRAB = 1
FLAG_TICK_VALID = 2
HELLO_KIND = 1
ACK_KIND = 2
STOP_KIND = 3
MAX_HELLO_BYTES = 16 * 1024
MAX_XWD_HEADER = 65536
MAX_XWD_COLORS = 65536
MAX_XWD_STRIDE = 65536
XWD_FIXED_HEADER = struct.Struct(">25I")
MAX_U64 = (1 << 64) - 1
ROOT = "/dev/shm/vqa-ramws"

_BINDING_KEYS = frozenset((
    "schema", "run_id", "lease_id", "seat_grant_sha256", "container_id",
    "target_handle", "epoch", "display", "xid", "width", "height",
    "frames_root", "frame_run_root", "image_sha256", "reader_sha256",
    "exec_allowlist_sha256", "max_record_bytes", "max_inflight_frames", "tick_mode",
))
_HANDLE_KEYS = frozenset(("kind", "canonical_id", "generation"))
_DIGEST_KEYS = (
    "seat_grant_sha256", "image_sha256", "reader_sha256", "exec_allowlist_sha256",
)


class WireError(ValueError):
    """A closed W8U error name, without speculative numeric status codes."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str) -> None:
    raise WireError(code, message)


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        _refuse("E_BINDING", f"{label} must be a positive exact integer")
    return value


def _unsigned(value: Any, label: str, bits: int = 64) -> int:
    if type(value) is not int or not (0 <= value < 1 << bits):
        _refuse("E_BINDING", f"{label} must be an unsigned exact integer")
    return value


def _string(value: Any, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        _refuse("E_BINDING", f"invalid {label}")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise WireError("E_BINDING", f"invalid UTF-8: {label}") from exc
    return value


def _closed(obj: Any, keys: frozenset[str], label: str) -> dict:
    if type(obj) is not dict or frozenset(obj) != keys:
        _refuse("E_BINDING", f"{label} has absent or unknown fields")
    return obj


def _strict_json(value: Any) -> None:
    """Exclude floats, bool-as-int, nested lists and non-UTF8 text everywhere."""
    if value is None or type(value) is bool or type(value) is float:
        _refuse("E_BINDING", "null/bool/float are forbidden in binding")
    if type(value) is int:
        # Canonical integer-only JSON, constrained to interoperable signed 64-bit.
        if not -(1 << 63) <= value < (1 << 63):
            _refuse("E_BINDING", "out-of-range binding JSON integer")
    elif type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise WireError("E_BINDING", "invalid JSON string") from exc
    elif type(value) is dict:
        if any(type(k) is not str for k in value):
            _refuse("E_BINDING", "JSON keys must be strings")
        for v in value.values():
            _strict_json(v)
    else:
        _refuse("E_BINDING", "unsupported binding JSON type")


def _canonical_json(obj: Any) -> bytes:
    _strict_json(obj)
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise WireError("E_BINDING", "cannot canonicalise binding") from exc


def _decode_canonical_json(body: bytes) -> dict:
    def unique_pairs(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                _refuse("E_BINDING", "duplicate JSON key")
            d[k] = v
        return d
    try:
        obj = json.loads(body.decode("utf-8", errors="strict"),
                         object_pairs_hook=unique_pairs,
                         parse_float=lambda _: _refuse("E_BINDING", "noninteger JSON"),
                         parse_constant=lambda _: _refuse("E_BINDING", "nonfinite JSON"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WireError("E_BINDING", "invalid HELLO JSON") from exc
    if type(obj) is not dict or _canonical_json(obj) != body:
        _refuse("E_BINDING", "noncanonical HELLO JSON")
    return obj


@dataclass(frozen=True)
class WireLimits:
    max_record_bytes: int
    max_control_bytes: int
    max_inflight_frames: int
    binding_schema: str  # An accepted external contract must provide this.
    xwd_error_codes: dict  # Accepted native-to-W8U error mapping, no local defaults.

    def __post_init__(self) -> None:
        for k in ("max_record_bytes", "max_control_bytes", "max_inflight_frames"):
            _positive(getattr(self, k), k)
        _string(self.binding_schema, "binding_schema")
        if type(self.xwd_error_codes) is not dict or not self.xwd_error_codes:
            _refuse("E_BINDING", "accepted XWD error translations required")
        for native, code in self.xwd_error_codes.items():
            if type(native) is not str or type(code) is not str or code not in (
                    "E_TORN", "E_WIRE", "E_LENGTH", "E_HASH", "E_SEQ", "E_TICK",
                    "E_BINDING", "E_GEOMETRY", "E_CREDIT", "E_BACKPRESSURE",
                    "E_TIMEOUT", "E_ROOT", "E_RAM_QUOTA"):
                _refuse("E_BINDING", "invalid XWD error translation")


@dataclass(frozen=True)
class BindingInfo:
    canonical: bytes
    digest: bytes
    width: int
    height: int
    max_record_bytes: int
    max_inflight_frames: int
    tick_mode: str
    schema: str


def validate_binding(binding: dict, limits: WireLimits) -> BindingInfo:
    """Validate a structural binding; independently authenticate it at HELLO."""
    if type(limits) is not WireLimits:
        _refuse("E_BINDING", "WireLimits required")
    b = _closed(binding, _BINDING_KEYS, "binding")
    h = _closed(b["target_handle"], _HANDLE_KEYS, "target_handle")
    if b["schema"] != limits.binding_schema:
        _refuse("E_BINDING", "binding schema not externally authorised")
    for key in ("schema", "run_id", "lease_id", "container_id", "display"):
        _string(b[key], key)
    for key in ("kind", "canonical_id"):
        _string(h[key], "target_handle." + key)
    for key in _DIGEST_KEYS:
        if type(b[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", b[key]):
            _refuse("E_BINDING", "invalid hex digest " + key)
    for key in ("epoch", "xid"):
        _unsigned(b[key], key)
    _unsigned(h["generation"], "target_handle.generation")
    width = _positive(b["width"], "width")
    height = _positive(b["height"], "height")
    if width > xfb.MAX_SIDE or height > xfb.MAX_SIDE:
        _refuse("E_GEOMETRY", "binding dimensions exceed XWD side bounds")
    cap = _positive(b["max_record_bytes"], "max_record_bytes")
    inflight = _positive(b["max_inflight_frames"], "max_inflight_frames")
    if inflight > limits.max_inflight_frames:
        _refuse("E_BINDING", "negotiated capacity is not reserved")
    if b["tick_mode"] not in ("committed", "unavailable"):
        _refuse("E_TICK", "unknown tick mode")
    run_id = b["run_id"]
    if (run_id in (".", "..") or "/" in run_id or "\\" in run_id
            or posixpath.normpath(run_id) != run_id):
        _refuse("E_ROOT", "run id must be one path component")
    if b["frames_root"] != ROOT or b["frame_run_root"] != ROOT + "/opensuperficies/os-cu-0/" + run_id:
        _refuse("E_ROOT", "no root alias or fallback")
    canonical = _canonical_json(b)
    return BindingInfo(canonical=canonical, digest=hashlib.sha256(canonical).digest(),
                       width=width, height=height, max_record_bytes=cap,
                       max_inflight_frames=inflight, tick_mode=b["tick_mode"],
                       schema=b["schema"])


@dataclass(frozen=True)
class PairRecord:
    seq: int
    tick: Optional[int]
    tick_valid: bool
    capture_mono_ns: int
    width: int
    height: int
    stride: int
    pixel_format: int
    sha256: str
    binding_sha256: str
    xwd_a: bytes = field(repr=False)
    xwd_b: bytes = field(repr=False)

    @property
    def metadata(self) -> dict:
        return {"seq": self.seq, "tick": self.tick,
                "tick_valid": self.tick_valid,
                "capture_mono_ns": self.capture_mono_ns,
                "width": self.width, "height": self.height, "stride": self.stride,
                "pixel_format": self.pixel_format, "sha256": self.sha256,
                "binding_sha256": self.binding_sha256}


@dataclass(frozen=True)
class ControlRecord:
    magic: bytes
    kind: int
    binding_sha256: str
    body: bytes = field(repr=False)
    ack_seq: Optional[int] = None
    cumulative_credit_total: Optional[int] = None
    end_last_seq: Optional[int] = None
    end_sent: Optional[int] = None
    end_error_code: Optional[int] = None


@dataclass
class CreditWindow:
    """ACK validation for producers; credit must precede any capture."""
    capacity: int
    ack_seq: int = 0
    credit_total: int = 0
    started: bool = False

    def __post_init__(self) -> None:
        _positive(self.capacity, "capacity")

    def accept(self, ack_seq: int, cumulative_credit_total: int, *, produced: int) -> None:
        if any(type(v) is not int or v < 0 or v > MAX_U64
               for v in (ack_seq, cumulative_credit_total, produced)):
            _refuse("E_CREDIT", "invalid exact credit integer")
        if not self.started:
            if ack_seq != 0 or produced != 0 or cumulative_credit_total != min(2, self.capacity):
                _refuse("E_CREDIT", "invalid initial ACK")
            self.ack_seq, self.credit_total, self.started = ack_seq, cumulative_credit_total, True
            return
        if ack_seq == self.ack_seq and cumulative_credit_total == self.credit_total:
            return  # Identical replay, no extra credit.
        if (ack_seq <= self.ack_seq or ack_seq > produced
                or cumulative_credit_total < self.credit_total
                or cumulative_credit_total > ack_seq + self.capacity):
            _refuse("E_CREDIT", "regressing, duplicate-mutated, or inflated ACK")
        self.ack_seq, self.credit_total = ack_seq, cumulative_credit_total

    def require_credit(self, next_seq: int) -> None:
        if (not self.started or type(next_seq) is not int or next_seq <= 0
                or next_seq > self.credit_total):
            _refuse("E_CREDIT", "credit exhausted before grab")


@dataclass
class WireSession:
    """One-session receive state, including surplus bytes and sequenced handshake."""
    binding_sha256: bytes
    next_seq: int = 1
    hello_received: bool = False
    hello_confirmed: bool = False
    stopped: bool = False
    ended: bool = False
    pending: bytes = field(default=b"", repr=False)
    pending_limit: int = 0
    credits: Optional[CreditWindow] = None

    @classmethod
    def for_binding(cls, binding: dict, limits: WireLimits) -> "WireSession":
        info = validate_binding(binding, limits)
        return cls(binding_sha256=info.digest, pending_limit=min(info.max_record_bytes, limits.max_record_bytes),
                   credits=CreditWindow(info.max_inflight_frames))


def _verify_context(binding: dict, limits: WireLimits, session: WireSession) -> BindingInfo:
    if type(session) is not WireSession:
        _refuse("E_BINDING", "persistent WireSession required")
    info = validate_binding(binding, limits)
    if session.binding_sha256 != info.digest:
        _refuse("E_BINDING", "session/binding mismatch")
    if session.pending_limit <= 0 or session.pending_limit > min(info.max_record_bytes, limits.max_record_bytes):
        _refuse("E_BINDING", "session pending buffer cap invalid")
    return info


def _pull(reader: Any, n: int) -> bytes:
    fn = reader.read if hasattr(reader, "read") else reader
    if not callable(fn):
        _refuse("E_WIRE", "injected reader must be a callable or .read")
    chunk = fn(n)
    if type(chunk) not in (bytes, bytearray, memoryview):
        _refuse("E_WIRE", "reader returned nonbytes")
    return bytes(chunk)


def _read_exact(reader: Any, session: WireSession, n: int, *, boundary: bool = False) -> Optional[bytes]:
    """One bounded read, preserving oversized coalesced chunks for the next call."""
    if type(n) is not int or n < 0:
        _refuse("E_LENGTH", "invalid requested byte count")
    if n == 0:
        return b""
    parts = []
    available = session.pending
    session.pending = b""
    if available:
        parts.append(available[:n])
        session.pending = available[n:]
    got = sum(map(len, parts))
    while got < n:
        chunk = _pull(reader, n - got)
        if not chunk:
            if boundary and got == 0:
                return None
            _refuse("E_TORN", "partial EOF inside a wire record")
        needed = n - got
        parts.append(chunk[:needed])
        got += min(len(chunk), needed)
        if len(chunk) > needed:
            if len(session.pending) + len(chunk) - needed > session.pending_limit:
                _refuse("E_LENGTH", "coalesced surplus exceeds bounded buffer")
            session.pending += chunk[needed:]
    return b"".join(parts)


def _xwd_prefix(prefix: bytes, length: int, width: int, height: int) -> None:
    """Apply cheap W8U subset checks before allocating the remaining XWD body."""
    if len(prefix) != XWD_FIXED_HEADER.size:
        _refuse("E_LENGTH", "short fixed XWD prefix")
    f = XWD_FIXED_HEADER.unpack(prefix)
    header_size, version, fmt, depth, w, h = f[:6]
    pixel_order, bit_order, bpp, stride, visual = f[7], f[9], f[11], f[12], f[13]
    masks, ncolors = f[14:17], f[19]
    if version != 7 or fmt != 2 or depth != 24 or bpp != 32:
        _refuse("E_WIRE", "unsupported XWD format")
    if pixel_order not in (0, 1) or bit_order not in (0, 1):
        _refuse("E_WIRE", "invalid pixel or bitmap bit order")
    if visual not in (4, 5) or masks != (0xFF0000, 0xFF00, 0xFF):
        _refuse("E_WIRE", "invalid XWD visual/masks")
    if w != width or h != height or not (1 <= w <= 16384 and 1 <= h <= 16384):
        _refuse("E_GEOMETRY", "inner XWD geometry differs")
    if not (100 <= header_size <= MAX_XWD_HEADER) or ncolors > MAX_XWD_COLORS:
        _refuse("E_LENGTH", "XWD header or colormap too large")
    if stride < 4 * w or stride % 4 or stride > MAX_XWD_STRIDE:
        _refuse("E_GEOMETRY", "XWD stride outside subset")
    if header_size + ncolors * 12 + h * stride != length:
        _refuse("E_LENGTH", "XWD declared size not exact")


def _read_xwd(reader: Any, session: WireSession, n: int, info: BindingInfo) -> bytes:
    max_xwd = 4 * info.width * info.height + (1 << 20)
    if type(n) is not int or n < 100 or n > max_xwd:
        _refuse("E_LENGTH", "inner XWD exceeds geometry bound")
    prefix = _read_exact(reader, session, XWD_FIXED_HEADER.size)
    _xwd_prefix(prefix, n, info.width, info.height)
    return prefix + _read_exact(reader, session, n - len(prefix))


def _rgb_hash(raw: bytes, info: BindingInfo, limits: WireLimits) -> bytes:
    try:
        h = xfb.validate_frame(raw, info.width, info.height)
        return hashlib.sha256(xfb.rgb24(raw, h)).digest()
    except xfb.XwdError as exc:
        code = limits.xwd_error_codes.get(exc.code)
        if code is None:
            raise WireError("E_WIRE", "untranslated native XWD error; no success fallback") from exc
        raise WireError(code, "native XWD validation refused") from exc


def decode_record(reader: Any, *, binding: dict, limits: WireLimits,
                  session: WireSession) -> Optional[PairRecord]:
    """Read one W8UF frame or None at clean record-boundary EOF.

    Unlike HELLO, `session.hello_confirmed` requires explicit verified W8UH
    acknowledgement (see parse_control). Incomplete EOF always refuses.
    """
    info = _verify_context(binding, limits, session)
    if not session.hello_confirmed or session.stopped or session.ended:
        _refuse("E_BINDING", "authenticated HELLO confirmation required")
    magic = _read_exact(reader, session, 4, boundary=True)
    if magic is None:
        return None
    if magic != FRAME_MAGIC:
        _refuse("E_WIRE", "unexpected wire magic, never resynchronise")
    head = magic + _read_exact(reader, session, FRAME_HEADER.size - 4)
    (_, version, header_bytes, record_bytes, seq, tick, mono,
     width, height, stride, fmt, flags, payload_bytes,
     advertised_hash, wire_binding_hash) = FRAME_HEADER.unpack(head)
    if version != VERSION or header_bytes != FRAME_HEADER.size:
        _refuse("E_WIRE", "bad frame version/header size")
    if flags & ~3 or not flags & FLAG_PAIR_UNDER_GRAB or fmt != PIXEL_RGB24:
        _refuse("E_WIRE", "forged pixel format or flags")
    if (width != info.width or height != info.height or
            stride != 3 * width):
        _refuse("E_GEOMETRY", "outer frame geometry differs")
    if seq != session.next_seq or seq == 0:
        _refuse("E_SEQ", "duplicate/gap/rollover")
    if session.credits is None:
        _refuse("E_CREDIT", "no negotiated reserved capacity")
    session.credits.require_credit(seq)  # HELLO itself grants zero credit.
    if wire_binding_hash != info.digest:
        _refuse("E_BINDING", "wire binding digest mismatch")
    tick_valid = bool(flags & FLAG_TICK_VALID)
    if ((info.tick_mode == "committed" and not tick_valid)
            or (info.tick_mode == "unavailable" and (tick_valid or tick != 0))):
        _refuse("E_TICK", "invalid negotiated tick semantics")
    geometry_cap = 128 + 16 + 2 * (4 * width * height + (1 << 20))
    max_bytes = min(limits.max_record_bytes, info.max_record_bytes, geometry_cap)
    if (record_bytes < 128 + 16 + 200 or record_bytes > max_bytes
            or payload_bytes != record_bytes - FRAME_HEADER.size):
        _refuse("E_LENGTH", "record length/geometry/cap disagreement")
    len_a_raw = _read_exact(reader, session, U64.size)
    len_a = U64.unpack(len_a_raw)[0]
    if len_a > payload_bytes - 16:
        _refuse("E_LENGTH", "first XWD length exceeds payload")
    xwd_a = _read_xwd(reader, session, len_a, info)
    len_b = U64.unpack(_read_exact(reader, session, U64.size))[0]
    if len_a + len_b + 16 != payload_bytes:
        _refuse("E_LENGTH", "second XWD length or trailing bytes")
    xwd_b = _read_xwd(reader, session, len_b, info)
    if _rgb_hash(xwd_a, info, limits) != advertised_hash or _rgb_hash(xwd_b, info, limits) != advertised_hash:
        _refuse("E_HASH", "canonical RGB24 pair hash mismatch")
    session.next_seq += 1
    return PairRecord(seq=seq, tick=tick if tick_valid else None,
                      tick_valid=tick_valid, capture_mono_ns=mono,
                      width=width, height=height, stride=stride,
                      pixel_format=fmt, sha256=advertised_hash.hex(),
                      binding_sha256=wire_binding_hash.hex(),
                      xwd_a=xwd_a, xwd_b=xwd_b)


def _contract_field(contract: Any, name: str) -> Any:
    if contract is None:
        _refuse("E_WIRE", "accepted external control contract is absent")
    value = contract.get(name) if type(contract) is dict else getattr(contract, name, None)
    if value is None:
        _refuse("E_WIRE", f"missing accepted control field: {name}")
    return value


def _verified(callback: Any, *args: Any) -> bool:
    if not callable(callback):
        return False
    try:
        return callback(*args) is True
    except Exception:
        return False


def parse_control(reader: Any, *, binding: dict, limits: WireLimits,
                  session: WireSession, verify_hello: Optional[Callable] = None,
                  verify_confirmation: Optional[Callable] = None,
                  contract: Any = None, verify_end_exit: Optional[Callable] = None,
                  verify_ack_release: Optional[Callable] = None) -> Optional[ControlRecord]:
    """Parse one control, refusing undefined confirmation/END wire constants.

    An injected verifier independently authenticates the grant/capability;
    the digest is only a binding consistency check. For END, a separate
    callback must confirm joined exit and EOF. No control magic resync.
    """
    info = _verify_context(binding, limits, session)
    if session.ended:
        _refuse("E_WIRE", "control received after terminal END")
    magic = _read_exact(reader, session, 4, boundary=True)
    if magic is None:
        return None
    if magic not in (CONTROL_MAGIC, CONFIRM_MAGIC, END_MAGIC):
        _refuse("E_WIRE", "unknown control magic")
    head = magic + _read_exact(reader, session, CONTROL_HEADER.size - 4)
    _, ver, kind, n, digest = CONTROL_HEADER.unpack(head)
    if ver != VERSION or digest != info.digest:
        _refuse("E_BINDING" if digest != info.digest else "E_WIRE", "bad control version or digest")
    if n > limits.max_control_bytes:
        _refuse("E_LENGTH", "control allocation exceeds cap")
    if magic == CONTROL_MAGIC:
        if kind == HELLO_KIND:
            if session.hello_received or n > MAX_HELLO_BYTES or n == 0:
                _refuse("E_WIRE", "duplicate/oversized/empty HELLO")
            body = _read_exact(reader, session, n)
            if body != info.canonical or _decode_canonical_json(body) != binding:
                _refuse("E_BINDING", "HELLO binding is noncanonical/mismatched")
            if not _verified(verify_hello, binding, info.digest):
                _refuse("E_BINDING", "HELLO grant/capability not independently authenticated")
            session.hello_received = True
        elif kind == ACK_KIND:
            if not session.hello_confirmed or n != ACK_BODY.size or session.credits is None:
                _refuse("E_CREDIT", "ACK size/handshake invalid")
            body = _read_exact(reader, session, n)
            ack, credit = ACK_BODY.unpack(body)
            if ack and not _verified(verify_ack_release, ack, binding):
                _refuse("E_CREDIT", "ACK requires validated publication and lease release")
            session.credits.accept(ack, credit, produced=session.next_seq - 1)
            return ControlRecord(magic, kind, digest.hex(), body, ack_seq=ack,
                                 cumulative_credit_total=credit)
        elif kind == STOP_KIND:
            if n:
                _refuse("E_WIRE", "STOP must have empty body")
            body = b""
            session.stopped = True  # Bypasses full queues.
        else:
            _refuse("E_WIRE", "unknown W8UC kind")
    elif magic == CONFIRM_MAGIC:
        allowed_kind = _contract_field(contract, "confirmation_kind")
        if type(allowed_kind) is not int or kind != allowed_kind or not session.hello_received or session.hello_confirmed:
            _refuse("E_WIRE", "unrecognised/out-of-order confirmation")
        body = _read_exact(reader, session, n)
        if not _verified(verify_confirmation, body, binding, info.digest):
            _refuse("E_BINDING", "HELLO confirmation not authenticated")
        session.hello_confirmed = True
    else:
        end_kind = _contract_field(contract, "end_kind")
        end_codes = _contract_field(contract, "end_error_codes")
        if (type(end_kind) is not int or kind != end_kind
                or type(end_codes) not in (dict, frozenset, set)
                or n != END_BODY.size or not session.hello_confirmed):
            _refuse("E_WIRE", "unknown END kind/size or handshake")
        body = _read_exact(reader, session, n)
        last_seq, sent, code = END_BODY.unpack(body)
        if code not in end_codes or type(code) is not int:
            _refuse("E_WIRE", "unknown END numeric error")
        # The END record is terminal; never mistake trailing data for a new frame.
        if _read_exact(reader, session, 1, boundary=True) is not None:
            _refuse("E_WIRE", "trailing data after END")
        if not _verified(verify_end_exit, last_seq, sent, code):
            _refuse("E_WIRE", "reader exit/join not authenticated")
        session.ended = True
        return ControlRecord(magic, kind, digest.hex(), body,
                             end_last_seq=last_seq, end_sent=sent, end_error_code=code)
    return ControlRecord(magic, kind, digest.hex(), body)
