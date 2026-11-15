"""Focused tests for vadapt.pslot_encode (jq3-e-pslot-cache-contract).

Verifies independent in-memory lossless PNG encoding via zlib and struct,
independent stdlib PNG chunk/CRC decoding and byte-for-byte RGB comparison,
explicit per-call lossy opt-in and receipt provenance, rejection of implicit
JPEG/resizing/silent fallback, pre-provider geometry and encoding refusal,
distinct raw-frame vs encoded-payload digests, network-first role bindings
(Meta Muse Spark 1.3, Gemini 3.8 Flash, Gemini 4 Argon with quota-floor refusal,
DeepSeek V4.1 Flash not configured), AUSENTE semantics, seeded property checks
with minimal counterexample shrinking, and bounded barrier/event concurrency.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import random
import struct
import threading
import unittest
import zlib
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

PROPERTY_SEED: int = 20261020


def _lazy_pslot_encode() -> Any:
    return importlib.import_module("vadapt.pslot_encode")


def _lazy_xfb() -> Any:
    return importlib.import_module("vadapt.xfb")


def _build_synthetic_xwd(
    width: int,
    height: int,
    pixel_fn: Callable[[int, int], Tuple[int, int, int]],
    *,
    byte_order: int = 0,
    pad: int = 0,
    name: bytes = b"Xvfb_screen0\x00",
) -> bytes:
    xfb = _lazy_xfb()
    hs = xfb.MIN_HEADER + len(name)
    bpl = width * 4 + pad
    head = (
        struct.pack(
            ">25I",
            hs,
            xfb.XWD_VERSION,
            xfb.ZPIXMAP,
            24,
            width,
            height,
            0,
            byte_order,
            32,
            byte_order,
            32,
            32,
            bpl,
            xfb.TRUECOLOR,
            0xFF0000,
            0x00FF00,
            0x0000FF,
            8,
            0,
            0,
            width,
            height,
            0,
            0,
            0,
        )
        + name
    )
    body = bytearray()
    for y in range(height):
        row = bytearray(bpl)
        for x in range(width):
            r, g, b = pixel_fn(x, y)
            if byte_order == 0:
                row[x * 4 : x * 4 + 4] = bytes((b, g, r, 0))
            else:
                row[x * 4 : x * 4 + 4] = bytes((0, r, g, b))
        body.extend(row)
    return head + bytes(body)


def _independent_stdlib_png_decode(png_data: bytes) -> Tuple[int, int, bytes, List[Tuple[str, int, int]]]:
    """Test-side independent PNG decoder using only stdlib struct and zlib."""
    if len(png_data) < 8 or png_data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG signature")
    pos = 8
    chunks_meta: List[Tuple[str, int, int]] = []
    width = 0
    height = 0
    idat_stream = bytearray()
    saw_ihdr = False
    saw_iend = False

    while pos < len(png_data):
        if saw_iend:
            raise ValueError("Trailing bytes after IEND")
        if pos + 8 > len(png_data):
            raise ValueError("Truncated chunk header")
        length, ctype_bytes = struct.unpack(">I4s", png_data[pos : pos + 8])
        pos += 8
        if pos + length + 4 > len(png_data):
            raise ValueError("Truncated chunk payload/CRC")
        cdata = png_data[pos : pos + length]
        pos += length
        (stored_crc,) = struct.unpack(">I", png_data[pos : pos + 4])
        pos += 4

        calc_crc = zlib.crc32(ctype_bytes)
        calc_crc = zlib.crc32(cdata, calc_crc) & 0xFFFFFFFF
        if calc_crc != stored_crc:
            raise ValueError(f"CRC mismatch for {ctype_bytes!r}: {calc_crc:#x} != {stored_crc:#x}")

        cname = ctype_bytes.decode("ascii")
        chunks_meta.append((cname, length, calc_crc))

        if cname == "IHDR":
            if saw_ihdr or length != 13:
                raise ValueError("Invalid IHDR")
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(">IIBBBBB", cdata)
            if (bit_depth, color_type, comp, filt, interlace) != (8, 2, 0, 0, 0):
                raise ValueError("Unexpected IHDR color/filter/interlace fields")
            saw_ihdr = True
        elif cname == "IDAT":
            if not saw_ihdr:
                raise ValueError("IDAT before IHDR")
            idat_stream.extend(cdata)
        elif cname == "IEND":
            if length != 0:
                raise ValueError("Non-empty IEND")
            saw_iend = True

    if not saw_ihdr or not saw_iend or not idat_stream:
        raise ValueError("Incomplete PNG chunk sequence")

    decompressed = zlib.decompress(bytes(idat_stream))
    row_bytes = width * 3
    stride = row_bytes + 1
    if len(decompressed) != height * stride:
        raise ValueError("Scanline buffer size mismatch")

    reconstructed = bytearray(width * height * 3)
    for y in range(height):
        line = decompressed[y * stride : (y + 1) * stride]
        if line[0] != 0:
            raise ValueError(f"Expected filter type 0, got {line[0]}")
        reconstructed[y * row_bytes : (y + 1) * row_bytes] = line[1:]

    return width, height, bytes(reconstructed), chunks_meta


class DeterministicFakeLossyCodec:
    """Injected deterministic fake lossy codec for testing opt-in and non-fallback invariants."""

    def __init__(
        self,
        *,
        name: str = "fake_quant_rgb",
        mime_type: str = "image/x-fake-lossy",
        should_fail: bool = False,
        alter_dimensions: Optional[Tuple[int, int]] = None,
        return_png_header: bool = False,
    ) -> None:
        self.name = name
        self.mime_type = mime_type
        self.should_fail = should_fail
        self.alter_dimensions = alter_dimensions
        self.return_png_header = return_png_header
        self.calls: int = 0

    def encode(self, rgb_bytes: bytes, width: int, height: int) -> Any:
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("simulated injected lossy codec failure")
        if self.return_png_header:
            return b"\x89PNG\r\n\x1a\n" + b"fake"
        quantized = bytes((b & 0xF0) for b in rgb_bytes)
        header = struct.pack(">4sII", b"LSY1", width, height)
        payload = header + zlib.compress(quantized, 1)
        if self.alter_dimensions is not None:
            return payload, self.alter_dimensions[0], self.alter_dimensions[1]
        return payload


class DeterministicFakeTransport:
    """Deterministic in-RAM fake transport with zero network/socket usage."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def send(self, *, payload: bytes, receipt: Any, role: str, model_name: str) -> Dict[str, Any]:
        if not isinstance(payload, bytes) or len(payload) == 0:
            raise AssertionError("Transport requires non-empty in-memory payload bytes")
        entry = {
            "role": role,
            "model_name": model_name,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_len": len(payload),
            "receipt_mode": receipt.encoding_mode,
        }
        with self._lock:
            self.calls.append(entry)
        return {
            "transport": "fake_in_ram_transport",
            "wire_digest": entry["payload_sha256"],
            "bytes_sent": len(payload),
            "runtime_environment": "VM",
            "host_measurement": "AUSENTE",
        }


