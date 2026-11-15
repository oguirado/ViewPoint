# Vision brief v1

## 1. Purpose, status, and source boundary

`vqa.vision-brief/v1` is a bounded, deterministic summary of engine report
metadata for a window of game frames. A multimodal model such as Gemini can
consume this brief while a separate reasoner drives Blender or Godot. The brief
contains report values and pixel crop coordinates; producing or checking it
does not require access to pixels.

This document defines the new brief contract and its independent checker. Its
source evidence is the four bound files read at `bridge/integration`, whose
resolved branch snapshot for this task is
`9637587da02809d2600a8cef0dc99b596e36a046`. Source citations below use
`repository/path:line` or `repository/path:first-last`. The JSON Schema is
embedded here to stay within the task's exclusive write paths.

The keywords MUST, MUST NOT, and MAY are normative for the brief contract.
Rules identified as new brief policy are not claims about existing engine
behavior. ABSENT means absent from the four bound files, not a claim that no
other repository file could define the behavior.

### 1.1 What the source establishes

| Evidence | Established behavior |
| --- | --- |
| `visioncore/schemas/vqa.core-frame.v1.schema.json:1` | A `frames.jsonl` row has schema `vqa.core-frame/v1` and requires `frame`, `grid`, `n`, `cov_ppm`, `global_mv`, `regions`, and `tiles_sha256`. It permits additional properties. |
| `visioncore/schemas/vqa.core-region.v1.schema.json:1` | A `regions.jsonl` row has schema `vqa.core-region/v1` and requires `id`, `frame`, `grid`, `cls`, `n_tiles`, `bbox_t`, `bbox_px`, `mv`, `r_max`, `r_sum`, `edge_ppm`, `kind`, `shade_only`, and `spans`. It permits additional properties. |
| `visioncore/regions.py:129-143` | `bbox_t` contains inclusive tile extrema; `bbox_px` is `[x, y, width, height]`. Pixel width and height are clipped against the frame dimensions. |
| `visioncore/regions.py:113-128`, `visioncore/regions.py:140-141` | `n_tiles` is island size. The dominant class is selected from a histogram; the lowest histogram index wins a tie. |
| `visioncore/regions.py:21`, `visioncore/regions.py:46-51`, `visioncore/regions.py:115-146` | Region kinds are `EDGE`, `TEXTURE`, `SMOOTH`, and `MIXED`. The most common kind wins, with this order breaking ties. Per-tile classification gives TEXTURE priority over EDGE. |
| `visioncore/regions.py:36-43`, `visioncore/regions.py:143` | CHANGED, FLICKER, and DRIFT can share one CHG island. MOV groups preserve their common motion; other groups emit `[0, 0]`. |
| `visioncore/regions.py:4-5`, `visioncore/regions.py:138-139`, `visioncore/regions.py:153` | A region ID is constructed using the frame, minimum island Morton code, and dimensions. Source output is ordered by its Morton-derived component, not by descending tile count. |
| `visioncore/regions.py:192-231` | The engine has deterministic previous-to-current region correspondence using displaced Morton spans, an overlap threshold, greedy one-to-one links, and split/merge metadata. This does not define brief flicker groups. |
| `visioncore/pipeline.py:20-22`, `visioncore/pipeline.py:204-205` | Frame numbers use driver `render_index` when present, otherwise the dense frame index. Reported frame IDs are not established to be consecutive. |
| `visioncore/pipeline.py:216-220`, `visioncore/pipeline.py:237-239` | Frame `n`, `cov_ppm`, and `global_mv` are copied from classification summaries. The row's `regions` is the number of region rows for that frame. |
| `visioncore/pipeline.py:226-239` | The pipeline substitutes zero motion for matching when necessary but preserves the original nullable global motion in the frame report. |
| `visioncore/pipeline.py:93-97`, `visioncore/pipeline.py:183-185` | Pixel dimensions are available in pipeline metadata and `vision.json`; they are not fields in the bound core-frame schema. |
| `visioncore/pipeline.py:105-112` | The pipeline serializes frame, region, and correspondence metadata to separate `frames.jsonl`, `regions.jsonl`, and `links.jsonl` carriers. |

