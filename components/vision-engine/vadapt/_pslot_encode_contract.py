"""vadapt.pslot_encode - In-memory lossless PNG encoding and PSLOT wave-1 encoding contract.

Implements independent in-memory lossless PNG encoding by default using stdlib
``zlib`` and ``struct``, preserving exact RGB24 bytes and dimensions with
verified PNG chunks and CRCs. Supports explicit per-call lossy opt-in for
injected lossy codecs with full receipt provenance (input digest, encoded
payload digest, and encoding mode), while forbidding implicit JPEG, resizing,
or silent fallback. Enforces pre-provider geometry/encoding/quota-floor/config
refusal gates and keeps raw-frame digests distinct from encoded-payload digests.
"""
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

AUSENTE: str = "AUSENTE"
RUNTIME_LABEL_VM: str = "VM"
RUNTIME_LABEL_HOST: str = AUSENTE

PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
PNG_BIT_DEPTH_8: int = 8
PNG_COLOR_TYPE_RGB: int = 2
PNG_COMPRESSION_DEFLATE: int = 0
PNG_FILTER_ADAPTIVE: int = 0
PNG_INTERLACE_NONE: int = 0
PNG_FILTER_NONE: int = 0

MAX_SIDE: int = xfb.MAX_SIDE
MAX_RGB_BYTES: int = MAX_SIDE * MAX_SIDE * 3

MODE_LOSSLESS_PNG: str = "lossless_png"
MIME_PNG: str = "image/png"
CODEC_STDLIB_PNG: str = "stdlib_zlib_struct_png"

ROLE_DEFAULT_PERCEIVER: str = "default_perceiver"
ROLE_ALTERNATIVE_PERCEIVER: str = "alternative_perceiver"
ROLE_OUT_OF_LOOP_JUDGE: str = "out_of_loop_judge"

MODEL_META_MUSE_SPARK: str = "Meta Muse Spark 1.3"
MODEL_GEMINI_FLASH: str = "Gemini 3.8 Flash"
MODEL_GEMINI_ARGON: str = "Gemini 4 Argon"
MODEL_DEEPSEEK_FLASH: str = "DeepSeek V4.1 Flash"

ROLE_MODEL_BINDINGS: Mapping[str, str] = {
    ROLE_DEFAULT_PERCEIVER: MODEL_META_MUSE_SPARK,
    ROLE_ALTERNATIVE_PERCEIVER: MODEL_GEMINI_FLASH,
    ROLE_OUT_OF_LOOP_JUDGE: MODEL_GEMINI_ARGON,
}

PLANNED_PSLOT_EXPORTS: Mapping[str, str] = {
    "vadapt.pslot_cache": AUSENTE,
    "vadapt.pslot_registry": AUSENTE,
    "vadapt.pslot_transport": AUSENTE,
    "vadapt.pslot_judge": AUSENTE,
    "vadapt.pslot_perceiver": AUSENTE,
}


class PslotEncodeRefusal(Exception):
    """Base frozen refusal exception for PSLOT encoding and pre-dispatch validation."""

    def __init__(self, code: str, message: str = "", *, details: Optional[Mapping[str, Any]] = None) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("Refusal code must be a non-empty string")
        super().__init__(f"{code}: {message}" if message else code)
        self.code: str = code
        self.message: str = message
        self.details: Dict[str, Any] = dict(details) if details is not None else {}


class GeometryRefusalError(PslotEncodeRefusal):
    """Refusal raised when frame dimensions, buffer types, sizes, or resize rules are violated."""


class EncodingContractRefusalError(PslotEncodeRefusal):
    """Refusal raised when encoding mode, lossy opt-in, PNG structure, or codec rules are violated."""


class DispatchGateRefusalError(PslotEncodeRefusal):
    """Refusal raised before provider/transport access on missing producers, AUSENTE config, or quota floor."""


EncodingRefusalError = PslotEncodeRefusal


@dataclass(frozen=True)
class ModelCapabilityEntry:
    """Capability matrix entry for a network-first model role."""

    model_name: str
    role: Optional[str]
    configured: bool
    network_first: bool = True
    supports_png: Union[bool, str] = AUSENTE
    max_payload_bytes: Union[int, str] = AUSENTE
    quota_floor: Union[int, str] = AUSENTE
    tariff_per_unit: Union[float, int, str] = AUSENTE


