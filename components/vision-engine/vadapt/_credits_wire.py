"""w8u_credits: credits wire."""
from dataclasses import dataclass
import hashlib
import json
import os
import struct
from typing import Any, Callable
from vadapt import rtring
from vadapt import window_stream_bank

from ._credits_binding import (
    AUSENTE, U16_MAX, U32_MAX, U64_MAX, FRAME_MAGIC, FRAME_VERSION, FRAME_HEADER_BYTES,
    PIXEL_FORMAT_RGB24, FLAG_PAIR_UNDER_GRAB, FLAG_TICK_VALID, ALLOWED_FRAME_FLAGS_MASK,
    GEOMETRY_OVERHEAD_BYTES, INNER_LENGTH_BYTES, CONTROL_MAGIC_HELLO, CONTROL_MAGIC_CONFIRM,
    CONTROL_MAGIC_END, CONTROL_HEADER_BYTES, CONTROL_KIND_HELLO, CONTROL_KIND_ACK,
    CONTROL_KIND_STOP, HELLO_MAX_BODY_BYTES, PREFERRED_INITIAL_GRANT, FRAMES_ROOT,
    CONFINED_RUN_PREFIX, RECEIPT_SCHEMA, FRAME_HEADER_STRUCT, INNER_LEN_STRUCT,
    CONTROL_HEADER_STRUCT, ACK_BODY_STRUCT, END_BODY_STRUCT, XWD_HEADER_STRUCT, CLOSED_ERRORS,
    BINDING_KEYS, TARGET_HANDLE_KEYS, FRAME_METADATA_KEYS, W8UCreditError, _require_exact_int,
    _require_hex_sha256, _require_nonempty_str, InjectedClock, ContractEvidence,
    validate_contract_evidence, _validate_canonical_json_value, canonical_json_bytes,
    parse_canonical_binding_json, validate_roots, geometry_cap_bytes, effective_record_cap,
    ConfirmedBinding, validate_binding,
)

def translate_native_xwd_error(native_code: str, contract: ContractEvidence) -> str:
    """Translate a native XWD validator error into its frozen W8U closed error code."""
    contract = validate_contract_evidence(contract)
    if type(native_code) is not str or not native_code:
        return "E_WIRE"
    mapped = contract.native_error_map.get(native_code)
    if mapped is None or mapped not in CLOSED_ERRORS:
        raise W8UCreditError("E_WIRE", f"unmapped native XWD error: {native_code!r}")
    return mapped


def _mask_shift(mask: int) -> int:
    if mask <= 0 or (mask & 0xFF) == 0 and ((mask >> 8) & 0xFF) == 0 and ((mask >> 16) & 0xFF) == 0 and ((mask >> 24) & 0xFF) == 0:
        raise W8UCreditError("E_WIRE", f"invalid channel mask {mask:#x}")
    shift = (mask & -mask).bit_length() - 1
    if (mask >> shift) != 0xFF:
        raise W8UCreditError("E_WIRE", f"non-8bit channel mask {mask:#x}")
    return shift


