"""jq3_chromium_pixels: chromium parity."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
from typing import Callable
import numpy as np
from vadapt import window_stream_bank

from ._chromium_pixel_layout import (
    MAX_DIM, MAX_STRIDE_BYTES, MAX_FRAME_BYTES, MIN_PPM, MAX_PPM, DEFAULT_PPM, CHANNEL_ORDERS,
    THREE_CHANNEL_ORDERS, ALPHA_CHANNEL_ORDERS, PAD_CHANNEL_ORDERS, ORIENTATIONS,
    ALPHA_POLICIES, COLOR_SPACES, VALID_RESIDENCIES, CLOSED_STATES, RECEIPT_FIELDS, _ENV_ID_RE,
    _LAYOUT_FIELD_NAMES, ChromiumPixelError, _exact_int, _validate_environment_id,
    PixelLayoutDeclaration, validate_layout_declaration, NormalizedFrame, _extract_channels,
    _apply_alpha_policy, normalize_chromium_pixels, RamPixelLease,
)

@dataclass(frozen=True)
class ParityReport:
    """Comparison report for a declared RAM pixel pair."""

    matched: bool
    presentation_revision: int
    raw_sha256_a: str
    raw_sha256_b: str
    normalized_sha256: str
    width: int
    height: int


def verify_ram_pair_parity(
    raw_a: object,
    decl_a: object,
    revision_a: int,
    raw_b: object,
    decl_b: object,
    revision_b: int,
    *,
    expected_decl_a: object | None = None,
    expected_decl_b: object | None = None,
) -> ParityReport:
    """Compare two declared RAM pixel representations and enforce revision and declaration parity."""
    v_decl_a = validate_layout_declaration(decl_a)
    v_decl_b = validate_layout_declaration(decl_b)
    if expected_decl_a is not None and v_decl_a != validate_layout_declaration(
        expected_decl_a
    ):
        raise ChromiumPixelError(
            "E_DECLARATION_MISMATCH",
            "decl_a does not match attested layout declaration",
        )
    if expected_decl_b is not None and v_decl_b != validate_layout_declaration(
        expected_decl_b
    ):
        raise ChromiumPixelError(
            "E_DECLARATION_MISMATCH",
            "decl_b does not match attested layout declaration",
        )
    _exact_int(revision_a, "revision_a", "E_REVISION_MISMATCH", minimum=0)
    _exact_int(revision_b, "revision_b", "E_REVISION_MISMATCH", minimum=0)

    norm_a = normalize_chromium_pixels(raw_a, v_decl_a)
    norm_b = normalize_chromium_pixels(raw_b, v_decl_b)

    if (
        v_decl_a.width != v_decl_b.width
        or v_decl_a.height != v_decl_b.height
        or v_decl_a.color_space != v_decl_b.color_space
        or v_decl_a.dpr_ppm != v_decl_b.dpr_ppm
        or v_decl_a.zoom_ppm != v_decl_b.zoom_ppm
        or v_decl_a.environment_id != v_decl_b.environment_id
    ):
        raise ChromiumPixelError(
            "E_CORRELATION_MISMATCH",
            "RAM pair declarations disagree on geometry, color_space, scale, or environment_id",
        )
    if revision_a != revision_b:
        raise ChromiumPixelError(
            "E_REVISION_MISMATCH",
            f"RAM pair bound to different presentation revisions ({revision_a} != {revision_b})",
        )
    if norm_a.normalized_sha256 != norm_b.normalized_sha256 or not np.array_equal(
        norm_a.rgb, norm_b.rgb
    ):
        raise ChromiumPixelError(
            "E_PARITY_MISMATCH",
            f"normalized digest mismatch ({norm_a.normalized_sha256} != {norm_b.normalized_sha256})",
        )
    return ParityReport(
        matched=True,
        presentation_revision=revision_a,
        raw_sha256_a=norm_a.raw_sha256,
        raw_sha256_b=norm_b.raw_sha256,
        normalized_sha256=norm_a.normalized_sha256,
        width=norm_a.width,
        height=norm_a.height,
    )


def build_sentinel_rgb(width: int, height: int, *, seed: int = 0) -> np.ndarray:
    """Build asymmetric top/bottom and RGB-vs-BGR sentinel RGB24 test/control frames."""
    _exact_int(width, "width", "E_LAYOUT_DIMENSIONS", minimum=1, maximum=MAX_DIM)
    _exact_int(
        height, "height", "E_LAYOUT_DIMENSIONS", minimum=1, maximum=MAX_DIM
    )
    out = np.empty((height, width, 3), dtype=np.uint8)
    for y in range(height):
        for x in range(width):
            r = (37 * x + 19 * y + 11 * seed + 29) & 0xFF
            g = (53 * x + 71 * y + 23 * seed + 97) & 0xFF
            b = (89 * x + 31 * y + 41 * seed + 173) & 0xFF
            if r == b:
                b = (b ^ 0x55) & 0xFF
            out[y, x, 0] = r
            out[y, x, 1] = g
            out[y, x, 2] = b
    # Explicit asymmetric top vs bottom and RGB vs BGR sentinels:
    out[0, 0] = np.array([250, 40, 10], dtype=np.uint8)
    if width > 1:
        out[0, width - 1] = np.array([15, 220, 95], dtype=np.uint8)
    if height > 1:
        out[height - 1, 0] = np.array([20, 85, 245], dtype=np.uint8)
        if width > 1:
            out[height - 1, width - 1] = np.array(
                [190, 30, 140], dtype=np.uint8
            )
    return out


def encode_raw_chromium_buffer(
    rgb: np.ndarray,
    declaration: PixelLayoutDeclaration,
    *,
    alpha_plane: np.ndarray | None = None,
    pad_seed: int = 0xA5,
) -> bytes:
    """Encode an RGB24 array (and optional alpha plane) into a raw Chromium RAM buffer."""
    decl = validate_layout_declaration(declaration)
    arr = np.asarray(rgb, dtype=np.uint8)
    if arr.shape != (decl.height, decl.width, 3):
        raise ChromiumPixelError(
            "E_LAYOUT_DIMENSIONS",
            "rgb shape must match declaration height x width x 3",
        )

    if alpha_plane is not None:
        alpha = np.asarray(alpha_plane, dtype=np.uint8)
        if alpha.shape != (decl.height, decl.width):
            raise ChromiumPixelError(
                "E_LAYOUT_DIMENSIONS",
                "alpha_plane shape must match height x width",
            )
    else:
        alpha = np.full((decl.height, decl.width), 255, dtype=np.uint8)

    bpp = decl.bytes_per_pixel
    packed = np.empty((decl.height, decl.width, bpp), dtype=np.uint8)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    if decl.channel_order == "RGB":
        packed[:, :, 0], packed[:, :, 1], packed[:, :, 2] = r, g, b
    elif decl.channel_order == "BGR":
        packed[:, :, 0], packed[:, :, 1], packed[:, :, 2] = b, g, r
    elif decl.channel_order == "RGBA":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (r, g, b, alpha)
    elif decl.channel_order == "BGRA":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (b, g, r, alpha)
    elif decl.channel_order == "ARGB":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (alpha, r, g, b)
    elif decl.channel_order == "ABGR":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (alpha, b, g, r)
    elif decl.channel_order == "RGBX":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (r, g, b, alpha)
    elif decl.channel_order == "BGRX":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (b, g, r, alpha)
    elif decl.channel_order == "XRGB":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (alpha, r, g, b)
    elif decl.channel_order == "XBGR":
        (
            packed[:, :, 0],
            packed[:, :, 1],
            packed[:, :, 2],
            packed[:, :, 3],
        ) = (alpha, b, g, r)

    if decl.orientation == "bottom_up":
        packed = packed[::-1, :, :]

    active_bytes = decl.width * bpp
    raw_rows = bytearray(decl.stride_bytes * decl.height)
    for y in range(decl.height):
        row_off = y * decl.stride_bytes
        raw_rows[row_off : row_off + active_bytes] = packed[y].tobytes()
        for p in range(active_bytes, decl.stride_bytes):
            raw_rows[row_off + p] = (pad_seed + y * 29 + p * 17) & 0xFF
    return bytes(raw_rows)


@dataclass(frozen=True)
class BehavioralCaseReport:
    """Independent outcome record for behavioral acceptance cases."""

    case_id: str
    category: str
    positive: bool
    passed: bool
    error_code: str | None
    raw_sha256: str | None
    normalized_sha256: str | None


def run_behavioral_case_matrix() -> dict[str, BehavioralCaseReport]:
    """Run and report each positive and single-condition negative behavioral case independently."""
    from .jq3_chromium_pixels import ChromiumStreamAdapter

    reports: dict[str, BehavioralCaseReport] = {}
    w, h = 5, 4  # odd width with asymmetric top/bottom and RGB/BGR sentinels
    base_rgb = build_sentinel_rgb(w, h, seed=7)

    decl_bgra = PixelLayoutDeclaration(
        width=w,
        height=h,
        channel_order="BGRA",
        stride_bytes=w * 4 + 12,
        orientation="bottom_up",
        alpha_policy="opaque",
        color_space="sRGB",
        dpr_ppm=2_000_000,
        zoom_ppm=1_250_000,
        environment_id="env-chromium-pos-1",
    )
    decl_rgb = PixelLayoutDeclaration(
        width=w,
        height=h,
        channel_order="RGB",
        stride_bytes=w * 3,
        orientation="top_down",
        alpha_policy="none",
        color_space="sRGB",
        dpr_ppm=2_000_000,
        zoom_ppm=1_250_000,
        environment_id="env-chromium-pos-1",
    )
    raw_bgra = encode_raw_chromium_buffer(base_rgb, decl_bgra, pad_seed=0x33)
    raw_rgb = encode_raw_chromium_buffer(base_rgb, decl_rgb, pad_seed=0x00)

    # Category 1: Row-stride normalization (positive + single-condition negative)
    norm_stride = normalize_chromium_pixels(raw_bgra, decl_bgra)
    norm_ref = normalize_chromium_pixels(raw_rgb, decl_rgb)
    reports["pos_stride_odd_width_padded"] = BehavioralCaseReport(
        case_id="pos_stride_odd_width_padded",
        category="stride",
        positive=True,
        passed=(
            norm_stride.normalized_sha256 == norm_ref.normalized_sha256
            and np.array_equal(norm_stride.rgb, base_rgb)
        ),
        error_code=None,
        raw_sha256=norm_stride.raw_sha256,
        normalized_sha256=norm_stride.normalized_sha256,
    )
    try:
        PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="RGBA",
            stride_bytes=w * 4 - 1,
            orientation="top_down",
            alpha_policy="opaque",
        )
        reports["neg_stride_too_small"] = BehavioralCaseReport(
            "neg_stride_too_small", "stride", False, False, None, None, None
        )
    except ChromiumPixelError as exc:
        reports["neg_stride_too_small"] = BehavioralCaseReport(
            "neg_stride_too_small",
            "stride",
            False,
            exc.code == "E_LAYOUT_STRIDE",
            exc.code,
            None,
            None,
        )

    # Category 2: Channel order (positive + single-condition negative)
    bgr_decl = PixelLayoutDeclaration(
        width=w,
        height=h,
        channel_order="BGR",
        stride_bytes=w * 3,
        orientation="top_down",
        alpha_policy="none",
        color_space="sRGB",
        dpr_ppm=2_000_000,
        zoom_ppm=1_250_000,
        environment_id="env-chromium-pos-1",
    )
    raw_bgr = encode_raw_chromium_buffer(base_rgb, bgr_decl)
    norm_bgr = normalize_chromium_pixels(raw_bgr, bgr_decl)
    reports["pos_channel_order_bgr_reordered"] = BehavioralCaseReport(
        case_id="pos_channel_order_bgr_reordered",
        category="channel_order",
        positive=True,
        passed=(
            norm_bgr.normalized_sha256 == norm_ref.normalized_sha256
            and tuple(norm_bgr.rgb[0, 0]) == (250, 40, 10)
        ),
        error_code=None,
        raw_sha256=norm_bgr.raw_sha256,
        normalized_sha256=norm_bgr.normalized_sha256,
    )
    try:
        verify_ram_pair_parity(raw_bgr, decl_rgb, 11, raw_rgb, decl_rgb, 11)
        reports["neg_channel_order_mismatch"] = BehavioralCaseReport(
            "neg_channel_order_mismatch",
            "channel_order",
            False,
            False,
            None,
            None,
            None,
        )
    except ChromiumPixelError as exc:
        reports["neg_channel_order_mismatch"] = BehavioralCaseReport(
            "neg_channel_order_mismatch",
            "channel_order",
            False,
            exc.code == "E_PARITY_MISMATCH",
            exc.code,
            None,
            None,
        )

    # Category 3: Orientation (positive + single-condition negative)
    bottom_up_decl = PixelLayoutDeclaration(
        width=w,
        height=h,
        channel_order="RGB",
        stride_bytes=w * 3,
        orientation="bottom_up",
        alpha_policy="none",
        color_space="sRGB",
        dpr_ppm=2_000_000,
        zoom_ppm=1_250_000,
        environment_id="env-chromium-pos-1",
    )
    raw_bottom_up = encode_raw_chromium_buffer(base_rgb, bottom_up_decl)
    norm_bu = normalize_chromium_pixels(raw_bottom_up, bottom_up_decl)
    reports["pos_orientation_bottom_up_flipped"] = BehavioralCaseReport(
        case_id="pos_orientation_bottom_up_flipped",
        category="orientation",
        positive=True,
        passed=(
            norm_bu.normalized_sha256 == norm_ref.normalized_sha256
            and tuple(norm_bu.rgb[0, 0]) == (250, 40, 10)
            and tuple(norm_bu.rgb[h - 1, 0]) == (20, 85, 245)
        ),
        error_code=None,
        raw_sha256=norm_bu.raw_sha256,
        normalized_sha256=norm_bu.normalized_sha256,
    )
    try:
        verify_ram_pair_parity(
            raw_bottom_up, decl_rgb, 11, raw_rgb, decl_rgb, 11
        )
        reports["neg_orientation_mismatch"] = BehavioralCaseReport(
            "neg_orientation_mismatch",
            "orientation",
            False,
            False,
            None,
            None,
            None,
        )
    except ChromiumPixelError as exc:
        reports["neg_orientation_mismatch"] = BehavioralCaseReport(
            "neg_orientation_mismatch",
            "orientation",
            False,
            exc.code == "E_PARITY_MISMATCH",
            exc.code,
            None,
            None,
        )

    # Category 4: Revision correlation & parity (positive + single-condition negative)
    parity = verify_ram_pair_parity(
        raw_bgra, decl_bgra, 11, raw_rgb, decl_rgb, 11
    )
    reports["pos_matching_exact_ram_pair"] = BehavioralCaseReport(
        case_id="pos_matching_exact_ram_pair",
        category="revision",
        positive=True,
        passed=parity.matched and parity.raw_sha256_a != parity.raw_sha256_b,
        error_code=None,
        raw_sha256=parity.raw_sha256_a,
        normalized_sha256=parity.normalized_sha256,
    )
    try:
        verify_ram_pair_parity(raw_bgra, decl_bgra, 10, raw_rgb, decl_rgb, 11)
        reports["neg_stale_revision_mismatch"] = BehavioralCaseReport(
            "neg_stale_revision_mismatch",
            "revision",
            False,
            False,
            None,
            None,
            None,
        )
    except ChromiumPixelError as exc:
        reports["neg_stale_revision_mismatch"] = BehavioralCaseReport(
            "neg_stale_revision_mismatch",
            "revision",
            False,
            exc.code == "E_REVISION_MISMATCH",
            exc.code,
            None,
            None,
        )

    # Category 5: Separation of raw vs normalized digests (positive + single-condition negative)
    raw_bgra_pad2 = encode_raw_chromium_buffer(
        base_rgb, decl_bgra, pad_seed=0xCC
    )
    norm1 = normalize_chromium_pixels(raw_bgra, decl_bgra)
    norm2 = normalize_chromium_pixels(raw_bgra_pad2, decl_bgra)
    reports["pos_digest_separation"] = BehavioralCaseReport(
        case_id="pos_digest_separation",
        category="digest_separation",
        positive=True,
        passed=(
            norm1.raw_sha256 != norm2.raw_sha256
            and norm1.normalized_sha256 == norm2.normalized_sha256
            and norm1.raw_sha256 != norm1.normalized_sha256
        ),
        error_code=None,
        raw_sha256=norm1.raw_sha256,
        normalized_sha256=norm1.normalized_sha256,
    )
    corrupted_frame = NormalizedFrame(
        width=norm1.width,
        height=norm1.height,
        rgb=norm1.rgb,
        raw_sha256=norm1.raw_sha256,
        normalized_sha256=norm1.raw_sha256,  # wrong: substituting raw_sha256 for normalized_sha256
        declaration=norm1.declaration,
        raw_byte_length=norm1.raw_byte_length,
        normalized_byte_length=norm1.normalized_byte_length,
    )
    ring_sz = window_stream_bank.rtring.ring_bytes(2, w, h)
    tmp_bank = window_stream_bank.WindowStreamBank(quota_bytes=2 * ring_sz)
    tmp_adapter = ChromiumStreamAdapter(
        tmp_bank,
        window_stream_bank.Handle("cdp_tab", "case-matrix-tab", 1),
        window_stream_bank.StreamSpec(
            width=w, height=h, slots=2, quota_bytes=ring_sz
        ),
    )
    try:
        try:
            tmp_adapter.publish_frame(corrupted_frame, frame_seq=0)
            reports["neg_digest_conflation_rejected"] = BehavioralCaseReport(
                "neg_digest_conflation_rejected",
                "digest_separation",
                False,
                False,
                None,
                None,
                None,
            )
        except ChromiumPixelError as exc:
            reports["neg_digest_conflation_rejected"] = BehavioralCaseReport(
                "neg_digest_conflation_rejected",
                "digest_separation",
                False,
                exc.code == "E_STREAM_DIGEST_MISMATCH",
                exc.code,
                norm1.raw_sha256,
                norm1.normalized_sha256,
            )
    finally:
        tmp_adapter.close()

    return reports
