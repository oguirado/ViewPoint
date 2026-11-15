"""Compatibility exports for :mod:`vadapt.w8u_digest`.

Implementation is split into contracts and cohesive operations; public names are stable.
"""
from __future__ import annotations
import hashlib
import json
import os
import posixpath
import re
import struct
from collections import namedtuple
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from vadapt import xfb

from ._digest_pixels import (
    AUSENTE, STATUS_PREPARED, STATUS_ACCEPTED, RECEIPT_SCHEMA_V1, REQUIRED_FRAMES_ROOT,
    REQUIRED_RUN_CHILD_PREFIX, W8UF_MAGIC, W8UF_VERSION, W8UF_HEADER_BYTES,
    W8UF_PIXEL_FORMAT_RGB24, W8UF_FLAG_PAIR_UNDER_GRAB, W8UF_FLAG_TICK_VALID,
    W8UF_ALLOWED_FLAGS_MASK, W8UF_HEADER_STRUCT, W8UF_INNER_LENGTH_BYTES,
    W8UF_GEOMETRY_SLACK_BYTES, W8UC_MAGIC, W8UH_MAGIC, W8UE_MAGIC, CONTROL_HEADER_STRUCT,
    CONTROL_HEADER_BYTES, HELLO_MAX_BODY_BYTES, CONTROL_KIND_HELLO, CONTROL_KIND_ACK,
    CONTROL_KIND_STOP, PREFERRED_INITIAL_CREDIT_GRANT, MAX_U64, MAX_U32, CLOSED_ERRORS,
    NATIVE_TO_W8U_ERROR_MAP, BINDING_KEYS, TARGET_HANDLE_KEYS, FRAME_METADATA_KEYS,
    FORBIDDEN_METADATA_SUBSTRINGS, FORBIDDEN_METADATA_KEY_TOKENS, HEX64_RE, W8UFFrameHeader,
    ValidatedW8UFrame, ControlFrame, W8UDigestError, _is_exact_int, _is_exact_bool, _is_hex64,
    _ensure_bytes_like, ContractEvidence, require_contract_evidence, numeric_error_code,
    translate_native_xwd_error, _extract_xwd_bounds, validate_w8u_xwd, canonical_rgb24_bytes,
    rgb24_sha256_raw, rgb24_sha256_hex, validate_raw_rgb24_buffer, rgba8_from_rgb24,
    rgba8_from_xwd, compare_rgba8_engine_buffer, _resolve_pixel_source, _normalize_digest_bytes,
)
from ._digest_binding import (
    build_synthetic_xwd, build_bgrx_xwd, build_xrgb_xwd, _validate_canonical_json_value,
    canonical_json_bytes, _validate_confined_roots, validate_binding, domain_sha256_raw,
    domain_sha256_hex, compute_geometry_record_cap, compute_effective_record_cap,
)
from ._digest_records import (
    parse_w8uf_header, build_w8uf_record, validate_w8uf_record,
    project_frame_metadata_from_header, _check_no_forbidden_pixel_or_encoding_tricks,
    validate_frame_metadata, canonicalize_frame_metadata, hash_frame_metadata_raw,
    hash_frame_metadata_hex,
)
from ._digest_control import (
    project_u5_vision_receipt, parse_control_frame, CreditWindowController,
)
