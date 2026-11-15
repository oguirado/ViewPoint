"""Independent, stdlib-only checker for vqa.vision-brief/v1.

The contract and source citations are in docs/spec/VISION_BRIEF_V1_SPEC.md.
Validation consumes report dictionaries and numeric parameters, never pixels.
No visioncore imports, filesystem operations, mutation of inputs, or timing.
JSON_SCHEMA is the portable structural schema; validate also checks the
source-dependent rules and requires Python integers (not bool or float).
"""

import json
import re


SCHEMA = "vqa.vision-brief/v1"
CLASSES = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")
KINDS = ("EDGE", "TEXTURE", "SMOOTH", "MIXED")


def _object(properties, additional=False):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": additional}


def _array(items, length=None):
    result = {"type": "array", "items": items}
    if length is not None:
        result.update(minItems=length, maxItems=length)
    return result


_INT = {"type": "integer"}
_NONNEG = {"type": "integer", "minimum": 0}
_POSITIVE = {"type": "integer", "minimum": 1}
_TEXT = {"type": "string", "minLength": 1, "pattern": r"^[ -~]+(?![\s\S])"}
_VECTOR = _array(_INT, 2)
_MOTION = {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/vector"}]}
_XYWH = {"type": "array", "minItems": 4, "maxItems": 4,
         "prefixItems": [_NONNEG, _NONNEG, _POSITIVE, _POSITIVE], "items": False}
_FRAME_PROPERTIES = {
    "frame": _NONNEG,
    "cov_ppm": {"type": "integer", "minimum": 0, "maximum": 1000000},
    "global_mv": _MOTION,
}
_REGION_PROPERTIES = {
    "id": _TEXT,
    "cls": {"type": "string", "enum": list(CLASSES)},
    "kind": {"type": "string", "enum": list(KINDS)},
    "bbox_px": {"$ref": "#/$defs/xywh"},
    "mv": {"$ref": "#/$defs/vector"},
    "n_tiles": _POSITIVE,
    "first_frame": _NONNEG,
    "last_frame": _NONNEG,
    "toggles": _NONNEG,
}
_DEFS = {"vector": _VECTOR, "xywh": _XYWH,
         "frame": _object(_FRAME_PROPERTIES),
         "region": _object(_REGION_PROPERTIES)}

# Embedded here because the task's exclusive paths do not include a .json file.
JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SCHEMA,
    "title": SCHEMA,
    "$defs": _DEFS,
    **_object({
        "schema": {"type": "string", "const": SCHEMA},
        "frame_range": _array(_NONNEG, 2),
        "grid": _TEXT,
        "frames": {"type": "array", "minItems": 1,
                   "items": {"$ref": "#/$defs/frame"}},
        "regions": _array({"$ref": "#/$defs/region"}),
        "crops": _array(_array(_NONNEG, 4)),
        "truncated": _NONNEG,
    }),
}

# Only fields consumed by this contract are checked here. Upstream report
# validation owns n, tiles_sha256, bbox_t, spans, residuals and shade_only.
_SOURCE_FRAME = _object({
    "schema": {"type": "string", "const": "vqa.core-frame/v1"},
    "grid": _TEXT,
    **_FRAME_PROPERTIES,
    "regions": _NONNEG,
}, additional=True)
_SOURCE_REGION = _object({
    "schema": {"type": "string", "const": "vqa.core-region/v1"},
    "frame": _NONNEG,
    "grid": _TEXT,
    **{key: value for key, value in _REGION_PROPERTIES.items()
       if key not in ("first_frame", "last_frame", "toggles")},
}, additional=True)


