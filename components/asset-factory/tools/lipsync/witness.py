"""Diagnostic timing checks against an independent, sample-indexed witness."""

from bisect import bisect_left, bisect_right
from fractions import Fraction
from hashlib import sha256

from asset_factory import core

from . import (
    ENVELOPE, PPM, SHAPES, VoiceError, _activity, _audio_peaks, _frame_rate, _pair, _silence_threshold,
    validate_track,
)


def _receipt(rc, reasons, **metrics):
    return {
        "schema": "af.voice-witness-receipt/v1",
        "rc": rc,
        "status": core.STATUS[rc],
        "fixture_verdict": core.VERDICT[rc],
        "production_verdict": "CANNOT_SEE",
        "criterion_basis": "TARGET",
        "reason_codes": reasons,
        **metrics,
    }


def _valid_onsets(onsets, count):
    return (all(type(sample) is int and 0 <= sample < count for sample in onsets)
            and list(onsets) == sorted(set(onsets)))


def _labels(witness, fps, rate, count, fallback):
    if type(witness) is not dict:
        raise VoiceError("witness_coverage_missing", rc=3)
    if any(type(witness.get(field)) is not int or witness[field] <= 0
           for field in ("sample_rate", "sample_count")):
        raise VoiceError("witness_timebase_missing", rc=3)
    if (witness["sample_rate"], witness["sample_count"]) != (rate, count):
        raise VoiceError("witness_timebase_changed", rc=4)
    try:
        expected_fps = _frame_rate(witness.get("fps"))
    except VoiceError as exc:
        raise VoiceError("witness_timebase_missing", rc=3) from exc
    if expected_fps != fps:
        raise VoiceError("witness_timebase_changed", rc=4)
    audio, silence = witness.get("audio_onsets"), witness.get("silences")
    mouth = witness.get("mouth_onsets") if not fallback else None
    required = (audio, silence) if fallback else (audio, silence, mouth)
    if any(type(rows) not in (list, tuple) or not rows for rows in required):
        raise VoiceError("witness_coverage_missing", rc=3)
    if not _valid_onsets(audio, count):
        raise VoiceError("invalid_witness")
    if not fallback and (
        any(type(row) not in (list, tuple) or len(row) != 2
            or row[1] not in SHAPES or row[1] == "rest" for row in mouth)
        or not _valid_onsets([row[0] for row in mouth], count)
    ):
        raise VoiceError("invalid_witness")
    end = 0
    for span in silence:
        if (type(span) not in (tuple, list) or len(span) != 2
                or any(type(sample) is not int for sample in span)
                or not end <= span[0] < span[1] <= count):
            raise VoiceError("invalid_witness")
        end = span[1]
    shape_onsets = [(i, "jaw") for i in audio] if fallback else list(map(tuple, mouth))
    shape_positions = [position for position, _ in shape_onsets]
    for start, end in silence:
        for onsets in (audio, shape_positions):
            if bisect_left(onsets, start) != bisect_left(onsets, end):
                raise VoiceError("invalid_witness")
    return audio, shape_onsets, silence


def _mouth_events(frames, positions, fallback):
    previous = "rest"
    events = []
    closure_movement = 0
    for frame, position in zip(frames, positions, strict=True):
        weights = frame["mouth_ppm"]
        if fallback:
            shape = "jaw" if frame["jaw_ppm"] else "rest"
        else:
            shape = max(SHAPES, key=weights.__getitem__)
        if shape == "closed":
            closure_movement = max(closure_movement, frame["jaw_ppm"], PPM - weights["closed"])
        if shape != previous and shape != "rest":
            events.append((position, shape))
        previous = shape
    return events, closure_movement


def _onset_error(actual, expected, frames_per_sample):
    return _pair(max(abs(a - e) for a, e in zip(actual, expected, strict=True)) * frames_per_sample)


