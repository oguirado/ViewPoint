"""tests.test_jq2b_j2_frame_digest_implementation - Acceptance tests for Gate G J2 1.

Exercises the real public API of vadapt.frame_digest:
- C-PIXEL canonical frame digest and strict declaration validation
- Independent known-byte goldens with individual mutations (channels, row order, alpha, stride, geometry)
- Separation of raw_sha256 and normalized_sha256
- INV-J2-1: declaration validated before reading buffer; no inferred RGB default
- INV-J2-2: handle generation, epoch, geometry, and publication identity binding
- INV-J2-3: owned memory and sequence/CRC revalidation after copying
- INV-J2-4: blocks unsupported capture and unobserved residency
- Simulation tick vs render_index separation
- Same-tick distinct commit controls
- Trusted window/crop association for Chromium/CDP
- Legacy receipt schema: exactly frame_sha256, dimensions, tick, telemetry
- Write spies rejecting pixel persistence to disk/swap
- Source-neutral boundary and Holdings records
"""
import hashlib
import unittest
import numpy as np

from vadapt.frame_digest import (
    CommitTracker,
    CommittedFrame,
    DeclarationError,
    FrameDigest,
    FrameDigestError,
    InjectedObservation,
    InvariantViolationError,
    PixelDeclaration,
    PixelPersistenceForbiddenError,
    RevalidationError,
    SourceNeutralBoundary,
    T2Submission,
    UnsupportedLayoutError,
    WriteSpy,
    compute_pixel_decl_sha256,
    create_legacy_receipt,
    digest_frame,
    make_holdings_record,
    validate_declaration,
)


