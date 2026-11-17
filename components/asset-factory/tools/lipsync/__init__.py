"""Build and validate a lip-motion track from supplied ElevenLabs media."""

from array import array
from decimal import Decimal
from fractions import Fraction
from functools import cache
from hashlib import sha256
from io import BytesIO
import json
from math import ceil, floor
from pathlib import Path
import re
import sys
import unicodedata
import wave

from asset_factory import core


TRACK_SCHEMA = "af.viseme-track/v1"
ALIGNMENT = "elevenlabs_alignment"
RHUBARB = "rhubarb"
ENVELOPE = "audio_envelope"
PPM = 1_000_000
PCM_FULL_SCALE = 32768
SHAPE_JAW_PPM = {
    "rest": 0,
    "closed": 0,
    "open": 850_000,
    "wide": 500_000,
    "round": 550_000,
    "bite": 240_000,
}
SHAPES = tuple(SHAPE_JAW_PPM)
_SMOOTH_SHAPES = frozenset(("open", "wide", "round", "bite"))
_MAX_CLOSURE_GAP_MS = 150
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "af.viseme-track.v1.schema.json"


class VoiceError(ValueError):
    def __init__(self, code, rc=2):
        self.code = code
        self.rc = rc
        super().__init__(code)


def _pair(value):
    return [value.numerator, value.denominator]


def _rational(pair, code):
    if (type(pair) not in (tuple, list) or len(pair) != 2
            or any(type(item) is not int for item in pair) or pair[1] <= 0):
        raise VoiceError(code)
    value = Fraction(*pair)
    if _pair(value) != list(pair):
        raise VoiceError(code)
    return value


def _frame_rate(pair):
    rate = _rational(pair, "unknown_timebase")
    if rate <= 0:
        raise VoiceError("unknown_timebase")
    return rate


def _silence_threshold(threshold):
    if type(threshold) is not int or not 0 <= threshold < PCM_FULL_SCALE:
        raise VoiceError("invalid_silence_threshold")


def _check_frame_budget(rate, count, fps, max_frames):
    if type(max_frames) is not int or max_frames <= 0:
        raise VoiceError("invalid_frame_budget")
    frames = ceil(Fraction(count, rate) * fps)
    if frames > max_frames:
        raise VoiceError("frame_budget_exceeded", rc=6)
    return frames


def _audio_peaks(audio, *, frame_budget=None):
    """Return the WAV rate and one absolute PCM peak (across channels) per sample time."""
    if type(audio) is not bytes:
        raise VoiceError("invalid_audio")
    if not audio:
        raise VoiceError("empty_audio")
    peaks = array("H")
    try:
        with wave.open(BytesIO(audio), "rb") as reader:
            rate = reader.getframerate()
            count = reader.getnframes()
            channels = reader.getnchannels()
            if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
                raise VoiceError("unsupported_audio_encoding")
            if rate <= 0:
                raise VoiceError("unknown_timebase")
            if count == 0:
                raise VoiceError("empty_audio")
            if frame_budget is not None:
                _check_frame_budget(rate, count, *frame_budget)
            remaining = count
            while remaining:
                frames = min(remaining, 8192)
                raw = reader.readframes(frames)
                if len(raw) != frames * channels * 2:
                    raise VoiceError("invalid_audio")
                samples = array("h", raw)
                if sys.byteorder != "little":
                    samples.byteswap()
                if channels == 1:
                    peaks.extend(map(abs, samples))
                else:
                    peaks.extend(max(map(abs, samples[i:i + channels]))
                                 for i in range(0, len(samples), channels))
                remaining -= frames
    except (wave.Error, EOFError) as exc:
        raise VoiceError("invalid_audio") from exc
    return rate, peaks


def _activity(peaks, threshold, bridge_samples):
    """Return half-open activity intervals. Bridge brief interior PCM zero crossings."""
    start = last = None
    for sample, peak in enumerate(peaks):
        if peak <= threshold:
            continue
        if last is None or sample - last > bridge_samples + 1:
            if last is not None:
                yield start, last + 1
            start = sample
        last = sample
    if last is not None:
        yield start, last + 1


