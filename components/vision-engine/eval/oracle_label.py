"""Validator and stream helpers for vqa.oracle.label/v1 records.

Used by the read-only Chromium oracle. Consistent with eval/gui_synth/labels.py
(vqa.gui_synth.label/v1, which vqa.oracle.label/v1 is a superset of) and class
order 0..6 from visioncore/change.py.
"""
import json
import numpy as np

SCHEMA = "vqa.oracle.label/v1"

UNCHANGED, JITTER, CHANGED, MOVED, FLICKER, FROZEN, DRIFT = range(7)
C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT = range(7)
NAMES = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")
CLASSES = tuple(range(7))

FLAGS = ("POINTER", "UNCOVERED", "UNOBSERVABLE", "STRADDLE")
FLAG_SET = frozenset(FLAGS)

SOURCES = ("scroll", "layoutshift", "mutation", "focus", "pointer", "window", "animation")
SOURCE_SET = frozenset(SOURCES)

CONFIDENCES = ("exact", "derived")
CONFIDENCE_SET = frozenset(CONFIDENCES)

REQUIRED_FIELDS = (
    "schema",
    "run_id",
    "event_id",
    "f0",
    "f1",
    "rects",
    "ideal",
    "mv",
    "period_ms",
    "flags",
    "source",
    "source_ref",
    "confidence",
)
OPTIONAL_FIELDS = ("tiles",)
ALLOWED_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

ERR_RECORD = "E_ORACLE_RECORD"
ERR_EXTRA_FIELDS = "E_ORACLE_EXTRA_FIELDS"
ERR_SCHEMA = "E_ORACLE_SCHEMA"
ERR_RUN_ID = "E_ORACLE_RUN_ID"
ERR_EVENT_ID = "E_ORACLE_EVENT_ID"
ERR_FRAME_RANGE = "E_ORACLE_FRAME_RANGE"
ERR_RECTS = "E_ORACLE_RECTS"
ERR_TILES = "E_ORACLE_TILES"
ERR_IDEAL = "E_ORACLE_IDEAL"
ERR_MV = "E_ORACLE_MV"
ERR_PERIOD_MS = "E_ORACLE_PERIOD_MS"
ERR_FLAGS = "E_ORACLE_FLAGS"
ERR_SOURCE = "E_ORACLE_SOURCE"
ERR_SOURCE_REF = "E_ORACLE_SOURCE_REF"
ERR_CONFIDENCE = "E_ORACLE_CONFIDENCE"
ERR_JSON = "E_ORACLE_JSON"


class OracleLabelError(ValueError):
    """ValueError carrying a stable error code string."""

    def __init__(self, code, detail=""):
        self.code = code
        msg = f"{code}: {detail}" if detail else code
        super().__init__(msg)


def _is_int(v):
    return isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_))


def _no_float(token):
    raise OracleLabelError(ERR_JSON, f"non-integer numeric literal {token!r}")


