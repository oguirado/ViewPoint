"""vadapt.w8u_digest - W8U canonical RGB24 and closed frame-metadata hashing owner (w8u-ve-digest).

Implements:
- Revision-bound w8u-os-contract evidence gate (uninjected/pending prerequisites remain AUSENTE; no local defaults).
- W8U XWD subset wrapper validation around vadapt.xfb with explicit native-to-W8U error translation.
- Canonical top-down RGB24 pixel extraction and SHA-256 hashing (excluding header, colormap, row padding, and X/alpha).
- Separately named optional RGBA8 engine comparison helper (never interchangeable with canonical RGB24 wire digests).
- Synthetic little-endian BGRX and big-endian XRGB XWD builders with distinct headers/padding and identical RGB24 pixels.
- W8UF unsigned big-endian 128-byte frame record validation, cap enforcement before body allocation, and double-read XWD A/B verification.
- Closed binding validation and canonical integer-only UTF-8 JSON hashing with root/child confinement and independent grant authentication.
- Closed frame-metadata canonicalization and hashing (excluding floats, bool/int tricks, unknown keys, pixels, base64, data URLs, deltas).
- Hash-domain separation across RGB24 pixel, UI/frame-metadata, binding, and simulation domains.
- U5 w8u.receipt/v1 vision metadata projection without fabricating foreign action/T2/STATE receipts.
- Control frame validation (W8UC HELLO/ACK/STOP, contract-bound W8UH confirmation and W8UE END) and credit window state machine.
"""
from __future__ import annotations
import hashlib
import json
import os
import posixpath
import re
import struct
from collections import namedtuple
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from vadapt import xfb

AUSENTE = "AUSENTE"
STATUS_PREPARED = "PREPARED"
STATUS_ACCEPTED = "ACCEPTED"

RECEIPT_SCHEMA_V1 = "w8u.receipt/v1"
REQUIRED_FRAMES_ROOT = "/dev/shm/vqa-ramws"
REQUIRED_RUN_CHILD_PREFIX = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"

W8UF_MAGIC = b"W8UF"
W8UF_VERSION = 1
W8UF_HEADER_BYTES = 128
W8UF_PIXEL_FORMAT_RGB24 = 1
W8UF_FLAG_PAIR_UNDER_GRAB = 0x0001
W8UF_FLAG_TICK_VALID = 0x0002
W8UF_ALLOWED_FLAGS_MASK = W8UF_FLAG_PAIR_UNDER_GRAB | W8UF_FLAG_TICK_VALID
W8UF_HEADER_STRUCT = struct.Struct(">4sHHQQQQIIIHHQ32s32s")
W8UF_INNER_LENGTH_BYTES = 16
W8UF_GEOMETRY_SLACK_BYTES = 1024 * 1024

W8UC_MAGIC = b"W8UC"
W8UH_MAGIC = b"W8UH"
W8UE_MAGIC = b"W8UE"
CONTROL_HEADER_STRUCT = struct.Struct(">4sHHI32s")
CONTROL_HEADER_BYTES = CONTROL_HEADER_STRUCT.size
HELLO_MAX_BODY_BYTES = 16 * 1024
CONTROL_KIND_HELLO = 1
CONTROL_KIND_ACK = 2
CONTROL_KIND_STOP = 3
PREFERRED_INITIAL_CREDIT_GRANT = 2
MAX_U64 = (1 << 64) - 1
MAX_U32 = (1 << 32) - 1

CLOSED_ERRORS: Tuple[str, ...] = (
    "E_TORN",
    "E_WIRE",
    "E_LENGTH",
    "E_HASH",
    "E_SEQ",
    "E_TICK",
    "E_BINDING",
    "E_GEOMETRY",
    "E_CREDIT",
    "E_BACKPRESSURE",
    "E_TIMEOUT",
    "E_ROOT",
    "E_RAM_QUOTA",
)