def validate_and_convert_xwd(
    xwd_bytes: bytes,
    *,
    expected_width: int,
    expected_height: int,
    contract: ContractEvidence,
    native_validator: Callable[[bytes], str | None] | None = None,
) -> bytes:
    """Validate W8U XWD subset before canonical conversion and return top-down RGB24 bytes."""
    contract = validate_contract_evidence(contract)
    if not isinstance(xwd_bytes, (bytes, bytearray, memoryview)):
        raise W8UCreditError("E_WIRE", "XWD payload must be bytes")
    raw = bytes(xwd_bytes)
    subset = contract.xwd_subset
    min_hdr = _require_exact_int(subset["min_header_bytes"], "min_header_bytes", minimum=100, maximum=U32_MAX)
    max_hdr = _require_exact_int(subset["max_header_bytes"], "max_header_bytes", minimum=min_hdr, maximum=U32_MAX)
    max_ncolors = _require_exact_int(subset["max_ncolors"], "max_ncolors", minimum=0, maximum=U32_MAX)

    if len(raw) < min_hdr:
        raise W8UCreditError("E_LENGTH", "XWD shorter than minimum header size")

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
    ) = XWD_HEADER_STRUCT.unpack_from(raw, 0)

    if header_size < min_hdr or header_size > max_hdr:
        raise W8UCreditError("E_LENGTH", f"XWD header_size {header_size} outside [{min_hdr}, {max_hdr}]")
    if file_version != 7 or pixmap_format != 2 or xoffset != 0 or window_bdrwidth != 0:
        raise W8UCreditError("E_WIRE", "invalid XWD version, format, xoffset, or border width")
    if byte_order != subset["byte_order"]:
        raise W8UCreditError("E_WIRE", f"invalid XWD byte_order {byte_order}")
    if bitmap_bit_order != subset["bitmap_bit_order"]:
        raise W8UCreditError("E_WIRE", f"invalid XWD bitmap_bit_order {bitmap_bit_order}")
    if ncolors > max_ncolors:
        raise W8UCreditError("E_WIRE", f"XWD ncolors {ncolors} exceeds max_ncolors {max_ncolors}")
    if bits_per_pixel not in subset["allowed_bpp"] or pixmap_depth not in (24, 32):
        raise W8UCreditError("E_WIRE", f"unsupported XWD bpp={bits_per_pixel} depth={pixmap_depth}")
    if bitmap_pad not in subset["bitmap_pad_bits"] or bitmap_unit not in subset["bitmap_pad_bits"]:
        raise W8UCreditError("E_WIRE", "unsupported XWD bitmap_pad or bitmap_unit")
    if visual_class not in (4, 5) or bits_per_rgb != 8:
        raise W8UCreditError("E_WIRE", "unsupported XWD visual_class or bits_per_rgb")
    if (
        red_mask != subset["red_mask"]
        or green_mask != subset["green_mask"]
        or blue_mask != subset["blue_mask"]
    ):
        raise W8UCreditError("E_WIRE", "XWD channel masks do not match frozen contract subset")

    if (
        pixmap_width != expected_width
        or pixmap_height != expected_height
        or window_width != expected_width
        or window_height != expected_height
    ):
        raise W8UCreditError("E_GEOMETRY", "XWD dimensions do not match binding geometry")

    bytes_per_px = bits_per_pixel // 8
    min_stride = expected_width * bytes_per_px
    pad_bytes = bitmap_pad // 8
    max_stride = min_stride + 4096
    if (
        bytes_per_line < min_stride
        or bytes_per_line > max_stride
        or (bytes_per_line % pad_bytes) != 0
    ):
        raise W8UCreditError("E_GEOMETRY", f"invalid XWD bytes_per_line {bytes_per_line}")

    pixel_offset = header_size + ncolors * 12
    expected_total = pixel_offset + bytes_per_line * expected_height
    if len(raw) != expected_total:
        raise W8UCreditError("E_LENGTH", f"XWD byte size {len(raw)} != expected {expected_total}")

    if native_validator is not None:
        nat_err = native_validator(raw)
        if nat_err is not None:
            code = translate_native_xwd_error(nat_err, contract)
            raise W8UCreditError(code, f"native XWD validator rejected payload: {nat_err}")

    r_shift = _mask_shift(red_mask)
    g_shift = _mask_shift(green_mask)
    b_shift = _mask_shift(blue_mask)
    endian = "big" if byte_order == 1 else "little"

    out = bytearray(expected_width * expected_height * 3)
    out_pos = 0
    for row in range(expected_height):
        row_start = pixel_offset + row * bytes_per_line
        for col in range(expected_width):
            px_start = row_start + col * bytes_per_px
            px_val = int.from_bytes(raw[px_start:px_start + bytes_per_px], endian)
            out[out_pos] = (px_val >> r_shift) & 0xFF
            out[out_pos + 1] = (px_val >> g_shift) & 0xFF
            out[out_pos + 2] = (px_val >> b_shift) & 0xFF
            out_pos += 3
    return bytes(out)