The two report schemas are permissive, one-line schemas. They do not constrain
array lengths or item types for bounding boxes, motion, or `n`, do not give a
grid grammar, and do not impose the brief's numeric or ASCII limits. This
contract adds those requirements to the fields it consumes.

## 2. Checker API and trusted context

The implementation is `tools/vision_brief_check.py` and uses Python's standard
library only. It MUST NOT import `visioncore`.

```python
validate(
    brief,
    *,
    width,
    height,
    max_regions,
    max_crops,
    pad_px,
    source_frames,
    source_regions,
    budget_tokens=600,
) -> list[str]
```

`brief` is a JSON-compatible Python dictionary. Arrays are represented as
lists. The caller supplies the complete frame window in `source_frames` and
all of its region rows in `source_regions`. The checker can verify that those
inputs and the brief agree; it cannot prove that the caller supplied the true
or complete original engine output. Completeness is a caller obligation.

Every numeric parameter MUST have exact Python type `int`, excluding `bool` and
floating-point values such as `1.0`. `width`, `height`, and `budget_tokens` MUST
be at least 1. `max_regions`, `max_crops`, and `pad_px` MUST be at least 0.
Dimensions and padding are pixels. Caps count objects, not bytes. Only
`budget_tokens` has a default, which is 600.

The return value is a list of diagnostic strings. An empty list means the
brief satisfies this contract against the supplied parameters and source
context. Invalid inputs MUST result in problems rather than an accidental
indexing or type exception. Diagnostics need not be exhaustive after a
malformed structure makes later checks unsafe, and their prose is not part of
the wire contract.

The validator performs semantic checks using its source context. A detached
brief and structural JSON Schema alone cannot establish provenance, complete
truncation accounting, or correct flicker and crop aggregation. There is no
requirement for an image extractor, a production brief generator, or a command
line interface in this task.

### 2.1 Consumed frame fields

`source_frames` MUST be nonempty. Source rows may be supplied in any order.
The checker consumes and validates these fields:

| Field | Requirement |
| --- | --- |
| `schema` | Exactly `vqa.core-frame/v1`. |
| `frame` | Exact nonnegative integer; unique within `source_frames`. |
| `grid` | Nonempty printable ASCII string; identical in all frame rows. |
| `cov_ppm` | Exact integer in the inclusive range 0 through 1000000. |
| `global_mv` | `null`, or a list of two exact signed integers. |
| `regions` | Exact nonnegative integer equal to the number of supplied region rows referencing this frame. |

Sort source frame rows by numeric `frame` ascending to obtain the window
sequence. A numeric gap does not create additional frames or observations.
The window endpoints are the first and last IDs in this sequence.

`grid` is an opaque identifier: the checker MUST NOT parse it, decode frame
dimensions from it, or assume a tile-size grammar. Pixel dimensions come only
from the explicit parameters.

### 2.2 Consumed region fields

Each row in `source_regions` MUST satisfy the following consumed projection:

| Field | Requirement |
| --- | --- |
| `schema` | Exactly `vqa.core-region/v1`. |
| `id` | Nonempty printable ASCII string, globally unique across all supplied region rows. No ID grammar is parsed. |
| `frame` | Exact integer identifying one of the supplied frame rows. |
| `grid` | The same opaque grid identifier as the source window. |
| `cls` | One of `UNCHANGED`, `JITTER`, `CHANGED`, `MOVED`, `FLICKER`, `FROZEN`, `DRIFT`. |
| `kind` | One of `EDGE`, `TEXTURE`, `SMOOTH`, `MIXED`. |
| `bbox_px` | Four exact integers `[x, y, w, h]`; `x >= 0`, `y >= 0`, `w >= 1`, `h >= 1`, `x + w <= width`, and `y + h <= height`. |
| `mv` | Two exact signed integers `[dx, dy]`, preserved as supplied. |
| `n_tiles` | Exact integer at least 1. |