class TestGateGJ21_KnownByteGoldens(unittest.TestCase):
    """Test Gate G J2 1: Independent known-byte goldens across representations."""

    def setUp(self):
        # 2x2 known test pattern:
        # (0, 0): Red   (255, 0, 0)
        # (1, 0): Green (0, 255, 0)
        # (0, 1): Blue  (0, 0, 255)
        # (1, 1): White (255, 255, 255)
        self.w, self.h = 2, 2
        self.canonical_rgb24 = (
            b"\xff\x00\x00"  # (0,0) Red
            b"\x00\xff\x00"  # (1,0) Green
            b"\x00\x00\xff"  # (0,1) Blue
            b"\xff\xff\xff"  # (1,1) White
        )
        self.expected_norm_sha = hashlib.sha256(self.canonical_rgb24).hexdigest()

    def test_canonical_rgb_top_down_healthy_control(self):
        # Top-down RGB with stride = 6 (no padding)
        decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-001",
        )
        buf = bytes(self.canonical_rgb24)
        digest = digest_frame(buf, decl)

        # In this specific case, raw bytes match canonical bytes
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)
        self.assertEqual(digest.raw_sha256, self.expected_norm_sha)
        self.assertEqual(digest.normalized_bytes, self.canonical_rgb24)
        # Verify normalized_bytes is a distinct owned copy, not the exact input object
        self.assertIsNot(digest.normalized_bytes, buf)

    def test_bgr_order_normalization(self):
        # BGR: Red=(0,0,255), Green=(0,255,0), Blue=(255,0,0), White=(255,255,255)
        bgr_buf = (
            b"\x00\x00\xff"
            b"\x00\xff\x00"
            b"\xff\x00\x00"
            b"\xff\xff\xff"
        )
        decl = PixelDeclaration(
            channel_order="BGR",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-002",
        )
        digest = digest_frame(bgr_buf, decl)

        # Raw sha differs because bytes are in BGR order, but normalized matches golden
        self.assertNotEqual(digest.raw_sha256, digest.normalized_sha256)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)
        self.assertEqual(digest.normalized_bytes, self.canonical_rgb24)

    def test_rgba_order_normalization(self):
        # RGBA with alpha=opaque: stride=8
        rgba_buf = (
            b"\xff\x00\x00\xff"
            b"\x00\xff\x00\xff"
            b"\x00\x00\xff\xff"
            b"\xff\xff\xff\xff"
        )
        decl = PixelDeclaration(
            channel_order="RGBA",
            stride_bytes=8,
            orientation="top_down",
            alpha="opaque",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-003",
        )
        digest = digest_frame(rgba_buf, decl)

        self.assertNotEqual(digest.raw_sha256, digest.normalized_sha256)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)
        self.assertEqual(digest.normalized_bytes, self.canonical_rgb24)

    def test_bgra_order_normalization(self):
        # BGRA with alpha=unpremultiplied
        bgra_buf = (
            b"\x00\x00\xff\xff"
            b"\x00\xff\x00\xff"
            b"\xff\x00\x00\xff"
            b"\xff\xff\xff\xff"
        )
        decl = PixelDeclaration(
            channel_order="BGRA",
            stride_bytes=8,
            orientation="top_down",
            alpha="unpremultiplied",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-004",
        )
        digest = digest_frame(bgra_buf, decl)

        self.assertNotEqual(digest.raw_sha256, digest.normalized_sha256)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)

    def test_argb_order_normalization(self):
        # ARGB with alpha=opaque: stride=8
        argb_buf = (
            b"\xff\xff\x00\x00"  # A, R, G, B
            b"\xff\x00\xff\x00"
            b"\xff\x00\x00\xff"
            b"\xff\xff\xff\xff"
        )
        decl = PixelDeclaration(
            channel_order="ARGB",
            stride_bytes=8,
            orientation="top_down",
            alpha="opaque",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-004a",
        )
        digest = digest_frame(argb_buf, decl)
        self.assertNotEqual(digest.raw_sha256, digest.normalized_sha256)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)
        self.assertEqual(digest.normalized_bytes, self.canonical_rgb24)

    def test_abgr_order_normalization(self):
        # ABGR with alpha=opaque: stride=8
        abgr_buf = (
            b"\xff\x00\x00\xff"  # A, B, G, R
            b"\xff\x00\xff\x00"
            b"\xff\xff\x00\x00"
            b"\xff\xff\xff\xff"
        )
        decl = PixelDeclaration(
            channel_order="ABGR",
            stride_bytes=8,
            orientation="top_down",
            alpha="opaque",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-004b",
        )
        digest = digest_frame(abgr_buf, decl)
        self.assertNotEqual(digest.raw_sha256, digest.normalized_sha256)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)
        self.assertEqual(digest.normalized_bytes, self.canonical_rgb24)

    def test_bottom_up_orientation_normalization(self):
        # bottom_up: row 0 in memory is row 1 of the image (Blue, White)
        # row 1 in memory is row 0 of the image (Red, Green)
        bottom_up_buf = (
            b"\x00\x00\xff\xff\xff\xff"  # row 1 (bottom)
            b"\xff\x00\x00\x00\xff\x00"  # row 0 (top)
        )
        decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="bottom_up",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-005",
        )
        digest = digest_frame(bottom_up_buf, decl)

        self.assertNotEqual(digest.raw_sha256, digest.normalized_sha256)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)
        self.assertEqual(digest.normalized_bytes, self.canonical_rgb24)

    def test_padded_stride_normalization(self):
        # RGB with stride=10 (4 bytes padding per row)
        padded_buf = (
            b"\xff\x00\x00\x00\xff\x00\xaa\xbb\xcc\xdd"
            b"\x00\x00\xff\xff\xff\xff\x11\x22\x33\x44"
        )
        decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=10,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-006",
        )
        digest = digest_frame(padded_buf, decl)

        self.assertNotEqual(digest.raw_sha256, digest.normalized_sha256)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)
        self.assertEqual(digest.normalized_bytes, self.canonical_rgb24)

    def test_premultiplied_alpha_normalization(self):
        # Pre-multiplied alpha: half transparent (alpha = 128 / 0.5)
        # Red = round(255 * 0.5) = 128
        # Green = round(255 * 0.5) = 128
        # Blue = round(255 * 0.5) = 128
        # White = round(255 * 0.5) = 128
        premul_buf = (
            b"\x80\x00\x00\x80"
            b"\x00\x80\x00\x80"
            b"\x00\x00\x80\x80"
            b"\x80\x80\x80\x80"
        )
        decl = PixelDeclaration(
            channel_order="RGBA",
            stride_bytes=8,
            orientation="top_down",
            alpha="premultiplied",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-test",
            binding_id="bind-007",
        )
        digest = digest_frame(premul_buf, decl)
        self.assertEqual(digest.normalized_sha256, self.expected_norm_sha)


