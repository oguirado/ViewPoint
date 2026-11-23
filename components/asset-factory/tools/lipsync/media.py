"""Offline media boundaries for ElevenLabs responses and the Rhubarb trial."""

import base64
import binascii
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
from math import ceil
import os
from pathlib import Path
import re
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
import wave

from asset_factory import proc

from . import VoiceError, _check_frame_budget, _frame_rate, _read_timing_json, build_track


def build_elevenlabs_track(response, *, output_format, fps, silence_peak_pcm, max_frames,
                           ffmpeg=None, ffprobe=None, timeout=None):
    """Return the aligned track and exact WAV bytes for its timing witness.

    Declare the requested output_format. Timing-critical MP3 also requires
    explicit local FFmpeg tools and packet-level gapless evidence.
    """
    obj = _read_timing_json(response, "invalid_elevenlabs_response")
    encoded = obj.get("audio_base64")
    if type(encoded) is not str:
        raise VoiceError("invalid_audio_base64")
    if type(output_format) is not str:
        raise VoiceError("unknown_timebase")

    mp3 = output_format == "mp3" or output_format.startswith("mp3_")
    mp3_format = re.fullmatch(r"mp3_([1-9][0-9]{0,9})_[1-9][0-9]{0,9}", output_format)
    pcm_format = re.fullmatch(r"pcm_([1-9][0-9]{0,9})", output_format)
    if mp3 and (ffmpeg is None or ffprobe is None or timeout is None):
        raise VoiceError("mp3_timing_unproven", rc=3)
    if pcm_format is None and mp3_format is None and output_format != "mp3":
        raise VoiceError("unknown_timebase")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceError("invalid_audio_base64") from exc
    if not payload:
        raise VoiceError("empty_audio")

    if mp3:
        audio, receipt = decode_mp3_timed(payload, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                         fps=fps, max_frames=max_frames, timeout=timeout)
        if mp3_format and int(mp3_format[1]) != receipt["sample_rate"]:
            raise VoiceError("audio_timebase_changed", rc=4)
    else:
        audio = _pcm_wav(payload, int(pcm_format[1]), _frame_rate(fps), max_frames)
    # Require the response's explicit timing members. The standalone direct
    # alignment format is a separate entry point.
    if not {"alignment", "normalized_alignment"}.intersection(obj):
        raise VoiceError("invalid_alignment")
    return build_track(audio, response, fps=fps, silence_peak_pcm=silence_peak_pcm,
                       max_frames=max_frames), audio