Region input order is immaterial. An empty region list is valid if all frame
`regions` counts are zero. The allowed classes follow
`visioncore/schemas/vqa.core-region.v1.schema.json:1`; this checker does not
reclassify report rows or infer class labels from pixels.

Extra fields in source objects are permitted. The checker does not validate
unused report fields such as `n`, `tiles_sha256`, `bbox_t`, `spans`, residual
statistics, `edge_ppm`, `shade_only`, or `cls_hist`. Validating the complete
upstream report schemas and their own invariants is the responsibility of the
upstream report validator. In particular, this checker does not recalculate
coverage, tile hashes, or residuals.

## 3. Brief shape and field meanings

The top-level object has exactly these seven keys:

| Key | Meaning and constraints |
| --- | --- |
| `schema` | Exactly `vqa.vision-brief/v1`. |
| `frame_range` | Two exact nonnegative integers `[first, last]`, equal to the minimum and maximum supplied frame IDs. A one-frame window has equal endpoints. |
| `grid` | The source window's opaque grid string, copied exactly. |
| `frames` | One frame summary per supplied source frame, in numeric frame order, with no omissions. |
| `regions` | The retained prefix of ranked region candidates after the cap and budget rules. |
| `crops` | The retained prefix of ordered, merged, closed crop boxes after the cap and budget rules. |
| `truncated` | Exact nonnegative integer counting omitted region candidates and omitted merged crop groups, as defined in section 6. |

Each `frames` object has exactly `frame`, `cov_ppm`, and `global_mv`.
Their values MUST equal the corresponding consumed source fields, including
`null` for unavailable global motion. They retain the bounds and types in
section 2.1. The list MUST be strictly increasing by `frame` and have the same
length as `source_frames`.

The brief's per-frame ppm field is named `cov_ppm` and copies the engine's
reported coverage/changed-fraction proxy without conversion. A derivation of
physical changed-pixel fraction, the meaning of the upstream numerator, and
the class-count denominator are ABSENT from the bound files. This contract
does not manufacture them from `n` or rename unknown motion to zero.

Each `regions` object has exactly these nine keys:

| Key | Meaning and constraints |
| --- | --- |
| `id` | The representative source ID, printable ASCII and nonempty. |
| `cls` | The representative source class; one of the seven allowed class strings. |
| `kind` | The representative source kind; one of the four allowed kind strings. |
| `bbox_px` | Representative `[x, y, width, height]`, preserving the source XYWH convention and source bounds. |
| `mv` | Representative two-integer region motion vector, copied exactly. |
| `n_tiles` | Representative positive tile count. It is not a sum across repeated observations. |
| `first_frame` | Earliest source frame represented by this candidate. |
| `last_frame` | Latest source frame represented by this candidate. |
| `toggles` | Nonnegative count of report-presence transitions for a FLICKER group; zero for all other candidates. |

`first_frame` and `last_frame` are exact nonnegative integers satisfying
`first_frame <= last_frame`; both must arise from the candidate's source
members. They are not synthetic dense-frame offsets.

Each element of `crops` is a list of four exact nonnegative integers
`[x0, y0, x1, y1]`, with both endpoints included. It MUST satisfy
`0 <= x0 <= x1 < width` and `0 <= y0 <= y1 < height`. Its dimensions in pixels
are `x1 - x0 + 1` and `y1 - y0 + 1`. A one-pixel crop is valid.

All brief objects have fixed keys; missing or extra keys are errors. All
brief string values MUST contain only printable ASCII code points 32 through
126, inclusive. Grid strings and IDs must also be nonempty. No brief numeric
field accepts a float, boolean, NaN, or infinity. `null` is permitted only as
the value of a frame summary's `global_mv`.

## 4. Region candidates, flicker grouping, and ranking

These are new brief policies. They do not reuse the source's min-Morton output
order or claim to establish physical object tracks.

### 4.1 Non-FLICKER candidates

