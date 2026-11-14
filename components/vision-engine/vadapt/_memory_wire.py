"""w8u_memory: memory wire."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
from vadapt import ramws, rtring, window_stream_bank

from ._memory_contract import (
    AUSENTE, PREPARED, ACCEPT, GATE_NAMES, CLOSED_ERROR_CODES, RESERVATION_CATEGORIES,
    BINDING_KEYS, TARGET_HANDLE_KEYS, FRAME_METADATA_KEYS, RECEIPT_SCHEMA,
    DEFAULT_RECEIPT_SECTIONS, FRAME_MAGIC, FRAME_VERSION, FRAME_HEADER_BYTES,
    FRAME_PIXEL_FORMAT_RGB24, FLAG_PAIR_UNDER_GRAB, FLAG_TICK_VALID, FLAG_ALLOWED_MASK,
    CONTROL_MAGIC_CLIENT, CONTROL_MAGIC_HELLO_ACK, CONTROL_MAGIC_END, CONTROL_VERSION,
    CONTROL_HEADER_BYTES, CONTROL_KIND_HELLO, CONTROL_KIND_ACK, CONTROL_KIND_STOP,
    HELLO_MAX_BODY_BYTES, PREFERRED_INITIAL_CREDIT_GRANT, MIB, MAX_U16, MAX_U32, MAX_U64,
    CONFINED_TOOL, CONFINED_CAMPAIGN, _FRAME_HDR_STRUCT, _PAIR_LEN_STRUCT, _CTRL_HDR_STRUCT,
    _ACK_BODY_STRUCT, _END_BODY_STRUCT, _XWD_HDR_STRUCT, _HEX64_CHARS, W8UMemoryError,
    _exact_int, _checked_mul, _checked_sum, _is_hex64, domain_sha256, REQUIRED_CONTRACT_FIELDS,
    ContractEvidence, inspect_contract_evidence, require_accepted_contract,
    translate_native_error, canonical_rgb24_bytes, actual_ring_bytes, geometry_record_cap,
    effective_record_cap, canonical_json_bytes, parse_canonical_json_bytes,
    validate_confined_roots, validate_binding,
)

def validate_and_convert_xwd(
    xwd_bytes: bytes,
    *,
    expected_width: int,
    expected_height: int,
    contract,
    native_validator=None,
) -> bytes:
    """Validate W8U XWD subset wrapper checks before canonical top-down RGB24 conversion."""
    require_accepted_contract(contract, error_code="E_WIRE")
    subset = (
        getattr(contract, "xwd_subset", {})
        if not isinstance(contract, dict)
        else contract.get("xwd_subset", {})
    )
    if not isinstance(subset, dict) or not subset:
        raise W8UMemoryError("E_WIRE", "xwd_subset missing from contract (%s)" % AUSENTE)

    for req_key in (
        "byte_order",
        "bitmap_bit_order",
        "max_header_bytes",
        "max_ncolors",
        "allowed_bpp",
        "allowed_depth",
        "stride_alignment",
        "red_mask",
        "green_mask",
        "blue_mask",
    ):
        if req_key not in subset:
            raise W8UMemoryError("E_WIRE", "xwd_subset.%s is %s" % (req_key, AUSENTE))

    if callable(native_validator):
        nat_ok, nat_err = native_validator(xwd_bytes, expected_width, expected_height)
        if not nat_ok:
            raise W8UMemoryError(translate_native_error(nat_err, contract), "native validator rejected XWD: %s" % nat_err)

    raw = bytes(xwd_bytes)
    if len(raw) < _XWD_HDR_STRUCT.size:
        raise W8UMemoryError("E_LENGTH", "XWD shorter than 100-byte base header (%d B)" % len(raw))

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
        _window_x,
        _window_y,
        window_bdrwidth,
    ) = _XWD_HDR_STRUCT.unpack_from(raw, 0)

    if file_version != 7 or pixmap_format != 2 or xoffset != 0 or window_bdrwidth != 0:
        raise W8UMemoryError("E_WIRE", "invalid XWD version/format/xoffset/border")

    if header_size < _XWD_HDR_STRUCT.size or header_size > subset["max_header_bytes"]:
        raise W8UMemoryError("E_LENGTH", "XWD header_size %d out of contract bounds" % header_size)

    if ncolors > subset["max_ncolors"] or colormap_entries > subset.get("max_colormap_entries", 256):
        raise W8UMemoryError("E_LENGTH", "XWD ncolors/colormap_entries out of contract bounds")

    allowed_bo = subset["byte_order"] if isinstance(subset["byte_order"], (list, tuple, set, frozenset)) else (subset["byte_order"],)
    allowed_bbo = (
        subset["bitmap_bit_order"]
        if isinstance(subset["bitmap_bit_order"], (list, tuple, set, frozenset))
        else (subset["bitmap_bit_order"],)
    )
    if byte_order not in allowed_bo or bitmap_bit_order not in allowed_bbo:
        raise W8UMemoryError("E_WIRE", "XWD byte_order=%d or bitmap_bit_order=%d rejected" % (byte_order, bitmap_bit_order))

    if bitmap_unit not in (8, 16, 32) or bitmap_pad not in (8, 16, 32) or visual_class not in (4, 5) or bits_per_rgb != 8:
        raise W8UMemoryError("E_WIRE", "XWD unit/pad/visual_class/bits_per_rgb rejected")

    allowed_bpp = subset["allowed_bpp"] if isinstance(subset["allowed_bpp"], (list, tuple, set, frozenset)) else (subset["allowed_bpp"],)
    allowed_depth = (
        subset["allowed_depth"] if isinstance(subset["allowed_depth"], (list, tuple, set, frozenset)) else (subset["allowed_depth"],)
    )
    if bits_per_pixel not in allowed_bpp or pixmap_depth not in allowed_depth:
        raise W8UMemoryError("E_GEOMETRY", "XWD depth=%d bpp=%d rejected" % (pixmap_depth, bits_per_pixel))

    if (red_mask, green_mask, blue_mask) != (subset["red_mask"], subset["green_mask"], subset["blue_mask"]):
        raise W8UMemoryError("E_WIRE", "XWD RGB masks do not match frozen contract subset")

    if pixmap_width != expected_width or pixmap_height != expected_height:
        raise W8UMemoryError(
            "E_GEOMETRY",
            "XWD pixmap geometry (%d, %d) != (%d, %d)" % (pixmap_width, pixmap_height, expected_width, expected_height),
        )
    if window_width != expected_width or window_height != expected_height:
        raise W8UMemoryError("E_GEOMETRY", "XWD window geometry mismatch")

    bytes_per_px = bits_per_pixel // 8
    min_stride = expected_width * bytes_per_px
    align = subset["stride_alignment"]
    expected_aligned_stride = ((min_stride + align - 1) // align) * align
    max_stride = subset.get("max_stride_bytes", expected_aligned_stride)
    if bytes_per_line < min_stride or bytes_per_line % align != 0 or bytes_per_line > max_stride:
        raise W8UMemoryError("E_GEOMETRY", "XWD bytes_per_line %d violates bounded aligned stride" % bytes_per_line)

    prefix_bytes = header_size + ncolors * 12
    expected_total = prefix_bytes + bytes_per_line * expected_height
    if len(raw) != expected_total:
        raise W8UMemoryError("E_LENGTH", "XWD exact size mismatch (%d != %d)" % (len(raw), expected_total))

    # Convert to canonical top-down RGB24 (never hashing raw BGRX or RGBA8 or row padding)
    out = bytearray(expected_width * expected_height * 3)
    out_pos = 0
    for row in range(expected_height):
        row_start = prefix_bytes + row * bytes_per_line
        for col in range(expected_width):
            px_off = row_start + col * bytes_per_px
            if bytes_per_px == 4:
                if byte_order == 0:  # LSBFirst: B, G, R, X
                    b_val = raw[px_off]
                    g_val = raw[px_off + 1]
                    r_val = raw[px_off + 2]
                else:  # MSBFirst: X, R, G, B
                    r_val = raw[px_off + 1]
                    g_val = raw[px_off + 2]
                    b_val = raw[px_off + 3]
            else:  # 24 bpp
                if byte_order == 0:  # LSBFirst: B, G, R
                    b_val = raw[px_off]
                    g_val = raw[px_off + 1]
                    r_val = raw[px_off + 2]
                else:  # MSBFirst: R, G, B
                    r_val = raw[px_off]
                    g_val = raw[px_off + 1]
                    b_val = raw[px_off + 2]
            out[out_pos] = r_val
            out[out_pos + 1] = g_val
            out[out_pos + 2] = b_val
            out_pos += 3
    return bytes(out)


def build_synthetic_xwd(
    rgb24_bytes: bytes,
    width: int,
    height: int,
    *,
    byte_order: int = 0,
    bitmap_bit_order: int = 0,
    bits_per_pixel: int = 32,
    pixmap_depth: int = 24,
    header_extra_bytes: bytes = b"",
    ncolors: int = 0,
    pad_byte: int = 0,
) -> bytes:
    """Build an in-memory synthetic XWD record from canonical RGB24 bytes for unit tests."""
    if len(rgb24_bytes) != width * height * 3:
        raise ValueError("rgb24_bytes length must equal width * height * 3")
    bytes_per_px = bits_per_pixel // 8
    min_stride = width * bytes_per_px
    bytes_per_line = ((min_stride + 3) // 4) * 4
    header_size = _XWD_HDR_STRUCT.size + len(header_extra_bytes)
    hdr = _XWD_HDR_STRUCT.pack(
        header_size,
        7,
        2,
        pixmap_depth,
        width,
        height,
        0,
        byte_order,
        32,
        bitmap_bit_order,
        32,
        bits_per_pixel,
        bytes_per_line,
        4,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        8,
        256,
        ncolors,
        width,
        height,
        0,
        0,
        0,
    )
    cmap = bytes(ncolors * 12)
    body = bytearray(bytes_per_line * height)
    for row in range(height):
        row_off = row * bytes_per_line
        for col in range(width):
            src = (row * width + col) * 3
            r_val, g_val, b_val = rgb24_bytes[src], rgb24_bytes[src + 1], rgb24_bytes[src + 2]
            dst = row_off + col * bytes_per_px
            if bytes_per_px == 4:
                if byte_order == 0:
                    body[dst : dst + 4] = bytes((b_val, g_val, r_val, pad_byte))
                else:
                    body[dst : dst + 4] = bytes((pad_byte, r_val, g_val, b_val))
            else:
                if byte_order == 0:
                    body[dst : dst + 3] = bytes((b_val, g_val, r_val))
                else:
                    body[dst : dst + 3] = bytes((r_val, g_val, b_val))
    return hdr + header_extra_bytes + cmap + bytes(body)


# ---------------------------------------------------------------------------
# FRAME wire parser/validator & closed metadata projection
# ---------------------------------------------------------------------------

def pack_frame_record(
    *,
    seq: int,
    tick: int,
    capture_mono_ns: int,
    width: int,
    height: int,
    flags: int,
    sha256_rgb24: bytes,
    binding_sha256: bytes,
    xwd_a: bytes,
    xwd_b: bytes,
    magic: bytes = FRAME_MAGIC,
    version: int = FRAME_VERSION,
    header_bytes: int = FRAME_HEADER_BYTES,
    stride: int | None = None,
    pixel_format: int = FRAME_PIXEL_FORMAT_RGB24,
    trailing_bytes: bytes = b"",
    override_record_bytes: int | None = None,
    override_payload_bytes: int | None = None,
) -> bytes:
    """Pack a big-endian W8UF frame record in RAM for testing."""
    payload = (
        _PAIR_LEN_STRUCT.pack(len(xwd_a))
        + bytes(xwd_a)
        + _PAIR_LEN_STRUCT.pack(len(xwd_b))
        + bytes(xwd_b)
        + bytes(trailing_bytes)
    )
    rec_bytes = (FRAME_HEADER_BYTES + len(payload)) if override_record_bytes is None else override_record_bytes
    pay_bytes = len(payload) if override_payload_bytes is None else override_payload_bytes
    st = (width * 3) if stride is None else stride
    hdr = _FRAME_HDR_STRUCT.pack(
        magic,
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
        sha256_rgb24,
        binding_sha256,
    )
    return hdr + payload


def validate_frame_record(
    frame_bytes: bytes,
    *,
    binding: dict,
    binding_sha256: bytes,
    expected_seq: int,
    contract,
    native_validator=None,
) -> tuple[dict, bytes]:
    """Validate a W8UF frame header and payload, returning (closed_frame_metadata, canonical_rgb24)."""
    require_accepted_contract(contract, error_code="E_WIRE")
    raw = bytes(frame_bytes)
    if len(raw) < FRAME_HEADER_BYTES:
        raise W8UMemoryError("E_LENGTH", "frame shorter than 128-byte header (%d B)" % len(raw))

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
        wire_binding_sha256,
    ) = _FRAME_HDR_STRUCT.unpack_from(raw, 0)

    if magic != FRAME_MAGIC or version != FRAME_VERSION:
        raise W8UMemoryError("E_WIRE", "invalid frame magic/version (no magic resync)")
    if header_bytes != FRAME_HEADER_BYTES:
        raise W8UMemoryError("E_LENGTH", "header_bytes must be 128 (got %d)" % header_bytes)

    if wire_binding_sha256 != binding_sha256:
        raise W8UMemoryError("E_BINDING", "frame binding_sha256 mismatch")

    b_w, b_h = binding["width"], binding["height"]
    if width != b_w or height != b_h or stride != b_w * 3:
        raise W8UMemoryError("E_GEOMETRY", "frame width/height/stride mismatch")
    if pixel_format != FRAME_PIXEL_FORMAT_RGB24:
        raise W8UMemoryError("E_GEOMETRY", "pixel_format must be 1 (RGB24)")

    # Enforce smaller negotiated cap and geometry cap BEFORE body allocation/slice
    cap = effective_record_cap(b_w, b_h, binding["max_record_bytes"], error_code="E_LENGTH")
    if record_bytes < FRAME_HEADER_BYTES + 16 or record_bytes > cap:
        raise W8UMemoryError("E_LENGTH", "record_bytes %d violates cap %d" % (record_bytes, cap))
    if payload_bytes != record_bytes - FRAME_HEADER_BYTES or len(raw) != record_bytes:
        raise W8UMemoryError("E_LENGTH", "payload_bytes / record_bytes mismatch")

    if expected_seq < 1 or expected_seq > MAX_U64 or seq != expected_seq:
        raise W8UMemoryError("E_SEQ", "non-contiguous or rolled-over seq %d (expected %d)" % (seq, expected_seq))

    if flags & ~FLAG_ALLOWED_MASK:
        raise W8UMemoryError("E_WIRE", "reserved frame flag bits set: 0x%x" % flags)
    if not (flags & FLAG_PAIR_UNDER_GRAB):
        raise W8UMemoryError("E_TORN", "pair_under_grab bit 0 is required")

    tick_valid = bool(flags & FLAG_TICK_VALID)
    tick_mode = binding["tick_mode"]
    if tick_mode == "committed":
        if not tick_valid:
            raise W8UMemoryError("E_TICK", "committed tick_mode requires tick_valid=True")
        projected_tick = tick
    elif tick_mode == "unavailable":
        if tick_valid or tick != 0:
            raise W8UMemoryError("E_TICK", "unavailable tick_mode requires wire tick=0 and tick_valid=False")
        projected_tick = None
    else:
        raise W8UMemoryError("E_TICK", "invalid tick_mode %r" % (tick_mode,))

    # Parse payload: lenA u64, exact XWD A, lenB u64, exact XWD B with no trailing bytes
    pos = FRAME_HEADER_BYTES
    if pos + 8 > record_bytes:
        raise W8UMemoryError("E_LENGTH", "truncated lenA")
    len_a = _PAIR_LEN_STRUCT.unpack_from(raw, pos)[0]
    pos += 8
    if len_a == 0 or pos + len_a + 8 > record_bytes:
        raise W8UMemoryError("E_LENGTH", "invalid lenA=%d" % len_a)
    xwd_a = raw[pos : pos + len_a]
    pos += len_a
    len_b = _PAIR_LEN_STRUCT.unpack_from(raw, pos)[0]
    pos += 8
    if len_b == 0 or pos + len_b != record_bytes:
        raise W8UMemoryError("E_LENGTH", "invalid lenB=%d or trailing bytes present" % len_b)
    xwd_b = raw[pos : pos + len_b]

    rgb_a = validate_and_convert_xwd(
        xwd_a,
        expected_width=b_w,
        expected_height=b_h,
        contract=contract,
        native_validator=native_validator,
    )
    rgb_b = validate_and_convert_xwd(
        xwd_b,
        expected_width=b_w,
        expected_height=b_h,
        contract=contract,
        native_validator=native_validator,
    )

    digest_a = hashlib.sha256(rgb_a).digest()
    digest_b = hashlib.sha256(rgb_b).digest()
    if digest_a != digest_b:
        raise W8UMemoryError("E_TORN", "canonical RGB24 pair digests disagree")
    if digest_a != sha256_rgb24:
        raise W8UMemoryError("E_HASH", "canonical RGB24 digest does not match advertised sha256_rgb24")

    meta = {
        "seq": seq,
        "tick": projected_tick,
        "tick_valid": tick_valid,
        "capture_mono_ns": capture_mono_ns,
        "width": width,
        "height": height,
        "stride": stride,
        "pixel_format": pixel_format,
        "sha256": digest_a.hex(),
        "binding_sha256": wire_binding_sha256.hex(),
    }
    if tuple(meta.keys()) != FRAME_METADATA_KEYS:
        raise W8UMemoryError("E_WIRE", "closed frame metadata keys violated")
    return meta, rgb_a


# ---------------------------------------------------------------------------
# Control Wire Envelopes (W8UC, W8UH, W8UE)
# ---------------------------------------------------------------------------

def pack_control_frame(magic: bytes, kind: int, binding_sha256: bytes, body: bytes = b"", *, version: int = CONTROL_VERSION) -> bytes:
    """Pack a 44-byte control header plus body."""
    body_b = bytes(body)
    return _CTRL_HDR_STRUCT.pack(magic, version, kind, len(body_b), binding_sha256) + body_b


def parse_control_frame(raw: bytes, *, expected_binding_sha256: bytes | None = None, contract=None) -> dict:
    """Parse and validate a W8UC, W8UH, or W8UE control frame without magic resync."""
    b = bytes(raw)
    if len(b) < CONTROL_HEADER_BYTES:
        raise W8UMemoryError("E_LENGTH", "control frame shorter than 44-byte header")
    magic, version, kind, body_bytes, binding_sha256 = _CTRL_HDR_STRUCT.unpack_from(b, 0)
    if version != CONTROL_VERSION:
        raise W8UMemoryError("E_WIRE", "unsupported control version %d" % version)
    if len(b) != CONTROL_HEADER_BYTES + body_bytes:
        raise W8UMemoryError("E_LENGTH", "control body_bytes mismatch (%d != %d)" % (len(b) - CONTROL_HEADER_BYTES, body_bytes))
    if expected_binding_sha256 is not None and binding_sha256 != expected_binding_sha256:
        raise W8UMemoryError("E_BINDING", "control binding_sha256 mismatch")

    body = b[CONTROL_HEADER_BYTES:]
    if magic == CONTROL_MAGIC_CLIENT:
        if kind == CONTROL_KIND_HELLO:
            if body_bytes == 0 or body_bytes > HELLO_MAX_BODY_BYTES:
                raise W8UMemoryError("E_LENGTH", "HELLO body_bytes %d outside (0, 16384]" % body_bytes)
            if hashlib.sha256(body).digest() != binding_sha256:
                raise W8UMemoryError("E_BINDING", "HELLO binding_sha256 does not match body digest")
            binding_obj = parse_canonical_json_bytes(body)
            return {
                "magic": magic,
                "kind": kind,
                "type": "HELLO",
                "binding_sha256": binding_sha256,
                "binding": binding_obj,
                "credit_granted": 0,
            }
        if kind == CONTROL_KIND_ACK:
            if body_bytes != _ACK_BODY_STRUCT.size:
                raise W8UMemoryError("E_LENGTH", "ACK body must be 16 bytes")
            ack_seq, cumulative_credit_total = _ACK_BODY_STRUCT.unpack(body)
            return {
                "magic": magic,
                "kind": kind,
                "type": "ACK",
                "binding_sha256": binding_sha256,
                "ack_seq": ack_seq,
                "cumulative_credit_total": cumulative_credit_total,
            }
        if kind == CONTROL_KIND_STOP:
            if body_bytes != 0:
                raise W8UMemoryError("E_LENGTH", "STOP body must be empty")
            return {
                "magic": magic,
                "kind": kind,
                "type": "STOP",
                "binding_sha256": binding_sha256,
            }
        raise W8UMemoryError("E_WIRE", "unknown W8UC kind %d" % kind)

    if magic == CONTROL_MAGIC_HELLO_ACK:
        require_accepted_contract(contract, error_code="E_WIRE")
        ctrl = getattr(contract, "control_combinations", {}) if not isinstance(contract, dict) else contract.get("control_combinations", {})
        spec = ctrl.get("HELLO_CONFIRM")
        if not isinstance(spec, dict) or spec.get("magic") != "W8UH" or spec.get("kind") != kind or spec.get("body_bytes") != body_bytes:
            raise W8UMemoryError("E_WIRE", "W8UH confirmation violates accepted contract")
        return {
            "magic": magic,
            "kind": kind,
            "type": "HELLO_CONFIRM",
            "binding_sha256": binding_sha256,
            "body": body,
        }

    if magic == CONTROL_MAGIC_END:
        require_accepted_contract(contract, error_code="E_WIRE")
        ctrl = getattr(contract, "control_combinations", {}) if not isinstance(contract, dict) else contract.get("control_combinations", {})
        end_spec = ctrl.get("END")
        if not isinstance(end_spec, dict) or end_spec.get("magic") != "W8UE" or end_spec.get("kind") != kind:
            raise W8UMemoryError("E_WIRE", "W8UE kind violates accepted contract")
        if body_bytes != _END_BODY_STRUCT.size:
            raise W8UMemoryError("E_LENGTH", "W8UE END body must be 20 bytes")
        last_seq, sent, error_code = _END_BODY_STRUCT.unpack(body)
        end_codes = getattr(contract, "end_error_codes", {}) if not isinstance(contract, dict) else contract.get("end_error_codes", {})
        inv_codes = {v: k for k, v in end_codes.items() if type(v) is int}
        if error_code not in inv_codes:
            raise W8UMemoryError("E_WIRE", "unknown numeric END error_code %d refused" % error_code)
        return {
            "magic": magic,
            "kind": kind,
            "type": "END",
            "binding_sha256": binding_sha256,
            "last_seq": last_seq,
            "sent": sent,
            "error_code": error_code,
            "error_name": inv_codes[error_code],
        }

    raise W8UMemoryError("E_WIRE", "unknown control magic %r (no magic resync)" % (magic,))


# ---------------------------------------------------------------------------
# Telemetry Observation & Injector
# ---------------------------------------------------------------------------

