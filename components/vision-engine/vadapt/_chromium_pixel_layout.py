"""Declared Chromium RAM pixel normalization and transactional capture (JQ3-E-E3-G1).

Invariants enforced:
- INV-E3-1: Validate and freeze pixel layout declaration (channel_order,
  stride_bytes, orientation, alpha_policy, color_space, width, height,
  dpr_ppm, zoom_ppm, environment_id) before hashing or pixel access.
- INV-E3-2: Preserve raw_sha256 (over exact raw buffer bytes including row
  padding and source channel/orientation layout) separately from
  normalized_sha256 (over canonical top-down RGB24 bytes), removing padding
  and reordering only as explicitly declared.
- INV-E3-3: Capture transaction freezes presentation changes, reads pixels
  and presentation tree, and rechecks presentation revision and metadata
  correlation before committing.
- INV-E3-4: Forbid persistent pixel storage on disk or in receipts; zero and
  release transient pixel buffers on success, cancellation, timeout, or error,
  and consume JQ2-B WindowStreamBank without creating a second stream.
"""
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

MAX_DIM = 4096
MAX_STRIDE_BYTES = MAX_DIM * 8
MAX_FRAME_BYTES = 64 * 1024 * 1024
MIN_PPM = 100_000
MAX_PPM = 16_000_000
DEFAULT_PPM = 1_000_000

CHANNEL_ORDERS = (
    "RGB",
    "BGR",
    "RGBA",
    "BGRA",
    "ARGB",
    "ABGR",
    "RGBX",
    "BGRX",
    "XRGB",
    "XBGR",
)
THREE_CHANNEL_ORDERS = frozenset({"RGB", "BGR"})
ALPHA_CHANNEL_ORDERS = frozenset({"RGBA", "BGRA", "ARGB", "ABGR"})
PAD_CHANNEL_ORDERS = frozenset({"RGBX", "BGRX", "XRGB", "XBGR"})

ORIENTATIONS = ("top_down", "bottom_up")
ALPHA_POLICIES = (
    "none",
    "opaque",
    "require_opaque",
    "discard",
    "unpremultiplied",
    "premultiplied",
)
COLOR_SPACES = ("sRGB",)
VALID_RESIDENCIES = frozenset({"ram_verified", "ram_only"})
CLOSED_STATES = frozenset(
    {"READY", "BLOCKED", "COMMITTED", "ABORTED", "CANCELLED", "TIMED_OUT"}
)
RECEIPT_FIELDS = (
    "state",
    "handle",
    "epoch",
    "frame_seq",
    "presentation_revision",
    "tree_revision",
    "tree_digest",
    "raw_sha256",
    "normalized_sha256",
    "width",
    "height",
    "channel_order",
    "stride_bytes",
    "orientation",
    "alpha_policy",
    "color_space",
    "dpr_ppm",
    "zoom_ppm",
    "environment_id",
    "memory_released",
    "elapsed_ns",
)

_ENV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LAYOUT_FIELD_NAMES = frozenset(
    {
        "width",
        "height",
        "channel_order",
        "stride_bytes",
        "orientation",
        "alpha_policy",
        "color_space",
        "dpr_ppm",
        "zoom_ppm",
        "environment_id",
    }
)


class ChromiumPixelError(window_stream_bank.StreamError):
    """Fail-closed normalization, attestation, correlation, or transaction error."""

    def __init__(self, code: str, msg: str = "", *, state: str | None = None):
        if state is None:
            state = "BLOCKED" if code == "E_LIVE_BLOCKED" else "ABORTED"
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.msg = msg
        self.state = state


