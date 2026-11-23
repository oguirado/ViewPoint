"""Decoded RGB metrics with integer arithmetic and explicit pixel denominators."""
from collections import Counter


def uniform_ppm(rgb):
    pixels = [rgb[i:i+3] for i in range(0, len(rgb), 3)]
    return max(Counter(pixels).values()) * 1000000 // len(pixels)


def changed_ppm(before, after, tolerance=0):
    count = sum(any(abs(before[i+c] - after[i+c]) > tolerance for c in range(3))
                for i in range(0, len(before), 3))
    return count * 1000000 // (len(before) // 3)


def noise_ppm(width, height, rgb, threshold=32, luma_weights=(77, 150, 29)):
    # Rec.601 integer luma; edge replication keeps the denominator all pixels.
    denominator = sum(luma_weights)
    luma = [(sum(luma_weights[c]*rgb[i+c] for c in range(3)) + denominator//2) // denominator
            for i in range(0, len(rgb), 3)]
    outliers = 0
    for y in range(height):
        for x in range(width):
            neighbours = sorted(luma[min(height-1, max(0, y+dy))*width +
                                     min(width-1, max(0, x+dx))]
                                for dy in (-1, 0, 1) for dx in (-1, 0, 1))
            outliers += abs(luma[y*width+x] - neighbours[4]) > threshold
    # Ceiling preserves the strict upper bound, including a single sub-ppm defect.
    return (outliers * 1000000 + width * height - 1) // (width * height)