@dataclass(frozen=True)
class OperatorModelConfig:
    """Explicit operator configuration required to populate AUSENTE capabilities, limits, and tariffs."""

    supports_png: Union[bool, str] = AUSENTE
    max_payload_bytes: Union[int, str] = AUSENTE
    quota_floor: Union[int, str] = AUSENTE
    tariff_per_unit: Union[float, int, str] = AUSENTE


DEFAULT_CAPABILITY_MATRIX: Mapping[str, ModelCapabilityEntry] = {
    MODEL_META_MUSE_SPARK: ModelCapabilityEntry(
        model_name=MODEL_META_MUSE_SPARK,
        role=ROLE_DEFAULT_PERCEIVER,
        configured=True,
        network_first=True,
        supports_png=AUSENTE,
        max_payload_bytes=AUSENTE,
        quota_floor=AUSENTE,
        tariff_per_unit=AUSENTE,
    ),
    MODEL_GEMINI_FLASH: ModelCapabilityEntry(
        model_name=MODEL_GEMINI_FLASH,
        role=ROLE_ALTERNATIVE_PERCEIVER,
        configured=True,
        network_first=True,
        supports_png=AUSENTE,
        max_payload_bytes=AUSENTE,
        quota_floor=AUSENTE,
        tariff_per_unit=AUSENTE,
    ),
    MODEL_GEMINI_ARGON: ModelCapabilityEntry(
        model_name=MODEL_GEMINI_ARGON,
        role=ROLE_OUT_OF_LOOP_JUDGE,
        configured=True,
        network_first=True,
        supports_png=AUSENTE,
        max_payload_bytes=AUSENTE,
        quota_floor=AUSENTE,
        tariff_per_unit=AUSENTE,
    ),
    MODEL_DEEPSEEK_FLASH: ModelCapabilityEntry(
        model_name=MODEL_DEEPSEEK_FLASH,
        role=None,
        configured=False,
        network_first=True,
        supports_png=AUSENTE,
        max_payload_bytes=AUSENTE,
        quota_floor=AUSENTE,
        tariff_per_unit=AUSENTE,
    ),
}


def configure_capability_matrix(
    overrides: Mapping[str, OperatorModelConfig],
    base_matrix: Optional[Mapping[str, ModelCapabilityEntry]] = None,
) -> Dict[str, ModelCapabilityEntry]:
    """Apply explicit operator configuration to the capability matrix without enabling DeepSeek V4.1 Flash."""
    matrix: Dict[str, ModelCapabilityEntry] = dict(base_matrix or DEFAULT_CAPABILITY_MATRIX)
    for model_name, cfg in overrides.items():
        if model_name == MODEL_DEEPSEEK_FLASH:
            raise DispatchGateRefusalError(
                "E_MODEL_NOT_CONFIGURED",
                "DeepSeek V4.1 Flash must remain not configured in the capability matrix",
                details={"model_name": model_name},
            )
        if model_name not in matrix:
            raise DispatchGateRefusalError(
                "E_MODEL_NOT_CONFIGURED",
                f"Unknown model {model_name!r} is not in the capability matrix",
                details={"model_name": model_name},
            )
        if not isinstance(cfg, OperatorModelConfig):
            raise DispatchGateRefusalError(
                "E_CONFIG_AUSENTE",
                "Operator configuration must be an OperatorModelConfig instance",
                details={"model_name": model_name},
            )
        prev = matrix[model_name]
        matrix[model_name] = ModelCapabilityEntry(
            model_name=prev.model_name,
            role=prev.role,
            configured=prev.configured,
            network_first=prev.network_first,
            supports_png=cfg.supports_png,
            max_payload_bytes=cfg.max_payload_bytes,
            quota_floor=cfg.quota_floor,
            tariff_per_unit=cfg.tariff_per_unit,
        )
    return matrix