Every source row whose `cls` is not `FLICKER` creates one candidate. Copy its
`id`, `cls`, `kind`, `bbox_px`, `mv`, and `n_tiles`. Set `first_frame` and
`last_frame` to that row's `frame`, and set `toggles` to zero. Repeated geometry
across different non-FLICKER rows does not merge them.

### 4.2 FLICKER groups

Partition only source rows whose `cls` is `FLICKER` by exact equality of:

```text
(kind, tuple(bbox_px), tuple(mv))
```

All rows already share the window's grid. Class is implicitly FLICKER and
therefore is not a varying signature component. `id`, `frame`, and `n_tiles`
are not part of the signature. Equal geometry in another class does not join
the group. A changed kind, bounding box, or motion starts a different group.

Each group creates one candidate. Choose its representative by ascending
`(-n_tiles, id)`: the greatest tile count wins, then the smallest ASCII ID.
Copy that row's `id`, `cls`, `kind`, `bbox_px`, `mv`, and `n_tiles`. Set
`first_frame` and `last_frame` to the minimum and maximum member frame IDs.
Do not sum tile counts or construct an aggregate XYWH box.

To count toggles, let the entire sorted source frame sequence be
`f[0], ..., f[k-1]`. Define `p[i] = 1` when at least one source row in the group
has frame `f[i]`, and otherwise `p[i] = 0`. Then:

```text
toggles = sum(p[i] != p[i-1] for i in range(1, k))
```

This counts transitions in report presence, including observed absences
before the first member and after the last member within the supplied window.
Several same-signature rows in one frame contribute one presence value. Do
not add a synthetic absence outside the window, and do not create observations
for missing numeric frame IDs. A one-frame window has zero transitions.

For example, source frames `[10, 11, 14, 20, 21]` and group presence at frames
11 and 20 give `[0, 1, 0, 1, 0]`, `first_frame=11`, `last_frame=20`, and
`toggles=4`. A group present in every supplied frame has zero toggles.

These groups describe repeated report footprints. They are not persistent
physical identities. Actual visual toggle counts are ABSENT from the bound
reports. A majority FLICKER label also does not imply that every tile flickers:
CHANGED, FLICKER, and DRIFT share a source island group
(`visioncore/regions.py:36-38`), and its class uses the histogram majority
(`visioncore/regions.py:140`). The engine's span-based temporal matcher
(`visioncore/regions.py:192-231`) is a different relation and is not called by
this checker.

### 4.3 Window order and region cap

Combine all non-FLICKER candidates and FLICKER-group candidates. Let their
total count be `N`. Sort by ascending `(-n_tiles, id)`, using case-sensitive
ASCII lexicographic string order for IDs. No ID parsing or locale collation is
permitted. Globally unique source IDs make the tie break deterministic.

Take the first `min(N, max_regions)` candidates. This capped prefix seeds
crop generation before any budget removals. It is also the initial retained
region list. Budget handling may remove only items from its end.

## 5. Crop derivation, merging, and order

Crops use the full common pixel coordinate frame across the window. They are
metadata boxes, not image files and not separately frame-tagged image samples.
The task defines no crop-pixel producer.

### 5.1 Padding and clamping

Assign every region in the capped prefix its zero-based ranking position.
From its XYWH `bbox_px=[x, y, w, h]`, create the following closed box:

```text
x0 = max(0, x - pad_px)
y0 = max(0, y - pad_px)
x1 = min(width - 1, x + w - 1 + pad_px)
y1 = min(height - 1, y + h - 1 + pad_px)
```

The `-1` converts the source extent to a closed last pixel; omitting it is an
off-by-one error. The source convention follows
`visioncore/regions.py:131-133` and `visioncore/regions.py:142`. Padding is
applied once, before merging, and is not reapplied to merged rectangles.

Each initial crop group carries its closed box and its input rank. An empty
capped region prefix produces no crop groups.

### 5.2 Inclusive overlap and deterministic fixed-point merging

Boxes `a` and `b` overlap exactly when their inclusive intervals intersect on
both axes:

