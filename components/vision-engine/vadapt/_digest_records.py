"""w8u_digest: digest records."""
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

from ._digest_pixels import (
    AUSENTE, STATUS_PREPARED, STATUS_ACCEPTED, RECEIPT_SCHEMA_V1, REQUIRED_FRAMES_ROOT,
    REQUIRED_RUN_CHILD_PREFIX, W8UF_MAGIC, W8UF_VERSION, W8UF_HEADER_BYTES,
    W8UF_PIXEL_FORMAT_RGB24, W8UF_FLAG_PAIR_UNDER_GRAB, W8UF_FLAG_TICK_VALID,
    W8UF_ALLOWED_FLAGS_MASK, W8UF_HEADER_STRUCT, W8UF_INNER_LENGTH_BYTES,
    W8UF_GEOMETRY_SLACK_BYTES, W8UC_MAGIC, W8UH_MAGIC, W8UE_MAGIC, CONTROL_HEADER_STRUCT,
    CONTROL_HEADER_BYTES, HELLO_MAX_BODY_BYTES, CONTROL_KIND_HELLO, CONTROL_KIND_ACK,
    CONTROL_KIND_STOP, PREFERRED_INITIAL_CREDIT_GRANT, MAX_U64, MAX_U32, CLOSED_ERRORS,
    NATIVE_TO_W8U_ERROR_MAP, BINDING_KEYS, TARGET_HANDLE_KEYS, FRAME_METADATA_KEYS,
    FORBIDDEN_METADATA_SUBSTRINGS, FORBIDDEN_METADATA_KEY_TOKENS, HEX64_RE, W8UFFrameHeader,
    ValidatedW8UFrame, ControlFrame, W8UDigestError, _is_exact_int, _is_exact_bool, _is_hex64,
    _ensure_bytes_like, ContractEvidence, require_contract_evidence, numeric_error_code,
    translate_native_xwd_error, _extract_xwd_bounds, validate_w8u_xwd, canonical_rgb24_bytes,
    rgb24_sha256_raw, rgb24_sha256_hex, validate_raw_rgb24_buffer, rgba8_from_rgb24,
    rgba8_from_xwd, compare_rgba8_engine_buffer, _resolve_pixel_source, _normalize_digest_bytes,
)
from ._digest_binding import (
    build_synthetic_xwd, build_bgrx_xwd, build_xrgb_xwd, _validate_canonical_json_value,
    canonical_json_bytes, _validate_confined_roots, validate_binding, domain_sha256_raw,
    domain_sha256_hex, compute_geometry_record_cap, compute_effective_record_cap,
)

