"""pslot_encode: pslot encode dispatch."""
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
from ._pslot_png import (_pack_png_chunk, encode_png_rgb24, _paeth_predictor, decode_and_verify_png)

@dataclass(frozen=True)
class EncodingReceipt:
    """Metadata-only receipt for an in-memory frame encoding operation (no media payload bytes)."""

    input_digest: str
    raw_frame_digest: str
    encoded_payload_digest: str
    encoding_mode: str
    mime_type: str
    width: int
    height: int
    raw_byte_length: int
    encoded_byte_length: int
    lossy_opt_in: bool
    lossless: bool
    codec_name: str
    chunk_crcs: Tuple[Tuple[str, int], ...] = field(default_factory=tuple)
    runtime_environment: str = RUNTIME_LABEL_VM
    host_measurement: str = RUNTIME_LABEL_HOST

    def __post_init__(self) -> None:
        if not isinstance(self.input_digest, str) or len(self.input_digest) != 64:
            raise EncodingContractRefusalError("E_DIGEST_INVALID", "input_digest must be a 64-char hex SHA-256")
        if not isinstance(self.raw_frame_digest, str) or len(self.raw_frame_digest) != 64:
            raise EncodingContractRefusalError("E_DIGEST_INVALID", "raw_frame_digest must be a 64-char hex SHA-256")
        if self.input_digest != self.raw_frame_digest:
            raise EncodingContractRefusalError("E_DIGEST_INVALID", "input_digest must equal raw_frame_digest")
        if not isinstance(self.encoded_payload_digest, str) or len(self.encoded_payload_digest) != 64:
            raise EncodingContractRefusalError(
                "E_DIGEST_INVALID",
                "encoded_payload_digest must be a 64-char hex SHA-256",
            )
        if self.raw_frame_digest == self.encoded_payload_digest:
            raise EncodingContractRefusalError(
                "E_DIGEST_COLLISION",
                "raw_frame_digest must be distinct from encoded_payload_digest",
            )
        if type(self.lossy_opt_in) is not bool or type(self.lossless) is not bool:
            raise EncodingContractRefusalError("E_LOSSY_OPT_IN_REQUIRED", "lossy_opt_in and lossless must be exact bools")
        if self.lossy_opt_in == self.lossless:
            raise EncodingContractRefusalError(
                "E_ENCODING_UNSUPPORTED",
                "lossy_opt_in and lossless must be mutually exclusive",
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_digest": self.input_digest,
            "raw_frame_digest": self.raw_frame_digest,
            "encoded_payload_digest": self.encoded_payload_digest,
            "encoding_mode": self.encoding_mode,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "raw_byte_length": self.raw_byte_length,
            "encoded_byte_length": self.encoded_byte_length,
            "lossy_opt_in": self.lossy_opt_in,
            "lossless": self.lossless,
            "codec_name": self.codec_name,
            "chunk_crcs": [[name, crc] for name, crc in self.chunk_crcs],
            "runtime_environment": self.runtime_environment,
            "host_measurement": self.host_measurement,
        }


def assert_receipt_contains_no_media(receipt: EncodingReceipt, raw_rgb: bytes, encoded_payload: bytes) -> None:
    """Verify that no raw or encoded media payload bytes appear in an EncodingReceipt."""
    if not isinstance(receipt, EncodingReceipt):
        raise EncodingContractRefusalError("E_RECEIPT_INVALID", "Expected EncodingReceipt instance")
    as_dict = receipt.to_dict()
    for key, value in as_dict.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise EncodingContractRefusalError(
                "E_MEDIA_IN_RECEIPT",
                f"Receipt field {key!r} contains raw byte buffer",
            )
        if isinstance(value, str) and (
            (len(raw_rgb) >= 8 and raw_rgb.hex() in value)
            or (len(encoded_payload) >= 8 and encoded_payload.hex() in value)
        ):
            raise EncodingContractRefusalError(
                "E_MEDIA_IN_RECEIPT",
                f"Receipt field {key!r} leaks hex-encoded media payload",
            )