def build_xwd_bytes(
    rgb24_bytes: bytes,
    *,
    width: int,
    height: int,
    contract: ContractEvidence,
    bits_per_pixel: int = 32,
    extra_header_bytes: bytes = b"",
    row_pad_bytes: bytes = b"",
    alpha_fill: int = 0,
    byte_order: int | None = None,
    bitmap_bit_order: int | None = None,
    ncolors: int = 0,
) -> bytes:
    """Construct a wire-format XWD image from canonical RGB24 bytes for testing and transport."""
    subset = contract.xwd_subset
    w = _require_exact_int(width, "width", minimum=1, maximum=U32_MAX, err_code="E_GEOMETRY")
    h = _require_exact_int(height, "height", minimum=1, maximum=U32_MAX, err_code="E_GEOMETRY")
    if len(rgb24_bytes) != w * h * 3:
        raise W8UCreditError("E_LENGTH", "rgb24_bytes length must equal width * height * 3")
    bo = subset["byte_order"] if byte_order is None else byte_order
    bbo = subset["bitmap_bit_order"] if bitmap_bit_order is None else bitmap_bit_order
    red_mask = subset["red_mask"]
    green_mask = subset["green_mask"]
    blue_mask = subset["blue_mask"]
    r_shift = _mask_shift(red_mask)
    g_shift = _mask_shift(green_mask)
    b_shift = _mask_shift(blue_mask)
    used_mask = red_mask | green_mask | blue_mask
    alpha_mask = (~used_mask) & 0xFFFFFFFF
    a_shift = ((alpha_mask & -alpha_mask).bit_length() - 1) if alpha_mask else 24

    bytes_per_px = bits_per_pixel // 8
    bytes_per_line = w * bytes_per_px + len(row_pad_bytes)
    header_size = 100 + len(extra_header_bytes)
    hdr = XWD_HEADER_STRUCT.pack(
        header_size,
        7,
        2,
        24,
        w,
        h,
        0,
        bo,
        32,
        bbo,
        32,
        bits_per_pixel,
        bytes_per_line,
        4,
        red_mask,
        green_mask,
        blue_mask,
        8,
        256,
        ncolors,
        w,
        h,
        0,
        0,
        0,
    )
    endian = "big" if bo == 1 else "little"
    body = bytearray()
    pos = 0
    for _ in range(h):
        for _ in range(w):
            r = rgb24_bytes[pos]
            g = rgb24_bytes[pos + 1]
            b = rgb24_bytes[pos + 2]
            pos += 3
            val = (r << r_shift) | (g << g_shift) | (b << b_shift)
            if bits_per_pixel == 32 and alpha_mask:
                val |= (alpha_fill & 0xFF) << a_shift
            body.extend(val.to_bytes(bytes_per_px, endian))
        body.extend(row_pad_bytes)
    cmap = b"\x00" * (ncolors * 12)
    return hdr + bytes(extra_header_bytes) + cmap + bytes(body)


def compute_rgb24_sha256(rgb24_bytes: bytes, contract: ContractEvidence) -> bytes:
    """Compute contract-domain-separated SHA-256 digest (32 raw bytes) over canonical RGB24 bytes."""
    contract = validate_contract_evidence(contract)
    prefix = bytes(contract.digest_domains["rgb24"])
    return hashlib.sha256(prefix + bytes(rgb24_bytes)).digest()


def compute_ui_sha256(ui_bytes: bytes, contract: ContractEvidence) -> str:
    """Compute UI domain SHA-256 hex digest."""
    contract = validate_contract_evidence(contract)
    prefix = bytes(contract.digest_domains["ui"])
    return hashlib.sha256(prefix + bytes(ui_bytes)).hexdigest()


def compute_sim_sha256(sim_bytes: bytes, contract: ContractEvidence) -> str:
    """Compute simulation domain SHA-256 hex digest."""
    contract = validate_contract_evidence(contract)
    prefix = bytes(contract.digest_domains["sim"])
    return hashlib.sha256(prefix + bytes(sim_bytes)).hexdigest()