class DeterministicFakeProvider:
    """Deterministic in-RAM fake provider that inspects receipts without live models or keys."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def evaluate(
        self,
        *,
        receipt: Any,
        transport_receipt: Dict[str, Any],
        role: str,
        model_name: str,
    ) -> Dict[str, Any]:
        record = {
            "role": role,
            "model_name": model_name,
            "input_digest": receipt.input_digest,
            "encoded_payload_digest": receipt.encoded_payload_digest,
            "wire_digest": transport_receipt["wire_digest"],
        }
        with self._lock:
            self.calls.append(record)
        return {
            "status": "OK",
            "verdict": "DETERMINISTIC_PASS",
            "role": role,
            "model_name": model_name,
            "input_digest": receipt.input_digest,
            "encoded_payload_digest": receipt.encoded_payload_digest,
        }


def _configured_matrix_for_tests() -> Dict[str, Any]:
    ps = _lazy_pslot_encode()
    return ps.configure_capability_matrix(
        {
            ps.MODEL_META_MUSE_SPARK: ps.OperatorModelConfig(
                supports_png=True,
                max_payload_bytes=4 * 1024 * 1024,
                quota_floor=10,
                tariff_per_unit=0.01,
            ),
            ps.MODEL_GEMINI_FLASH: ps.OperatorModelConfig(
                supports_png=True,
                max_payload_bytes=4 * 1024 * 1024,
                quota_floor=15,
                tariff_per_unit=0.02,
            ),
            ps.MODEL_GEMINI_ARGON: ps.OperatorModelConfig(
                supports_png=True,
                max_payload_bytes=8 * 1024 * 1024,
                quota_floor=50,
                tariff_per_unit=0.05,
            ),
        }
    )


def _shrink_counterexample(
    cases: List[Tuple[int, int, bytes]],
    predicate: Callable[[int, int, bytes], bool],
) -> Optional[Tuple[int, int, int]]:
    """Find minimal counterexample (width, height, byte_len) if predicate fails on any case."""
    failing: List[Tuple[int, int, bytes]] = []
    for w, h, rgb in cases:
        if not predicate(w, h, rgb):
            failing.append((w, h, rgb))
    if not failing:
        return None
    failing.sort(key=lambda item: (item[0] * item[1], item[0], item[1]))
    w0, h0, rgb0 = failing[0]
    while w0 > 1 and not predicate(w0 - 1, h0, rgb0[: (w0 - 1) * h0 * 3]):
        w0 -= 1
        rgb0 = rgb0[: w0 * h0 * 3]
    while h0 > 1 and not predicate(w0, h0 - 1, rgb0[: w0 * (h0 - 1) * 3]):
        h0 -= 1
        rgb0 = rgb0[: w0 * h0 * 3]
    return (w0, h0, len(rgb0))


class _InjectedFrameBox:
    def __init__(self, rgb_bytes: bytes, width: int, height: int) -> None:
        self.rgb_bytes = rgb_bytes
        self.width = width
        self.height = height


class TestJq3EPslotEncode(unittest.TestCase):
    """Focused test suite for vadapt.pslot_encode."""

    def test_snapshot_xfb_api_facts_and_unmerged_pslot_ausente(self) -> None:
        xfb = _lazy_xfb()
        ps = _lazy_pslot_encode()

        sig_validate = inspect.signature(xfb.validate_frame)
        self.assertEqual(tuple(sig_validate.parameters.keys()), ("buf", "width", "height"))
        self.assertNotIn("max_bytes", sig_validate.parameters)

        sig_rgb24 = inspect.signature(xfb.rgb24)
        self.assertEqual(tuple(sig_rgb24.parameters.keys()), ("buf", "hd"))

        xwd_buf = _build_synthetic_xwd(4, 3, lambda x, y: ((x * 29) % 256, (y * 53) % 256, (x + y) % 256))
        hd = xfb.validate_frame(xwd_buf, width=4, height=3)
        raw_rgb = xfb.rgb24(xwd_buf, hd)
        self.assertEqual(len(raw_rgb), 4 * 3 * 3)
        self.assertNotEqual(raw_rgb[:8], ps.PNG_SIGNATURE)

        for mod_name, status in ps.PLANNED_PSLOT_EXPORTS.items():
            self.assertEqual(status, ps.AUSENTE)
            self.assertEqual(ps.inspect_pslot_export(mod_name, "encode"), ps.AUSENTE)

        self.assertIs(ps.inspect_pslot_export("vadapt.pslot_encode", "encode_frame"), ps.encode_frame)

    def test_lossless_png_default_exact_rgb_chunks_and_crcs(self) -> None:
        ps = _lazy_pslot_encode()
        xfb = _lazy_xfb()

        xwd_buf = _build_synthetic_xwd(
            7,
            5,
            lambda x, y: ((x * 37 + y) % 256, (y * 53 + 7) % 256, (x * y) % 256),
            pad=4,
        )
        expected_rgb = xfb.rgb24(xwd_buf)
        expected_raw_sha = xfb.frame_sha256(xwd_buf)

        # Passing real-module control
        encoded = ps.encode_frame(xwd_buf, width=7, height=5, is_xwd=True)
        self.assertEqual(encoded.width, 7)
        self.assertEqual(encoded.height, 5)
        self.assertEqual(encoded.encoding_mode, ps.MODE_LOSSLESS_PNG)
        self.assertFalse(encoded.lossy_opt_in)
        self.assertTrue(encoded.receipt.lossless)
        self.assertEqual(encoded.raw_frame_digest, expected_raw_sha)
        self.assertEqual(encoded.input_digest, expected_raw_sha)
        self.assertEqual(encoded.encoded_payload_digest, hashlib.sha256(encoded.payload).hexdigest())
        self.assertNotEqual(encoded.raw_frame_digest, encoded.encoded_payload_digest)
        self.assertEqual(encoded.receipt.runtime_environment, "VM")
        self.assertEqual(encoded.receipt.host_measurement, "AUSENTE")

        dec_w, dec_h, dec_rgb, chunks_meta = _independent_stdlib_png_decode(encoded.payload)
        self.assertEqual((dec_w, dec_h), (7, 5))
        self.assertEqual(dec_rgb, expected_rgb)
        self.assertEqual([c[0] for c in chunks_meta], ["IHDR", "IDAT", "IEND"])
        self.assertEqual(
            tuple((c[0], c[2]) for c in chunks_meta),
            encoded.receipt.chunk_crcs,
        )

        mod_w, mod_h, mod_rgb, mod_chunks = ps.decode_and_verify_png(encoded.payload)
        self.assertEqual((mod_w, mod_h), (7, 5))
        self.assertEqual(mod_rgb, expected_rgb)
        self.assertEqual(mod_chunks, encoded.receipt.chunk_crcs)

        # Single-condition negative: flip 1 bit in IDAT CRC -> must fail CRC validation
        corrupted_crc_png = bytearray(encoded.payload)
        idat_crc_offset = len(corrupted_crc_png) - 12 - 4
        corrupted_crc_png[idat_crc_offset] ^= 0x01
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx:
            ps.decode_and_verify_png(bytes(corrupted_crc_png))
        self.assertEqual(ctx.exception.code, "E_PNG_CORRUPT")
        with self.assertRaises(ValueError):
            _independent_stdlib_png_decode(bytes(corrupted_crc_png))

    def test_png_structural_corruptions_and_scanline_filters(self) -> None:
        ps = _lazy_pslot_encode()
        rgb = bytes((i * 23 + 5) % 256 for i in range(4 * 3 * 3))
        png_bytes, _ = ps.encode_png_rgb24(rgb, 4, 3)

        # Bad signature
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_sig:
            ps.decode_and_verify_png(b"NOT_A_PNG" + png_bytes[8:])
        self.assertEqual(ctx_sig.exception.code, "E_PNG_CORRUPT")

        # Trailing byte after IEND
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_trail:
            ps.decode_and_verify_png(png_bytes + b"\x00")
        self.assertEqual(ctx_trail.exception.code, "E_PNG_CORRUPT")

        # Invalid compress_level
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_lvl:
            ps.encode_png_rgb24(rgb, 4, 3, compress_level=12)
        self.assertEqual(ctx_lvl.exception.code, "E_ENCODING_UNSUPPORTED")

        # Verify scanline filters 1 (Sub), 2 (Up), 3 (Average), 4 (Paeth) decode accurately
        ihdr_chunk, _ = ps._pack_png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        iend_chunk, _ = ps._pack_png_chunk(b"IEND", b"")
        for f0, f1 in ((1, 2), (3, 4)):
            scan = bytes([f0, 0, 0, 0, 0, 0, 0, f1, 0, 0, 0, 0, 0, 0])
            idat_chunk, _ = ps._pack_png_chunk(b"IDAT", zlib.compress(scan))
            synthetic_png = ps.PNG_SIGNATURE + ihdr_chunk + idat_chunk + iend_chunk
            w, h, out_rgb, _ = ps.decode_and_verify_png(synthetic_png)
            self.assertEqual((w, h), (2, 2))
            self.assertEqual(out_rgb, bytes(12))

        # Invalid filter type 9 -> E_PNG_CORRUPT
        bad_scan = bytes([9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        bad_idat, _ = ps._pack_png_chunk(b"IDAT", zlib.compress(bad_scan))
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_flt:
            ps.decode_and_verify_png(ps.PNG_SIGNATURE + ihdr_chunk + bad_idat + iend_chunk)
        self.assertEqual(ctx_flt.exception.code, "E_PNG_CORRUPT")

    def test_lossy_opt_in_contract_and_no_implicit_jpeg_or_fallback(self) -> None:
        ps = _lazy_pslot_encode()
        rgb = bytes((i * 19) % 256 for i in range(6 * 4 * 3))
        raw_digest = hashlib.sha256(rgb).hexdigest()
        codec = DeterministicFakeLossyCodec()

        # Passing real-module control: explicit per-call lossy_opt_in=True with injected codec
        encoded_lossy = ps.encode_frame(
            rgb,
            width=6,
            height=4,
            encoding_mode="lossy",
            lossy_opt_in=True,
            lossy_codec=codec,
        )
        self.assertEqual(codec.calls, 1)
        self.assertTrue(encoded_lossy.receipt.lossy_opt_in)
        self.assertFalse(encoded_lossy.receipt.lossless)
        self.assertEqual(encoded_lossy.receipt.input_digest, raw_digest)
        self.assertEqual(encoded_lossy.receipt.raw_frame_digest, raw_digest)
        self.assertEqual(encoded_lossy.receipt.encoding_mode, "lossy:fake_quant_rgb")
        self.assertNotEqual(encoded_lossy.receipt.raw_frame_digest, encoded_lossy.receipt.encoded_payload_digest)

        # Single-condition negative 1: lossy_opt_in=False with injected lossy codec -> refused before codec call
        codec_no_opt = DeterministicFakeLossyCodec()
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_opt:
            ps.encode_frame(
                rgb,
                width=6,
                height=4,
                encoding_mode="lossy",
                lossy_opt_in=False,
                lossy_codec=codec_no_opt,
            )
        self.assertEqual(ctx_opt.exception.code, "E_LOSSY_OPT_IN_REQUIRED")
        self.assertEqual(codec_no_opt.calls, 0)

        # Single-condition negative 2: non-bool truthy lossy_opt_in=1 -> refused
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_int:
            ps.encode_frame(
                rgb,
                width=6,
                height=4,
                encoding_mode="lossy",
                lossy_opt_in=1,  # type: ignore[arg-type]
                lossy_codec=codec_no_opt,
            )
        self.assertEqual(ctx_int.exception.code, "E_LOSSY_OPT_IN_REQUIRED")
        self.assertEqual(codec_no_opt.calls, 0)

        # Single-condition negative 3: implicit JPEG without injected codec -> E_IMPLICIT_JPEG_FORBIDDEN
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_jpeg:
            ps.encode_frame(rgb, width=6, height=4, encoding_mode="jpeg", lossy_opt_in=True)
        self.assertEqual(ctx_jpeg.exception.code, "E_IMPLICIT_JPEG_FORBIDDEN")

        # Single-condition negative 4: failing lossy codec -> E_CODEC_FAILURE, no silent fallback to PNG
        failing_codec = DeterministicFakeLossyCodec(should_fail=True)
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_fail:
            ps.encode_frame(
                rgb,
                width=6,
                height=4,
                encoding_mode="lossy",
                lossy_opt_in=True,
                lossy_codec=failing_codec,
            )
        self.assertEqual(ctx_fail.exception.code, "E_CODEC_FAILURE")
        self.assertEqual(failing_codec.calls, 1)

        # Single-condition negative 5: lossy codec silently returning PNG bytes -> E_CODEC_INVALID
        silent_png_codec = DeterministicFakeLossyCodec(return_png_header=True)
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_png_fb:
            ps.encode_frame(
                rgb,
                width=6,
                height=4,
                encoding_mode="lossy",
                lossy_opt_in=True,
                lossy_codec=silent_png_codec,
            )
        self.assertEqual(ctx_png_fb.exception.code, "E_CODEC_INVALID")

    def test_geometry_and_resize_refusals_before_provider_access(self) -> None:
        ps = _lazy_pslot_encode()
        matrix = _configured_matrix_for_tests()
        transport = DeterministicFakeTransport()
        provider = DeterministicFakeProvider()

        valid_rgb = bytes(range(24)) * 2  # 4x4x3 = 48 bytes

        # Passing real-module control
        outcome = ps.dispatch_encoded_frame(
            valid_rgb,
            width=4,
            height=4,
            role=ps.ROLE_DEFAULT_PERCEIVER,
            provider=provider,
            transport=transport,
            capability_matrix=matrix,
            remaining_quota=100,
        )
        self.assertEqual(outcome.model_name, ps.MODEL_META_MUSE_SPARK)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(provider.calls), 1)

        # Single-condition negatives: corrupt geometry, byte count mismatch, resize attempt, unsupported encoding
        bad_transport = DeterministicFakeTransport()
        bad_provider = DeterministicFakeProvider()

        # Negative A: truncated RGB buffer (47 bytes instead of 48)
        with self.assertRaises(ps.GeometryRefusalError) as ctx_len:
            ps.dispatch_encoded_frame(
                valid_rgb[:-1],
                width=4,
                height=4,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=bad_provider,
                transport=bad_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_len.exception.code, "E_RGB_BYTE_LENGTH")
        self.assertEqual(len(bad_transport.calls), 0)
        self.assertEqual(len(bad_provider.calls), 0)

        # Negative B: zero or negative dimension
        with self.assertRaises(ps.GeometryRefusalError) as ctx_zero:
            ps.dispatch_encoded_frame(
                b"",
                width=0,
                height=4,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=bad_provider,
                transport=bad_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_zero.exception.code, "E_GEOMETRY_BOUNDS")
        self.assertEqual(len(bad_transport.calls), 0)

        # Negative C: boolean width (isinstance(True, int) guard)
        with self.assertRaises(ps.GeometryRefusalError) as ctx_bool:
            ps.dispatch_encoded_frame(
                b"\x00\x00\x00",
                width=True,  # type: ignore[arg-type]
                height=1,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=bad_provider,
                transport=bad_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_bool.exception.code, "E_GEOMETRY_TYPE")
        self.assertEqual(len(bad_transport.calls), 0)

        # Negative D: resize attempt via target_width/target_height
        with self.assertRaises(ps.GeometryRefusalError) as ctx_resize:
            ps.dispatch_encoded_frame(
                valid_rgb,
                width=4,
                height=4,
                target_width=2,
                target_height=2,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=bad_provider,
                transport=bad_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_resize.exception.code, "E_RESIZE_FORBIDDEN")
        self.assertEqual(len(bad_transport.calls), 0)

        # Negative E: lossy codec attempting to resize output dimensions
        resizing_codec = DeterministicFakeLossyCodec(alter_dimensions=(2, 2))
        with self.assertRaises(ps.GeometryRefusalError) as ctx_codec_resize:
            ps.dispatch_encoded_frame(
                valid_rgb,
                width=4,
                height=4,
                encoding_mode="lossy",
                lossy_opt_in=True,
                lossy_codec=resizing_codec,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=bad_provider,
                transport=bad_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_codec_resize.exception.code, "E_RESIZE_FORBIDDEN")
        self.assertEqual(len(bad_transport.calls), 0)

        # Negative F: unsupported encoding mode rejected before provider/transport access
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_enc:
            ps.dispatch_encoded_frame(
                valid_rgb,
                width=4,
                height=4,
                encoding_mode="bmp_raw",
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=bad_provider,
                transport=bad_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_enc.exception.code, "E_ENCODING_UNSUPPORTED")
        self.assertEqual(len(bad_transport.calls), 0)
        self.assertEqual(len(bad_provider.calls), 0)

        # Negative G: corrupt XWD buffer rejected before provider/transport access
        with self.assertRaises(ps.GeometryRefusalError) as ctx_xwd:
            ps.dispatch_encoded_frame(
                b"corrupt_xwd_header",
                width=4,
                height=4,
                is_xwd=True,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=bad_provider,
                transport=bad_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_xwd.exception.code, "E_XWD_INVALID")
        self.assertEqual(len(bad_transport.calls), 0)

    def test_injected_frame_formats_and_no_frame_reference_or_registry(self) -> None:
        ps = _lazy_pslot_encode()

        arr = np.arange(3 * 2 * 3, dtype=np.uint8).reshape((2, 3, 3))
        vf_np = ps.accept_validated_frame(arr, width=3, height=2)
        self.assertEqual(vf_np.width, 3)
        self.assertEqual(vf_np.height, 2)
        self.assertEqual(vf_np.rgb_bytes, arr.tobytes())

        vf_dict = ps.accept_validated_frame({"rgb_bytes": arr.tobytes(), "width": 3, "height": 2})
        self.assertEqual(vf_dict.raw_frame_digest, vf_np.raw_frame_digest)

        vf_obj = ps.accept_validated_frame(_InjectedFrameBox(arr.tobytes(), 3, 2))
        self.assertEqual(vf_obj.raw_frame_digest, vf_np.raw_frame_digest)

        xwd_buf = _build_synthetic_xwd(3, 2, lambda x, y: (x, y, x + y))
        vf_xwd_dict = ps.accept_validated_frame({"xwd_bytes": xwd_buf, "width": 3, "height": 2})
        self.assertEqual((vf_xwd_dict.width, vf_xwd_dict.height), (3, 2))

        # Single-condition negative 1: forged digest in ValidatedFrame is rejected
        with self.assertRaises(ps.GeometryRefusalError) as ctx_forged:
            ps.ValidatedFrame(
                rgb_bytes=arr.tobytes(),
                width=3,
                height=2,
                raw_frame_digest="0" * 64,
            )
        self.assertEqual(ctx_forged.exception.code, "E_DIGEST_MISMATCH")

        # Single-condition negative 2: passing a frame reference string or registry dict is refused
        with self.assertRaises(ps.GeometryRefusalError) as ctx_ref:
            ps.accept_validated_frame("frame://slot-0/frame-42", width=3, height=2)
        self.assertEqual(ctx_ref.exception.code, "E_FRAME_REF_UNSUPPORTED")

        with self.assertRaises(ps.GeometryRefusalError) as ctx_reg:
            ps.accept_validated_frame({"registry_key": "slot_0", "width": 3, "height": 2})
        self.assertEqual(ctx_reg.exception.code, "E_FRAME_REF_UNSUPPORTED")

    def test_network_first_roles_capability_matrix_ausente_and_quota_floor(self) -> None:
        ps = _lazy_pslot_encode()
        matrix = _configured_matrix_for_tests()
        rgb = bytes((i * 11) % 256 for i in range(4 * 4 * 3))

        # Verify default capability matrix bindings and DeepSeek V4.1 Flash not configured
        self.assertEqual(ps.ROLE_MODEL_BINDINGS[ps.ROLE_DEFAULT_PERCEIVER], "Meta Muse Spark 1.3")
        self.assertEqual(ps.ROLE_MODEL_BINDINGS[ps.ROLE_ALTERNATIVE_PERCEIVER], "Gemini 3.8 Flash")
        self.assertEqual(ps.ROLE_MODEL_BINDINGS[ps.ROLE_OUT_OF_LOOP_JUDGE], "Gemini 4 Argon")
        ds_entry = ps.DEFAULT_CAPABILITY_MATRIX[ps.MODEL_DEEPSEEK_FLASH]
        self.assertFalse(ds_entry.configured)
        self.assertIsNone(ds_entry.role)
        self.assertEqual(ds_entry.supports_png, ps.AUSENTE)

        # Passing real-module controls for all 3 bound network-first roles
        for role, expected_model, quota in (
            (ps.ROLE_DEFAULT_PERCEIVER, "Meta Muse Spark 1.3", 10),
            (ps.ROLE_ALTERNATIVE_PERCEIVER, "Gemini 3.8 Flash", 15),
            (ps.ROLE_OUT_OF_LOOP_JUDGE, "Gemini 4 Argon", 50),
        ):
            transport = DeterministicFakeTransport()
            provider = DeterministicFakeProvider()
            out = ps.dispatch_encoded_frame(
                rgb,
                width=4,
                height=4,
                role=role,
                provider=provider,
                transport=transport,
                capability_matrix=matrix,
                remaining_quota=quota,
            )
            self.assertEqual(out.model_name, expected_model)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(len(provider.calls), 1)

        # Single-condition negative 1: Gemini 4 Argon out-of-loop judge quota-floor refusal before transport
        judge_transport = DeterministicFakeTransport()
        judge_provider = DeterministicFakeProvider()
        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_quota:
            ps.dispatch_encoded_frame(
                rgb,
                width=4,
                height=4,
                role=ps.ROLE_OUT_OF_LOOP_JUDGE,
                provider=judge_provider,
                transport=judge_transport,
                capability_matrix=matrix,
                remaining_quota=49,  # 1 below floor of 50
            )
        self.assertEqual(ctx_quota.exception.code, "E_QUOTA_FLOOR_REFUSAL")
        self.assertEqual(len(judge_transport.calls), 0)
        self.assertEqual(len(judge_provider.calls), 0)

        # Single-condition negative 2: DeepSeek V4.1 Flash cannot be configured or dispatched
        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_ds_cfg:
            ps.configure_capability_matrix(
                {
                    ps.MODEL_DEEPSEEK_FLASH: ps.OperatorModelConfig(
                        supports_png=True,
                        max_payload_bytes=1024,
                        quota_floor=1,
                        tariff_per_unit=0.01,
                    )
                }
            )
        self.assertEqual(ctx_ds_cfg.exception.code, "E_MODEL_NOT_CONFIGURED")

        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_ds_disp:
            ps.dispatch_encoded_frame(
                rgb,
                width=4,
                height=4,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                model_name=ps.MODEL_DEEPSEEK_FLASH,
                provider=judge_provider,
                transport=judge_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_ds_disp.exception.code, "E_MODEL_NOT_CONFIGURED")
        self.assertEqual(len(judge_transport.calls), 0)

        # Single-condition negative 3: unconfigured default matrix (AUSENTE capabilities/limits/tariffs) blocks dispatch
        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_ausente:
            ps.dispatch_encoded_frame(
                rgb,
                width=4,
                height=4,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=judge_provider,
                transport=judge_transport,
                capability_matrix=ps.DEFAULT_CAPABILITY_MATRIX,
                remaining_quota=100,
            )
        self.assertEqual(ctx_ausente.exception.code, "E_CONFIG_AUSENTE")
        self.assertEqual(len(judge_transport.calls), 0)

        # Single-condition negative 4: missing producer or AUSENTE evidence blocks dispatch
        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_prod:
            ps.dispatch_encoded_frame(
                rgb,
                width=4,
                height=4,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=None,
                transport=judge_transport,
                capability_matrix=matrix,
                remaining_quota=100,
            )
        self.assertEqual(ctx_prod.exception.code, "E_PRODUCER_ABSENT")
        self.assertEqual(len(judge_transport.calls), 0)

        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_ev:
            ps.dispatch_encoded_frame(
                rgb,
                width=4,
                height=4,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=judge_provider,
                transport=judge_transport,
                capability_matrix=matrix,
                remaining_quota=100,
                evidence=ps.AUSENTE,
            )
        self.assertEqual(ctx_ev.exception.code, "E_PRODUCER_ABSENT")
        self.assertEqual(len(judge_transport.calls), 0)

        # Single-condition negative 5: payload exceeding max_payload_bytes blocks before transport
        tiny_matrix = ps.configure_capability_matrix(
            {
                ps.MODEL_META_MUSE_SPARK: ps.OperatorModelConfig(
                    supports_png=True,
                    max_payload_bytes=10,
                    quota_floor=1,
                    tariff_per_unit=0.01,
                )
            }
        )
        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_cap_bytes:
            ps.dispatch_encoded_frame(
                rgb,
                width=4,
                height=4,
                role=ps.ROLE_DEFAULT_PERCEIVER,
                provider=judge_provider,
                transport=judge_transport,
                capability_matrix=tiny_matrix,
                remaining_quota=10,
            )
        self.assertEqual(ctx_cap_bytes.exception.code, "E_PAYLOAD_LIMIT_EXCEEDED")
        self.assertEqual(len(judge_transport.calls), 0)

    def test_local_http_design_seam_capped_unit_hook(self) -> None:
        ps = _lazy_pslot_encode()
        hook_calls: List[int] = []

        seam = ps.LocalHttpDesignSeam(
            endpoint_hint="http://127.0.0.1:0/future-seam",
            max_capped_units=8,
            on_demand_hook=lambda units: (hook_calls.append(units) or units),
            evaluation_dependency=False,
        )
        self.assertEqual(seam.invoke_capped_units(5), 5)
        self.assertEqual(hook_calls, [5])

        # Single-condition negative 1: exceeding capped units refused
        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_cap:
            seam.invoke_capped_units(9)
        self.assertEqual(ctx_cap.exception.code, "E_QUOTA_FLOOR_REFUSAL")

        # Single-condition negative 2: evaluation_dependency=True refused
        bad_seam = ps.LocalHttpDesignSeam(
            endpoint_hint="http://127.0.0.1:0/future-seam",
            max_capped_units=8,
            on_demand_hook=lambda u: u,
            evaluation_dependency=True,
        )
        with self.assertRaises(ps.DispatchGateRefusalError) as ctx_dep:
            bad_seam.invoke_capped_units(1)
        self.assertEqual(ctx_dep.exception.code, "E_LOCAL_EVAL_FORBIDDEN")

    def test_receipt_never_contains_media_payload(self) -> None:
        ps = _lazy_pslot_encode()
        rgb = bytes((i * 7 + 3) % 256 for i in range(8 * 6 * 3))
        encoded = ps.encode_frame(rgb, width=8, height=6)

        ps.assert_receipt_contains_no_media(encoded.receipt, rgb, encoded.payload)
        receipt_dict = encoded.receipt.to_dict()
        for val in receipt_dict.values():
            self.assertNotIsInstance(val, (bytes, bytearray, memoryview))

        # Single-condition negative: if a receipt field leaked hex media bytes, guard fails
        forged_receipt = ps.EncodingReceipt(
            input_digest=encoded.receipt.input_digest,
            raw_frame_digest=encoded.receipt.raw_frame_digest,
            encoded_payload_digest=encoded.receipt.encoded_payload_digest,
            encoding_mode=rgb.hex(),
            mime_type=ps.MIME_PNG,
            width=8,
            height=6,
            raw_byte_length=len(rgb),
            encoded_byte_length=len(encoded.payload),
            lossy_opt_in=False,
            lossless=True,
            codec_name=ps.CODEC_STDLIB_PNG,
            chunk_crcs=encoded.receipt.chunk_crcs,
        )
        with self.assertRaises(ps.EncodingContractRefusalError) as ctx_leak:
            ps.assert_receipt_contains_no_media(forged_receipt, rgb, encoded.payload)
        self.assertEqual(ctx_leak.exception.code, "E_MEDIA_IN_RECEIPT")

    def test_seeded_bounded_synthetic_property_cases_and_minimal_counterexample(self) -> None:
        ps = _lazy_pslot_encode()
        rng = random.Random(PROPERTY_SEED)
        cases: List[Tuple[int, int, bytes]] = []
        for _ in range(24):
            w = rng.randint(1, 32)
            h = rng.randint(1, 32)
            rgb = rng.randbytes(w * h * 3)
            cases.append((w, h, rgb))

        def roundtrip_invariant(w: int, h: int, rgb: bytes) -> bool:
            enc = ps.encode_frame(rgb, width=w, height=h)
            dw, dh, drgb, _ = _independent_stdlib_png_decode(enc.payload)
            return (
                (dw, dh) == (w, h)
                and drgb == rgb
                and enc.raw_frame_digest == hashlib.sha256(rgb).hexdigest()
                and enc.encoded_payload_digest == hashlib.sha256(enc.payload).hexdigest()
                and enc.raw_frame_digest != enc.encoded_payload_digest
            )

        # Passing real-module property control across all 24 seeded cases
        counterexample = _shrink_counterexample(cases, roundtrip_invariant)
        self.assertIsNone(
            counterexample,
            f"Seed={PROPERTY_SEED} unexpected counterexample={counterexample}",
        )

        # Single-condition negative: Sabotaged invariant (simulating bypassed last-byte preservation)
        # Verify shrinker pinpoints the minimal 1x1 (3-byte) counterexample and records seed.
        def bypassed_last_byte_guard(w: int, h: int, rgb: bytes) -> bool:
            enc = ps.encode_frame(rgb, width=w, height=h)
            _, _, drgb, _ = _independent_stdlib_png_decode(enc.payload)
            mutated = drgb[:-1] + bytes([(drgb[-1] ^ 0xFF)])
            return mutated == rgb

        min_ce = _shrink_counterexample(cases, bypassed_last_byte_guard)
        self.assertEqual(
            min_ce,
            (1, 1, 3),
            f"Seed={PROPERTY_SEED} expected minimal counterexample (1, 1, 3), got {min_ce}",
        )

    def test_concurrent_encoding_and_dispatch_isolation_with_barriers_and_leak_checks(self) -> None:
        ps = _lazy_pslot_encode()
        matrix = _configured_matrix_for_tests()
        encoder = ps.PslotEncoder(capability_matrix=matrix)
        transport = DeterministicFakeTransport()
        provider = DeterministicFakeProvider()

        num_workers = 8
        barrier = threading.Barrier(num_workers, timeout=5.0)
        start_event = threading.Event()
        done_event = threading.Event()
        errors: List[BaseException] = []
        results: List[Optional[Tuple[str, str, bool]]] = [None] * num_workers
        errors_lock = threading.Lock()

        baseline_threads = set(threading.enumerate())

        def worker(idx: int) -> None:
            try:
                w = 4 + idx
                h = 3 + (idx % 4)
                rgb = bytes(((idx + 1) * (i + 3)) % 256 for i in range(w * h * 3))
                use_lossy = (idx % 2 == 1)
                codec = DeterministicFakeLossyCodec(name=f"codec_{idx}") if use_lossy else None

                barrier.wait(timeout=5.0)
                if not start_event.wait(timeout=5.0):
                    raise TimeoutError("Timed out waiting for start_event")

                outcome = encoder.dispatch(
                    rgb,
                    width=w,
                    height=h,
                    encoding_mode="lossy" if use_lossy else ps.MODE_LOSSLESS_PNG,
                    lossy_opt_in=use_lossy,
                    lossy_codec=codec,
                    role=ps.ROLE_OUT_OF_LOOP_JUDGE if idx % 3 == 0 else ps.ROLE_DEFAULT_PERCEIVER,
                    provider=provider,
                    transport=transport,
                    remaining_quota=100,
                )
                expected_raw = hashlib.sha256(rgb).hexdigest()
                if outcome.receipt.raw_frame_digest != expected_raw:
                    raise AssertionError("Digest cross-talk between concurrent workers")
                if outcome.receipt.lossy_opt_in != use_lossy:
                    raise AssertionError("lossy_opt_in leaked across concurrent workers")
                results[idx] = (
                    outcome.receipt.raw_frame_digest,
                    outcome.receipt.encoded_payload_digest,
                    outcome.receipt.lossy_opt_in,
                )
            except BaseException as exc:  # noqa: BLE001
                with errors_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,), name=f"pslot-enc-worker-{i}", daemon=False)
            for i in range(num_workers)
        ]
        for t in threads:
            t.start()
        start_event.set()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), f"Worker thread {t.name} leaked / did not join within timeout")
        done_event.set()

        if errors:
            raise errors[0]

        for idx in range(num_workers):
            self.assertIsNotNone(results[idx])
            raw_d, enc_d, lossy_flag = results[idx]  # type: ignore[misc]
            self.assertNotEqual(raw_d, enc_d)
            self.assertEqual(lossy_flag, idx % 2 == 1)

        self.assertEqual(len(transport.calls), num_workers)
        self.assertEqual(len(provider.calls), num_workers)

        remaining_threads = set(threading.enumerate()) - baseline_threads
        self.assertEqual(len(remaining_threads), 0, f"Leaked threads detected: {remaining_threads}")


if __name__ == "__main__":
    unittest.main()
