"""vadapt.pslot_frame_ref - Immutable exact RAM frame-reference API (PSLOT wave 1).

Carries canonical RGB24 SHA-256 digests, validated dimensions, and validated integer
ROI crops from C-contiguous uint8 RGB buffers in RAM. Caller pixels are copied and
locked on admission so subsequent caller mutations cannot alter evidence. Preserves
full-frame and parent crop digest provenance and exact coordinate transforms across
arbitrary crop chains. Does not own provider transport, PNG encoding, or registry.
"""
from __future__ import annotations
import hashlib
from typing import Any, Mapping, Optional, Sequence, Tuple, Union
import numpy as np
from vadapt import xfb

AUSENTE = "AUSENTE"

RGB_CHANNELS = 3
MAX_SIDE = xfb.MAX_SIDE
MAX_FRAME_PIXELS = MAX_SIDE * MAX_SIDE
MAX_RGB_BYTES = MAX_FRAME_PIXELS * RGB_CHANNELS

E_FRAME_NULL = "E_FRAME_NULL"
E_FRAME_TYPE = "E_FRAME_TYPE"
E_FRAME_DTYPE = "E_FRAME_DTYPE"
E_FRAME_CONTIGUOUS = "E_FRAME_CONTIGUOUS"
E_FRAME_GEOMETRY = "E_FRAME_GEOMETRY"
E_ROI_TYPE = "E_ROI_TYPE"
E_ROI_EMPTY = "E_ROI_EMPTY"
E_ROI_BOUNDS = "E_ROI_BOUNDS"
E_COORD_BOUNDS = "E_COORD_BOUNDS"
E_IMMUTABLE = "E_IMMUTABLE"


class FrameRefError(Exception):
    """Base typed refusal exception for PSLOT RAM frame references (``code``)."""

    def __init__(self, code: str, msg: str = "") -> None:
        text = f"{code} {msg}".strip() if msg else str(code)
        super().__init__(text)
        self.code = code
        self.msg = msg


PslotFrameRefError = FrameRefError


class FrameFormatError(FrameRefError):
    """Raised when frame input container, dtype, or memory contiguity is invalid."""


class FrameGeometryError(FrameRefError):
    """Raised when frame dimensions, channel count, or byte length are invalid or empty."""


class RoiValidationError(FrameRefError):
    """Base refusal exception for invalid ROI specifications or coordinate transforms."""


class RoiBoundsError(RoiValidationError):
    """Raised on bool/non-int ROI values, empty ROI geometry, or out-of-bounds coordinates."""


class FrameImmutabilityError(FrameRefError, TypeError):
    """Raised when attempting to mutate an immutable FrameRef, RoiBox, or FrameProvenance."""


def _require_exact_int(value: Any, field_name: str, *, err_cls: type = RoiBoundsError, code: str = E_ROI_TYPE) -> int:
    if isinstance(value, bool) or isinstance(value, np.bool_):
        raise err_cls(code, f"{field_name} must be an exact int, got bool ({value!r})")
    if type(value) is not int:
        raise err_cls(code, f"{field_name} must be an exact int, got {type(value).__name__}")
    return value


def _validate_dimensions(width: Any, height: Any) -> Tuple[int, int]:
    w = _require_exact_int(width, "width", err_cls=FrameGeometryError, code=E_FRAME_GEOMETRY)
    h = _require_exact_int(height, "height", err_cls=FrameGeometryError, code=E_FRAME_GEOMETRY)
    if w <= 0 or h <= 0:
        raise FrameGeometryError(E_FRAME_GEOMETRY, f"frame dimensions must be positive, got {w}x{h}")
    if w > MAX_SIDE or h > MAX_SIDE:
        raise FrameGeometryError(
            E_FRAME_GEOMETRY,
            f"frame dimensions {w}x{h} exceed MAX_SIDE ({MAX_SIDE})",
        )
    return w, h


