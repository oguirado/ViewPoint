"""w8u_digest: digest binding."""
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

def build_synthetic_xwd(
    width: int,
    height: int,
    pixels: Union[Callable[[int, int], Tuple[int, int, int]], bytes, bytearray, memoryview, Sequence[Tuple[int, int, int]]],
    *,
    byte_order: int = 0,
    pad: int = 0,
    name: bytes = b"w8u-synth\x00",
    ncolors: int = 0,
    version: int = xfb.XWD_VERSION,
    fmt: int = xfb.ZPIXMAP,
    depth: int = 24,
    bpp: int = 32,
    vclass: int = xfb.TRUECOLOR,
    masks: Tuple[int, int, int] = (0xFF0000, 0xFF00, 0xFF),
    bitmap_unit: int = 32,
    bitmap_bit_order: Optional[int] = None,
    bitmap_pad: int = 32,
    bits_per_rgb: int = 8,
    colormap_entries: Optional[int] = None,
    xoffset: int = 0,
    win_width: Optional[int] = None,
    win_height: Optional[int] = None,
    win_border: int = 0,
    filler_x: int = 0,
    pad_byte: int = 0,
    header_size_override: Optional[int] = None,
) -> bytes:
    """Build a synthetic XWD image in RAM (little-endian BGRX when byte_order=0, big-endian XRGB when byte_order=1)."""
    rgb = _resolve_pixel_source(width, height, pixels)
    bit_order = byte_order if bitmap_bit_order is None else bitmap_bit_order
    cmap_entries = ncolors if colormap_entries is None else colormap_entries
    ww = width if win_width is None else win_width
    wh = height if win_height is None else win_height
    hs = (xfb.MIN_HEADER + len(name)) if header_size_override is None else header_size_override
    bpl = width * 4 + pad
    head = (
        struct.pack(
            ">25I",
            hs,
            version,
            fmt,
            depth,
            width,
            height,
            xoffset,
            byte_order,
            bitmap_unit,
            bit_order,
            bitmap_pad,
            bpp,
            bpl,
            vclass,
            masks[0],
            masks[1],
            masks[2],
            bits_per_rgb,
            cmap_entries,
            ncolors,
            ww,
            wh,
            0,
            0,
            win_border,
        )
        + name
    )
    cmap = bytearray(ncolors * xfb.COLOR_ENTRY)
    for i in range(ncolors):
        struct.pack_into(">I3HBB", cmap, i * xfb.COLOR_ENTRY, i, (i * 257) & 0xFFFF, 0, 0, 7, 0)
    body = bytearray(cmap)
    fx = filler_x & 0xFF
    pb = pad_byte & 0xFF
    for y in range(height):
        row = bytearray([pb] * bpl)
        for x in range(width):
            p = (y * width + x) * 3
            r, g, b = rgb[p], rgb[p + 1], rgb[p + 2]
            if byte_order == 0:
                row[x * 4 : x * 4 + 4] = bytes((b, g, r, fx))
            else:
                row[x * 4 : x * 4 + 4] = bytes((fx, r, g, b))
        body += row
    return head + bytes(body)


def build_bgrx_xwd(
    width: int,
    height: int,
    pixels: Union[Callable[[int, int], Tuple[int, int, int]], bytes, bytearray, memoryview, Sequence[Tuple[int, int, int]]],
    *,
    pad: int = 0,
    name: bytes = b"w8u-lsb-bgrx\x00",
    ncolors: int = 0,
    filler_x: int = 0xAA,
    pad_byte: int = 0xCC,
    **kwargs: Any,
) -> bytes:
    """Build a little-endian BGRX synthetic XWD (byte_order=0)."""
    return build_synthetic_xwd(
        width,
        height,
        pixels,
        byte_order=0,
        pad=pad,
        name=name,
        ncolors=ncolors,
        filler_x=filler_x,
        pad_byte=pad_byte,
        **kwargs,
    )


def build_xrgb_xwd(
    width: int,
    height: int,
    pixels: Union[Callable[[int, int], Tuple[int, int, int]], bytes, bytearray, memoryview, Sequence[Tuple[int, int, int]]],
    *,
    pad: int = 8,
    name: bytes = b"w8u-msb-xrgb-distinct-header\x00",
    ncolors: int = 4,
    filler_x: int = 0x55,
    pad_byte: int = 0x33,
    **kwargs: Any,
) -> bytes:
    """Build a big-endian XRGB synthetic XWD (byte_order=1) with distinct header, colormap, and row padding."""
    return build_synthetic_xwd(
        width,
        height,
        pixels,
        byte_order=1,
        pad=pad,
        name=name,
        ncolors=ncolors,
        filler_x=filler_x,
        pad_byte=pad_byte,
        **kwargs,
    )


