"""Lossless local presentation mux and decoded video/PCM clock verification."""

from fractions import Fraction
from hashlib import sha256
from math import ceil, lcm
from pathlib import Path
import tempfile
import wave

from asset_factory import core
from tools.render.pipeline import Refusal, run


_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC", "CUDA_VISIBLE_DEVICES": ""}
_MAX_AUDIO_BYTES = 64 * 2**20
_MAX_VIDEO_BYTES = 8 * 2**30
_FFMPEG = ("-nostdin", "-v", "error", "-xerror", "-threads", "1")
_LOCAL = ("-protocol_whitelist", "file")


def audio_pcm(path, clock):
    """Authenticate an exact PCM-s16 source before any mux process runs."""
    if clock is None:
        if path is not None:
            raise Refusal(4, "unexpected_narration")
        return None
    if path is None:
        raise Refusal(3, "narration_unavailable")
    if path.stat().st_size > _MAX_AUDIO_BYTES:
        raise Refusal(6, "audio_byte_budget")
    try:
        with wave.open(str(path), "rb") as source:
            rate, count, channels = source.getframerate(), source.getnframes(), source.getnchannels()
            if source.getsampwidth() != 2 or not 1 <= channels <= 2 or source.getcomptype() != "NONE":
                raise Refusal(2, "narration_pcm16_required")
            if rate != clock["sample_rate"] or count != clock["sample_count"] or count <= 0:
                raise Refusal(4, "narration_clock_mismatch")
            pcm = source.readframes(count + 1)
    except (EOFError, wave.Error) as exc:
        raise Refusal(2, "invalid_narration_wav") from exc
    if len(pcm) != count * channels * 2:
        raise Refusal(4, "narration_pcm_size_mismatch")
    return {"sample_rate": rate, "sample_count": count, "channels": channels,
            "pcm_sha256": sha256(pcm).hexdigest()}


def validate_timing(schedule, fps, clock):
    """Admit only timelines the lossless CFR mux can represent exactly."""
    fps = Fraction(*fps)
    if not schedule or fps <= 0:
        raise Refusal(3, "presentation_mux_timing_unqualified")
    for index, slot in enumerate(schedule):
        times = tuple(Fraction(*pair) for pair in slot["time"])
        if slot["frame"] != schedule[0]["frame"] + index or times != (index / fps, (index + 1) / fps):
            raise Refusal(3, "presentation_mux_timing_unqualified")
        expected = [ceil(time * clock["sample_rate"]) for time in times] if clock else None
        if slot["samples"] != expected:
            raise Refusal(4, "presentation_audio_schedule_mismatch")
    if clock and schedule[-1]["samples"][1] != clock["sample_count"]:
        raise Refusal(4, "presentation_audio_schedule_mismatch")
    movie_scale = lcm(fps.numerator, clock["sample_rate"] if clock else 1)
    if movie_scale > 2**31 - 1:
        raise Refusal(3, "unsupported_mux_timebase")
    return fps, movie_scale


def mux(root, film, *, schedule, fps, audio, pcm, ffmpeg, timeout, scratch=None):
    """Encode the contiguous, receipt-verified render sequence without frame conversion."""
    fps, movie_scale = validate_timing(schedule, fps, pcm)
    command = [ffmpeg, *_FFMPEG, *_LOCAL, "-f", "image2", "-framerate", str(fps),
               "-start_number", schedule[0]["frame"], "-start_number_range", 1,
               "-i", root / "low/%06d/denoised.png"]
    if pcm:
        command += [*_LOCAL, "-f", "wav", "-i", audio, "-map", "1:a:0", "-c:a", "pcm_s16le"]
    command += ["-map", "0:v:0", "-c:v", "png", "-pix_fmt", "rgb24", "-threads:v", 1,
                "-fps_mode", "passthrough", "-video_track_timescale", fps.numerator,
                "-movie_timescale", movie_scale, "-map_metadata", "-1", "-map_chapters", "-1",
                "-f", "mov", "-n", film]
    run(command, (scratch if scratch is not None else film.parent) / "mux.log", timeout, env=_ENV)


def _stream_clock(stream):
    base = Fraction(stream["time_base"])
    start, duration = int(stream["start_pts"]), int(stream["duration_ts"])
    if base <= 0 or start != 0 or duration <= 0:
        raise Refusal(4, "decoded_stream_clock_mismatch")
    return base, base * duration


def _frame_duration(row):
    # FFprobe versions expose duration under either spelling. Both, when
    # supplied, must agree in integer stream ticks.
    values = []
    for field in ("duration", "pkt_duration"):
        if field in row:
            value = row[field]
            if type(value) not in (int, str) or type(value) is str and not value.isdecimal():
                raise ValueError("invalid frame duration")
            values.append(int(value))
    if not values or min(values) <= 0 or len(set(values)) != 1:
        raise ValueError("missing or inconsistent frame duration")
    return values[0]