class TestGateGJ21_IndividualMutations(unittest.TestCase):
    """Mutate individual layout parameters and prove contrasts."""

    def setUp(self):
        self.base_decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-main",
            binding_id="bind-main",
        )
        self.buf = b"\xff\x00\x00\x00\xff\x00\x00\x00\xff\xff\xff\xff"

    def test_contrast_channel_mutation_changes_hash(self):
        ctrl = digest_frame(self.buf, self.base_decl)
        bgr_decl = PixelDeclaration(
            channel_order="BGR",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-main",
            binding_id="bind-main",
        )
        mutant = digest_frame(self.buf, bgr_decl)
        self.assertEqual(ctrl.raw_sha256, mutant.raw_sha256)
        self.assertNotEqual(ctrl.normalized_sha256, mutant.normalized_sha256)

    def test_contrast_orientation_mutation_changes_hash(self):
        ctrl = digest_frame(self.buf, self.base_decl)
        bottom_up_decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="bottom_up",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-main",
            binding_id="bind-main",
        )
        mutant = digest_frame(self.buf, bottom_up_decl)
        self.assertEqual(ctrl.raw_sha256, mutant.raw_sha256)
        self.assertNotEqual(ctrl.normalized_sha256, mutant.normalized_sha256)

    def test_contrast_padding_mutation_alters_raw_retains_normalized(self):
        ctrl = digest_frame(self.buf, self.base_decl)
        padded_buf = b"\xff\x00\x00\x00\xff\x00\x00\x00\x00\x00\xff\xff\xff\xff\x00\x00"
        padded_decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=8,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-main",
            binding_id="bind-main",
        )
        mutant = digest_frame(padded_buf, padded_decl)
        self.assertNotEqual(ctrl.raw_sha256, mutant.raw_sha256)
        self.assertEqual(ctrl.normalized_sha256, mutant.normalized_sha256)

    def test_short_buffer_raises_buffer_short_error(self):
        short_buf = b"\x00" * 8  # Need 12 bytes
        with self.assertRaises(FrameDigestError) as ctx:
            digest_frame(short_buf, self.base_decl)
        self.assertEqual(ctx.exception.code, "E_BUFFER_SHORT")