@dataclass(frozen=True)
class EncodedFrame:
    """In-memory encoded frame pairing the RAM payload bytes with its metadata-only receipt."""

    payload: bytes
    receipt: EncodingReceipt

    @property
    def width(self) -> int:
        return self.receipt.width

    @property
    def height(self) -> int:
        return self.receipt.height

    @property
    def raw_frame_digest(self) -> str:
        return self.receipt.raw_frame_digest

    @property
    def input_digest(self) -> str:
        return self.receipt.input_digest

    @property
    def encoded_payload_digest(self) -> str:
        return self.receipt.encoded_payload_digest

    @property
    def encoding_mode(self) -> str:
        return self.receipt.encoding_mode

    @property
    def lossy_opt_in(self) -> bool:
        return self.receipt.lossy_opt_in


def encode_frame(
    frame_input: Any,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    encoding_mode: str = MODE_LOSSLESS_PNG,
    lossy_opt_in: bool = False,
    lossy_codec: Any = None,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    allow_resize: bool = False,
    compress_level: int = 6,
    is_xwd: bool = False,
) -> EncodedFrame:
    """Validate and encode a frame in memory (lossless PNG by default; explicit per-call opt-in for lossy)."""
    validated = accept_validated_frame(
        frame_input,
        width=width,
        height=height,
        target_width=target_width,
        target_height=target_height,
        allow_resize=allow_resize,
        is_xwd=is_xwd,
    )

    if type(lossy_opt_in) is not bool:
        raise EncodingContractRefusalError(
            "E_LOSSY_OPT_IN_REQUIRED",
            "lossy_opt_in must be an explicit bool (True or False)",
            details={"lossy_opt_in": lossy_opt_in},
        )
    if not isinstance(encoding_mode, str) or not encoding_mode:
        raise EncodingContractRefusalError(
            "E_ENCODING_UNSUPPORTED",
            "encoding_mode must be a non-empty string",
        )

    normalized_mode = encoding_mode.strip().lower()

    if normalized_mode in (MODE_LOSSLESS_PNG, "png"):
        if lossy_codec is not None:
            raise EncodingContractRefusalError(
                "E_ENCODING_UNSUPPORTED",
                "lossy_codec cannot be supplied when encoding_mode is lossless PNG",
            )
        if lossy_opt_in is True:
            raise EncodingContractRefusalError(
                "E_LOSSY_OPT_IN_REQUIRED",
                "lossy_opt_in=True is invalid for lossless PNG encoding",
            )
        png_bytes, crcs = encode_png_rgb24(
            validated.rgb_bytes,
            validated.width,
            validated.height,
            compress_level=compress_level,
        )
        encoded_digest = hashlib.sha256(png_bytes).hexdigest()
        receipt = EncodingReceipt(
            input_digest=validated.raw_frame_digest,
            raw_frame_digest=validated.raw_frame_digest,
            encoded_payload_digest=encoded_digest,
            encoding_mode=MODE_LOSSLESS_PNG,
            mime_type=MIME_PNG,
            width=validated.width,
            height=validated.height,
            raw_byte_length=len(validated.rgb_bytes),
            encoded_byte_length=len(png_bytes),
            lossy_opt_in=False,
            lossless=True,
            codec_name=CODEC_STDLIB_PNG,
            chunk_crcs=crcs,
        )
        assert_receipt_contains_no_media(receipt, validated.rgb_bytes, png_bytes)
        return EncodedFrame(payload=png_bytes, receipt=receipt)

    is_jpeg_token = normalized_mode in ("jpeg", "jpg", "image/jpeg", "lossy_jpeg", "lossy:jpeg")
    is_lossy_token = is_jpeg_token or normalized_mode in ("lossy", "webp", "lossy_webp") or normalized_mode.startswith("lossy:")

    if not is_lossy_token:
        raise EncodingContractRefusalError(
            "E_ENCODING_UNSUPPORTED",
            f"Unsupported encoding_mode {encoding_mode!r}",
            details={"encoding_mode": encoding_mode},
        )

    if lossy_codec is None:
        if is_jpeg_token:
            raise EncodingContractRefusalError(
                "E_IMPLICIT_JPEG_FORBIDDEN",
                "Implicit JPEG encoding is forbidden; inject a lossy codec and set lossy_opt_in=True",
                details={"encoding_mode": encoding_mode},
            )
        raise EncodingContractRefusalError(
            "E_ENCODING_UNSUPPORTED",
            "Lossy encoding requires an explicit injected lossy_codec collaborator",
            details={"encoding_mode": encoding_mode},
        )

    if lossy_opt_in is not True:
        raise EncodingContractRefusalError(
            "E_LOSSY_OPT_IN_REQUIRED",
            "An explicit per-call lossy_opt_in=True is required for any injected lossy codec",
            details={"encoding_mode": encoding_mode, "lossy_opt_in": lossy_opt_in},
        )

    encode_fn = getattr(lossy_codec, "encode", None)
    if encode_fn is None or not callable(encode_fn):
        raise EncodingContractRefusalError(
            "E_CODEC_INVALID",
            "Injected lossy_codec must provide a callable encode(rgb_bytes, width, height) method",
        )

    try:
        codec_out = encode_fn(validated.rgb_bytes, validated.width, validated.height)
    except PslotEncodeRefusal:
        raise
    except Exception as exc:
        raise EncodingContractRefusalError(
            "E_CODEC_FAILURE",
            f"Injected lossy codec failed without fallback: {exc}",
        ) from exc

    out_w = validated.width
    out_h = validated.height
    if isinstance(codec_out, tuple):
        if len(codec_out) != 3:
            raise EncodingContractRefusalError(
                "E_CODEC_INVALID",
                "Lossy codec tuple return must be (payload_bytes, width, height)",
            )
        payload_raw, out_w, out_h = codec_out
    else:
        payload_raw = codec_out

    if out_w != validated.width or out_h != validated.height:
        raise GeometryRefusalError(
            "E_RESIZE_FORBIDDEN",
            f"Injected lossy codec altered geometry from {validated.width}x{validated.height} to {out_w}x{out_h}",
            details={
                "input_width": validated.width,
                "input_height": validated.height,
                "output_width": out_w,
                "output_height": out_h,
            },
        )

    if not isinstance(payload_raw, (bytes, bytearray, memoryview)):
        raise EncodingContractRefusalError(
            "E_CODEC_INVALID",
            "Injected lossy codec must return bytes",
        )
    payload_bytes = payload_raw if isinstance(payload_raw, bytes) else bytes(payload_raw)
    if len(payload_bytes) == 0:
        raise EncodingContractRefusalError(
            "E_CODEC_INVALID",
            "Injected lossy codec returned an empty payload",
        )
    if payload_bytes[:8] == PNG_SIGNATURE:
        raise EncodingContractRefusalError(
            "E_CODEC_INVALID",
            "Silent PNG fallback inside a lossy codec is forbidden",
        )

    codec_name = str(getattr(lossy_codec, "name", "injected_lossy_codec"))
    mime_type = str(getattr(lossy_codec, "mime_type", "application/octet-stream"))
    resolved_mode = (
        normalized_mode if normalized_mode.startswith("lossy:") else f"lossy:{codec_name}"
    )
    encoded_digest = hashlib.sha256(payload_bytes).hexdigest()
    if encoded_digest == validated.raw_frame_digest:
        raise EncodingContractRefusalError(
            "E_DIGEST_COLLISION",
            "Encoded payload digest must be distinct from raw frame digest",
        )

    receipt = EncodingReceipt(
        input_digest=validated.raw_frame_digest,
        raw_frame_digest=validated.raw_frame_digest,
        encoded_payload_digest=encoded_digest,
        encoding_mode=resolved_mode,
        mime_type=mime_type,
        width=validated.width,
        height=validated.height,
        raw_byte_length=len(validated.rgb_bytes),
        encoded_byte_length=len(payload_bytes),
        lossy_opt_in=True,
        lossless=False,
        codec_name=codec_name,
        chunk_crcs=(),
    )
    assert_receipt_contains_no_media(receipt, validated.rgb_bytes, payload_bytes)
    return EncodedFrame(payload=payload_bytes, receipt=receipt)