def _read_timing_json(raw, code):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise VoiceError(code)
        return result

    if type(raw) is not bytes:
        raise VoiceError(code)
    try:
        obj = json.loads(raw.decode("utf-8"), parse_float=Decimal,
                         parse_constant=Decimal, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise VoiceError(code) from exc
    if type(obj) is not dict:
        raise VoiceError(code)
    return obj


def _sample_index(seconds, rate, code="invalid_alignment"):
    if type(seconds) not in (int, Decimal):
        raise VoiceError(code)
    if type(seconds) is Decimal and (
        not seconds.is_finite() or not -30 <= seconds.as_tuple().exponent <= 30
    ):
        raise VoiceError(code)
    if seconds < 0:
        raise VoiceError(code)
    return floor(Fraction(seconds) * rate + Fraction(1, 2))


def _character_shape(character):
    base = unicodedata.normalize("NFD", character.casefold())[0]
    for letters, shape in (("mbp", "closed"), ("a", "open"), ("ei", "wide"),
                           ("ou", "round"), ("fv", "bite")):
        if base in letters:
            return shape
    return "rest"


def _parse_elevenlabs_alignment(raw, rate, sample_count):
    obj = _read_timing_json(raw, "invalid_alignment")
    if "alignment" in obj or "normalized_alignment" in obj:
        spoken = obj.get("normalized_alignment")
        obj = obj.get("alignment") if spoken is None else spoken
    if type(obj) is not dict:
        raise VoiceError("invalid_alignment")
    characters = obj.get("characters")
    starts = obj.get("character_start_times_seconds")
    ends = obj.get("character_end_times_seconds")
    if (any(type(values) is not list for values in (characters, starts, ends))
            or not characters or len(characters) != len(starts) or len(starts) != len(ends)):
        raise VoiceError("invalid_alignment")

    graphemes = []
    previous_end = 0
    for character, start, end in zip(characters, starts, ends, strict=True):
        if (type(character) is not str or not character
                or any(unicodedata.category(char) == "Cs" for char in character)):
            raise VoiceError("invalid_alignment")
        first = _sample_index(start, rate)
        stop = _sample_index(end, rate)
        if not previous_end <= start <= end or stop > sample_count:
            raise VoiceError("invalid_alignment")

        decomposed = unicodedata.normalize("NFD", character)
        bases = [index for index, char in enumerate(decomposed)
                 if not unicodedata.category(char).startswith("M")]
        if bases == [0] or (len(character) == 1 and bases):
            graphemes.append((first, stop, _character_shape(character)))
        elif not bases and graphemes and start == previous_end:
            begin, _, shape = graphemes[-1]
            graphemes[-1] = (begin, stop, shape)
        else:
            raise VoiceError("invalid_alignment")
        previous_end = end

    spans = [span for span in graphemes if span[0] < span[1]]
    if not spans:
        raise VoiceError("invalid_alignment")
    return spans, sha256(raw).hexdigest()


def _intersect_activity(spans, peaks, threshold, bridge, max_closure_gap):
    """Clip cues to PCM activity, allowing a brief silent closure between active samples."""
    intervals = iter(_activity(peaks, threshold, bridge))
    active = next(intervals, None)
    previous_end = None
    audible, inactive = [], []
    for first, stop, shape in spans:
        while active and active[1] <= first:
            previous_end, active = active[1], next(intervals, None)
        if not any(peaks[sample] > threshold for sample in range(first, stop)):
            if shape == "closed" and active:
                # An activity interval can already bridge a brief PCM zero crossing.
                inside_bridge = active[0] < first and stop < active[1]
                between_intervals = (previous_end is not None
                                     and stop <= active[0] <= previous_end + max_closure_gap)
                if inside_bridge or between_intervals:
                    audible.append((first, stop, shape))
                    continue
            inactive.append({"start_sample": first, "end_sample": stop, "shape": shape})
            continue
        while active and active[0] < stop:
            begin, end = max(first, active[0]), min(stop, active[1])
            if begin < end:
                if audible and audible[-1][1:] == (begin, shape):
                    audible[-1] = (audible[-1][0], end, shape)
                else:
                    audible.append((begin, end, shape))
            if active[1] >= stop:
                break
            previous_end, active = active[1], next(intervals, None)
    return audible, inactive


def _blend(position, left, right, collar, weights):
    start, boundary, first_shape = left
    next_start, stop, second_shape = right
    if (boundary != next_start or first_shape not in _SMOOTH_SHAPES
            or second_shape not in _SMOOTH_SHAPES):
        return False
    radius = min(collar, Fraction(boundary - start, 2), Fraction(stop - boundary, 2))
    if abs(position - boundary) > radius:
        return False
    second_weight = floor(PPM * (position - boundary + radius) / (2 * radius))
    weights[first_shape] = PPM - second_weight
    weights[second_shape] = second_weight
    return True


def _aligned_mouths(positions, step, spans, collar):
    """Select one cue per held frame, prioritizing complete bilabial closures."""
    first = 0
    for position in positions:
        end = position + step
        while first < len(spans) and spans[first][1] <= position:
            first += 1
        selected = None
        priority = None
        for index in range(first, len(spans)):
            start, stop, shape = spans[index]
            if start >= end:
                break
            overlap = min(stop, end) - max(start, position)
            rank = (shape == "closed", shape != "rest", overlap)
            if priority is None or rank > priority:
                selected, priority = index, rank

        weights = dict.fromkeys(SHAPES, 0)
        if selected is None:
            weights["rest"] = PPM
        else:
            start, _, shape = spans[selected]
            anchor = max(start, position)
            if selected > 0 and _blend(anchor, spans[selected - 1], spans[selected], collar, weights):
                yield weights
                continue
            if selected + 1 < len(spans) and _blend(
                anchor, spans[selected], spans[selected + 1], collar, weights
            ):
                yield weights
                continue
            weights[shape] = PPM
        yield weights


def _envelope_jaw(positions, step, peaks, threshold, merge):
    intervals = iter(_activity(peaks, threshold, merge))
    span = next(intervals, None)
    previous = 0
    for position in positions:
        while span and position >= span[1]:
            span = next(intervals, None)
            previous = 0
        if span is None or position < span[0]:
            previous = 0
            yield 0
            continue
        start = max(span[0], floor(position - step / 2))
        stop = min(span[1], ceil(position + step / 2))
        peak = max(peaks[start:stop])
        target = (peak - threshold) * PPM // (PCM_FULL_SCALE - threshold)
        target = max(1, target)
        previous = (previous + target) // 2 if previous else target
        yield previous


def build_track(audio, alignment, *, fps, silence_peak_pcm, max_frames):
    """Build a track from PCM WAV and optional ElevenLabs timing JSON bytes.

    Only a Python None alignment selects the jaw-only envelope. Embedded audio
    in a timing document is used through media.build_elevenlabs_track.
    """
    return _build_track(audio, alignment, _parse_elevenlabs_alignment,
                        ENVELOPE if alignment is None else ALIGNMENT,
                        fps=fps, silence_peak_pcm=silence_peak_pcm, max_frames=max_frames)


def _build_track(audio, timing, parse_timing, source, *, fps, silence_peak_pcm, max_frames):
    fps = _frame_rate(fps)
    _silence_threshold(silence_peak_pcm)
    rate, peaks = _audio_peaks(audio, frame_budget=(fps, max_frames))
    frame_count = _check_frame_budget(rate, len(peaks), fps, max_frames)

    step = rate / fps
    positions = (index * step for index in range(frame_count))
    bridge = rate // 200
    timing_digest = None
    if source == ENVELOPE:
        rest = dict.fromkeys(SHAPES, 0)
        rest["rest"] = PPM
        controls = ((rest.copy(), jaw) for jaw in
                    _envelope_jaw(positions, step, peaks, silence_peak_pcm, bridge))
    else:
        spans, timing_digest = parse_timing(timing, rate, len(peaks))
        audible, inactive = _intersect_activity(
            spans, peaks, silence_peak_pcm, bridge, rate * _MAX_CLOSURE_GAP_MS // 1000,
        )
        mouths = _aligned_mouths(positions, step, audible, Fraction(rate, 40))
        controls = ((mouth, sum(mouth[name] * level for name, level in SHAPE_JAW_PPM.items()) // PPM)
                    for mouth in mouths)

    frames = []
    for index, (mouth, jaw) in enumerate(controls):
        frames.append({
            "index": index,
            "pts": _pair(index / fps),
            "audio_sample": _pair(index * step),
            "mouth_ppm": mouth,
            "jaw_ppm": jaw,
        })
    track = {
        "schema": TRACK_SCHEMA,
        "source": source,
        "audio_sha256": sha256(audio).hexdigest(),
        "alignment_sha256": timing_digest,
        "sample_rate": rate,
        "sample_count": len(peaks),
        "fps": _pair(fps),
        "silence_peak_pcm": silence_peak_pcm,
        "frames": frames,
    }
    if source != ENVELOPE:
        track["inactive_aligned_spans"] = inactive
    return track


@cache
def _schema():
    # The shared validator checks structure and exact integer types. Scalar
    # bounds, tuple shape and cross-field invariants are checked below, keeping
    # the same runtime available inside Blender's embedded Python.
    schema = core.load_json(_SCHEMA_PATH, max_bytes=16_384)

    def structural(node):
        for keyword in (
            "minimum", "maximum", "maxItems", "prefixItems", "pattern",
            "minLength", "maxLength", "if", "then", "else",
        ):
            node.pop(keyword, None)
        for children in ("properties", "$defs"):
            for child in node.get(children, {}).values():
                structural(child)
        if "items" in node:
            structural(node["items"])

    structural(schema)
    return schema


def validate_track(track):
    """Apply the closed schema and cross-field numeric and timebase invariants."""
    try:
        core.validate(track, _schema())
    except core.ContractError as exc:
        raise VoiceError(exc.code) from exc
    fps = _frame_rate(track["fps"])
    rate, count = track["sample_rate"], track["sample_count"]
    if rate <= 0 or count <= 0:
        raise VoiceError("unknown_timebase")
    _silence_threshold(track["silence_peak_pcm"])
    fallback = track["source"] == ENVELOPE
    pin = track["alignment_sha256"]
    if (re.fullmatch("[0-9a-f]{64}", track["audio_sha256"]) is None
            or (fallback and pin is not None)
            or (not fallback and (pin is None or re.fullmatch("[0-9a-f]{64}", pin) is None))):
        raise VoiceError("invalid_source_identity")

    if len(track["frames"]) != ceil(Fraction(count, rate) * fps):
        raise VoiceError("frame_coverage_mismatch")
    for index, frame in enumerate(track["frames"]):
        pts = index / fps
        if (frame["index"] != index or frame["pts"] != _pair(pts)
                or frame["audio_sample"] != _pair(pts * rate)):
            raise VoiceError("frame_timebase_mismatch")
        mouth = frame["mouth_ppm"]
        if (any(not 0 <= weight <= PPM for weight in mouth.values())
                or sum(mouth.values()) != PPM or not 0 <= frame["jaw_ppm"] <= PPM
                or (fallback and mouth["rest"] != PPM)):
            raise VoiceError("invalid_mouth_weights")

    previous_end = 0
    if fallback and "inactive_aligned_spans" in track:
        raise VoiceError("invalid_source_identity")
    for span in track.get("inactive_aligned_spans", ()):
        start, end = span["start_sample"], span["end_sample"]
        if not previous_end <= start < end <= count:
            raise VoiceError("invalid_inactive_aligned_span")
        previous_end = end
