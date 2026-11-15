"""Unit tests for vadapt.w8u_digest (owner w8u-ve-digest)."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import unittest
from unittest.mock import Mock, patch

from vadapt import w8u_digest, xfb


# Independent short literal RGB24 byte string (2x2 pixels = 12 bytes) for golden digest calculation.
LITERAL_RGB24_2X2 = (
    b"\x11\x22\x33"  # (0, 0)
    b"\x44\x55\x66"  # (1, 0)
    b"\x77\x88\x99"  # (0, 1)
    b"\xaa\xbb\xcc"  # (1, 1)
)
INDEPENDENT_RGB24_RAW_SHA256 = hashlib.sha256(LITERAL_RGB24_2X2).digest()
INDEPENDENT_RGB24_HEX_SHA256 = hashlib.sha256(LITERAL_RGB24_2X2).hexdigest()


def make_accepted_contract() -> w8u_digest.ContractEvidence:
    error_numbers = {code: idx + 1 for idx, code in enumerate(w8u_digest.CLOSED_ERRORS)}
    end_codes = {0: "OK"}
    for code, num in error_numbers.items():
        end_codes[num] = code
    return w8u_digest.ContractEvidence(
        revision="rev-w8u-os-contract-accepted-001",
        accepted=True,
        binding_schema_id="w8u.binding/v1",
        action_result_schema_id="w8u.action_result/v1",
        control_specs={
            ("W8UC", 1): {"name": "HELLO", "max_body_bytes": 16384},
            ("W8UC", 2): {"name": "ACK", "body_bytes": 16},
            ("W8UC", 3): {"name": "STOP", "body_bytes": 0},
            ("W8UH", 1): {"name": "HELLO_CONFIRM", "body_bytes": 0},
            ("W8UE", 4): {"name": "END", "body_bytes": 20},
        },
        end_error_codes=end_codes,
        error_code_numbers=error_numbers,
        digest_domains={
            "rgb24": b"w8u.rgb24.v1\x00",
            "ui": b"w8u.ui.frame_metadata.v1\x00",
            "frame_metadata": b"w8u.ui.frame_metadata.v1\x00",
            "simulation": b"w8u.simulation.state.v1\x00",
            "binding": b"w8u.binding.v1\x00",
        },
        xwd_bounds={
            "max_header_size": 4096,
            "max_ncolors": 256,
            "max_row_pad": 4096,
        },
        cross_language_vectors={
            "accepted": ["lsb_bgrx_2x2", "msb_xrgb_padded_2x2"],
            "rejected": ["oversized_header", "invalid_bitmap_bit_order", "bgrx_mislabelled_rgb24"],
        },
        receipt_closed_fields=(
            "schema",
            "identity",
            "populations",
            "first_frame",
            "last_frame",
            "retention",
            "foreign_receipts",
            "verdict",
            "evidence",
        ),
    )


def make_valid_binding(
    *,
    width: int = 2,
    height: int = 2,
    tick_mode: str = "committed",
    max_record_bytes: int = 4 * 1024 * 1024,
    max_inflight_frames: int = 2,
) -> dict:
    return {
        "schema": "w8u.binding/v1",
        "run_id": "run-001",
        "lease_id": "lease-85cff92edd3e472d",
        "seat_grant_sha256": "a" * 64,
        "container_id": "os-cu-0-container",
        "target_handle": {
            "kind": "x11_window",
            "canonical_id": "win-root-0",
            "generation": 1,
        },
        "epoch": 0,
        "display": ":99",
        "xid": 256,
        "width": width,
        "height": height,
        "frames_root": "/dev/shm/vqa-ramws",
        "frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-001",
        "image_sha256": "b" * 64,
        "reader_sha256": "c" * 64,
        "exec_allowlist_sha256": "d" * 64,
        "max_record_bytes": max_record_bytes,
        "max_inflight_frames": max_inflight_frames,
        "tick_mode": tick_mode,
    }


class TestW8UVeDigestUnit(unittest.TestCase):
    def test_contract_gate_requires_accepted_revision_bound_evidence_and_no_local_defaults(self) -> None:
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_none:
            w8u_digest.require_contract_evidence()
        self.assertEqual(cm_none.exception.code, w8u_digest.AUSENTE)

        prepared = w8u_digest.ContractEvidence(revision="rev-1", accepted=False)
        self.assertEqual(prepared.readiness_status, w8u_digest.STATUS_PREPARED)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_prep:
            w8u_digest.validate_binding(make_valid_binding(), contract=prepared)
        self.assertEqual(cm_prep.exception.code, w8u_digest.AUSENTE)

        missing_field = make_accepted_contract()
        missing_field.end_error_codes = w8u_digest.AUSENTE
        self.assertEqual(missing_field.readiness_status, w8u_digest.STATUS_PREPARED)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_missing:
            missing_field.require_accepted()
        self.assertEqual(cm_missing.exception.code, w8u_digest.AUSENTE)

        accepted = make_accepted_contract()
        self.assertEqual(accepted.readiness_status, w8u_digest.STATUS_ACCEPTED)

    def test_independent_rgb24_golden_and_synthetic_bgrx_xrgb_equivalence(self) -> None:
        contract = make_accepted_contract()
        bgrx_xwd = w8u_digest.build_bgrx_xwd(
            2,
            2,
            LITERAL_RGB24_2X2,
            pad=0,
            name=b"lsb-bgrx-header\x00",
            ncolors=0,
            filler_x=0xFE,
        )
        xrgb_xwd = w8u_digest.build_xrgb_xwd(
            2,
            2,
            LITERAL_RGB24_2X2,
            pad=8,
            name=b"msb-xrgb-distinct-header-with-longer-name\x00",
            ncolors=4,
            filler_x=0x13,
            pad_byte=0x7F,
        )
        self.assertNotEqual(bgrx_xwd, xrgb_xwd)
        self.assertNotEqual(hashlib.sha256(bgrx_xwd).digest(), hashlib.sha256(xrgb_xwd).digest())

        rgb_from_bgrx = w8u_digest.canonical_rgb24_bytes(bgrx_xwd, width=2, height=2, contract=contract)
        rgb_from_xrgb = w8u_digest.canonical_rgb24_bytes(xrgb_xwd, width=2, height=2, contract=contract)
        self.assertEqual(rgb_from_bgrx, LITERAL_RGB24_2X2)
        self.assertEqual(rgb_from_xrgb, LITERAL_RGB24_2X2)

        self.assertEqual(w8u_digest.rgb24_sha256_raw(bgrx_xwd, width=2, height=2, contract=contract), INDEPENDENT_RGB24_RAW_SHA256)
        self.assertEqual(w8u_digest.rgb24_sha256_raw(xrgb_xwd, width=2, height=2, contract=contract), INDEPENDENT_RGB24_RAW_SHA256)
        self.assertEqual(w8u_digest.rgb24_sha256_hex(bgrx_xwd, width=2, height=2, contract=contract), INDEPENDENT_RGB24_HEX_SHA256)
        self.assertEqual(w8u_digest.rgb24_sha256_hex(xrgb_xwd, width=2, height=2, contract=contract), xfb.frame_sha256(bgrx_xwd))

    def test_optional_rgba8_engine_comparison_is_separate_from_rgb24_digest(self) -> None:
        contract = make_accepted_contract()
        bgrx_xwd = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2, filler_x=0x00)
        expected_rgba8 = (
            b"\x11\x22\x33\xff"
            b"\x44\x55\x66\xff"
            b"\x77\x88\x99\xff"
            b"\xaa\xbb\xcc\xff"
        )
        rgba8_out = w8u_digest.rgba8_from_xwd(bgrx_xwd, width=2, height=2, contract=contract)
        self.assertEqual(rgba8_out, expected_rgba8)
        cmp_res = w8u_digest.compare_rgba8_engine_buffer(
            bgrx_xwd,
            expected_rgba8,
            width=2,
            height=2,
            contract=contract,
        )
        self.assertTrue(cmp_res["match"])
        self.assertEqual(cmp_res["rgb24_sha256"], INDEPENDENT_RGB24_HEX_SHA256)
        self.assertEqual(cmp_res["rgba8_engine_sha256"], hashlib.sha256(expected_rgba8).hexdigest())
        self.assertNotEqual(cmp_res["rgb24_sha256"], cmp_res["rgba8_engine_sha256"])

        bad_alpha = bytearray(expected_rgba8)
        bad_alpha[3] = 0x00
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_hash:
            w8u_digest.compare_rgba8_engine_buffer(bgrx_xwd, bad_alpha, width=2, height=2, contract=contract)
        self.assertEqual(cm_hash.exception.code, "E_HASH")

        with self.assertRaises(w8u_digest.W8UDigestError) as cm_len:
            w8u_digest.compare_rgba8_engine_buffer(bgrx_xwd, expected_rgba8[:-1], width=2, height=2, contract=contract)
        self.assertEqual(cm_len.exception.code, "E_LENGTH")

    def test_row_must_fail_bgrx_mislabelled_rgb24_and_wrong_stride(self) -> None:
        contract = make_accepted_contract()
        binding = make_valid_binding(width=2, height=2)
        _, bind_raw, bind_hex = w8u_digest.validate_binding(binding, contract=contract)

        # Positive control: independent RGB24 bytes validate cleanly.
        valid_rgb = w8u_digest.validate_raw_rgb24_buffer(
            LITERAL_RGB24_2X2,
            width=2,
            height=2,
            stride=6,
            pixel_format=w8u_digest.W8UF_PIXEL_FORMAT_RGB24,
            orientation="top_down",
            binding_sha256=bind_raw,
            expected_binding_sha256=bind_hex,
            expected_rgb24_sha256=INDEPENDENT_RGB24_RAW_SHA256,
        )
        self.assertEqual(valid_rgb, LITERAL_RGB24_2X2)

        # Must-FAIL 1: BGRX 16-byte buffer with BGRX stride=8 labelled as RGB24.
        bgrx_raw_rows = (
            b"\x33\x22\x11\x00"
            b"\x66\x55\x44\x00"
            b"\x99\x88\x77\x00"
            b"\xcc\xbb\xaa\x00"
        )
        side_effect_sink = Mock()
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_stride:
            w8u_digest.validate_raw_rgb24_buffer(
                bgrx_raw_rows,
                width=2,
                height=2,
                stride=8,
                pixel_format=w8u_digest.W8UF_PIXEL_FORMAT_RGB24,
                binding_sha256=bind_raw,
                expected_rgb24_sha256=INDEPENDENT_RGB24_RAW_SHA256,
            )
            side_effect_sink()
        self.assertEqual(cm_stride.exception.code, "E_GEOMETRY")
        side_effect_sink.assert_not_called()

        # Must-FAIL 2: BGRX bytes truncated/sliced to 12 bytes with stride=6 mislabelled as RGB24.
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_bgrx_hash:
            w8u_digest.validate_raw_rgb24_buffer(
                bgrx_raw_rows[:12],
                width=2,
                height=2,
                stride=6,
                pixel_format=w8u_digest.W8UF_PIXEL_FORMAT_RGB24,
                binding_sha256=bind_raw,
                expected_rgb24_sha256=INDEPENDENT_RGB24_RAW_SHA256,
            )
            side_effect_sink()
        self.assertEqual(cm_bgrx_hash.exception.code, "E_HASH")
        side_effect_sink.assert_not_called()

        # Must-FAIL 3: wrong orientation, wrong advertised format, missing binding.
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_orient:
            w8u_digest.validate_raw_rgb24_buffer(
                LITERAL_RGB24_2X2,
                width=2,
                height=2,
                stride=6,
                orientation="bottom_up",
                binding_sha256=bind_raw,
            )
        self.assertEqual(cm_orient.exception.code, "E_GEOMETRY")

        with self.assertRaises(w8u_digest.W8UDigestError) as cm_fmt:
            w8u_digest.validate_raw_rgb24_buffer(
                LITERAL_RGB24_2X2,
                width=2,
                height=2,
                stride=6,
                pixel_format=2,
                binding_sha256=bind_raw,
            )
        self.assertEqual(cm_fmt.exception.code, "E_WIRE")

        with self.assertRaises(w8u_digest.W8UDigestError) as cm_bind:
            w8u_digest.validate_raw_rgb24_buffer(
                LITERAL_RGB24_2X2,
                width=2,
                height=2,
                stride=6,
                binding_sha256=None,
            )
        self.assertEqual(cm_bind.exception.code, "E_BINDING")

    def test_xwd_wrapper_rejects_oversized_header_invalid_bit_order_masks_stride_and_length(self) -> None:
        contract = make_accepted_contract()

        # Cross-language vector 1: oversized header_size > 4096
        oversized_name = b"a" * 4000 + b"\x00"
        oversized_xwd = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2, name=oversized_name)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_hs:
            w8u_digest.validate_w8u_xwd(oversized_xwd, width=2, height=2, contract=contract)
        self.assertEqual(cm_hs.exception.code, "E_WIRE")

        # Cross-language vector 2: oversized ncolors > 256
        oversized_ncolors_xwd = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2, ncolors=257)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_nc:
            w8u_digest.validate_w8u_xwd(oversized_ncolors_xwd, width=2, height=2, contract=contract)
        self.assertEqual(cm_nc.exception.code, "E_WIRE")

        # Cross-language vector 3: invalid bitmap_bit_order and byte_order
        bad_bit_order_xwd = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2, bitmap_bit_order=2)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_bit:
            w8u_digest.validate_w8u_xwd(bad_bit_order_xwd, width=2, height=2, contract=contract)
        self.assertEqual(cm_bit.exception.code, "E_WIRE")

        bad_byte_order_xwd = bytearray(w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2))
        struct.pack_into(">I", bad_byte_order_xwd, 7 * 4, 99)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_bo:
            w8u_digest.validate_w8u_xwd(bad_byte_order_xwd, width=2, height=2, contract=contract)
        self.assertEqual(cm_bo.exception.code, "E_WIRE")

        # Swapped RGB masks -> E_WIRE
        bad_masks_xwd = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2, masks=(0xFF, 0xFF00, 0xFF0000))
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_masks:
            w8u_digest.validate_w8u_xwd(bad_masks_xwd, width=2, height=2, contract=contract)
        self.assertEqual(cm_masks.exception.code, "E_WIRE")

        # Unaligned stride -> E_GEOMETRY
        unaligned_stride_xwd = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2, pad=2)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_bpl:
            w8u_digest.validate_w8u_xwd(unaligned_stride_xwd, width=2, height=2, contract=contract)
        self.assertEqual(cm_bpl.exception.code, "E_GEOMETRY")

        # Trailing or missing byte -> E_LENGTH
        good_xwd = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2)
        for bad_len_buf in (good_xwd + b"\x00", good_xwd[:-1]):
            with self.assertRaises(w8u_digest.W8UDigestError) as cm_len:
                w8u_digest.validate_w8u_xwd(bad_len_buf, width=2, height=2, contract=contract)
            self.assertEqual(cm_len.exception.code, "E_LENGTH")

        # Explicit native-to-W8U error translation check
        self.assertEqual(w8u_digest.translate_native_xwd_error(xfb.XwdError("E_TORN", "torn")).code, "E_TORN")
        self.assertEqual(w8u_digest.translate_native_xwd_error(xfb.XwdError("E_XWD_GEOMETRY", "geom")).code, "E_GEOMETRY")
        self.assertEqual(w8u_digest.translate_native_xwd_error(xfb.XwdError("E_XWD_SIZE", "sz")).code, "E_LENGTH")

    def test_w8uf_record_validation_cap_before_allocation_and_double_read_comparison(self) -> None:
        contract = make_accepted_contract()
        binding = make_valid_binding(width=2, height=2, tick_mode="committed")
        _, bind_raw, bind_hex = w8u_digest.validate_binding(binding, contract=contract)

        xwd_a = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2, pad=0, name=b"xwd-a\x00")
        xwd_b = w8u_digest.build_xrgb_xwd(2, 2, LITERAL_RGB24_2X2, pad=8, name=b"xwd-b-diff-header\x00", ncolors=2)

        record = w8u_digest.build_w8uf_record(
            seq=1,
            tick=0,
            capture_mono_ns=987654321,
            width=2,
            height=2,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
            sha256_rgb24=INDEPENDENT_RGB24_RAW_SHA256,
            binding_sha256=bind_raw,
            pair_under_grab=True,
            tick_valid=True,
        )
        validated = w8u_digest.validate_w8uf_record(
            io.BytesIO(record),
            expected_seq=1,
            expected_width=2,
            expected_height=2,
            expected_binding_sha256=bind_hex,
            negotiated_max_record_bytes=binding["max_record_bytes"],
            tick_mode="committed",
            contract=contract,
        )
        self.assertEqual(validated.rgb24, LITERAL_RGB24_2X2)
        self.assertEqual(validated.sha256_rgb24_hex, INDEPENDENT_RGB24_HEX_SHA256)
        self.assertEqual(validated.frame_metadata["tick"], 0)
        self.assertIs(validated.frame_metadata["tick_valid"], True)

        # Enforce smaller negotiated cap and geometry cap BEFORE body allocation.
        allocator = Mock(side_effect=lambda n: bytearray(n))
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_cap:
            w8u_digest.validate_w8uf_record(
                io.BytesIO(record),
                expected_seq=1,
                expected_width=2,
                expected_height=2,
                expected_binding_sha256=bind_hex,
                negotiated_max_record_bytes=len(record) - 1,
                tick_mode="committed",
                contract=contract,
                body_allocator=allocator,
            )
        self.assertEqual(cm_cap.exception.code, "E_LENGTH")
        allocator.assert_not_called()

        # Torn XWD pair (XWD B differs by 1 pixel channel) -> E_TORN, no stale last-good returned.
        altered_rgb = bytearray(LITERAL_RGB24_2X2)
        altered_rgb[-1] ^= 0x01
        xwd_b_torn = w8u_digest.build_xrgb_xwd(2, 2, bytes(altered_rgb))
        torn_record = w8u_digest.build_w8uf_record(
            seq=2,
            tick=1,
            capture_mono_ns=987654322,
            width=2,
            height=2,
            xwd_a=xwd_a,
            xwd_b=xwd_b_torn,
            sha256_rgb24=INDEPENDENT_RGB24_RAW_SHA256,
            binding_sha256=bind_raw,
        )
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_torn:
            w8u_digest.validate_w8uf_record(
                torn_record,
                expected_seq=2,
                expected_width=2,
                expected_height=2,
                expected_binding_sha256=bind_hex,
                contract=contract,
            )
        self.assertEqual(cm_torn.exception.code, "E_TORN")

        # Advertised digest computed from raw BGRX instead of canonical RGB24 -> E_HASH.
        wrong_adv_record = w8u_digest.build_w8uf_record(
            seq=1,
            tick=0,
            capture_mono_ns=100,
            width=2,
            height=2,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
            sha256_rgb24=hashlib.sha256(xwd_a).digest(),
            binding_sha256=bind_raw,
        )
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_hash:
            w8u_digest.validate_w8uf_record(
                wrong_adv_record,
                expected_seq=1,
                expected_width=2,
                expected_height=2,
                expected_binding_sha256=bind_hex,
                contract=contract,
            )
        self.assertEqual(cm_hash.exception.code, "E_HASH")

    def test_tick_semantics_committed_zero_vs_unavailable_null(self) -> None:
        contract = make_accepted_contract()
        binding_unavail = make_valid_binding(width=2, height=2, tick_mode="unavailable")
        _, bind_raw, bind_hex = w8u_digest.validate_binding(binding_unavail, contract=contract)
        xwd_a = w8u_digest.build_bgrx_xwd(2, 2, LITERAL_RGB24_2X2)
        xwd_b = w8u_digest.build_xrgb_xwd(2, 2, LITERAL_RGB24_2X2)

        unavail_record = w8u_digest.build_w8uf_record(
            seq=1,
            tick=0,
            capture_mono_ns=55555,
            width=2,
            height=2,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
            sha256_rgb24=INDEPENDENT_RGB24_RAW_SHA256,
            binding_sha256=bind_raw,
            tick_valid=False,
        )
        validated = w8u_digest.validate_w8uf_record(
            unavail_record,
            expected_seq=1,
            expected_width=2,
            expected_height=2,
            expected_binding_sha256=bind_hex,
            tick_mode="unavailable",
            contract=contract,
        )
        self.assertIsNone(validated.frame_metadata["tick"])
        self.assertIs(validated.frame_metadata["tick_valid"], False)
        canon_unavail = json.loads(w8u_digest.canonicalize_frame_metadata(validated.frame_metadata, tick_mode="unavailable"))
        self.assertIsNone(canon_unavail["tick"])
        self.assertIs(canon_unavail["tick_valid"], False)

        # Unavailable tick with integer 0 in metadata dict must fail E_TICK.
        bad_meta = dict(validated.frame_metadata, tick=0)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_tick0:
            w8u_digest.validate_frame_metadata(bad_meta, tick_mode="unavailable")
        self.assertEqual(cm_tick0.exception.code, "E_TICK")

        # Wire non-zero tick when tick_valid=False must fail E_TICK.
        bad_wire_tick = w8u_digest.build_w8uf_record(
            seq=1,
            tick=7,
            capture_mono_ns=55555,
            width=2,
            height=2,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
            sha256_rgb24=INDEPENDENT_RGB24_RAW_SHA256,
            binding_sha256=bind_raw,
            tick_valid=False,
        )
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_wire_tick:
            w8u_digest.validate_w8uf_record(bad_wire_tick, expected_seq=1, tick_mode="unavailable", contract=contract)
        self.assertEqual(cm_wire_tick.exception.code, "E_TICK")

    def test_metadata_canonicalization_rejects_float_bool_tricks_unknown_keys_and_pixel_leaks(self) -> None:
        contract = make_accepted_contract()
        base_meta = {
            "seq": 1,
            "tick": 0,
            "tick_valid": True,
            "capture_mono_ns": 123456,
            "width": 2,
            "height": 2,
            "stride": 6,
            "pixel_format": 1,
            "sha256": INDEPENDENT_RGB24_HEX_SHA256,
            "binding_sha256": "e" * 64,
        }
        canon = w8u_digest.canonicalize_frame_metadata(base_meta, tick_mode="committed")
        self.assertNotIn(b"\n", canon)
        self.assertNotIn(b" ", canon)
        expected_meta_digest = hashlib.sha256(b"w8u.ui.frame_metadata.v1\x00" + canon).hexdigest()
        self.assertEqual(w8u_digest.hash_frame_metadata_hex(base_meta, tick_mode="committed", contract=contract), expected_meta_digest)

        # Reject bool-as-int and int-as-bool tricks
        for field, trick_val, expected_code in (
            ("seq", True, "E_SEQ"),
            ("tick", False, "E_TICK"),
            ("tick_valid", 1, "E_TICK"),
            ("width", True, "E_GEOMETRY"),
            ("capture_mono_ns", 100.0, "E_WIRE"),
        ):
            mut = dict(base_meta, **{field: trick_val})
            with self.subTest(field=field, trick_val=trick_val):
                with self.assertRaises(w8u_digest.W8UDigestError) as cm:
                    w8u_digest.canonicalize_frame_metadata(mut)
                self.assertEqual(cm.exception.code, expected_code)

        # Reject unknown keys, reconstructible pixels, base64, data URLs, deltas
        for bad_extra in (
            {"unknown_field": 1},
            {"pixels": [1, 2, 3]},
            {"delta": "00ff"},
            {"preview": "data:image/png;base64,iVBORw0KGgo="},
            {"raw": b"\x00\x01\x02"},
        ):
            mut = dict(base_meta, **bad_extra)
            with self.assertRaises(w8u_digest.W8UDigestError) as cm_extra:
                w8u_digest.canonicalize_frame_metadata(mut)
            self.assertEqual(cm_extra.exception.code, "E_WIRE")

    def test_binding_validation_root_confinement_grant_auth_and_quota(self) -> None:
        contract = make_accepted_contract()
        binding = make_valid_binding()

        # Grant authentication is checked independently of digest
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_grant:
            w8u_digest.validate_binding(binding, contract=contract, grant_authenticator=lambda _b: False)
        self.assertEqual(cm_grant.exception.code, "E_BINDING")

        # Root and child confinement checks
        for bad_roots in (
            {"frames_root": "/tmp/vqa-ramws"},
            {"frames_root": "/dev/shm/vqa-ramws/"},
            {"frame_run_root": "/dev/shm/vqa-ramws/other/run-001"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/../escape"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-001/"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run/run-001"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-001/nested"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-1/run-001"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/another-run"},
            {"frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-001\x00"},
        ):
            mut = dict(binding, **bad_roots)
            with self.assertRaises(w8u_digest.W8UDigestError) as cm_root:
                w8u_digest.validate_binding(mut, contract=contract)
            self.assertEqual(cm_root.exception.code, "E_ROOT")

        # RAM quota and reserved capacity checks
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_quota:
            w8u_digest.validate_binding(binding, contract=contract, reserved_ram_bytes=1024)
        self.assertEqual(cm_quota.exception.code, "E_RAM_QUOTA")

        with self.assertRaises(w8u_digest.W8UDigestError) as cm_cap:
            w8u_digest.validate_binding(binding, contract=contract, reserved_capacity_frames=1)
        self.assertEqual(cm_cap.exception.code, "E_CREDIT")

    def test_binding_run_id_is_one_component_even_when_root_matches(self) -> None:
        contract = make_accepted_contract()
        for run_id, expected_code in (
            ("nested/run-001", "E_ROOT"),
            (".", "E_ROOT"),
            ("..", "E_ROOT"),
            ("", "E_BINDING"),
            ("run-001\x00", "E_BINDING"),
        ):
            with self.subTest(run_id=run_id):
                binding = make_valid_binding()
                binding["run_id"] = run_id
                binding["frame_run_root"] = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/" + run_id
                with self.assertRaises(w8u_digest.W8UDigestError) as cm:
                    w8u_digest.validate_binding(binding, contract=contract)
                self.assertEqual(cm.exception.code, expected_code)

    def test_binding_rejects_symlinked_root_campaign_and_run_before_grant(self) -> None:
        contract = make_accepted_contract()
        binding = make_valid_binding()
        for link in (
            "/dev/shm/vqa-ramws",
            "/dev/shm/vqa-ramws/opensuperficies",
            "/dev/shm/vqa-ramws/opensuperficies/os-cu-0",
            binding["frame_run_root"],
        ):
            with self.subTest(link=link):
                authenticator = Mock(return_value=True)
                with patch("vadapt.w8u_digest.os.path.islink", side_effect=lambda p: p == link):
                    with self.assertRaises(w8u_digest.W8UDigestError) as cm:
                        w8u_digest.validate_binding(binding, contract=contract, grant_authenticator=authenticator)
                self.assertEqual(cm.exception.code, "E_ROOT")
                authenticator.assert_not_called()

    def test_u5_receipt_fields_and_hash_domain_separation(self) -> None:
        contract = make_accepted_contract()
        binding = make_valid_binding(width=2, height=2, tick_mode="committed")
        _, _, bind_hex = w8u_digest.validate_binding(binding, contract=contract)

        # Hash-domain separation: identical byte payload hashes to distinct digests in rgb24, ui/frame_metadata, simulation, and binding domains.
        same_payload = LITERAL_RGB24_2X2
        d_rgb = w8u_digest.domain_sha256_hex("rgb24", same_payload, contract=contract)
        d_ui = w8u_digest.domain_sha256_hex("ui", same_payload, contract=contract)
        d_sim = w8u_digest.domain_sha256_hex("simulation", same_payload, contract=contract)
        d_bind = w8u_digest.domain_sha256_hex("binding", same_payload, contract=contract)
        self.assertEqual(len({d_rgb, d_ui, d_sim, d_bind}), 4)

        frame_meta = {
            "seq": 1,
            "tick": 0,
            "tick_valid": True,
            "capture_mono_ns": 1000,
            "width": 2,
            "height": 2,
            "stride": 6,
            "pixel_format": 1,
            "sha256": INDEPENDENT_RGB24_HEX_SHA256,
            "binding_sha256": bind_hex,
        }
        receipt = w8u_digest.project_u5_vision_receipt(
            contract=contract,
            binding=binding,
            binding_sha256=bind_hex,
            frames=[frame_meta],
            retention_mode="metadata_only",
            pixels_purged=True,
            claim_pixel_replay=False,
            foreign_action_receipt=None,
            foreign_t2_receipt=None,
            foreign_state_receipt=None,
            verdict="ACCEPT",
            error_code=None,
        )
        self.assertEqual(receipt["schema"], "w8u.receipt/v1")
        self.assertIsNone(receipt["foreign_receipts"]["action"])
        self.assertIsNone(receipt["foreign_receipts"]["t2"])
        self.assertIsNone(receipt["foreign_receipts"]["state"])
        self.assertIsNone(receipt["populations"]["admitted_actions"])
        self.assertIsNone(receipt["populations"]["applied_actions"])
        self.assertIs(receipt["retention"]["pixel_replay_available"], False)
        self.assertIs(receipt["retention"]["capture_grants_input_authority"], False)

        # Applied actions differ from admissions when foreign action receipt is injected.
        receipt_with_actions = w8u_digest.project_u5_vision_receipt(
            contract=contract,
            binding=binding,
            binding_sha256=bind_hex,
            frames=[frame_meta],
            foreign_action_receipt={"admitted_actions": 5, "applied_actions": 3},
        )
        self.assertEqual(receipt_with_actions["populations"]["admitted_actions"], 5)
        self.assertEqual(receipt_with_actions["populations"]["applied_actions"], 3)

        # metadata_only retention cannot claim pixel replay after purge.
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_replay:
            w8u_digest.project_u5_vision_receipt(
                contract=contract,
                binding=binding,
                binding_sha256=bind_hex,
                frames=[frame_meta],
                retention_mode="metadata_only",
                pixels_purged=True,
                claim_pixel_replay=True,
            )
        self.assertEqual(cm_replay.exception.code, "E_WIRE")

    def test_controls_and_credit_window_single_capacity_exhaustion_duplicate_ack_inflation_and_stop(self) -> None:
        contract = make_accepted_contract()
        binding = make_valid_binding(max_inflight_frames=2)
        canon_bind, bind_raw, _ = w8u_digest.validate_binding(binding, contract=contract)

        # W8UC HELLO kind 1
        hello_pkt = w8u_digest.CONTROL_HEADER_STRUCT.pack(b"W8UC", 1, 1, len(canon_bind), bind_raw) + canon_bind
        hello_cf = w8u_digest.parse_control_frame(hello_pkt, contract=contract, expected_binding_sha256=bind_raw)
        self.assertEqual(hello_cf.parsed["type"], "HELLO")
        self.assertEqual(hello_cf.parsed["credit_granted"], 0)

        # W8UH confirmation kind 1
        confirm_pkt = w8u_digest.CONTROL_HEADER_STRUCT.pack(b"W8UH", 1, 1, 0, bind_raw)
        confirm_cf = w8u_digest.parse_control_frame(confirm_pkt, contract=contract, expected_binding_sha256=bind_raw)
        self.assertEqual(confirm_cf.parsed["type"], "HELLO_CONFIRM")

        # W8UE END kind 4 normal completion (0) and unknown error_code refusal
        end_ok_body = struct.pack(">QQI", 2, 2, 0)
        end_ok_pkt = w8u_digest.CONTROL_HEADER_STRUCT.pack(b"W8UE", 1, 4, len(end_ok_body), bind_raw) + end_ok_body
        end_cf = w8u_digest.parse_control_frame(end_ok_pkt, contract=contract, expected_binding_sha256=bind_raw)
        self.assertEqual(end_cf.parsed["error_symbol"], "OK")

        end_unknown_body = struct.pack(">QQI", 2, 2, 9999)
        end_unknown_pkt = w8u_digest.CONTROL_HEADER_STRUCT.pack(b"W8UE", 1, 4, len(end_unknown_body), bind_raw) + end_unknown_body
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_end_unk:
            w8u_digest.parse_control_frame(end_unknown_pkt, contract=contract, expected_binding_sha256=bind_raw)
        self.assertEqual(cm_end_unk.exception.code, "E_WIRE")

        # Single-frame capacity (max_inflight_frames=1): initial ACK must grant min(2, 1) == 1; granting 2 is E_CREDIT.
        ctrl_single = w8u_digest.CreditWindowController(max_inflight_frames=1, reserved_capacity_frames=1)
        self.assertEqual(ctrl_single.on_hello(), 0)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_overgrant:
            ctrl_single.on_ack(0, 2)
        self.assertEqual(cm_overgrant.exception.code, "E_CREDIT")
        ctrl_single.on_ack(0, 1)
        self.assertEqual(ctrl_single.available_credit, 1)

        # Identical duplicate ACK is idempotent.
        dup_res = ctrl_single.on_ack(0, 1)
        self.assertTrue(dup_res["duplicate"])
        self.assertEqual(ctrl_single.available_credit, 1)

        # Acquire credit before grab -> complete checked ungrab before blocking output -> exhaustion -> publication & lease release -> ACK.
        seq1 = ctrl_single.acquire_credit_before_grab()
        self.assertEqual(seq1, 1)
        self.assertEqual(ctrl_single.available_credit, 0)

        # Sending before checked ungrab is forbidden (E_WIRE).
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_ungrab:
            ctrl_single.send_frame_after_ungrab(seq1)
        self.assertEqual(cm_ungrab.exception.code, "E_WIRE")

        ctrl_single.complete_checked_ungrab(seq1, ungrab_ok=True)
        ctrl_single.send_frame_after_ungrab(seq1)

        # Credit exhaustion blocks next grab with E_BACKPRESSURE.
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_exhaust:
            ctrl_single.acquire_credit_before_grab()
        self.assertEqual(cm_exhaust.exception.code, "E_BACKPRESSURE")

        # ACK before validated publication and recording-consumer lease release is forbidden (E_CREDIT).
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_early_ack:
            ctrl_single.on_ack(1, 2)
        self.assertEqual(cm_early_ack.exception.code, "E_CREDIT")

        ctrl_single.record_validated_publication(1)
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_no_lease:
            ctrl_single.on_ack(1, 2)
        self.assertEqual(cm_no_lease.exception.code, "E_CREDIT")

        ctrl_single.release_recording_consumer_lease(1)

        # Capacity inflation (granting ack_seq + 2 when max_inflight_frames == 1) is E_CREDIT.
        with self.assertRaises(w8u_digest.W8UDigestError) as cm_inflate:
            ctrl_single.on_ack(1, 3)
        self.assertEqual(cm_inflate.exception.code, "E_CREDIT")

        ctrl_single.on_ack(1, 2)
        self.assertEqual(ctrl_single.available_credit, 1)

        # Acquire again to exhaust credit, then verify STOP bypasses full queue immediately.
        seq2 = ctrl_single.acquire_credit_before_grab()
        ctrl_single.complete_checked_ungrab(seq2, ungrab_ok=True)
        ctrl_single.send_frame_after_ungrab(seq2)
        self.assertEqual(ctrl_single.available_credit, 0)
        self.assertTrue(ctrl_single.trigger_stop())


MANDATORY_CASES = (
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_contract_gate_requires_accepted_revision_bound_evidence_and_no_local_defaults",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_independent_rgb24_golden_and_synthetic_bgrx_xrgb_equivalence",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_optional_rgba8_engine_comparison_is_separate_from_rgb24_digest",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_row_must_fail_bgrx_mislabelled_rgb24_and_wrong_stride",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_xwd_wrapper_rejects_oversized_header_invalid_bit_order_masks_stride_and_length",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_w8uf_record_validation_cap_before_allocation_and_double_read_comparison",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_tick_semantics_committed_zero_vs_unavailable_null",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_metadata_canonicalization_rejects_float_bool_tricks_unknown_keys_and_pixel_leaks",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_binding_validation_root_confinement_grant_auth_and_quota",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_u5_receipt_fields_and_hash_domain_separation",
    "tests.test_w8u_ve_digest_unit.TestW8UVeDigestUnit.test_controls_and_credit_window_single_capacity_exhaustion_duplicate_ack_inflation_and_stop",
)


if __name__ == "__main__":
    unittest.main()