def _validate_canonical_json_value(
    val: Any,
    *,
    allow_bool_keys: Iterable[str] = (),
    current_key: Optional[str] = None,
    error_code: str = "E_WIRE",
) -> None:
    if val is None:
        return
    if _is_exact_bool(val):
        if current_key not in allow_bool_keys:
            raise W8UDigestError(error_code, f"boolean value not permitted at key {current_key!r}")
        return
    if _is_exact_int(val):
        if current_key in allow_bool_keys:
            raise W8UDigestError(error_code, f"integer value not permitted for boolean key {current_key!r}")
        return
    if isinstance(val, float):
        raise W8UDigestError(error_code, f"float value not permitted at key {current_key!r}")
    if isinstance(val, str):
        try:
            val.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise W8UDigestError(error_code, "invalid UTF-8 string") from exc
        return
    if isinstance(val, Mapping):
        for k, v in val.items():
            if not isinstance(k, str):
                raise W8UDigestError(error_code, "JSON object keys must be strings")
            _validate_canonical_json_value(
                v,
                allow_bool_keys=allow_bool_keys,
                current_key=k,
                error_code=error_code,
            )
        return
    if isinstance(val, (list, tuple)):
        for item in val:
            _validate_canonical_json_value(
                item,
                allow_bool_keys=allow_bool_keys,
                current_key=current_key,
                error_code=error_code,
            )
        return
    raise W8UDigestError(error_code, f"unsupported canonical JSON value type {type(val).__name__}")


def canonical_json_bytes(
    obj: Mapping[str, Any],
    *,
    allow_bool_keys: Iterable[str] = (),
    error_code: str = "E_WIRE",
) -> bytes:
    """Serialize a mapping to sorted compact UTF-8 integer-only JSON bytes without LF."""
    if not isinstance(obj, Mapping):
        raise W8UDigestError(error_code, "canonical JSON root must be an object")
    _validate_canonical_json_value(obj, allow_bool_keys=set(allow_bool_keys), error_code=error_code)
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if "\n" in text or "\r" in text:
        raise W8UDigestError(error_code, "canonical JSON must not contain line breaks")
    return text.encode("utf-8")


def _validate_confined_roots(frames_root: Any, frame_run_root: Any, run_id: Any) -> None:
    if not isinstance(frames_root, str) or not isinstance(frame_run_root, str):
        raise W8UDigestError("E_ROOT", "frames_root and frame_run_root must be strings")
    if "\\" in frames_root or "\\" in frame_run_root or "\x00" in frames_root or "\x00" in frame_run_root:
        raise W8UDigestError("E_ROOT", "invalid path characters in root configuration")
    if frames_root != REQUIRED_FRAMES_ROOT:
        raise W8UDigestError("E_ROOT", f"frames_root must be exactly {REQUIRED_FRAMES_ROOT!r}")
    if (not isinstance(run_id, str) or not run_id or run_id in (".", "..")
            or "/" in run_id or "\\" in run_id or "\x00" in run_id):
        raise W8UDigestError("E_ROOT", "run_id must be a single nonempty path component")
    if frame_run_root.endswith("/") or "//" in frame_run_root:
        raise W8UDigestError("E_ROOT", "frame_run_root must not contain empty or trailing slash segments")
    parts = frame_run_root.split("/")
    if any(part in (".", "..") for part in parts):
        raise W8UDigestError("E_ROOT", "dot segments are forbidden in frame_run_root")
    norm_run_root = posixpath.normpath(frame_run_root)
    if norm_run_root != frame_run_root:
        raise W8UDigestError("E_ROOT", "frame_run_root must be canonical without aliases")
    expected = REQUIRED_RUN_CHILD_PREFIX + "/" + run_id
    if frame_run_root != expected:
        raise W8UDigestError(
            "E_ROOT",
            f"frame_run_root must be the exact campaign child {expected!r}",
        )
    current = "/"
    for part in parts[1:]:
        current = posixpath.join(current, part)
        if os.path.islink(current):
            raise W8UDigestError("E_ROOT", "symlink in root or run path")
    if os.path.realpath(frame_run_root) != frame_run_root:
        raise W8UDigestError("E_ROOT", "frame_run_root must not resolve through an alias")


