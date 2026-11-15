"""pslot_encode: pslot png."""
from __future__ import annotations
import hashlib
import importlib
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import numpy as np
from vadapt import xfb

from ._pslot_encode_contract import (
    AUSENTE, RUNTIME_LABEL_VM, RUNTIME_LABEL_HOST, PNG_SIGNATURE, PNG_BIT_DEPTH_8,
    PNG_COLOR_TYPE_RGB, PNG_COMPRESSION_DEFLATE, PNG_FILTER_ADAPTIVE, PNG_INTERLACE_NONE,
    PNG_FILTER_NONE, MAX_SIDE, MAX_RGB_BYTES, MODE_LOSSLESS_PNG, MIME_PNG, CODEC_STDLIB_PNG,
    ROLE_DEFAULT_PERCEIVER, ROLE_ALTERNATIVE_PERCEIVER, ROLE_OUT_OF_LOOP_JUDGE,
    MODEL_META_MUSE_SPARK, MODEL_GEMINI_FLASH, MODEL_GEMINI_ARGON, MODEL_DEEPSEEK_FLASH,
    ROLE_MODEL_BINDINGS, PLANNED_PSLOT_EXPORTS, PslotEncodeRefusal, GeometryRefusalError,
    EncodingContractRefusalError, DispatchGateRefusalError, EncodingRefusalError,
    ModelCapabilityEntry, OperatorModelConfig, DEFAULT_CAPABILITY_MATRIX,
    configure_capability_matrix, LocalHttpDesignSeam, inspect_pslot_export, _is_exact_int,
    _validate_dimensions, ValidatedFrame, accept_xwd_frame, accept_validated_frame,
)

def _pack_png_chunk(chunk_type: bytes, chunk_data: bytes) -> Tuple[bytes, int]:
    """Pack a single PNG chunk using struct and zlib.crc32, returning (chunk_bytes, crc32)."""
    if not isinstance(chunk_type, bytes) or len(chunk_type) != 4:
        raise EncodingContractRefusalError("E_PNG_CORRUPT", "PNG chunk_type must be 4 ASCII bytes")
    if not isinstance(chunk_data, bytes):
        raise EncodingContractRefusalError("E_PNG_CORRUPT", "PNG chunk_data must be bytes")
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(chunk_data, crc) & 0xFFFFFFFF
    packed = struct.pack(">I", len(chunk_data)) + chunk_type + chunk_data + struct.pack(">I", crc)
    return packed, crc