def parse_w8uf_header(
    header_buf: Any,
    *,
    expected_seq: Optional[int] = None,
    expected_width: Optional[int] = None,
    expected_height: Optional[int] = None,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
    negotiated_max_record_bytes: Optional[int] = None,
    tick_mode: Optional[str] = None,
) -> W8UFFrameHeader:
    """Parse and validate the 128-byte big-endian W8UF header BEFORE allocating/reading the record payload."""
    mv = _ensure_bytes_like(header_buf, "E_WIRE")
    if len(mv) < W8UF_HEADER_BYTES:
        raise W8UDigestError("E_LENGTH", f"W8UF header shorter than {W8UF_HEADER_BYTES} bytes")

    (
        magic,
        version,
        header_bytes,
        record_bytes,
        seq,
        tick,
        capture_mono_ns,
        width,
        height,
        stride,
        pixel_format,
        flags,
        payload_bytes,
        sha256_rgb24,
        binding_sha256,
    ) = W8UF_HEADER_STRUCT.unpack(bytes(mv[:W8UF_HEADER_BYTES]))

    if magic != W8UF_MAGIC:
        raise W8UDigestError("E_WIRE", f"invalid W8UF magic {magic!r}")
    if version != W8UF_VERSION:
        raise W8UDigestError("E_WIRE", f"unsupported W8UF version {version}")
    if header_bytes != W8UF_HEADER_BYTES:
        raise W8UDigestError("E_WIRE", f"invalid W8UF header_bytes {header_bytes}")

    if not (0 < width <= xfb.MAX_SIDE and 0 < height <= xfb.MAX_SIDE):
        raise W8UDigestError("E_GEOMETRY", f"invalid W8UF geometry {width}x{height}")
    if expected_width is not None and width != expected_width:
        raise W8UDigestError("E_GEOMETRY", f"W8UF width {width} != binding width {expected_width}")
    if expected_height is not None and height != expected_height:
        raise W8UDigestError("E_GEOMETRY", f"W8UF height {height} != binding height {expected_height}")
    if stride != width * 3:
        raise W8UDigestError("E_GEOMETRY", f"W8UF stride {stride} != width * 3 ({width * 3})")
    if pixel_format != W8UF_PIXEL_FORMAT_RGB24:
        raise W8UDigestError("E_WIRE", f"unsupported W8UF pixel_format {pixel_format}")

    if (flags & ~W8UF_ALLOWED_FLAGS_MASK) != 0:
        raise W8UDigestError("E_WIRE", f"unsupported W8UF flags 0x{flags:04x}")
    pair_under_grab = bool(flags & W8UF_FLAG_PAIR_UNDER_GRAB)
    tick_valid = bool(flags & W8UF_FLAG_TICK_VALID)
    if not pair_under_grab:
        raise W8UDigestError("E_WIRE", "W8UF pair_under_grab flag bit 0 is required")

    geometry_cap = compute_geometry_record_cap(width, height)
    cap = (
        compute_effective_record_cap(width, height, negotiated_max_record_bytes)
        if negotiated_max_record_bytes is not None
        else geometry_cap
    )
    if record_bytes < W8UF_HEADER_BYTES + W8UF_INNER_LENGTH_BYTES:
        raise W8UDigestError("E_LENGTH", f"record_bytes {record_bytes} smaller than minimum W8UF size")
    if record_bytes > cap:
        raise W8UDigestError("E_LENGTH", f"record_bytes {record_bytes} exceeds cap {cap}")
    if payload_bytes != record_bytes - W8UF_HEADER_BYTES:
        raise W8UDigestError("E_LENGTH", f"payload_bytes {payload_bytes} != record_bytes - 128")

    if seq < 1 or seq > MAX_U64:
        raise W8UDigestError("E_SEQ", f"invalid seq {seq}")
    if expected_seq is not None and seq != expected_seq:
        raise W8UDigestError("E_SEQ", f"non-contiguous seq {seq}, expected {expected_seq}")

    if tick_mode is not None:
        if tick_mode not in ("committed", "unavailable"):
            raise W8UDigestError("E_TICK", f"invalid tick_mode {tick_mode!r}")
        if tick_mode == "committed" and not tick_valid:
            raise W8UDigestError("E_TICK", "committed tick_mode requires tick_valid=True")
        if tick_mode == "unavailable" and (tick_valid or tick != 0):
            raise W8UDigestError("E_TICK", "unavailable tick_mode requires tick_valid=False and wire tick=0")
    else:
        if not tick_valid and tick != 0:
            raise W8UDigestError("E_TICK", "wire tick must be 0 when tick_valid is False")

    if expected_binding_sha256 is not None:
        exp_bind = _normalize_digest_bytes(expected_binding_sha256, "E_BINDING")
        if binding_sha256 != exp_bind:
            raise W8UDigestError("E_BINDING", "W8UF binding_sha256 mismatch")

    return W8UFFrameHeader(
        magic=magic,
        version=version,
        header_bytes=header_bytes,
        record_bytes=record_bytes,
        seq=seq,
        tick=tick,
        capture_mono_ns=capture_mono_ns,
        width=width,
        height=height,
        stride=stride,
        pixel_format=pixel_format,
        flags=flags,
        payload_bytes=payload_bytes,
        sha256_rgb24=sha256_rgb24,
        binding_sha256=binding_sha256,
        pair_under_grab=pair_under_grab,
        tick_valid=tick_valid,
    )