```text
a.x0 <= b.x1 and b.x0 <= a.x1 and
a.y0 <= b.y1 and b.y0 <= a.y1
```

Sharing even one pixel, including a corner, is overlap. Adjacent but distinct
pixels do not overlap: an interval ending at 9 and one beginning at 10 are
disjoint. Containment and identical boxes count as overlap.

Merging MUST use this deterministic algorithm:

1. Sort groups by their minimum contributing input rank, then by the box tuple
   `(x0, y0, x1, y1)` as a tie break.
2. Scan pairs in increasing first index, then increasing second index, with
   the second index greater than the first. Select the first overlapping pair.
3. Replace that pair by its enclosing rectangle:
   `[min(x0), min(y0), max(x1), max(y1)]`, carrying the smaller group rank.
4. Resort and restart pair scanning from the beginning.
5. Stop only when no pair overlaps.

Restarting matters because a bounding rectangle can create an overlap that
neither original rectangle had. This is merging to a fixed point of the
defined bounding-box operation, not merely a one-pass test on original boxes.
All groups remain within the frame because all original endpoints are within
the frame.

Let `M` be the number of merged groups at this point, before the crop cap.
Order them by minimum contributing rank, using the box tuple tie break above.
Take the first `min(M, max_crops)` boxes as the initial retained crop list.
Ranks and group membership are internal derivation data and are not serialized.

### 5.3 Frozen crop candidates during budgeting

The complete crop derivation uses the region prefix after `max_regions` but
before budget truncation. Freeze the resulting ordered crop candidates and
`M`. Do not regenerate, resize, or remerge crops when the budget later removes
regions. A retained crop may therefore outlive every retained region that
contributed to it; this is valid and required by the deletion order.

The final crop list MUST have no overlaps. Merely being nonoverlapping and
inside the frame is insufficient: its order and exact geometry must also
match the source-derived retained prefix.

## 6. Truncation and canonical size budget

### 6.1 Accounting

`N` is the total count of region candidates after FLICKER grouping and before
the region cap. `M` is the total count of merged crop groups generated from
the capped region prefix, before the crop cap. For any retained lists:

```text
truncated = (N - len(regions)) + (M - len(crops))
```

Caps and budget removals both contribute. FLICKER grouping and crop merging
do not count as truncation. A raw source-row count is not a substitute for
`N`, and an unmerged crop count is not a substitute for `M`.

The checker derives `N` and `M` from the complete supplied source context and
parameters. The field is not self-certifying: an arbitrary nonnegative integer
or a value matching only final list lengths is insufficient.

### 6.2 Canonical bytes

Serialize the complete current brief, including the current `truncated`, as:

```python
json.dumps(
    brief,
    ensure_ascii=True,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("ascii")
```

There is no trailing newline, byte-order mark, indentation, or surrounding
whitespace. Object keys sort lexicographically; array order is preserved.
All supplied string values must already satisfy the printable-ASCII rules:
escaping a non-ASCII string is not permission to use it.

The size proxy is exact integer arithmetic:

```text
size_proxy = (len(canonical_bytes) + 3) // 4
```

This is a specified byte-size proxy, not a claim about any model's tokenizer.
It includes all mandatory fields, both optional lists, JSON punctuation,
escaped characters, and the decimal representation of `truncated`.

### 6.3 Removal algorithm

Start with all mandatory data, the capped region prefix, the capped crop
prefix, and the accounting value above. Serialize and check the proxy. While
it is greater than `budget_tokens`:

1. If any regions remain, remove the last region, which is the lowest ranked
   retained candidate.
2. Otherwise, if any crops remain, remove the last crop.
3. Otherwise, the budget is impossible for this window and parameters. Report
   a validation problem; no compliant brief exists.
4. After every removal, recompute `truncated`, serialize the complete object
   again, and recalculate the proxy before deciding whether to remove more.

Recalculation is required because changing `truncated` can change its digit
count. Stop at the first state that fits; removing extra candidates when an
earlier state already fits is not the prescribed output. Do not drop crops
while a region remains solely to retain a more convenient combination.