class TestGateGJ21_MissingLayoutAndValidation(unittest.TestCase):
    """Missing layout is strictly typed unsupported; never an inferred RGB default."""

    def test_missing_declaration_is_unsupported(self):
        with self.assertRaises(UnsupportedLayoutError) as ctx:
            digest_frame(b"\x00" * 12, None)
        self.assertEqual(ctx.exception.code, "E_INV_J2_1_NO_DECL")

    def test_partial_dict_declaration_fails(self):
        incomplete = {"width": 2, "height": 2, "channel_order": "RGB"}
        with self.assertRaises(UnsupportedLayoutError) as ctx:
            validate_declaration(incomplete)
        self.assertEqual(ctx.exception.code, "E_LAYOUT_MISSING")

    def test_invalid_stride_less_than_width_fails(self):
        d = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=4,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        with self.assertRaises(UnsupportedLayoutError) as ctx:
            validate_declaration(d)
        self.assertEqual(ctx.exception.code, "E_LAYOUT_STRIDE")

    def test_unsupported_color_space_fails(self):
        d = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="display_p3",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        with self.assertRaises(UnsupportedLayoutError) as ctx:
            validate_declaration(d)
        self.assertEqual(ctx.exception.code, "E_LAYOUT_COLOR_SPACE")

    def test_alpha_convention_mismatch_fails(self):
        # 3-channel RGB with alpha != 'none'
        d1 = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="opaque",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        with self.assertRaises(UnsupportedLayoutError) as ctx:
            validate_declaration(d1)
        self.assertEqual(ctx.exception.code, "E_LAYOUT_ALPHA")

        # 4-channel RGBA with alpha == 'none'
        d2 = PixelDeclaration(
            channel_order="RGBA",
            stride_bytes=8,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        with self.assertRaises(UnsupportedLayoutError) as ctx:
            validate_declaration(d2)
        self.assertEqual(ctx.exception.code, "E_LAYOUT_ALPHA")

    def test_pixel_decl_sha256_binding(self):
        d = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        h1 = compute_pixel_decl_sha256(d)
        self.assertEqual(len(h1), 64)
        self.assertEqual(h1, d.pixel_decl_sha256)

        # Mutating a field changes pixel_decl_sha256
        d2 = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=10,  # mutated
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        self.assertNotEqual(h1, d2.pixel_decl_sha256)


class TestGateGJ21_Invariants(unittest.TestCase):
    """Test INV-J2-1 through INV-J2-4 committed-frame invariants."""

    def setUp(self):
        self.decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-inv",
            binding_id="bind-inv",
        )
        self.buf = b"\x00" * 12
        self.digest = digest_frame(self.buf, self.decl)

    def test_inv_j2_1_declaration_before_read(self):
        # Buffer that would raise on read if attempted
        class PoisonBuffer:
            def __getitem__(self, item):
                raise RuntimeError("poisoned read attempted")

            @property
            def nbytes(self):
                raise RuntimeError("poisoned length checked")

        # When declaration is invalid, buffer is NEVER touched
        with self.assertRaises(UnsupportedLayoutError):
            digest_frame(PoisonBuffer(), None)

    def test_inv_j2_2_handle_epoch_publication_identity_required(self):
        tracker = CommitTracker()

        # Healthy control
        committed = tracker.commit(
            self.digest,
            frame_seq=1,
            committed_tick=100,
            state_sha256="state-hash-100",
            observer_ref="obs-1",
            handle_generation=0,
            epoch=1,
            publication_identity="pub-100",
        )
        self.assertEqual(committed.publication_identity, "pub-100")

        # Negative contrasts
        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=2,
                committed_tick=101,
                state_sha256="state-hash-101",
                observer_ref="obs-1",
                handle_generation=-1,  # Invalid handle generation
                epoch=1,
                publication_identity="pub-101",
            )
        self.assertEqual(ctx.exception.code, "E_INV_J2_2_HANDLE")

        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=2,
                committed_tick=101,
                state_sha256="state-hash-101",
                observer_ref="obs-1",
                handle_generation=0,
                epoch=-1,  # Invalid epoch
                publication_identity="pub-101",
            )
        self.assertEqual(ctx.exception.code, "E_INV_J2_2_EPOCH")

        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=2,
                committed_tick=101,
                state_sha256="state-hash-101",
                observer_ref="obs-1",
                handle_generation=0,
                epoch=1,
                publication_identity="",  # Missing pub id
            )
        self.assertEqual(ctx.exception.code, "E_INV_J2_2_PUB_ID")

    def test_inv_j2_3_revalidation_after_copying(self):
        # Healthy control: revalidation succeeds
        d = digest_frame(self.buf, self.decl, revalidator=lambda: True)
        self.assertIsNotNone(d)

        # Negative contrast: torn frame detected post-copy
        with self.assertRaises(RevalidationError) as ctx:
            digest_frame(self.buf, self.decl, revalidator=lambda: False)
        self.assertEqual(ctx.exception.code, "E_INV_J2_3_TORN")

    def test_inv_j2_4_unsupported_capture_and_unobserved_residency_blocked(self):
        tracker = CommitTracker()

        # Negative contrast: unsupported capture
        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=1,
                committed_tick=10,
                state_sha256="st",
                observer_ref="obs",
                handle_generation=0,
                epoch=0,
                publication_identity="pub-1",
                capture_supported=False,
            )
        self.assertEqual(ctx.exception.code, "E_INV_J2_4_CAPTURE")

        # Negative contrast: unobserved residency
        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=1,
                committed_tick=10,
                state_sha256="st",
                observer_ref="obs",
                handle_generation=0,
                epoch=0,
                publication_identity="pub-1",
                residency_observed=False,
            )
        self.assertEqual(ctx.exception.code, "E_INV_J2_4_RESIDENCY")


