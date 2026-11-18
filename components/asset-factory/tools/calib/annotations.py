"""Join independent sample labels to observed presentation intervals."""
from bisect import bisect_left, bisect_right
from fractions import Fraction

from tools.lipsync import PPM, SHAPES

from .common import digest, fields, parse, require

ONSET = "onset_error_ns"
CLOSURE = "closure_error_ppm"
SILENCE = "silence_movement_ppm"
SHAPE_ORDER = ("rest", *(shape for shape in SHAPES if shape != "rest"))


def _rational(pair):
    require(type(pair) is list and len(pair) == 2 and all(type(v) is int for v in pair)
            and pair[0] >= 0 and pair[1] > 0, 2, "invalid_presentation_pts")
    return Fraction(*pair)


def _ordered(pts):
    return all(a < b for a, b in zip(pts, pts[1:]))


def presentation(timeline, duration):
    starts = [_rational(pair) for pair in timeline["pts"]]
    end = _rational(timeline["end_pts"])
    require(starts and starts[0] == 0 and _ordered(starts) and starts[-1] < duration <= end,
            2, "invalid_presentation_coverage")
    return starts, end


def _spans(rows, count):
    previous_end = 0
    for span in rows:
        require(type(span) is list and len(span) == 2 and all(type(n) is int for n in span)
                and previous_end <= span[0] < span[1] <= count, 2, "invalid_annotation_span")
        previous_end = span[1]


def labels(raw, case, fallback):
    require(raw is not None, 3, "annotations_missing")
    require(type(raw) is bytes, 2, "expected_json_bytes")
    require(digest(raw) == case["annotation_digest"], 4, "annotation_digest_changed")
    data = parse(raw)
    fields(data, "annotator", "audio_digest", "onsets", "closures", "silences")
    require(data["audio_digest"] == case["audio_digest"] and data["annotator"] == case["annotator"],
            4, "annotation_identity_changed")
    onsets, closures, silences = data["onsets"], data["closures"], data["silences"]
    count = case["sample_count"]
    require(all(type(rows) is list for rows in (onsets, closures, silences)), 2, "invalid_annotations")
    expected = case["annotation_counts"]
    require((len(onsets), len(closures), len(silences)) == (expected[ONSET], expected[CLOSURE], expected[SILENCE]),
            3, "annotation_coverage_incomplete")
    allowed_shapes = ("jaw",) if fallback else SHAPE_ORDER[1:]
    previous = -1
    for row in onsets:
        require(type(row) is list and len(row) == 2 and type(row[0]) is int
                and previous < row[0] < count and row[1] in allowed_shapes, 2, "invalid_annotation_onset")
        previous = row[0]
    _spans(closures, count)
    _spans(silences, count)

    silence_ends = [end for _, end in silences]
    for start, end in [*((position, position + 1) for position, _ in onsets), *closures]:
        index = bisect_right(silence_ends, start)
        require(index == len(silences) or end <= silences[index][0], 2, "conflicting_annotations")
    return onsets, closures, silences


def _ppm(value):
    return type(value) is int and 0 <= value <= PPM


def render_frames(raw, case, track_digest, fallback):
    require(raw is not None, 3, "render_observations_missing")
    render = parse(raw)
    fields(render, "observer", "audio_digest", "track_digest", "frames", "end_pts")
    require(render["observer"] == case["render_observer"] and render["audio_digest"] == case["audio_digest"]
            and render["track_digest"] == track_digest, 4, "render_identity_changed")
    frames = render["frames"]
    expected, end = presentation(case["presentation"], Fraction(case["sample_count"], case["sample_rate"]))
    require(type(frames) is list, 2, "invalid_render_frames")
    require(len(frames) == len(expected), 3, "presentation_frame_coverage_changed")
    require(_rational(render["end_pts"]) == end, 4, "presentation_end_changed")
    for frame, pts in zip(frames, expected, strict=True):
        fields(frame, "pts", "mouth_ppm", "jaw_ppm")
        require(_rational(frame["pts"]) == pts, 4, "presentation_pts_changed")
        weights = frame["mouth_ppm"]
        fields(weights, *SHAPES)
        require(all(_ppm(w) for w in weights.values()) and sum(weights.values()) == PPM
                and _ppm(frame["jaw_ppm"]) and (not fallback or weights["rest"] == PPM),
                2, "invalid_observed_mouth_weights")
    return frames, expected


def metric(expected, checked=0, value=None):
    return {"expected": expected, "checked": checked,
            "value": [value.numerator, value.denominator] if value is not None else None}


def measure(observed, annotations, rate, fallback):
    frames, pts = observed
    onsets, closures, silences = annotations
    reasons = {}
    events = []
    previous = "rest"
    for frame, timestamp in zip(frames, pts, strict=True):
        shape = (("jaw" if frame["jaw_ppm"] else "rest") if fallback
                 else max(SHAPE_ORDER, key=frame["mouth_ppm"].__getitem__))
        if shape != previous and shape != "rest":
            events.append((timestamp, shape))
        previous = shape

    metrics = {ONSET: metric(len(onsets)), SILENCE: metric(len(silences))}
    if [shape for _, shape in onsets] != [shape for _, shape in events]:
        reasons[ONSET] = (5, "rendered_onset_sequence_changed")
    else:
        error = max(abs(actual - Fraction(sample, rate)) for (sample, _), (actual, _) in
                    zip(onsets, events, strict=True))
        metrics[ONSET] = metric(len(onsets), len(onsets), error * 1_000_000_000)

    if not fallback:
        errors = []
        for start, end in closures:
            first = bisect_left(pts, Fraction(start, rate))
            stop = bisect_left(pts, Fraction(end, rate))
            if first < stop:
                errors.append(max(max(f["jaw_ppm"], PPM - f["mouth_ppm"]["closed"]) for f in frames[first:stop]))
        covered = len(errors)
        metrics[CLOSURE] = metric(len(closures), covered, max(errors) if covered == len(closures) else None)
        if covered < len(closures):
            reasons[CLOSURE] = (3, "closure_presentation_coverage_missing")

    # A rendered pose holds until the next PTS. Include a frame that starts
    # before a labelled silence whenever part of its hold overlaps the silence.
    movement = 0
    for start, end in silences:
        first = bisect_right(pts, Fraction(start, rate)) - 1
        stop = bisect_left(pts, Fraction(end, rate))
        peak = max(max(f["jaw_ppm"], PPM - f["mouth_ppm"]["rest"]) for f in frames[first:stop])
        movement = max(movement, peak)
    metrics[SILENCE] = metric(len(silences), len(silences), movement)
    return metrics, reasons
