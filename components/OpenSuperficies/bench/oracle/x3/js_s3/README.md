# JS S3 Seed (bench/oracle/x3/js_s3)

## Scroll Convention
The coordinate conversion convention used for document and viewport coordinates is:
- `document_xy = [x + Math.floor(w / 2), y + Math.floor(h / 2)]`
- `viewport_xy = [document_xy[0] - scrollOffsetX, document_xy[1] - scrollOffsetY]`

All coordinates and offsets are integers.

## Open Questions
1. Handling of multi-frame / multi-document DOM snapshots where nested document scroll offsets or subframe coordinate translations apply.
2. Handling fractional bounds and subpixel alignment vs integer floor division.
3. Behavior and timing when dynamic page layouts shift or mutate between action planning retries.