NATIVE_TO_W8U_ERROR_MAP: Dict[str, str] = {
    "E_XWD_SHORT": "E_LENGTH",
    "E_XWD_SIZE": "E_LENGTH",
    "E_XWD_LIMIT": "E_LENGTH",
    "E_XWD_VERSION": "E_WIRE",
    "E_XWD_FORMAT": "E_WIRE",
    "E_XWD_BYTE_ORDER": "E_WIRE",
    "E_XWD_VISUAL": "E_WIRE",
    "E_XWD_TRIES": "E_WIRE",
    "E_XWD_SOURCE": "E_WIRE",
    "E_XWD_GEOMETRY": "E_GEOMETRY",
    "E_TORN": "E_TORN",
}

BINDING_KEYS: Tuple[str, ...] = (
    "schema",
    "run_id",
    "lease_id",
    "seat_grant_sha256",
    "container_id",
    "target_handle",
    "epoch",
    "display",
    "xid",
    "width",
    "height",
    "frames_root",
    "frame_run_root",
    "image_sha256",
    "reader_sha256",
    "exec_allowlist_sha256",
    "max_record_bytes",
    "max_inflight_frames",
    "tick_mode",
)

TARGET_HANDLE_KEYS: Tuple[str, ...] = (
    "kind",
    "canonical_id",
    "generation",
)