def build_w8uf_record(
    *,
    seq: int,
    tick: int,
    capture_mono_ns: int,
    width: int,
    height: int,
    xwd_a: bytes,
    xwd_b: bytes,
    sha256_rgb24: Union[bytes, str],
    binding_sha256: Union[bytes, str],
    pair_under_grab: bool = True,
    tick_valid: bool = True,
    version: int = W8UF_VERSION,
    header_bytes: int = W8UF_HEADER_BYTES,
    stride: Optional[int] = None,
    pixel_format: int = W8UF_PIXEL_FORMAT_RGB24,
    extra_flags: int = 0,
    record_bytes_override: Optional[int] = None,
    payload_bytes_override: Optional[int] = None,
    trailing_bytes: bytes = b"",
) -> bytes:
    """Pack a big-endian W8UF frame record (128-byte header + lenA + XWD A + lenB + XWD B)."""
    sha_rgb = _normalize_digest_bytes(sha256_rgb24, "E_HASH")
    sha_bind = _normalize_digest_bytes(binding_sha256, "E_BINDING")
    st = width * 3 if stride is None else stride
    flags = (W8UF_FLAG_PAIR_UNDER_GRAB if pair_under_grab else 0) | (W8UF_FLAG_TICK_VALID if tick_valid else 0) | extra_flags
    payload = struct.pack(">Q", len(xwd_a)) + bytes(xwd_a) + struct.pack(">Q", len(xwd_b)) + bytes(xwd_b) + bytes(trailing_bytes)
    pay_bytes = len(payload) if payload_bytes_override is None else payload_bytes_override
    rec_bytes = (W8UF_HEADER_BYTES + len(payload)) if record_bytes_override is None else record_bytes_override
    head = W8UF_HEADER_STRUCT.pack(
        W8UF_MAGIC,
        version,
        header_bytes,
        rec_bytes,
        seq,
        tick,
        capture_mono_ns,
        width,
        height,
        st,
        pixel_format,
        flags,
        pay_bytes,
        sha_rgb,
        sha_bind,
    )
    return head + payload


def validate_w8uf_record(
    record_or_stream: Any,
    *,
    expected_seq: Optional[int] = 1,
    expected_width: Optional[int] = None,
    expected_height: Optional[int] = None,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
    negotiated_max_record_bytes: Optional[int] = None,
    tick_mode: Optional[str] = None,
    contract: Any = None,
    require_contract: bool = False,
    body_allocator: Optional[Callable[[int], bytearray]] = None,
) -> ValidatedW8UFrame:
    """Validate a complete W8UF frame record, enforcing caps before body allocation and comparing XWD A/B in RGB24."""
    if hasattr(record_or_stream, "read"):
        raw_head = record_or_stream.read(W8UF_HEADER_BYTES)
        if not isinstance(raw_head, (bytes, bytearray, memoryview)) or len(raw_head) != W8UF_HEADER_BYTES:
            raise W8UDigestError("E_LENGTH", "truncated W8UF header on stream")
        hd = parse_w8uf_header(
            raw_head,
            expected_seq=expected_seq,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_binding_sha256=expected_binding_sha256,
            negotiated_max_record_bytes=negotiated_max_record_bytes,
            tick_mode=tick_mode,
        )
        alloc = body_allocator(hd.payload_bytes) if body_allocator is not None else bytearray(hd.payload_bytes)
        body_data = record_or_stream.read(hd.payload_bytes + 1)
        if len(body_data) != hd.payload_bytes:
            raise W8UDigestError("E_LENGTH", f"stream payload length {len(body_data)} != {hd.payload_bytes}")
        alloc[:] = body_data
        payload_mv = memoryview(alloc)
    else:
        mv = _ensure_bytes_like(record_or_stream, "E_WIRE")
        if len(mv) < W8UF_HEADER_BYTES:
            raise W8UDigestError("E_LENGTH", "record shorter than 128-byte W8UF header")
        hd = parse_w8uf_header(
            mv[:W8UF_HEADER_BYTES],
            expected_seq=expected_seq,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_binding_sha256=expected_binding_sha256,
            negotiated_max_record_bytes=negotiated_max_record_bytes,
            tick_mode=tick_mode,
        )
        if len(mv) != hd.record_bytes:
            raise W8UDigestError("E_LENGTH", f"record length {len(mv)} != header record_bytes {hd.record_bytes}")
        if body_allocator is not None:
            alloc = body_allocator(hd.payload_bytes)
            alloc[:] = mv[W8UF_HEADER_BYTES:]
            payload_mv = memoryview(alloc)
        else:
            payload_mv = mv[W8UF_HEADER_BYTES:]

    if len(payload_mv) < W8UF_INNER_LENGTH_BYTES:
        raise W8UDigestError("E_LENGTH", "payload too short for lenA and lenB")

    (len_a,) = struct.unpack(">Q", bytes(payload_mv[:8]))
    if 8 + len_a + 8 > len(payload_mv):
        raise W8UDigestError("E_LENGTH", "lenA exceeds W8UF payload bounds")
    xwd_a_mv = payload_mv[8 : 8 + len_a]
    (len_b,) = struct.unpack(">Q", bytes(payload_mv[8 + len_a : 16 + len_a]))
    if 16 + len_a + len_b != len(payload_mv):
        raise W8UDigestError("E_LENGTH", "16 + lenA + lenB != payload_bytes (trailing or missing bytes)")
    xwd_b_mv = payload_mv[16 + len_a : 16 + len_a + len_b]

    xwd_hd_a = validate_w8u_xwd(
        xwd_a_mv,
        width=hd.width,
        height=hd.height,
        contract=contract,
        require_contract=require_contract,
    )
    xwd_hd_b = validate_w8u_xwd(
        xwd_b_mv,
        width=hd.width,
        height=hd.height,
        contract=contract,
        require_contract=require_contract,
    )

    rgb_a = canonical_rgb24_bytes(
        xwd_a_mv,
        width=hd.width,
        height=hd.height,
        contract=contract,
        require_contract=require_contract,
    )
    rgb_b = canonical_rgb24_bytes(
        xwd_b_mv,
        width=hd.width,
        height=hd.height,
        contract=contract,
        require_contract=require_contract,
    )

    sha_a = hashlib.sha256(rgb_a).digest()
    sha_b = hashlib.sha256(rgb_b).digest()
    if sha_a != sha_b or rgb_a != rgb_b:
        raise W8UDigestError("E_TORN", "canonical RGB24 digest mismatch between XWD A and XWD B")

    if sha_a != hd.sha256_rgb24:
        raise W8UDigestError("E_HASH", "canonical RGB24 digest does not match W8UF advertised sha256_rgb24")

    meta = project_frame_metadata_from_header(hd)
    meta_sha_hex = hash_frame_metadata_hex(meta, contract=contract)
    return ValidatedW8UFrame(
        header=hd,
        rgb24=rgb_a,
        sha256_rgb24_raw=sha_a,
        sha256_rgb24_hex=sha_a.hex(),
        xwd_a_header=xwd_hd_a,
        xwd_b_header=xwd_hd_b,
        frame_metadata=meta,
        frame_metadata_sha256_hex=meta_sha_hex,
    )


