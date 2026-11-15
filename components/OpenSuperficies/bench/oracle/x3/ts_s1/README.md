# Oracle AX Query & Action Planning Client (X3 / TS Seed 1)

## Overview
This module provides a read-only Chrome DevTools Protocol (CDP) client over pipe stdio (fd 3 / fd 4) along with pure functions for parsing CDP Accessibility Tree (`getFullAXTree`) and DOM Snapshot (`captureSnapshot`) structures. It implements action planning (`plan_click`) with retry logic, local allow-list enforcement, and sensitive input refusal checks.

## Conventions

### Document vs Viewport Coordinates
- **Document Coordinates (`xy`)**: Calculated from DOMSnapshot layout bounding rects in CSS pixels:
  - `x = bounds[0] + floor(bounds[2] / 2)`
  - `y = bounds[1] + floor(bounds[3] / 2)`
- **Viewport Coordinates (`viewport_xy`)**: Computed relative to current document scroll offset (`scrollOffsetX`, `scrollOffsetY`):
  - `vx = x - scrollOffsetX`
  - `vy = y - scrollOffsetY`

## Open Questions
1. **Multi-frame / Sub-frame Coordinates**: Currently assumes a single document root or single frame context for bounding box offset calculations.
2. **Dynamic Zoom / Pinch**: Does not account for CSS zoom levels or visual viewport pinches outside layout bounds.