@dataclass(frozen=True)
class LocalHttpDesignSeam:
    """Future design seam for a local HTTP interface with on-demand capped-unit hooks and zero evaluation dependency."""

    endpoint_hint: Optional[str] = None
    max_capped_units: Union[int, str] = AUSENTE
    on_demand_hook: Optional[Callable[[int], int]] = None
    evaluation_dependency: bool = False

    def invoke_capped_units(self, requested_units: int) -> int:
        if self.evaluation_dependency is not False:
            raise DispatchGateRefusalError(
                "E_LOCAL_EVAL_FORBIDDEN",
                "No evaluation may depend on a local model or local HTTP interface",
            )
        if self.max_capped_units == AUSENTE or type(self.max_capped_units) is not int or self.max_capped_units <= 0:
            raise DispatchGateRefusalError(
                "E_CONFIG_AUSENTE",
                "max_capped_units is AUSENTE or not a positive integer",
            )
        if type(requested_units) is not int or requested_units <= 0:
            raise DispatchGateRefusalError(
                "E_QUOTA_FLOOR_REFUSAL",
                "requested_units must be a positive integer",
            )
        if requested_units > self.max_capped_units:
            raise DispatchGateRefusalError(
                "E_QUOTA_FLOOR_REFUSAL",
                "requested_units exceeds max_capped_units",
                details={"requested_units": requested_units, "max_capped_units": self.max_capped_units},
            )
        if self.on_demand_hook is None or not callable(self.on_demand_hook):
            raise DispatchGateRefusalError(
                "E_PRODUCER_ABSENT",
                "on_demand_hook collaborator is AUSENTE",
            )
        granted = self.on_demand_hook(requested_units)
        if type(granted) is not int or granted < 0 or granted > self.max_capped_units:
            raise DispatchGateRefusalError(
                "E_QUOTA_FLOOR_REFUSAL",
                "on_demand_hook returned an invalid capped unit grant",
            )
        return granted


def inspect_pslot_export(module_name: str, symbol_name: Optional[str] = None) -> Any:
    """Lazily inspect an actual producer export after merge; returns AUSENTE if unmerged or missing."""
    if not isinstance(module_name, str) or not module_name.startswith("vadapt."):
        return AUSENTE
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        return AUSENTE
    if symbol_name is None:
        return mod
    return getattr(mod, symbol_name, AUSENTE)


def _is_exact_int(val: Any) -> bool:
    return type(val) is int


def _validate_dimensions(
    width: Any,
    height: Any,
    *,
    expected_width: Any = None,
    expected_height: Any = None,
    target_width: Any = None,
    target_height: Any = None,
    allow_resize: Any = False,
) -> Tuple[int, int]:
    if allow_resize is not False:
        raise GeometryRefusalError(
            "E_RESIZE_FORBIDDEN",
            "Implicit or explicit frame resizing is forbidden",
            details={"allow_resize": allow_resize},
        )
    if not _is_exact_int(width) or not _is_exact_int(height):
        raise GeometryRefusalError(
            "E_GEOMETRY_TYPE",
            "width and height must be exact integers",
            details={"width_type": type(width).__name__, "height_type": type(height).__name__},
        )
    if width <= 0 or height <= 0 or width > MAX_SIDE or height > MAX_SIDE:
        raise GeometryRefusalError(
            "E_GEOMETRY_BOUNDS",
            f"width and height must be in 1..{MAX_SIDE}, got {width}x{height}",
            details={"width": width, "height": height, "max_side": MAX_SIDE},
        )
    if expected_width is not None:
        if not _is_exact_int(expected_width) or expected_width <= 0 or expected_width > MAX_SIDE:
            raise GeometryRefusalError(
                "E_GEOMETRY_TYPE",
                "expected_width must be a valid positive integer within bounds",
                details={"expected_width": expected_width},
            )
        if width != expected_width:
            raise GeometryRefusalError(
                "E_GEOMETRY_MISMATCH",
                f"frame width {width} != expected_width {expected_width}",
                details={"width": width, "expected_width": expected_width},
            )
    if expected_height is not None:
        if not _is_exact_int(expected_height) or expected_height <= 0 or expected_height > MAX_SIDE:
            raise GeometryRefusalError(
                "E_GEOMETRY_TYPE",
                "expected_height must be a valid positive integer within bounds",
                details={"expected_height": expected_height},
            )
        if height != expected_height:
            raise GeometryRefusalError(
                "E_GEOMETRY_MISMATCH",
                f"frame height {height} != expected_height {expected_height}",
                details={"height": height, "expected_height": expected_height},
            )
    if target_width is not None or target_height is not None:
        if target_width != width or target_height != height:
            raise GeometryRefusalError(
                "E_RESIZE_FORBIDDEN",
                f"Resizing from {width}x{height} to {target_width}x{target_height} is forbidden",
                details={
                    "width": width,
                    "height": height,
                    "target_width": target_width,
                    "target_height": target_height,
                },
            )
    return width, height