def project_frame_metadata_from_header(hd: W8UFFrameHeader) -> Dict[str, Any]:
    """Project a validated W8UFFrameHeader into the closed 10-field frame metadata dictionary."""
    projected_tick: Optional[int] = hd.tick if hd.tick_valid else None
    return {
        "seq": hd.seq,
        "tick": projected_tick,
        "tick_valid": hd.tick_valid,
        "capture_mono_ns": hd.capture_mono_ns,
        "width": hd.width,
        "height": hd.height,
        "stride": hd.stride,
        "pixel_format": hd.pixel_format,
        "sha256": hd.sha256_rgb24.hex(),
        "binding_sha256": hd.binding_sha256.hex(),
    }


def _check_no_forbidden_pixel_or_encoding_tricks(obj: Any) -> None:
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raise W8UDigestError("E_WIRE", "raw byte buffers are forbidden in frame metadata")
    if isinstance(obj, float):
        raise W8UDigestError("E_WIRE", "floats are forbidden in frame metadata")
    if isinstance(obj, str):
        lower = obj.lower()
        for sub in FORBIDDEN_METADATA_SUBSTRINGS:
            if sub in lower:
                raise W8UDigestError("E_WIRE", f"forbidden image/base64 encoding substring {sub!r} in metadata")
        return
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise W8UDigestError("E_WIRE", "metadata keys must be strings")
            k_lower = k.lower()
            if k not in FRAME_METADATA_KEYS:
                for tok in FORBIDDEN_METADATA_KEY_TOKENS:
                    if tok in k_lower:
                        raise W8UDigestError("E_WIRE", f"forbidden pixel/delta key {k!r} in metadata")
            _check_no_forbidden_pixel_or_encoding_tricks(v)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _check_no_forbidden_pixel_or_encoding_tricks(item)