def check_timing(track, audio, witness, *, onset_limit_frames, bridge_samples, silence_peak_pcm):
    """Require actual WAV and rig onsets, closure and covered silent frames.

    The caller owns the independent witness, the fixed policy parameters and
    any production calibration. Onsets are measured separately before joining.
    """
    try:
        validate_track(track)
        if any(type(value) is not int or value < 0 for value in (onset_limit_frames, bridge_samples)):
            raise VoiceError("invalid_witness_policy")
        _silence_threshold(silence_peak_pcm)
        if silence_peak_pcm != track["silence_peak_pcm"]:
            raise VoiceError("silence_policy_changed", rc=4)
        if type(audio) is not bytes:
            raise VoiceError("invalid_audio")
        if sha256(audio).hexdigest() != track["audio_sha256"]:
            raise VoiceError("audio_identity_changed", rc=4)
        rate, peaks = _audio_peaks(audio)
        if (rate, len(peaks)) != (track["sample_rate"], track["sample_count"]):
            raise VoiceError("audio_timebase_changed", rc=4)
        fallback = track["source"] == ENVELOPE
        fps = Fraction(*track["fps"])
        audio_labels, mouth_labels, silences = _labels(witness, fps, rate, len(peaks), fallback)
    except (VoiceError, core.ContractError) as exc:
        return _receipt(exc.rc, [exc.code])

    frames_per_sample = fps / rate
    frames = track["frames"]
    positions = [Fraction(*frame["audio_sample"]) for frame in frames]
    audio_onsets = [start for start, _ in _activity(peaks, silence_peak_pcm, bridge_samples)]
    mouth_events, closure_movement = _mouth_events(frames, positions, fallback)
    failures = []

    audio_error = mouth_error = joint_error = None
    if len(audio_onsets) == len(audio_labels):
        audio_error = _onset_error(audio_onsets, audio_labels, frames_per_sample)
        if Fraction(*audio_error) > onset_limit_frames:
            failures.append("audio_onset_out_of_bound")
    else:
        failures.append("audio_onset_coverage_changed")
    if [shape for _, shape in mouth_events] == [shape for _, shape in mouth_labels]:
        mouth_error = _onset_error([i for i, _ in mouth_events], [i for i, _ in mouth_labels],
                                   frames_per_sample)
        if Fraction(*mouth_error) > onset_limit_frames:
            failures.append("mouth_onset_out_of_bound")
    else:
        failures.append("mouth_onset_coverage_changed")
    if audio_error is not None and mouth_error is not None:
        audio_by_label = dict(zip(audio_labels, audio_onsets, strict=True))
        paired = [(audio_by_label[label], actual)
                  for (label, _), (actual, _) in zip(mouth_labels, mouth_events, strict=True)
                  if label in audio_by_label]
        if paired:
            audio_pair, mouth_pair = zip(*paired, strict=True)
            joint_error = _onset_error(audio_pair, mouth_pair, frames_per_sample)
            if Fraction(*joint_error) > onset_limit_frames:
                failures.append("audio_mouth_onset_out_of_bound")
    if closure_movement:
        failures.append("incomplete_bilabial_closure")

    interval_controls = "inactive_aligned_spans" in track
    silence_checked = silence_unobserved = movement = silence_peak = 0
    for start, end in silences:
        silence_peak = max(silence_peak, max(peaks[start:end]))
        first = bisect_left(positions, start)
        if not interval_controls or end == len(peaks):
            stop = bisect_left(positions, end)
        else:
            # Held aligned controls cover a forward frame interval. Boundary
            # frames may also contain speech, and do not witness pure silence.
            stop = bisect_right(positions, end - rate / fps)
        if first >= stop:
            silence_unobserved += 1
        for frame in frames[first:stop]:
            silence_checked += 1
            movement = max(movement, frame["jaw_ppm"], PPM - frame["mouth_ppm"]["rest"])
    if silence_peak > silence_peak_pcm:
        failures.append("labelled_silence_contains_audio")
    if movement:
        failures.append("mouth_moves_in_silence")
    rc = 5 if failures else 3 if silence_unobserved else 0
    if silence_unobserved:
        failures.append("silence_frame_coverage_missing")
    return _receipt(
        rc,
        failures,
        source=track["source"],
        audio_sha256=track["audio_sha256"],
        track_sha256=core.sha256_json(track),
        witness_sha256=core.sha256_json({
            "fps": _pair(fps), "sample_rate": rate, "sample_count": len(peaks),
            "audio_onsets": list(audio_labels), "mouth_onsets": [list(row) for row in mouth_labels],
            "silences": [list(row) for row in silences],
        }),
        audio_onsets_expected=len(audio_labels),
        audio_onsets_observed=len(audio_onsets),
        audio_onsets_checked=len(audio_labels) if audio_error is not None else 0,
        audio_onset_error_frames=audio_error,
        mouth_onsets_expected=len(mouth_labels),
        mouth_onsets_observed=len(mouth_events),
        mouth_onsets_checked=len(mouth_labels) if mouth_error is not None else 0,
        mouth_onset_error_frames=mouth_error,
        audio_mouth_onset_error_frames=joint_error,
        max_closure_movement_ppm=closure_movement,
        silence_frames_checked=silence_checked,
        silence_intervals_expected=len(silences),
        silence_intervals_unobserved=silence_unobserved,
        max_silence_movement_ppm=movement if silence_checked else None,
        max_labelled_silence_pcm=silence_peak,
        silence_peak_pcm=silence_peak_pcm,
        onset_limit_frames=onset_limit_frames,
        bridge_samples=bridge_samples,
    )