@dataclass(frozen=True)
class ValidatedFrame:
    """In-memory validated canonical RGB24 frame data with precomputed raw digest."""

    rgb_bytes: bytes
    width: int
    height: int
    raw_frame_digest: str
    source_kind: str = "raw_rgb24"

    def __post_init__(self) -> None:
        _validate_dimensions(self.width, self.height)
        if not isinstance(self.rgb_bytes, bytes):
            raise GeometryRefusalError("E_RGB_BUFFER_TYPE", "ValidatedFrame.rgb_bytes must be immutable bytes")
        expected_len = self.width * self.height * 3
        if len(self.rgb_bytes) != expected_len:
            raise GeometryRefusalError(
                "E_RGB_BYTE_LENGTH",
                f"RGB24 byte length {len(self.rgb_bytes)} != {expected_len} ({self.width}*{self.height}*3)",
                details={"actual_bytes": len(self.rgb_bytes), "expected_bytes": expected_len},
            )
        actual_digest = hashlib.sha256(self.rgb_bytes).hexdigest()
        if not isinstance(self.raw_frame_digest, str) or self.raw_frame_digest != actual_digest:
            raise GeometryRefusalError(
                "E_DIGEST_MISMATCH",
                "ValidatedFrame.raw_frame_digest does not match canonical RGB24 SHA-256",
                details={"provided_digest": self.raw_frame_digest, "actual_digest": actual_digest},
            )


def accept_xwd_frame(
    xwd_buf: Union[bytes, bytearray, memoryview],
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    allow_resize: bool = False,
) -> ValidatedFrame:
    """Validate an in-memory XWD buffer via vadapt.xfb and extract canonical RGB24 bytes."""
    if allow_resize is not False:
        raise GeometryRefusalError("E_RESIZE_FORBIDDEN", "Frame resizing is forbidden")
    if not isinstance(xwd_buf, (bytes, bytearray, memoryview)):
        raise GeometryRefusalError("E_RGB_BUFFER_TYPE", "XWD input must be bytes, bytearray, or memoryview")
    if width is not None and (not _is_exact_int(width) or width <= 0 or width > MAX_SIDE):
        raise GeometryRefusalError("E_GEOMETRY_BOUNDS", f"Invalid expected width {width!r}")
    if height is not None and (not _is_exact_int(height) or height <= 0 or height > MAX_SIDE):
        raise GeometryRefusalError("E_GEOMETRY_BOUNDS", f"Invalid expected height {height!r}")
    try:
        hd = xfb.validate_frame(xwd_buf, width=width, height=height)
        rgb = xfb.rgb24(xwd_buf, hd)
    except xfb.XwdError as exc:
        raise GeometryRefusalError(
            "E_XWD_INVALID",
            f"XWD validation failed ({exc.code}): {exc}",
            details={"xwd_code": exc.code},
        ) from exc
    _validate_dimensions(
        hd.width,
        hd.height,
        expected_width=width,
        expected_height=height,
        target_width=target_width,
        target_height=target_height,
        allow_resize=allow_resize,
    )
    digest = hashlib.sha256(rgb).hexdigest()
    return ValidatedFrame(
        rgb_bytes=rgb,
        width=hd.width,
        height=hd.height,
        raw_frame_digest=digest,
        source_kind="xwd_buffer",
    )