def _exact_int(
    value: object,
    name: str,
    code: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ChromiumPixelError(
            code, f"{name} must be an exact int in [{minimum}, {maximum}]"
        )
    return value


def _validate_environment_id(
    value: object, code: str = "E_LAYOUT_ENVIRONMENT"
) -> str:
    if type(value) is not str or _ENV_ID_RE.fullmatch(value) is None:
        raise ChromiumPixelError(
            code, "environment_id must be a non-empty safe ASCII token"
        )
    return value


@dataclass(frozen=True)
class PixelLayoutDeclaration:
    """Explicit Chromium RAM pixel layout contract validated before hashing (INV-E3-1)."""

    width: int
    height: int
    channel_order: str
    stride_bytes: int
    orientation: str = "top_down"
    alpha_policy: str = "opaque"
    color_space: str = "sRGB"
    dpr_ppm: int = DEFAULT_PPM
    zoom_ppm: int = DEFAULT_PPM
    environment_id: str = "chromium-ram-env"

    def __post_init__(self) -> None:
        _exact_int(
            self.width, "width", "E_LAYOUT_DIMENSIONS", minimum=1, maximum=MAX_DIM
        )
        _exact_int(
            self.height,
            "height",
            "E_LAYOUT_DIMENSIONS",
            minimum=1,
            maximum=MAX_DIM,
        )
        if (
            type(self.channel_order) is not str
            or self.channel_order not in CHANNEL_ORDERS
        ):
            raise ChromiumPixelError(
                "E_LAYOUT_CHANNEL_ORDER",
                f"unsupported channel_order {self.channel_order!r}",
            )
        bpp = 3 if self.channel_order in THREE_CHANNEL_ORDERS else 4
        min_stride = self.width * bpp
        _exact_int(
            self.stride_bytes,
            "stride_bytes",
            "E_LAYOUT_STRIDE",
            minimum=min_stride,
            maximum=MAX_STRIDE_BYTES,
        )
        if self.stride_bytes * self.height > MAX_FRAME_BYTES:
            raise ChromiumPixelError(
                "E_LAYOUT_STRIDE", "raw frame footprint exceeds MAX_FRAME_BYTES"
            )
        if (
            type(self.orientation) is not str
            or self.orientation not in ORIENTATIONS
        ):
            raise ChromiumPixelError(
                "E_LAYOUT_ORIENTATION",
                f"unsupported orientation {self.orientation!r}",
            )
        if (
            type(self.alpha_policy) is not str
            or self.alpha_policy not in ALPHA_POLICIES
        ):
            raise ChromiumPixelError(
                "E_LAYOUT_ALPHA_POLICY",
                f"unsupported alpha_policy {self.alpha_policy!r}",
            )
        if self.channel_order in THREE_CHANNEL_ORDERS:
            if self.alpha_policy not in ("none", "opaque", "require_opaque"):
                raise ChromiumPixelError(
                    "E_LAYOUT_ALPHA_POLICY",
                    f"3-channel order {self.channel_order} incompatible with alpha_policy {self.alpha_policy!r}",
                )
        elif self.channel_order in PAD_CHANNEL_ORDERS:
            if self.alpha_policy not in ("discard", "opaque"):
                raise ChromiumPixelError(
                    "E_LAYOUT_ALPHA_POLICY",
                    f"padded order {self.channel_order} requires alpha_policy 'discard' or 'opaque'",
                )
        else:
            if self.alpha_policy == "none":
                raise ChromiumPixelError(
                    "E_LAYOUT_ALPHA_POLICY",
                    f"alpha-bearing order {self.channel_order} requires an explicit alpha_policy",
                )
        if (
            type(self.color_space) is not str
            or self.color_space not in COLOR_SPACES
        ):
            raise ChromiumPixelError(
                "E_LAYOUT_COLOR_SPACE",
                f"unsupported color_space {self.color_space!r}",
            )
        _exact_int(
            self.dpr_ppm,
            "dpr_ppm",
            "E_LAYOUT_DPR",
            minimum=MIN_PPM,
            maximum=MAX_PPM,
        )
        _exact_int(
            self.zoom_ppm,
            "zoom_ppm",
            "E_LAYOUT_ZOOM",
            minimum=MIN_PPM,
            maximum=MAX_PPM,
        )
        _validate_environment_id(self.environment_id, "E_LAYOUT_ENVIRONMENT")

    @property
    def bytes_per_pixel(self) -> int:
        return 3 if self.channel_order in THREE_CHANNEL_ORDERS else 4

    @property
    def min_stride_bytes(self) -> int:
        return self.width * self.bytes_per_pixel

    @property
    def row_padding_bytes(self) -> int:
        return self.stride_bytes - self.min_stride_bytes

    @property
    def expected_raw_bytes(self) -> int:
        return self.stride_bytes * self.height

    @property
    def normalized_bytes(self) -> int:
        return self.width * self.height * 3

    @property
    def has_alpha_channel(self) -> bool:
        return self.channel_order in ALPHA_CHANNEL_ORDERS


def validate_layout_declaration(declaration: object) -> PixelLayoutDeclaration:
    """Validate and freeze a PixelLayoutDeclaration before any hashing or pixel read."""
    if isinstance(declaration, PixelLayoutDeclaration):
        return declaration
    if isinstance(declaration, dict):
        unknown = set(declaration.keys()) - _LAYOUT_FIELD_NAMES
        if unknown or not _LAYOUT_FIELD_NAMES.issset(declaration.keys()) if hasattr(_LAYOUT_FIELD_NAMES, "isshet") else (unknown or not _LAYOUT_FIELD_NAMES.issubset(declaration.keys())):
            raise ChromiumPixelError(
                "E_LAYOUT_DECLARATION",
                "declaration mapping must provide all 10 required layout fields with no extras",
            )
        return PixelLayoutDeclaration(
            width=declaration["width"],
            height=declaration["height"],
            channel_order=declaration["channel_order"],
            stride_bytes=declaration["stride_bytes"],
            orientation=declaration["orientation"],
            alpha_policy=declaration["alpha_policy"],
            color_space=declaration["color_space"],
            dpr_ppm=declaration["dpr_ppm"],
            zoom_ppm=declaration["zoom_ppm"],
            environment_id=declaration["environment_id"],
        )
    try:
        return PixelLayoutDeclaration(
            width=getattr(declaration, "width"),
            height=getattr(declaration, "height"),
            channel_order=getattr(declaration, "channel_order"),
            stride_bytes=getattr(declaration, "stride_bytes"),
            orientation=getattr(declaration, "orientation"),
            alpha_policy=getattr(declaration, "alpha_policy"),
            color_space=getattr(declaration, "color_space"),
            dpr_ppm=getattr(declaration, "dpr_ppm"),
            zoom_ppm=getattr(declaration, "zoom_ppm"),
            environment_id=getattr(declaration, "environment_id"),
        )
    except AttributeError as exc:
        raise ChromiumPixelError(
            "E_LAYOUT_DECLARATION",
            "declaration must provide all 10 required layout attributes",
        ) from exc


@dataclass(frozen=True)
class NormalizedFrame:
    """Normalized canonical RGB24 RAM frame with separated raw and normalized digests (INV-E3-2)."""

    width: int
    height: int
    rgb: np.ndarray
    raw_sha256: str
    normalized_sha256: str
    declaration: PixelLayoutDeclaration
    raw_byte_length: int
    normalized_byte_length: int


def _extract_channels(
    pixels: np.ndarray, channel_order: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
    if channel_order == "RGB":
        return pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2], None
    if channel_order == "BGR":
        return pixels[:, :, 2], pixels[:, :, 1], pixels[:, :, 0], None
    if channel_order == "RGBA":
        return (
            pixels[:, :, 0],
            pixels[:, :, 1],
            pixels[:, :, 2],
            pixels[:, :, 3],
        )
    if channel_order == "BGRA":
        return (
            pixels[:, :, 2],
            pixels[:, :, 1],
            pixels[:, :, 0],
            pixels[:, :, 3],
        )
    if channel_order == "ARGB":
        return (
            pixels[:, :, 1],
            pixels[:, :, 2],
            pixels[:, :, 3],
            pixels[:, :, 0],
        )
    if channel_order == "ABGR":
        return (
            pixels[:, :, 3],
            pixels[:, :, 2],
            pixels[:, :, 1],
            pixels[:, :, 0],
        )
    if channel_order == "RGBX":
        return pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2], None
    if channel_order == "BGRX":
        return pixels[:, :, 2], pixels[:, :, 1], pixels[:, :, 0], None
    if channel_order == "XRGB":
        return pixels[:, :, 1], pixels[:, :, 2], pixels[:, :, 3], None
    if channel_order == "XBGR":
        return pixels[:, :, 3], pixels[:, :, 2], pixels[:, :, 1], None
    raise ChromiumPixelError(
        "E_LAYOUT_CHANNEL_ORDER", f"unsupported channel_order {channel_order!r}"
    )


