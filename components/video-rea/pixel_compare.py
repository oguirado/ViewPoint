# SPDX-License-Identifier: AGPL-3.0-only
"""Compare packed RGB8 checkpoints within a caller-supplied region."""

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal


@dataclass(frozen=True)
class RGB8Frame:
    """A top-down, row-major RGB raster with three bytes per pixel and no padding."""

    width: int
    height: int
    pixels: bytes

    def __post_init__(self) -> None:
        if type(self.width) is not int or type(self.height) is not int:
            raise ValueError("frame dimensions must be integers")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame dimensions must be positive")
        if type(self.pixels) is not bytes:
            raise ValueError("RGB8 payload must be bytes")
        if len(self.pixels) != self.width * self.height * 3:
            raise ValueError("RGB8 payload length must equal width * height * 3")


@dataclass(frozen=True)
class Region:
    """A rectangle in pixels, with a top-left origin and exclusive right and bottom edges."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.x, self.y, self.width, self.height)):
            raise ValueError("region coordinates and dimensions must be integers")
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("region must have a nonnegative origin and positive area")


@dataclass(frozen=True)
class PixelComparison:
    mismatched_pixels: int
    compared_pixels: int
    mismatch_fraction: Fraction
    verdict: Literal["PASS", "FAIL"]


def compare_checkpoints(
    reference: RGB8Frame,
    candidate: RGB8Frame,
    region: Region,
    *,
    max_mismatch_ppm: int,
    max_channel_delta: int = 0,
) -> PixelComparison:
    """Compare every pixel in region and raise ValueError for invalid inputs.

    A pixel differs when any channel's absolute delta exceeds max_channel_delta
    (0–255, inclusive). The verdict passes at or below max_mismatch_ppm
    (0–1,000,000, inclusive), using the exact fraction of mismatched pixels.
    """
    if type(reference) is not RGB8Frame or type(candidate) is not RGB8Frame:
        raise ValueError("checkpoints must be RGB8Frame instances")
    if (reference.width, reference.height) != (candidate.width, candidate.height):
        raise ValueError("checkpoint dimensions must match")
    if type(region) is not Region:
        raise ValueError("a Region is required")
    right = region.x + region.width
    bottom = region.y + region.height
    if right > reference.width or bottom > reference.height:
        raise ValueError("region must fit within the frame")
    if type(max_mismatch_ppm) is not int or not 0 <= max_mismatch_ppm <= 1_000_000:
        raise ValueError("max_mismatch_ppm must be an integer from 0 to 1000000")
    if type(max_channel_delta) is not int or not 0 <= max_channel_delta <= 255:
        raise ValueError("max_channel_delta must be an integer from 0 to 255")

    reference_rgb = reference.pixels
    candidate_rgb = candidate.pixels
    row_stride = reference.width * 3
    mismatched = 0
    for y in range(region.y, bottom):
        row_start = y * row_stride
        for offset in range(row_start + region.x * 3, row_start + right * 3, 3):
            if any(
                abs(reference_rgb[channel] - candidate_rgb[channel]) > max_channel_delta
                for channel in range(offset, offset + 3)
            ):
                mismatched += 1

    area = region.width * region.height
    verdict = "PASS" if mismatched * 1_000_000 <= max_mismatch_ppm * area else "FAIL"
    return PixelComparison(mismatched, area, Fraction(mismatched, area), verdict)