def canonical_bytes(brief):  # @anchor:tools.vision_brief_check.canonical_bytes
    """Serialize a JSON value; callers must validate its types and ASCII first."""
    return json.dumps(brief, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def token_proxy(brief):  # @anchor:tools.vision_brief_check.token_proxy
    """Return ceil(canonical byte length / 4), using integer arithmetic."""
    return (len(canonical_bytes(brief)) + 3) // 4


def _shape(value, schema, path, problems):
    """Check only the fixed schema vocabulary used above, with exact ints."""
    if "$ref" in schema:
        _shape(value, _DEFS[schema["$ref"].rsplit("/", 1)[1]], path, problems)
        return
    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            candidate_problems = []
            _shape(value, alternative, path, candidate_problems)
            if not candidate_problems:
                return
        problems.append(path + ": must be null or a two-integer motion vector")
        return
    kind = schema["type"]
    types = {"object": dict, "array": list, "integer": int,
             "string": str, "null": type(None)}
    if type(value) is not types[kind]:
        problems.append(path + ": expected " + kind +
                        " (integer fields reject bool and float)")
        return
    if "const" in schema and value != schema["const"]:
        problems.append(path + ": wrong schema identifier")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(path + ": unknown enum value")
    if kind == "integer":
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(path + ": below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(path + ": above maximum")
    elif kind == "string":
        if not value.isascii():
            problems.append(path + ": text must be ASCII")
        if len(value) < schema.get("minLength", 0):
            problems.append(path + ": empty text is not allowed")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            problems.append(path + ": text must be nonempty printable ASCII")
    elif kind == "object":
        properties = schema["properties"]
        for key in schema["required"]:
            if key not in value:
                problems.append(path + "." + key + ": missing required key")
        if not schema["additionalProperties"]:
            for key in sorted(set(value) - set(properties), key=ascii):
                problems.append(path + ": unexpected key " + ascii(key))
        for key, subschema in properties.items():
            if key in value:
                _shape(value[key], subschema, path + "." + key, problems)
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            problems.append(path + ": too few array items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(path + ": too many array items")
        prefix = schema.get("prefixItems", [])
        for index, element in enumerate(value):
            subschema = prefix[index] if index < len(prefix) else schema["items"]
            if subschema is False:
                problems.append(path + ": unexpected array item")
            else:
                _shape(element, subschema, path + "[" + str(index) + "]", problems)


def _xywh_inside(box, width, height):
    x, y, w, h = box
    return 0 <= x < width and 0 <= y < height and w > 0 and h > 0 and \
        x + w <= width and y + h <= height


def _overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _source_relations(frames, regions, width, height, problems):
    frame_ids = set()
    grid = frames[0]["grid"]
    for index, row in enumerate(frames):
        path = "source_frames[" + str(index) + "]"
        if row["frame"] in frame_ids:
            problems.append(path + ".frame: duplicate frame")
        frame_ids.add(row["frame"])
        if row["grid"] != grid:
            problems.append(path + ".grid: mixed grids in one window")
    identifiers = set()
    counts = {}
    for index, row in enumerate(regions):
        path = "source_regions[" + str(index) + "]"
        if row["id"] in identifiers:
            problems.append(path + ".id: duplicate source region id")
        identifiers.add(row["id"])
        if row["frame"] not in frame_ids:
            problems.append(path + ".frame: outside the supplied frame window")
        if row["grid"] != grid:
            problems.append(path + ".grid: does not match the frame grid")
        if not _xywh_inside(row["bbox_px"], width, height):
            problems.append(path + ".bbox_px: XYWH box must be inside the frame")
        counts[row["frame"]] = counts.get(row["frame"], 0) + 1
    for index, row in enumerate(frames):
        if row["regions"] != counts.get(row["frame"], 0):
            problems.append("source_frames[" + str(index) +
                            "].regions: count does not match supplied region rows")


def _rank(region):
    return (-region["n_tiles"], region["id"])


def _region_candidates(frames, rows):
    groups = []
    flicker = {}
    for row in rows:
        if row["cls"] == "FLICKER":
            signature = (row["kind"], tuple(row["bbox_px"]), tuple(row["mv"]))
            flicker.setdefault(signature, []).append(row)
        else:
            groups.append([row])
    groups.extend(flicker.values())
    frame_ids = [row["frame"] for row in frames]
    result = []
    for group in groups:
        representative = min(group, key=_rank)
        seen = {row["frame"] for row in group}
        toggles = 0
        if representative["cls"] == "FLICKER":
            toggles = sum((left in seen) != (right in seen)
                          for left, right in zip(frame_ids, frame_ids[1:]))
        result.append({
            "id": representative["id"], "cls": representative["cls"],
            "kind": representative["kind"], "bbox_px": list(representative["bbox_px"]),
            "mv": list(representative["mv"]), "n_tiles": representative["n_tiles"],
            "first_frame": min(seen), "last_frame": max(seen), "toggles": toggles,
        })
    return sorted(result, key=_rank)


def _crop_candidates(regions, width, height, pad_px):
    groups = []
    for rank, region in enumerate(regions):
        x, y, w, h = region["bbox_px"]
        groups.append((rank, [max(0, x - pad_px), max(0, y - pad_px),
                              min(width - 1, x + w - 1 + pad_px),
                              min(height - 1, y + h - 1 + pad_px)]))
    while True:
        groups.sort(key=lambda group: (group[0], tuple(group[1])))
        pair = next(((i, j) for i in range(len(groups))
                     for j in range(i + 1, len(groups))
                     if _overlap(groups[i][1], groups[j][1])), None)
        if pair is None:
            return [box for _, box in groups]
        i, j = pair
        rank_a, a = groups[i]
        rank_b, b = groups[j]
        groups[i] = (min(rank_a, rank_b),
                     [min(a[0], b[0]), min(a[1], b[1]),
                      max(a[2], b[2]), max(a[3], b[3])])
        del groups[j]


def _expected(frames, rows, width, height, max_regions, max_crops, pad_px,
              budget_tokens):
    """Independent report-only reference calculation, not an engine producer."""
    candidates = _region_candidates(frames, rows)
    selected = candidates[:max_regions]
    crops = _crop_candidates(selected, width, height, pad_px)
    result = {
        "schema": SCHEMA,
        "frame_range": [frames[0]["frame"], frames[-1]["frame"]],
        "grid": frames[0]["grid"],
        "frames": [{"frame": row["frame"], "cov_ppm": row["cov_ppm"],
                    "global_mv": None if row["global_mv"] is None
                    else list(row["global_mv"])} for row in frames],
        "regions": list(selected), "crops": crops[:max_crops],
        "truncated": len(candidates) - len(selected) + max(0, len(crops) - max_crops),
    }
    while token_proxy(result) > budget_tokens:
        if result["regions"]:
            result["regions"].pop()
        elif result["crops"]:
            result["crops"].pop()
        else:
            return result, len(candidates), len(crops), False
        result["truncated"] += 1
    return result, len(candidates), len(crops), True


def _brief_relations(brief, frames, width, height, max_regions, max_crops, problems):
    first, last = brief["frame_range"]
    if first > last:
        problems.append("brief.frame_range: closed range is reversed")
    numbers = [row["frame"] for row in brief["frames"]]
    if numbers != sorted(set(numbers)):
        problems.append("brief.frames: frame numbers must be strictly increasing")
    regions = brief["regions"]
    if len(regions) > max_regions:
        problems.append("brief.regions: exceeds max_regions")
    if regions != sorted(regions, key=_rank):
        problems.append("brief.regions: ranking must be n_tiles descending then id")
    if len({row["id"] for row in regions}) != len(regions):
        problems.append("brief.regions: duplicate region id")
    source_numbers = {row["frame"] for row in frames}
    for index, region in enumerate(regions):
        path = "brief.regions[" + str(index) + "]"
        if not _xywh_inside(region["bbox_px"], width, height):
            problems.append(path + ".bbox_px: XYWH box must be inside the frame")
        if not first <= region["first_frame"] <= region["last_frame"] <= last:
            problems.append(path + ": first/last frame must be ordered within the window")
        if region["first_frame"] not in source_numbers or region["last_frame"] not in source_numbers:
            problems.append(path + ": first/last frame must be supplied observations")
        if region["toggles"] > len(frames) - 1:
            problems.append(path + ".toggles: exceeds observed transitions in the window")
        if region["cls"] != "FLICKER" and (
                region["toggles"] != 0 or region["first_frame"] != region["last_frame"]):
            problems.append(path + ": non-flicker observations have one frame and zero toggles")
    crops = brief["crops"]
    if len(crops) > max_crops:
        problems.append("brief.crops: exceeds max_crops")
    for index, box in enumerate(crops):
        x0, y0, x1, y1 = box
        if not (0 <= x0 <= x1 < width and 0 <= y0 <= y1 < height):
            problems.append("brief.crops[" + str(index) +
                            "]: closed box must be ordered and inside the frame")
        for other in range(index):
            if _overlap(crops[other], box):
                problems.append("brief.crops: overlapping boxes must be merged")


def validate(brief, *, width, height, max_regions, max_crops, pad_px,
             source_frames, source_regions, budget_tokens=600):  # @anchor:tools.vision_brief_check.validate
    """Return problem strings, or [] for the exact contract for these inputs.

    Required context is the complete window of engine frame/region report rows
    and the explicit pixel dimensions, caps, and padding. Only consumed source
    fields are checked; upstream validation owns the rest of the report schema.
    Row order is immaterial. Missing or malformed consumed data is a problem,
    never an assertion of valid truncation. This function does not mutate inputs.
    """
    problems = []
    for name, value, minimum in (
            ("width", width, 1), ("height", height, 1),
            ("max_regions", max_regions, 0), ("max_crops", max_crops, 0),
            ("pad_px", pad_px, 0), ("budget_tokens", budget_tokens, 1)):
        if type(value) is not int or value < minimum:
            problems.append("params." + name + ": expected integer >= " + str(minimum))
    _shape(brief, JSON_SCHEMA, "brief", problems)
    _shape(source_frames, {"type": "array", "minItems": 1,
                           "items": _SOURCE_FRAME}, "source_frames", problems)
    _shape(source_regions, _array(_SOURCE_REGION), "source_regions", problems)
    if problems:
        return problems
    _source_relations(source_frames, source_regions, width, height, problems)
    if problems:
        return problems
    frames = sorted(source_frames, key=lambda row: row["frame"])
    _brief_relations(brief, frames, width, height, max_regions, max_crops, problems)
    try:
        expected, total_regions, total_crops, fits = _expected(
            frames, source_regions, width, height, max_regions, max_crops,
            pad_px, budget_tokens)
        size = token_proxy(brief)
    except (TypeError, ValueError, OverflowError):
        problems.append("budget: canonical JSON serialization failed")
        return problems
    if not fits:
        problems.append("budget: impossible fit; protected frame range and motion exceed budget_tokens")
    if size > budget_tokens:
        problems.append("brief: canonical size proxy exceeds budget_tokens")
    count = total_regions - len(brief["regions"]) + total_crops - len(brief["crops"])
    if len(brief["regions"]) > total_regions or len(brief["crops"]) > total_crops:
        problems.append("brief: retained item count exceeds source candidates")
    if brief["truncated"] != count:
        problems.append("brief.truncated: must equal omitted region and merged crop candidates")
    for key in ("frame_range", "grid", "frames"):
        if brief[key] != expected[key]:
            problems.append("brief." + key + ": must preserve the complete source window exactly")
    if brief["regions"] != expected["regions"]:
        problems.append("brief.regions: must match ranked source candidates, flicker summaries, and budget prefix")
    if brief["crops"] != expected["crops"]:
        problems.append("brief.crops: must match padded, clamped, fully merged source crops and budget prefix")
    if brief["truncated"] != expected["truncated"]:
        problems.append("brief.truncated: differs from deterministic caps and regions-before-crops budget removal")
    return problems