@dataclass(frozen=True)
class DispatchOutcome:
    """Outcome of dispatching an in-memory encoded frame through injected transport and provider."""

    role: str
    model_name: str
    receipt: EncodingReceipt
    transport_receipt: Mapping[str, Any]
    provider_response: Mapping[str, Any]
    vm_elapsed_ns: int
    runtime_environment: str = RUNTIME_LABEL_VM
    host_measurement: str = RUNTIME_LABEL_HOST


def dispatch_encoded_frame(
    frame_input: Any,
    *,
    role: str = ROLE_DEFAULT_PERCEIVER,
    model_name: Optional[str] = None,
    provider: Any = None,
    transport: Any = None,
    capability_matrix: Optional[Mapping[str, ModelCapabilityEntry]] = None,
    remaining_quota: Any = AUSENTE,
    evidence: Any = "verified",
    width: Optional[int] = None,
    height: Optional[int] = None,
    encoding_mode: str = MODE_LOSSLESS_PNG,
    lossy_opt_in: bool = False,
    lossy_codec: Any = None,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    allow_resize: bool = False,
    is_xwd: bool = False,
) -> DispatchOutcome:
    """Validate geometry, encoding, role/capability config, and quota floor before provider/transport access."""
    t0 = time.perf_counter_ns()

    # Step 1 & 2: Validate frame geometry and encoding contract BEFORE any provider or transport access.
    encoded = encode_frame(
        frame_input,
        width=width,
        height=height,
        encoding_mode=encoding_mode,
        lossy_opt_in=lossy_opt_in,
        lossy_codec=lossy_codec,
        target_width=target_width,
        target_height=target_height,
        allow_resize=allow_resize,
        is_xwd=is_xwd,
    )

    # Step 3: Validate producers and evidence presence.
    if provider is None or provider == AUSENTE or transport is None or transport == AUSENTE:
        raise DispatchGateRefusalError(
            "E_PRODUCER_ABSENT",
            "Missing provider or transport collaborator blocks dispatch",
        )
    if evidence is None or evidence == AUSENTE or evidence == "":
        raise DispatchGateRefusalError(
            "E_PRODUCER_ABSENT",
            "Missing or AUSENTE producer evidence blocks dispatch",
        )
    if not callable(getattr(transport, "send", None)) or not callable(getattr(provider, "evaluate", None)):
        raise DispatchGateRefusalError(
            "E_PRODUCER_ABSENT",
            "Injected transport must expose send(...) and provider must expose evaluate(...)",
        )

    # Step 4: Resolve network-first role and capability matrix entry.
    matrix = capability_matrix if capability_matrix is not None else DEFAULT_CAPABILITY_MATRIX
    resolved_model = model_name if model_name is not None else ROLE_MODEL_BINDINGS.get(role)
    if resolved_model is None:
        raise DispatchGateRefusalError(
            "E_MODEL_NOT_CONFIGURED",
            f"Unknown role {role!r} has no bound model",
            details={"role": role},
        )
    if resolved_model not in matrix:
        raise DispatchGateRefusalError(
            "E_MODEL_NOT_CONFIGURED",
            f"Model {resolved_model!r} is absent from capability matrix",
            details={"model_name": resolved_model},
        )

    entry = matrix[resolved_model]
    if not entry.configured or resolved_model == MODEL_DEEPSEEK_FLASH or not entry.network_first:
        raise DispatchGateRefusalError(
            "E_MODEL_NOT_CONFIGURED",
            f"Model {resolved_model!r} is not configured for evaluation",
            details={"model_name": resolved_model, "configured": entry.configured},
        )
    if entry.role != role:
        raise DispatchGateRefusalError(
            "E_MODEL_NOT_CONFIGURED",
            f"Model {resolved_model!r} is bound to role {entry.role!r}, not {role!r}",
            details={"model_name": resolved_model, "expected_role": entry.role, "requested_role": role},
        )

    # Step 5: Unknown capabilities, limits, and tariffs are AUSENTE; absence is never zero.
    if (
        entry.supports_png == AUSENTE
        or entry.max_payload_bytes == AUSENTE
        or entry.quota_floor == AUSENTE
        or entry.tariff_per_unit == AUSENTE
    ):
        raise DispatchGateRefusalError(
            "E_CONFIG_AUSENTE",
            f"Model {resolved_model!r} has AUSENTE capability, limit, quota_floor, or tariff; explicit operator config required",
            details={"model_name": resolved_model},
        )
    if entry.supports_png is not True:
        raise DispatchGateRefusalError(
            "E_CONFIG_AUSENTE",
            f"Model {resolved_model!r} does not have supports_png=True configured",
        )
    if not _is_exact_int(entry.max_payload_bytes) or entry.max_payload_bytes <= 0:
        raise DispatchGateRefusalError(
            "E_CONFIG_AUSENTE",
            "max_payload_bytes must be a positive exact integer (absence is never zero)",
        )
    if not _is_exact_int(entry.quota_floor) or entry.quota_floor <= 0:
        raise DispatchGateRefusalError(
            "E_CONFIG_AUSENTE",
            "quota_floor must be a positive exact integer (absence is never zero)",
        )
    if isinstance(entry.tariff_per_unit, bool) or not isinstance(entry.tariff_per_unit, (int, float)) or entry.tariff_per_unit < 0:
        raise DispatchGateRefusalError(
            "E_CONFIG_AUSENTE",
            "tariff_per_unit must be a non-negative number",
        )

    # Step 6: Quota-floor refusal before transport (especially for Gemini 4 Argon out-of-loop judge).
    if remaining_quota == AUSENTE or not _is_exact_int(remaining_quota):
        raise DispatchGateRefusalError(
            "E_QUOTA_FLOOR_REFUSAL",
            "remaining_quota is AUSENTE or not an exact integer before transport",
            details={"remaining_quota": remaining_quota, "quota_floor": entry.quota_floor},
        )
    if remaining_quota < entry.quota_floor:
        raise DispatchGateRefusalError(
            "E_QUOTA_FLOOR_REFUSAL",
            f"remaining_quota {remaining_quota} is below quota_floor {entry.quota_floor} for {resolved_model}",
            details={"remaining_quota": remaining_quota, "quota_floor": entry.quota_floor, "model_name": resolved_model},
        )

    # Step 7: Payload byte cap check before transport.
    if len(encoded.payload) > entry.max_payload_bytes:
        raise DispatchGateRefusalError(
            "E_PAYLOAD_LIMIT_EXCEEDED",
            f"Encoded payload ({len(encoded.payload)} bytes) exceeds max_payload_bytes ({entry.max_payload_bytes})",
            details={"encoded_bytes": len(encoded.payload), "max_payload_bytes": entry.max_payload_bytes},
        )

    transport_receipt = transport.send(
        payload=encoded.payload,
        receipt=encoded.receipt,
        role=role,
        model_name=resolved_model,
    )
    provider_response = provider.evaluate(
        receipt=encoded.receipt,
        transport_receipt=transport_receipt,
        role=role,
        model_name=resolved_model,
    )
    elapsed_ns = max(1, time.perf_counter_ns() - t0)
    return DispatchOutcome(
        role=role,
        model_name=resolved_model,
        receipt=encoded.receipt,
        transport_receipt=dict(transport_receipt),
        provider_response=dict(provider_response),
        vm_elapsed_ns=elapsed_ns,
        runtime_environment=RUNTIME_LABEL_VM,
        host_measurement=RUNTIME_LABEL_HOST,
    )