class TestGateGJ21_ExecutionSemantics(unittest.TestCase):
    """Test render_index vs tick separation, same-tick commits, and Chromium identity."""

    def setUp(self):
        self.decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-sem",
            binding_id="bind-sem",
        )
        self.digest = digest_frame(b"\x00" * 12, self.decl)

    def test_render_index_cannot_be_simulation_tick(self):
        tracker = CommitTracker()
        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=1,
                committed_tick=42,
                render_index=42,  # Confused with tick
                state_sha256="st",
                observer_ref="obs",
                handle_generation=0,
                epoch=0,
                publication_identity="pub-42",
            )
        self.assertEqual(ctx.exception.code, "E_RENDER_INDEX_IS_NOT_TICK")

    def test_same_tick_duplicate_publication_refused(self):
        tracker = CommitTracker()
        tracker.commit(
            self.digest,
            frame_seq=1,
            committed_tick=50,
            state_sha256="st-1",
            observer_ref="obs",
            handle_generation=0,
            epoch=0,
            publication_identity="pub-50",
        )
        # Attempt to reuse same publication identity at same tick
        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=2,
                committed_tick=50,
                state_sha256="st-2",
                observer_ref="obs",
                handle_generation=0,
                epoch=0,
                publication_identity="pub-50",
            )
        self.assertEqual(ctx.exception.code, "E_SAME_TICK_DUPLICATE_PUB")

    def test_chromium_screen_requires_trusted_window_and_crop(self):
        tracker = CommitTracker()
        # CDP without window_id and crop_bounds is rejected
        with self.assertRaises(InvariantViolationError) as ctx:
            tracker.commit(
                self.digest,
                frame_seq=1,
                committed_tick=60,
                state_sha256="st",
                observer_ref="obs",
                handle_generation=0,
                epoch=0,
                publication_identity="pub-60",
                source_kind="cdp",
                window_id=None,
                crop_bounds=None,
            )
        self.assertEqual(ctx.exception.code, "E_UNTRUSTED_TAB_IDENTITY")

        # CDP with trusted window_id and crop_bounds succeeds
        c = tracker.commit(
            self.digest,
            frame_seq=1,
            committed_tick=60,
            state_sha256="st",
            observer_ref="obs",
            handle_generation=0,
            epoch=0,
            publication_identity="pub-60",
            source_kind="cdp",
            window_id="win-100",
            crop_bounds=(0, 0, 2, 2),
        )
        self.assertEqual(c.window_id, "win-100")


