# Seed s3 Python Client (bench/oracle/x3/py_s3)

## Overview
This directory contains the Python 3 stdlib implementation of an Accessibility Tree (AX) and DOM Snapshot query client with action planning (`plan_click`) over a NUL-framed CDP transport pipe connected to `fake_chromium.py`.

## Scroll Conventions
1. **Document Coordinates**: Center point `(x, y)` calculated as `(rect.left + rect.width // 2, rect.top + rect.height // 2)`.
2. **Viewport Coordinates**: `(cx - scrollOffsetX, cy - scrollOffsetY)` computed using integer arithmetic.
3. **Scroll Offsets**: Extracted directly from `DOMSnapshot.captureSnapshot` document root `scrollOffsetX` and `scrollOffsetY`.

## Open Questions
1. **Cross-Domain Frame Offsets**: How should scroll offsets and bounding rectangles be transformed when element target is located inside a nested cross-origin iframe?
2. **Sub-pixel Coordinate Rounding**: Should sub-pixel bounding box coordinates use floor division `//` or rounding `round()` to match browser hit testing precision?
3. **Actuation Pipelines**: How should external actuation agents handle timing delays between action planning and event dispatch during dynamic page shifts?
