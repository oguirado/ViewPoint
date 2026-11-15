# AX Tree Query and Action Planning Client (JS S1 Seed)

## Overview
This directory contains the JavaScript ES module implementation (`client.mjs`) and test suite (`client.test.mjs`) for a read-only AX tree query and action planning client operating over the CDP pipe transport.

## Scroll Convention
- **Document Coordinates (`xy`)**: Calculated from element layout bounds `[x, y, w, h]` as the integer midpoint:
  - `cx = x + Math.floor(w / 2)`
  - `cy = y + Math.floor(h / 2)`
- **Viewport Coordinates (`viewport` / `viewport_xy`)**: Computed by subtracting the document scroll offsets (`scrollOffsetX` and `scrollOffsetY` from `DOMSnapshot.captureSnapshot` document root) from the document coordinates:
  - `vx = cx - scrollOffsetX`
  - `vy = cy - scrollOffsetY`

## Open Questions
1. **Sub-frame and Element-level Scrolling**: How to handle elements inside nested scrollable containers or iframe elements where individual scroll positions are not reflected in the root document scroll offsets.
2. **Device Pixel Ratio & Zoom**: Whether viewport coordinates should account for pinch-zoom factors or CSS-to-device-pixel scaling in high-DPI rendering contexts.
3. **Sensitivity Pattern Expansion**: Fine-tuning refusal regex patterns for localized/internationalized sensitive field names and attributes across non-English web scenes.