def validate_record(obj):
    """Validate a single vqa.oracle.label/v1 record dict.

    Raises ValueError (OracleLabelError) with a stable error code string on any
    schema or invariant violation. Returns obj when valid.
    """
    if not isinstance(obj, dict):
        raise OracleLabelError(ERR_RECORD, "record must be a JSON object")

    extra = sorted(set(obj.keys()) - ALLOWED_FIELDS)
    if extra:
        raise OracleLabelError(ERR_EXTRA_FIELDS, f"unknown fields: {extra}")

    if obj.get("schema") != SCHEMA:
        raise OracleLabelError(ERR_SCHEMA, f"expected schema {SCHEMA!r}")

    run_id = obj.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise OracleLabelError(ERR_RUN_ID, "run_id must be a non-empty string")

    event_id = obj.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise OracleLabelError(ERR_EVENT_ID, "event_id must be a non-empty string")

    if "f0" not in obj or "f1" not in obj:
        raise OracleLabelError(ERR_FRAME_RANGE, "f0 and f1 are required")
    f0, f1 = obj["f0"], obj["f1"]
    if not _is_int(f0) or not _is_int(f1) or int(f0) < 0 or int(f1) < 0 or int(f0) > int(f1):
        raise OracleLabelError(ERR_FRAME_RANGE, "f0 and f1 must be non-negative ints with f0 <= f1")

    if "rects" not in obj:
        raise OracleLabelError(ERR_RECTS, "rects is required")
    rects = obj["rects"]
    if not isinstance(rects, list) or len(rects) < 1:
        raise OracleLabelError(ERR_RECTS, "rects must be a non-empty list of [x, y, w, h]")
    for r in rects:
        if not isinstance(r, list) or len(r) != 4 or not all(_is_int(c) for c in r):
            raise OracleLabelError(ERR_RECTS, "each rect must be [x, y, w, h] ints")
        x, y, w, h = (int(c) for c in r)
        if x < 0 or y < 0 or w < 1 or h < 1:
            raise OracleLabelError(ERR_RECTS, "rect requires x >= 0, y >= 0, w >= 1, h >= 1")

    if "tiles" in obj and obj["tiles"] is not None:
        tiles = obj["tiles"]
        if not isinstance(tiles, list):
            raise OracleLabelError(ERR_TILES, "tiles must be a list of ints or null")
        seen_tiles = set()
        for t in tiles:
            if not _is_int(t) or int(t) < 0:
                raise OracleLabelError(ERR_TILES, "tiles elements must be non-negative ints")
            ti = int(t)
            if ti in seen_tiles:
                raise OracleLabelError(ERR_TILES, f"duplicate tile {ti}")
            seen_tiles.add(ti)

    if "ideal" not in obj:
        raise OracleLabelError(ERR_IDEAL, "ideal is required")
    ideal = obj["ideal"]
    if not _is_int(ideal) or not (0 <= int(ideal) <= 6):
        raise OracleLabelError(ERR_IDEAL, "ideal must be an int in 0..6")
    ideal_int = int(ideal)

    if "mv" not in obj:
        raise OracleLabelError(ERR_MV, "mv field is required")
    mv = obj["mv"]
    if ideal_int == MOVED:
        if not isinstance(mv, list) or len(mv) != 2 or not all(_is_int(c) for c in mv):
            raise OracleLabelError(ERR_MV, "mv must be [dx, dy] ints when ideal is 3 (MOVED)")
    else:
        if mv is not None:
            raise OracleLabelError(ERR_MV, "mv must be null when ideal is not 3 (MOVED)")

    if "period_ms" not in obj:
        raise OracleLabelError(ERR_PERIOD_MS, "period_ms field is required")
    period_ms = obj["period_ms"]
    if ideal_int == FLICKER:
        if not _is_int(period_ms) or int(period_ms) < 1:
            raise OracleLabelError(ERR_PERIOD_MS, "period_ms must be a positive int when ideal is 4 (FLICKER)")
    else:
        if period_ms is not None:
            raise OracleLabelError(ERR_PERIOD_MS, "period_ms must be null when ideal is not 4 (FLICKER)")

    if "flags" not in obj:
        raise OracleLabelError(ERR_FLAGS, "flags is required")
    flags = obj["flags"]
    if not isinstance(flags, list):
        raise OracleLabelError(ERR_FLAGS, "flags must be a list")
    seen_flags = set()
    for flag in flags:
        if not isinstance(flag, str) or flag not in FLAG_SET:
            raise OracleLabelError(ERR_FLAGS, f"invalid flag {flag!r}")
        if flag in seen_flags:
            raise OracleLabelError(ERR_FLAGS, f"duplicate flag {flag!r}")
        seen_flags.add(flag)

    if "source" not in obj:
        raise OracleLabelError(ERR_SOURCE, "source is required")
    source = obj["source"]
    if not isinstance(source, str) or source not in SOURCE_SET:
        raise OracleLabelError(ERR_SOURCE, f"source must be one of {SOURCES}")

    if "source_ref" not in obj:
        raise OracleLabelError(ERR_SOURCE_REF, "source_ref is required")
    source_ref = obj["source_ref"]
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise OracleLabelError(ERR_SOURCE_REF, "source_ref must be a non-empty string")

    if "confidence" not in obj:
        raise OracleLabelError(ERR_CONFIDENCE, "confidence is required")
    confidence = obj["confidence"]
    if not isinstance(confidence, str) or confidence not in CONFIDENCE_SET:
        raise OracleLabelError(ERR_CONFIDENCE, f"confidence must be one of {CONFIDENCES}")

    return obj


def iter_records(lines):
    """Iterate and validate vqa.oracle.label/v1 records from JSONL text or line iterable."""
    if isinstance(lines, (bytes, bytearray)):
        try:
            lines = bytes(lines).decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise OracleLabelError(ERR_JSON, f"invalid utf-8: {exc}") from exc
    elif isinstance(lines, str):
        lines = lines.splitlines()

    for raw in lines:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = bytes(raw).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OracleLabelError(ERR_JSON, f"invalid utf-8: {exc}") from exc
        if not isinstance(raw, str):
            raise OracleLabelError(ERR_JSON, "line must be a string")
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s, parse_float=_no_float, parse_constant=_no_float)
        except OracleLabelError:
            raise
        except ValueError as exc:
            raise OracleLabelError(ERR_JSON, f"invalid JSON: {exc}") from exc
        yield validate_record(obj)


def gate_claim_set(records):
    """Validate records and keep only those with confidence == 'exact'."""
    if isinstance(records, (str, bytes, bytearray)):
        iterable = iter_records(records)
    else:
        iterable = (validate_record(r) for r in records)
    return [r for r in iterable if r["confidence"] == "exact"]
