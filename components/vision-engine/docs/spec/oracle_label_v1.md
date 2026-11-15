# Specification: `vqa.oracle.label/v1`

## 1. Overview and Relationship to `vqa.gui_synth.label/v1`

`vqa.oracle.label/v1` is the canonical JSON Lines (JSONL, one JSON object per line, integers only) record format emitted by the read-only Chromium DOM/compositor oracle and validated by `eval/oracle_label.py`.

It is a strict conceptual superset of the synthetic GUI benchmark label schema `vqa.gui_synth.label/v1` defined in `eval/gui_synth/labels.py`:

- **Shared core semantics**: Both formats bind ground-truth change events to a run/scene identifier, a unique `event_id`, frame bounds `f0` and `f1`, Morton-ordered tile indices (`tiles`), the 7-class `ideal` taxonomy (`0..6` in `visioncore/change.py` order), and integer pixel translation vectors `mv` (`[dx, dy]`, destination minus source) for class `3` (`MOVED`).
- **Oracle extensions over `vqa.gui_synth.label/v1`**:
  - `run_id` generalizes `scene_id` to arbitrary Chromium oracle capture runs.
  - `f0` and `f1` represent an **inclusive** frame interval `[f0, f1]` with `0 <= f0 <= f1` (whereas `vqa.gui_synth.label/v1` uses half-open `[f0, f1)` for single-step transitions).
  - `rects` records one or more pixel-space bounding boxes `[x, y, w, h]` directly from the browser layout/damage tree, while `tiles` is optional (`list[int]` or `null`/omitted before grid projection).
  - `period_ms` records the blink/oscillation period in integer milliseconds iff `ideal == 4` (`FLICKER`).
  - `flags` annotates boundary and observability conditions (`POINTER`, `UNCOVERED`, `UNOBSERVABLE`, `STRADDLE`).
  - `source` and `source_ref` attribute the event to the originating browser mechanism (`scroll`, `layoutshift`, `mutation`, `focus`, `pointer`, `window`, `animation`) and DOM/compositor reference string.
  - `confidence` distinguishes `exact` oracle observations (eligible for hard gating via `gate_claim_set`) from `derived` heuristic/interpolated labels.

## 2. Class Taxonomy (`ideal`)

The `ideal` field uses the integer class IDs `0..6` in `visioncore/change.py` (`C_UNCHANGED .. C_DRIFT`) and `eval/gui_synth/labels.py` order:

| `ideal` | Name | `mv` Requirement | `period_ms` Requirement |
|---|---|---|---|
| `0` | `UNCHANGED` | `null` | `null` |
| `1` | `JITTER` | `null` | `null` |
| `2` | `CHANGED` | `null` | `null` |
| `3` | `MOVED` | `[dx, dy]` (two ints, required) | `null` |
| `4` | `FLICKER` | `null` | positive int (`>= 1`, required) |
| `5` | `FROZEN` | `null` | `null` |
| `6` | `DRIFT` | `null` | `null` |

## 3. Record Fields and Validation Rules

Every line in a `vqa.oracle.label/v1` JSONL stream must parse as a JSON object containing no floating-point or `NaN`/`Infinity` literals and no unknown keys.

| Field | Type | Required | Rule & Constraints | Error Code |
|---|---|---|---|---|
| *(record)* | `object` | yes | Must be a JSON object (`dict`) with no unknown keys; JSONL lines must be valid UTF-8 integer-only JSON. | `E_ORACLE_RECORD`, `E_ORACLE_EXTRA_FIELDS`, `E_ORACLE_JSON` |
| `schema` | `string` | yes | Must equal `"vqa.oracle.label/v1"`. | `E_ORACLE_SCHEMA` |
| `run_id` | `string` | yes | Non-empty, non-whitespace string identifying the capture run. | `E_ORACLE_RUN_ID` |
| `event_id` | `string` | yes | Non-empty, non-whitespace string identifying the oracle event. | `E_ORACLE_EVENT_ID` |
| `f0`, `f1` | `int` | yes | Non-negative integers (`0 <= f0 <= f1`), inclusive frame range (`f0` not above `f1`). Booleans rejected. | `E_ORACLE_FRAME_RANGE` |
| `rects` | `list[[x,y,w,h]]` | yes | Non-empty list of 4-element integer lists `[x, y, w, h]` with `x >= 0`, `y >= 0`, `w >= 1`, `h >= 1`. | `E_ORACLE_RECTS` |
| `tiles` | `list[int] \| null` | optional | May be omitted or `null`. When present as a list, elements must be unique non-negative integers (`>= 0`, Morton tile ranks). | `E_ORACLE_TILES` |
| `ideal` | `int` | yes | Integer class ID in `0..6` (`UNCHANGED` through `DRIFT`). | `E_ORACLE_IDEAL` |
| `mv` | `[dx, dy] \| null` | yes | Required `[dx, dy]` list of two integers iff `ideal == 3` (`MOVED`); must be `null` for all other classes. | `E_ORACLE_MV` |
| `period_ms` | `int \| null` | yes | Required positive integer (`>= 1`) iff `ideal == 4` (`FLICKER`); must be `null` for all other classes. | `E_ORACLE_PERIOD_MS` |
| `flags` | `list[string]` | yes | Subset of `{"POINTER", "UNCOVERED", "UNOBSERVABLE", "STRADDLE"}` with no duplicate entries. | `E_ORACLE_FLAGS` |
| `source` | `string` | yes | One of `"scroll"`, `"layoutshift"`, `"mutation"`, `"focus"`, `"pointer"`, `"window"`, `"animation"`. | `E_ORACLE_SOURCE` |
| `source_ref` | `string` | yes | Non-empty, non-whitespace string identifying the DOM/compositor source node or handle. | `E_ORACLE_SOURCE_REF` |
| `confidence` | `string` | yes | One of `"exact"` or `"derived"`. | `E_ORACLE_CONFIDENCE` |

## 4. Validator API (`eval/oracle_label.py`)

- `validate_record(obj)`: Validates a parsed JSON object (`dict`) against all rules above. Returns `obj` when valid, or raises `ValueError` (`OracleLabelError`) whose message begins with the stable error code string (`E_ORACLE_*`).
- `iter_records(lines)`: Accepts a JSONL string, UTF-8 bytes, or an iterable of lines, skips blank lines, rejects floating-point literals, validates each JSON object via `validate_record`, and yields each valid record.
- `gate_claim_set(records)`: Validates all input records (from a record iterable or JSONL text) and returns the filtered list containing only records with `confidence == "exact"`.
