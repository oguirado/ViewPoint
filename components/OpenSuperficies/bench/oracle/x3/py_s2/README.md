# OpenSuperficies X3 Seed (py_s2)

## Scroll Convention
Coordinates returned by action planning follow standard document coordinate system conventions:
- **Document Coordinates (`xy`)**: Calculated from the element's bounding rect centre point relative to the top-left of the document:
  `center_x = bounds.x + (bounds.w // 2)`
  `center_y = bounds.y + (bounds.h // 2)`
- **Viewport Coordinates (`viewport_xy`)**: Calculated by subtracting scroll offsets from the document coordinates:
  `viewport_x = center_x - scrollOffsetX`
  `viewport_y = center_y - scrollOffsetY`

## Open Questions
1. **Multiple Document Support**: Current snapshot parsing processes documents in order; in multi-frame or multi-document snapshots, document selection heuristics may need clarification if target backendNodeIds collide across frames.
2. **Scroll Offset Source**: DOMSnapshot returns document-level scroll offsets (`scrollOffsetX`, `scrollOffsetY`); if individual element scroll containers are nested, viewport coordinate calculation would require additional layout box scroll offset computations.
