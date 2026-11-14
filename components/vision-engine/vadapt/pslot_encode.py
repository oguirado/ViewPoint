"""Compatibility exports for :mod:`vadapt.pslot_encode`.

Implementation is split into contracts and cohesive operations; public names are stable.
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
from ._pslot_encode_dispatch import (
    EncodingReceipt, assert_receipt_contains_no_media, EncodedFrame, encode_frame,
    DispatchOutcome, dispatch_encoded_frame, PslotEncoder, __all__,
)