@dataclass(frozen=True)
class ValidatedFrame:
    """Validated W8U frame record and its closed metadata projection."""

    seq: int
    tick: int | None
    wire_tick: int
    tick_valid: bool
    capture_mono_ns: int
    width: int
    height: int
    stride: int
    pixel_format: int
    sha256: str
    binding_sha256: str
    rgb24_bytes: bytes
    record_bytes: int

    def metadata_dict(self) -> dict[str, Any]:
        """Return the closed 10-field frame metadata dictionary."""
        return {
            "seq": self.seq,
            "tick": self.tick,
            "tick_valid": self.tick_valid,
            "capture_mono_ns": self.capture_mono_ns,
            "width": self.width,
            "height": self.height,
            "stride": self.stride,
            "pixel_format": self.pixel_format,
            "sha256": self.sha256,
            "binding_sha256": self.binding_sha256,
        }


def encode_wire_frame(
    *,
    seq: int,
    wire_tick: int,
    tick_valid: bool,
    pair_under_grab: bool,
    capture_mono_ns: int,
    width: int,
    height: int,
    sha256_rgb24: bytes,
    binding_sha256: bytes,
    xwd_a: bytes,
    xwd_b: bytes,
    extra_flags: int = 0,
    trailing_bytes: bytes = b"",
    pixel_format: int = PIXEL_FORMAT_RGB24,
    stride: int | None = None,
    version: int = FRAME_VERSION,
    header_bytes: int = FRAME_HEADER_BYTES,
    magic: bytes = FRAME_MAGIC,
) -> bytes:
    """Pack a big-endian W8UF wire frame for transport or testing."""
    payload = (
        INNER_LEN_STRUCT.pack(len(xwd_a))
        + bytes(xwd_a)
        + INNER_LEN_STRUCT.pack(len(xwd_b))
        + bytes(xwd_b)
        + bytes(trailing_bytes)
    )
    payload_bytes = len(payload)
    record_bytes = header_bytes + payload_bytes
    flags = (
        (FLAG_PAIR_UNDER_GRAB if pair_under_grab else 0)
        | (FLAG_TICK_VALID if tick_valid else 0)
        | extra_flags
    )
    actual_stride = width * 3 if stride is None else stride
    header = FRAME_HEADER_STRUCT.pack(
        magic,
        version,
        header_bytes,
        record_bytes,
        seq,
        wire_tick,
        capture_mono_ns,
        width,
        height,
        actual_stride,
        pixel_format,
        flags,
        payload_bytes,
        bytes(sha256_rgb24),
        bytes(binding_sha256),
    )
    return header + payload