def encode_png_rgb24(
    rgb_bytes: Union[bytes, bytearray, memoryview],
    width: int,
    height: int,
    *,
    compress_level: int = 6,
) -> Tuple[bytes, Tuple[Tuple[str, int], ...]]:
    """Encode canonical RGB24 bytes into a lossless PNG in RAM using only stdlib zlib and struct."""
    w, h = _validate_dimensions(width, height)
    if not isinstance(rgb_bytes, (bytes, bytearray, memoryview)):
        raise GeometryRefusalError("E_RGB_BUFFER_TYPE", "rgb_bytes must be a byte buffer")
    raw = rgb_bytes if isinstance(rgb_bytes, bytes) else bytes(rgb_bytes)
    expected_len = w * h * 3
    if len(raw) != expected_len:
        raise GeometryRefusalError(
            "E_RGB_BYTE_LENGTH",
            f"RGB24 byte length {len(raw)} != {expected_len}",
            details={"actual_bytes": len(raw), "expected_bytes": expected_len},
        )
    if not _is_exact_int(compress_level) or not (0 <= compress_level <= 9):
        raise EncodingContractRefusalError(
            "E_ENCODING_UNSUPPORTED",
            f"compress_level must be an integer in 0..9, got {compress_level!r}",
        )

    ihdr_data = struct.pack(
        ">IIBBBBB",
        w,
        h,
        PNG_BIT_DEPTH_8,
        PNG_COLOR_TYPE_RGB,
        PNG_COMPRESSION_DEFLATE,
        PNG_FILTER_ADAPTIVE,
        PNG_INTERLACE_NONE,
    )
    ihdr_chunk, ihdr_crc = _pack_png_chunk(b"IHDR", ihdr_data)

    row_bytes = w * 3
    scanline_stride = row_bytes + 1
    scanlines = bytearray(h * scanline_stride)
    mv = memoryview(raw)
    for y in range(h):
        dst_off = y * scanline_stride
        scanlines[dst_off] = PNG_FILTER_NONE
        src_off = y * row_bytes
        scanlines[dst_off + 1 : dst_off + scanline_stride] = mv[src_off : src_off + row_bytes]

    compressed = zlib.compress(bytes(scanlines), compress_level)
    idat_chunk, idat_crc = _pack_png_chunk(b"IDAT", compressed)
    iend_chunk, iend_crc = _pack_png_chunk(b"IEND", b"")

    png_bytes = PNG_SIGNATURE + ihdr_chunk + idat_chunk + iend_chunk
    crc_summary = (
        ("IHDR", ihdr_crc),
        ("IDAT", idat_crc),
        ("IEND", iend_crc),
    )
    return png_bytes, crc_summary


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_and_verify_png(png_bytes: Union[bytes, bytearray, memoryview]) -> Tuple[int, int, bytes, Tuple[Tuple[str, int], ...]]:
    """Independently decode a 24-bit RGB PNG using only stdlib struct and zlib, validating every chunk and CRC."""
    if not isinstance(png_bytes, (bytes, bytearray, memoryview)):
        raise EncodingContractRefusalError("E_PNG_CORRUPT", "png_bytes must be a byte buffer")
    buf = png_bytes if isinstance(png_bytes, bytes) else bytes(png_bytes)
    if len(buf) < len(PNG_SIGNATURE) + 12 or buf[:8] != PNG_SIGNATURE:
        raise EncodingContractRefusalError("E_PNG_CORRUPT", "Invalid or missing PNG signature")

    offset = 8
    chunks: List[Tuple[str, int]] = []
    ihdr_parsed: Optional[Tuple[int, int, int, int, int, int, int]] = None
    idat_parts: List[bytes] = []
    seen_iend = False

    while offset < len(buf):
        if seen_iend:
            raise EncodingContractRefusalError("E_PNG_CORRUPT", "Trailing bytes after IEND chunk")
        if offset + 8 > len(buf):
            raise EncodingContractRefusalError("E_PNG_CORRUPT", "Truncated PNG chunk header")
        length, raw_type = struct.unpack(">I4s", buf[offset : offset + 8])
        offset += 8
        if offset + length + 4 > len(buf):
            raise EncodingContractRefusalError("E_PNG_CORRUPT", "Truncated PNG chunk data or CRC")
        chunk_data = buf[offset : offset + length]
        offset += length
        (expected_crc,) = struct.unpack(">I", buf[offset : offset + 4])
        offset += 4

        computed_crc = zlib.crc32(raw_type)
        computed_crc = zlib.crc32(chunk_data, computed_crc) & 0xFFFFFFFF
        try:
            chunk_name = raw_type.decode("ascii")
        except UnicodeDecodeError as exc:
            raise EncodingContractRefusalError("E_PNG_CORRUPT", "Non-ASCII chunk type") from exc

        if computed_crc != expected_crc:
            raise EncodingContractRefusalError(
                "E_PNG_CORRUPT",
                f"CRC mismatch in chunk {chunk_name}: expected {expected_crc:#010x}, computed {computed_crc:#010x}",
                details={"chunk": chunk_name, "expected_crc": expected_crc, "computed_crc": computed_crc},
            )

        if not chunks and chunk_name != "IHDR":
            raise EncodingContractRefusalError("E_PNG_CORRUPT", f"First chunk must be IHDR, got {chunk_name}")

        if chunk_name == "IHDR":
            if ihdr_parsed is not None or length != 13:
                raise EncodingContractRefusalError("E_PNG_CORRUPT", "Duplicate or malformed IHDR chunk")
            w, h, bit_depth, color_type, comp, filt, interlace = struct.unpack(">IIBBBBB", chunk_data)
            if w <= 0 or h <= 0 or w > MAX_SIDE or h > MAX_SIDE:
                raise EncodingContractRefusalError("E_PNG_CORRUPT", f"Invalid IHDR dimensions {w}x{h}")
            if (
                bit_depth != PNG_BIT_DEPTH_8
                or color_type != PNG_COLOR_TYPE_RGB
                or comp != PNG_COMPRESSION_DEFLATE
                or filt != PNG_FILTER_ADAPTIVE
                or interlace != PNG_INTERLACE_NONE
            ):
                raise EncodingContractRefusalError(
                    "E_PNG_CORRUPT",
                    "Unsupported IHDR parameters (require 8-bit RGB non-interlaced)",
                )
            ihdr_parsed = (w, h, bit_depth, color_type, comp, filt, interlace)
        elif chunk_name == "IDAT":
            if ihdr_parsed is None:
                raise EncodingContractRefusalError("E_PNG_CORRUPT", "IDAT before IHDR")
            idat_parts.append(chunk_data)
        elif chunk_name == "IEND":
            if length != 0:
                raise EncodingContractRefusalError("E_PNG_CORRUPT", "IEND chunk must have length 0")
            seen_iend = True

        chunks.append((chunk_name, computed_crc))

    if ihdr_parsed is None or not idat_parts or not seen_iend:
        raise EncodingContractRefusalError("E_PNG_CORRUPT", "Missing required IHDR, IDAT, or IEND chunk")

    width, height = ihdr_parsed[0], ihdr_parsed[1]
    try:
        raw_scanlines = zlib.decompress(b"".join(idat_parts))
    except zlib.error as exc:
        raise EncodingContractRefusalError("E_PNG_CORRUPT", f"Invalid zlib stream in IDAT: {exc}") from exc

    row_bytes = width * 3
    stride = row_bytes + 1
    if len(raw_scanlines) != height * stride:
        raise EncodingContractRefusalError(
            "E_PNG_CORRUPT",
            f"Decompressed scanline length {len(raw_scanlines)} != {height * stride}",
        )

    out_rgb = bytearray(width * height * 3)
    prev_row = bytearray(row_bytes)
    cur_row = bytearray(row_bytes)
    bpp = 3

    for y in range(height):
        line_start = y * stride
        filter_type = raw_scanlines[line_start]
        scan = raw_scanlines[line_start + 1 : line_start + stride]
        if filter_type == 0:
            cur_row[:] = scan
        elif filter_type == 1:
            for i in range(row_bytes):
                left = cur_row[i - bpp] if i >= bpp else 0
                cur_row[i] = (scan[i] + left) & 0xFF
        elif filter_type == 2:
            for i in range(row_bytes):
                cur_row[i] = (scan[i] + prev_row[i]) & 0xFF
        elif filter_type == 3:
            for i in range(row_bytes):
                left = cur_row[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                cur_row[i] = (scan[i] + ((left + up) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(row_bytes):
                left = cur_row[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                up_left = prev_row[i - bpp] if i >= bpp else 0
                cur_row[i] = (scan[i] + _paeth_predictor(left, up, up_left)) & 0xFF
        else:
            raise EncodingContractRefusalError("E_PNG_CORRUPT", f"Invalid PNG scanline filter type {filter_type}")

        out_off = y * row_bytes
        out_rgb[out_off : out_off + row_bytes] = cur_row
        prev_row[:] = cur_row

    return width, height, bytes(out_rgb), tuple(chunks)