class PslotEncoder:
    """Thread-safe stateless/collaborator-injected encoder façade for PSLOT wave 1."""

    def __init__(
        self,
        *,
        capability_matrix: Optional[Mapping[str, ModelCapabilityEntry]] = None,
        local_http_seam: Optional[LocalHttpDesignSeam] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._capability_matrix: Dict[str, ModelCapabilityEntry] = dict(
            capability_matrix or DEFAULT_CAPABILITY_MATRIX
        )
        self._local_http_seam = local_http_seam

    @property
    def capability_matrix(self) -> Dict[str, ModelCapabilityEntry]:
        with self._lock:
            return dict(self._capability_matrix)

    @property
    def local_http_seam(self) -> Optional[LocalHttpDesignSeam]:
        return self._local_http_seam

    def encode(
        self,
        frame_input: Any,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        encoding_mode: str = MODE_LOSSLESS_PNG,
        lossy_opt_in: bool = False,
        lossy_codec: Any = None,
        target_width: Optional[int] = None,
        target_height: Optional[int] = None,
        allow_resize: bool = False,
        compress_level: int = 6,
        is_xwd: bool = False,
    ) -> EncodedFrame:
        """Encode a single frame in memory; lossy_opt_in is strictly per-call and never persisted."""
        return encode_frame(
            frame_input,
            width=width,
            height=height,
            encoding_mode=encoding_mode,
            lossy_opt_in=lossy_opt_in,
            lossy_codec=lossy_codec,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
            compress_level=compress_level,
            is_xwd=is_xwd,
        )

    def dispatch(
        self,
        frame_input: Any,
        *,
        role: str = ROLE_DEFAULT_PERCEIVER,
        model_name: Optional[str] = None,
        provider: Any = None,
        transport: Any = None,
        remaining_quota: Any = AUSENTE,
        evidence: Any = "verified",
        width: Optional[int] = None,
        height: Optional[int] = None,
        encoding_mode: str = MODE_LOSSLESS_PNG,
        lossy_opt_in: bool = False,
        lossy_codec: Any = None,
        target_width: Optional[int] = None,
        target_height: Optional[int] = None,
        allow_resize: bool = False,
        is_xwd: bool = False,
    ) -> DispatchOutcome:
        return dispatch_encoded_frame(
            frame_input,
            role=role,
            model_name=model_name,
            provider=provider,
            transport=transport,
            capability_matrix=self.capability_matrix,
            remaining_quota=remaining_quota,
            evidence=evidence,
            width=width,
            height=height,
            encoding_mode=encoding_mode,
            lossy_opt_in=lossy_opt_in,
            lossy_codec=lossy_codec,
            target_width=target_width,
            target_height=target_height,
            allow_resize=allow_resize,
            is_xwd=is_xwd,
        )


__all__ = [
    "AUSENTE",
    "CODEC_STDLIB_PNG",
    "DEFAULT_CAPABILITY_MATRIX",
    "DispatchGateRefusalError",
    "DispatchOutcome",
    "EncodedFrame",
    "EncodingContractRefusalError",
    "EncodingReceipt",
    "EncodingRefusalError",
    "GeometryRefusalError",
    "LocalHttpDesignSeam",
    "MAX_RGB_BYTES",
    "MAX_SIDE",
    "MIME_PNG",
    "MODEL_DEEPSEEK_FLASH",
    "MODEL_GEMINI_ARGON",
    "MODEL_GEMINI_FLASH",
    "MODEL_META_MUSE_SPARK",
    "MODE_LOSSLESS_PNG",
    "ModelCapabilityEntry",
    "OperatorModelConfig",
    "PLANNED_PSLOT_EXPORTS",
    "PNG_BIT_DEPTH_8",
    "PNG_COLOR_TYPE_RGB",
    "PNG_SIGNATURE",
    "PslotEncodeRefusal",
    "PslotEncoder",
    "ROLE_ALTERNATIVE_PERCEIVER",
    "ROLE_DEFAULT_PERCEIVER",
    "ROLE_MODEL_BINDINGS",
    "ROLE_OUT_OF_LOOP_JUDGE",
    "RUNTIME_LABEL_HOST",
    "RUNTIME_LABEL_VM",
    "ValidatedFrame",
    "accept_validated_frame",
    "accept_xwd_frame",
    "assert_receipt_contains_no_media",
    "configure_capability_matrix",
    "decode_and_verify_png",
    "dispatch_encoded_frame",
    "encode_frame",
    "encode_png_rgb24",
    "inspect_pslot_export",
]
