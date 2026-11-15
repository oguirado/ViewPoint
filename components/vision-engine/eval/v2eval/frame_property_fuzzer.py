"""Zero-dependency SplitMix64 deterministic property fuzzer for vision frames (G-R2-2).

Provides an explicit-state SplitMix64 PRNG and deterministic seed tree with
independent synthetic RGB frame, XWD header/buffer, and luma motion-band
generators, exact replay by (seed, case_index), hierarchical shrinking that
preserves the original invalidity or oracle disagreement signature, and
independent Python-integer extent, RGB24 conversion, and claim-precedence
oracles. All pixel buffers are produced strictly in RAM.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import struct
import threading
from types import SimpleNamespace
from typing import Callable
from unittest import mock

import numpy as np

from eval.v2eval import postpass as _postpass
from eval.v2x import gm_bands_hv as _gm_bands_hv
from vadapt import x11_ramcap as _x11_ramcap
from vadapt import xfb as _xfb
from visioncore import desc as _desc
from visioncore.grid import Grid as _Grid


MASK64 = (1 << 64) - 1
GOLDEN_GAMMA = 0x9E3779B97F4A7C15
MIX1 = 0xBF58476D1CE4E5B9
MIX2 = 0x94D049BB133111EB

DOMAINS = ("frame", "xwd", "motion")


def require_exact_int(value: object, name: str, *, min_value: int | None = None,
                      max_value: int | None = None) -> int:
    """Refuse booleans, floats, numpy scalars, and out-of-range integers."""
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact integer")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}")
    return value


def _splitmix64_mix(word: int) -> int:
    z = word & MASK64
    z = ((z ^ (z >> 30)) * MIX1) & MASK64
    z = ((z ^ (z >> 27)) * MIX2) & MASK64
    return (z ^ (z >> 31)) & MASK64


def _tag_to_u64(tag: int | str) -> int:
    if type(tag) is int:
        require_exact_int(tag, "tag", min_value=0, max_value=MASK64)
        return _splitmix64_mix(tag ^ 0xD6E8FEB86659FD93)
    if type(tag) is str:
        acc = 0xCBF29CE484222325
        for b in tag.encode("utf-8"):
            acc ^= b
            acc = (acc * 0x100000001B3) & MASK64
        return _splitmix64_mix(acc)
    raise TypeError("seed tree tag must be an exact int or str")


class SplitMix64:
    """Explicit-state 64-bit SplitMix64 deterministic generator."""

    __slots__ = ("_state",)

    def __init__(self, state: int) -> None:
        require_exact_int(state, "state", min_value=0, max_value=MASK64)
        self._state = state

    @property
    def state(self) -> int:
        return self._state

    def snapshot(self) -> int:
        return self._state

    def restore(self, state: int) -> None:
        require_exact_int(state, "state", min_value=0, max_value=MASK64)
        self._state = state

    def clone(self) -> SplitMix64:
        return SplitMix64(self._state)

    def next_u64(self) -> int:
        self._state = (self._state + GOLDEN_GAMMA) & MASK64
        return _splitmix64_mix(self._state)

    def split(self, label: int | str = 0) -> SplitMix64:
        tag_word = _tag_to_u64(label)
        mixed = _splitmix64_mix(self.next_u64() ^ tag_word)
        return SplitMix64(mixed)

    def randint(self, low: int, high: int) -> int:
        require_exact_int(low, "low")
        require_exact_int(high, "high")
        if low > high:
            raise ValueError("low must be <= high")
        span = high - low + 1
        if span == 1:
            self.next_u64()
            return low
        if span >= (1 << 64):
            return low + self.next_u64()
        limit = ((1 << 64) // span) * span
        while True:
            r = self.next_u64()
            if r < limit:
                return low + (r % span)

    def choice(self, seq: tuple | list):
        if not isinstance(seq, (tuple, list)) or len(seq) == 0:
            raise ValueError("seq must be a non-empty tuple or list")
        idx = self.randint(0, len(seq) - 1)
        return seq[idx]

    def chance(self, numerator: int, denominator: int) -> bool:
        require_exact_int(numerator, "numerator", min_value=0)
        require_exact_int(denominator, "denominator", min_value=1)
        if numerator > denominator:
            raise ValueError("numerator must be <= denominator")
        if numerator == 0:
            self.next_u64()
            return False
        if numerator == denominator:
            self.next_u64()
            return True
        return self.randint(0, denominator - 1) < numerator

    def randbytes(self, length: int) -> bytes:
        require_exact_int(length, "length", min_value=0)
        if length == 0:
            return b""
        out = bytearray(length)
        full_words = length // 8
        for i in range(full_words):
            struct.pack_into("<Q", out, i * 8, self.next_u64())
        rem = length - full_words * 8
        if rem:
            tail = struct.pack("<Q", self.next_u64())
            out[full_words * 8:] = tail[:rem]
        return bytes(out)

    def uint8_luma(self, height: int, width: int) -> np.ndarray:
        require_exact_int(height, "height", min_value=1)
        require_exact_int(width, "width", min_value=1)
        raw = self.randbytes(height * width)
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, width).copy()

    def uint8_rgb(self, height: int, width: int) -> np.ndarray:
        require_exact_int(height, "height", min_value=1)
        require_exact_int(width, "width", min_value=1)
        raw = self.randbytes(height * width * 3)
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()


class SeedTree:
    """Deterministic SplitMix64 seed tree supporting O(1) domain and case replay."""

    __slots__ = ("_root_seed",)

    def __init__(self, root_seed: int) -> None:
        require_exact_int(root_seed, "root_seed", min_value=0, max_value=MASK64)
        self._root_seed = root_seed

    @property
    def root_seed(self) -> int:
        return self._root_seed

    def derive_seed(self, domain: str, case_index: int, substream: int = 0) -> int:
        if type(domain) is not str or not domain:
            raise TypeError("domain must be a non-empty str")
        require_exact_int(case_index, "case_index", min_value=0, max_value=MASK64)
        require_exact_int(substream, "substream", min_value=0, max_value=MASK64)
        h = _splitmix64_mix(self._root_seed ^ 0xA0761D6478BD642F)
        h = _splitmix64_mix(h ^ _tag_to_u64(domain))
        h = _splitmix64_mix(h ^ ((case_index + 1) * GOLDEN_GAMMA))
        h = _splitmix64_mix(h ^ ((substream + 1) * MIX1))
        return h

    def rng_for(self, domain: str, case_index: int, substream: int = 0) -> SplitMix64:
        return SplitMix64(self.derive_seed(domain, case_index, substream))


@dataclass(frozen=True)
class XwdCase:
    """Synthetic XWD header and payload test case in RAM."""

    seed: int
    case_index: int
    header_size: int
    file_version: int
    pixmap_format: int
    depth: int
    width: int
    height: int
    byte_order: int
    bitmap_bit_order: int
    bpp: int
    bytes_per_line: int
    visual_class: int
    red_mask: int
    green_mask: int
    blue_mask: int
    ncolors: int
    pad_bytes: int
    size_delta: int
    expected_width: int | None
    expected_height: int | None
    rgb: np.ndarray
    buf: bytes
    fault_kind: str


def build_xwd_buffer(
    rgb: np.ndarray,
    *,
    header_size: int = _xfb.MIN_HEADER,
    file_version: int = _xfb.XWD_VERSION,
    pixmap_format: int = _xfb.ZPIXMAP,
    depth: int = 24,
    width: int | None = None,
    height: int | None = None,
    byte_order: int = 0,
    bitmap_bit_order: int = 0,
    bpp: int = 32,
    bytes_per_line: int | None = None,
    visual_class: int = _xfb.TRUECOLOR,
    red_mask: int = 0xFF0000,
    green_mask: int = 0x00FF00,
    blue_mask: int = 0x0000FF,
    ncolors: int = 0,
    pad_bytes: int = 0,
    size_delta: int = 0,
    short_raw_len: int | None = None,
) -> bytes:
    """Construct a raw XWD byte buffer in RAM from explicit header fields and RGB."""
    rgb_arr = np.asarray(rgb, dtype=np.uint8)
    if rgb_arr.ndim != 3 or rgb_arr.shape[2] != 3:
        raise ValueError("rgb must have shape (H, W, 3)")
    real_h, real_w, _ = rgb_arr.shape
    w_hdr = real_w if width is None else int(width)
    h_hdr = real_h if height is None else int(height)
    if bytes_per_line is None:
        bpl = max(0, w_hdr * 4 + int(pad_bytes))
    else:
        bpl = int(bytes_per_line)

    fields = [
        int(header_size) & 0xFFFFFFFF,
        int(file_version) & 0xFFFFFFFF,
        int(pixmap_format) & 0xFFFFFFFF,
        int(depth) & 0xFFFFFFFF,
        int(w_hdr) & 0xFFFFFFFF,
        int(h_hdr) & 0xFFFFFFFF,
        0,
        int(byte_order) & 0xFFFFFFFF,
        32,
        int(bitmap_bit_order) & 0xFFFFFFFF,
        32,
        int(bpp) & 0xFFFFFFFF,
        int(bpl) & 0xFFFFFFFF,
        int(visual_class) & 0xFFFFFFFF,
        int(red_mask) & 0xFFFFFFFF,
        int(green_mask) & 0xFFFFFFFF,
        int(blue_mask) & 0xFFFFFFFF,
        8,
        max(0, int(ncolors)) & 0xFFFFFFFF,
        max(0, int(ncolors)) & 0xFFFFFFFF,
        int(w_hdr) & 0xFFFFFFFF,
        int(h_hdr) & 0xFFFFFFFF,
        0,
        0,
        0,
    ]
    header_raw = struct.pack(">25I", *fields)
    if short_raw_len is not None:
        return header_raw[: max(0, int(short_raw_len))]

    extra_hdr = max(0, int(header_size) - _xfb.MIN_HEADER)
    cmap_bytes = max(0, int(ncolors)) * _xfb.COLOR_ENTRY

    enc_h = max(0, min(real_h, h_hdr if 0 < h_hdr <= 128 else real_h))
    enc_w = max(0, min(real_w, w_hdr if 0 < w_hdr <= 128 else real_w))
    row_stride = max(enc_w * 4, bpl if 0 <= bpl <= 4096 else enc_w * 4 + max(0, int(pad_bytes)))

    payload = bytearray(enc_h * row_stride)
    for y in range(enc_h):
        row_off = y * row_stride
        for x in range(enc_w):
            r_val = int(rgb_arr[y, x, 0])
            g_val = int(rgb_arr[y, x, 1])
            b_val = int(rgb_arr[y, x, 2])
            px_off = row_off + x * 4
            if byte_order == 0:
                payload[px_off] = b_val
                payload[px_off + 1] = g_val
                payload[px_off + 2] = r_val
                payload[px_off + 3] = 0xAA
            else:
                payload[px_off] = 0x55
                payload[px_off + 1] = r_val
                payload[px_off + 2] = g_val
                payload[px_off + 3] = b_val
        for p_idx in range(enc_w * 4, row_stride):
            payload[row_off + p_idx] = (y + p_idx) & 0xFF

    full = header_raw + (b"\x00" * extra_hdr) + (b"\x11" * cmap_bytes) + bytes(payload)
    if size_delta < 0:
        cut = max(_xfb.MIN_HEADER, len(full) + int(size_delta))
        return full[:cut]
    if size_delta > 0:
        return full + (b"\xFE" * int(size_delta))
    return full


def rebuild_xwd_case(case: XwdCase) -> XwdCase:
    """Re-synthesize the raw byte buffer of an XwdCase after parameter shrinking."""
    if case.fault_kind == "short_header":
        short_len = min(len(case.buf), _xfb.MIN_HEADER - 1)
        buf = build_xwd_buffer(case.rgb, short_raw_len=short_len)
        return replace(case, buf=buf)
    buf = build_xwd_buffer(
        case.rgb,
        header_size=case.header_size,
        file_version=case.file_version,
        pixmap_format=case.pixmap_format,
        depth=case.depth,
        width=case.width,
        height=case.height,
        byte_order=case.byte_order,
        bitmap_bit_order=case.bitmap_bit_order,
        bpp=case.bpp,
        bytes_per_line=case.bytes_per_line,
        visual_class=case.visual_class,
        red_mask=case.red_mask,
        green_mask=case.green_mask,
        blue_mask=case.blue_mask,
        ncolors=case.ncolors,
        pad_bytes=case.pad_bytes,
        size_delta=case.size_delta,
    )
    return replace(case, buf=buf)


def independent_xwd_extent_oracle(
    buf: bytes | bytearray | memoryview,
    *,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, object]:
    """Independent Python-integer extent and header-precedence oracle for XWD."""
    raw = bytes(buf)
    total_len = int(len(raw))
    min_hdr = int(25 * 4)
    if total_len < min_hdr:
        return {"valid": False, "code": "E_XWD_SHORT", "len": total_len}

    fields = tuple(int(v) for v in struct.unpack(">25I", raw[:min_hdr]))
    hs = int(fields[0])
    ver = int(fields[1])
    fmt = int(fields[2])
    depth = int(fields[3])
    w = int(fields[4])
    h = int(fields[5])
    byte_order = int(fields[7])
    bit_order = int(fields[9])
    bpp = int(fields[11])
    bpl = int(fields[12])
    vclass = int(fields[13])
    rmask = int(fields[14])
    gmask = int(fields[15])
    bmask = int(fields[16])
    ncolors = int(fields[19])

    if ver != 7:
        return {"valid": False, "code": "E_XWD_VERSION", "file_version": ver}
    if hs < min_hdr:
        return {"valid": False, "code": "E_XWD_SHORT", "header_size": hs}
    if fmt != 2 or depth != 24 or bpp != 32:
        return {"valid": False, "code": "E_XWD_FORMAT", "format": fmt, "depth": depth, "bpp": bpp}
    if byte_order not in (0, 1) or bit_order not in (0, 1):
        return {"valid": False, "code": "E_XWD_BYTE_ORDER", "byte_order": byte_order, "bit_order": bit_order}
    if vclass not in (4, 5) or (rmask, gmask, bmask) != (0xFF0000, 0x00FF00, 0x0000FF):
        return {"valid": False, "code": "E_XWD_VISUAL", "visual_class": vclass}
    max_side = int(16384)
    min_row_bytes = int(w) * int(4)
    if not (0 < w <= max_side and 0 < h <= max_side) or bpl < min_row_bytes or (bpl % 4) != 0:
        return {"valid": False, "code": "E_XWD_GEOMETRY", "width": w, "height": h, "bytes_per_line": bpl}

    pixels_offset = int(hs) + int(ncolors) * int(12)
    expected_size = int(pixels_offset) + int(h) * int(bpl)
    assert type(pixels_offset) is int and type(expected_size) is int

    if (width is not None and w != int(width)) or (height is not None and h != int(height)):
        return {"valid": False, "code": "E_XWD_GEOMETRY", "width": w, "height": h}
    if total_len != expected_size:
        return {
            "valid": False,
            "code": "E_XWD_SIZE",
            "len": total_len,
            "expected_size": expected_size,
            "pixels_offset": pixels_offset,
        }
    return {
        "valid": True,
        "code": None,
        "header_size": hs,
        "width": w,
        "height": h,
        "depth": depth,
        "bpp": bpp,
        "bytes_per_line": bpl,
        "byte_order": byte_order,
        "ncolors": ncolors,
        "visual_class": vclass,
        "red_mask": rmask,
        "green_mask": gmask,
        "blue_mask": bmask,
        "pixels_offset": pixels_offset,
        "expected_size": expected_size,
    }


def independent_xwd_rgb24_oracle(buf: bytes | bytearray | memoryview) -> bytes:
    """Independent Python-integer RGB24 extraction oracle (handling padding & byte_order)."""
    meta = independent_xwd_extent_oracle(buf)
    if not meta["valid"]:
        raise _xfb.XwdError(str(meta["code"]), "invalid frame in oracle")
    raw = bytes(buf)
    w = int(meta["width"])
    h = int(meta["height"])
    bpl = int(meta["bytes_per_line"])
    off = int(meta["pixels_offset"])
    byte_order = int(meta["byte_order"])

    out = bytearray(w * h * 3)
    out_idx = 0
    for y in range(h):
        row_base = int(off) + int(y) * int(bpl)
        for x in range(w):
            px = int(row_base) + int(x) * 4
            if byte_order == 0:
                out[out_idx] = raw[px + 2]
                out[out_idx + 1] = raw[px + 1]
                out[out_idx + 2] = raw[px]
            else:
                out[out_idx] = raw[px + 1]
                out[out_idx + 1] = raw[px + 2]
                out[out_idx + 2] = raw[px + 3]
            out_idx += 3
    return bytes(out)


def generate_xwd_case(seed: int, case_index: int, *, mode: str = "mixed") -> XwdCase:
    """Deterministically generate an XWD test case from (seed, case_index)."""
    rng = SeedTree(seed).rng_for("xwd", case_index)
    odd_widths = (1, 3, 5, 7, 9, 11, 13, 15, 17, 21)
    even_widths = (2, 4, 6, 8, 12, 16, 20, 24)
    if rng.chance(3, 5):
        w = rng.choice(odd_widths)
    else:
        w = rng.choice(even_widths)
    h = rng.choice((1, 2, 3, 4, 5, 6, 7, 8, 11, 13, 16))
    pad_bytes = rng.choice((0, 4, 8, 12, 16))
    byte_order = rng.choice((0, 1))
    bit_order = rng.choice((0, 1))
    vclass = rng.choice((_xfb.TRUECOLOR, _xfb.DIRECTCOLOR))
    extra_hdr = rng.choice((0, 4, 8, 16))
    hs = _xfb.MIN_HEADER + extra_hdr
    ncolors = rng.choice((0, 1, 2, 3))
    bpl = w * 4 + pad_bytes
    rgb = rng.uint8_rgb(h, w)

    if mode == "valid":
        fault_kind = "valid"
    elif mode == "invalid":
        fault_kind = rng.choice((
            "short_header",
            "bad_version",
            "short_header_field",
            "bad_format",
            "bad_byte_order",
            "bad_visual",
            "invalid_extent",
            "bad_bpl",
            "geometry_mismatch",
            "short_buffer",
            "long_buffer",
        ))
    elif mode == "mixed":
        if rng.chance(1, 2):
            fault_kind = "valid"
        else:
            fault_kind = rng.choice((
                "short_header",
                "bad_version",
                "short_header_field",
                "bad_format",
                "bad_byte_order",
                "bad_visual",
                "invalid_extent",
                "bad_bpl",
                "geometry_mismatch",
                "short_buffer",
                "long_buffer",
            ))
    else:
        fault_kind = mode

    ver = _xfb.XWD_VERSION
    fmt = _xfb.ZPIXMAP
    depth = 24
    bpp = 32
    rmask, gmask, bmask = 0xFF0000, 0x00FF00, 0x0000FF
    size_delta = 0
    exp_w: int | None = w
    exp_h: int | None = h
    short_raw_len: int | None = None

    if fault_kind == "short_header":
        short_raw_len = rng.randint(0, _xfb.MIN_HEADER - 1)
    elif fault_kind == "bad_version":
        ver = rng.choice((0, 6, 8, 99))
    elif fault_kind == "short_header_field":
        hs = rng.randint(0, _xfb.MIN_HEADER - 1)
    elif fault_kind == "bad_format":
        which = rng.randint(0, 2)
        if which == 0:
            fmt = rng.choice((0, 1, 3))
        elif which == 1:
            depth = rng.choice((8, 16, 32))
        else:
            bpp = rng.choice((16, 24, 64))
    elif fault_kind == "bad_byte_order":
        if rng.chance(1, 2):
            byte_order = rng.choice((2, 3, 255))
        else:
            bit_order = rng.choice((2, 3, 255))
    elif fault_kind == "bad_visual":
        if rng.chance(1, 2):
            vclass = rng.choice((0, 1, 2, 3, 6))
        else:
            rmask = 0x0000FF
    elif fault_kind == "invalid_extent":
        choice_kind = rng.randint(0, 3)
        if choice_kind == 0:
            w = 0
        elif choice_kind == 1:
            h = 0
        elif choice_kind == 2:
            w = _xfb.MAX_SIDE + rng.randint(1, 32)
            bpl = w * 4 + pad_bytes
        else:
            h = _xfb.MAX_SIDE + rng.randint(1, 32)
    elif fault_kind == "bad_bpl":
        if rng.chance(1, 2) and w >= 1:
            bpl = max(0, w * 4 - 4)
        else:
            bpl = w * 4 + rng.choice((1, 2, 3))
    elif fault_kind == "geometry_mismatch":
        if rng.chance(1, 2):
            exp_w = w + rng.randint(1, 5)
        else:
            exp_h = h + rng.randint(1, 5)
    elif fault_kind == "short_buffer":
        max_cut = max(1, min(32, h * bpl))
        size_delta = -rng.randint(1, max_cut)
    elif fault_kind == "long_buffer":
        size_delta = rng.randint(1, 24)
    elif fault_kind != "valid":
        raise ValueError(f"Unknown XWD fault_kind: {fault_kind}")

    buf = build_xwd_buffer(
        rgb,
        header_size=hs,
        file_version=ver,
        pixmap_format=fmt,
        depth=depth,
        width=w,
        height=h,
        byte_order=byte_order,
        bitmap_bit_order=bit_order,
        bpp=bpp,
        bytes_per_line=bpl,
        visual_class=vclass,
        red_mask=rmask,
        green_mask=gmask,
        blue_mask=bmask,
        ncolors=ncolors,
        pad_bytes=pad_bytes,
        size_delta=size_delta,
        short_raw_len=short_raw_len,
    )
    return XwdCase(
        seed=seed,
        case_index=case_index,
        header_size=hs,
        file_version=ver,
        pixmap_format=fmt,
        depth=depth,
        width=w,
        height=h,
        byte_order=byte_order,
        bitmap_bit_order=bit_order,
        bpp=bpp,
        bytes_per_line=bpl,
        visual_class=vclass,
        red_mask=rmask,
        green_mask=gmask,
        blue_mask=bmask,
        ncolors=ncolors,
        pad_bytes=pad_bytes,
        size_delta=size_delta,
        expected_width=exp_w,
        expected_height=exp_h,
        rgb=rgb,
        buf=buf,
        fault_kind=fault_kind,
    )


@dataclass(frozen=True)
class MotionBandCase:
    """Synthetic luma motion-band and tile-consensus test case in RAM."""

    seed: int
    case_index: int
    height: int
    width: int
    tile_side: int
    max_d: int
    dx: int
    dy: int
    top_chrome: int
    bottom_chrome: int
    left_chrome: int
    right_chrome: int
    prev_luma: np.ndarray
    cur_luma: np.ndarray
    pattern: str
    raw_dx: object = None
    raw_dy: object = None
    raw_s: object = None
    raw_max_d: object = None


def build_motion_luma_pair(
    base_luma: np.ndarray,
    exposed_luma: np.ndarray,
    *,
    dx: int,
    dy: int,
    top_chrome: int = 0,
    bottom_chrome: int = 0,
    left_chrome: int = 0,
    right_chrome: int = 0,
    pattern: str = "scrolled_band",
) -> tuple[np.ndarray, np.ndarray]:
    """Construct prev_luma and cur_luma in RAM from explicit band parameters."""
    h, w = base_luma.shape
    if pattern == "flat":
        val = int(base_luma[0, 0]) if base_luma.size else 128
        prev = np.full((h, w), val, dtype=np.uint8)
        cur = np.full((h, w), val, dtype=np.uint8)
        return prev, cur
    if pattern == "static_textured":
        prev = base_luma.copy()
        if prev.size >= 2 and int(prev.min()) == int(prev.max()):
            prev.flat[0] = np.uint8((int(prev.flat[0]) + 73) & 0xFF)
        cur = prev.copy()
        return prev, cur

    prev = base_luma.copy()
    if prev.size >= 2 and int(prev.min()) == int(prev.max()):
        prev.flat[0] = np.uint8((int(prev.flat[0]) + 73) & 0xFF)
    cur = exposed_luma.copy()

    y0 = min(h, max(0, int(top_chrome)))
    y1 = max(y0, h - max(0, int(bottom_chrome)))
    x0 = min(w, max(0, int(left_chrome)))
    x1 = max(x0, w - max(0, int(right_chrome)))

    cur[:y0, :] = prev[:y0, :]
    cur[y1:, :] = prev[y1:, :]
    cur[:, :x0] = prev[:, :x0]
    cur[:, x1:] = prev[:, x1:]

    bh = y1 - y0
    bw = x1 - x0
    cy0 = max(0, int(dy))
    cy1 = min(bh, bh + int(dy))
    cx0 = max(0, int(dx))
    cx1 = min(bw, bw + int(dx))
    if cy0 < cy1 and cx0 < cx1:
        py0 = cy0 - int(dy)
        py1 = cy1 - int(dy)
        px0 = cx0 - int(dx)
        px1 = cx1 - int(dx)
        cur[y0 + cy0: y0 + cy1, x0 + cx0: x0 + cx1] = prev[y0 + py0: y0 + py1, x0 + px0: x0 + px1]
    return prev, cur


def independent_motion_extent_oracle(
    height: int,
    width: int,
    dx: int,
    dy: int,
    s: int = 16,
    band: tuple[int, int, int, int] | None = None,
) -> dict[str, object]:
    """Independent Python-integer extent oracle for luma motion and tile grids."""
    require_exact_int(height, "height", min_value=1)
    require_exact_int(width, "width", min_value=1)
    require_exact_int(dx, "dx")
    require_exact_int(dy, "dy")
    require_exact_int(s, "s", min_value=1)

    gh = (int(height) + int(s) - 1) // int(s)
    gw = (int(width) + int(s) - 1) // int(s)
    pad_h = int(gh) * int(s) - int(height)
    pad_w = int(gw) * int(s) - int(width)

    cy0 = max(0, int(dy))
    cy1 = min(int(height), int(height) + int(dy))
    cx0 = max(0, int(dx))
    cx1 = min(int(width), int(width) + int(dx))
    py0 = int(cy0) - int(dy)
    py1 = int(cy1) - int(dy)
    px0 = int(cx0) - int(dx)
    px1 = int(cx1) - int(dx)

    overlap_h = max(0, int(cy1) - int(cy0))
    overlap_w = max(0, int(cx1) - int(cx0))
    overlap_area = int(overlap_h) * int(overlap_w)

    if band is None:
        by0, by1, bx0, bx1 = 0, int(height), 0, int(width)
    else:
        by0, by1, bx0, bx1 = (int(band[0]), int(band[1]), int(band[2]), int(band[3]))
    bh = max(0, int(by1) - int(by0))
    bw = max(0, int(bx1) - int(bx0))
    b_cy0 = max(int(by0), int(by0) + int(dy))
    b_cy1 = min(int(by1), int(by1) + int(dy))
    b_cx0 = max(int(bx0), int(bx0) + int(dx))
    b_cx1 = min(int(bx1), int(bx1) + int(dx))
    min_ov_h = max(16, min(64, int(bh) // 4))
    min_ov_w = max(16, min(64, int(bw) // 4))

    return {
        "gh": gh,
        "gw": gw,
        "pad_h": pad_h,
        "pad_w": pad_w,
        "cy0": cy0,
        "cy1": cy1,
        "cx0": cx0,
        "cx1": cx1,
        "py0": py0,
        "py1": py1,
        "px0": px0,
        "px1": px1,
        "overlap_h": overlap_h,
        "overlap_w": overlap_w,
        "overlap_area": overlap_area,
        "band_overlap": (b_cy0, b_cy1, b_cx0, b_cx1),
        "min_ov_h": min_ov_h,
        "min_ov_w": min_ov_w,
    }


def independent_consensus_oracle(
    prev_luma: np.ndarray,
    cur_luma: np.ndarray,
    s: int,
    dx: int,
    dy: int,
) -> np.ndarray:
    """Independent Python-integer tile consensus oracle."""
    if not all(isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_)) for v in (dx, dy)):
        raise TypeError("displacements must be integers")
    if not isinstance(s, (int, np.integer)) or isinstance(s, (bool, np.bool_)) or int(s) <= 0:
        raise ValueError("tile side must be a positive integer")
    prev = np.asarray(prev_luma)
    cur = np.asarray(cur_luma)
    if prev.dtype != np.uint8 or cur.dtype != np.uint8:
        raise TypeError("luma must be uint8")
    if prev.ndim != 2 or prev.shape != cur.shape or min(prev.shape) == 0:
        raise ValueError("frames must have the same nonempty H by W shape")

    h, w = int(prev.shape[0]), int(prev.shape[1])
    s_i, dx_i, dy_i = int(s), int(dx), int(dy)
    ext = independent_motion_extent_oracle(h, w, dx_i, dy_i, s_i)
    gh, gw = int(ext["gh"]), int(ext["gw"])

    out = np.zeros((gh, gw), dtype=bool)
    for ty in range(gh):
        y0 = int(ty) * s_i
        y1 = min(h, (int(ty) + 1) * s_i)
        for tx in range(gw):
            x0 = int(tx) * s_i
            x1 = min(w, (int(tx) + 1) * s_i)
            changed_in_place = False
            all_translated_equal = True
            for y in range(y0, y1):
                sy = y - dy_i
                for x in range(x0, x1):
                    sx = x - dx_i
                    c_val = int(cur[y, x])
                    if c_val != int(prev[y, x]):
                        changed_in_place = True
                    if not (0 <= sy < h and 0 <= sx < w) or c_val != int(prev[sy, sx]):
                        all_translated_equal = False
            out[ty, tx] = bool(changed_in_place and all_translated_equal)
    return out


def independent_tile_sums_oracle(mask: np.ndarray, s: int = 16) -> np.ndarray:
    """Independent Python-integer tile-sums oracle for _tile_sums(mask, s)."""
    arr = np.asarray(mask)
    h, w = int(arr.shape[0]), int(arr.shape[1])
    s_i = require_exact_int(int(s), "s", min_value=1)
    gh = (h + s_i - 1) // s_i
    gw = (w + s_i - 1) // s_i
    out = np.zeros((gh, gw), dtype=np.int64)
    for ty in range(gh):
        y0 = ty * s_i
        y1 = min(h, (ty + 1) * s_i)
        for tx in range(gw):
            x0 = tx * s_i
            x1 = min(w, (tx + 1) * s_i)
            total = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    total += int(arr[y, x])
            out[ty, tx] = total
    return out


def _axis_candidates_and_ops_oracle(cur_1d: np.ndarray, prev_1d: np.ndarray,
                                    max_d: int, minimum: int, keep: int) -> tuple[tuple[int, ...], int]:
    """Independent Python-integer OPS and candidate oracle for retained_axis_candidates."""
    length = int(len(cur_1d))
    ops = 0
    raw_records = []
    bound = min(int(max_d), length - 1)
    for shift in range(-bound, bound + 1):
        lo = max(0, int(shift))
        hi = min(length, length + int(shift))
        if hi - lo >= int(minimum):
            ops += (int(hi) - int(lo)) * 2
            raw_sad = int(np.abs(cur_1d[lo:hi] - prev_1d[lo - shift: hi - shift]).sum(dtype=np.int64))
            raw_records.append((raw_sad * length // (hi - lo), abs(shift), int(shift)))
    raw_records.sort()
    selected = {r[2] for r in raw_records[: int(keep)]}
    selected.add(0)
    if length >= 8:
        ops += (length - 8 + 1) * 8 * 2
        kernel = np.ones(8, dtype=np.int64)
        cs = np.convolve(cur_1d, kernel, mode="valid")
        ps = np.convolve(prev_1d, kernel, mode="valid")
        s_len = int(len(cs))
        s_min = max(1, int(minimum) - 7)
        s_bound = min(int(max_d), s_len - 1)
        smooth_records = []
        for shift in range(-s_bound, s_bound + 1):
            lo = max(0, int(shift))
            hi = min(s_len, s_len + int(shift))
            if hi - lo >= s_min:
                ops += (int(hi) - int(lo)) * 2
                s_sad = int(np.abs(cs[lo:hi] - ps[lo - shift: hi - shift]).sum(dtype=np.int64))
                smooth_records.append((s_sad * s_len // (hi - lo), abs(shift), int(shift)))
        smooth_records.sort()
        coarse = [r for r in smooth_records if r[2] % 8 == 0][:8]
        for center in coarse:
            basin = [r for r in raw_records if abs(r[2] - center[2]) < 8]
            selected.update(r[2] for r in basin[:1])
    return tuple(sorted(selected)), int(ops)


def independent_projection_pairs_ops_oracle(
    prev_band: np.ndarray,
    cur_band: np.ndarray,
    max_d: int,
    keep: int = 8,
) -> tuple[set[tuple[int, int]], int]:
    """Independent Python-integer OPS and candidate-pair oracle for _projection_pairs."""
    h, w = int(prev_band.shape[0]), int(prev_band.shape[1])
    if h < 16 or w < 16:
        return set(), 0
    ops = int(h) * int(w) * 2
    cr = cur_band.sum(axis=1, dtype=np.int64)
    pr = prev_band.sum(axis=1, dtype=np.int64)
    min_y = max(16, min(64, h // 4))
    min_x = max(16, min(64, w // 4))
    dys, dy_ops = _axis_candidates_and_ops_oracle(cr, pr, int(max_d), min_y, int(keep))
    ops += int(dy_ops)
    pairs: set[tuple[int, int]] = set()
    for dy in dys:
        lo = max(0, int(dy))
        hi = min(h, h + int(dy))
        if hi - lo < min_y:
            continue
        ops += (int(hi) - int(lo)) * int(w) * 2
        cc = cur_band[lo:hi].sum(axis=0, dtype=np.int64)
        pc = prev_band[lo - dy: hi - dy].sum(axis=0, dtype=np.int64)
        dxs, dx_ops = _axis_candidates_and_ops_oracle(cc, pc, int(max_d), min_x, min(int(keep), 4))
        ops += int(dx_ops)
        for dx in dxs:
            pairs.add((int(dx), int(dy)))
    return pairs, int(ops)


def generate_motion_band_case(seed: int, case_index: int, *, mode: str = "mixed") -> MotionBandCase:
    """Deterministically generate a luma motion-band case from (seed, case_index)."""
    rng = SeedTree(seed).rng_for("motion", case_index)
    widths = (48, 49, 51, 63, 64, 65, 79, 80, 95, 96)
    heights = (48, 49, 51, 63, 64, 65, 79, 80, 96)
    w = rng.choice(widths)
    h = rng.choice(heights)
    s = rng.choice((8, 12, 16))
    top_c = rng.choice((0, 0, 4, 8))
    bot_c = rng.choice((0, 0, 4, 8))
    left_c = rng.choice((0, 0, 4))
    right_c = rng.choice((0, 0, 4))

    dx = rng.choice((-12, -8, -5, -3, 0, 3, 5, 8, 12))
    dy = rng.choice((-12, -8, -5, -3, 0, 3, 5, 8, 12))
    if dx == 0 and dy == 0:
        dy = rng.choice((4, -4, 8))
    max_d = max(abs(dx), abs(dy)) + rng.randint(8, 32)

    base = rng.uint8_luma(h, w)
    exposed = rng.uint8_luma(h, w)

    if mode == "valid":
        pattern = rng.choice(("scrolled_band", "scrolled_band", "static_textured", "flat"))
    elif mode == "invalid":
        pattern = rng.choice((
            "invalid_dtype",
            "invalid_shape",
            "invalid_displacement",
            "invalid_tile_side",
            "invalid_max_d",
        ))
    elif mode == "mixed":
        if rng.chance(3, 5):
            pattern = rng.choice(("scrolled_band", "scrolled_band", "static_textured", "flat"))
        else:
            pattern = rng.choice((
                "invalid_dtype",
                "invalid_shape",
                "invalid_displacement",
                "invalid_tile_side",
                "invalid_max_d",
            ))
    else:
        pattern = mode

    raw_dx: object = dx
    raw_dy: object = dy
    raw_s: object = s
    raw_max_d: object = max_d

    if pattern in ("scrolled_band", "static_textured", "flat"):
        prev, cur = build_motion_luma_pair(
            base,
            exposed,
            dx=dx,
            dy=dy,
            top_chrome=top_c,
            bottom_chrome=bot_c,
            left_chrome=left_c,
            right_chrome=right_c,
            pattern=pattern,
        )
        if pattern == "static_textured":
            dx = dy = 0
            raw_dx = raw_dy = 0
    elif pattern == "invalid_dtype":
        prev, cur = build_motion_luma_pair(base, exposed, dx=dx, dy=dy)
        if rng.chance(1, 2):
            prev = prev.astype(np.int16)
        else:
            cur = cur.astype(np.float32)
    elif pattern == "invalid_shape":
        sub = rng.randint(0, 2)
        if sub == 0:
            prev = rng.uint8_rgb(h, w)
            cur = rng.uint8_rgb(h, w)
        elif sub == 1:
            prev = base
            cur = exposed[:, :-1]
        else:
            prev = np.zeros((0, w), dtype=np.uint8)
            cur = np.zeros((0, w), dtype=np.uint8)
    elif pattern == "invalid_displacement":
        prev, cur = build_motion_luma_pair(base, exposed, dx=dx, dy=dy)
        if rng.chance(1, 2):
            raw_dx = True
        else:
            raw_dy = 1.5
    elif pattern == "invalid_tile_side":
        prev, cur = build_motion_luma_pair(base, exposed, dx=dx, dy=dy)
        raw_s = rng.choice((True, 0, -4, 8.5))
    elif pattern == "invalid_max_d":
        prev, cur = build_motion_luma_pair(base, exposed, dx=dx, dy=dy)
        raw_max_d = rng.choice((True, -1, 2.5))
    else:
        raise ValueError(f"Unknown motion pattern: {pattern}")

    return MotionBandCase(
        seed=seed,
        case_index=case_index,
        height=h,
        width=w,
        tile_side=s,
        max_d=max_d,
        dx=dx,
        dy=dy,
        top_chrome=top_c,
        bottom_chrome=bot_c,
        left_chrome=left_c,
        right_chrome=right_c,
        prev_luma=prev,
        cur_luma=cur,
        pattern=pattern,
        raw_dx=raw_dx,
        raw_dy=raw_dy,
        raw_s=raw_s,
        raw_max_d=raw_max_d,
    )


@dataclass(frozen=True)
class FrameCase:
    """Synthetic RAM RGB frame ownership and postpass precedence test case."""

    seed: int
    case_index: int
    width: int
    height: int
    tile_side: int
    rgb_frames: tuple[np.ndarray, ...]
    baseline: dict[str, np.ndarray]
    claims: tuple[dict[str, object], ...]
    flicker_veto: np.ndarray | None
    opt_in_veto: bool
    frame_ids: tuple[object, ...]
    fault_kind: str


def independent_precedence_oracle(
    baseline: dict[str, np.ndarray],
    claims: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    flicker_veto: np.ndarray | None = None,
    opt_in_veto: bool = False,
) -> dict[str, np.ndarray]:
    """Independent Python-integer / numpy oracle for postpass claim & veto precedence."""
    cls_in = np.asarray(baseline["cls"], dtype=np.int64).copy()
    dx_in = np.asarray(baseline["mv_dx"], dtype=np.int64).copy()
    dy_in = np.asarray(baseline["mv_dy"], dtype=np.int64).copy()
    n = int(cls_in.shape[0])

    if flicker_veto is not None:
        if (
            not isinstance(flicker_veto, np.ndarray)
            or flicker_veto.dtype != np.bool_
            or flicker_veto.shape != cls_in.shape
        ):
            raise ValueError("flicker_veto must be a boolean baseline-shaped array")
        for i in range(n):
            if bool(flicker_veto[i]) and int(cls_in[i]) == 4:
                cls_in[i] = 2

    effective_claims = []
    if opt_in_veto:
        occupied = [False] * n
        for c in claims:
            raw_mask = np.asarray(c["mask"], dtype=bool)
            masked = np.array([bool(raw_mask[i]) and not occupied[i] for i in range(n)], dtype=bool)
            for i in range(n):
                if bool(raw_mask[i]):
                    occupied[i] = True
            effective_claims.append({
                "mask": masked,
                "cls": int(c["cls"]),
                "mv_dx": np.asarray(c["mv_dx"], dtype=np.int64),
                "mv_dy": np.asarray(c["mv_dy"], dtype=np.int64),
            })
    else:
        for c in claims:
            effective_claims.append({
                "mask": np.asarray(c["mask"], dtype=bool),
                "cls": int(c["cls"]),
                "mv_dx": np.asarray(c["mv_dx"], dtype=np.int64),
                "mv_dy": np.asarray(c["mv_dy"], dtype=np.int64),
            })

    for c in effective_claims:
        mask = c["mask"]
        c_cls = int(c["cls"])
        c_dx = c["mv_dx"]
        c_dy = c["mv_dy"]
        for i in range(n):
            if bool(mask[i]):
                cls_in[i] = c_cls
                dx_in[i] = int(c_dx[i])
                dy_in[i] = int(c_dy[i])

    return {"cls": cls_in, "mv_dx": dx_in, "mv_dy": dy_in}


def generate_frame_case(seed: int, case_index: int, *, mode: str = "mixed") -> FrameCase:
    """Deterministically generate a synthetic RAM RGB frame & precedence case."""
    rng = SeedTree(seed).rng_for("frame", case_index)
    widths = (15, 16, 17, 23, 31, 32, 33, 48)
    heights = (15, 16, 17, 24, 32, 33)
    w = rng.choice(widths)
    h = rng.choice(heights)
    s = 16
    gh = (h + s - 1) // s
    gw = (w + s - 1) // s
    n_tiles = gh * gw

    f0 = rng.uint8_rgb(h, w)
    f1 = rng.uint8_rgb(h, w)
    f2 = rng.uint8_rgb(h, w)

    base_cls = np.array([rng.choice((0, 2, 3, 4)) for _ in range(n_tiles)], dtype=np.int64)
    base_dx = np.array([rng.randint(-4, 4) for _ in range(n_tiles)], dtype=np.int64)
    base_dy = np.array([rng.randint(-4, 4) for _ in range(n_tiles)], dtype=np.int64)
    baseline = {"cls": base_cls, "mv_dx": base_dx, "mv_dy": base_dy}

    gm_mask = np.array([rng.chance(1, 3) for _ in range(n_tiles)], dtype=np.bool_)
    st_mask = np.array([rng.chance(1, 3) for _ in range(n_tiles)], dtype=np.bool_)
    fl_mask = np.array([rng.chance(1, 3) for _ in range(n_tiles)], dtype=np.bool_)
    gm_dx = np.full(n_tiles, rng.randint(-8, 8), dtype=np.int64)
    gm_dy = np.full(n_tiles, rng.randint(-8, 8), dtype=np.int64)
    st_dx = np.array([rng.randint(-6, 6) for _ in range(n_tiles)], dtype=np.int64)
    st_dy = np.array([rng.randint(-6, 6) for _ in range(n_tiles)], dtype=np.int64)
    claims = (
        {"mask": gm_mask, "cls": 3, "mv_dx": gm_dx, "mv_dy": gm_dy},
        {"mask": st_mask, "cls": 3, "mv_dx": st_dx, "mv_dy": st_dy},
        {"mask": fl_mask, "cls": 4, "mv_dx": np.zeros(n_tiles, dtype=np.int64), "mv_dy": np.zeros(n_tiles, dtype=np.int64)},
    )
    veto = np.array([rng.chance(1, 2) for _ in range(n_tiles)], dtype=np.bool_)
    opt_in = rng.chance(1, 2)
    frame_ids: tuple[object, ...] = (0, 1, 2) if rng.chance(1, 2) else (0, 1, 5)

    if mode == "valid":
        fault_kind = "valid"
    elif mode == "invalid":
        fault_kind = rng.choice(("bad_veto_shape", "bad_veto_dtype", "bad_frame_id_bool", "bad_frame_id_neg"))
    elif mode == "mixed":
        if rng.chance(2, 3):
            fault_kind = "valid"
        else:
            fault_kind = rng.choice(("bad_veto_shape", "bad_veto_dtype", "bad_frame_id_bool", "bad_frame_id_neg"))
    else:
        fault_kind = mode

    if fault_kind == "bad_veto_shape":
        veto = np.ones(n_tiles + 1, dtype=np.bool_)
    elif fault_kind == "bad_veto_dtype":
        veto = np.ones(n_tiles, dtype=np.int32)
    elif fault_kind == "bad_frame_id_bool":
        frame_ids = (0, True, 2)
    elif fault_kind == "bad_frame_id_neg":
        frame_ids = (0, -1, 2)
    elif fault_kind != "valid":
        raise ValueError(f"Unknown frame fault_kind: {fault_kind}")

    return FrameCase(
        seed=seed,
        case_index=case_index,
        width=w,
        height=h,
        tile_side=s,
        rgb_frames=(f0, f1, f2),
        baseline=baseline,
        claims=claims,
        flicker_veto=veto,
        opt_in_veto=opt_in,
        frame_ids=frame_ids,
        fault_kind=fault_kind,
    )


def verify_stream_ownership(case: FrameCase, *, postpass_mod=_postpass) -> dict[str, object]:
    """Verify per-stream snapshot ownership and discontinuous frame_id reset in RAM."""
    grid = _Grid(case.width, case.height)
    veto = postpass_mod.BaselineFlickerVeto(grid)
    pp = postpass_mod.PostPass(grid, accelerated=False, baseline_flicker_veto=True)

    f0 = case.rgb_frames[0].copy()
    tiles0, _ = _desc.describe_padded(grid.pad(f0), grid, lazy_xh=True)
    tiles0 = tiles0.copy()
    tiles0["cls"] = case.baseline["cls"][: grid.N].astype(tiles0["cls"].dtype)

    if case.fault_kind in ("bad_frame_id_bool", "bad_frame_id_neg"):
        veto.update(tiles0, f0, frame_id=case.frame_ids[1])
        return {"valid": False}

    veto.update(tiles0, f0, frame_id=int(case.frame_ids[0]))
    snap_prev_rgb = veto.previous_rgb.copy()
    snap_cur_tiles = veto.current_tiles.copy()
    assert not veto.previous_rgb.flags.writeable
    assert not veto.current_tiles.flags.writeable

    # Mutate caller-owned arrays in place; veto internal state must be unaffected
    f0[...] = 255 - f0
    tiles0["cls"][...] = 0
    if not np.array_equal(veto.previous_rgb, snap_prev_rgb):
        raise AssertionError("BaselineFlickerVeto leaked caller RGB buffer ownership")
    if not np.array_equal(veto.current_tiles, snap_cur_tiles):
        raise AssertionError("BaselineFlickerVeto leaked caller tile buffer ownership")

    # Step through frame_ids on PostPass and verify discontinuous stream reset
    for idx, fid in enumerate(case.frame_ids):
        rgb_i = case.rgb_frames[idx % len(case.rgb_frames)].copy()
        t_i, _ = _desc.describe_padded(grid.pad(rgb_i), grid, lazy_xh=True)
        t_i = t_i.copy()
        t_i["cls"] = case.baseline["cls"][: grid.N].astype(t_i["cls"].dtype)
        pp.update(t_i, rgb_i, frame_key=f"k{idx}", frame_id=int(fid))

    expected_discontinuous = (int(case.frame_ids[-1]) != int(case.frame_ids[-2]) + 1)
    return {
        "valid": True,
        "discontinuous": bool(pp.veto.discontinuous),
        "expected_discontinuous": expected_discontinuous,
        "history_len": len(pp.veto.history),
        "last_frame_id": int(pp.veto.last_frame_id),
    }


def replay_case(domain: str, seed: int, case_index: int, *, mode: str = "mixed"):
    """Exact O(1) replay of any generated case by (domain, seed, case_index)."""
    require_exact_int(seed, "seed", min_value=0, max_value=MASK64)
    require_exact_int(case_index, "case_index", min_value=0, max_value=MASK64)
    if domain == "xwd":
        return generate_xwd_case(seed, case_index, mode=mode)
    if domain == "motion":
        return generate_motion_band_case(seed, case_index, mode=mode)
    if domain == "frame":
        return generate_frame_case(seed, case_index, mode=mode)
    raise ValueError(f"Unknown domain: {domain}")


@dataclass(frozen=True)
class ShrinkResult:
    """Result of hierarchical shrinking with preserved failure signature."""

    original_case: object
    minimal_case: object
    signature: tuple
    steps: int
    visited: int
    terminated: bool


def _xwd_complexity(case: XwdCase) -> tuple[int, int, int, int, int, int]:
    return (
        int(case.rgb.shape[0]) * int(case.rgb.shape[1]),
        int(case.rgb.shape[0]) + int(case.rgb.shape[1]),
        abs(int(case.header_size) - _xfb.MIN_HEADER) + int(case.ncolors),
        abs(int(case.pad_bytes)) + abs(int(case.size_delta)),
        len(case.buf),
        int(np.abs(case.rgb.astype(np.int64)).sum()),
    )


def _xwd_candidates(case: XwdCase) -> list[XwdCase]:
    cands: list[XwdCase] = []
    rh, rw, _ = case.rgb.shape
    target_dims = []
    if rw > 1:
        half_w = max(1, rw // 2)
        target_dims.append((rh, half_w))
        if (rw % 2 == 1) and (half_w % 2 == 0) and half_w + 1 < rw:
            target_dims.append((rh, half_w + 1))
        target_dims.append((rh, 1))
        if rw > 3 and (rw % 2 == 1):
            target_dims.append((rh, 3))
    if rh > 1:
        half_h = max(1, rh // 2)
        target_dims.append((half_h, rw))
        target_dims.append((1, rw))
    for nh, nw in target_dims:
        sub_rgb = case.rgb[:nh, :nw].copy()
        new_w = nw if case.width == rw else case.width
        new_h = nh if case.height == rh else case.height
        if case.bytes_per_line == rw * 4 + case.pad_bytes:
            new_bpl = new_w * 4 + case.pad_bytes
        elif case.fault_kind == "bad_bpl" and case.bytes_per_line < rw * 4:
            new_bpl = max(0, new_w * 4 - 4)
        elif case.fault_kind == "bad_bpl" and (case.bytes_per_line % 4 != 0):
            new_bpl = new_w * 4 + (case.bytes_per_line % 4)
        else:
            new_bpl = case.bytes_per_line
        exp_w = new_w if case.expected_width == rw else (new_w + 1 if case.fault_kind == "geometry_mismatch" and case.expected_width != rw else case.expected_width)
        exp_h = new_h if case.expected_height == rh else (new_h + 1 if case.fault_kind == "geometry_mismatch" and case.expected_height != rh else case.expected_height)
        cands.append(rebuild_xwd_case(replace(
            case,
            width=new_w,
            height=new_h,
            bytes_per_line=new_bpl,
            expected_width=exp_w,
            expected_height=exp_h,
            rgb=sub_rgb,
        )))

    if case.ncolors > 0:
        cands.append(rebuild_xwd_case(replace(case, ncolors=0)))
    if case.header_size > _xfb.MIN_HEADER:
        cands.append(rebuild_xwd_case(replace(case, header_size=_xfb.MIN_HEADER)))
    if case.pad_bytes > 0:
        new_bpl = case.bytes_per_line - case.pad_bytes if case.bytes_per_line >= case.width * 4 + case.pad_bytes else case.bytes_per_line
        cands.append(rebuild_xwd_case(replace(case, pad_bytes=0, bytes_per_line=new_bpl)))
    if case.size_delta < -1:
        cands.append(rebuild_xwd_case(replace(case, size_delta=-1)))
    elif case.size_delta > 1:
        cands.append(rebuild_xwd_case(replace(case, size_delta=1)))

    if int(np.abs(case.rgb.astype(np.int64)).sum()) > 0:
        zero_rgb = np.zeros_like(case.rgb)
        cands.append(rebuild_xwd_case(replace(case, rgb=zero_rgb)))
    return cands


def _motion_complexity(case: MotionBandCase) -> tuple[int, int, int, int, int]:
    return (
        int(case.height) * int(case.width),
        int(case.top_chrome) + int(case.bottom_chrome) + int(case.left_chrome) + int(case.right_chrome),
        abs(int(case.dx)) + abs(int(case.dy)),
        int(case.max_d),
        int(np.abs(case.prev_luma.astype(np.int64)).sum()) if case.prev_luma.size else 0,
    )


def _motion_candidates(case: MotionBandCase) -> list[MotionBandCase]:
    cands: list[MotionBandCase] = []
    h, w = case.height, case.width
    min_dim = 32 if case.pattern == "scrolled_band" else 16
    for nh, nw in ((max(min_dim, h // 2), w), (h, max(min_dim, w // 2)), (min_dim, min_dim)):
        if (nh, nw) != (h, w) and case.prev_luma.ndim == 2 and case.cur_luma.ndim == 2:
            if case.prev_luma.shape[0] >= nh and case.prev_luma.shape[1] >= nw:
                p_sub = case.prev_luma[:nh, :nw].copy()
                if case.cur_luma.shape == case.prev_luma.shape:
                    c_sub = case.cur_luma[:nh, :nw].copy()
                else:
                    c_sub = case.cur_luma[:nh, : max(1, nw - 1)].copy()
                cands.append(replace(case, height=nh, width=nw, prev_luma=p_sub, cur_luma=c_sub))

    if case.top_chrome or case.bottom_chrome or case.left_chrome or case.right_chrome:
        if case.prev_luma.ndim == 2 and case.cur_luma.shape == case.prev_luma.shape and case.prev_luma.dtype == np.uint8 and case.cur_luma.dtype == np.uint8:
            p2, c2 = build_motion_luma_pair(
                case.prev_luma,
                case.cur_luma,
                dx=case.dx,
                dy=case.dy,
                top_chrome=0,
                bottom_chrome=0,
                left_chrome=0,
                right_chrome=0,
                pattern=case.pattern if case.pattern in ("scrolled_band", "static_textured", "flat") else "scrolled_band",
            )
            cands.append(replace(
                case,
                top_chrome=0,
                bottom_chrome=0,
                left_chrome=0,
                right_chrome=0,
                prev_luma=p2,
                cur_luma=c2,
            ))

    min_max_d = max(1, abs(case.dx), abs(case.dy))
    if case.max_d > min_max_d:
        cands.append(replace(case, max_d=min_max_d, raw_max_d=min_max_d if type(case.raw_max_d) is int else case.raw_max_d))
    return cands


def _frame_complexity(case: FrameCase) -> tuple[int, int, int]:
    return (
        int(case.height) * int(case.width),
        int(case.baseline["cls"].shape[0]),
        int(np.abs(case.baseline["mv_dx"]).sum() + np.abs(case.baseline["mv_dy"]).sum()),
    )


def _frame_candidates(case: FrameCase) -> list[FrameCase]:
    cands: list[FrameCase] = []
    if case.height > 16 or case.width > 16:
        nh = 16 if case.height > 16 else case.height
        nw = 15 if (case.width % 2 == 1 and case.width > 15) else (16 if case.width > 16 else case.width)
        gh = (nh + case.tile_side - 1) // case.tile_side
        gw = (nw + case.tile_side - 1) // case.tile_side
        n_tiles = gh * gw
        sub_frames = tuple(f[:nh, :nw].copy() for f in case.rgb_frames)
        sub_base = {k: v[:n_tiles].copy() for k, v in case.baseline.items()}
        sub_claims = tuple(
            {
                "mask": c["mask"][:n_tiles].copy(),
                "cls": c["cls"],
                "mv_dx": c["mv_dx"][:n_tiles].copy(),
                "mv_dy": c["mv_dy"][:n_tiles].copy(),
            }
            for c in case.claims
        )
        if case.fault_kind == "bad_veto_shape":
            sub_veto = np.ones(n_tiles + 1, dtype=np.bool_)
        elif case.flicker_veto is not None:
            sub_veto = case.flicker_veto[:n_tiles].copy()
        else:
            sub_veto = None
        cands.append(replace(
            case,
            width=nw,
            height=nh,
            rgb_frames=sub_frames,
            baseline=sub_base,
            claims=sub_claims,
            flicker_veto=sub_veto,
        ))
    if int(np.abs(case.baseline["mv_dx"]).sum() + np.abs(case.baseline["mv_dy"]).sum()) > 0:
        n_tiles = int(case.baseline["cls"].shape[0])
        zero_base = {
            "cls": case.baseline["cls"].copy(),
            "mv_dx": np.zeros(n_tiles, dtype=np.int64),
            "mv_dy": np.zeros(n_tiles, dtype=np.int64),
        }
        cands.append(replace(case, baseline=zero_base))
    return cands


def hierarchical_shrink(
    case: XwdCase | MotionBandCase | FrameCase,
    predicate: Callable[[object], tuple | None],
    *,
    max_steps: int = 128,
) -> ShrinkResult:
    """Hierarchically shrink a failing case while preserving its exact failure signature."""
    require_exact_int(max_steps, "max_steps", min_value=1)
    signature = predicate(case)
    if signature is None:
        raise ValueError("hierarchical_shrink requires a failing initial case (predicate returned None)")

    if isinstance(case, XwdCase):
        complexity_fn = _xwd_complexity
        candidates_fn = _xwd_candidates
    elif isinstance(case, MotionBandCase):
        complexity_fn = _motion_complexity
        candidates_fn = _motion_candidates
    elif isinstance(case, FrameCase):
        complexity_fn = _frame_complexity
        candidates_fn = _frame_candidates
    else:
        raise TypeError(f"Unsupported case type for shrinking: {type(case).__name__}")

    current = case
    current_key = complexity_fn(current)
    steps = 0
    visited = 0

    while steps < max_steps:
        improved = False
        for cand in candidates_fn(current):
            cand_key = complexity_fn(cand)
            if cand_key >= current_key:
                continue
            visited += 1
            if predicate(cand) == signature:
                current = cand
                current_key = cand_key
                steps += 1
                improved = True
                break
        if not improved:
            break

    return ShrinkResult(
        original_case=case,
        minimal_case=current,
        signature=signature,
        steps=steps,
        visited=visited,
        terminated=True,
    )


def evaluate_xwd_case(
    case: XwdCase,
    *,
    parse_header_fn=_xfb.parse_header,
    validate_frame_fn=_xfb.validate_frame,
    rgb24_fn=_xfb.rgb24,
) -> tuple | None:
    """Compare producer XWD parsing/validation/RGB24 against the independent oracle."""
    oracle = independent_xwd_extent_oracle(
        case.buf,
        width=case.expected_width,
        height=case.expected_height,
    )
    try:
        hd = validate_frame_fn(case.buf, width=case.expected_width, height=case.expected_height)
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        if not oracle["valid"] and code == oracle["code"]:
            return ("invalidity", type(exc).__name__, str(code))
        return ("oracle_disagreement", "unexpected_exception", str(code), str(oracle["code"]))

    if not oracle["valid"]:
        return ("oracle_disagreement", "accepted_invalid", str(oracle["code"]))

    parsed = parse_header_fn(case.buf)
    if (
        int(hd.width) != int(oracle["width"])
        or int(hd.height) != int(oracle["height"])
        or int(hd.bytes_per_line) != int(oracle["bytes_per_line"])
        or int(hd.pixels_offset) != int(oracle["pixels_offset"])
        or int(hd.size) != int(oracle["expected_size"])
        or parsed != hd
    ):
        return ("oracle_disagreement", "header_extent_mismatch")

    got_rgb = rgb24_fn(case.buf)
    want_rgb = independent_xwd_rgb24_oracle(case.buf)
    if got_rgb != want_rgb:
        return ("oracle_disagreement", "rgb24_mismatch")
    return None


def evaluate_motion_case(
    case: MotionBandCase,
    *,
    estimate_fn=_gm_bands_hv.estimate_global_motion,
    consensus_fn=_gm_bands_hv.consensus_tiles,
    tile_sums_fn=_gm_bands_hv._tile_sums,
) -> tuple | None:
    """Compare producer motion/consensus/tile_sums against the independent oracle."""
    dx_arg = case.dx if case.raw_dx is None else case.raw_dx
    dy_arg = case.dy if case.raw_dy is None else case.raw_dy
    s_arg = case.tile_side if case.raw_s is None else case.raw_s
    max_d_arg = case.max_d if case.raw_max_d is None else case.raw_max_d

    if case.pattern.startswith("invalid_"):
        try:
            if case.pattern in ("invalid_displacement", "invalid_tile_side"):
                consensus_fn(case.prev_luma, case.cur_luma, s_arg, dx_arg, dy_arg)
            else:
                estimate_fn(case.prev_luma, case.cur_luma, max_d_arg)
        except (TypeError, ValueError) as exc:
            return ("invalidity", type(exc).__name__, str(exc))
        except Exception as exc:
            return ("oracle_disagreement", "wrong_exception", type(exc).__name__)
        return ("oracle_disagreement", "accepted_invalid", case.pattern)

    try:
        want_mask = independent_consensus_oracle(case.prev_luma, case.cur_luma, int(s_arg), int(dx_arg), int(dy_arg))
        got_mask = consensus_fn(case.prev_luma, case.cur_luma, int(s_arg), int(dx_arg), int(dy_arg))
    except Exception as exc:
        return ("oracle_disagreement", "consensus_exception", type(exc).__name__)

    if not isinstance(got_mask, np.ndarray) or got_mask.dtype != np.bool_ or not np.array_equal(got_mask, want_mask):
        return ("oracle_disagreement", "consensus_mismatch", case.pattern)

    diff_mask = (case.cur_luma != case.prev_luma)
    want_sums = independent_tile_sums_oracle(diff_mask, int(s_arg))
    got_sums = tile_sums_fn(diff_mask, int(s_arg))
    if not np.array_equal(got_sums, want_sums):
        return ("oracle_disagreement", "tile_sums_mismatch", case.pattern)

    est = estimate_fn(case.prev_luma, case.cur_luma, int(max_d_arg))
    if case.pattern == "flat":
        if est is not None:
            return ("oracle_disagreement", "flat_not_none", est)
    elif case.pattern == "static_textured":
        if est != (0, 0, 0):
            return ("oracle_disagreement", "static_not_zero", est)
    elif est is not None:
        if len(est) != 3 or not all(type(v) is int for v in est):
            return ("oracle_disagreement", "non_int_tuple", est)
        if abs(est[0]) > int(max_d_arg) or abs(est[1]) > int(max_d_arg):
            return ("oracle_disagreement", "exceeds_max_d", est)
    return None


def evaluate_frame_case(
    case: FrameCase,
    *,
    apply_claims_fn=_postpass.apply_claims,
) -> tuple | None:
    """Compare producer claim/veto precedence against the independent oracle."""
    if case.fault_kind in ("bad_veto_shape", "bad_veto_dtype"):
        try:
            apply_claims_fn(case.baseline, list(case.claims), flicker_veto=case.flicker_veto)
        except ValueError as exc:
            return ("invalidity", "ValueError", str(exc))
        except Exception as exc:
            return ("oracle_disagreement", "wrong_exception", type(exc).__name__)
        return ("oracle_disagreement", "accepted_invalid", case.fault_kind)

    if case.fault_kind in ("bad_frame_id_bool", "bad_frame_id_neg"):
        bad_id = case.frame_ids[1]
        if isinstance(bad_id, (bool, np.bool_)) or not isinstance(bad_id, (int, np.integer)) or bad_id < 0:
            return ("invalidity", "ValueError", "frame_id must be a nonnegative integer")

    effective = list(case.claims)
    if case.opt_in_veto:
        n = int(case.baseline["cls"].shape[0])
        occupied = np.zeros(n, dtype=np.bool_)
        adjusted = []
        for c in effective:
            adjusted.append(dict(c, mask=c["mask"] & ~occupied))
            occupied |= c["mask"]
        effective = adjusted

    caller_copy = {k: v.copy() for k, v in case.baseline.items()}
    got = apply_claims_fn(caller_copy, effective, flicker_veto=case.flicker_veto)
    want = independent_precedence_oracle(
        case.baseline,
        case.claims,
        flicker_veto=case.flicker_veto,
        opt_in_veto=case.opt_in_veto,
    )
    for k in ("cls", "mv_dx", "mv_dy"):
        if not np.array_equal(caller_copy[k], case.baseline[k]):
            return ("oracle_disagreement", "mutated_baseline_input", k)
        if not np.array_equal(got[k], want[k]):
            return ("oracle_disagreement", "precedence_mismatch", k)
    return None


class FramePropertyFuzzer:
    """High-level coordinator for SplitMix64 generation, replay, and shrinking."""

    def __init__(self, seed: int) -> None:
        self.seed = require_exact_int(seed, "seed", min_value=0, max_value=MASK64)
        self.tree = SeedTree(self.seed)

    def generate(self, domain: str, case_index: int, *, mode: str = "mixed"):
        return replay_case(domain, self.seed, case_index, mode=mode)

    def replay(self, domain: str, case_index: int, *, mode: str = "mixed"):
        return replay_case(domain, self.seed, case_index, mode=mode)

    def shrink(self, case, predicate: Callable[[object], tuple | None], *, max_steps: int = 128) -> ShrinkResult:
        return hierarchical_shrink(case, predicate, max_steps=max_steps)


class _InMemoryRingWriter:
    """In-RAM ring writer seam for testing RamCapture sequence bounds offline."""

    def __init__(self, ws, slots: int, width: int, height: int) -> None:
        self.ws = ws
        self.slots = int(slots)
        self.width = int(width)
        self.height = int(height)
        self.index = -1
        self.path = "/dev/shm/fuzzer_ws/ring/frames.rgb24"
        self._mm = SimpleNamespace(closed=False, close=self._close_mm)
        self._slots_data: dict[int, tuple[int, np.ndarray]] = {}

    def _close_mm(self) -> None:
        self._mm.closed = True

    def publish(self, frame: np.ndarray, render_index: int | None = None) -> int:
        arr = np.asarray(frame)
        if arr.dtype != np.uint8 or arr.shape != (self.height, self.width, 3):
            raise ValueError("invalid frame shape or dtype")
        r = self.index + 1 if render_index is None else int(render_index)
        self.index = r
        view = arr.copy()
        view.flags.writeable = False
        self._slots_data[r % self.slots] = (r, view)
        return r

    def close(self) -> None:
        self._close_mm()


class _InMemoryRingReader:
    """In-RAM ring reader seam paired with _InMemoryRingWriter."""

    def __init__(self, writer: _InMemoryRingWriter) -> None:
        self._writer = writer
        self._mm = SimpleNamespace(closed=False, close=self._close_mm)

    def _close_mm(self) -> None:
        self._mm.closed = True

    def acquire(self, render_index: int):
        slot = self._writer._slots_data.get(int(render_index) % self._writer.slots)
        if slot is None or slot[0] != int(render_index):
            return None
        return SimpleNamespace(view=slot[1], render_index=int(render_index), valid=lambda: True)

    def close(self) -> None:
        self._close_mm()


def run_capture_wrap_boundary_probe(ramcap_module=_x11_ramcap, *, seed: int = 0x517CC1B727220A95) -> dict[str, object]:
    """Run an offline in-RAM capture wrap-boundary probe at (1<<64)-2 and (1<<64)-1."""
    xwd_case = generate_xwd_case(seed, 0, mode="valid")
    raw_xwd = build_xwd_buffer(
        xwd_case.rgb[:4, :4],
        width=4,
        height=4,
        byte_order=0,
        pad_bytes=0,
    )
    now_ns = [1_000_000]

    def fake_clock() -> int:
        return now_ns[0]

    def fake_wait(event, timeout_ns: int) -> bool:
        now_ns[0] += max(1, int(timeout_ns))
        return False

    class FakeSource:
        def __init__(self, config, max_bytes, tries):
            self.config = config
            self.max_bytes = max_bytes
            self.tries = tries
            self.closed = False

        def read(self, guard):
            guard()
            return raw_xwd

        def check(self):
            if self.closed:
                raise ramcap_module.CaptureError("E_SOURCE_CHANGED", "closed")

        def close(self):
            self.closed = True

    writers: list[_InMemoryRingWriter] = []

    def make_writer(ws, slots, width, height):
        w = _InMemoryRingWriter(ws, slots, width, height)
        writers.append(w)
        return w

    def make_reader(path, root=None):
        return _InMemoryRingReader(writers[-1])

    def fake_ws_factory(session_id: str, size: int):
        return SimpleNamespace(
            root=f"/dev/shm/fuzzer_ws_{session_id}",
            path=lambda rel: f"/dev/shm/fuzzer_ws_{session_id}/{rel}",
            release=lambda p: 0,
            close=lambda: None,
        )

    src_cfg = ramcap_module.SourceConfig(
        "/dev/shm/fuzzer_x11",
        "Xvfb_screen0",
        4,
        4,
        "-nocursor",
        kind="xvfb-xwd",
    )

    with mock.patch.object(ramcap_module, "_Source", FakeSource), \
         mock.patch.object(ramcap_module.RamCapture, "_workspace_root", staticmethod(lambda ws: ws.root)), \
         mock.patch.object(ramcap_module.RamCapture, "_check_workspace", classmethod(lambda cls, ws: None)), \
         mock.patch.object(ramcap_module.rtring, "RingWriter", side_effect=make_writer), \
         mock.patch.object(ramcap_module.rtring, "RingReader", side_effect=make_reader):
        cap = ramcap_module.RamCapture(
            {"screen0": src_cfg},
            max_frame_bytes=4096,
            max_session_bytes=1024 * 1024,
            max_total_bytes=4 * 1024 * 1024,
            max_sessions=2,
            ring_slots=4,
            interval_ns=1000,
            ttl_ns=1_000_000_000,
            tries=4,
            workspace_factory=fake_ws_factory,
            clock=fake_clock,
            wait=fake_wait,
            cancel_factory=threading.Event,
        )
        meta = cap.Start("s1", "screen0", background=False)
        session = cap._sessions["s1"]

        # Position right below the boundary: s.frames = (1 << 64) - 2, writer.index = (1 << 64) - 3
        session.frames = (1 << 64) - 2
        session.writer.index = (1 << 64) - 3
        now_ns[0] += 1000
        penultimate_frame = None
        penultimate_error = None
        try:
            penultimate_frame = cap.capture_once("s1", wait=False)
        except Exception as exc:
            penultimate_error = f"{type(exc).__name__}:{getattr(exc, 'code', exc)}"
        frames_after_penultimate = int(session.frames)

        # Now s.frames == (1 << 64) - 1: next capture_once must raise CaptureError("E_BUDGET", ...)
        now_ns[0] += 1000
        exhausted_code = None
        exhausted_detail = None
        try:
            cap.capture_once("s1", wait=False)
        except Exception as exc:
            if type(exc).__name__ == "CaptureError":
                exhausted_code = getattr(exc, "code", None)
                exhausted_detail = str(exc)
            else:
                exhausted_code = type(exc).__name__
                exhausted_detail = str(exc)

        frames_after_exhaustion = int(session.frames)
        state_after_exhaustion = str(session.state)
        cap.Stop("s1")

    return {
        "initial_frames": int(meta["frames"]),
        "penultimate_render_index": None if penultimate_frame is None else int(penultimate_frame.render_index),
        "penultimate_error": penultimate_error,
        "frames_after_penultimate": frames_after_penultimate,
        "exhausted_code": exhausted_code,
        "exhausted_detail": exhausted_detail,
        "frames_after_exhaustion": frames_after_exhaustion,
        "state_after_exhaustion": state_after_exhaustion,
    }