def _apply_alpha_policy(
    r: np.ndarray,
    g: np.ndarray,
    b: np.ndarray,
    a: np.ndarray | None,
    decl: PixelLayoutDeclaration,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not decl.has_alpha_channel or a is None:
        return r, g, b
    policy = decl.alpha_policy
    if policy in ("opaque", "require_opaque"):
        if not bool(np.all(a == 255)):
            raise ChromiumPixelError(
                "E_ALPHA_NOT_OPAQUE",
                "alpha_policy requires all alpha bytes to equal 255",
            )
        return r, g, b
    if policy == "discard":
        return r, g, b
    if policy == "unpremultiplied":
        a16 = a.astype(np.uint16)
        r_out = ((r.astype(np.uint16) * a16 + 127) // 255).astype(np.uint8)
        g_out = ((g.astype(np.uint16) * a16 + 127) // 255).astype(np.uint8)
        b_out = ((b.astype(np.uint16) * a16 + 127) // 255).astype(np.uint8)
        return r_out, g_out, b_out
    if policy == "premultiplied":
        if bool(np.any((r > a) | (g > a) | (b > a))):
            raise ChromiumPixelError(
                "E_ALPHA_PREMULTIPLIED_OVERFLOW",
                "color channel exceeds alpha in premultiplied buffer",
            )
        a16 = a.astype(np.uint16)
        safe_a = np.where(a16 > 0, a16, 1)
        r_out = np.where(
            a16 > 0, (r.astype(np.uint16) * 255 + (safe_a // 2)) // safe_a, 0
        ).astype(np.uint8)
        g_out = np.where(
            a16 > 0, (g.astype(np.uint16) * 255 + (safe_a // 2)) // safe_a, 0
        ).astype(np.uint8)
        b_out = np.where(
            a16 > 0, (b.astype(np.uint16) * 255 + (safe_a // 2)) // safe_a, 0
        ).astype(np.uint8)
        return r_out, g_out, b_out
    raise ChromiumPixelError(
        "E_LAYOUT_ALPHA_POLICY", f"unsupported alpha_policy {policy!r}"
    )


def normalize_chromium_pixels(
    raw_buffer: object, declaration: object
) -> NormalizedFrame:
    """Validate layout declaration before hashing, then separate raw and normalized digests."""
    # INV-E3-1: Validate all 10 declaration parameters BEFORE touching raw_buffer or hashing.
    decl = validate_layout_declaration(declaration)

    if isinstance(raw_buffer, RamPixelLease):
        raw_source = raw_buffer.view()
    else:
        raw_source = raw_buffer

    if not isinstance(raw_source, (bytes, bytearray, memoryview)):
        raise ChromiumPixelError(
            "E_RAW_BUFFER_TYPE",
            "raw_buffer must be a bytes-like RAM buffer or RamPixelLease",
        )
    mv = memoryview(raw_source)
    try:
        byte_view = mv.cast("B")
        try:
            raw_len = len(byte_view)
            if raw_len != decl.expected_raw_bytes:
                raise ChromiumPixelError(
                    "E_RAW_BUFFER_SIZE",
                    f"expected {decl.expected_raw_bytes} raw bytes, got {raw_len}",
                )
            raw_bytes = bytes(byte_view)
        finally:
            byte_view.release()
    finally:
        mv.release()

    # INV-E3-2: Hash exact raw input bytes (including padding and source layout) separately.
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    bpp = decl.bytes_per_pixel
    active_row_bytes = decl.width * bpp
    rows = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(
        (decl.height, decl.stride_bytes)
    )
    trimmed = rows[:, :active_row_bytes]
    if decl.orientation == "bottom_up":
        trimmed = trimmed[::-1, :]
    pixels = trimmed.reshape((decl.height, decl.width, bpp))

    r, g, b, a = _extract_channels(pixels, decl.channel_order)
    r, g, b = _apply_alpha_policy(r, g, b, a, decl)

    out = np.empty((decl.height, decl.width, 3), dtype=np.uint8)
    out[:, :, 0] = r
    out[:, :, 1] = g
    out[:, :, 2] = b
    norm_bytes = out.tobytes(order="C")
    normalized_sha256 = hashlib.sha256(norm_bytes).hexdigest()

    immutable_rgb = np.frombuffer(norm_bytes, dtype=np.uint8).reshape(
        (decl.height, decl.width, 3)
    )
    return NormalizedFrame(
        width=decl.width,
        height=decl.height,
        rgb=immutable_rgb,
        raw_sha256=raw_sha256,
        normalized_sha256=normalized_sha256,
        declaration=decl,
        raw_byte_length=len(raw_bytes),
        normalized_byte_length=len(norm_bytes),
    )


class RamPixelLease:
    """Transient in-RAM pixel buffer lease that zeroes and frees memory on release (INV-E3-4)."""

    def __init__(self, initial_bytes: bytes | bytearray | memoryview):
        if not isinstance(initial_bytes, (bytes, bytearray, memoryview)):
            raise ChromiumPixelError(
                "E_RAW_BUFFER_TYPE", "RamPixelLease requires bytes-like data"
            )
        self._buf: bytearray | None = bytearray(initial_bytes)
        self._byte_length = len(self._buf)
        self._released = False
        self._lock = threading.RLock()

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    @property
    def byte_length(self) -> int:
        return self._byte_length

    def view(self) -> bytes:
        with self._lock:
            if self._released or self._buf is None:
                raise ChromiumPixelError(
                    "E_LEASE_RELEASED", "pixel lease has already been released"
                )
            return bytes(self._buf)

    def release(self) -> bool:
        with self._lock:
            if self._released:
                return True
            if self._buf is not None:
                self._buf[:] = b"\x00" * len(self._buf)
                self._buf = None
            self._released = True
            return True


