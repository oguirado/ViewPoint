# X3 Python Seed (`py_s1`) CDP Client & Action Planner

## Overview
This module (`client.py`) implements a read-only CDP client over a stdio pipe transport communicating with `fake_chromium.py`. It provides AX tree / DOM Snapshot parsing, local policy refusal checks, and click action planning with retries.

## Scroll Convention
The client computes element coordinates in both document coordinates and viewport coordinates:
- Document coordinates `xy`:
  - `center_x = x + (width // 2)`
  - `center_y = y + (height // 2)`
- Viewport coordinates `viewport_xy`:
  - `viewport_x = center_x - scrollOffsetX`
  - `viewport_y = center_y - scrollOffsetY`

Where `scrollOffsetX` and `scrollOffsetY` are extracted from the primary document (`documents[0]`) in `DOMSnapshot.captureSnapshot`.

## Open Questions
1. **Iframe & Frame Bounding**: Handling cross-origin or nested iframe scroll offsets and local coordinate spaces when target nodes reside inside child frames.
2. **Element Auto-Scroll**: Whether action planning should automatically request CDP `DOM.scrollIntoViewIfNeeded` or similar when target element centers lie outside the visible viewport.
3. **Dynamic Layout & Mutation**: Handling layout shifts or DOM mutations between retry snapshots during action planning.
4. **Multi-document DOMSnapshots**: Resolving layout bounds across multi-document DOMSnapshots when multiple documents are present in the snapshot result.