def validate_frame_metadata(
    meta: Mapping[str, Any],
    *,
    tick_mode: Optional[str] = None,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
) -> Dict[str, Any]:
    """Validate closed 10-field W8U frame metadata, rejecting floats, bool/int tricks, unknown keys, or pixel leaks."""
    if not isinstance(meta, Mapping):
        raise W8UDigestError("E_WIRE", "frame metadata must be a mapping")
    _check_no_forbidden_pixel_or_encoding_tricks(meta)
    if set(meta.keys()) != set(FRAME_METADATA_KEYS):
        raise W8UDigestError("E_WIRE", "frame metadata must contain exact closed keys")

    seq = meta["seq"]
    if not _is_exact_int(seq) or seq < 1 or seq > MAX_U64:
        raise W8UDigestError("E_SEQ", f"invalid metadata seq {seq!r}")

    tick_valid = meta["tick_valid"]
    if not _is_exact_bool(tick_valid):
        raise W8UDigestError("E_TICK", "tick_valid must be an exact bool")

    tick = meta["tick"]
    if tick_valid:
        if not _is_exact_int(tick) or tick < 0 or tick > MAX_U64:
            raise W8UDigestError("E_TICK", "committed/valid tick must be a non-negative exact integer")
        if tick_mode == "unavailable":
            raise W8UDigestError("E_TICK", "tick_valid=True is forbidden when tick_mode is unavailable")
    else:
        if tick is not None:
            raise W8UDigestError("E_TICK", "unavailable tick (tick_valid=False) must be None/null, never 0")
        if tick_mode == "committed":
            raise W8UDigestError("E_TICK", "tick_valid=False is forbidden when tick_mode is committed")

    capture_mono_ns = meta["capture_mono_ns"]
    if not _is_exact_int(capture_mono_ns) or capture_mono_ns < 0 or capture_mono_ns > MAX_U64:
        raise W8UDigestError("E_WIRE", "capture_mono_ns must be a non-negative exact integer")

    width = meta["width"]
    height = meta["height"]
    stride = meta["stride"]
    if not (_is_exact_int(width) and _is_exact_int(height) and _is_exact_int(stride)):
        raise W8UDigestError("E_GEOMETRY", "width, height, and stride must be exact integers")
    if not (0 < width <= xfb.MAX_SIDE and 0 < height <= xfb.MAX_SIDE):
        raise W8UDigestError("E_GEOMETRY", f"invalid metadata geometry {width}x{height}")
    if stride != width * 3:
        raise W8UDigestError("E_GEOMETRY", f"metadata stride {stride} != width * 3 ({width * 3})")

    pixel_format = meta["pixel_format"]
    if not _is_exact_int(pixel_format) or pixel_format != W8UF_PIXEL_FORMAT_RGB24:
        raise W8UDigestError("E_WIRE", f"invalid metadata pixel_format {pixel_format!r}")

    sha256_hex = meta["sha256"]
    if not _is_hex64(sha256_hex):
        raise W8UDigestError("E_HASH", "metadata sha256 must be 64 lowercase hex characters")

    binding_hex = meta["binding_sha256"]
    if not _is_hex64(binding_hex):
        raise W8UDigestError("E_BINDING", "metadata binding_sha256 must be 64 lowercase hex characters")

    if expected_binding_sha256 is not None:
        exp_bind_hex = _normalize_digest_bytes(expected_binding_sha256, "E_BINDING").hex()
        if binding_hex != exp_bind_hex:
            raise W8UDigestError("E_BINDING", "metadata binding_sha256 does not match expected binding")

    return {k: meta[k] for k in FRAME_METADATA_KEYS}


def canonicalize_frame_metadata(
    meta: Mapping[str, Any],
    *,
    tick_mode: Optional[str] = None,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
) -> bytes:
    """Validate and serialize closed frame metadata to canonical compact UTF-8 JSON bytes without LF."""
    clean = validate_frame_metadata(meta, tick_mode=tick_mode, expected_binding_sha256=expected_binding_sha256)
    return canonical_json_bytes(clean, allow_bool_keys=("tick_valid",), error_code="E_WIRE")


def hash_frame_metadata_raw(
    meta: Mapping[str, Any],
    *,
    tick_mode: Optional[str] = None,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
    contract: Any = None,
) -> bytes:
    """Compute the domain-separated 32-byte SHA-256 digest of canonical frame metadata."""
    canon = canonicalize_frame_metadata(meta, tick_mode=tick_mode, expected_binding_sha256=expected_binding_sha256)
    return domain_sha256_raw("frame_metadata", canon, contract=contract)


def hash_frame_metadata_hex(
    meta: Mapping[str, Any],
    *,
    tick_mode: Optional[str] = None,
    expected_binding_sha256: Optional[Union[bytes, str]] = None,
    contract: Any = None,
) -> str:
    """Compute the domain-separated 64-char hex SHA-256 digest of canonical frame metadata."""
    return hash_frame_metadata_raw(
        meta,
        tick_mode=tick_mode,
        expected_binding_sha256=expected_binding_sha256,
        contract=contract,
    ).hex()