def validate_binding(
    binding: Mapping[str, Any],
    *,
    contract: Any = AUSENTE,
    grant_authenticator: Optional[Callable[[Mapping[str, Any]], bool]] = None,
    expected_target: Optional[Mapping[str, Any]] = None,
    reserved_capacity_frames: Optional[int] = None,
    reserved_ram_bytes: Optional[int] = None,
) -> Tuple[bytes, bytes, str]:
    """Validate a W8U binding object, enforce root/quota/target/grant rules, and return (canonical_bytes, raw_sha256, hex_sha256)."""
    ev = require_contract_evidence(contract, "binding_schema_id", "digest_domains")
    if not isinstance(binding, Mapping):
        raise W8UDigestError("E_BINDING", "binding must be a mapping")
    if set(binding.keys()) != set(BINDING_KEYS):
        raise W8UDigestError("E_BINDING", "binding keys do not match exact closed schema")

    if binding["schema"] != ev.binding_schema_id:
        raise W8UDigestError("E_BINDING", f"binding schema {binding['schema']!r} != {ev.binding_schema_id!r}")

    for str_key in ("run_id", "lease_id", "container_id", "display"):
        val = binding[str_key]
        if not isinstance(val, str) or not val or "\n" in val or "\r" in val or "\x00" in val:
            raise W8UDigestError("E_BINDING", f"invalid string field {str_key}")

    for hex_key in ("seat_grant_sha256", "image_sha256", "reader_sha256", "exec_allowlist_sha256"):
        if not _is_hex64(binding[hex_key]):
            raise W8UDigestError("E_BINDING", f"{hex_key} must be 64 lowercase hex characters")

    th = binding["target_handle"]
    if not isinstance(th, Mapping) or set(th.keys()) != set(TARGET_HANDLE_KEYS):
        raise W8UDigestError("E_BINDING", "target_handle must contain exactly kind, canonical_id, generation")
    if not isinstance(th["kind"], str) or not th["kind"]:
        raise W8UDigestError("E_BINDING", "target_handle.kind must be a non-empty string")
    if not isinstance(th["canonical_id"], str) or not th["canonical_id"]:
        raise W8UDigestError("E_BINDING", "target_handle.canonical_id must be a non-empty string")
    if not _is_exact_int(th["generation"]) or th["generation"] < 0:
        raise W8UDigestError("E_BINDING", "target_handle.generation must be a non-negative exact integer")

    if not _is_exact_int(binding["epoch"]) or binding["epoch"] < 0:
        raise W8UDigestError("E_BINDING", "epoch must be a non-negative exact integer")
    if not _is_exact_int(binding["xid"]) or binding["xid"] <= 0:
        raise W8UDigestError("E_BINDING", "xid must be a positive exact integer")

    w, h = binding["width"], binding["height"]
    if not _is_exact_int(w) or not _is_exact_int(h) or not (0 < w <= xfb.MAX_SIDE and 0 < h <= xfb.MAX_SIDE):
        raise W8UDigestError("E_GEOMETRY", f"invalid binding geometry {w!r}x{h!r}")

    _validate_confined_roots(binding["frames_root"], binding["frame_run_root"], binding["run_id"])

    max_rec = binding["max_record_bytes"]
    if not _is_exact_int(max_rec) or max_rec <= W8UF_HEADER_BYTES + W8UF_INNER_LENGTH_BYTES:
        raise W8UDigestError("E_LENGTH", "max_record_bytes must be an exact integer > 144")

    max_inflight = binding["max_inflight_frames"]
    if not _is_exact_int(max_inflight) or max_inflight <= 0:
        raise W8UDigestError("E_CREDIT", "max_inflight_frames must be a positive exact integer")

    if reserved_capacity_frames is not None:
        if not _is_exact_int(reserved_capacity_frames) or reserved_capacity_frames < max_inflight:
            raise W8UDigestError("E_CREDIT", "max_inflight_frames exceeds reserved frame capacity")
    if reserved_ram_bytes is not None:
        if not _is_exact_int(reserved_ram_bytes) or reserved_ram_bytes < max_inflight * max_rec:
            raise W8UDigestError("E_RAM_QUOTA", "reserved_ram_bytes insufficient for max_inflight_frames * max_record_bytes")

    tick_mode = binding["tick_mode"]
    if tick_mode not in ("committed", "unavailable"):
        raise W8UDigestError("E_TICK", f"invalid tick_mode {tick_mode!r}")

    if expected_target is not None:
        if ("width" in expected_target and expected_target["width"] != w) or (
            "height" in expected_target and expected_target["height"] != h
        ):
            raise W8UDigestError("E_GEOMETRY", "binding geometry does not match target geometry")
        for tk in ("kind", "canonical_id", "generation"):
            if tk in expected_target and th[tk] != expected_target[tk]:
                raise W8UDigestError("E_BINDING", f"target_handle.{tk} mismatch")
        for bk in ("epoch", "display", "xid", "container_id", "run_id", "lease_id"):
            if bk in expected_target and binding[bk] != expected_target[bk]:
                raise W8UDigestError("E_BINDING", f"binding.{bk} mismatch")

    if grant_authenticator is not None:
        if not grant_authenticator(binding):
            raise W8UDigestError("E_BINDING", "seat grant / capability authentication failed")

    canon_bytes = canonical_json_bytes(binding, allow_bool_keys=(), error_code="E_BINDING")
    if len(canon_bytes) > HELLO_MAX_BODY_BYTES:
        raise W8UDigestError("E_LENGTH", "canonical binding JSON exceeds 16 KiB HELLO limit")

    raw_digest = domain_sha256_raw("binding", canon_bytes, contract=ev)
    return canon_bytes, raw_digest, raw_digest.hex()


