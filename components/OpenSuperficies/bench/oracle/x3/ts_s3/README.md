# Oracle X3 TS Seed S3 (`bench/oracle/x3/ts_s3`)

This seed directory provides a read-only Accessibility (AX) tree query, DOM Snapshot parser, and action planning client over the W6a fake CDP pipe (`docker/cu/tests/fixtures/cdp/fake_chromium.py`).

## Scroll Convention

- **Document Coordinates**: Calculated from the element's bounding box `[x, y, width, height]` in the snapshot layout array:
  $$\text{center\_x}_{\text{doc}} = x + \lfloor \frac{\text{width}}{2} \rfloor$$
  $$\text{center\_y}_{\text{doc}} = y + \lfloor \frac{\text{height}}{2} \rfloor$$
- **Viewport Coordinates**: Derived by subtracting the document scroll offsets obtained from `DOMSnapshot.captureSnapshot` (`scrollOffsetX`, `scrollOffsetY`):
  $$\text{center\_x}_{\text{vp}} = \text{center\_x}_{\text{doc}} - \text{scrollOffsetX}$$
  $$\text{center\_y}_{\text{vp}} = \text{center\_y}_{\text{doc}} - \text{scrollOffsetY}$$

## Open Questions

1. **Multi-frame / iFrame Layout Trees**:
   - How should layout bounds and scroll offsets be resolved across nested iframe document trees when `DOMSnapshot` reports multiple document records?
2. **CSS Transform & Dynamic Scaling**:
   - How should element coordinates be adjusted if parent elements have CSS transforms (`scale`, `matrix`) or zoom levels that alter visual representation without modifying bounding box values in layout bounds?
3. **Custom Non-standard Controls**:
   - What heuristics should be used for web components or custom ARIA widgets that do not expose standard HTML input `type`, `id`, or `name` attributes for security refusal checks?