FRAME_METADATA_KEYS: Tuple[str, ...] = (
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

FORBIDDEN_METADATA_SUBSTRINGS: Tuple[str, ...] = (
    "data:image/",
    ";base64,",
    "base64,",
)

FORBIDDEN_METADATA_KEY_TOKENS: Tuple[str, ...] = (
    "pixel",
    "pixels",
    "rgb24_bytes",
    "rgba8_bytes",
    "bgrx",
    "xrgb",
    "xwd",
    "base64",
    "data_url",
    "delta",
    "patch",
    "raw_frame",
)

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

W8UFFrameHeader = namedtuple(
    "W8UFFrameHeader",
    (
        "magic",
        "version",
        "header_bytes",
        "record_bytes",
        "seq",
        "tick",
        "capture_mono_ns",
        "width",
        "height",
        "stride",
        "pixel_format",
        "flags",
        "payload_bytes",
        "sha256_rgb24",
        "binding_sha256",
        "pair_under_grab",
        "tick_valid",
    ),
)

ValidatedW8UFrame = namedtuple(
    "ValidatedW8UFrame",
    (
        "header",
        "rgb24",
        "sha256_rgb24_raw",
        "sha256_rgb24_hex",
        "xwd_a_header",
        "xwd_b_header",
        "frame_metadata",
        "frame_metadata_sha256_hex",
    ),
)

ControlFrame = namedtuple(
    "ControlFrame",
    (
        "magic",
        "version",
        "kind",
        "body_bytes",
        "binding_sha256",
        "body",
        "parsed",
    ),
)


class W8UDigestError(Exception):
    """Closed W8U error with canonical predicate code and optional numeric contract code."""

    def __init__(self, code: str, msg: str = "", *, numeric_code: Optional[int] = None) -> None:
        if code != AUSENTE and code not in CLOSED_ERRORS:
            raise ValueError(f"unrecognized W8U error code: {code!r}")
        super().__init__(f"{code} {msg}".strip())
        self.code = code
        self.msg = msg
        self.numeric_code = numeric_code


def _is_exact_int(val: Any) -> bool:
    return type(val) is int


def _is_exact_bool(val: Any) -> bool:
    return type(val) is bool


def _is_hex64(val: Any) -> bool:
    return isinstance(val, str) and bool(HEX64_RE.match(val))


def _ensure_bytes_like(buf: Any, error_code: str = "E_WIRE") -> memoryview:
    if not isinstance(buf, (bytes, bytearray, memoryview)):
        raise W8UDigestError(error_code, "expected byte buffer")
    mv = memoryview(buf)
    if mv.ndim != 1 or mv.itemsize != 1:
        raise W8UDigestError(error_code, "expected 1-D byte memoryview")
    return mv


class ContractEvidence:
    """Revision-bound w8u-os-contract evidence container.

    Any omitted field remains AUSENTE; no local defaults are fabricated.
    """

    REQUIRED_FIELDS: Tuple[str, ...] = (
        "revision",
        "accepted",
        "binding_schema_id",
        "action_result_schema_id",
        "control_specs",
        "end_error_codes",
        "error_code_numbers",
        "digest_domains",
        "xwd_bounds",
        "cross_language_vectors",
        "receipt_closed_fields",
    )

    def __init__(
        self,
        *,
        revision: Any = AUSENTE,
        accepted: Any = False,
        binding_schema_id: Any = AUSENTE,
        action_result_schema_id: Any = AUSENTE,
        control_specs: Any = AUSENTE,
        end_error_codes: Any = AUSENTE,
        error_code_numbers: Any = AUSENTE,
        digest_domains: Any = AUSENTE,
        xwd_bounds: Any = AUSENTE,
        cross_language_vectors: Any = AUSENTE,
        receipt_closed_fields: Any = AUSENTE,
    ) -> None:
        self.revision = revision
        self.accepted = accepted
        self.binding_schema_id = binding_schema_id
        self.action_result_schema_id = action_result_schema_id
        self.control_specs = control_specs
        self.end_error_codes = end_error_codes
        self.error_code_numbers = error_code_numbers
        self.digest_domains = digest_domains
        self.xwd_bounds = xwd_bounds
        self.cross_language_vectors = cross_language_vectors
        self.receipt_closed_fields = receipt_closed_fields

    @property
    def readiness_status(self) -> str:
        try:
            self.require_accepted()
            return STATUS_ACCEPTED
        except W8UDigestError:
            return STATUS_PREPARED

    def require_accepted(self, *required_fields: str) -> "ContractEvidence":
        if not _is_exact_bool(self.accepted) or not self.accepted:
            raise W8UDigestError(AUSENTE, "w8u-os-contract evidence is not accepted (PREPARED only)")
        if not isinstance(self.revision, str) or not self.revision.strip() or self.revision == AUSENTE:
            raise W8UDigestError(AUSENTE, "w8u-os-contract revision is AUSENTE")
        fields_to_check = required_fields or self.REQUIRED_FIELDS
        for field in fields_to_check:
            val = getattr(self, field, AUSENTE)
            if val is None or val == AUSENTE:
                raise W8UDigestError(AUSENTE, f"w8u-os-contract field {field} is AUSENTE")
        return self


def require_contract_evidence(
    contract: Any = AUSENTE,
    *required_fields: str,
) -> ContractEvidence:
    """Validate that accepted revision-bound w8u-os-contract evidence was injected."""
    if contract is None or contract == AUSENTE:
        raise W8UDigestError(AUSENTE, "w8u-os-contract evidence is AUSENTE")
    if isinstance(contract, Mapping):
        contract = ContractEvidence(**{k: contract.get(k, AUSENTE) for k in ContractEvidence.REQUIRED_FIELDS})
    if not isinstance(contract, ContractEvidence):
        raise W8UDigestError(AUSENTE, "invalid w8u-os-contract evidence object")
    return contract.require_accepted(*required_fields)


def numeric_error_code(code: str, contract: Any = AUSENTE) -> int:
    """Resolve a closed W8U error symbol to its contract-frozen numeric code."""
    if code not in CLOSED_ERRORS:
        raise W8UDigestError("E_WIRE", f"unknown W8U error symbol {code!r}")
    ev = require_contract_evidence(contract, "error_code_numbers")
    mapping = ev.error_code_numbers
    if not isinstance(mapping, Mapping) or code not in mapping:
        raise W8UDigestError(AUSENTE, f"numeric assignment for {code} is AUSENTE")
    num = mapping[code]
    if not _is_exact_int(num) or num < 0 or num > MAX_U32:
        raise W8UDigestError(AUSENTE, f"invalid numeric error code for {code}")
    return num


def translate_native_xwd_error(exc: BaseException) -> W8UDigestError:
    """Explicit native-to-W8U error translation for vadapt.xfb.XwdError."""
    if isinstance(exc, W8UDigestError):
        return exc
    if isinstance(exc, xfb.XwdError):
        native_code = getattr(exc, "code", "")
        if native_code == "E_XWD_SHORT":
            msg = str(exc)
            if "header_size" in msg:
                return W8UDigestError("E_WIRE", f"native {native_code}: {exc}")
            return W8UDigestError("E_LENGTH", f"native {native_code}: {exc}")
        mapped = NATIVE_TO_W8U_ERROR_MAP.get(native_code, "E_WIRE")
        return W8UDigestError(mapped, f"native {native_code}: {exc}")
    return W8UDigestError("E_WIRE", f"unexpected XWD failure: {exc}")


def _extract_xwd_bounds(contract: Any) -> Dict[str, int]:
    if contract is None or contract == AUSENTE:
        return {
            "max_header_size": 4096,
            "max_ncolors": 256,
            "max_row_pad": 4096,
        }
    ev = require_contract_evidence(contract, "xwd_bounds")
    bounds = ev.xwd_bounds
    if not isinstance(bounds, Mapping):
        raise W8UDigestError(AUSENTE, "xwd_bounds must be a mapping")
    for k in ("max_header_size", "max_ncolors", "max_row_pad"):
        if k not in bounds or not _is_exact_int(bounds[k]) or bounds[k] < 0:
            raise W8UDigestError(AUSENTE, f"xwd_bounds[{k!r}] is AUSENTE or invalid")
    if bounds["max_header_size"] < xfb.MIN_HEADER:
        raise W8UDigestError(AUSENTE, "max_header_size smaller than MIN_HEADER")
    return {
        "max_header_size": bounds["max_header_size"],
        "max_ncolors": bounds["max_ncolors"],
        "max_row_pad": bounds["max_row_pad"],
    }


def validate_w8u_xwd(
    buf: Any,
    width: Optional[int] = None,
    height: Optional[int] = None,
    *,
    contract: Any = None,
    require_contract: bool = False,
) -> xfb.Header:
    """Validate a W8U XWD buffer using wrapper bounds before vadapt.xfb.validate_frame."""
    if require_contract:
        require_contract_evidence(contract, "xwd_bounds")
    bounds = _extract_xwd_bounds(contract)
    mv = _ensure_bytes_like(buf, "E_WIRE")
    if width is not None and (not _is_exact_int(width) or width <= 0 or width > xfb.MAX_SIDE):
        raise W8UDigestError("E_GEOMETRY", "invalid expected width")
    if height is not None and (not _is_exact_int(height) or height <= 0 or height > xfb.MAX_SIDE):
        raise W8UDigestError("E_GEOMETRY", "invalid expected height")

    if len(mv) < xfb.MIN_HEADER:
        raise W8UDigestError("E_LENGTH", f"XWD shorter than {xfb.MIN_HEADER} bytes")

    f = struct.unpack(f">{xfb.HEADER_FIELDS}I", bytes(mv[:xfb.MIN_HEADER]))
    hs, ver, fmt, depth, w, h, xoffset = f[0], f[1], f[2], f[3], f[4], f[5], f[6]
    byte_order, bitmap_unit, bitmap_bit_order, bitmap_pad = f[7], f[8], f[9], f[10]
    bpp, bpl, vclass = f[11], f[12], f[13]
    rmask, gmask, bmask = f[14], f[15], f[16]
    bits_per_rgb, colormap_entries, ncolors = f[17], f[18], f[19]
    win_w, win_h, win_x, win_y, win_border = f[20], f[21], f[22], f[23], f[24]

    if ver != xfb.XWD_VERSION:
        raise W8UDigestError("E_WIRE", f"unsupported XWD version {ver}")
    if hs < xfb.MIN_HEADER or hs > bounds["max_header_size"]:
        raise W8UDigestError("E_WIRE", f"header_size {hs} outside [{xfb.MIN_HEADER}, {bounds['max_header_size']}]")
    if fmt != xfb.ZPIXMAP or depth != 24 or bpp != 32:
        raise W8UDigestError("E_WIRE", f"unsupported format={fmt} depth={depth} bpp={bpp}")
    if byte_order not in (0, 1) or bitmap_bit_order not in (0, 1):
        raise W8UDigestError("E_WIRE", f"invalid byte_order={byte_order} bitmap_bit_order={bitmap_bit_order}")
    if bitmap_unit != 32 or bitmap_pad != 32 or bits_per_rgb != 8:
        raise W8UDigestError(
            "E_WIRE",
            f"invalid bitmap_unit={bitmap_unit} bitmap_pad={bitmap_pad} bits_per_rgb={bits_per_rgb}",
        )
    if xoffset != 0 or win_border != 0:
        raise W8UDigestError("E_GEOMETRY", f"non-zero xoffset={xoffset} or border={win_border}")
    if vclass not in (xfb.TRUECOLOR, xfb.DIRECTCOLOR) or (rmask, gmask, bmask) != (0xFF0000, 0xFF00, 0xFF):
        raise W8UDigestError("E_WIRE", "invalid visual_class or RGB masks")
    if ncolors > bounds["max_ncolors"] or colormap_entries > bounds["max_ncolors"]:
        raise W8UDigestError("E_WIRE", f"ncolors={ncolors} exceeds cap {bounds['max_ncolors']}")
    if not (0 < w <= xfb.MAX_SIDE and 0 < h <= xfb.MAX_SIDE):
        raise W8UDigestError("E_GEOMETRY", f"invalid dimensions {w}x{h}")
    if win_w not in (0, w) or win_h not in (0, h):
        raise W8UDigestError("E_GEOMETRY", f"window geometry {win_w}x{win_h} mismatches pixmap {w}x{h}")
    min_bpl = w * 4
    max_bpl = min_bpl + bounds["max_row_pad"]
    if bpl < min_bpl or (bpl % 4) != 0 or bpl > max_bpl:
        raise W8UDigestError("E_GEOMETRY", f"invalid bytes_per_line {bpl} for width {w}")

    expected_total = hs + ncolors * xfb.COLOR_ENTRY + h * bpl
    if len(mv) != expected_total:
        raise W8UDigestError("E_LENGTH", f"XWD byte length {len(mv)} != expected {expected_total}")

    try:
        hd = xfb.validate_frame(mv, width=width, height=height)
    except xfb.XwdError as exc:
        raise translate_native_xwd_error(exc) from exc
    return hd


def canonical_rgb24_bytes(
    buf: Any,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    contract: Any = None,
    require_contract: bool = False,
) -> bytes:
    """Validate an XWD buffer and return canonical top-down RGB24 pixel bytes (width * height * 3)."""
    hd = validate_w8u_xwd(buf, width=width, height=height, contract=contract, require_contract=require_contract)
    try:
        rgb = xfb.rgb24(buf, hd)
    except xfb.XwdError as exc:
        raise translate_native_xwd_error(exc) from exc
    if len(rgb) != hd.width * hd.height * 3:
        raise W8UDigestError("E_LENGTH", "canonical RGB24 output size mismatch")
    return rgb


def rgb24_sha256_raw(
    buf: Any,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    contract: Any = None,
    require_contract: bool = False,
) -> bytes:
    """Return the 32-byte raw SHA-256 digest of canonical RGB24 pixels from an XWD frame."""
    hd = validate_w8u_xwd(buf, width=width, height=height, contract=contract, require_contract=require_contract)
    try:
        hex_digest = xfb.frame_sha256(buf, hd)
    except xfb.XwdError as exc:
        raise translate_native_xwd_error(exc) from exc
    return bytes.fromhex(hex_digest)


def rgb24_sha256_hex(
    buf: Any,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    contract: Any = None,
    require_contract: bool = False,
) -> str:
    """Return the 64-character lowercase hex SHA-256 digest of canonical RGB24 pixels from an XWD frame."""
    return rgb24_sha256_raw(
        buf,
        width=width,
        height=height,
        contract=contract,
        require_contract=require_contract,
    ).hex()


def validate_raw_rgb24_buffer(
    buf: Any,
    *,
    width: int,
    height: int,
    stride: int,
    pixel_format: int = W8UF_PIXEL_FORMAT_RGB24,
    orientation: str = "top_down",
    binding_sha256: Optional[Union[bytes, str]] = None,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
    expected_rgb24_sha256: Optional[Union[bytes, str]] = None,
) -> bytes:
    """Validate an advertised raw RGB24 buffer, refusing mislabelled BGRX, wrong stride, or wrong orientation."""
    mv = _ensure_bytes_like(buf, "E_WIRE")
    if not _is_exact_int(width) or not _is_exact_int(height) or width <= 0 or height <= 0:
        raise W8UDigestError("E_GEOMETRY", "width and height must be positive exact integers")
    if width > xfb.MAX_SIDE or height > xfb.MAX_SIDE:
        raise W8UDigestError("E_GEOMETRY", "width or height exceeds MAX_SIDE")
    if not _is_exact_int(pixel_format) or pixel_format != W8UF_PIXEL_FORMAT_RGB24:
        raise W8UDigestError("E_WIRE", f"unsupported pixel_format {pixel_format!r}; require RGB24 (1)")
    if orientation != "top_down":
        raise W8UDigestError("E_GEOMETRY", f"unsupported orientation {orientation!r}; require top_down")
    if not _is_exact_int(stride) or stride != width * 3:
        raise W8UDigestError("E_GEOMETRY", f"stride {stride} != width * 3 ({width * 3})")
    expected_len = height * stride
    if len(mv) != expected_len:
        raise W8UDigestError("E_LENGTH", f"raw RGB24 length {len(mv)} != {expected_len}")

    if binding_sha256 is None:
        raise W8UDigestError("E_BINDING", "missing binding_sha256")
    norm_binding = _normalize_digest_bytes(binding_sha256, "E_BINDING")
    if expected_binding_sha256 is not None:
        norm_expected_binding = _normalize_digest_bytes(expected_binding_sha256, "E_BINDING")
        if norm_binding != norm_expected_binding:
            raise W8UDigestError("E_BINDING", "binding_sha256 mismatch")

    raw_bytes = bytes(mv)
    actual_digest = hashlib.sha256(raw_bytes).digest()
    if expected_rgb24_sha256 is not None:
        norm_expected_rgb = _normalize_digest_bytes(expected_rgb24_sha256, "E_HASH")
        if actual_digest != norm_expected_rgb:
            raise W8UDigestError("E_HASH", "raw buffer digest does not match expected canonical RGB24 digest")
    return raw_bytes


def rgba8_from_rgb24(rgb24_buf: Any, width: int, height: int) -> bytes:
    """Expand canonical RGB24 bytes into tightly packed RGBA8 bytes (alpha=0xFF) for optional engine comparison."""
    mv = _ensure_bytes_like(rgb24_buf, "E_WIRE")
    if not _is_exact_int(width) or not _is_exact_int(height) or width <= 0 or height <= 0:
        raise W8UDigestError("E_GEOMETRY", "invalid dimensions for RGBA8 conversion")
    expected = width * height * 3
    if len(mv) != expected:
        raise W8UDigestError("E_LENGTH", f"RGB24 buffer length {len(mv)} != {expected}")
    pixel_count = width * height
    out = bytearray(pixel_count * 4)
    out[0::4] = mv[0::3]
    out[1::4] = mv[1::3]
    out[2::4] = mv[2::3]
    out[3::4] = b"\xff" * pixel_count
    return bytes(out)


def rgba8_from_xwd(
    buf: Any,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    contract: Any = None,
    require_contract: bool = False,
) -> bytes:
    """Convert a validated XWD frame into RGBA8 (alpha=0xFF), stripping header, padding, and X channel."""
    hd = validate_w8u_xwd(buf, width=width, height=height, contract=contract, require_contract=require_contract)
    rgb = canonical_rgb24_bytes(buf, width=hd.width, height=hd.height, contract=contract, require_contract=require_contract)
    return rgba8_from_rgb24(rgb, hd.width, hd.height)


def compare_rgba8_engine_buffer(
    xwd_buf: Any,
    engine_rgba8_buf: Any,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    contract: Any = None,
    require_contract: bool = False,
) -> Dict[str, Any]:
    """Separately named optional RGBA8 engine comparison helper.

    Never substitutes RGBA8 for the canonical RGB24 wire/metadata digest.
    """
    hd = validate_w8u_xwd(xwd_buf, width=width, height=height, contract=contract, require_contract=require_contract)
    rgb = canonical_rgb24_bytes(
        xwd_buf,
        width=hd.width,
        height=hd.height,
        contract=contract,
        require_contract=require_contract,
    )
    expected_rgba8 = rgba8_from_rgb24(rgb, hd.width, hd.height)
    mv_engine = _ensure_bytes_like(engine_rgba8_buf, "E_WIRE")
    if len(mv_engine) != len(expected_rgba8):
        raise W8UDigestError("E_LENGTH", f"RGBA8 engine buffer length {len(mv_engine)} != {len(expected_rgba8)}")
    engine_bytes = bytes(mv_engine)
    rgb_hex = hashlib.sha256(rgb).hexdigest()
    expected_rgba_hex = hashlib.sha256(expected_rgba8).hexdigest()
    actual_rgba_hex = hashlib.sha256(engine_bytes).hexdigest()
    if engine_bytes != expected_rgba8:
        raise W8UDigestError("E_HASH", "optional RGBA8 engine buffer does not match canonical RGB24 pixels")
    return {
        "match": True,
        "width": hd.width,
        "height": hd.height,
        "rgb24_sha256": rgb_hex,
        "rgba8_engine_sha256": actual_rgba_hex,
        "expected_rgba8_sha256": expected_rgba_hex,
    }


def _resolve_pixel_source(
    width: int,
    height: int,
    pixels: Union[Callable[[int, int], Tuple[int, int, int]], bytes, bytearray, memoryview, Sequence[Tuple[int, int, int]]],
) -> bytes:
    expected_len = width * height * 3
    if isinstance(pixels, (bytes, bytearray, memoryview)):
        raw = bytes(pixels)
        if len(raw) != expected_len:
            raise W8UDigestError("E_LENGTH", f"literal RGB24 pixel bytes length {len(raw)} != {expected_len}")
        return raw
    if callable(pixels):
        out = bytearray(expected_len)
        idx = 0
        for y in range(height):
            for x in range(width):
                r, g, b = pixels(x, y)
                if not (_is_exact_int(r) and _is_exact_int(g) and _is_exact_int(b)):
                    raise W8UDigestError("E_WIRE", "pixel callable must return exact integers")
                if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
                    raise W8UDigestError("E_WIRE", "pixel channel out of byte range")
                out[idx : idx + 3] = bytes((r, g, b))
                idx += 3
        return bytes(out)
    out = bytearray(expected_len)
    if len(pixels) != width * height:
        raise W8UDigestError("E_LENGTH", "pixel sequence length does not match width * height")
    idx = 0
    for r, g, b in pixels:
        out[idx : idx + 3] = bytes((r, g, b))
        idx += 3
    return bytes(out)


def _normalize_digest_bytes(val: Union[bytes, bytearray, memoryview, str], error_code: str = "E_WIRE") -> bytes:
    if isinstance(val, str):
        if not _is_hex64(val):
            raise W8UDigestError(error_code, "digest hex must be 64 lowercase hex characters")
        return bytes.fromhex(val)
    mv = _ensure_bytes_like(val, error_code)
    if len(mv) != 32:
        raise W8UDigestError(error_code, f"raw digest must be 32 bytes, got {len(mv)}")
    return bytes(mv)