def _decoded_timeline(report, schedule, width, height, pcm):
    """Compare decoded PTS and extents against the rational schedule."""
    streams = report["streams"]
    video = [stream for stream in streams if stream["codec_type"] == "video"]
    audio = [stream for stream in streams if stream["codec_type"] == "audio"]
    if len(video) != 1 or len(audio) != bool(pcm) or len(streams) != 1 + bool(pcm):
        raise Refusal(4, "decoded_stream_membership_mismatch")
    video = video[0]
    if (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) != ("png", "rgb24", width, height):
        raise Refusal(4, "decoded_video_format_mismatch")
    frames = report["frames"]
    video_frames = [row for row in frames if row["stream_index"] == video["index"]]
    if len(video_frames) != len(schedule):
        raise Refusal(4, "decoded_frame_count_mismatch")
    base, duration = _stream_clock(video)
    if duration != Fraction(*schedule[-1]["time"][1]):
        raise Refusal(4, "decoded_video_duration_mismatch")
    timeline = []
    for row, slot in zip(video_frames, schedule):
        start, end = map(lambda pair: Fraction(*pair), slot["time"])
        pts = int(row["pts"])
        ticks = _frame_duration(row)
        if (row["media_type"] != "video" or row["width"] != width or row["height"] != height
                or base * pts != start or base * ticks != end - start):
            raise Refusal(4, "decoded_video_pts_mismatch")
        timeline.append({"frame": slot["frame"], "pts": pts, "duration": ticks})

    audio_timeline = []
    if pcm:
        audio = audio[0]
        rate = pcm["sample_rate"]
        if (audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) != (
                "pcm_s16le", rate, pcm["channels"]):
            raise Refusal(4, "decoded_audio_format_mismatch")
        audio_base, audio_duration = _stream_clock(audio)
        if audio_duration * rate != pcm["sample_count"]:
            raise Refusal(4, "decoded_audio_duration_mismatch")
        offset = 0
        for row in frames:
            if row["stream_index"] != audio["index"]:
                continue
            pts, samples, ticks = int(row["pts"]), int(row["nb_samples"]), _frame_duration(row)
            if (row["media_type"] != "audio" or row["channels"] != pcm["channels"] or samples <= 0
                    or audio_base * pts * rate != offset or audio_base * ticks * rate != samples):
                raise Refusal(4, "decoded_audio_pts_mismatch")
            audio_timeline.append({"pts": pts, "start_sample": offset, "sample_count": samples})
            offset += samples
        if offset != pcm["sample_count"]:
            raise Refusal(4, "decoded_sample_count_mismatch")
    if len(frames) != len(timeline) + len(audio_timeline):
        raise Refusal(4, "unexpected_decoded_frame")
    return {"video_time_base": str(base), "video": timeline,
            "audio_time_base": str(audio_base) if pcm else None, "audio": audio_timeline}


def validate_video_budget(width, height, frame_count):
    if min(width, height, frame_count) <= 0 or width * height * 3 * (frame_count + 1) > _MAX_VIDEO_BYTES:
        raise Refusal(6, "decoded_video_byte_budget")


def decode(film, scratch, *, schedule, width, height, pcm, ffmpeg, ffprobe, timeout):
    """Probe decoded frames, then decode every RGB pixel and verify every PCM byte."""
    frame_bytes = width * height * 3
    validate_video_budget(width, height, len(schedule))
    if film.stat().st_size > _MAX_VIDEO_BYTES:
        raise Refusal(6, "decoded_video_byte_budget")
    if pcm:
        rate = pcm["sample_rate"]
        for slot in schedule:
            if slot["samples"] != [ceil(Fraction(*time) * rate) for time in slot["time"]]:
                raise Refusal(4, "presentation_audio_schedule_mismatch")
        if schedule[-1]["samples"][1] != pcm["sample_count"]:
            raise Refusal(4, "presentation_audio_schedule_mismatch")
    elif any(slot["samples"] is not None for slot in schedule):
        raise Refusal(4, "presentation_audio_schedule_mismatch")
    # FFmpeg can decline an existing output path without a failing exit code.
    scratch = Path(tempfile.mkdtemp(prefix="decode-", dir=scratch))
    raw = scratch / "decoded.rgb"
    before = core.sha256_file(film)
    probe = scratch / "decoded-timeline.json"
    run([ffprobe, "-v", "error", "-threads", 1, *_LOCAL, "-f", "mov", "-i", film,
         "-show_streams", "-show_frames", "-show_entries",
         "stream=index,codec_type,codec_name,pix_fmt,width,height,time_base,start_pts,duration_ts,sample_rate,channels:"
         "frame=stream_index,media_type,pts,duration,pkt_duration,width,height,nb_samples,channels",
         "-of", "json"], probe, timeout, env=_ENV)
    try:
        timeline = _decoded_timeline(core.load_json(probe, max_bytes=32 * 2**20),
                                     schedule, width, height, pcm)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise Refusal(4, "decoded_pts_evidence_invalid") from exc
    source = [ffmpeg, *_FFMPEG, *_LOCAL, "-f", "mov", "-i", film]
    run([*source, "-map", "0:v:0", "-fps_mode", "passthrough", "-pix_fmt", "rgb24",
         "-frames:v", len(schedule) + 1, "-threads:v", 1, "-f", "rawvideo", "-n", raw],
        scratch / "decode-video.log", timeout, env=_ENV)
    if raw.stat().st_size != frame_bytes * len(schedule):
        raise Refusal(4, "decoded_frame_size_or_count_mismatch")
    if pcm:
        samples = scratch / "decoded.pcm"
        run([*source, "-map", "0:a:0", "-c:a", "pcm_s16le", "-fs", _MAX_AUDIO_BYTES + 1,
             "-f", "s16le", "-n", samples], scratch / "decode-audio.log", timeout, env=_ENV)
        if (samples.stat().st_size != pcm["sample_count"] * pcm["channels"] * 2
                or core.sha256_file(samples) != pcm["pcm_sha256"]):
            raise Refusal(4, "decoded_audio_payload_mismatch")
    if core.sha256_file(film) != before:
        raise Refusal(4, "film_changed_during_decode")
    return raw, {**timeline, "film_sha256": before, "pcm": pcm}