def accept_validated_frame(
    frame_input: Any,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    allow_resize: bool = False,
    is_xwd: bool = False,
) -> ValidatedFrame:
    """Validate and normalize injected in-memory frame data without frame-reference or registry lookup."""
    if allow_resize is not False:
        raise GeometryRefusalError("E_RESIZE_FORBIDDEN", "Frame resizing is forbidden")
    if frame_input is None or (isinstance(frame_input, str) and frame_input == AUSENTE):
        raise DispatchGateRefusalError("E_PRODUCER_ABSENT", "Injected frame data is None or AUSENTE")
    if isinstance(frame_input, str):
        raise GeometryRefusalError(
            "E_FRAME_REF_UNSUPPORTED",
            "Frame references, paths, or registry keys are unsupported; pass validated in-memory frame data",
        )
    if isinstance(frame_input, ValidatedFrame):
        _validate_dimensions(
            frame_input.width,
            frame_input.height,
            expected_width=width,
            expected_height=height,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
        )
        return frame_input

    if is_xwd:
        return accept_xwd_frame(
            frame_input,
            width=width,
            height=height,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
        )

    if isinstance(frame_input, np.ndarray):
        if frame_input.dtype != np.uint8:
            raise GeometryRefusalError(
                "E_RGB_BUFFER_TYPE",
                f"numpy frame array must have dtype uint8, got {frame_input.dtype}",
            )
        if frame_input.ndim != 3 or frame_input.shape[2] != 3:
            raise GeometryRefusalError(
                "E_GEOMETRY_MISMATCH",
                f"numpy frame array must have shape (H, W, 3), got {frame_input.shape}",
            )
        arr_h, arr_w, _ = (int(frame_input.shape[0]), int(frame_input.shape[1]), int(frame_input.shape[2]))
        _validate_dimensions(
            arr_w,
            arr_h,
            expected_width=width,
            expected_height=height,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
        )
        rgb = np.ascontiguousarray(frame_input).tobytes()
        return ValidatedFrame(
            rgb_bytes=rgb,
            width=arr_w,
            height=arr_h,
            raw_frame_digest=hashlib.sha256(rgb).hexdigest(),
            source_kind="numpy_rgb24",
        )

    if isinstance(frame_input, Mapping):
        if "frame_ref" in frame_input or "registry_key" in frame_input:
            raise GeometryRefusalError(
                "E_FRAME_REF_UNSUPPORTED",
                "Frame reference/registry keys are unsupported; provide in-memory pixel data",
            )
        if "xwd_bytes" in frame_input:
            return accept_xwd_frame(
                frame_input["xwd_bytes"],
                width=width if width is not None else frame_input.get("width"),
                height=height if height is not None else frame_input.get("height"),
                target_width=target_width,
                target_height=target_height,
                allow_resize=allow_resize,
            )
        raw_buf = frame_input.get("rgb_bytes", frame_input.get("rgb"))
        in_w = frame_input.get("width", width)
        in_h = frame_input.get("height", height)
        if raw_buf is None:
            raise GeometryRefusalError("E_RGB_BUFFER_TYPE", "Injected mapping missing rgb_bytes")
        return accept_validated_frame(
            raw_buf,
            width=in_w,
            height=in_h,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
        )

    if hasattr(frame_input, "rgb_bytes") and hasattr(frame_input, "width") and hasattr(frame_input, "height"):
        in_w = getattr(frame_input, "width")
        in_h = getattr(frame_input, "height")
        if width is not None and in_w != width:
            raise GeometryRefusalError("E_GEOMETRY_MISMATCH", f"Frame object width {in_w} != expected {width}")
        if height is not None and in_h != height:
            raise GeometryRefusalError("E_GEOMETRY_MISMATCH", f"Frame object height {in_h} != expected {height}")
        return accept_validated_frame(
            getattr(frame_input, "rgb_bytes"),
            width=in_w,
            height=in_h,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
        )

    if isinstance(frame_input, (bytes, bytearray, memoryview)):
        raw_bytes = frame_input if isinstance(frame_input, bytes) else bytes(frame_input)
        if width is None and height is None and len(raw_bytes) >= xfb.MIN_HEADER:
            try:
                return accept_xwd_frame(
                    raw_bytes,
                    target_width=target_width,
                    target_height=target_height,
                    allow_resize=allow_resize,
                )
            except GeometryRefusalError:
                pass
        w, h = _validate_dimensions(
            width,
            height,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
        )
        expected_len = w * h * 3
        if len(raw_bytes) != expected_len:
            if len(raw_bytes) >= xfb.MIN_HEADER:
                try:
                    return accept_xwd_frame(
                        raw_bytes,
                        width=w,
                        height=h,
                        target_width=target_width,
                        target_height=target_height,
                        allow_resize=allow_resize,
                    )
                except GeometryRefusalError:
                    pass
            raise GeometryRefusalError(
                "E_RGB_BYTE_LENGTH",
                f"RGB24 byte length {len(raw_bytes)} != {expected_len} ({w}*{h}*3)",
                details={"actual_bytes": len(raw_bytes), "expected_bytes": expected_len, "width": w, "height": h},
            )
        return ValidatedFrame(
            rgb_bytes=raw_bytes,
            width=w,
            height=h,
            raw_frame_digest=hashlib.sha256(raw_bytes).hexdigest(),
            source_kind="raw_rgb24",
        )

    raise GeometryRefusalError(
        "E_RGB_BUFFER_TYPE",
        f"Unsupported frame_input type: {type(frame_input).__name__}",
    )