class TestGateGJ21_LegacyReceiptAndWriteSpy(unittest.TestCase):
    """Test legacy receipt compatibility and write spy rejection."""

    def test_legacy_receipt_exact_schema(self):
        decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        digest = digest_frame(b"\x11" * 12, decl)
        receipt = create_legacy_receipt(digest, tick=123, telemetry={"fps": 60})

        expected_keys = {"dimensions", "frame_sha256", "telemetry", "tick"}
        self.assertEqual(set(receipt.keys()), expected_keys)
        self.assertEqual(receipt["dimensions"], [2, 2])
        self.assertEqual(receipt["frame_sha256"], digest.normalized_sha256)
        self.assertEqual(receipt["tick"], 123)
        self.assertEqual(receipt["telemetry"], {"fps": 60})

    def test_write_spy_rejects_pixel_persistence(self):
        spy = WriteSpy()
        with self.assertRaises(PixelPersistenceForbiddenError) as ctx:
            spy.record_attempt("/tmp/saved_frame.png")
        self.assertEqual(ctx.exception.code, "E_PERSISTENCE_FORBIDDEN")
        self.assertTrue(spy.attempted_write)


class TestGateGJ21_SourceNeutralBoundaryAndHoldings(unittest.TestCase):
    """Test neutral boundary and strict Holdings commands."""

    def test_holdings_pause_command_must_have_empty_args(self):
        # Healthy control
        sub = make_holdings_record("pause", tick=1, seq=1)
        self.assertEqual(sub.cmd, "pause")
        self.assertEqual(sub.args, {})

        # Negative contrast
        with self.assertRaises(DeclarationError) as ctx:
            make_holdings_record("pause", tick=1, seq=1, extra_arg="forbidden")
        self.assertEqual(ctx.exception.code, "E_HOLDINGS_PAUSE_ARGS")

    def test_holdings_new_game_requires_exact_fields(self):
        # Healthy control
        sub = make_holdings_record(
            "new_game",
            tick=1,
            seq=1,
            country_id="FRA",
            difficulty="normal",
            opening_mode="standard",
            seed=42,
        )
        self.assertEqual(sub.cmd, "new_game")
        self.assertEqual(
            sub.args,
            {
                "country_id": "FRA",
                "difficulty": "normal",
                "opening_mode": "standard",
                "seed": 42,
            },
        )

        # Negative contrast: missing seed
        with self.assertRaises(DeclarationError) as ctx:
            make_holdings_record(
                "new_game",
                tick=1,
                seq=1,
                country_id="FRA",
                difficulty="normal",
                opening_mode="standard",
            )
        self.assertEqual(ctx.exception.code, "E_HOLDINGS_NEW_GAME_FIELDS")

    def test_neutral_boundary_cdp_cannot_relabel_godot_tree(self):
        decl = PixelDeclaration(
            channel_order="RGB",
            stride_bytes=6,
            orientation="top_down",
            alpha="none",
            color_space="srgb",
            width=2,
            height=2,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env",
            binding_id="bind",
        )
        # Attempting to inject Godot tree into cdp source_kind is rejected
        with self.assertRaises(InvariantViolationError) as ctx:
            InjectedObservation(
                source_kind="cdp",
                tree_data={"godot_node": "Root/CanvasItem"},
                frame_buffer=b"\x00" * 12,
                declaration=decl,
            )
        self.assertEqual(ctx.exception.code, "E_INVALID_CDP_SOURCE")

    def test_admission_advances_bookkeeping_without_state_mutation_on_rejection(self):
        boundary = SourceNeutralBoundary()
        sub = make_holdings_record("pause", tick=1, seq=1)

        # Rejected attempt
        admitted = boundary.submit_t2(sub, admit=False, reason="quota_exceeded")
        self.assertFalse(admitted)
        self.assertEqual(boundary.simulation_state_version, 0)
        self.assertEqual(boundary.bookkeeping_counter, 1)
        self.assertEqual(len(boundary.rejected_attempts), 1)

        # Admitted attempt
        admitted = boundary.submit_t2(sub, admit=True)
        self.assertTrue(admitted)
        self.assertEqual(boundary.simulation_state_version, 1)


if __name__ == "__main__":
    unittest.main()