def validate_wire_frame(
    frame_bytes: bytes,
    *,
    binding: ConfirmedBinding,
    expected_seq: int,
    contract: ContractEvidence,
    native_xwd_validator: Callable[[bytes], str | None] | None = None,
) -> ValidatedFrame:
    """Validate a big-endian W8UF frame against binding, caps, XWD subset, and RGB24 digests."""
    contract = validate_contract_evidence(contract)
    if not isinstance(frame_bytes, (bytes, bytearray, memoryview)):
        raise W8UCreditError("E_WIRE", "frame must be bytes")
    raw = memoryview(frame_bytes).cast("B")
    if len(raw) < FRAME_HEADER_BYTES:
        raise W8UCreditError("E_LENGTH", f"frame shorter than {FRAME_HEADER_BYTES} header bytes")

    (
        magic,
        version,
        header_bytes,
        record_bytes,
        seq,
        wire_tick,
        capture_mono_ns,
        width,
        height,
        stride,
        pixel_format,
        flags,
        payload_bytes,
        sha256_rgb24,
        binding_sha256,
    ) = FRAME_HEADER_STRUCT.unpack_from(raw, 0)

    if magic != FRAME_MAGIC or version != FRAME_VERSION or header_bytes != FRAME_HEADER_BYTES:
        raise W8UCreditError("E_WIRE", "invalid W8UF magic, version, or header_bytes")

    # Enforce smaller negotiated cap and geometry cap BEFORE body allocation/slice
    cap = effective_record_cap(binding.width, binding.height, binding.max_record_bytes)
    if (
        record_bytes < FRAME_HEADER_BYTES + INNER_LENGTH_BYTES
        or record_bytes > cap
        or payload_bytes != record_bytes - FRAME_HEADER_BYTES
        or len(raw) != record_bytes
    ):
        raise W8UCreditError("E_LENGTH", "frame record_bytes/payload_bytes violates cap or wire size")

    if binding_sha256 != binding.binding_sha256:
        raise W8UCreditError("E_BINDING", "frame binding_sha256 does not match confirmed binding")

    if width != binding.width or height != binding.height or stride != binding.width * 3:
        raise W8UCreditError("E_GEOMETRY", "frame width/height/stride mismatch")

    if pixel_format != PIXEL_FORMAT_RGB24:
        raise W8UCreditError("E_WIRE", f"unsupported pixel_format {pixel_format}")

    if (flags & ~ALLOWED_FRAME_FLAGS_MASK) != 0 or (flags & FLAG_PAIR_UNDER_GRAB) == 0:
        raise W8UCreditError("E_WIRE", f"invalid frame flags {flags:#x}; pair_under_grab required")

    if expected_seq < 1 or expected_seq > U64_MAX or seq != expected_seq:
        raise W8UCreditError("E_SEQ", f"expected contiguous seq {expected_seq}, got {seq}")

    tick_valid = bool(flags & FLAG_TICK_VALID)
    if binding.tick_mode == "committed":
        if not tick_valid:
            raise W8UCreditError("E_TICK", "committed tick_mode requires tick_valid=True")
        projected_tick: int | None = int(wire_tick)
    elif binding.tick_mode == "unavailable":
        if tick_valid or wire_tick != 0:
            raise W8UCreditError(
                "E_TICK", "unavailable tick_mode requires wire tick 0 and tick_valid=False"
            )
        projected_tick = None
    else:
        raise W8UCreditError("E_TICK", f"unknown tick_mode {binding.tick_mode!r}")

    # Parse payload: lenA u64, exact XWD A, lenB u64, exact XWD B with no trailing bytes
    body = bytes(raw[FRAME_HEADER_BYTES:record_bytes])
    len_a = INNER_LEN_STRUCT.unpack_from(body, 0)[0]
    if 8 + len_a + 8 > len(body):
        raise W8UCreditError("E_LENGTH", "lenA exceeds payload_bytes")
    xwd_a = body[8:8 + len_a]
    len_b = INNER_LEN_STRUCT.unpack_from(body, 8 + len_a)[0]
    if 8 + len_a + 8 + len_b != len(body):
        raise W8UCreditError("E_LENGTH", "lenA + lenB + 16 must equal payload_bytes with no trailing bytes")
    xwd_b = body[16 + len_a:16 + len_a + len_b]

    rgb24_a = validate_and_convert_xwd(
        xwd_a,
        expected_width=binding.width,
        expected_height=binding.height,
        contract=contract,
        native_validator=native_xwd_validator,
    )
    rgb24_b = validate_and_convert_xwd(
        xwd_b,
        expected_width=binding.width,
        expected_height=binding.height,
        contract=contract,
        native_validator=native_xwd_validator,
    )

    digest_a = compute_rgb24_sha256(rgb24_a, contract)
    digest_b = compute_rgb24_sha256(rgb24_b, contract)
    if digest_a != digest_b:
        raise W8UCreditError("E_TORN", "XWD pair canonical RGB24 hashes differ")
    if digest_a != sha256_rgb24:
        raise W8UCreditError("E_HASH", "canonical RGB24 digest does not match advertised sha256_rgb24")

    return ValidatedFrame(
        seq=seq,
        tick=projected_tick,
        wire_tick=wire_tick,
        tick_valid=tick_valid,
        capture_mono_ns=capture_mono_ns,
        width=width,
        height=height,
        stride=stride,
        pixel_format=pixel_format,
        sha256=digest_a.hex(),
        binding_sha256=binding.binding_sha256_hex,
        rgb24_bytes=rgb24_a,
        record_bytes=record_bytes,
    )


