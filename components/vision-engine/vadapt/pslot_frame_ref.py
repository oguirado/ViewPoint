"""Compatibility exports for :mod:`vadapt.pslot_frame_ref`.

Implementation is split into contracts and cohesive operations; public names are stable.
"""
from __future__ import annotations
import hashlib
from typing import Any, Mapping, Optional, Sequence, Tuple, Union
import numpy as np
from vadapt import xfb

from ._pslot_frame_geometry import (
    AUSENTE, RGB_CHANNELS, MAX_SIDE, MAX_FRAME_PIXELS, MAX_RGB_BYTES, E_FRAME_NULL,
    E_FRAME_TYPE, E_FRAME_DTYPE, E_FRAME_CONTIGUOUS, E_FRAME_GEOMETRY, E_ROI_TYPE, E_ROI_EMPTY,
    E_ROI_BOUNDS, E_COORD_BOUNDS, E_IMMUTABLE, FrameRefError, PslotFrameRefError,
    FrameFormatError, FrameGeometryError, RoiValidationError, RoiBoundsError,
    FrameImmutabilityError, _require_exact_int, _validate_dimensions, RoiBox, FrameProvenance,
    _admit_pixels,
)
from ._pslot_frame_buffer import (
    FrameRef, RamFrameRef, admit_frame, admit_xwd_frame, validate_roi, compute_rgb24_sha256,
    __all__,
)