class RoiBox:
    """Immutable validated integer ROI rectangle in pixel coordinates (x, y, width, height)."""

    __slots__ = ("_x", "_y", "_width", "_height")

    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        ix = _require_exact_int(x, "roi.x", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        iy = _require_exact_int(y, "roi.y", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        iw = _require_exact_int(width, "roi.width", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        ih = _require_exact_int(height, "roi.height", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        if ix < 0 or iy < 0:
            raise RoiBoundsError(E_ROI_BOUNDS, f"ROI origin must be non-negative, got ({ix}, {iy})")
        if iw <= 0 or ih <= 0:
            raise RoiBoundsError(E_ROI_EMPTY, f"ROI geometry must be non-empty, got {iw}x{ih}")
        if iw > MAX_SIDE or ih > MAX_SIDE:
            raise RoiBoundsError(E_ROI_BOUNDS, f"ROI dimensions {iw}x{ih} exceed MAX_SIDE ({MAX_SIDE})")
        object.__setattr__(self, "_x", ix)
        object.__setattr__(self, "_y", iy)
        object.__setattr__(self, "_width", iw)
        object.__setattr__(self, "_height", ih)

    def __setattr__(self, name: str, value: Any) -> None:
        raise FrameImmutabilityError(E_IMMUTABLE, "RoiBox is immutable")

    def __delattr__(self, name: str) -> None:
        raise FrameImmutabilityError(E_IMMUTABLE, "RoiBox is immutable")

    @classmethod
    def from_xywh(cls, x: int, y: int, width: int, height: int) -> "RoiBox":
        return cls(x, y, width, height)

    @classmethod
    def from_xyxy(cls, x0: int, y0: int, x1: int, y1: int) -> "RoiBox":
        ix0 = _require_exact_int(x0, "roi.x0", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        iy0 = _require_exact_int(y0, "roi.y0", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        ix1 = _require_exact_int(x1, "roi.x1", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        iy1 = _require_exact_int(y1, "roi.y1", err_cls=RoiBoundsError, code=E_ROI_TYPE)
        if ix0 < 0 or iy0 < 0 or ix1 < 0 or iy1 < 0:
            raise RoiBoundsError(
                E_ROI_BOUNDS,
                f"ROI xyxy coordinates must be non-negative, got ({ix0}, {iy0}, {ix1}, {iy1})",
            )
        w = ix1 - ix0
        h = iy1 - iy0
        if w <= 0 or h <= 0:
            raise RoiBoundsError(
                E_ROI_EMPTY,
                f"ROI xyxy geometry must have x1 > x0 and y1 > y0, got ({ix0}, {iy0}, {ix1}, {iy1})",
            )
        return cls(ix0, iy0, w, h)

    @classmethod
    def coerce(
        cls,
        roi_or_x: Any,
        y: Any = None,
        width: Any = None,
        height: Any = None,
    ) -> "RoiBox":
        if y is not None or width is not None or height is not None:
            if y is None or width is None or height is None:
                raise RoiBoundsError(
                    E_ROI_TYPE,
                    "positional ROI requires all four integers (x, y, width, height)",
                )
            return cls(roi_or_x, y, width, height)
        if isinstance(roi_or_x, RoiBox):
            return roi_or_x
        if isinstance(roi_or_x, Mapping):
            if {"x", "y", "width", "height"} <= set(roi_or_x.keys()):
                if len(roi_or_x) != 4:
                    raise RoiBoundsError(E_ROI_TYPE, "ROI mapping has unexpected extra keys")
                return cls(roi_or_x["x"], roi_or_x["y"], roi_or_x["width"], roi_or_x["height"])
            if {"x", "y", "w", "h"} <= set(roi_or_x.keys()):
                if len(roi_or_x) != 4:
                    raise RoiBoundsError(E_ROI_TYPE, "ROI mapping has unexpected extra keys")
                return cls(roi_or_x["x"], roi_or_x["y"], roi_or_x["w"], roi_or_x["h"])
            if {"x0", "y0", "x1", "y1"} <= set(roi_or_x.keys()):
                if len(roi_or_x) != 4:
                    raise RoiBoundsError(E_ROI_TYPE, "ROI mapping has unexpected extra keys")
                return cls.from_xyxy(roi_or_x["x0"], roi_or_x["y0"], roi_or_x["x1"], roi_or_x["y1"])
            raise RoiBoundsError(E_ROI_TYPE, "ROI mapping must contain (x, y, width, height) or (x0, y0, x1, y1)")
        if isinstance(roi_or_x, (tuple, list)):
            if len(roi_or_x) != 4:
                raise RoiBoundsError(E_ROI_TYPE, f"ROI sequence must have 4 elements, got {len(roi_or_x)}")
            return cls(roi_or_x[0], roi_or_x[1], roi_or_x[2], roi_or_x[3])
        raise RoiBoundsError(E_ROI_TYPE, f"unsupported ROI specification type: {type(roi_or_x).__name__}")

    def validate_within(self, frame_width: int, frame_height: int) -> "RoiBox":
        fw, fh = _validate_dimensions(frame_width, frame_height)
        if self._x >= fw or self._y >= fh or (self._x + self._width) > fw or (self._y + self._height) > fh:
            raise RoiBoundsError(
                E_ROI_BOUNDS,
                f"ROI ({self._x}, {self._y}, {self._width}, {self._height}) exceeds frame bounds {fw}x{fh}",
            )
        return self

    def translate(self, dx: int, dy: int) -> "RoiBox":
        idx = _require_exact_int(dx, "dx", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        idy = _require_exact_int(dy, "dy", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        nx = self._x + idx
        ny = self._y + idy
        if nx < 0 or ny < 0:
            raise RoiBoundsError(E_ROI_BOUNDS, f"translated ROI origin becomes negative: ({nx}, {ny})")
        return RoiBox(nx, ny, self._width, self._height)

    def contains_point(self, px: int, py: int) -> bool:
        ipx = _require_exact_int(px, "px", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        ipy = _require_exact_int(py, "py", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        return (self._x <= ipx < self._x + self._width) and (self._y <= ipy < self._y + self._height)

    def contains_box(self, other: "RoiBox") -> bool:
        if not isinstance(other, RoiBox):
            raise RoiBoundsError(E_ROI_TYPE, "other must be an RoiBox")
        return (
            other._x >= self._x
            and other._y >= self._y
            and (other._x + other._width) <= (self._x + self._width)
            and (other._y + other._height) <= (self._y + self._height)
        )

    @property
    def x(self) -> int:
        return self._x

    @property
    def y(self) -> int:
        return self._y

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def w(self) -> int:
        return self._width

    @property
    def h(self) -> int:
        return self._height

    @property
    def x0(self) -> int:
        return self._x

    @property
    def y0(self) -> int:
        return self._y

    @property
    def x1(self) -> int:
        return self._x + self._width

    @property
    def y1(self) -> int:
        return self._y + self._height

    @property
    def left(self) -> int:
        return self._x

    @property
    def top(self) -> int:
        return self._y

    @property
    def right(self) -> int:
        return self._x + self._width

    @property
    def bottom(self) -> int:
        return self._y + self._height

    @property
    def origin(self) -> Tuple[int, int]:
        return (self._x, self._y)

    @property
    def dimensions(self) -> Tuple[int, int]:
        return (self._width, self._height)

    @property
    def size(self) -> Tuple[int, int]:
        return (self._width, self._height)

    @property
    def shape(self) -> Tuple[int, int, int]:
        return (self._height, self._width, RGB_CHANNELS)

    @property
    def area(self) -> int:
        return self._width * self._height

    @property
    def nbytes(self) -> int:
        return self._width * self._height * RGB_CHANNELS

    @property
    def byte_length(self) -> int:
        return self.nbytes

    def as_xywh(self) -> Tuple[int, int, int, int]:
        return (self._x, self._y, self._width, self._height)

    def as_xyxy(self) -> Tuple[int, int, int, int]:
        return (self._x, self._y, self._x + self._width, self._y + self._height)

    def to_dict(self) -> dict[str, int]:
        return {"x": self._x, "y": self._y, "width": self._width, "height": self._height}

    def __iter__(self):
        yield self._x
        yield self._y
        yield self._width
        yield self._height

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index: int) -> int:
        return (self._x, self._y, self._width, self._height)[index]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RoiBox):
            return False
        return self.as_xywh() == other.as_xywh()

    def __hash__(self) -> int:
        return hash(self.as_xywh())

    def __repr__(self) -> str:
        return f"RoiBox(x={self._x}, y={self._y}, width={self._width}, height={self._height})"


class FrameProvenance:
    """Immutable digest and coordinate provenance for a full frame or ROI crop."""

    __slots__ = (
        "_sha256",
        "_root_sha256",
        "_parent_sha256",
        "_width",
        "_height",
        "_root_width",
        "_root_height",
        "_roi_in_root",
        "_roi_in_parent",
        "_crop_chain",
    )

    def __init__(
        self,
        *,
        sha256: str,
        root_sha256: str,
        parent_sha256: Optional[str],
        width: int,
        height: int,
        root_width: int,
        root_height: int,
        roi_in_root: RoiBox,
        roi_in_parent: Optional[RoiBox],
        crop_chain: Sequence[RoiBox] = (),
    ) -> None:
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise FrameFormatError(E_FRAME_TYPE, "sha256 must be a 64-char hex string")
        if not isinstance(root_sha256, str) or len(root_sha256) != 64:
            raise FrameFormatError(E_FRAME_TYPE, "root_sha256 must be a 64-char hex string")
        if parent_sha256 is not None and (not isinstance(parent_sha256, str) or len(parent_sha256) != 64):
            raise FrameFormatError(E_FRAME_TYPE, "parent_sha256 must be None or a 64-char hex string")
        w, h = _validate_dimensions(width, height)
        rw, rh = _validate_dimensions(root_width, root_height)
        if not isinstance(roi_in_root, RoiBox):
            raise RoiBoundsError(E_ROI_TYPE, "roi_in_root must be an RoiBox")
        roi_in_root.validate_within(rw, rh)
        if (roi_in_root.width, roi_in_root.height) != (w, h):
            raise FrameGeometryError(E_FRAME_GEOMETRY, "roi_in_root dimensions must match frame width and height")
        if roi_in_parent is not None and not isinstance(roi_in_parent, RoiBox):
            raise RoiBoundsError(E_ROI_TYPE, "roi_in_parent must be None or an RoiBox")
        chain_tuple = tuple(crop_chain)
        for step in chain_tuple:
            if not isinstance(step, RoiBox):
                raise RoiBoundsError(E_ROI_TYPE, "crop_chain elements must be RoiBox instances")

        object.__setattr__(self, "_sha256", sha256)
        object.__setattr__(self, "_root_sha256", root_sha256)
        object.__setattr__(self, "_parent_sha256", parent_sha256)
        object.__setattr__(self, "_width", w)
        object.__setattr__(self, "_height", h)
        object.__setattr__(self, "_root_width", rw)
        object.__setattr__(self, "_root_height", rh)
        object.__setattr__(self, "_roi_in_root", roi_in_root)
        object.__setattr__(self, "_roi_in_parent", roi_in_parent)
        object.__setattr__(self, "_crop_chain", chain_tuple)

    def __setattr__(self, name: str, value: Any) -> None:
        raise FrameImmutabilityError(E_IMMUTABLE, "FrameProvenance is immutable")

    def __delattr__(self, name: str) -> None:
        raise FrameImmutabilityError(E_IMMUTABLE, "FrameProvenance is immutable")

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def root_sha256(self) -> str:
        return self._root_sha256

    @property
    def full_frame_sha256(self) -> str:
        return self._root_sha256

    @property
    def parent_sha256(self) -> Optional[str]:
        return self._parent_sha256

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def dimensions(self) -> Tuple[int, int]:
        return (self._width, self._height)

    @property
    def root_width(self) -> int:
        return self._root_width

    @property
    def root_height(self) -> int:
        return self._root_height

    @property
    def root_dimensions(self) -> Tuple[int, int]:
        return (self._root_width, self._root_height)

    @property
    def roi_in_root(self) -> RoiBox:
        return self._roi_in_root

    @property
    def roi_in_parent(self) -> Optional[RoiBox]:
        return self._roi_in_parent

    @property
    def crop_chain(self) -> Tuple[RoiBox, ...]:
        return self._crop_chain

    @property
    def is_crop(self) -> bool:
        return len(self._crop_chain) > 0

    @property
    def crop_depth(self) -> int:
        return len(self._crop_chain)

    @property
    def origin_in_root(self) -> Tuple[int, int]:
        return self._roi_in_root.origin

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FrameProvenance):
            return False
        return (
            self._sha256 == other._sha256
            and self._root_sha256 == other._root_sha256
            and self._parent_sha256 == other._parent_sha256
            and self._width == other._width
            and self._height == other._height
            and self._root_width == other._root_width
            and self._root_height == other._root_height
            and self._roi_in_root == other._roi_in_root
            and self._roi_in_parent == other._roi_in_parent
            and self._crop_chain == other._crop_chain
        )

    def __hash__(self) -> int:
        return hash(
            (
                self._sha256,
                self._root_sha256,
                self._parent_sha256,
                self._width,
                self._height,
                self._root_width,
                self._root_height,
                self._roi_in_root,
                self._roi_in_parent,
                self._crop_chain,
            )
        )


def _admit_pixels(
    source: Any,
    *,
    width: Any = None,
    height: Any = None,
) -> Tuple[np.ndarray, bytes, int, int, str]:
    """Validate and copy caller pixels into an immutable C-contiguous uint8 array and bytes."""
    if source is None:
        raise FrameFormatError(E_FRAME_NULL, "frame source cannot be None")

    if isinstance(source, (bool, int, float, str, list, tuple, dict, set)):
        raise FrameFormatError(
            E_FRAME_TYPE,
            f"unsupported frame container {type(source).__name__}; expected C-contiguous uint8 ndarray or RGB24 buffer",
        )

    if isinstance(source, np.ndarray):
        if source.dtype == np.bool_ or source.dtype != np.uint8:
            raise FrameFormatError(
                E_FRAME_DTYPE,
                f"frame ndarray dtype must be uint8, got {source.dtype}",
            )
        if source.ndim != 3:
            raise FrameGeometryError(
                E_FRAME_GEOMETRY,
                f"frame ndarray must have 3 dimensions (H, W, 3), got ndim={source.ndim} shape={source.shape}",
            )
        h, w, c = source.shape
        if h <= 0 or w <= 0 or source.size == 0:
            raise FrameGeometryError(
                E_FRAME_GEOMETRY,
                f"frame ndarray has empty geometry: shape={source.shape}",
            )
        if c != RGB_CHANNELS:
            raise FrameGeometryError(
                E_FRAME_GEOMETRY,
                f"frame ndarray must have exactly {RGB_CHANNELS} RGB channels, got {c}",
            )
        if w > MAX_SIDE or h > MAX_SIDE:
            raise FrameGeometryError(
                E_FRAME_GEOMETRY,
                f"frame dimensions {w}x{h} exceed MAX_SIDE ({MAX_SIDE})",
            )
        if width is not None or height is not None:
            exp_w = w if width is None else _require_exact_int(
                width, "width", err_cls=FrameGeometryError, code=E_FRAME_GEOMETRY
            )
            exp_h = h if height is None else _require_exact_int(
                height, "height", err_cls=FrameGeometryError, code=E_FRAME_GEOMETRY
            )
            if (exp_w, exp_h) != (w, h):
                raise FrameGeometryError(
                    E_FRAME_GEOMETRY,
                    f"ndarray geometry {w}x{h} does not match expected {exp_w}x{exp_h}",
                )
        if not source.flags["C_CONTIGUOUS"]:
            raise FrameFormatError(
                E_FRAME_CONTIGUOUS,
                "frame ndarray must be C-contiguous uint8 RGB",
            )
        # Immediate owned copy so subsequent caller mutations cannot alter evidence.
        owned_arr = np.array(source, dtype=np.uint8, copy=True, order="C")
        owned_arr.setflags(write=False)
        raw_bytes = owned_arr.tobytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        return owned_arr, raw_bytes, w, h, digest

    if isinstance(source, (bytes, bytearray, memoryview)):
        if width is None or height is None:
            raise FrameGeometryError(
                E_FRAME_GEOMETRY,
                "raw RGB24 buffer admission requires explicit integer width and height",
            )
        w, h = _validate_dimensions(width, height)
        if isinstance(source, memoryview):
            if not source.c_contiguous:
                raise FrameFormatError(
                    E_FRAME_CONTIGUOUS,
                    "memoryview source must be C-contiguous",
                )
            if source.format != "B" or source.itemsize != 1:
                raise FrameFormatError(
                    E_FRAME_DTYPE,
                    f"memoryview format must be unsigned byte ('B'), got {source.format!r} (itemsize={source.itemsize})",
                )
            if source.ndim not in (1, 3):
                raise FrameGeometryError(
                    E_FRAME_GEOMETRY,
                    f"memoryview must be 1D raw RGB24 bytes or 3D (H, W, 3), got ndim={source.ndim}",
                )
            if source.ndim == 3 and tuple(source.shape) != (h, w, RGB_CHANNELS):
                raise FrameGeometryError(
                    E_FRAME_GEOMETRY,
                    f"memoryview shape {tuple(source.shape)} does not match ({h}, {w}, {RGB_CHANNELS})",
                )
        # Copy immediately from caller buffer to own the evidence bytes.
        raw_bytes = bytes(source)
        expected_nbytes = w * h * RGB_CHANNELS
        if len(raw_bytes) != expected_nbytes:
            raise FrameGeometryError(
                E_FRAME_GEOMETRY,
                f"RGB24 byte length {len(raw_bytes)} does not match {w}x{h}x{RGB_CHANNELS} ({expected_nbytes})",
            )
        owned_arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((h, w, RGB_CHANNELS)).copy(order="C")
        owned_arr.setflags(write=False)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        return owned_arr, raw_bytes, w, h, digest

    raise FrameFormatError(
        E_FRAME_TYPE,
        f"unsupported frame source type: {type(source).__name__}",
    )