def domain_sha256_raw(domain: str, payload: Union[bytes, bytearray, memoryview], *, contract: Any = None) -> bytes:
    """Compute a domain-separated SHA-256 raw digest.

    Note: canonical wire RGB24 pixel digest (`sha256_rgb24`) matches `xfb.frame_sha256` on raw RGB24 bytes
    unless a non-empty prefix is configured in `contract.digest_domains`. UI/frame_metadata, simulation, and
    RGB24 domains are guaranteed distinct under `separate_hash_domains`.
    """
    mv = _ensure_bytes_like(payload, "E_WIRE")
    if contract is not None and contract != AUSENTE:
        ev = require_contract_evidence(contract, "digest_domains")
        domains = ev.digest_domains
        if not isinstance(domains, Mapping) or domain not in domains:
            raise W8UDigestError(AUSENTE, f"digest domain {domain!r} is AUSENTE in contract")
        prefix = domains[domain]
        if isinstance(prefix, str):
            prefix_bytes = prefix.encode("utf-8")
        elif isinstance(prefix, (bytes, bytearray, memoryview)):
            prefix_bytes = bytes(prefix)
        else:
            raise W8UDigestError(AUSENTE, f"invalid prefix type for domain {domain!r}")
        return hashlib.sha256(prefix_bytes + bytes(mv)).digest()

    if domain == "rgb24_raw" or domain == "binding":
        return hashlib.sha256(bytes(mv)).digest()
    default_prefixes = {
        "rgb24": b"w8u.rgb24.v1\x00",
        "ui": b"w8u.ui.frame_metadata.v1\x00",
        "frame_metadata": b"w8u.ui.frame_metadata.v1\x00",
        "simulation": b"w8u.simulation.state.v1\x00",
    }
    if domain not in default_prefixes:
        raise W8UDigestError(AUSENTE, f"unknown digest domain {domain!r} without contract authority")
    return hashlib.sha256(default_prefixes[domain] + bytes(mv)).digest()


def domain_sha256_hex(domain: str, payload: Union[bytes, bytearray, memoryview], *, contract: Any = None) -> str:
    """Return lowercase hex digest for the specified hash domain."""
    return domain_sha256_raw(domain, payload, contract=contract).hex()


def compute_geometry_record_cap(width: int, height: int) -> int:
    """Compute W8UF geometry record cap: 2 * (4 * width * height + 1 MiB) + 16 + 128."""
    if not _is_exact_int(width) or not _is_exact_int(height) or width <= 0 or height <= 0:
        raise W8UDigestError("E_GEOMETRY", "width and height must be positive exact integers")
    if width > xfb.MAX_SIDE or height > xfb.MAX_SIDE:
        raise W8UDigestError("E_GEOMETRY", "width or height exceeds MAX_SIDE")
    return 2 * (4 * width * height + W8UF_GEOMETRY_SLACK_BYTES) + W8UF_INNER_LENGTH_BYTES + W8UF_HEADER_BYTES


def compute_effective_record_cap(width: int, height: int, negotiated_max_record_bytes: int) -> int:
    """Return min(negotiated_max_record_bytes, geometry_cap)."""
    if not _is_exact_int(negotiated_max_record_bytes) or negotiated_max_record_bytes <= 0:
        raise W8UDigestError("E_LENGTH", "negotiated_max_record_bytes must be a positive exact integer")
    return min(negotiated_max_record_bytes, compute_geometry_record_cap(width, height))


