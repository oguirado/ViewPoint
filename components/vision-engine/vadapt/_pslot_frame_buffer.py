"""pslot_frame_ref: pslot frame buffer."""
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

class FrameRef:
    """Immutable exact RAM frame-reference carrying sha256, dimensions, and ROI provenance."""

    __slots__ = (
        "_pixels",
        "_rgb_bytes",
        "_width",
        "_height",
        "_sha256",
        "_provenance",
    )

    def __init__(
        self,
        pixels: Union[np.ndarray, bytes, bytearray, memoryview],
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        _provenance: Optional[FrameProvenance] = None,
    ) -> None:
        owned_arr, raw_bytes, w, h, digest = _admit_pixels(pixels, width=width, height=height)
        if _provenance is None:
            prov = FrameProvenance(
                sha256=digest,
                root_sha256=digest,
                parent_sha256=None,
                width=w,
                height=h,
                root_width=w,
                root_height=h,
                roi_in_root=RoiBox(0, 0, w, h),
                roi_in_parent=None,
                crop_chain=(),
            )
        else:
            if not isinstance(_provenance, FrameProvenance):
                raise FrameFormatError(E_FRAME_TYPE, "_provenance must be a FrameProvenance instance")
            if _provenance.sha256 != digest or _provenance.dimensions != (w, h):
                raise FrameGeometryError(
                    E_FRAME_GEOMETRY,
                    "provenance digest or dimensions do not match admitted pixels",
                )
            prov = _provenance

        object.__setattr__(self, "_pixels", owned_arr)
        object.__setattr__(self, "_rgb_bytes", raw_bytes)
        object.__setattr__(self, "_width", w)
        object.__setattr__(self, "_height", h)
        object.__setattr__(self, "_sha256", digest)
        object.__setattr__(self, "_provenance", prov)

    def __setattr__(self, name: str, value: Any) -> None:
        raise FrameImmutabilityError(E_IMMUTABLE, "FrameRef is immutable")

    def __delattr__(self, name: str) -> None:
        raise FrameImmutabilityError(E_IMMUTABLE, "FrameRef is immutable")

    @classmethod
    def from_array(
        cls,
        arr: np.ndarray,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> "FrameRef":
        if not isinstance(arr, np.ndarray):
            raise FrameFormatError(E_FRAME_TYPE, f"from_array requires numpy.ndarray, got {type(arr).__name__}")
        return cls(arr, width=width, height=height)

    from_rgb_array = from_array

    @classmethod
    def from_bytes(cls, buf: Union[bytes, bytearray, memoryview], width: int, height: int) -> "FrameRef":
        if not isinstance(buf, (bytes, bytearray, memoryview)):
            raise FrameFormatError(
                E_FRAME_TYPE,
                f"from_bytes requires bytes, bytearray, or memoryview, got {type(buf).__name__}",
            )
        return cls(buf, width=width, height=height)

    from_rgb24 = from_bytes

    @classmethod
    def from_xwd(cls, buf: Union[bytes, bytearray, memoryview], hd: Optional[xfb.Header] = None) -> "FrameRef":
        if not isinstance(buf, (bytes, bytearray, memoryview)):
            raise FrameFormatError(
                E_FRAME_TYPE,
                f"from_xwd requires XWD byte buffer, got {type(buf).__name__}",
            )
        raw_xwd = bytes(buf)
        validated_hd = xfb.validate_frame(raw_xwd)
        if hd is not None and hd != validated_hd:
            raise xfb.XwdError("E_XWD_GEOMETRY", "cached header does not match validated frame")
        rgb = xfb.rgb24(raw_xwd, validated_hd)
        return cls.from_bytes(rgb, validated_hd.width, validated_hd.height)

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def frame_sha256(self) -> str:
        return self._sha256

    @property
    def digest(self) -> str:
        return self._sha256

    @property
    def root_sha256(self) -> str:
        return self._provenance.root_sha256

    @property
    def full_frame_sha256(self) -> str:
        return self._provenance.root_sha256

    @property
    def source_frame_sha256(self) -> str:
        return self._provenance.root_sha256

    @property
    def parent_sha256(self) -> Optional[str]:
        return self._provenance.parent_sha256

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def channels(self) -> int:
        return RGB_CHANNELS

    @property
    def dimensions(self) -> Tuple[int, int]:
        return (self._width, self._height)

    @property
    def root_width(self) -> int:
        return self._provenance.root_width

    @property
    def root_height(self) -> int:
        return self._provenance.root_height

    @property
    def root_dimensions(self) -> Tuple[int, int]:
        return self._provenance.root_dimensions

    @property
    def full_frame_dimensions(self) -> Tuple[int, int]:
        return self._provenance.root_dimensions

    @property
    def shape(self) -> Tuple[int, int, int]:
        return (self._height, self._width, RGB_CHANNELS)

    @property
    def dtype(self) -> np.dtype:
        return self._pixels.dtype

    @property
    def nbytes(self) -> int:
        return len(self._rgb_bytes)

    @property
    def byte_size(self) -> int:
        return len(self._rgb_bytes)

    @property
    def strides(self) -> Tuple[int, int, int]:
        return self._pixels.strides

    @property
    def c_contiguous(self) -> bool:
        return bool(self._pixels.flags["C_CONTIGUOUS"])

    @property
    def is_crop(self) -> bool:
        return self._provenance.is_crop

    @property
    def crop_depth(self) -> int:
        return self._provenance.crop_depth

    @property
    def roi(self) -> RoiBox:
        return self._provenance.roi_in_root

    @property
    def crop_box(self) -> RoiBox:
        return self._provenance.roi_in_root

    @property
    def roi_in_root(self) -> RoiBox:
        return self._provenance.roi_in_root

    @property
    def roi_in_parent(self) -> Optional[RoiBox]:
        return self._provenance.roi_in_parent

    @property
    def parent_roi(self) -> Optional[RoiBox]:
        return self._provenance.roi_in_parent

    @property
    def origin_in_root(self) -> Tuple[int, int]:
        return self._provenance.origin_in_root

    @property
    def crop_chain(self) -> Tuple[RoiBox, ...]:
        return self._provenance.crop_chain

    @property
    def provenance(self) -> FrameProvenance:
        return self._provenance

    @property
    def pixels(self) -> np.ndarray:
        """Read-only C-contiguous uint8 (H, W, 3) numpy view."""
        view = self._pixels.view()
        view.setflags(write=False)
        return view

    @property
    def array(self) -> np.ndarray:
        return self.pixels

    @property
    def rgb_bytes(self) -> bytes:
        return self._rgb_bytes

    @property
    def rgb_view(self) -> memoryview:
        return memoryview(self._rgb_bytes)

    def rgb24_bytes(self) -> bytes:
        return self._rgb_bytes

    def as_readonly_array(self) -> np.ndarray:
        return self.pixels

    def copy_array(self) -> np.ndarray:
        """Return a fresh writable C-contiguous uint8 copy detached from internal RAM."""
        return np.array(self._pixels, dtype=np.uint8, copy=True, order="C")

    to_numpy = copy_array

    def pixel_at(self, x: int, y: int) -> Tuple[int, int, int]:
        ix = _require_exact_int(x, "x", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        iy = _require_exact_int(y, "y", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        if ix < 0 or iy < 0 or ix >= self._width or iy >= self._height:
            raise RoiBoundsError(
                E_COORD_BOUNDS,
                f"pixel coordinate ({ix}, {iy}) outside frame bounds {self._width}x{self._height}",
            )
        px = self._pixels[iy, ix]
        return (int(px[0]), int(px[1]), int(px[2]))

    def crop(
        self,
        roi_or_x: Any,
        y: Any = None,
        width: Any = None,
        height: Any = None,
    ) -> "FrameRef":
        """Create a new immutable FrameRef for a validated integer ROI crop."""
        box = RoiBox.coerce(roi_or_x, y, width, height).validate_within(self._width, self._height)
        sub_arr = np.ascontiguousarray(
            self._pixels[box.y : box.y + box.height, box.x : box.x + box.width, :],
            dtype=np.uint8,
        ).copy(order="C")
        sub_arr.setflags(write=False)
        sub_bytes = sub_arr.tobytes()
        sub_sha = hashlib.sha256(sub_bytes).hexdigest()

        root_roi = RoiBox(
            self._provenance.roi_in_root.x + box.x,
            self._provenance.roi_in_root.y + box.y,
            box.width,
            box.height,
        ).validate_within(self._provenance.root_width, self._provenance.root_height)

        prov = FrameProvenance(
            sha256=sub_sha,
            root_sha256=self._provenance.root_sha256,
            parent_sha256=self._sha256,
            width=box.width,
            height=box.height,
            root_width=self._provenance.root_width,
            root_height=self._provenance.root_height,
            roi_in_root=root_roi,
            roi_in_parent=box,
            crop_chain=self._provenance.crop_chain + (box,),
        )
        return FrameRef(sub_arr, _provenance=prov)

    crop_roi = crop

    def crop_xyxy(self, x0: int, y0: int, x1: int, y1: int) -> "FrameRef":
        box = RoiBox.from_xyxy(x0, y0, x1, y1).validate_within(self._width, self._height)
        return self.crop(box)

    def crop_to_root_point(self, cx: int, cy: int) -> Tuple[int, int]:
        icx = _require_exact_int(cx, "cx", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        icy = _require_exact_int(cy, "cy", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        if icx < 0 or icy < 0 or icx >= self._width or icy >= self._height:
            raise RoiBoundsError(
                E_COORD_BOUNDS,
                f"crop coordinate ({icx}, {icy}) outside crop bounds {self._width}x{self._height}",
            )
        return (self._provenance.roi_in_root.x + icx, self._provenance.roi_in_root.y + icy)

    local_to_root_point = crop_to_root_point
    to_root_coords = crop_to_root_point

    def root_to_crop_point(self, rx: int, ry: int) -> Tuple[int, int]:
        irx = _require_exact_int(rx, "rx", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        iry = _require_exact_int(ry, "ry", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        rbox = self._provenance.roi_in_root
        if not rbox.contains_point(irx, iry):
            raise RoiBoundsError(
                E_COORD_BOUNDS,
                f"root coordinate ({irx}, {iry}) outside crop root ROI {rbox.as_xywh()}",
            )
        return (irx - rbox.x, iry - rbox.y)

    root_to_local_point = root_to_crop_point
    from_root_coords = root_to_crop_point

    def crop_to_parent_point(self, cx: int, cy: int) -> Tuple[int, int]:
        icx = _require_exact_int(cx, "cx", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        icy = _require_exact_int(cy, "cy", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        if icx < 0 or icy < 0 or icx >= self._width or icy >= self._height:
            raise RoiBoundsError(
                E_COORD_BOUNDS,
                f"crop coordinate ({icx}, {icy}) outside crop bounds {self._width}x{self._height}",
            )
        pbox = self._provenance.roi_in_parent
        if pbox is None:
            return (icx, icy)
        return (pbox.x + icx, pbox.y + icy)

    local_to_parent_point = crop_to_parent_point

    def parent_to_crop_point(self, px: int, py: int) -> Tuple[int, int]:
        ipx = _require_exact_int(px, "px", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        ipy = _require_exact_int(py, "py", err_cls=RoiBoundsError, code=E_COORD_BOUNDS)
        pbox = self._provenance.roi_in_parent
        if pbox is None:
            if ipx < 0 or ipy < 0 or ipx >= self._width or ipy >= self._height:
                raise RoiBoundsError(
                    E_COORD_BOUNDS,
                    f"parent coordinate ({ipx}, {ipy}) outside bounds {self._width}x{self._height}",
                )
            return (ipx, ipy)
        if not pbox.contains_point(ipx, ipy):
            raise RoiBoundsError(
                E_COORD_BOUNDS,
                f"parent coordinate ({ipx}, {ipy}) outside crop parent ROI {pbox.as_xywh()}",
            )
        return (ipx - pbox.x, ipy - pbox.y)

    parent_to_local_point = parent_to_crop_point

    def crop_to_root_roi(
        self,
        roi_or_x: Any,
        y: Any = None,
        width: Any = None,
        height: Any = None,
    ) -> RoiBox:
        local_box = RoiBox.coerce(roi_or_x, y, width, height).validate_within(self._width, self._height)
        rbox = self._provenance.roi_in_root
        return RoiBox(
            rbox.x + local_box.x,
            rbox.y + local_box.y,
            local_box.width,
            local_box.height,
        ).validate_within(self._provenance.root_width, self._provenance.root_height)

    local_to_root_roi = crop_to_root_roi
    to_root_box = crop_to_root_roi

    def root_to_crop_roi(
        self,
        roi_or_x: Any,
        y: Any = None,
        width: Any = None,
        height: Any = None,
    ) -> RoiBox:
        root_box = RoiBox.coerce(roi_or_x, y, width, height).validate_within(
            self._provenance.root_width,
            self._provenance.root_height,
        )
        rbox = self._provenance.roi_in_root
        if not rbox.contains_box(root_box):
            raise RoiBoundsError(
                E_ROI_BOUNDS,
                f"root ROI {root_box.as_xywh()} is not contained within crop root ROI {rbox.as_xywh()}",
            )
        return RoiBox(
            root_box.x - rbox.x,
            root_box.y - rbox.y,
            root_box.width,
            root_box.height,
        ).validate_within(self._width, self._height)

    root_to_local_roi = root_to_crop_roi
    from_root_box = root_to_crop_roi

    def verify_integrity(self) -> bool:
        """Verify internal buffer immutability, C-contiguity, and SHA-256 consistency."""
        if self._pixels.dtype != np.uint8:
            return False
        if not self._pixels.flags["C_CONTIGUOUS"]:
            return False
        if self._pixels.flags["WRITEABLE"]:
            return False
        if self._pixels.shape != (self._height, self._width, RGB_CHANNELS):
            return False
        now_bytes = self._pixels.tobytes()
        if now_bytes != self._rgb_bytes:
            return False
        return hashlib.sha256(now_bytes).hexdigest() == self._sha256

    def same_pixels(self, other: Any) -> bool:
        """True if ``other`` has identical dimensions and exact RGB24 SHA-256 digest."""
        if not isinstance(other, FrameRef):
            return False
        return (
            self._width == other._width
            and self._height == other._height
            and self._sha256 == other._sha256
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FrameRef):
            return False
        return (
            self._sha256 == other._sha256
            and self._width == other._width
            and self._height == other._height
            and self._provenance == other._provenance
        )

    def __hash__(self) -> int:
        return hash((self._sha256, self._width, self._height, self._provenance))

    def __repr__(self) -> str:
        return (
            f"FrameRef(width={self._width}, height={self._height}, "
            f"sha256='{self._sha256[:12]}...', is_crop={self.is_crop})"
        )


RamFrameRef = FrameRef


def admit_frame(
    source: Union[np.ndarray, bytes, bytearray, memoryview, FrameRef],
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> FrameRef:
    """Admit a caller pixel container into an owned, immutable FrameRef."""
    if isinstance(source, FrameRef):
        if width is not None or height is not None:
            w, h = _validate_dimensions(
                source.width if width is None else width,
                source.height if height is None else height,
            )
            if (w, h) != source.dimensions:
                raise FrameGeometryError(
                    E_FRAME_GEOMETRY,
                    f"FrameRef dimensions {source.dimensions} do not match expected {(w, h)}",
                )
        return source
    return FrameRef(source, width=width, height=height)


def admit_xwd_frame(buf: Union[bytes, bytearray, memoryview], hd: Optional[xfb.Header] = None) -> FrameRef:
    """Validate an XWD buffer via vadapt.xfb and admit its canonical RGB24 pixels."""
    return FrameRef.from_xwd(buf, hd=hd)


def validate_roi(
    roi_or_x: Any,
    y: Any = None,
    width: Any = None,
    height: Any = None,
    *,
    frame_width: Optional[int] = None,
    frame_height: Optional[int] = None,
) -> RoiBox:
    """Validate an integer ROI specification and optionally check bounds against frame dimensions."""
    box = RoiBox.coerce(roi_or_x, y, width, height)
    if frame_width is not None or frame_height is not None:
        if frame_width is None or frame_height is None:
            raise FrameGeometryError(
                E_FRAME_GEOMETRY,
                "both frame_width and frame_height must be provided when validating bounds",
            )
        box.validate_within(frame_width, frame_height)
    return box


def compute_rgb24_sha256(
    source: Union[np.ndarray, bytes, bytearray, memoryview, FrameRef],
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> str:
    """Return the canonical RGB24 SHA-256 hex digest for any valid pixel container."""
    return admit_frame(source, width=width, height=height).sha256


__all__ = [
    "AUSENTE",
    "RGB_CHANNELS",
    "MAX_SIDE",
    "MAX_FRAME_PIXELS",
    "MAX_RGB_BYTES",
    "E_FRAME_NULL",
    "E_FRAME_TYPE",
    "E_FRAME_DTYPE",
    "E_FRAME_CONTIGUOUS",
    "E_FRAME_GEOMETRY",
    "E_ROI_TYPE",
    "E_ROI_EMPTY",
    "E_ROI_BOUNDS",
    "E_COORD_BOUNDS",
    "E_IMMUTABLE",
    "FrameRefError",
    "PslotFrameRefError",
    "FrameFormatError",
    "FrameGeometryError",
    "RoiValidationError",
    "RoiBoundsError",
    "FrameImmutabilityError",
    "RoiBox",
    "FrameProvenance",
    "FrameRef",
    "RamFrameRef",
    "admit_frame",
    "admit_xwd_frame",
    "validate_roi",
    "compute_rgb24_sha256",
]
