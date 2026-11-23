"""Optional local Rhubarb adapter for the generic facial rig."""

from hashlib import sha256
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from . import RHUBARB, VoiceError, _audio_peaks, _build_track, _frame_rate, _read_timing_json, _sample_index
from .media import _run_local_tool


# B and H use the existing narrow/wide pose. The generic rig has no distinct
# teeth-together or raised-tongue controls.
_SHAPES = {
    "A": "closed",
    "B": "wide",
    "C": "wide",
    "D": "open",
    "E": "round",
    "F": "round",
    "G": "bite",
    "H": "wide",
    "X": "rest",
}


def _canonical_seconds(value):
    """Render a validated JSON number exactly in decimal notation."""
    if type(value) is int:
        return str(value)
    if value == 0:
        return "0"
    number = format(value, "f")
    return number.rstrip("0").rstrip(".") if "." in number else number


def _parse_cues(raw, rate, sample_count):
    error = "invalid_rhubarb_cues"
    cues = _read_timing_json(raw, error).get("mouthCues")
    if type(cues) is not list or not cues:
        raise VoiceError(error)
    spans, canonical_cues = [], []
    previous_end = 0
    for cue in cues:
        if type(cue) is not dict or set(cue) != {"start", "end", "value"}:
            raise VoiceError(error)
        shape = cue["value"]
        if type(shape) is not str or shape not in _SHAPES:
            raise VoiceError(error)
        start, end = cue["start"], cue["end"]
        first = _sample_index(start, rate, error)
        stop = _sample_index(end, rate, error)
        if not previous_end <= start < end or stop > sample_count:
            raise VoiceError(error)
        previous_end = end
        canonical_cues.append(
            f'{{"end":{_canonical_seconds(end)},"start":{_canonical_seconds(start)},"value":"{shape}"}}'
        )
        if first < stop:
            spans.append((first, stop, _SHAPES[shape]))
    if not spans:
        raise VoiceError(error)
    canonical = "[" + ",".join(canonical_cues) + "]\n"
    return spans, sha256(canonical.encode("ascii")).hexdigest()


def track_from_cues(audio, cues, *, fps, silence_peak_pcm, max_frames):
    """Map raw Rhubarb JSON and its input WAV onto af.viseme-track/v1."""
    return _build_track(audio, cues, _parse_cues, RHUBARB, fps=fps,
                        silence_peak_pcm=silence_peak_pcm, max_frames=max_frames)


def build_rhubarb_track(audio, *, binary, language, fps, silence_peak_pcm, max_frames, timeout):
    """Return a viseme track and exact Rhubarb JSON from a local, guarded run."""
    if type(language) is not str or re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language) is None:
        raise VoiceError("rhubarb_language_required", rc=3)
    recognizer = "pocketSphinx" if language.lower().split("-", 1)[0] == "en" else "phonetic"
    # Admit the WAV and its budget before invoking a recognizer.
    _audio_peaks(audio, frame_budget=(_frame_rate(fps), max_frames))
    with TemporaryDirectory(prefix="af-voice-rhubarb-") as directory:
        root = Path(directory)
        (root / "clip.wav").write_bytes(audio)
        cues = _run_local_tool(
            [os.fspath(binary), "-q", "--threads", "1", "-r", recognizer, "-f", "json", "clip.wav"],
            cwd=root, timeout=timeout, failure_code="rhubarb_run_failed",
        )
    return track_from_cues(audio, cues, fps=fps, silence_peak_pcm=silence_peak_pcm,
                           max_frames=max_frames), cues