def _pcm_wav(pcm, rate, fps, max_frames):
    if not 0 < rate <= 0xFFFFFFFF // 2:
        raise VoiceError("unknown_timebase")
    if len(pcm) % 2 or len(pcm) > 0xFFFFFFFF - 36:
        raise VoiceError("invalid_audio")
    _check_frame_budget(rate, len(pcm) // 2, fps, max_frames)
    wav = BytesIO()
    with wave.open(wav, "wb") as writer:
        writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(pcm)
    return wav.getvalue()


def _run_local_tool(argv, *, cwd, timeout, failure_code):
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise VoiceError("invalid_tool_timeout")
    try:
        job = proc.popen(argv, cwd=cwd, start_new_session=True, runtime_s=timeout,
                         env={"PATH": os.defpath, "LANG": "C"})
    except (OSError, proc.SignalRefusal) as exc:
        raise VoiceError(failure_code, rc=3) from exc
    try:
        stdout, _ = proc.communicate(job, timeout=timeout)
        if job.returncode != 0:
            raise VoiceError(failure_code, rc=3)
        return stdout
    except TimeoutExpired as exc:
        raise VoiceError("voice_tool_timeout", rc=6) from exc
    except (OSError, proc.SignalRefusal) as exc:
        raise VoiceError(failure_code, rc=3) from exc
    finally:
        if proc.cleanup(job):
            raise VoiceError("voice_tool_cleanup_unproven", rc=3)


def _decode_mp3(root, ffmpeg, fps, max_frames, timeout):
    # Leave room beyond the requested budget so overlength audio is detected
    # from the WAV header. FFmpeg duration options have microsecond precision.
    limit_us = ceil(Fraction((max_frames + 1) * 1_000_000) / fps)
    _run_local_tool(
        [os.fspath(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
         "-protocol_whitelist", "file", "-f", "mp3", "-i", "clip.mp3",
         "-map", "0:a:0", "-map_metadata", "-1", "-t", f"{limit_us}us",
         "-c:a", "pcm_s16le", "-f", "wav", "decoded.wav"],
        cwd=root, timeout=timeout, failure_code="mp3_decode_failed",
    )
    path = root / "decoded.wav"
    try:
        with wave.open(os.fspath(path), "rb") as decoded:
            rate, count, channels = decoded.getframerate(), decoded.getnframes(), decoded.getnchannels()
            if rate <= 0 or decoded.getsampwidth() != 2 or decoded.getcomptype() != "NONE":
                raise VoiceError("mp3_decode_failed", rc=3)
            if count == 0:
                raise VoiceError("empty_audio")
            _check_frame_budget(rate, count, fps, max_frames)
        return path.read_bytes(), (rate, count, channels)
    except (wave.Error, EOFError, OSError) as exc:
        raise VoiceError("mp3_decode_failed", rc=3) from exc


def _gapless_clock(probe):
    """Check the packet clock and both trim witnesses before starting a timed decode."""
    try:
        obj = _read_timing_json(probe, "mp3_timing_unproven")
        stream, = obj["streams"]
        rate = int(stream["sample_rate"])
        channels = stream["channels"]
        sample_unit = Fraction(stream["time_base"]) * rate
        packets = obj["packets"]
        if (stream["codec_name"] != "mp3" or rate <= 0 or type(channels) is not int
                or channels not in (1, 2) or sample_unit <= 0 or not packets):
            raise ValueError("invalid stream clock")

        ticks = leading = trailing = 0
        for index, row in enumerate(packets):
            pts, duration = row["pts"], row["duration"]
            if (type(pts) is not int or pts != ticks or row["stream_index"] != stream["index"]
                    or type(duration) is not int or duration <= 0
                    or (sample_unit * duration).denominator != 1):
                raise ValueError("invalid packet clock")
            ticks += duration
            for data in row.get("side_data_list", ()):
                if data.get("side_data_type") != "Skip Samples":
                    continue
                skip, discard = data["skip_samples"], data["discard_padding"]
                if (type(skip) is not int or type(discard) is not int or skip < 0 or discard < 0
                        or data["skip_reason"] != 0 or data["discard_reason"] != 0):
                    raise ValueError("invalid trim")
                if skip:
                    if index != 0 or leading:
                        raise ValueError("ambiguous leading trim")
                    leading = skip
                if discard:
                    if index != len(packets) - 1 or trailing:
                        raise ValueError("ambiguous trailing trim")
                    trailing = discard

        untrimmed = int(sample_unit * ticks)
        count = untrimmed - leading - trailing
        if not leading or not trailing or count <= 0:
            raise ValueError("missing gapless trim witness")
        return {
            "sample_rate": rate, "sample_count": count, "channels": channels,
            "untrimmed_samples": untrimmed,
            "leading_skip_samples": leading, "trailing_discard_samples": trailing,
            "packet_probe_sha256": sha256(probe).hexdigest(),
        }
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise VoiceError("mp3_timing_unproven", rc=3) from exc


def _mp3_input(mp3, fps, max_frames):
    if type(mp3) is not bytes:
        raise VoiceError("invalid_audio")
    if not mp3:
        raise VoiceError("empty_audio")
    fps = _frame_rate(fps)
    _check_frame_budget(1, 0, fps, max_frames)
    return fps


def decode_mp3_timed(mp3, *, ffmpeg, ffprobe, fps, max_frames, timeout):
    """Return gapless PCM WAV and evidence of the MP3-to-PCM sample clock.

    Require contiguous packets and explicit leading skip and trailing discard.
    FFmpeg applies them when decoding the same seekable input. Its native-rate
    WAV must have exactly the expected post-trim sample count and channels.
    """
    fps = _mp3_input(mp3, fps, max_frames)
    with TemporaryDirectory(prefix="af-voice-decode-") as directory:
        root = Path(directory)
        (root / "clip.mp3").write_bytes(mp3)
        probe = _run_local_tool(
            [os.fspath(ffprobe), "-v", "error", "-protocol_whitelist", "file", "-f", "mp3",
             "-select_streams", "a:0", "-show_packets", "-show_streams", "-show_entries",
             "stream=index,codec_name,sample_rate,channels,time_base:packet=stream_index,pts,duration:"
             "packet_side_data=side_data_type,skip_samples,discard_padding,skip_reason,discard_reason",
             "-of", "json", "clip.mp3"],
            cwd=root, timeout=timeout, failure_code="mp3_timing_unproven",
        )
        receipt = _gapless_clock(probe)
        rate, count, channels = receipt["sample_rate"], receipt["sample_count"], receipt["channels"]
        _check_frame_budget(rate, count, fps, max_frames)
        audio, actual = _decode_mp3(root, ffmpeg, fps, max_frames, timeout)
        if actual != (rate, count, channels):
            raise VoiceError("mp3_timing_unproven", rc=3)
    return audio, {
        **receipt,
        "mp3_sha256": sha256(mp3).hexdigest(),
        "decoded_audio_sha256": sha256(audio).hexdigest(),
    }


def decode_mp3_diagnostic(mp3, *, ffmpeg, fps, max_frames, timeout):
    """Decode local audio for envelope and Rhubarb diagnostics without a timing claim."""
    fps = _mp3_input(mp3, fps, max_frames)
    with TemporaryDirectory(prefix="af-voice-decode-") as directory:
        root = Path(directory)
        (root / "clip.mp3").write_bytes(mp3)
        audio, _ = _decode_mp3(root, ffmpeg, fps, max_frames, timeout)
    return audio