Never omit or shorten `frame_range`, `grid`, the `frames` sequence, any frame's
global motion value, or any other mandatory key to meet a budget. Do not
replace unavailable motion, shorten IDs, edit source values, or reorder
content to find a different result. A budget too small for the mandatory data
remains impossible after both optional lists are emptied.

## 7. Structural JSON Schema

The following Draft 2020-12 JSON Schema specifies fixed keys, structural
types, constants, enums, and intrinsic numeric bounds. The exported
`JSON_SCHEMA` in the checker supplies the same schema. It is embedded rather
than written to an additional repository file.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "vqa.vision-brief/v1",
  "title": "vqa.vision-brief/v1",
  "$defs": {
    "vector": {
      "type": "array",
      "items": {
        "type": "integer"
      },
      "minItems": 2,
      "maxItems": 2
    },
    "xywh": {
      "type": "array",
      "minItems": 4,
      "maxItems": 4,
      "prefixItems": [
        {
          "type": "integer",
          "minimum": 0
        },
        {
          "type": "integer",
          "minimum": 0
        },
        {
          "type": "integer",
          "minimum": 1
        },
        {
          "type": "integer",
          "minimum": 1
        }
      ],
      "items": false
    },
    "frame": {
      "type": "object",
      "properties": {
        "frame": {
          "type": "integer",
          "minimum": 0
        },
        "cov_ppm": {
          "type": "integer",
          "minimum": 0,
          "maximum": 1000000
        },
        "global_mv": {
          "anyOf": [
            {
              "type": "null"
            },
            {
              "$ref": "#/$defs/vector"
            }
          ]
        }
      },
      "required": [
        "frame",
        "cov_ppm",
        "global_mv"
      ],
      "additionalProperties": false
    },
    "region": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string",
          "minLength": 1,
          "pattern": "^[ -~]+(?![\\s\\S])"
        },
        "cls": {
          "type": "string",
          "enum": [
            "UNCHANGED",
            "JITTER",
            "CHANGED",
            "MOVED",
            "FLICKER",
            "FROZEN",
            "DRIFT"
          ]
        },
        "kind": {
          "type": "string",
          "enum": [
            "EDGE",
            "TEXTURE",
            "SMOOTH",
            "MIXED"
          ]
        },
        "bbox_px": {
          "$ref": "#/$defs/xywh"
        },
        "mv": {
          "$ref": "#/$defs/vector"
        },
        "n_tiles": {
          "type": "integer",
          "minimum": 1
        },
        "first_frame": {
          "type": "integer",
          "minimum": 0
        },
        "last_frame": {
          "type": "integer",
          "minimum": 0
        },
        "toggles": {
          "type": "integer",
          "minimum": 0
        }
      },
      "required": [
        "id",
        "cls",
        "kind",
        "bbox_px",
        "mv",
        "n_tiles",
        "first_frame",
        "last_frame",
        "toggles"
      ],
      "additionalProperties": false
    }
  },
  "type": "object",
  "properties": {
    "schema": {
      "type": "string",
      "const": "vqa.vision-brief/v1"
    },
    "frame_range": {
      "type": "array",
      "items": {
        "type": "integer",
        "minimum": 0
      },
      "minItems": 2,
      "maxItems": 2
    },
    "grid": {
      "type": "string",
      "minLength": 1,
      "pattern": "^[ -~]+(?![\\s\\S])"
    },
    "frames": {
      "type": "array",
      "minItems": 1,
      "items": {
        "$ref": "#/$defs/frame"
      }
    },
    "regions": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/region"
      }
    },
    "crops": {
      "type": "array",
      "items": {
        "type": "array",
        "items": {
          "type": "integer",
          "minimum": 0
        },
        "minItems": 4,
        "maxItems": 4
      }
    },
    "truncated": {
      "type": "integer",
      "minimum": 0
    }
  },
  "required": [
    "schema",
    "frame_range",
    "grid",
    "frames",
    "regions",
    "crops",
    "truncated"
  ],
  "additionalProperties": false
}
```

JSON Schema's mathematical integer type may accept a JSON number written
`1.0`. The Python validator intentionally requires exact `int` values instead
and rejects both `1.0` and `True` in integer fields. This stricter rule is part
of this contract.

Schema validation does not replace semantic checks for frame-relative
coordinates, exact source copies, source completeness assumptions, candidate
membership, ordering, caps, flicker transitions, crop derivation and overlap,
truncation accounting, and canonical budget enforcement.

## 8. Required validation behavior

The checker MUST validate the supplied parameters and consumed source
projections before relying on them. It MUST check fixed brief keys, object
and array shapes, exact integers, allowed nulls, printable ASCII, enum values,
and all intrinsic bounds. It MUST then derive the deterministic expected
summary from the supplied metadata and enforce:

- The exact frame endpoints, grid, complete ordered frame list, ppm values,
  and nullable global motion.
- Candidate membership, representative fields, first and last frames,
  report-presence toggles, descending tile-count/ID ranking, and region cap.
- Closed, padded, clamped crop geometry, deterministic merging and order,
  absence of overlaps, and crop cap.
- The exact cap and budget omission sequence, consistent `truncated`, and the
  complete object's size proxy being at most `budget_tokens`.

The checker must not silently repair a malformed brief. It reports problems
against the submitted dictionary. As with all source-context checks, a valid
result certifies consistency with supplied metadata, not the correctness of
unseen pixels or the authenticity of the caller's reports.

## 9. Tests, storage, and performance evidence

`tests/test_vision_brief_check.py` uses `unittest` and hand-written metadata
briefs as Python dictionaries. It includes a valid brief and independent
invalid cases for the contract's rules. Run the requested gate in the VM:

```sh
python3 -m unittest tests.test_vision_brief_check
```

`validate` performs no filesystem input or output. Its source context,
candidate calculations, and canonical bytes live in RAM. No raw frame, luma
plane, or pixel buffer is needed or written to disk. Tests must not commit
binary fixtures; deterministic metadata fixtures remain ordinary Python
values. If any timing is recorded in the VM, label it VM, never MEASURED.
Host performance and acceptance remain the orchestrator's responsibility.

## 10. Explicit ABSENT boundaries

| Topic | What is ABSENT and how v1 handles it |
| --- | --- |
| Changed fraction | The bound schemas and pipeline do not define `cov_ppm`'s underlying numerator, denominator, class combination, or rounding. The brief copies `cov_ppm`; no pixel or tile fraction is invented. |
| Grid grammar | The bound files do not define how to parse `grid` into dimensions or tile geometry. The checker treats it as opaque and uses explicit `width` and `height`. |
| Complete ID grammar | The body of `ids.region_id` is not among the bound files. IDs remain opaque ASCII strings; the new policy requires supplied IDs to be unique and never parses them. |
| Persistent identity | The bound reports do not establish a durable object ID across the window. FLICKER groups are exact descriptive footprints, not physical tracks. |
| Physical flicker toggles | No per-tile on/off history or actual visual toggle count is present. `toggles` counts transitions in the supplied report-presence sequence only. |
| Brief aggregation | The bound engine sources do not define this schema, representative selection, window ranking, first/last aggregation, or the brief's FLICKER merge rule. Sections 3 and 4 introduce them. |
| Crop behavior | The bound files define source XYWH boxes but not this brief's closed crops, padding, clamping, ordering, merging, caps, or extraction. Section 5 defines the metadata rules; pixel extraction is outside this task. |
| Budget behavior | The bound files do not define this canonical byte proxy, 600-token default, omission priority, frozen crops, or truncation accounting. Section 6 defines them. |
| Complete upstream validation | The checker does not establish unused source fields, classification internals, hashes, residuals, spans, or underlying image truth. Those remain upstream responsibilities. |

These boundaries are deliberate: every new policy is fully specified here,
while statements about existing behavior remain limited to the cited source.
